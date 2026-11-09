"""Capture actual Prometheus firing alerts as immutable local investigations."""
import hashlib
import http.client
import json
import math
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event, Thread

from .errors import APIError
from .incident_reasoning import route_alert
from .monitoring_prometheus import PREFIX, read_api, read_source


def _time(value):
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError("Alert time must include an offset")
    return parsed.astimezone(timezone.utc)


def _request_evidence(path, source, value, observed):
    """Join the lab's measured request only when its exported value uniquely matches."""
    if path is None:
        return [], None
    try:
        with path.open("rb") as stream:
            length = stream.seek(0, 2)
            stream.seek(max(0, length - 65536))
            if length > 65536:
                stream.readline()
            lines = stream.read(65536).splitlines()
    except OSError:
        return [], None
    matches = []
    for line in reversed(lines):
        try:
            row = json.loads(line)
            seconds = observed.timestamp() - row["observed_at_unix"]
            if seconds > 3:
                break
            if (0 <= seconds <= 3 and row["service"] == source.service
                    and math.isclose(row["latency_ms"], value, rel_tol=0, abs_tol=1e-8)
                    and re.fullmatch(r"[0-9a-f]{32}", row["request_id"])):
                matches.append(row)
        except (ValueError, KeyError, TypeError):
            continue
    if len(matches) != 1:
        return [], None
    row = matches[0]
    dependency_ms, status = row.get("dependency_latency_ms"), row.get("http_status")
    if (row.get("dependency") != "payment" or type(dependency_ms) not in (int, float)
            or not math.isfinite(dependency_ms) or dependency_ms < 0 or type(status) is not int or not 100 <= status <= 599):
        return [], None
    fact = {key: row[key] for key in ("request_id", "service", "observed_at_unix", "latency_ms", "dependency", "dependency_latency_ms", "http_status")}
    fact.update({key: row[key] for key in ("route", "dependency_route") if key in row})
    control = row.get("control")
    if (isinstance(control, dict) and re.fullmatch(r"[0-9a-f]{32}", str(control.get("lab_id")))
            and type(control.get("generation")) is int and type(control.get("delay_ms")) is int):
        fact.update(lab_id=control["lab_id"], control_generation=control["generation"], configured_delay_ms=control["delay_ms"])
    return [{"id": "lab-request", "title": "Measured checkout request · local testbed",
             "detail": f"Request {row['request_id']}; HTTP {status}; checkout {value:.6f} ms; "
                       f"payment call {dependency_ms:.6f} ms. Exact exported value matched the rule evaluation."}], fact


def _read_cases(source, name, request_log=None):
    data = read_api(source, "/api/v1/rules", {"type": "alert", "rule_name[]": name})
    now = datetime.now(timezone.utc)
    rules = [rule for group in data["groups"] for rule in group["rules"]
             if rule["name"] == name and rule["type"] == "alerting"]
    if len(rules) != 1:
        raise ValueError("Configured alert rule is missing or ambiguous")
    rule = rules[0]
    query, operator, limit = rule["query"].rpartition(" > ")
    if (not operator or query != source.query or float(limit) != source.threshold
            or rule["duration"] != 0 or rule["health"] != "ok"):
        raise ValueError("Alert rule does not match the configured metric and threshold")

    for alert in rule["alerts"]:
        labels = alert["labels"]
        if alert["state"] != "firing" or labels.get("service") != source.service or labels.get("alertname") != name:
            continue
        value = float(alert["value"])
        triggered = _time(alert["activeAt"])
        observed = _time(rule.get("lastEvaluation") or alert["activeAt"])
        if not math.isfinite(value) or value <= source.threshold or not triggered <= observed <= now:
            continue
        identity = [source.host, source.port, rule["query"], sorted(labels.items()), alert["activeAt"]]
        incident_id = "prom-" + hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:24]
        start, end = observed - timedelta(seconds=source.step), observed
        timestamp = triggered.isoformat()
        reading = {"service": source.service, "metric": source.metric, "unit": source.unit,
                   "value": value, "threshold": source.threshold, "start": start.isoformat(),
                   "end": end.isoformat(), "source": "Prometheus firing rule · /api/v1/rules",
                   "observed_at": observed.isoformat()}
        request_evidence, fact = _request_evidence(request_log, source, value, observed)
        control_identity = [fact.get("lab_id"), fact.get("control_generation")] if fact else [None, None]
        yield {
            "id": incident_id, "title": f"{source.service} · {source.metric}", "source": "live",
            "triggered_at": timestamp, "start": start.isoformat(), "end": end.isoformat(),
            "metric_evidence": reading, "candidates": [],
            "alert_event": {"event_id": incident_id, "source": "prometheus", "scope": f"{source.host}:{source.port}",
                            "service": source.service, "rule": name, "target": json.dumps([sorted(labels.items()), control_identity]),
                            "triggered_at": timestamp, "suppressed": rule.get("labels", {}).get("autotriager_suppress") == "true"},
            "matched_request": fact,
            "evidence": [
                {"id": "prom-metric", "title": "Metric at rule evaluation",
                 "detail": f"{source.query} returned {value:.6g} {source.unit} at {observed.isoformat()}, above {source.threshold:.6g} {source.unit}."},
                {"id": "prom-rule", "title": "Prometheus firing receipt",
                 "detail": f"{name} fired at {timestamp}. Expression: {rule['query']}. Received at {now.isoformat()}."},
                *request_evidence,
            ],
            "limits": ["The alert identifies the observed symptom. Root-cause ranking requires additional evidence."],
        }


