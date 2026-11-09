"""Local incident conversations, bound to one incident and its recorded evidence."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import threading
import uuid

from .conversation import ConversationFailure, canonical, stamp
from .errors import APIError
from .incident_agent_context import (
    describe_incident_agent_context, load_incident_agent_context, select_incident_agent_context,
)


class IncidentAgent:
    def __init__(self, app, provider=None):
        self.app, self.provider = app, provider
        self.lock = threading.Lock()
        self.closed = False
        with app.store._db(write=True) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS incident_agent_turns (
                sequence INTEGER PRIMARY KEY, turn_id TEXT NOT NULL UNIQUE,
                actor_scope TEXT NOT NULL, incident_id TEXT NOT NULL, request_key TEXT NOT NULL,
                question TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('QUEUED','RUNNING','COMPLETED','FAILED')),
                created_at TEXT NOT NULL, context_digest TEXT, context_json TEXT,
                answer_json TEXT, error_json TEXT,
                UNIQUE(actor_scope, incident_id, request_key),
                CHECK((status='COMPLETED' AND answer_json IS NOT NULL AND error_json IS NULL)
                    OR (status='FAILED' AND answer_json IS NULL AND error_json IS NOT NULL)
                    OR (status IN ('QUEUED','RUNNING') AND answer_json IS NULL AND error_json IS NULL)))""")
            db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS incident_agent_one_active
                ON incident_agent_turns(actor_scope, incident_id) WHERE status IN ('QUEUED', 'RUNNING')""")
            db.execute("""UPDATE incident_agent_turns SET status='FAILED', error_json=?
                WHERE status IN ('QUEUED', 'RUNNING')""", (canonical({"code": "PROCESS_RESTARTED",
                    "message": "The workbench restarted before this reply finished."}),))
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="incident-agent")

    @staticmethod
    def _public(row):
        answer = json.loads(row["answer_json"]) if row["answer_json"] else None
        if answer and answer["citations"] and row["context_json"]:
            records = {item["evidence_id"]: item["record"] for item in json.loads(row["context_json"])["evidence"]}
            for citation in answer["citations"]:
                record = records.get(citation["evidence_id"], {})
                detail = citation["detail"]
                if "median child duration" not in detail:
                    for name, field in (("Reference", "reference_median_ms"), ("Incident", "incident_median_ms")):
                        value = record.get(field)
                        if type(value) in (int, float):
                            detail += f" {name} median: {value:.6g} ms."
                if record.get("source"):
                    detail += f"\nSource: {record['source']}"
                citation["detail"] = detail
        return {"turn_id": row["turn_id"], "incident_id": row["incident_id"],
                "question": row["question"], "status": row["status"], "created_at": row["created_at"],
                "answer": answer,
                "error": json.loads(row["error_json"]) if row["error_json"] else None}

    def _capabilities(self):
        if self.provider is None:
            return {"enabled": False, "disabled_reason": "The Agent model connection is unavailable."}
        status = self.provider.capabilities()
        return {"enabled": status["enabled"], "disabled_reason": status["disabled_reason"]}

    def history(self, actor, incident_id):
        context = describe_incident_agent_context(self.app, incident_id)
        with self.app.store._db() as db:
            rows = db.execute("""SELECT * FROM incident_agent_turns WHERE actor_scope=? AND incident_id=?
                ORDER BY sequence DESC LIMIT 101""", (actor, incident_id)).fetchall()
        return {"incident_id": incident_id, "context": context, **self._capabilities(),
                "turns": [self._public(row) for row in reversed(rows[:100])], "truncated": len(rows) > 100}

    def submit(self, actor, incident_id, body, key):
        question = body.get("question") if isinstance(body, dict) else None
        if not isinstance(question, str) or set(body) != {"question"} or not question.strip() or len(question) > 4000:
            raise APIError(400, "INVALID_QUESTION", "Enter a question of 1–4,000 characters.")
        if not isinstance(key, str) or not 1 <= len(key) <= 128 or any(not 33 <= ord(ch) <= 126 for ch in key):
            raise APIError(400, "INVALID_REQUEST", "A valid request key is required.")
        question = question.strip()
        describe_incident_agent_context(self.app, incident_id)
        with self.lock:
            if self.closed:
                raise APIError(503, "AGENT_CLOSED", "The Agent is shutting down.")
            with self.app.store._db(write=True) as db:
                previous = db.execute("""SELECT * FROM incident_agent_turns WHERE actor_scope=? AND
                    incident_id=? AND request_key=?""", (actor, incident_id, key)).fetchone()
                if previous:
                    if previous["question"] != question:
                        raise APIError(409, "IDEMPOTENCY_CONFLICT", "This request key belongs to a different question.")
                    return self._public(previous)
                if db.execute("""SELECT 1 FROM incident_agent_turns WHERE actor_scope=? AND incident_id=?
                    AND status IN ('QUEUED','RUNNING')""", (actor, incident_id)).fetchone():
                    raise APIError(409, "AGENT_BUSY", "Wait for the current reply to finish.")
                capability = self._capabilities()
                if not capability["enabled"]:
                    raise APIError(409, "AGENT_DISABLED", capability["disabled_reason"])
                turn_id = "agent_" + uuid.uuid4().hex
                db.execute("""INSERT INTO incident_agent_turns(turn_id,actor_scope,incident_id,request_key,
                    question,status,created_at) VALUES (?,?,?,?,?,'QUEUED',?)""",
                    (turn_id, actor, incident_id, key, question, stamp()))
                row = db.execute("SELECT * FROM incident_agent_turns WHERE turn_id=?", (turn_id,)).fetchone()
            self.executor.submit(self._execute, turn_id)
        return self._public(row)

    @staticmethod
    def _validate_answer(answer, context):
        if (not isinstance(answer, dict) or set(answer) != {"text", "citations", "limitations", "next_checks"}
                or not isinstance(answer["text"], str) or not answer["text"].strip() or len(answer["text"]) > 5000):
            raise ConversationFailure("INVALID_ANSWER", "The reply did not match the Agent answer format.")
        for key in ("limitations", "next_checks"):
            if (not isinstance(answer[key], list) or len(answer[key]) > 12
                    or any(not isinstance(item, str) or len(item) > 1200 for item in answer[key])):
                raise ConversationFailure("INVALID_ANSWER", "The reply contained invalid findings or checks.")
        seen = set()
        allowed = set(context["evidence_ids"])
        refs = answer["citations"]
        if not isinstance(refs, list) or len(refs) > 12:
            raise ConversationFailure("INVALID_CITATION", "The reply contained too many evidence references.")
        for item in refs:
            if (not isinstance(item, dict) or set(item) != {"evidence_id", "label", "detail"}
                    or any(not isinstance(v, str) or len(v) > 2000 for v in item.values())
                    or item["evidence_id"] not in allowed or item["evidence_id"] in seen):
                raise ConversationFailure("INVALID_CITATION", "The reply cited evidence outside this incident.")
            seen.add(item["evidence_id"])

    def _execute(self, turn_id):
        try:
            with self.app.store._db(write=True) as db:
                if not db.execute("UPDATE incident_agent_turns SET status='RUNNING' WHERE turn_id=? AND status='QUEUED'", (turn_id,)).rowcount:
                    return
                row = db.execute("SELECT * FROM incident_agent_turns WHERE turn_id=?", (turn_id,)).fetchone()
                prior = db.execute("""SELECT * FROM incident_agent_turns WHERE actor_scope=? AND incident_id=?
                    AND sequence<? AND status='COMPLETED' ORDER BY sequence DESC LIMIT 12""",
                    (row["actor_scope"], row["incident_id"], row["sequence"])).fetchall()
            full_context = load_incident_agent_context(self.app, row["incident_id"])
            workflow = getattr(self.app, "incident_workflow", None)
            if workflow is not None:
                full_context = workflow.enrich(row["actor_scope"], row["incident_id"], full_context)
            context = select_incident_agent_context(
                full_context, row["question"],
                previous_question=prior[0]["question"] if prior else "",
            )
            if context["incident_id"] != row["incident_id"]:
                raise ConversationFailure("CONTEXT_MISMATCH", "The evidence did not match the attached incident.")
            encoded = canonical(context)
            with self.app.store._db(write=True) as db:
                db.execute("UPDATE incident_agent_turns SET context_json=?,context_digest=? WHERE turn_id=? AND status='RUNNING'",
                           (encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest(), turn_id))
            answer = self.provider(context, [self._public(item) for item in reversed(prior)], row["question"], turn_id)
            self._validate_answer(answer, context)
            with self.app.store._db(write=True) as db:
                db.execute("UPDATE incident_agent_turns SET status='COMPLETED',answer_json=? WHERE turn_id=? AND status='RUNNING'",
                           (canonical(answer), turn_id))
        except Exception as exc:
            error = ({"code": exc.code, "message": exc.message} if isinstance(exc, ConversationFailure) else
                     {"code": "AGENT_FAILED", "message": "This reply could not be completed. The question is saved."})
            with self.app.store._db(write=True) as db:
                db.execute("UPDATE incident_agent_turns SET status='FAILED',error_json=? WHERE turn_id=? AND status='RUNNING'",
                           (canonical(error), turn_id))

    def close(self):
        with self.lock:
            self.closed = True
        self.executor.shutdown(wait=True)
