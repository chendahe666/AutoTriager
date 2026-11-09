"""Durable, case-scoped conversation; provider and evidence loading are injected.

This module never reads a model key, a benchmark label, or a closed experimental
ledger. The production context loader must validate a terminal source run owned
by the investigation. Conversation history is not fresh operational evidence.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import re
import sqlite3
import threading
import uuid

from .errors import APIError
from .process_lock import ProcessLock

MODEL = "gemini-3.5-flash-lite"
HISTORY_SCOPE = "same_context_run_completed_turns"
TERMINAL = frozenset(("COMPLETED", "FAILED"))
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")

DDL = (
    "CREATE TABLE IF NOT EXISTS conversation_schema (name TEXT PRIMARY KEY, identity TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS conversation_turns (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        turn_id TEXT UNIQUE NOT NULL,
        investigation_id TEXT NOT NULL REFERENCES investigations(investigation_id),
        context_run_id TEXT NOT NULL,
        question TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('QUEUED','RUNNING','COMPLETED','FAILED')),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        context_digest TEXT NOT NULL,
        context_json TEXT NOT NULL CHECK(json_valid(context_json)),
        answer_json TEXT CHECK(answer_json IS NULL OR json_valid(answer_json)),
        error_json TEXT CHECK(error_json IS NULL OR json_valid(error_json)),
        CHECK ((status='COMPLETED' AND answer_json IS NOT NULL AND error_json IS NULL)
            OR (status='FAILED' AND answer_json IS NULL AND error_json IS NOT NULL)
            OR (status IN ('QUEUED','RUNNING') AND answer_json IS NULL AND error_json IS NULL)))""",
    """CREATE UNIQUE INDEX IF NOT EXISTS conversation_one_active
        ON conversation_turns(investigation_id) WHERE status IN ('QUEUED','RUNNING')""",
    """CREATE TABLE IF NOT EXISTS conversation_idempotency (
        actor_scope TEXT NOT NULL,
        investigation_id TEXT NOT NULL REFERENCES investigations(investigation_id),
        key TEXT NOT NULL,
        request_digest TEXT NOT NULL,
        turn_id TEXT NOT NULL REFERENCES conversation_turns(turn_id),
        PRIMARY KEY(actor_scope, investigation_id, key))""",
    """CREATE TRIGGER IF NOT EXISTS conversation_terminal_immutable
        BEFORE UPDATE ON conversation_turns WHEN OLD.status IN ('COMPLETED','FAILED')
        BEGIN SELECT RAISE(ABORT,'terminal conversation turn is immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS conversation_identity_immutable
        BEFORE UPDATE ON conversation_turns WHEN NEW.turn_id!=OLD.turn_id
          OR NEW.sequence!=OLD.sequence OR NEW.investigation_id!=OLD.investigation_id
          OR NEW.context_run_id!=OLD.context_run_id OR NEW.question!=OLD.question
          OR NEW.context_digest!=OLD.context_digest OR NEW.context_json!=OLD.context_json
          OR NEW.created_at!=OLD.created_at
        BEGIN SELECT RAISE(ABORT,'conversation source binding is immutable'); END""",
)


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


