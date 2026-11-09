"""Durable, bounded investigation activity over immutable incident evidence.

The model chooses a server-defined read-only check. Only actual tool receipts
become completed activity; analysis and recorded recovery remain separate.
"""
from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import json
import threading
import uuid

from .conversation import canonical, stamp
from .errors import APIError
from .incident_agent_context import load_incident_agent_context, select_incident_agent_context
from .investigation_evidence import build_check_catalog, execute_check


class IncidentWorkflow:
    def __init__(self, app):
        self.app = app
        self.lock = threading.RLock()
        self.closed = False
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="incident-workflow")
        with app.store._db(write=True) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS incident_workflows (
                actor TEXT NOT NULL, incident_id TEXT NOT NULL, state TEXT NOT NULL,
                PRIMARY KEY(actor, incident_id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS incident_workflow_requests (
                actor TEXT NOT NULL, incident_id TEXT NOT NULL, request_key TEXT NOT NULL,
                operation TEXT NOT NULL, payload TEXT NOT NULL, result TEXT NOT NULL,
                PRIMARY KEY(actor, incident_id, request_key))""")
            for row in db.execute("SELECT * FROM incident_workflows").fetchall():
                state = json.loads(row["state"])
                if state["status"] == "running":
                    state.update(status="failed", active_turn_id=None)
                    for check in state["checks"]:
                        if check["status"] == "running":
                            check.update(status="failed", result_summary="Interrupted by a restart; not retried.")
                    self._event(state, "interrupted", "Investigation interrupted", "Workbench restarted. Completed receipts were retained; unfinished work was not retried.")
                    db.execute("UPDATE incident_workflows SET state=? WHERE actor=? AND incident_id=?",
                               (canonical(state), row["actor"], row["incident_id"]))

    @staticmethod
    def _event(state, kind, title, summary, **extra):
        state["events"].append({"id": "activity_" + uuid.uuid4().hex, "kind": kind, "title": title,
                                "at": stamp(), "summary": summary, "evidence_ids": [], **extra})

    def _read(self, actor, incident_id):
        with self.app.store._db() as db:
            row = db.execute("SELECT state FROM incident_workflows WHERE actor=? AND incident_id=?", (actor, incident_id)).fetchone()
        return json.loads(row["state"]) if row else None

    def _save(self, actor, state):
        state["revision"] += 1
        with self.app.store._db(write=True) as db:
            db.execute("INSERT OR REPLACE INTO incident_workflows VALUES (?,?,?)", (actor, state["incident_id"], canonical(state)))

    def _state(self, actor, incident_id):
        state = self._read(actor, incident_id)
        if state is not None:
            if any("verification_status" not in h for h in state["hypotheses"]):
                state["hypotheses"] = [self._unverified_hypothesis(h) for h in state["hypotheses"]]
                if state["status"] == "waiting_for_review":
                    state["summary"]["next_step"] = self._review_step(state)
                self._event(state, "policy_refinement", "Model support is not independent verification",
                            "Generated explanations remain unverified candidates. Original model assessments are retained separately.")
                self._save(actor, state)
            return state
        context = load_incident_agent_context(self.app, incident_id)
        scope = context["scope"]
        state = {"incident_id": incident_id, "revision": 0, "status": "idle", "active_turn_id": None,
                 "summary": {key: scope[key] for key in ("title", "source", "start", "end")},
                 "scope": scope, "context_run_id": context["context_run_id"],
                 "hypotheses": [], "checks": [], "evidence": context["evidence"], "events": [],
                 "recovery": {"status": "not_checked", "summary": "Later observations have not been checked.",
                              "evidence_ids": [], "executed_action": False},
                 "cause": {"status": "unconfirmed", "summary": "A unique root cause has not been verified."}}
        state["summary"].update(impact="Recorded latency and service observations; user or revenue impact has not been measured.",
                                focus="Compare plausible explanations against source records.", next_step="Start a bounded read-only investigation.")
        state["checks"] = [{**spec, "status": "proposed", "evidence_ids": [], "limitations": []}
                           for spec in build_check_catalog(self.app, incident_id, context)]
        self._event(state, "opened", "Evidence snapshot opened", "Recorded event times remain separate from this investigation's execution times.")
        self._save(actor, state)
        return state

    @staticmethod
    def _unverified_hypothesis(hypothesis):
        return {**hypothesis, "model_assessment": hypothesis.get("model_assessment", hypothesis["status"]),
                "status": "candidate", "verification_status": "unverified",
                "verification_note": "Reference existence and consistent observations do not validate a causal mechanism."}

    @staticmethod
    def _review_step(state):
        count = sum(c["status"] == "proposed" and not c["verification_only"] for c in state["checks"])
        return (f"Review the completed evidence. {count} read-only checks remain available; continue or select one if useful. "
                "Each pass is limited to two checks. The root cause remains unconfirmed.")

    def _capabilities(self):
        agent = getattr(self.app, "incident_agent", None)
        provider = getattr(agent, "provider", None)
        if provider is None or not hasattr(provider, "plan_check"):
            return {"enabled": False, "disabled_reason": "The bounded investigation model is unavailable."}
        cap = provider.capabilities()
        return {"enabled": cap["enabled"], "disabled_reason": cap["disabled_reason"]}

    def get(self, actor, incident_id):
        with self.lock:
            return {**self._state(actor, incident_id), **self._capabilities()}

    def enrich(self, actor, incident_id, context):
        """Make completed check receipts available to chat, without executing work."""
        with self.lock:
            state = self._read(actor, incident_id)
        if not state:
            return context
        known = {r["evidence_id"] for r in context["evidence"]}
        extra = [r for r in state["evidence"] if r["evidence_id"] not in known]
        return {**context, "evidence": [*extra, *context["evidence"]],
                "evidence_ids": [r["evidence_id"] for r in [*extra, *context["evidence"]]],
                "scope": {**context["scope"], "recorded_evidence_count": len(known) + len(extra),
                          "evidence_loading_note": "The initial snapshot excludes log bodies; completed log checks may supply bounded original excerpts.",
                          "completed_checks": [{"title": c["title"], "result": c.get("result_summary"),
                                                "evidence_ids": c["evidence_ids"]} for c in state["checks"]
                                               if c["status"] in {"completed", "insufficient"}]}}

    def _request(self, actor, incident_id, key, operation, payload):
        if not isinstance(key, str) or not 1 <= len(key) <= 128 or any(not 33 <= ord(ch) <= 126 for ch in key):
            raise APIError(400, "INVALID_REQUEST", "A valid request key is required.")
        with self.app.store._db() as db:
            row = db.execute("SELECT * FROM incident_workflow_requests WHERE actor=? AND incident_id=? AND request_key=?",
                             (actor, incident_id, key)).fetchone()
        if row:
            if row["operation"] != operation or row["payload"] != canonical(payload):
                raise APIError(409, "IDEMPOTENCY_CONFLICT", "This request key belongs to a different operation.")
            return json.loads(row["result"])

    def _remember(self, actor, incident_id, key, operation, payload, result):
        with self.app.store._db(write=True) as db:
            db.execute("INSERT INTO incident_workflow_requests VALUES (?,?,?,?,?,?)",
                       (actor, incident_id, key, operation, canonical(payload), canonical(result)))

    def _idle(self, state):
        if self.closed or state["status"] == "running":
            raise APIError(409, "WORKFLOW_BUSY", "Wait for this investigation to finish before starting another check.")

    def submit(self, actor, incident_id, body, key):
        question = body.get("question") if isinstance(body, dict) else None
        if not isinstance(question, str) or set(body) != {"question"} or not question.strip() or len(question) > 4000:
            raise APIError(400, "INVALID_QUESTION", "Enter a question of 1–4,000 characters.")
        with self.lock:
            prior = self._request(actor, incident_id, key, "continue", body)
            if prior:
                return prior
            state = self._state(actor, incident_id)
            self._idle(state)
            cap = self._capabilities()
            if not cap["enabled"]:
                raise APIError(409, "AGENT_DISABLED", cap["disabled_reason"])
            turn = {"turn_id": "workflow_" + uuid.uuid4().hex, "status": "QUEUED"}
            state.update(status="running", active_turn_id=turn["turn_id"])
            self._event(state, "requested", "Investigation requested", question.strip(), actor=actor)
            self._save(actor, state)
            self._remember(actor, incident_id, key, "continue", body, turn)
            self.executor.submit(self._run, actor, incident_id, question.strip(), turn["turn_id"])
            return turn

    @staticmethod
    def _model_context(state, question):
        diagnostic = [r for r in state["evidence"] if not (
            isinstance(r.get("derivation"), dict) and r["derivation"].get("verification_only"))]
        base = {"incident_id": state["incident_id"], "context_run_id": state["context_run_id"],
                "scope": state["scope"], "evidence": diagnostic,
                "evidence_ids": [r["evidence_id"] for r in diagnostic]}
        selected = select_incident_agent_context(base, question)
        # New tool receipts take priority, but total prompt evidence stays bounded.
        receipts = [r for r in diagnostic if isinstance(r.get("derivation"), dict) and r["derivation"].get("check_id")]
        rows, seen, used = [], set(), 0
        for row in [*reversed(receipts), *selected["evidence"]]:
            size = len(canonical(row).encode("utf-8"))
            if row["evidence_id"] in seen or used + size > 24000 or len(rows) >= 24:
                continue
            rows.append(row)
            seen.add(row["evidence_id"])
            used += size
        # The complete graph/directory remains in the workbench. Model calls use
        # check descriptors plus actual cited rows, not duplicate unloaded hints.
        scope = {k: v for k, v in selected["scope"].items() if k not in {
            "evidence_directory", "observed_cross_service_edges", "previous_candidate_hypotheses"}}
        return {**selected, "evidence": rows, "evidence_ids": [r["evidence_id"] for r in rows],
                "scope": {**scope, "selected_count": len(rows),
                          "evidence_loading_note": "Initial log records are counts. Completed log checks supply original excerpts with source coordinates.",
                          "recorded_evidence_count": len(diagnostic), "sampled": len(rows) < len(diagnostic)}}

    def _perform(self, actor, state, check, selected_by):
        check.update(status="running", started_at=stamp(), selected_by=selected_by)
        self._event(state, "check_started", check["title"], check["purpose"], check_id=check["check_id"],
                    tool=check["kind"], arguments=check.get("parameters", {}), actor=selected_by)
        self._save(actor, state)
        try:
            result = execute_check(self.app, state["incident_id"], check)
            if result.get("check_id") != check["check_id"] or result.get("status") not in {"completed", "insufficient"}:
                raise ValueError("Invalid tool receipt")
            old = {r["evidence_id"]: r for r in state["evidence"]}
            for row in result["evidence"]:
                if row["evidence_id"] in old and canonical(old[row["evidence_id"]]) != canonical(row):
                    raise ValueError("Evidence ID changed content")
                old[row["evidence_id"]] = row
            state["evidence"] = list(old.values())
            check.update(status=result["status"], completed_at=stamp(), result_summary=result["summary"],
                         evidence_ids=[r["evidence_id"] for r in result["evidence"]],
                         limitations=result.get("limitations", []), observed_window=result.get("observed_window"),
                         receipt_sha256=hashlib.sha256(canonical(result).encode()).hexdigest())
            self._event(state, "check_finished", check["title"], result["summary"],
                        check_id=check["check_id"], evidence_ids=check["evidence_ids"],
                        status=result["status"], receipt_sha256=check["receipt_sha256"])
            if check["verification_only"]:
                state["recovery"] = {**result.get("recovery_assessment", {"status": "insufficient_evidence", "summary": "Recovery was not established."}),
                                     "evidence_ids": check["evidence_ids"], "executed_action": False}
        except Exception:
            check.update(status="failed", completed_at=stamp(), result_summary="The check could not produce a valid source receipt; no retry was performed.")
            self._event(state, "check_failed", check["title"], check["result_summary"], check_id=check["check_id"])
            self._save(actor, state)
            raise
        self._save(actor, state)

    def _run(self, actor, incident_id, question, turn_id):
        try:
            for step in range(3):
                with self.lock:
                    state = self._state(actor, incident_id)
                    context = self._model_context(state, question)
                    self._event(state, "analysis_started", "Review evidence and alternatives", "Selecting from available read-only checks.",
                                model_call_id=f"{turn_id}_{step}", evidence_ids=context["evidence_ids"],
                                context_sha256=hashlib.sha256(canonical(context).encode()).hexdigest())
                    self._save(actor, state)
                proposal = self.app.incident_agent.provider.plan_check(context, copy.deepcopy(state), question,
                                                                     f"{turn_id}_{step}", allow_checks=step < 2)
                with self.lock:
                    state = self._state(actor, incident_id)
                    state["hypotheses"] = [self._unverified_hypothesis({"hypothesis_id": "hyp_" + hashlib.sha256(h["title"].casefold().encode()).hexdigest()[:16], **h})
                                           for h in proposal["hypotheses"]]
                    state["summary"].update(focus=proposal["focus"], next_step=proposal["selection_reason"])
                    state["last_answer"] = {k: proposal[k] for k in ("text", "citations", "limitations", "next_checks")}
                    self._event(state, "analysis_completed", "Evidence assessment updated", proposal["text"],
                                evidence_ids=[r["evidence_id"] for r in proposal["citations"]],
                                hypotheses=copy.deepcopy(state["hypotheses"]), raw_model_hypotheses=copy.deepcopy(proposal["hypotheses"]),
                                interpretation_status="unverified_model_assessment", model_call_id=f"{turn_id}_{step}")
                    self._save(actor, state)
                    check = next((c for c in state["checks"] if c["check_id"] == proposal["next_check_id"]), None)
                    if proposal["next_check_id"] == "NONE":
                        break
                    if step == 2 or check is None or check["verification_only"] or check["status"] != "proposed":
                        raise ValueError("The proposed check is not currently permitted")
                    check["selection_reason"] = proposal["selection_reason"]
                    self._perform(actor, state, check, "agent")
            with self.lock:
                state = self._state(actor, incident_id)
                state.update(status="waiting_for_review", active_turn_id=None)
                state["summary"]["next_step"] = self._review_step(state)
                self._event(state, "review_requested", "Ready for evidence review", "Completed checks support observations. A unique cause and successful remediation remain unverified.")
                self._save(actor, state)
        except Exception as exc:
            with self.lock:
                state = self._state(actor, incident_id)
                state.update(status="failed", active_turn_id=None)
                code = getattr(exc, "code", type(exc).__name__)
                detail = getattr(exc, "message", "A local workflow or source check failed.")
                diagnostic = getattr(exc, "diagnostic", None)
                self._event(state, "failed", "Investigation stopped", f"{code}: {detail} Completed receipts retained; no automatic retry or recovery action.",
                            **({"rejected_model_output": diagnostic} if diagnostic else {}))
                self._save(actor, state)

    def run_check(self, actor, incident_id, check_id, key, *, verify=False):
        operation = "verify" if verify else "check"
        with self.lock:
            prior = self._request(actor, incident_id, key, operation, {"check_id": check_id})
            if prior:
                return self.get(actor, incident_id)
            state = self._state(actor, incident_id)
            self._idle(state)
            check = next((c for c in state["checks"] if (c["verification_only"] if verify else c["check_id"] == check_id)), None)
            if check is None or check["verification_only"] != verify:
                raise APIError(404, "CHECK_NOT_FOUND", "This check is not available for this incident.")
            if check["status"] != "proposed":
                return self.get(actor, incident_id)
            self._perform(actor, state, check, actor)
            self._remember(actor, incident_id, key, operation, {"check_id": check_id}, {"check_id": check["check_id"]})
            return self.get(actor, incident_id)

    def close(self):
        with self.lock:
            self.closed = True
        self.executor.shutdown(wait=True)
