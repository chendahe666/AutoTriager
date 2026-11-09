"""Rank an observed local checkout alert with the pinned Metric BARO core."""

from contextlib import closing
from datetime import UTC, datetime
import json
import math
import re
import sqlite3
import time

from .contracts import ROOT
from .errors import APIError

REFERENCE_SECONDS = 25
GUARD_SECONDS = 5
INCIDENT_SECONDS = 10
MIN_SAMPLES = 8
JOURNAL_TAIL_BYTES = 256 * 1024
SIGNALS = {"checkout_latency": ("checkout", "latency_ms"),
           "payment_latency": ("payment", "dependency_latency_ms")}


def _stamp(seconds):
    return datetime.fromtimestamp(seconds, UTC).isoformat()


def _trigger(case):
    return datetime.fromisoformat(case["triggered_at"]).timestamp()


def read_samples(journal, case, end):
    matched = case.get("matched_request") or {}
    lab_id = matched.get("lab_id")
    if not isinstance(lab_id, str) or not re.fullmatch(r"[a-f0-9]{32}", lab_id):
        return []
    with journal.open("rb") as stream:
        length = stream.seek(0, 2)
        stream.seek(max(0, length - JOURNAL_TAIL_BYTES))
        if length > JOURNAL_TAIL_BYTES:
            stream.readline()
        payload = stream.read(JOURNAL_TAIL_BYTES)
    onset = _trigger(case)
    rows = {}
    for line in payload.split(b"\n")[:-1]:
        try:
            row = json.loads(line)
            at, request_id = row["observed_at_unix"], row["request_id"]
            if (not re.fullmatch(r"[a-f0-9]{32}", request_id) or row["control"]["lab_id"] != lab_id
                    or row["service"] != "checkout" or row["dependency"] != "payment"
                    or row["route"] != "/checkout" or row["dependency_route"] != "/authorize"
                    or not (onset - REFERENCE_SECONDS <= at < onset - GUARD_SECONDS or onset <= at < end)
                    or any(type(row[key]) not in (float, int) or not math.isfinite(row[key]) or row[key] < 0
                           for _, key in SIGNALS.values())):
                continue
            rows[request_id] = {key: row[key] for key in ("request_id", "observed_at_unix", "service", "dependency",
                                                       "route", "dependency_route", "latency_ms", "dependency_latency_ms", "http_status")}
        except (ValueError, TypeError, KeyError):
            continue
    return sorted(rows.values(), key=lambda row: (row["observed_at_unix"], row["request_id"]))


def rank_samples(samples, onset, core, dataframe):
    """Use only measured latency columns and their shared sample times."""
    frame = dataframe([{"time": row["observed_at_unix"], **{name: row[field] for name, (_, field) in SIGNALS.items()}}
                       for row in samples])
    result = core(frame, inject_time=onset, dataset="local-checkout-lab", dk_select_useful=False)
    return [(name, result["scores"][name]) for name in result["ranks"] if name in SIGNALS]


class LiveBaro:
    def __init__(self, path, journal):
        from pandas import DataFrame
        from tools.rcaeval_baro import PIN, upstream_core

        self.path, self.journal = path, journal
        self.pin, self.core, self.dataframe = PIN, upstream_core(ROOT / ".vendor/rcaeval"), DataFrame
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS live_baro_runs (incident_id TEXT PRIMARY KEY, result TEXT NOT NULL)")

    def capture_ready(self):
        with closing(sqlite3.connect(self.path)) as db:
            pending = db.execute("""
                SELECT alerts.snapshot, incident_actions.result
                FROM alerts LEFT JOIN live_baro_runs ON live_baro_runs.incident_id = alerts.id
                LEFT JOIN incident_actions ON incident_actions.incident_id = alerts.id
                WHERE live_baro_runs.incident_id IS NULL ORDER BY alerts.triggered_at DESC LIMIT 100
            """).fetchall()
        now = time.time()
        for raw_case, raw_action in pending:
            case = json.loads(raw_case)
            onset = _trigger(case)
            if now < onset + INCIDENT_SECONDS:
                continue
            action = json.loads(raw_action) if raw_action else {}
            end = min(onset + INCIDENT_SECONDS, max(onset, action.get("approved_at_unix", now)))
            run = self._analyze(case, onset, end, now)
            with closing(sqlite3.connect(self.path)) as db, db:
                db.execute("INSERT OR IGNORE INTO live_baro_runs VALUES (?, ?)",
                           (case["id"], json.dumps(run, allow_nan=False)))

    def _analyze(self, case, onset, end, now):
        window = {"reference_start": _stamp(onset - REFERENCE_SECONDS), "reference_end": _stamp(onset - GUARD_SECONDS),
                  "incident_start": _stamp(onset), "incident_end": _stamp(end)}
        status = {"status": "insufficient", "method": "Metric BARO", "source_commit": self.pin,
                  "input_window": window, "analyzed_at": _stamp(now), "ranking": [], "evidence": []}
        run = {"view": status, "records": {}}
        try:
            samples = read_samples(self.journal, case, end)
        except OSError:
            samples = []
        before = [row for row in samples if row["observed_at_unix"] < onset]
        after = [row for row in samples if row["observed_at_unix"] >= onset]
        status.update(reference_samples=len(before), incident_samples=len(after))
        if min(len(before), len(after)) < MIN_SAMPLES:
            status["message"] = "At least eight observed checkout and payment sample pairs are required in each window."
            return run
        try:
            ranks = rank_samples(samples, onset, self.core, self.dataframe)
        except (ValueError, TypeError, ArithmeticError):
            ranks = []
        if not ranks or any(not math.isfinite(score) for _, score in ranks):
            status["message"] = "BARO could not score the measured latency series in these windows."
            return run

        status["status"] = "completed"
        for indicator, score in ranks:
            service, field = SIGNALS[indicator]
            key = f"live-baro-{service}"
            peak = max(after, key=lambda row: row[field])
            status["ranking"].append({"service": service, "indicator": indicator, "score": score, "evidence_id": key})
            status["evidence"].append({"id": key, "kind": "metric", "title": f"{service} · observed HTTP latency",
                                       "detail": f"BARO score {score:.4g} from {len(before)} reference and {len(after)} incident samples. "
                                                 f"Largest observed incident value {peak[field]:.2f} ms.",
                                       "observed_at": _stamp(peak["observed_at_unix"])})
            run["records"][key] = {"case_id": case["id"], "evidence_id": key, "modality": "metrics", "metric_column": field,
                                   "source": "Local checkout probe journal", "unit": "ms", "input_window": window,
                                   "rows": [{"window": "reference" if row["observed_at_unix"] < onset else "incident",
                                             "record": row} for row in samples]}
        return run

    def _saved(self, incident_id):
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute("SELECT result FROM live_baro_runs WHERE incident_id = ?", (incident_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def view(self, incident_id):
        saved = self._saved(incident_id)
        return saved["view"] if saved else {"status": "waiting", "method": "Metric BARO", "evidence": []}

    def evidence(self, incident_id, evidence_id):
        saved = self._saved(incident_id)
        record = saved["records"].get(evidence_id) if saved else None
        if record is None:
            raise APIError(404, "BARO_EVIDENCE_NOT_FOUND", "This live BARO evidence is unavailable.")
        return record