class ConversationFailure(Exception):
    """Explicit safe provider failure, without raw response text or credentials."""
    def __init__(self, code, message):
        if not isinstance(code, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", code):
            raise ValueError("invalid safe conversation failure code")
        if not isinstance(message, str) or not 1 <= len(message) <= 512:
            raise ValueError("invalid safe conversation failure message")
        super().__init__(message)
        self.code, self.message = code, message


class ConversationService:
    def __init__(self, store, context_loader, reply_provider, *, capability_provider=None,
                 model_id=MODEL, max_history_turns=12, max_question_chars=4000, workers=2, context_enricher=None):
        if (not callable(context_loader) or not callable(reply_provider)
                or type(max_history_turns) is not int or not 1 <= max_history_turns <= 12
                or type(max_question_chars) is not int or not 1 <= max_question_chars <= 4000
                or type(workers) is not int or not 1 <= workers <= 4
                or not isinstance(model_id, str) or not 1 <= len(model_id) <= 128):
            raise ValueError("invalid bounded conversation configuration")
        self.store, self.context_loader, self.reply_provider = store, context_loader, reply_provider
        if context_enricher is not None and not callable(context_enricher):
            raise ValueError("Context enricher must be callable")
        self.context_enricher = context_enricher
        self.capability_provider, self.model_id = capability_provider, model_id
        self.max_history_turns, self.max_question_chars = max_history_turns, max_question_chars
        self._lock = threading.RLock()
        self._closed = False
        self._process_lock = None
        self._executor = None
        try:
            if store.db_path != ":memory:":
                self._process_lock = ProcessLock(str(store.db_path) + ".conversation.lock")
            self._initialize()
            self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="autotriager-conversation")
        except Exception:
            if self._process_lock:
                self._process_lock.close()
            raise

    def _initialize(self):
        with self.store._db(write=True) as db:
            for sql in DDL:
                db.execute(sql)
            identity = digest(list(DDL))
            previous = db.execute("SELECT identity FROM conversation_schema WHERE name='v1'").fetchone()
            if previous and previous[0] != identity:
                raise RuntimeError("Conversation schema differs; reconcile state before startup")
            db.execute("INSERT OR IGNORE INTO conversation_schema VALUES ('v1',?)", (identity,))
            # No retry or provider dispatch is permitted on process restart.
            error = canonical({"code": "PROCESS_RESTARTED", "message": "The process stopped before this turn finished. This turn was not retried."})
            db.execute("UPDATE conversation_turns SET status='FAILED',error_json=?,updated_at=? WHERE status IN ('QUEUED','RUNNING')", (error, stamp()))

    def capabilities(self):
        if self.capability_provider:
            value = self.capability_provider()
            if not isinstance(value, dict):
                raise RuntimeError("Conversation capability provider returned an invalid object")
            value = json.loads(canonical(value))
        else:
            value = {"enabled": False, "model_id": self.model_id,
                     "disabled_reason": "An explicitly bounded model capability profile is not configured.",
                     "budget": {"max_calls": 0, "max_nominal_usd": 0, "used_calls": 0, "reserved_nominal_usd": 0}}
        value.update(max_question_chars=self.max_question_chars, max_history_turns=self.max_history_turns,
                     evidence_scope="recorded_run", history_scope=HISTORY_SCOPE)
        if self._closed:
            value.update(enabled=False, disabled_reason="The conversation service has stopped.")
        return value

    @staticmethod
    def _public(row):
        return {"turn_id": row["turn_id"], "investigation_id": row["investigation_id"],
                "context_run_id": row["context_run_id"], "question": row["question"],
                "status": row["status"], "created_at": row["created_at"], "updated_at": row["updated_at"],
                "answer": json.loads(row["answer_json"]) if row["answer_json"] else None,
                "error": json.loads(row["error_json"]) if row["error_json"] else None,
                "context_digest": row["context_digest"]}

    def _row(self, db, actor, investigation_id, turn_id):
        self.store._investigation_row(db, actor, investigation_id)
        row = db.execute("SELECT * FROM conversation_turns WHERE turn_id=? AND investigation_id=?", (turn_id, investigation_id)).fetchone()
        if row is None:
            raise self.store._not_found()
        return row

    def get_turn(self, actor, investigation_id, turn_id):
        with self.store._db() as db:
            return self._public(self._row(db, actor, investigation_id, turn_id))

    def list_threads(self, actor):
        """Read owned saved threads only; never load context or call a provider.

        Index availability is independent of the remaining generation budget.
        The current question/status comes from the latest appended turn, and
        failed turns remain visible instead of falling back to an older answer.
        """
        with self.store._db() as db:
            rows = db.execute("""SELECT latest.investigation_id, owned.title,
                       owned.incident_id, summary.turn_count, latest.turn_id,
                       latest.question, latest.status, latest.updated_at,
                       latest.context_run_id
                FROM conversation_turns latest
                JOIN investigations owned
                  ON owned.investigation_id=latest.investigation_id
                JOIN (SELECT investigation_id, MAX(sequence) AS last_sequence,
                             COUNT(*) AS turn_count
                      FROM conversation_turns GROUP BY investigation_id) summary
                  ON summary.investigation_id=latest.investigation_id
                 AND summary.last_sequence=latest.sequence
                WHERE owned.actor_scope=?
                ORDER BY latest.updated_at DESC, latest.sequence DESC
                LIMIT 101""", (actor,)).fetchall()
            threads = [{"investigation_id": row["investigation_id"], "title": row["title"],
                        "incident_id": row["incident_id"], "turn_count": row["turn_count"],
                        "last_turn_id": row["turn_id"], "last_question": row["question"],
                        "last_status": row["status"], "updated_at": row["updated_at"],
                        "context_run_id": row["context_run_id"]} for row in rows[:100]]
            return {"enabled": True, "threads": threads, "truncated": len(rows) > 100}

    def history(self, actor, investigation_id):
        with self.store._db() as db:
            self.store._investigation_row(db, actor, investigation_id)
            rows = db.execute("SELECT * FROM conversation_turns WHERE investigation_id=? ORDER BY sequence DESC LIMIT 101", (investigation_id,)).fetchall()
            return {"investigation_id": investigation_id, "turns": [self._public(x) for x in reversed(rows[:100])],
                    "truncated": len(rows) > 100, "history_scope": HISTORY_SCOPE,
                    "max_history_turns": self.max_history_turns}

    def _prior(self, db, row):
        rows = db.execute("""SELECT * FROM conversation_turns
            WHERE investigation_id=? AND context_run_id=? AND sequence<? AND status='COMPLETED'
            ORDER BY sequence DESC LIMIT ?""", (row["investigation_id"], row["context_run_id"], row["sequence"], self.max_history_turns)).fetchall()
        return [self._public(x) for x in reversed(rows)]

    def _replay(self, db, actor, investigation_id, body_digest, key):
        previous = db.execute("SELECT * FROM conversation_idempotency WHERE actor_scope=? AND investigation_id=? AND key=?", (actor, investigation_id, key)).fetchone()
        if previous:
            if previous["request_digest"] != body_digest:
                raise APIError(409, "IDEMPOTENCY_CONFLICT", "This key was already used with another conversation request.")
            return self._public(self._row(db, actor, investigation_id, previous["turn_id"]))
        return None

    def _context(self, actor, investigation_id, context_run_id, incident_id, question):
        value = self.context_loader(actor, investigation_id, context_run_id)
        if not isinstance(value, dict):
            raise APIError(409, "CONTEXT_NOT_READY", "The recorded source context is unavailable.")
        if self.context_enricher:
            identity = {key: value.get(key) for key in ("incident_id", "context_run_id", "evidence_ids")}
            value = self.context_enricher(actor, json.loads(canonical(value)), question)
            if not isinstance(value, dict) or any(value.get(key) != val for key, val in identity.items()):
                raise APIError(409, "CONTEXT_NOT_READY", "Historical retrieval cannot change current source identity.")
        try:
            raw = canonical(value)
            if len(raw.encode("utf-8")) > 2_000_000:
                raise ValueError()
            value = json.loads(raw)
            ids = value["evidence_ids"]
            if (value["incident_id"] != incident_id or value["context_run_id"] != context_run_id
                    or not isinstance(ids, list) or len(ids) > 10000
                    or any(not isinstance(x, str) or not IDENTIFIER.fullmatch(x) for x in ids)
                    or len(ids) != len(set(ids))):
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise APIError(409, "CONTEXT_NOT_READY", "The recorded context does not match this investigation and source run.") from None
        return raw, hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def create_turn(self, actor, investigation_id, body, idempotency_key):
        if (not isinstance(body, dict) or set(body) != {"question", "context_run_id"}
                or not isinstance(body["question"], str) or not body["question"].strip()
                or len(body["question"]) > self.max_question_chars
                or not isinstance(body["context_run_id"], str) or not IDENTIFIER.fullmatch(body["context_run_id"])):
            raise APIError(400, "INVALID_REQUEST", "Supply a visible bounded question and one recorded context_run_id.")
        if (not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 128
                or any(ord(x) < 33 or ord(x) > 126 for x in idempotency_key)):
            raise APIError(400, "INVALID_REQUEST", "A printable Idempotency-Key of 1 to 128 characters is required.")
        body_digest = digest(body)
        with self.store._db() as db:
            inv = self.store._investigation_row(db, actor, investigation_id)
            replay = self._replay(db, actor, investigation_id, body_digest, idempotency_key)
            if replay:
                return replay, True
            incident_id = inv["incident_id"]
        if self.capabilities().get("enabled") is not True:
            raise APIError(409, "CONVERSATION_DISABLED", "Conversation is not enabled in the current bounded model profile.")
        context_json, context_digest = self._context(actor, investigation_id, body["context_run_id"], incident_id, body["question"])
        with self._lock:
            if self._closed:
                raise APIError(503, "CONVERSATION_CLOSED", "The conversation service has stopped.")
            with self.store._db(write=True) as db:
                self.store._investigation_row(db, actor, investigation_id)
                replay = self._replay(db, actor, investigation_id, body_digest, idempotency_key)
                if replay:
                    return replay, True
                if db.execute("SELECT 1 FROM conversation_turns WHERE investigation_id=? AND status IN ('QUEUED','RUNNING')", (investigation_id,)).fetchone():
                    raise APIError(409, "CONVERSATION_BUSY", "Wait for the active conversation turn to finish.")
                turn_id, instant = "turn_" + uuid.uuid4().hex, stamp()
                db.execute("""INSERT INTO conversation_turns
                    (turn_id,investigation_id,context_run_id,question,status,created_at,updated_at,context_digest,context_json)
                    VALUES (?,?,?,?,'QUEUED',?,?,?,?)""", (turn_id, investigation_id, body["context_run_id"], body["question"], instant, instant, context_digest, context_json))
                db.execute("INSERT INTO conversation_idempotency VALUES (?,?,?,?,?)", (actor, investigation_id, idempotency_key, body_digest, turn_id))
                result = self._public(self._row(db, actor, investigation_id, turn_id))
            try:
                self._executor.submit(self._execute, turn_id)
            except RuntimeError:
                self._fail(turn_id, "DISPATCH_FAILED", "This turn could not be queued and was not retried.")
                return self.get_turn(actor, investigation_id, turn_id), False
            return result, False

    def _validate_answer(self, answer, context):
        required = {"text", "citations", "limitations", "next_checks", "model_id"}
        optional = {"memory_citations", "memory_retrieval"}
        if (not isinstance(answer, dict) or not required.issubset(answer) or not set(answer).issubset(required | optional) or answer["model_id"] != self.model_id
                or not isinstance(answer["text"], str) or not answer["text"].strip() or len(answer["text"]) > 20000):
            raise ConversationFailure("INVALID_ANSWER", "The model answer did not match the conversation contract.")
        for field in ("limitations", "next_checks"):
            if (not isinstance(answer[field], list) or len(answer[field]) > 12
                    or any(not isinstance(x, str) or not x.strip() or len(x) > 2000 for x in answer[field])):
                raise ConversationFailure("INVALID_ANSWER", "The model answer contained invalid limitations or follow-up checks.")
        citations = answer["citations"]
        if not isinstance(citations, list) or len(citations) > 32:
            raise ConversationFailure("INVALID_CITATION", "The model answer contained invalid source references.")
        seen, allowed = set(), set(context["evidence_ids"])
        for ref in citations:
            if (not isinstance(ref, dict) or set(ref) != {"run_id", "evidence_id"}
                    or ref["run_id"] != context["context_run_id"]
                    or not isinstance(ref["evidence_id"], str) or ref["evidence_id"] not in allowed
                    or ref["evidence_id"] in seen):
                raise ConversationFailure("INVALID_CITATION", "A model reference was outside the selected recorded source run.")
            seen.add(ref["evidence_id"])
        memory_items = context.get("memory_retrieval", {}).get("items", [])
        allowed_memory = {item["memory_id"]: item for item in memory_items}
        memory_refs = answer.get("memory_citations", [])
        if not isinstance(memory_refs, list) or len(memory_refs) > 3:
            raise ConversationFailure("INVALID_MEMORY_CITATION", "Historical references exceed the pinned memory scope.")
        seen_memory = set()
        for ref in memory_refs:
            if not isinstance(ref, dict) or set(ref) != {"memory_id", "revision", "source_incident_id", "source_run_id"}:
                raise ConversationFailure("INVALID_MEMORY_CITATION", "Historical references need an explicit source version.")
            item = allowed_memory.get(ref["memory_id"])
            if (not item or ref["memory_id"] in seen_memory or ref["revision"] != item["revision"]
                    or ref["source_incident_id"] != item["source"]["incident_id"]
                    or ref["source_run_id"] != item["source"].get("run_id")):
                raise ConversationFailure("INVALID_MEMORY_CITATION", "Historical reference is outside this turn's pinned memory snapshot.")
            seen_memory.add(ref["memory_id"])
        if "memory_retrieval" in answer:
            from .memory_context import memory_trace
            pinned = context.get("memory_retrieval", {})
            supplied = answer["memory_retrieval"]
            if not isinstance(supplied, dict) or supplied != memory_trace(pinned):
                raise ConversationFailure("INVALID_MEMORY_CITATION", "Historical retrieval snapshot identity changed.")
        return json.loads(canonical(answer))

    def _fail(self, turn_id, code, message):
        with self.store._db(write=True) as db:
            db.execute("UPDATE conversation_turns SET status='FAILED',answer_json=NULL,error_json=?,updated_at=? WHERE turn_id=? AND status IN ('QUEUED','RUNNING')", (canonical({"code": code, "message": message}), stamp(), turn_id))

    def _execute(self, turn_id):
        try:
            with self.store._db(write=True) as db:
                changed = db.execute("UPDATE conversation_turns SET status='RUNNING',updated_at=? WHERE turn_id=? AND status='QUEUED'", (stamp(), turn_id)).rowcount
                if not changed:
                    return
                row = db.execute("SELECT * FROM conversation_turns WHERE turn_id=?", (turn_id,)).fetchone()
                context, prior = json.loads(row["context_json"]), self._prior(db, row)
                question = row["question"]
            answer = self._validate_answer(self.reply_provider(context, prior, question, turn_id), context)
            with self.store._db(write=True) as db:
                db.execute("UPDATE conversation_turns SET status='COMPLETED',answer_json=?,updated_at=? WHERE turn_id=? AND status='RUNNING'", (canonical(answer), stamp(), turn_id))
        except ConversationFailure as exc:
            self._fail(turn_id, exc.code, exc.message)
        except Exception:
            # Do not expose provider bodies, question text, keys or tracebacks.
            self._fail(turn_id, "CONVERSATION_FAILED", "This turn failed. No replacement or automatic retry was performed.")

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
        try:
            self._executor.shutdown(wait=True, cancel_futures=False)
        finally:
            if self._process_lock:
                self._process_lock.close()