class LiveAlerts:
    def __init__(self, db_path, source, name, request_log=None, enable_baro=False):
        self.path = db_path.with_name(db_path.name + ".live-alerts.sqlite3")
        self.source, self.name = source, name
        self.request_log = request_log
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS alerts (
                id TEXT PRIMARY KEY, triggered_at TEXT NOT NULL, snapshot TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS alert_routes (
                event_id TEXT PRIMARY KEY, incident_id TEXT, triggered_at TEXT NOT NULL, receipt TEXT NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS alert_routes_incident ON alert_routes(incident_id)")
            db.execute("""CREATE TABLE IF NOT EXISTS incident_actions (
                incident_id TEXT PRIMARY KEY, result TEXT NOT NULL)""")
        self.baro = None
        if enable_baro:
            from .live_baro import LiveBaro
            self.baro = LiveBaro(self.path, request_log)
        self.stop = Event()
        self.status = "connecting"
        self.worker = Thread(target=self._poll, name="prometheus-alerts", daemon=True)
        self.worker.start()

    @classmethod
    def configured(cls, db_path):
        name = os.environ.get(PREFIX + "ALERT_RULE", "").strip()
        if not name:
            return None
        source = read_source()
        if source is None or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,119}", name):
            raise ValueError("Configure a valid local Prometheus source and alert rule")
        evidence = os.environ.get(PREFIX + "LAB_REQUEST_LOG", "").strip()
        enable_baro = os.environ.get("AUTOTRIAGER_LIVE_BARO") == "1"
        if enable_baro and (not evidence or source.service != "checkout"):
            raise ValueError("Live BARO requires the checkout probe journal")
        return cls(db_path, source, name, Path(evidence) if evidence else None, enable_baro=enable_baro)

    def _poll(self):
        while not self.stop.is_set():
            try:
                cases = list(_read_cases(self.source, self.name, self.request_log))
                if cases:
                    with closing(sqlite3.connect(self.path)) as db, db:
                        db.execute("BEGIN IMMEDIATE")
                        for case in cases:
                            self._record(db, case)
                self.status = "connected"
            except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException, sqlite3.Error):
                # Preserve the existing receipts during a source outage or rule mismatch.
                self.status = "unavailable"
            if self.baro:
                try:
                    self.baro.capture_ready()
                except sqlite3.Error:
                    # The next poll can retry a concurrent or temporarily unavailable database.
                    pass
            self.stop.wait(1)

    def _record(self, db, case):
        event = case["alert_event"]
        if db.execute("SELECT 1 FROM alert_routes WHERE event_id = ?", (event["event_id"],)).fetchone():
            return
        if db.execute("SELECT 1 FROM alerts WHERE id = ?", (case["id"],)).fetchone():
            return  # Older snapshots already retain this firing occurrence.
        rows = db.execute("""SELECT a.snapshot, coalesce(max(r.triggered_at), a.triggered_at), ia.result
            FROM alerts a LEFT JOIN alert_routes r ON r.incident_id = a.id
            LEFT JOIN incident_actions ia ON ia.incident_id = a.id
            GROUP BY a.id ORDER BY a.triggered_at DESC LIMIT 100""").fetchall()
        incidents = []
        for snapshot, last_event, action in rows:
            previous = json.loads(snapshot)
            if "alert_event" not in previous:
                continue
            state = "closed" if action and json.loads(action)["status"] == "verified" else "open"
            incidents.append({**previous["alert_event"], "incident_id": previous["id"], "state": state,
                              "last_event_at": last_event, "event_ids": [previous["id"]]})
        decision = route_alert(event, incidents)
        incident_id = case["id"] if decision["decision"] == "new" else decision.get("incident_id")
        receipt = {**decision, "incident_id": incident_id, "event_id": event["event_id"],
                   "triggered_at": event["triggered_at"], "service": event["service"], "rule": event["rule"]}
        if decision["decision"] == "link":
            receipt.update(metric_evidence=case["metric_evidence"], evidence=case["evidence"])
        db.execute("INSERT INTO alert_routes VALUES (?, ?, ?, ?)",
                   (event["event_id"], incident_id, event["triggered_at"], json.dumps(receipt)))
        if decision["decision"] == "new":
            db.execute("INSERT INTO alerts VALUES (?, ?, ?)", (case["id"], case["triggered_at"], json.dumps(case)))

    def routing(self, incident_id):
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute("SELECT receipt FROM alert_routes WHERE incident_id = ? ORDER BY triggered_at, event_id",
                              (incident_id,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def close(self):
        self.stop.set()
        self.worker.join(timeout=4)

    def cases(self):
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute("SELECT snapshot FROM alerts ORDER BY triggered_at DESC LIMIT 100").fetchall()
        return [json.loads(row[0]) for row in rows]

    def queue(self):
        fields = ("id", "title", "start", "end", "source", "triggered_at")
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute("""
                SELECT alerts.snapshot,
                       CASE WHEN json_extract(incident_actions.result, '$.status') = 'verified'
                            THEN 1 ELSE 0 END AS verified
                FROM alerts LEFT JOIN incident_actions ON incident_actions.incident_id = alerts.id
                ORDER BY verified, alerts.triggered_at DESC, alerts.id
                LIMIT 100
            """).fetchall()
        items = []
        for raw_case, verified in rows:
            case = json.loads(raw_case)
            items.append({**{field: case[field] for field in fields},
                          "resolution": "resolved" if verified else "unresolved"})
        return items

    def summaries(self):
        return [{**{key: case["metric_evidence"][key] for key in ("service", "metric", "value", "threshold", "unit")},
                 "incident_id": case["id"], "triggered_at": case["triggered_at"]} for case in self.cases()[:20]]

    def detail(self, incident_id):
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute("SELECT snapshot FROM alerts WHERE id = ?", (incident_id,)).fetchone()
        if row is None:
            raise APIError(404, "TRIAGE_CASE_NOT_FOUND", "This alert is unavailable.")
        return json.loads(row[0])
