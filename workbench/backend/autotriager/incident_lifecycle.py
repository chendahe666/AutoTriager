"""Operator-approved recovery and incident learning for the local checkout lab."""
import http.client
import json
import os
import re
import sqlite3
import time
from contextlib import closing

from .errors import APIError
from .incident_actions import assess_recovery, draft_mitigation, prevention_recommendations
from .incident_reasoning import diagnose_incident


class LabControl:
    def __init__(self, port, token):
        self.port, self.token = port, token

    @classmethod
    def configured(cls):
        port = os.environ.get("AUTOTRIAGER_LAB_CONTROL_PORT")
        token = os.environ.get("AUTOTRIAGER_LAB_CONTROL_TOKEN")
        if port is None and token is None:
            return None
        if not port or not port.isascii() or not port.isdigit() or not 1024 <= int(port) <= 65535:
            raise ValueError("Configure a valid local lab control port")
        if not token or not re.fullmatch(r"[A-Za-z0-9]{32,128}", token):
            raise ValueError("Configure a valid local lab control token")
        return cls(int(port), token)

    def request(self, path, operation=None):
        client = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        try:
            headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
            client.request("POST" if operation is not None else "GET", path,
                           body=None if operation is None else json.dumps(operation), headers=headers)
            response = client.getresponse()
            if response.status == 409:
                raise APIError(409, "LAB_STATE_CHANGED", "The testbed changed after this proposal was generated.")
            raw = response.read(16_385)
            if response.status != 200 or len(raw) > 16_384:
                raise OSError("Testbed response unavailable")
            return json.loads(raw)
        finally:
            client.close()


class IncidentLifecycle:
    def __init__(self, alerts, control):
        self.alerts, self.control = alerts, control
        with closing(sqlite3.connect(alerts.path)) as db, db:
            for incident_id, raw in db.execute("SELECT incident_id, result FROM incident_actions"):
                result = json.loads(raw)
                if result["status"] in {"applying", "verifying"}:
                    result.update(status="unknown", interruption="Process restarted before the result could be verified.")
                    db.execute("UPDATE incident_actions SET result = ? WHERE incident_id = ?", (json.dumps(result), incident_id))

    def action(self, incident_id):
        with closing(sqlite3.connect(self.alerts.path)) as db:
            row = db.execute("SELECT result FROM incident_actions WHERE incident_id = ?", (incident_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def _save(self, incident_id, result):
        with closing(sqlite3.connect(self.alerts.path)) as db, db:
            db.execute("UPDATE incident_actions SET result = ? WHERE incident_id = ?", (json.dumps(result), incident_id))

    def _diagnose(self, case, action):
        recovery = None
        if action and action["status"] == "verified":
            recovery = {**action["recovery"], "action_receipt": action["action_receipt"], "plan": action["plan"]}
        return diagnose_incident(case, matched_request=case.get("matched_request"), recovery=recovery)

    def describe(self, incident_id, *, include_prevention=True):
        case = self.alerts.detail(incident_id)
        action = self.action(incident_id)
        diagnosis = self._diagnose(case, action)
        plan = action["plan"] if action else None
        if action is None and self.control and case.get("matched_request"):
            try:
                plan = draft_mitigation(incident_id, diagnosis, self.control.request("/lab-control"))
            except (OSError, ValueError, http.client.HTTPException, APIError):
                pass
        routes = self.alerts.routing(incident_id)
        return {"incident_id": incident_id, "routing": routes, "diagnosis": diagnosis, "mitigation": plan,
                "action": action, "prevention": self.prevention() if include_prevention else []}

    def prevention(self):
        records = []
        for case in self.alerts.cases():
            action = self.action(case["id"])
            if action and action["status"] == "verified":
                records.append({"incident_id": case["id"], "diagnosis": self._diagnose(case, action),
                                "recovery": action["recovery"], "action_receipt": action["action_receipt"]})
        return prevention_recommendations(records)

    def execute(self, incident_id, plan_id, actor):
        if not self.control:
            raise APIError(409, "LAB_CONTROL_DISABLED", "The local testbed action is unavailable.")
        case = self.alerts.detail(incident_id)
        prior = self.action(incident_id)
        if prior:
            if prior["plan"]["plan_id"] == plan_id:
                return prior
            raise APIError(409, "PLAN_CHANGED", "Refresh the investigation before approving a new proposal.")
        try:
            state = self.control.request("/lab-control")
        except (OSError, ValueError, http.client.HTTPException) as exc:
            raise APIError(503, "LAB_CONTROL_UNAVAILABLE", "The testbed state could not be read.") from exc
        plan = draft_mitigation(incident_id, self._diagnose(case, None), state)
        if not plan or plan["plan_id"] != plan_id:
            raise APIError(409, "PLAN_CHANGED", "The proposed action no longer matches the testbed state.")
        result = {"status": "applying", "plan": plan, "approved_by": actor, "approved_at_unix": time.time()}
        with closing(sqlite3.connect(self.alerts.path)) as db, db:
            inserted = db.execute("INSERT OR IGNORE INTO incident_actions VALUES (?, ?)",
                                  (incident_id, json.dumps(result))).rowcount
        if not inserted:
            prior = self.action(incident_id)
            if prior["plan"]["plan_id"] == plan_id:
                return prior
            raise APIError(409, "PLAN_CHANGED", "A different approved action already exists.")

        try:
            receipt = self.control.request("/lab-control", plan["action"])
            receipt["plan_id"] = plan_id
            result["action_receipt"] = receipt
            result["status"] = "verifying"
            self._save(incident_id, result)
            samples = {}
            deadline = time.monotonic() + plan["verification"]["max_seconds"]
            while True:
                for probe in self.control.request("/lab-recovery")["samples"]:
                    control = probe.get("control", {})
                    samples[probe["request_id"]] = {**probe, "lab_id": control.get("lab_id"),
                                                   "control_generation": control.get("generation")}
                recovery = assess_recovery(plan, list(samples.values()), applied_at_unix=receipt["applied_at_unix"],
                                           now_unix=time.time())
                if recovery["status"] != "pending" or time.monotonic() >= deadline:
                    break
                time.sleep(0.15)
            result["recovery"] = recovery
            result["status"] = "verified" if recovery["status"] == "verified" else "verification_failed"
        except APIError as exc:
            result["status"] = "rejected" if exc.code == "LAB_STATE_CHANGED" else "unknown"
        except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException):
            result["status"] = "unknown"
        self._save(incident_id, result)
        return result
