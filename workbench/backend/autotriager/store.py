"""Durable scoped persistence for fixtures, inspection and qualified model runs.

The copied SQL contract supplies lifecycle and reference fences. Public reads
never re-run a query or model. Raw outputs and source arrays are preserved as
content-addressed JSON beside the database; accepted objects live in SQLite.
"""
import base64
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import uuid

from jsonschema import Draft202012Validator, FormatChecker

from .errors import APIError
from . import fixture

VERSION = fixture.VERSION
TERMINAL = frozenset(("COMPLETED", "FAILED", "TIMED_OUT", "BUDGET_EXHAUSTED", "CANCELLED"))
ROOT = Path(__file__).resolve().parents[2]


def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def encoded(value):
    return fixture.canonical(value).decode("utf-8")


def timestamp(value):
    """Strict explicit-zone timestamp normalization for trusted grant metadata."""
    if not isinstance(value, str):
        raise ValueError("An explicit-zone timestamp is required")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("An explicit-zone timestamp is required")
    return result.astimezone(timezone.utc)


class Store:
    def __init__(self, db_path, assets_dir=None):
        self.db_path = str(db_path)
        self._keeper = None
        self._uri = self.db_path == ":memory:"
        self._dsn = f"file:autotriager_{uuid.uuid4().hex}?mode=memory&cache=shared" if self._uri else self.db_path
        if not self._uri:
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        if assets_dir is None:
            assets_dir = ROOT / "var" / "evidence" if self._uri else Path(self.db_path).parent / "evidence"
        self.assets_dir = Path(assets_dir)
        self.assets_dir.mkdir(parents=True, exist_ok=True)
        self.schema = json.loads((ROOT / "contracts" / "runtime.schema.json").read_text(encoding="utf-8"))
        self._validators = {}
        self._inspection_incidents = set()
        if self._uri:
            self._keeper = self._connect()
        ddl = (ROOT / "database" / "schema.sql").read_text(encoding="utf-8")
        with self._db(snapshot=False) as db:
            db.execute("PRAGMA journal_mode=WAL")
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='runs'").fetchone()
            if not exists:
                db.executescript("BEGIN IMMEDIATE;\n" + ddl)
                db.execute("INSERT INTO schema_migrations(version,applied_at) VALUES (?,?)",
                           ("initial:" + hashlib.sha256(ddl.encode()).hexdigest(), now()))
                db.commit()
        self._migrate()
        self._recover()

    def _migrate(self):
        """Additive, checksum-identified upgrade, including pre-gateway stores."""
        migrations = (("002_query_reply_artifacts", ("gateway_capabilities", "query_reply_artifacts")),
                      ("003_reviewed_grant_leases", ("reviewed_grant_leases",)),
                      ("004_service503_recoveries", ("service503_recoveries",)),
                      ("005_final_template_repairs", ("final_template_repairs",)),
                      ("006_explicit_paid_pilot", ("explicit_paid_authorizations", "paid_run_costs")))
        for name, tables in migrations:
            sql = (ROOT / "database/migrations" / (name + ".sql")).read_text(encoding="utf-8")
            identity = name + ":" + hashlib.sha256(sql.encode()).hexdigest()
            with self._db(snapshot=False) as db:
                previous = db.execute("SELECT version FROM schema_migrations WHERE version LIKE ?", (name + ":%",)).fetchone()
                if previous:
                    if previous[0] != identity:
                        raise RuntimeError("Applied migration checksum differs from this build")
                    for table in tables:
                        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                            raise RuntimeError("Applied migration is incomplete; restore or reconcile local state")
                    continue
                if not self._uri and db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]:
                    backup = Path(self.db_path).with_name(Path(self.db_path).name + ".before-" + name[:3] + "-" + uuid.uuid4().hex[:8] + ".sqlite3")
                    with sqlite3.connect(backup) as target:
                        db.backup(target)
                try:
                    db.executescript("BEGIN IMMEDIATE;\n" + sql)
                    db.execute("INSERT INTO schema_migrations(version,applied_at) VALUES (?,?)", (identity, now()))
                    db.commit()
                except Exception:
                    if db.in_transaction:
                        db.rollback()
                    raise

    @staticmethod
    def _effective_grant_expiry(db, grant):
        lease = db.execute("SELECT expires_at FROM reviewed_grant_leases WHERE grant_id=?", (grant["grant_id"],)).fetchone()
        return lease[0] if lease else grant["expires_at"]

    def _connect(self):
        db = sqlite3.connect(self._dsn, uri=self._uri, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=5000")
        return db

    @contextmanager
    def _db(self, write=False, snapshot=True):
        db = self._connect()
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            elif snapshot:
                # A Run combines state, answer and journal position. All of
                # them must come from one read snapshot while the worker writes.
                db.execute("BEGIN")
            yield db
            if write:
                db.commit()
        except Exception:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()

    def close(self):
        if self._keeper is not None:
            self._keeper.close()
            self._keeper = None

    def _validate(self, name, value):
        if name not in self._validators:
            schema = {"$ref": "#/$defs/" + name, "$defs": self.schema["$defs"]}
            self._validators[name] = Draft202012Validator(schema, format_checker=FormatChecker())
        errors = sorted(self._validators[name].iter_errors(value), key=lambda e: str(e.path))
        if errors:
            raise APIError(400, "INVALID_REQUEST", "The payload does not match " + name + ".",
                           [{"field": (".".join(str(p) for p in e.path) or "$")[:256], "reason": e.message[:1000]} for e in errors[:20]])
        return value

    def _preserve(self, value):
        data = fixture.canonical(value)
        digest = hashlib.sha256(data).hexdigest()
        path = self.assets_dir / (digest + ".json")
        try:
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            if path.read_bytes() != data:
                raise RuntimeError("Content-addressed evidence bytes changed: " + digest)
        else:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        return digest

    def _preserve_tool_reply(self, db, reply):
        """Link the exact admitted engine payload to its scoped query."""
        self._validate("ToolReply", reply)
        digest = self._preserve(reply)
        query = reply["query"]
        db.execute("INSERT INTO query_reply_artifacts VALUES (?,?,?,?,?,?)", (
            query["query_id"], query["run_id"], query["incident_id"], digest,
            len(fixture.canonical(reply)), now()))
        return digest

    def _recover(self):
        # Fence incomplete local work. The separate gate retains ambiguous
        # provider reservations; no external dispatch is resumed or retried.
        with self._db(write=True) as db:
            rows = db.execute("SELECT * FROM runs WHERE state NOT IN ('COMPLETED','FAILED','TIMED_OUT','BUDGET_EXHAUSTED','CANCELLED')").fetchall()
            for row in rows:
                config = db.execute("SELECT grant_id,inference_authorized FROM run_configs WHERE run_id=?", (row["run_id"],)).fetchone()
                db.execute("UPDATE gateway_capabilities SET revoked_at=? WHERE run_id=? AND revoked_at IS NULL", (now(), row["run_id"]))
                if config and config["inference_authorized"]:
                    db.execute("UPDATE inference_grants SET status='REVOKED' WHERE grant_id=?", (config["grant_id"],))
                if row["state"] != "CANCEL_REQUESTED":
                    self._transition(db, row["run_id"], "CANCEL_REQUESTED", "Process restarted; work is not automatically resumed.")
                self._transition(db, row["run_id"], "CANCELLED", "PROCESS_RESTARTED", "PROCESS_RESTARTED")

    def seed_fixture(self):
        inc = self._validate("Incident", fixture.incident())
        self._preserve(fixture.METRICS)
        self._preserve(fixture.TRACES)
        with self._db(write=True) as db:
            db.execute("INSERT OR IGNORE INTO incidents VALUES (?,?,?,?,?,?,?,?,?)",
                       (inc["incident_id"], inc["system_id"], inc["display_name"], inc["mode"], inc["data_revision"],
                        inc["manifest_digest"], inc["time_window"]["start"], inc["time_window"]["end"], encoded(inc)))
            stored = db.execute("SELECT manifest_digest FROM incidents WHERE incident_id=?", (inc["incident_id"],)).fetchone()
            if stored[0] != inc["manifest_digest"]:
                raise RuntimeError("Fixture revision changed without a data migration")
            for sid in inc["service_ids"]:
                db.execute("INSERT OR IGNORE INTO incident_services VALUES (?,?)", (inc["incident_id"], sid))
        return inc

    def register_incident(self, incident):
        """Register an operator-audited manifest without importing private labels."""
        value = self._validate("Incident", incident)
        with self._db(write=True) as db:
            current = db.execute("SELECT manifest_digest FROM incidents WHERE incident_id=?", (value["incident_id"],)).fetchone()
            if current and current[0] != value["manifest_digest"]:
                raise RuntimeError("Incident revision changed; use a new identity instead of rewriting history")
            db.execute("INSERT OR IGNORE INTO incidents VALUES (?,?,?,?,?,?,?,?,?)", (value["incident_id"], value["system_id"], value["display_name"], value["mode"], value["data_revision"], value["manifest_digest"], value["time_window"]["start"], value["time_window"]["end"], encoded(value)))
            for service in value["service_ids"]:
                db.execute("INSERT OR IGNORE INTO incident_services VALUES (?,?)", (value["incident_id"], service))
        if value["mode"] == "offline_replay":
            self._inspection_incidents.add(value["incident_id"])
        return value

    @staticmethod
    def _not_found():
        return APIError(404, "NOT_FOUND", "The requested resource was not found in this scope.")

    def _incident(self, db, incident_id):
        row = db.execute("SELECT manifest_json FROM incidents WHERE incident_id=?", (incident_id,)).fetchone()
        if row is None:
            raise self._not_found()
        return json.loads(row[0])

    def get_incident(self, incident_id):
        with self._db() as db:
            return self._incident(db, incident_id)

    def _investigation_row(self, db, actor, investigation_id):
        row = db.execute("SELECT * FROM investigations WHERE investigation_id=? AND actor_scope=?", (investigation_id, actor)).fetchone()
        if row is None:
            raise self._not_found()
        return row

    def _investigation(self, db, row):
        last = db.execute("SELECT run_id FROM runs WHERE investigation_id=? ORDER BY created_at DESC,run_id DESC LIMIT 1", (row["investigation_id"],)).fetchone()
        return {"schema_version": VERSION, "investigation_id": row["investigation_id"], "incident_id": row["incident_id"],
                "title": row["title"], "created_at": row["created_at"], "updated_at": row["updated_at"],
                "latest_run_id": last[0] if last else None}

    def get_investigation(self, actor, investigation_id):
        with self._db() as db:
            return self._investigation(db, self._investigation_row(db, actor, investigation_id))

    def _run_row(self, db, actor, run_id):
        row = db.execute("SELECT r.* FROM runs r JOIN investigations i ON i.investigation_id=r.investigation_id WHERE r.run_id=? AND i.actor_scope=?", (run_id, actor)).fetchone()
        if row is None:
            raise self._not_found()
        return row

    def _run(self, db, row):
        cfg = json.loads(db.execute("SELECT config_json FROM run_configs WHERE config_digest=?", (row["config_digest"],)).fetchone()[0])
        answer = db.execute("SELECT answer_json FROM answers WHERE run_id=?", (row["run_id"],)).fetchone()
        seq = db.execute("SELECT last_sequence FROM run_journal_positions WHERE run_id=?", (row["run_id"],)).fetchone()[0]
        return {"schema_version": VERSION, "run_id": row["run_id"], "investigation_id": row["investigation_id"],
                "incident_id": row["incident_id"], "parent_run_id": row["parent_run_id"], "question": row["question"],
                "mode": cfg["mode"], "state": row["state"], "revision": row["revision"], "run_generation": row["run_generation"],
                "config_digest": row["config_digest"], "created_at": row["created_at"], "updated_at": row["updated_at"],
                "termination_code": row["termination_code"], "remote_stop_status": self._remote_state(db, row, cfg), "last_sequence": seq,
                "config": cfg, "answer": json.loads(answer[0]) if answer else None}

    @staticmethod
    def _remote_state(db, run, cfg):
        if not cfg["inference_authorized"] or run["state"] in ("CREATED", "QUEUED"):
            return "not_dispatched"
        started = db.execute("SELECT 1 FROM events WHERE run_id=? AND event_type='run.state_changed' AND json_extract(payload_json,'$.to_state')='RUNNING' LIMIT 1", (run["run_id"],)).fetchone()
        if not started:
            return "not_dispatched"
        usage = db.execute("SELECT payload_json FROM events WHERE run_id=? AND event_type='usage.observed' ORDER BY sequence DESC LIMIT 1", (run["run_id"],)).fetchone()
        if usage:
            value = json.loads(usage[0])
            if value["accounting_status"] == "observed":
                return "observed_completed" if value["model_calls"] else "not_dispatched"
        return "unknown"

    def get_run(self, actor, run_id):
        with self._db() as db:
            return self._run(db, self._run_row(db, actor, run_id))

    @staticmethod
    def _paging(limit, cursor, scope):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise APIError(400, "INVALID_REQUEST", "limit must be an integer from 1 to 100.")
        if cursor is None:
            return None
        try:
            if not isinstance(cursor, str) or len(cursor) > 2048:
                raise ValueError()
            value = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode())
            if value["scope"] != scope or not isinstance(value["position"], list) or not all(isinstance(v, str) for v in value["position"]):
                raise ValueError()
            return value["position"]
        except (ValueError, KeyError, TypeError, UnicodeError):
            raise APIError(400, "INVALID_CURSOR", "The page cursor is invalid for this scope.") from None

    @staticmethod
    def _page(items, rows, limit, scope, keys):
        more = len(rows) > limit
        cursor = None
        if more:
            cursor = base64.urlsafe_b64encode(fixture.canonical({"scope": scope, "position": [rows[limit - 1][k] for k in keys]})).decode().rstrip("=")
        return {"schema_version": VERSION, "items": items, "next_cursor": cursor}

    def list_incidents(self, limit=20, cursor=None):
        scope = "incidents"
        position = self._paging(limit, cursor, scope)
        if position is not None and len(position) != 1:
            raise APIError(400, "INVALID_CURSOR", "Invalid incident cursor.")
        with self._db() as db:
            rows = db.execute("SELECT * FROM incidents WHERE incident_id>? ORDER BY incident_id LIMIT ?", (position[0] if position else "", limit + 1)).fetchall()
            return self._page([json.loads(r["manifest_json"]) for r in rows[:limit]], rows, limit, scope, ["incident_id"])

    def list_investigations(self, actor, limit=20, cursor=None):
        scope = "investigations:" + actor
        position = self._paging(limit, cursor, scope)
        if position is not None and len(position) != 2:
            raise APIError(400, "INVALID_CURSOR", "Invalid investigation cursor.")
        with self._db() as db:
            rows = db.execute("SELECT * FROM investigations WHERE actor_scope=? AND (created_at,investigation_id)>(?,?) ORDER BY created_at,investigation_id LIMIT ?", (actor, *(position or ["", ""]), limit + 1)).fetchall()
            return self._page([self._investigation(db, r) for r in rows[:limit]], rows, limit, scope, ["created_at", "investigation_id"])

    def list_runs(self, actor, investigation_id, limit=20, cursor=None):
        scope = "runs:" + actor + ":" + investigation_id
        position = self._paging(limit, cursor, scope)
        if position is not None and len(position) != 2:
            raise APIError(400, "INVALID_CURSOR", "Invalid run cursor.")
        with self._db() as db:
            self._investigation_row(db, actor, investigation_id)
            rows = db.execute("SELECT * FROM runs WHERE investigation_id=? AND (created_at,run_id)>(?,?) ORDER BY created_at,run_id LIMIT ?", (investigation_id, *(position or ["", ""]), limit + 1)).fetchall()
            return self._page([self._run(db, r) for r in rows[:limit]], rows, limit, scope, ["created_at", "run_id"])

    def _idempotent(self, db, actor, route, body, key):
        if not isinstance(key, str) or not 1 <= len(key) <= 200 or any(ord(c) < 33 or ord(c) > 126 for c in key):
            raise APIError(400, "INVALID_REQUEST", "A printable Idempotency-Key of 1 to 200 characters is required.")
        prior = db.execute("SELECT * FROM idempotency WHERE actor_scope=? AND route=? AND key=?", (actor, route, key)).fetchone()
        if prior:
            if prior["request_digest"] != fixture.digest(body):
                raise APIError(409, "IDEMPOTENCY_CONFLICT", "This idempotency key was already used with a different request.")
            return json.loads(prior["response_json"])
        return None

    @staticmethod
    def _remember(db, actor, route, key, body, resource, response, status):
        db.execute("INSERT INTO idempotency VALUES (?,?,?,?,?,?,?,?,?)", (actor, route, key, fixture.digest(body), resource,
                   response[resource + "_id"], status, encoded(response), now()))

    def create_investigation(self, actor, body, idempotency_key):
        self._validate("CreateInvestigation", body)
        if "title" in body and not body["title"].strip():
            raise APIError(400, "INVALID_REQUEST", "The title must contain visible text.")
        route = "/api/v1/investigations"
        with self._db(write=True) as db:
            replay = self._idempotent(db, actor, route, body, idempotency_key)
            if replay is not None:
                return replay, True
            inc = self._incident(db, body["incident_id"])
            iid, stamp = fixture.opaque("inv"), now()
            db.execute("INSERT INTO investigations VALUES (?,?,?,?,?,?)", (iid, inc["incident_id"], actor, body.get("title", inc["display_name"]), stamp, stamp))
            response = self._investigation(db, self._investigation_row(db, actor, iid))
            self._remember(db, actor, route, idempotency_key, body, "investigation", response, 201)
            return response, False

    def create_run(self, actor, investigation_id, body, idempotency_key, *, available_modes=None):
        self._validate("CreateRun", body)
        if not body["question"].strip():
            raise APIError(400, "INVALID_REQUEST", "The question must contain visible text.")
        route = "/api/v1/investigations/" + investigation_id + "/runs"
        with self._db(write=True) as db:
            inv = self._investigation_row(db, actor, investigation_id)
            replay = self._idempotent(db, actor, route, body, idempotency_key)
            if replay is not None:
                return replay, True
            inc = self._incident(db, inv["incident_id"])
            if available_modes is not None and inc["mode"] not in available_modes:
                raise APIError(409, "CONFIGURATION_NOT_READY", "No worker is available for this incident mode.")
            if inc["mode"] == "offline_replay" and inc["incident_id"] not in self._inspection_incidents:
                raise APIError(409, "CONFIGURATION_NOT_READY", "The incident is not registered with an audited local data adapter.")
            expected_policy = fixture.POLICY_ID if inc["mode"] == "fixture" else "offline_inspection"
            requested_policy = body.get("policy_id", expected_policy)
            if requested_policy != expected_policy:
                raise APIError(409, "CONFIGURATION_NOT_READY", "Only this incident's explicit no-model inspection policy is configured.")
            parent_id = body.get("parent_run_id")
            if parent_id:
                parent = self._run_row(db, actor, parent_id)
                if parent["investigation_id"] != investigation_id or parent["incident_id"] != inc["incident_id"]:
                    raise self._not_found()
                if parent["state"] not in TERMINAL:
                    raise APIError(409, "CONFLICT", "A follow-up requires a terminal parent in this investigation.")
            run_id, stamp = fixture.opaque("run"), now()
            cfg = fixture.config(run_id, inc)
            if inc["mode"] == "offline_replay":
                cfg.pop("config_digest")
                cfg.update(mode="offline_replay", policy_id="offline_inspection", policy_version="readonly-v1", adapter_version="rcaeval-readonly-v1")
                cfg["budget"] = dict(cfg["budget"], max_tool_calls=5, max_source_bytes=262144, max_total_rows=1000, max_wall_time_seconds=120)
                cfg["policy_digest"] = fixture.digest({"policy_id": cfg["policy_id"], "version": cfg["policy_version"], "budget": cfg["budget"], "behavior": "inspect original telemetry, no inference"})
                cfg["config_digest"] = fixture.digest(cfg)
            self._validate("RunConfig", cfg)
            db.execute("INSERT INTO run_configs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (cfg["config_digest"], run_id, inc["incident_id"], cfg["mode"], cfg["policy_digest"],
                       cfg["engine_source_commit"], None, None, 0, None, None, encoded(cfg)))
            db.execute("INSERT INTO runs(run_id,investigation_id,incident_id,parent_run_id,question,config_digest,state,created_at,updated_at) VALUES (?,?,?,?,?,?,'CREATED',?,?)", (run_id, investigation_id, inc["incident_id"], parent_id, body["question"], cfg["config_digest"], stamp, stamp))
            b = cfg["budget"]
            deadline = (datetime.now(timezone.utc) + timedelta(seconds=b["max_wall_time_seconds"])).isoformat().replace("+00:00", "Z")
            db.execute("INSERT INTO run_budgets(run_id,max_model_calls,max_tool_calls,max_source_bytes,max_total_rows,max_total_input_tokens,max_total_output_tokens,max_output_tokens,max_wall_time_seconds,deadline_at,paid_fallback_allowed) VALUES (?,?,?,?,?,?,?,?,?,?,0)", (run_id, b["max_model_calls"], b["max_tool_calls"], b["max_source_bytes"], b["max_total_rows"], b["max_total_input_tokens"], b["max_total_output_tokens"], b["max_output_tokens"], b["max_wall_time_seconds"], deadline))
            if inc["mode"] == "fixture":
                self._event(db, run_id, "warning", {"code": "SYNTHETIC_FIXTURE", "message": fixture.LIMITATION + " No engine or provider is called."})
                self._transition(db, run_id, "QUEUED", "Synthetic fixture queued; zero provider calls.")
            else:
                self._event(db, run_id, "warning", {"code": "DATA_INSPECTION_ONLY", "message": "Actual recorded telemetry; read-only inspection without model reasoning or root-cause confirmation."})
                self._transition(db, run_id, "QUEUED", "Actual-data inspection queued; zero model calls.")
            db.execute("UPDATE investigations SET updated_at=? WHERE investigation_id=?", (stamp, investigation_id))
            response = self._validate("Run", self._run(db, self._run_row(db, actor, run_id)))
            self._remember(db, actor, route, idempotency_key, body, "run", response, 202)
            return response, False

    def prepare_authorized_run(self, actor, incident_id, question, *, grant_reference,
                               model_key, resolved_model_id, engine_build_id,
                               expected_manifest_digest, max_provider_calls=10,
                               max_total_input_tokens=80000, max_total_output_tokens=10240,
                               max_input_tokens_per_call=8000, max_output_tokens_per_call=1024,
                               max_wall_time_seconds=120, grant_expires_at=None, parent_run_id=None):
        """Trusted CLI-only grant mirror and frozen run setup; never dispatch.

        This method is deliberately absent from the browser API. The independent
        ProviderGate ledger remains the pre-dispatch authority. A reference is
        reused rather than reissued, so repeating setup cannot reset credits.
        """
        self._validate("CreateRun", {"question": question, "parent_run_id": parent_run_id})
        ceilings = {"max_provider_calls": (max_provider_calls, 10),
                    "max_total_input_tokens": (max_total_input_tokens, 80000),
                    "max_total_output_tokens": (max_total_output_tokens, 10240),
                    "max_input_tokens_per_call": (max_input_tokens_per_call, 8000),
                    "max_output_tokens_per_call": (max_output_tokens_per_call, 1024),
                    "max_wall_time_seconds": (max_wall_time_seconds, 120)}
        if (not question.strip() or not isinstance(grant_reference, str) or not 1 <= len(grant_reference) <= 1024
                or model_key != "autotriager-gemini-qualified" or resolved_model_id != "gemini-3.1-flash-lite"
                or not isinstance(engine_build_id, str) or not engine_build_id
                or any(isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= bound
                       for value, bound in ceilings.values())
                or any(value != bound for key, (value, bound) in ceilings.items() if key != "max_wall_time_seconds")):
            raise APIError(400, "INVALID_REQUEST", "Trusted model setup exceeds the finite qualified profile.")
        stamp = now()
        with self._db(write=True) as db:
            incident = self._incident(db, incident_id)
            if (incident["mode"] != "offline_replay" or incident_id not in self._inspection_incidents
                    or incident["manifest_digest"] != expected_manifest_digest):
                raise APIError(409, "CONFIGURATION_NOT_READY", "The authorized case is not the audited active corpus.")
            replacement = "human:call_90d00ad9e8d643bf8342f66b4190c74d:model-replacement-same-ceiling"
            retired = "human:call_4f5e9ed41a6443ec8b9f59c9520bd0f8:confirmed-free-tier-finite-test"
            if grant_reference == retired:
                raise APIError(409, "INFERENCE_NOT_AUTHORIZED", "The earlier model approval was explicitly retired.")
            if grant_reference == replacement:
                old = db.execute("SELECT * FROM inference_grants WHERE decision_reference=?", (retired,)).fetchone()
                if old and (old["used_provider_calls"] or old["reserved_provider_calls"] or old["used_provider_tokens"] or old["reserved_provider_tokens"]):
                    raise APIError(409, "INFERENCE_NOT_AUTHORIZED", "Replacement cannot reset an earlier consumed or unresolved approval.")
                db.execute("UPDATE inference_grants SET status='REVOKED' WHERE decision_reference=?", (retired,))
            previous = db.execute("SELECT * FROM inference_grants WHERE decision_reference=?", (grant_reference,)).fetchone()
            expiry = (timestamp(grant_expires_at).isoformat(timespec="microseconds").replace("+00:00", "Z")
                      if grant_expires_at else previous["expires_at"] if previous else
                      (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(timespec="microseconds").replace("+00:00", "Z"))
            effective_expiry = self._effective_grant_expiry(db, previous) if previous else expiry
            if not datetime.now(timezone.utc) < timestamp(effective_expiry) <= datetime.now(timezone.utc) + timedelta(days=1):
                raise APIError(409, "INFERENCE_NOT_AUTHORIZED", "The finite grant has expired or its expiry is unsupported.")
            definition = {"decision_reference": grant_reference, "actor_scope": actor,
                "incident_id": incident_id, "manifest_digest": expected_manifest_digest,
                "data_revision": incident["data_revision"], "model_key": model_key,
                "resolved_model_id": resolved_model_id, "expires_at": expiry,
                **{key: value for key, (value, _) in ceilings.items()},
                "max_paid_cost_usd": 0, "paid_fallback_allowed": False,
                "billing_qualification": "Free Tier confirmed by owner; billing tier is not programmatically verified."}
            grant_digest = fixture.digest(definition)
            grant_id = "gr_" + hashlib.sha256(grant_reference.encode("utf-8")).hexdigest()[:32]
            self._preserve(definition)
            if previous:
                if previous["grant_digest"] != grant_digest or previous["grant_id"] != grant_id or previous["status"] != "ACTIVE":
                    raise APIError(409, "INFERENCE_NOT_AUTHORIZED", "An existing approval cannot be changed, reset, or reactivated.")
                if (previous["used_provider_calls"] + previous["reserved_provider_calls"] >= previous["max_provider_calls_total"]
                        or previous["used_provider_tokens"] + previous["reserved_provider_tokens"] >= previous["max_total_provider_tokens"]):
                    raise APIError(409, "INFERENCE_NOT_AUTHORIZED", "The shared finite approval has no remaining credit.")
            else:
                db.execute("INSERT INTO inference_grants(grant_id,actor_scope,status,permitted_mode,permitted_data_revision,permitted_manifest_digest,permitted_model_key,resolved_model_id,decision_reference,grant_digest,created_at,expires_at,max_provider_calls_total,max_total_provider_tokens) VALUES (?,?,'ACTIVE','offline_replay',?,?,?,?,?,?,?,?,?,?)",
                    (grant_id, actor, incident["data_revision"], expected_manifest_digest, model_key, resolved_model_id,
                     grant_reference, grant_digest, stamp, expiry, max_provider_calls, max_total_input_tokens + max_total_output_tokens))
            active = db.execute("SELECT r.run_id FROM runs r JOIN run_configs c ON c.run_id=r.run_id WHERE c.grant_id=? AND r.state NOT IN ('COMPLETED','FAILED','TIMED_OUT','BUDGET_EXHAUSTED','CANCELLED')", (grant_id,)).fetchone()
            if active:
                raise APIError(409, "CONFLICT", "This approval already has a local run pending.")
            parent = self._run_row(db, actor, parent_run_id) if parent_run_id else None
            if parent:
                parent_grant = db.execute("SELECT grant_id FROM run_configs WHERE run_id=?", (parent_run_id,)).fetchone()[0]
                if (parent["incident_id"] != incident_id or parent["state"] not in TERMINAL or parent_grant != grant_id):
                    raise APIError(409, "CONFLICT", "A model continuation requires a terminal parent under the same case and approval.")
            run_id = fixture.opaque("run")
            investigation_id = parent["investigation_id"] if parent else fixture.opaque("inv")
            budget = dict(fixture.BUDGET, max_model_calls=max_provider_calls, max_tool_calls=5,
                max_source_bytes=262144, max_total_rows=1000, max_total_input_tokens=max_total_input_tokens,
                max_total_output_tokens=max_total_output_tokens, max_output_tokens=max_output_tokens_per_call,
                max_wall_time_seconds=max_wall_time_seconds)
            final_repair = db.execute("SELECT parent_run_id FROM final_template_repairs WHERE grant_id=?", (grant_id,)).fetchone()
            if final_repair:
                if (parent_run_id != final_repair["parent_run_id"]
                        or db.execute("SELECT 1 FROM runs WHERE parent_run_id=?", (parent_run_id,)).fetchone()):
                    raise APIError(409, "INFERENCE_NOT_AUTHORIZED", "The final reviewed repair permits one linked child only.")
                budget.update(max_model_calls=4, max_total_input_tokens=32000, max_total_output_tokens=4096)
            cfg = fixture.config(run_id, incident)
            cfg.pop("config_digest")
            cfg.update(mode="offline_replay", engine_build_id=engine_build_id,
                adapter_version="holmes-qualified-gemini-v1", model_key=model_key, resolved_model_id=resolved_model_id,
                policy_id="qualified_model_cli", policy_version="finite-v1", inference_authorized=True,
                grant_id=grant_id, grant_expires_at=effective_expiry, budget=budget,
                policy_digest=fixture.digest({"grant_digest": grant_digest, "budget": budget,
                    "max_input_tokens_per_call": max_input_tokens_per_call, "allowed_tools": ["incident_manifest", "incident_services", "incident_metrics", "incident_logs", "incident_traces"]}))
            cfg["config_digest"] = fixture.digest(cfg)
            self._validate("RunConfig", cfg)
            if parent is None:
                db.execute("INSERT INTO investigations VALUES (?,?,?,?,?,?)", (investigation_id, incident_id, actor, "Authorized finite model investigation", stamp, stamp))
            db.execute("INSERT INTO run_configs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (cfg["config_digest"], run_id, incident_id,
                cfg["mode"], cfg["policy_digest"], cfg["engine_source_commit"], model_key, resolved_model_id, 1, grant_id, effective_expiry, encoded(cfg)))
            db.execute("INSERT INTO runs(run_id,investigation_id,incident_id,parent_run_id,question,config_digest,state,created_at,updated_at) VALUES (?,?,?,?,?,?,'CREATED',?,?)",
                (run_id, investigation_id, incident_id, parent_run_id, question, cfg["config_digest"], stamp, stamp))
            deadline = min(timestamp(effective_expiry), datetime.now(timezone.utc) + timedelta(seconds=max_wall_time_seconds)).isoformat(timespec="microseconds").replace("+00:00", "Z")
            db.execute("INSERT INTO run_budgets(run_id,max_model_calls,max_tool_calls,max_source_bytes,max_total_rows,max_total_input_tokens,max_total_output_tokens,max_output_tokens,max_wall_time_seconds,deadline_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (run_id, budget["max_model_calls"], budget["max_tool_calls"], budget["max_source_bytes"], budget["max_total_rows"],
                 budget["max_total_input_tokens"], budget["max_total_output_tokens"], max_output_tokens_per_call, max_wall_time_seconds, deadline))
            self._event(db, run_id, "warning", {"code": "FINITE_MODEL_GRANT", "message": "A finite owner-confirmed Free Tier experiment was prepared. No inference has been dispatched; no diagnostic result is claimed."})
            self._transition(db, run_id, "QUEUED", "Trusted CLI model setup complete; awaiting reviewed provider admission.")
            db.execute("UPDATE investigations SET updated_at=? WHERE investigation_id=?", (stamp, investigation_id))
            return self._validate("Run", self._run(db, self._run_row(db, actor, run_id)))

    def prepare_paid_run(self, actor, incident_id, question, *, grant_reference, model_key,
                         resolved_model_id, engine_build_id, expected_manifest_digest,
                         paid_budget, previous_experiment_reference=None):
        """Trusted one-workflow paid pilot, separate from every Free Tier grant.

        The additive paid authorization is the monetary authority for this named
        policy. The legacy Free Tier column is not reinterpreted as paid credit.
        This method never dispatches or renews a grant and has no public route.
        """
        from .engine.provider_gate import PAID_GRANT_REFERENCE, PAID_MODEL
        self._validate("CreateRun", {"question": question, "parent_run_id": None})
        try:
            profile = self._validate_paid_budget(paid_budget)
            created, expiry = timestamp(profile["created_at"]), timestamp(profile["expires_at"])
            if (not created <= datetime.now(timezone.utc) < expiry <= created + timedelta(hours=1)
                    or any(profile[key] != 0 for key in ("money_reserved_nano_usd", "known_fee_nano_usd"))):
                raise ValueError("A new unconsumed paid profile is required")
        except (ValueError, TypeError, KeyError) as exc:
            raise APIError(400, "INVALID_REQUEST", "Paid pilot profile, counters or expiry are invalid.") from exc
        if (not question.strip() or grant_reference != PAID_GRANT_REFERENCE
                or model_key != "autotriager-gemini-paid-pilot" or resolved_model_id != PAID_MODEL
                or incident_id != "incident-0001"
                or expected_manifest_digest != "c5e764e916014a623f652f23713420b978e3d428d690f892a832971128296f78"
                or not isinstance(engine_build_id, str) or not engine_build_id
                or (previous_experiment_reference is not None and
                    (not isinstance(previous_experiment_reference, str) or not 1 <= len(previous_experiment_reference) <= 1024))):
            raise APIError(400, "INVALID_REQUEST", "Trusted setup differs from the explicit paid pilot.")
        stamp = now()
        with self._db(write=True) as db:
            incident = self._incident(db, incident_id)
            if (incident["mode"] != "offline_replay" or incident_id not in self._inspection_incidents
                    or incident["manifest_digest"] != expected_manifest_digest):
                raise APIError(409, "CONFIGURATION_NOT_READY", "The paid pilot requires the exact audited recorded case.")
            if db.execute("SELECT 1 FROM inference_grants LIMIT 1").fetchone():
                raise APIError(409, "INFERENCE_NOT_AUTHORIZED", "The separate paid state permits one workflow; existing approvals cannot be reused.")
            definition = {"kind": "explicit_fixed_paid_pilot", "decision_reference": grant_reference,
                "actor_scope": actor, "incident_id": incident_id, "manifest_digest": expected_manifest_digest,
                "data_revision": incident["data_revision"], "model_key": model_key, "resolved_model_id": resolved_model_id,
                "paid_budget": profile, "max_provider_calls": 10, "max_total_input_tokens": 80000,
                "max_total_output_tokens": 10240, "max_input_tokens_per_call": 8000, "max_output_tokens_per_call": 1024,
                "max_wall_time_seconds": 300, "paid_fallback_allowed": False,
                "previous_experiment_reference": previous_experiment_reference,
                "cost_basis": "Nominal Standard API fee before promotional offsets; not a measured invoice."}
            profile_digest = self._preserve(definition)
            grant_id = "gr_" + hashlib.sha256(grant_reference.encode("utf-8")).hexdigest()[:32]
            db.execute("INSERT INTO inference_grants(grant_id,actor_scope,status,permitted_mode,permitted_data_revision,permitted_manifest_digest,permitted_model_key,resolved_model_id,decision_reference,grant_digest,created_at,expires_at,max_provider_calls_total,max_total_provider_tokens) VALUES (?,?,'ACTIVE','offline_replay',?,?,?,?,?,?,?,?,10,90240)",
                (grant_id, actor, incident["data_revision"], expected_manifest_digest, model_key, resolved_model_id,
                 grant_reference, profile_digest, stamp, profile["expires_at"]))
            db.execute("INSERT INTO explicit_paid_authorizations VALUES (?,?,?,?,?,?,?,?)",
                (grant_id, profile_digest, encoded(definition), profile["max_cost_nano_usd"],
                 profile["input_nano_usd_per_token"], profile["output_nano_usd_per_token"], profile["created_at"], profile["expires_at"]))
            run_id, investigation_id = fixture.opaque("run"), fixture.opaque("inv")
            budget = dict(fixture.BUDGET, max_model_calls=10, max_tool_calls=5, max_source_bytes=262144,
                max_total_rows=1000, max_total_input_tokens=80000, max_total_output_tokens=10240,
                max_output_tokens=1024, max_wall_time_seconds=300)
            cfg = fixture.config(run_id, incident)
            cfg.pop("config_digest")
            cfg.update(mode="offline_replay", engine_build_id=engine_build_id,
                adapter_version="holmes-paid-pilot-v1", model_key=model_key, resolved_model_id=resolved_model_id,
                policy_id="explicit_paid_pilot_cli", policy_version="fixed-paid-v1", inference_authorized=True,
                grant_id=grant_id, grant_expires_at=profile["expires_at"], budget=budget,
                policy_digest=fixture.digest({"paid_authorization_digest": profile_digest, "budget": budget,
                    "allowed_tools": ["incident_manifest", "incident_services", "incident_metrics", "incident_logs", "incident_traces"]}))
            cfg["config_digest"] = fixture.digest(cfg)
            self._validate("RunConfig", cfg)
            db.execute("INSERT INTO investigations VALUES (?,?,?,?,?,?)",
                (investigation_id, incident_id, actor, "Explicit paid development pilot", stamp, stamp))
            db.execute("INSERT INTO run_configs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (cfg["config_digest"], run_id,
                incident_id, cfg["mode"], cfg["policy_digest"], cfg["engine_source_commit"], model_key,
                resolved_model_id, 1, grant_id, profile["expires_at"], encoded(cfg)))
            db.execute("INSERT INTO runs(run_id,investigation_id,incident_id,parent_run_id,question,config_digest,state,created_at,updated_at) VALUES (?,?,?,NULL,?,?,'CREATED',?,?)",
                (run_id, investigation_id, incident_id, question, cfg["config_digest"], stamp, stamp))
            deadline = min(expiry, datetime.now(timezone.utc) + timedelta(seconds=300)).isoformat(timespec="microseconds").replace("+00:00", "Z")
            db.execute("INSERT INTO run_budgets(run_id,max_model_calls,max_tool_calls,max_source_bytes,max_total_rows,max_total_input_tokens,max_total_output_tokens,max_output_tokens,max_wall_time_seconds,deadline_at) VALUES (?,10,5,262144,1000,80000,10240,1024,300,?)", (run_id, deadline))
            self._event(db, run_id, "warning", {"code": "EXPLICIT_PAID_PILOT", "message": "Separately authorized fixed-paid pilot; maximum nominal API fee USD2, ten generation intents, no fallback. Monetary authority digest " + profile_digest + ". No inference has been dispatched; root cause remains unverified."})
            self._transition(db, run_id, "QUEUED", "Explicit paid pilot prepared; awaiting reviewed provider admission.")
            return self._validate("Run", self._run(db, self._run_row(db, actor, run_id)))

    @staticmethod
    def _validate_paid_budget(value):
        from .engine.provider_gate import PAID_PROFILE_ID, PAID_RATE_CARD_SHA256
        fixed = {"profile_id": PAID_PROFILE_ID, "rate_card_sha256": PAID_RATE_CARD_SHA256, "currency": "USD",
            "input_nano_usd_per_token": 300, "output_nano_usd_per_token": 2500,
            "max_cost_nano_usd": 2000000000, "max_metadata_requests": 20}
        counters = {"metadata_requests": 20, "money_reserved_nano_usd": 2000000000, "known_fee_nano_usd": 2000000000}
        if (not isinstance(value, dict) or set(value) != set(fixed) | set(counters) | {"created_at", "expires_at"}
                or any(value[key] != expected or isinstance(value[key], bool) for key, expected in fixed.items())
                or any(isinstance(value[key], bool) or not isinstance(value[key], int) or not 0 <= value[key] <= bound for key, bound in counters.items())
                or value["known_fee_nano_usd"] > value["money_reserved_nano_usd"]):
            raise ValueError("Explicit paid budget differs from the fixed authorization")
        timestamp(value["created_at"])
        timestamp(value["expires_at"])
        return dict(value)

    @staticmethod
    def _model_policy_active(db, cfg, grant):
        if cfg["policy_id"] == "qualified_model_cli":
            return not db.execute("SELECT 1 FROM explicit_paid_authorizations WHERE grant_id=?", (cfg["grant_id"],)).fetchone()
        if cfg["policy_id"] != "explicit_paid_pilot_cli":
            return False
        row = db.execute("SELECT * FROM explicit_paid_authorizations WHERE grant_id=?", (cfg["grant_id"],)).fetchone()
        if not row or row["profile_digest"] != grant["grant_digest"] or row["expires_at"] != grant["expires_at"]:
            return False
        definition = json.loads(row["profile_json"])
        return (fixture.digest(definition) == row["profile_digest"]
            and definition["model_key"] == cfg["model_key"] and definition["resolved_model_id"] == cfg["resolved_model_id"]
            and cfg["policy_digest"] == fixture.digest({"paid_authorization_digest": row["profile_digest"], "budget": cfg["budget"],
                "allowed_tools": ["incident_manifest", "incident_services", "incident_metrics", "incident_logs", "incident_traces"]}))

    def resume_authorized_grant(self, actor, prior_run_id, usage_receipt, provider_ledger_snapshot,
                                *, review_reference, lease_expires_at=None):
        """One explicitly reviewed R1 continuation, with unchanged resource credit.

        Only the observed two-call, fully accounted qualification failure is
        eligible. The immutable original grant and failed run are not rewritten.
        This trusted CLI method has no browser or model endpoint.
        """
        prefix = "root-review-sha256:"
        if (not isinstance(review_reference, str) or not review_reference.startswith(prefix)
                or len(review_reference) != len(prefix) + 64
                or any(char not in "0123456789abcdef" for char in review_reference[len(prefix):])):
            raise APIError(409, "INFERENCE_NOT_AUTHORIZED", "An explicit root review digest is required for R1.")
        with self._db(write=True) as db:
            run = self._run_row(db, actor, prior_run_id)
            cfg = json.loads(db.execute("SELECT config_json FROM run_configs WHERE run_id=?", (prior_run_id,)).fetchone()[0])
            grant = db.execute("SELECT * FROM inference_grants WHERE grant_id=?", (cfg["grant_id"],)).fetchone()
            if (run["state"] != "FAILED" or run["termination_code"] != "QUALIFICATION_STOPPED"
                    or cfg["policy_id"] != "qualified_model_cli" or not cfg["inference_authorized"]
                    or cfg["model_key"] != "autotriager-gemini-qualified" or cfg["resolved_model_id"] != "gemini-3.1-flash-lite"
                    or grant is None or grant["status"] != "REVOKED" or grant["actor_scope"] != actor
                    or grant["used_provider_calls"] != 2 or grant["reserved_provider_calls"] != 0
                    or grant["max_provider_calls_total"] != 10 or grant["max_total_provider_tokens"] != 90240
                    or db.execute("SELECT 1 FROM reviewed_grant_leases WHERE grant_id=?", (cfg["grant_id"],)).fetchone()):
                raise APIError(409, "INFERENCE_NOT_AUTHORIZED", "Only the unchanged, known two-call R1 failure can be continued once.")
            if db.execute("SELECT 1 FROM runs r JOIN run_configs c ON c.run_id=r.run_id WHERE c.grant_id=? AND r.state NOT IN ('COMPLETED','FAILED','TIMED_OUT','BUDGET_EXHAUSTED','CANCELLED')", (cfg["grant_id"],)).fetchone():
                raise APIError(409, "CONFLICT", "The approval already has local work pending.")
            if (not isinstance(provider_ledger_snapshot, dict)
                    or not {"grant", "attempts", "reviewed_resumes"} <= set(provider_ledger_snapshot)
                    or set(provider_ledger_snapshot) - {"grant", "attempts", "reviewed_resumes", "reviewed_503_recoveries", "reviewed_template_repairs"}
                    or provider_ledger_snapshot["reviewed_resumes"] != []
                    or provider_ledger_snapshot.get("reviewed_503_recoveries", []) != []
                    or provider_ledger_snapshot.get("reviewed_template_repairs", []) != []):
                raise APIError(409, "INFERENCE_NOT_AUTHORIZED", "The exact unused R1 provider snapshot is required.")
            native_grant = provider_ledger_snapshot["grant"]
            attempts = provider_ledger_snapshot["attempts"]
            expected_config = {"grant_id": grant["decision_reference"], "case_digest": cfg["incident_manifest_digest"],
                "incident_id": run["incident_id"], "model": cfg["resolved_model_id"], "max_requests": 10,
                "max_input_per_request": 8000, "max_output_per_request": 1024,
                "max_total_input": 80000, "max_total_output": 10240, "paid_fallback": False}
            try:
                native_config = json.loads(native_grant["config_json"])
                valid_snapshot = (native_config == expected_config and native_grant["state"] == "CANCELLED"
                    and native_grant["reason"] == "qualification stopped; no further provider admission"
                    and native_grant["calls_reserved"] == 2 and native_grant["input_reserved"] == 16000
                    and native_grant["output_reserved"] == 2048 and isinstance(attempts, list) and len(attempts) == 2
                    and len({item["attempt_id"] for item in attempts}) == 2
                    and all(item["state"] == "SUCCEEDED" and item["model_version"] == cfg["resolved_model_id"]
                        and not isinstance(item["preflight_tokens"], bool) and isinstance(item["preflight_tokens"], int)
                        and item["preflight_tokens"] == item["actual_input"] and item["error_code"] is None
                        and isinstance(item["response_digest"], str) and len(item["response_digest"]) == 64 for item in attempts)
                    and {item["attempt_id"]: item for item in attempts} == {item["attempt_id"]: item for item in usage_receipt["provider_attempts"]})
            except (ValueError, KeyError, TypeError):
                valid_snapshot = False
            if not valid_snapshot:
                raise APIError(409, "INFERENCE_NOT_AUTHORIZED", "Provider R1 snapshot is changed, uncertain, failed or out of scope.")
            receipt_digest = fixture.digest(usage_receipt)
            receipt_path = self.assets_dir / (receipt_digest + ".json")
            linked = db.execute("SELECT 1 FROM events WHERE run_id=? AND event_type='warning' AND json_extract(payload_json,'$.code')='PROVIDER_RECEIPT_PRESERVED' AND json_extract(payload_json,'$.message')=?", (prior_run_id,
                "Actual gate receipt preserved with digest " + receipt_digest + "; billing cost is unknown rather than measured zero.")).fetchone()
            if not linked or not receipt_path.is_file() or receipt_path.read_bytes() != fixture.canonical(usage_receipt):
                raise APIError(409, "INFERENCE_NOT_AUTHORIZED", "The original accounted run receipt must remain preserved and unchanged.")
            before = tuple(grant[key] for key in ("used_provider_calls", "reserved_provider_calls", "used_provider_tokens", "reserved_provider_tokens"))
            try:
                _, complete = self._mirror_model_usage(db, run, cfg, usage_receipt)
            except ValueError as exc:
                raise APIError(409, "INFERENCE_NOT_AUTHORIZED", "The accounted R1 receipt failed validation.") from exc
            current = db.execute("SELECT * FROM inference_grants WHERE grant_id=?", (cfg["grant_id"],)).fetchone()
            after = tuple(current[key] for key in ("used_provider_calls", "reserved_provider_calls", "used_provider_tokens", "reserved_provider_tokens"))
            if not complete or before != after:
                raise APIError(409, "INFERENCE_NOT_AUTHORIZED", "R1 cannot change or reset accounted approval credit.")
            created = datetime.now(timezone.utc)
            expires = timestamp(lease_expires_at) if lease_expires_at else created + timedelta(hours=1)
            if not max(created, timestamp(grant["expires_at"])) < expires <= created + timedelta(hours=1):
                raise APIError(409, "INFERENCE_NOT_AUTHORIZED", "The one R1 technical lease must end within one hour.")
            created_text = created.isoformat(timespec="microseconds").replace("+00:00", "Z")
            expires_text = expires.isoformat(timespec="microseconds").replace("+00:00", "Z")
            reason = "R1 bounded repair of rejected model query arguments"
            snapshot_digest = self._preserve(provider_ledger_snapshot)
            definition = {"grant_id": grant["grant_id"], "parent_run_id": prior_run_id,
                "incident_id": run["incident_id"], "manifest_digest": cfg["incident_manifest_digest"],
                "resolved_model_id": cfg["resolved_model_id"], "original_expires_at": grant["expires_at"],
                "expires_at": expires_text, "created_at": created_text, "review_reference": review_reference,
                "reason": reason, "provider_snapshot_digest": snapshot_digest, "receipt_digest": receipt_digest,
                "grant_digest": grant["grant_digest"], "prior_used_provider_calls": before[0],
                "prior_reserved_provider_calls": before[1], "prior_used_provider_tokens": before[2],
                "prior_reserved_provider_tokens": before[3]}
            lease_digest = self._preserve(definition)
            db.execute("INSERT INTO reviewed_grant_leases VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (*definition.values(), lease_digest))
            changed = db.execute("UPDATE inference_grants SET status='ACTIVE' WHERE grant_id=? AND status='REVOKED'", (grant["grant_id"],)).rowcount
            if changed != 1:
                raise APIError(409, "CONFLICT", "Approval state changed before reviewed continuation.")
            self._event(db, prior_run_id, "warning", {"code": "R1_GRANT_RESUMED", "message": "One reviewed R1 technical lease recorded with digest " + lease_digest + "; original failed run, grant expiry, two calls and all ceilings remain preserved. Review " + review_reference})
            return grant["grant_id"]

    def resume_service503_grant(self, actor, prior_run_id, usage_receipt, provider_ledger_snapshot,
                                *, review_reference, owner_reference):
        """The owner's single known-HTTP503 exception; no new lease or credit."""
        owner = "human:call_111de2b3c9e1486896635dc5ba1fb133:one503recovery-same-ceiling"
        prefix = "root-review-sha256:"
        if (owner_reference != owner or not isinstance(review_reference, str)
                or not review_reference.startswith(prefix) or len(review_reference) != len(prefix) + 64
                or any(char not in "0123456789abcdef" for char in review_reference[len(prefix):])):
            raise APIError(409, "INFERENCE_NOT_AUTHORIZED", "The exact owner exception and fresh root review are required.")
        with self._db(write=True) as db:
            run = self._run_row(db, actor, prior_run_id)
            cfg = json.loads(db.execute("SELECT config_json FROM run_configs WHERE run_id=?", (prior_run_id,)).fetchone()[0])
            grant = db.execute("SELECT * FROM inference_grants WHERE grant_id=?", (cfg["grant_id"],)).fetchone()
            lease = db.execute("SELECT * FROM reviewed_grant_leases WHERE grant_id=?", (cfg["grant_id"],)).fetchone()
            if (run["state"] != "FAILED" or run["termination_code"] != "QUALIFICATION_STOPPED"
                    or cfg["policy_id"] != "qualified_model_cli" or not cfg["inference_authorized"]
                    or cfg["model_key"] != "autotriager-gemini-qualified" or cfg["resolved_model_id"] != "gemini-3.1-flash-lite"
                    or grant is None or grant["status"] != "REVOKED" or grant["actor_scope"] != actor
                    or grant["used_provider_calls"] != 3 or grant["reserved_provider_calls"] != 0
                    or grant["max_provider_calls_total"] != 10 or grant["max_total_provider_tokens"] != 90240
                    or lease is None or timestamp(lease["expires_at"]) <= datetime.now(timezone.utc)
                    or review_reference == lease["review_reference"]
                    or run["parent_run_id"] != lease["parent_run_id"] or cfg["grant_expires_at"] != lease["expires_at"]
                    or db.execute("SELECT 1 FROM service503_recoveries WHERE grant_id=?", (cfg["grant_id"],)).fetchone()):
                raise APIError(409, "INFERENCE_NOT_AUTHORIZED", "Only the known three-call 503 failure can be recovered once under its existing live lease.")
            if db.execute("SELECT 1 FROM runs r JOIN run_configs c ON c.run_id=r.run_id WHERE c.grant_id=? AND r.state NOT IN ('COMPLETED','FAILED','TIMED_OUT','BUDGET_EXHAUSTED','CANCELLED')", (cfg["grant_id"],)).fetchone():
                raise APIError(409, "CONFLICT", "The approval already has local work pending.")
            try:
                native = provider_ledger_snapshot["grant"]
                attempts = provider_ledger_snapshot["attempts"]
                resumes = provider_ledger_snapshot["reviewed_resumes"]
                expected = {"grant_id": grant["decision_reference"], "case_digest": cfg["incident_manifest_digest"],
                    "incident_id": run["incident_id"], "model": cfg["resolved_model_id"], "max_requests": 10,
                    "max_input_per_request": 8000, "max_output_per_request": 1024,
                    "max_total_input": 80000, "max_total_output": 10240, "paid_fallback": False}
                known, failed = attempts[:2], attempts[-1]
                valid = (set(provider_ledger_snapshot) in ({"grant", "attempts", "reviewed_resumes", "reviewed_503_recoveries"}, {"grant", "attempts", "reviewed_resumes", "reviewed_503_recoveries", "reviewed_template_repairs"})
                    and provider_ledger_snapshot.get("reviewed_template_repairs", []) == []
                    and provider_ledger_snapshot["reviewed_503_recoveries"] == [] and len(resumes) == 1
                    and resumes[0]["parent_run_id"] == lease["parent_run_id"]
                    and resumes[0]["previous_snapshot_digest"] == lease["provider_snapshot_digest"]
                    and resumes[0]["review_reference"] == lease["review_reference"]
                    and json.loads(native["config_json"]) == expected and native["state"] == "CANCELLED"
                    and native["reason"] == "qualification stopped; no further provider admission"
                    and native["calls_reserved"] == 3 and native["input_reserved"] == 24000 and native["output_reserved"] == 3072
                    and len(attempts) == 3 and len({item["attempt_id"] for item in attempts}) == 3
                    and all(item["state"] == "SUCCEEDED" and item["model_version"] == cfg["resolved_model_id"]
                        and not isinstance(item["actual_input"], bool) and isinstance(item["actual_input"], int) and 0 < item["actual_input"] <= 8000
                        and item["actual_input"] == item["preflight_tokens"]
                        and not isinstance(item["actual_output"], bool) and isinstance(item["actual_output"], int) and 0 <= item["actual_output"] <= 1024
                        and item["error_code"] is None and bool(item["response_digest"]) for item in known)
                    and failed["state"] == "FAILED_KNOWN" and failed["error_code"] == "HTTP_503"
                    and not isinstance(failed["preflight_tokens"], bool) and isinstance(failed["preflight_tokens"], int) and 0 < failed["preflight_tokens"] <= 8000
                    and all(failed[field] is None for field in ("actual_input", "actual_output", "model_version", "response_digest"))
                    and usage_receipt["provider_attempts"] == [failed] and usage_receipt["provider_calls"] == 1
                    and usage_receipt["input_tokens"] == 0 and usage_receipt["output_tokens"] == 0
                    and usage_receipt["reserved_input_tokens"] == 8000 and usage_receipt["reserved_output_tokens"] == 1024
                    and usage_receipt["unresolved"] is False)
            except (ValueError, KeyError, TypeError, IndexError):
                valid = False
            if not valid:
                raise APIError(409, "INFERENCE_NOT_AUTHORIZED", "503 recovery requires the unchanged known three-attempt snapshot and original one-attempt receipt.")
            original_snapshot_path = self.assets_dir / (lease["provider_snapshot_digest"] + ".json")
            if not original_snapshot_path.is_file():
                raise APIError(409, "INFERENCE_NOT_AUTHORIZED", "The original R1 provider snapshot must remain preserved.")
            original_snapshot_bytes = original_snapshot_path.read_bytes()
            if (hashlib.sha256(original_snapshot_bytes).hexdigest() != lease["provider_snapshot_digest"]
                    or json.loads(original_snapshot_bytes)["attempts"] != attempts[:2]):
                raise APIError(409, "INFERENCE_NOT_AUTHORIZED", "Prior successful attempts cannot be changed by a 503 recovery.")
            receipt_digest = fixture.digest(usage_receipt)
            path = self.assets_dir / (receipt_digest + ".json")
            linked = db.execute("SELECT 1 FROM events WHERE run_id=? AND event_type='warning' AND json_extract(payload_json,'$.code')='PROVIDER_RECEIPT_PRESERVED' AND json_extract(payload_json,'$.message')=?", (prior_run_id,
                "Actual gate receipt preserved with digest " + receipt_digest + "; billing cost is unknown rather than measured zero.")).fetchone()
            if not linked or not path.is_file() or path.read_bytes() != fixture.canonical(usage_receipt):
                raise APIError(409, "INFERENCE_NOT_AUTHORIZED", "The original one-attempt 503 usage receipt must remain preserved.")
            fields = ("used_provider_calls", "reserved_provider_calls", "used_provider_tokens", "reserved_provider_tokens")
            before = tuple(grant[key] for key in fields)
            try:
                _, complete = self._mirror_model_usage(db, run, cfg, usage_receipt)
            except ValueError as exc:
                raise APIError(409, "INFERENCE_NOT_AUTHORIZED", "The original partial 503 accounting failed validation.") from exc
            current = db.execute("SELECT * FROM inference_grants WHERE grant_id=?", (cfg["grant_id"],)).fetchone()
            after = tuple(current[key] for key in fields)
            if complete or before != after:
                raise APIError(409, "INFERENCE_NOT_AUTHORIZED", "503 recovery cannot change or release any prior resource credit.")
            definition = {"grant_id": grant["grant_id"], "parent_run_id": prior_run_id, "lease_digest": lease["lease_digest"],
                "owner_reference": owner_reference, "review_reference": review_reference,
                "reason": "One owner-approved recovery after known HTTP503", "provider_snapshot_digest": self._preserve(provider_ledger_snapshot),
                "receipt_digest": receipt_digest, "created_at": now(), "prior_used_provider_calls": before[0],
                "prior_reserved_provider_calls": before[1], "prior_used_provider_tokens": before[2], "prior_reserved_provider_tokens": before[3]}
            recovery_digest = self._preserve(definition)
            db.execute("INSERT INTO service503_recoveries VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (*definition.values(), recovery_digest))
            if db.execute("UPDATE inference_grants SET status='ACTIVE' WHERE grant_id=? AND status='REVOKED'", (grant["grant_id"],)).rowcount != 1:
                raise APIError(409, "CONFLICT", "Approval state changed before reviewed recovery.")
            self._event(db, prior_run_id, "warning", {"code": "SERVICE503_RECOVERY_AUTHORIZED", "message": "One owner-approved 503 recovery recorded with digest " + recovery_digest + "; existing lease, all three calls, reservations and original ceilings remain unchanged."})
            return grant["grant_id"]

    def resume_template_repair_grant(self, actor, prior_run_id, usage_receipt, provider_ledger_snapshot,
                                      *, review_reference):
        """One final owned-template repair within the original ten-call credit."""
        prefix = "root-review-sha256:"
        if (not isinstance(review_reference, str) or not review_reference.startswith(prefix)
                or len(review_reference) != len(prefix) + 64
                or any(char not in "0123456789abcdef" for char in review_reference[len(prefix):])):
            raise APIError(409, "INFERENCE_NOT_AUTHORIZED", "A fresh explicit root review is required for the final repair.")
        with self._db(write=True) as db:
            run = self._run_row(db, actor, prior_run_id)
            cfg = json.loads(db.execute("SELECT config_json FROM run_configs WHERE run_id=?", (prior_run_id,)).fetchone()[0])
            grant = db.execute("SELECT * FROM inference_grants WHERE grant_id=?", (cfg["grant_id"],)).fetchone()
            lease = db.execute("SELECT * FROM reviewed_grant_leases WHERE grant_id=?", (cfg["grant_id"],)).fetchone()
            recovery = db.execute("SELECT * FROM service503_recoveries WHERE grant_id=?", (cfg["grant_id"],)).fetchone()
            if (run["state"] != "FAILED" or run["termination_code"] != "QUALIFICATION_STOPPED"
                    or cfg["policy_id"] != "qualified_model_cli" or not cfg["inference_authorized"]
                    or cfg["resolved_model_id"] != "gemini-3.1-flash-lite" or cfg["model_key"] != "autotriager-gemini-qualified"
                    or grant is None or grant["status"] != "REVOKED" or grant["actor_scope"] != actor
                    or grant["used_provider_calls"] != 6 or grant["reserved_provider_calls"] != 0
                    or grant["max_provider_calls_total"] != 10 or grant["max_total_provider_tokens"] != 90240
                    or lease is None or recovery is None or timestamp(lease["expires_at"]) <= datetime.now(timezone.utc)
                    or run["parent_run_id"] != recovery["parent_run_id"] or cfg["grant_expires_at"] != lease["expires_at"]
                    or review_reference in (lease["review_reference"], recovery["review_reference"])
                    or db.execute("SELECT 1 FROM final_template_repairs WHERE grant_id=?", (cfg["grant_id"],)).fetchone()):
                raise APIError(409, "INFERENCE_NOT_AUTHORIZED", "Only the known six-call template failure can be repaired once under its existing lease.")
            if db.execute("SELECT 1 FROM runs r JOIN run_configs c ON c.run_id=r.run_id WHERE c.grant_id=? AND r.state NOT IN ('COMPLETED','FAILED','TIMED_OUT','BUDGET_EXHAUSTED','CANCELLED')", (cfg["grant_id"],)).fetchone():
                raise APIError(409, "CONFLICT", "The approval already has local work pending.")
            try:
                snapshot = provider_ledger_snapshot
                native, attempts = snapshot["grant"], snapshot["attempts"]
                expected = {"grant_id": grant["decision_reference"], "case_digest": cfg["incident_manifest_digest"],
                    "incident_id": run["incident_id"], "model": cfg["resolved_model_id"], "max_requests": 10,
                    "max_input_per_request": 8000, "max_output_per_request": 1024,
                    "max_total_input": 80000, "max_total_output": 10240, "paid_fallback": False}
                valid = (set(snapshot) == {"grant", "attempts", "reviewed_resumes", "reviewed_503_recoveries", "reviewed_template_repairs"}
                    and snapshot["reviewed_template_repairs"] == [] and len(snapshot["reviewed_resumes"]) == 1
                    and len(snapshot["reviewed_503_recoveries"]) == 1
                    and snapshot["reviewed_resumes"][0]["review_reference"] == lease["review_reference"]
                    and snapshot["reviewed_503_recoveries"][0]["review_reference"] == recovery["review_reference"]
                    and snapshot["reviewed_503_recoveries"][0]["parent_run_id"] == recovery["parent_run_id"]
                    and json.loads(native["config_json"]) == expected and native["state"] == "CANCELLED"
                    and native["reason"] == "qualification stopped; no further provider admission"
                    and native["calls_reserved"] == 6 and native["input_reserved"] == 48000 and native["output_reserved"] == 6144
                    and len(attempts) == 6 and len({item["attempt_id"] for item in attempts}) == 6
                    and usage_receipt["provider_attempts"] == attempts[3:] and usage_receipt["provider_calls"] == 3
                    and usage_receipt["unresolved"] is False
                    and all(item["state"] == "SUCCEEDED" and item["model_version"] == cfg["resolved_model_id"]
                        and not isinstance(item["actual_input"], bool) and isinstance(item["actual_input"], int) and 0 < item["actual_input"] <= 8000
                        and item["preflight_tokens"] == item["actual_input"]
                        and not isinstance(item["actual_output"], bool) and isinstance(item["actual_output"], int) and 0 <= item["actual_output"] <= 1024
                        and item["error_code"] is None and bool(item["response_digest"]) for item in attempts[3:]))
            except (ValueError, KeyError, TypeError, IndexError):
                valid = False
            if not valid:
                raise APIError(409, "INFERENCE_NOT_AUTHORIZED", "The final repair requires the unchanged six-attempt snapshot and latest original three-success receipt.")
            previous_path = self.assets_dir / (recovery["provider_snapshot_digest"] + ".json")
            if not previous_path.is_file():
                raise APIError(409, "INFERENCE_NOT_AUTHORIZED", "The prior three-attempt snapshot must remain preserved.")
            previous_bytes = previous_path.read_bytes()
            if (hashlib.sha256(previous_bytes).hexdigest() != recovery["provider_snapshot_digest"]
                    or json.loads(previous_bytes)["attempts"] != attempts[:3]):
                raise APIError(409, "INFERENCE_NOT_AUTHORIZED", "The two earlier successes and fully reserved known503 cannot be altered.")
            receipt_digest = fixture.digest(usage_receipt)
            path = self.assets_dir / (receipt_digest + ".json")
            linked = db.execute("SELECT 1 FROM events WHERE run_id=? AND event_type='warning' AND json_extract(payload_json,'$.code')='PROVIDER_RECEIPT_PRESERVED' AND json_extract(payload_json,'$.message')=?", (prior_run_id,
                "Actual gate receipt preserved with digest " + receipt_digest + "; billing cost is unknown rather than measured zero.")).fetchone()
            if not linked or not path.is_file() or path.read_bytes() != fixture.canonical(usage_receipt):
                raise APIError(409, "INFERENCE_NOT_AUTHORIZED", "The original three-success receipt must remain preserved and unchanged.")
            fields = ("used_provider_calls", "reserved_provider_calls", "used_provider_tokens", "reserved_provider_tokens")
            before = tuple(grant[key] for key in fields)
            try:
                _, complete = self._mirror_model_usage(db, run, cfg, usage_receipt)
            except ValueError as exc:
                raise APIError(409, "INFERENCE_NOT_AUTHORIZED", "Original template-failure usage failed validation.") from exc
            current = db.execute("SELECT * FROM inference_grants WHERE grant_id=?", (cfg["grant_id"],)).fetchone()
            if not complete or before != tuple(current[key] for key in fields):
                raise APIError(409, "INFERENCE_NOT_AUTHORIZED", "The final repair cannot reset or release any of six prior allocations.")
            definition = {"grant_id": grant["grant_id"], "parent_run_id": prior_run_id, "lease_digest": lease["lease_digest"],
                "service503_recovery_digest": recovery["recovery_digest"], "review_reference": review_reference,
                "reason": "Final routine repair of literal schema version and empty metric filter",
                "provider_snapshot_digest": self._preserve(provider_ledger_snapshot), "receipt_digest": receipt_digest,
                "created_at": now(), "prior_used_provider_calls": before[0], "prior_reserved_provider_calls": before[1],
                "prior_used_provider_tokens": before[2], "prior_reserved_provider_tokens": before[3]}
            repair_digest = self._preserve(definition)
            db.execute("INSERT INTO final_template_repairs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (*definition.values(), repair_digest))
            if db.execute("UPDATE inference_grants SET status='ACTIVE' WHERE grant_id=? AND status='REVOKED'", (grant["grant_id"],)).rowcount != 1:
                raise APIError(409, "CONFLICT", "Approval state changed before the final repair.")
            self._event(db, prior_run_id, "warning", {"code": "FINAL_TEMPLATE_REPAIR_AUTHORIZED", "message": "Final reviewed owned-template repair recorded with digest " + repair_digest + "; six calls, prior failures, existing lease and original ceilings are unchanged. At most four calls remain."})
            return grant["grant_id"]

    def start_model_run(self, run_id):
        """Trusted worker start, bound to the same active SQL approval mirror."""
        with self._db(write=True) as db:
            run = self._admit_fixture(db, run_id, "QUEUED")
            if run is None:
                return False
            cfg = json.loads(db.execute("SELECT config_json FROM run_configs WHERE run_id=?", (run_id,)).fetchone()[0])
            grant = db.execute("SELECT * FROM inference_grants WHERE grant_id=?", (cfg["grant_id"],)).fetchone()
            if (not cfg["inference_authorized"] or grant is None or not self._model_policy_active(db, cfg, grant)
                    or grant["status"] != "ACTIVE" or timestamp(self._effective_grant_expiry(db, grant)) <= datetime.now(timezone.utc)
                    or cfg["grant_expires_at"] != self._effective_grant_expiry(db, grant)):
                raise APIError(409, "INFERENCE_NOT_AUTHORIZED", "The qualified model approval is not active.")
            self._transition(db, run_id, "RUNNING", "Reviewed finite model investigation started; all provider requests require ProviderGate admission.")
            return True

    def model_run_active(self, run_id):
        """Read-only callback for ProviderGate before each outbound admission."""
        with self._db() as db:
            row = db.execute("SELECT r.state,c.grant_id,c.grant_expires_at,c.config_json,b.deadline_at FROM runs r JOIN run_configs c ON c.run_id=r.run_id JOIN run_budgets b ON b.run_id=r.run_id WHERE r.run_id=? AND c.inference_authorized=1", (run_id,)).fetchone()
            if not row:
                return False
            grant = db.execute("SELECT * FROM inference_grants WHERE grant_id=?", (row["grant_id"],)).fetchone()
            effective = self._effective_grant_expiry(db, grant) if grant else None
            return bool(grant and row["state"] == "RUNNING" and grant["status"] == "ACTIVE"
                        and self._model_policy_active(db, json.loads(row["config_json"]), grant)
                        and row["grant_expires_at"] == effective and timestamp(effective) > datetime.now(timezone.utc)
                        and timestamp(row["deadline_at"]) > datetime.now(timezone.utc))

    def _mirror_model_usage(self, db, run, cfg, receipt):
        paid = cfg["policy_id"] == "explicit_paid_pilot_cli"
        if paid:
            if not isinstance(receipt, dict) or "paid_budget" not in receipt:
                raise ValueError("Paid observations require the explicit monetary receipt")
            paid_budget = receipt["paid_budget"]
            receipt = {key: value for key, value in receipt.items() if key != "paid_budget"}
        required = {"grant_reference", "model_key", "resolved_model_id", "run_id", "incident_id", "case_digest",
                    "provider_attempts", "provider_calls", "input_tokens", "output_tokens", "unresolved",
                    "reserved_input_tokens", "reserved_output_tokens"}
        grant = db.execute("SELECT * FROM inference_grants WHERE grant_id=?", (cfg["grant_id"],)).fetchone()
        if (not isinstance(receipt, dict) or set(receipt) != required or grant is None
                or receipt["grant_reference"] != grant["decision_reference"] or receipt["model_key"] != cfg["model_key"]
                or receipt["resolved_model_id"] != cfg["resolved_model_id"] or receipt["run_id"] != run["run_id"]
                or receipt["incident_id"] != run["incident_id"] or receipt["case_digest"] != cfg["incident_manifest_digest"]
                or not isinstance(receipt["unresolved"], bool)
                or not isinstance(receipt["provider_attempts"], list) or len(receipt["provider_attempts"]) > 32):
            raise ValueError("Provider receipt is not bound to this run and approval")
        numbers = ("provider_calls", "input_tokens", "output_tokens", "reserved_input_tokens", "reserved_output_tokens")
        if any(isinstance(receipt[key], bool) or not isinstance(receipt[key], int) or receipt[key] < 0 for key in numbers):
            raise ValueError("Provider receipt counters must be finite nonnegative integers")
        attempts, identities = [], set()
        preflight = {"COUNTING", "ABORTED_PREFLIGHT", "REJECTED_PREFLIGHT"}
        if paid:
            preflight.add("METADATA_ONLY")
        allowed_states = preflight | {"DISPATCH_INTENT", "SUCCEEDED", "FAILED_KNOWN", "UNRESOLVED", "LATE_RESPONSE", "INVALID_USAGE", "INVALID_MODEL"}
        for attempt in receipt["provider_attempts"]:
            if (not isinstance(attempt, dict) or not isinstance(attempt.get("attempt_id"), str)
                    or attempt["attempt_id"] in identities or attempt.get("state") not in allowed_states):
                raise ValueError("Provider attempt identities or states are invalid")
            identities.add(attempt["attempt_id"])
            if attempt.get("preflight_tokens") is not None and attempt["state"] not in preflight:
                attempts.append(attempt)
        if len(attempts) != receipt["provider_calls"]:
            raise ValueError("Provider receipt call count disagrees with generation intents")
        input_known = output_known = 0
        uncertain_calls = 0
        all_observed = bool(attempts)
        for attempt in attempts:
            if attempt["state"] in ("DISPATCH_INTENT", "UNRESOLVED"):
                uncertain_calls += 1
            for field, bound in (("actual_input", 8000), ("actual_output", 1024)):
                value = attempt.get(field)
                if value is not None and (isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= bound):
                    raise ValueError("Observed provider usage exceeds its qualified per-call ceiling")
                if value is not None:
                    if field == "actual_input":
                        input_known += value
                    else:
                        output_known += value
            all_observed = all_observed and (attempt["state"] == "SUCCEEDED"
                and isinstance(attempt.get("actual_input"), int) and attempt["actual_input"] > 0
                and isinstance(attempt.get("actual_output"), int)
                and attempt.get("model_version") == cfg["resolved_model_id"])
        if (receipt["input_tokens"] != input_known or receipt["output_tokens"] != output_known
                or receipt["unresolved"] != bool(uncertain_calls)
                or receipt["reserved_input_tokens"] != 8000 * len(attempts) - input_known
                or receipt["reserved_output_tokens"] != 1024 * len(attempts) - output_known):
            raise ValueError("Provider receipt totals disagree with its preserved attempts")
        budget = db.execute("SELECT * FROM run_budgets WHERE run_id=?", (run["run_id"],)).fetchone()
        if (len(attempts) > budget["max_model_calls"] or input_known + receipt["reserved_input_tokens"] > budget["max_total_input_tokens"]
                or output_known + receipt["reserved_output_tokens"] > budget["max_total_output_tokens"]):
            raise ValueError("Provider receipt exceeds the frozen run ceilings")
        # Receipt counters are a snapshot, not an increment. This also permits a
        # definitive late observation without double charging a prior snapshot.
        if (len(attempts) < budget["used_model_calls"] + budget["reserved_model_calls"]
                or input_known < budget["used_input_tokens"] or output_known < budget["used_output_tokens"]):
            raise ValueError("Provider receipt accounting cannot move backwards")
        db.execute("UPDATE run_budgets SET used_model_calls=?,reserved_model_calls=?,used_input_tokens=?,reserved_input_tokens=?,used_output_tokens=?,reserved_output_tokens=? WHERE run_id=?",
            (len(attempts) - uncertain_calls, uncertain_calls, input_known, receipt["reserved_input_tokens"], output_known, receipt["reserved_output_tokens"], run["run_id"]))
        totals = db.execute("SELECT COALESCE(SUM(b.used_model_calls),0),COALESCE(SUM(b.reserved_model_calls),0),COALESCE(SUM(b.used_input_tokens+b.used_output_tokens),0),COALESCE(SUM(b.reserved_input_tokens+b.reserved_output_tokens),0) FROM run_budgets b JOIN run_configs c ON c.run_id=b.run_id WHERE c.grant_id=?", (cfg["grant_id"],)).fetchone()
        if totals[0] + totals[1] > grant["max_provider_calls_total"] or totals[2] + totals[3] > grant["max_total_provider_tokens"]:
            raise ValueError("Provider receipt exceeds the shared approval ledger")
        db.execute("UPDATE inference_grants SET used_provider_calls=?,reserved_provider_calls=?,used_provider_tokens=?,reserved_provider_tokens=? WHERE grant_id=?",
                   (*tuple(totals), cfg["grant_id"]))
        tools = budget["used_tool_calls"]
        usage = {"model_calls": len(attempts), "tool_calls": tools,
                 "input_tokens": input_known if all_observed else None,
                 "output_tokens": output_known if all_observed else None,
                 "provider_cost_usd": None, "accounting_status": "observed" if all_observed else "partial"}
        if paid:
            self._mirror_paid_cost(db, run, cfg, receipt, paid_budget, attempts)
        self._validate("UsageObservedPayload", usage)
        return usage, bool(all_observed and not uncertain_calls)

    def _mirror_paid_cost(self, db, run, cfg, receipt, paid_budget, attempts):
        value = self._validate_paid_budget(paid_budget)
        row = db.execute("SELECT * FROM explicit_paid_authorizations WHERE grant_id=?", (cfg["grant_id"],)).fetchone()
        if row is None:
            raise ValueError("Paid observations have no separate monetary authority")
        frozen = json.loads(row["profile_json"])["paid_budget"]
        mutable = {"metadata_requests", "money_reserved_nano_usd", "known_fee_nano_usd"}
        if any(value[key] != frozen[key] for key in set(value) - mutable):
            raise ValueError("Paid receipt changed its frozen profile, price or deadline")
        if value["metadata_requests"] != len(receipt["provider_attempts"]):
            raise ValueError("Metadata request accounting disagrees with durable preflight attempts")
        expected_ids = {attempt["attempt_id"] for attempt in attempts}
        total_fee = 0
        for attempt in receipt["provider_attempts"]:
            generated = attempt["attempt_id"] in expected_ids
            reserved = attempt.get("money_reserved_nano_usd")
            known = attempt.get("known_fee_nano_usd")
            if type(reserved) is not int or reserved != (4960000 if generated else 0):
                raise ValueError("Paid generation must retain its full nominal fee reservation")
            observed = generated and attempt["state"] in ("SUCCEEDED", "LATE_RESPONSE")
            if observed:
                if (type(attempt.get("actual_input")) is not int or type(attempt.get("actual_output")) is not int
                        or attempt.get("model_version") != cfg["resolved_model_id"]
                        or type(known) is not int or known != attempt["actual_input"] * 300 + attempt["actual_output"] * 2500):
                    raise ValueError("Observed nominal token fee disagrees with exact provider usage")
                total_fee += known
            elif known is not None:
                raise ValueError("Unknown or preflight-only work cannot claim a measured token fee")
        if (value["money_reserved_nano_usd"] != 4960000 * len(attempts)
                or value["known_fee_nano_usd"] != total_fee):
            raise ValueError("Paid monetary totals disagree with preserved attempts")
        prior = db.execute("SELECT * FROM paid_run_costs WHERE run_id=?", (run["run_id"],)).fetchone()
        if prior and any(value[key] < prior[key] for key in mutable):
            raise ValueError("Paid usage snapshots cannot reset consumed or reserved credit")
        db.execute("INSERT INTO paid_run_costs VALUES (?,?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET metadata_requests=excluded.metadata_requests,money_reserved_nano_usd=excluded.money_reserved_nano_usd,known_fee_nano_usd=excluded.known_fee_nano_usd",
            (run["run_id"], cfg["grant_id"], value["metadata_requests"], value["money_reserved_nano_usd"], total_fee))
        totals = db.execute("SELECT SUM(metadata_requests),SUM(money_reserved_nano_usd),SUM(known_fee_nano_usd) FROM paid_run_costs WHERE grant_id=?", (cfg["grant_id"],)).fetchone()
        if totals[0] > 20 or totals[1] > row["max_cost_nano_usd"] or totals[2] > totals[1]:
            raise ValueError("Paid observations exceed the explicit aggregate monetary authority")
        # The public v1 usage field remains null: this is a rate-card estimate,
        # not a provider invoice. The exact estimate is preserved separately.
        self._event(db, run["run_id"], "warning", {"code": "NOMINAL_PAID_FEE_RECORDED", "message":
            "Nominal Standard token fee " + str(total_fee) + " nanoUSD; full conservative reservation " + str(value["money_reserved_nano_usd"]) +
            " nanoUSD under explicit USD2 cap. This estimate is before offsets and is not a measured invoice."})

    @staticmethod
    def _answer_reference_issues(model, run, services, evidence):
        issues = []
        if model["run_id"] != run["run_id"] or model["incident_id"] != run["incident_id"]:
            issues.append("Answer run or incident identity is outside the frozen scope.")
        candidates = model["candidates"]
        if (model["status"] == "ranked_candidates" and not candidates
                or model["status"] == "insufficient_evidence" and candidates):
            issues.append("Answer status and candidate inventory disagree.")
        if [candidate["rank"] for candidate in candidates] != list(range(1, len(candidates) + 1)):
            issues.append("Candidate ranks must be contiguous and ordered from one.")
        chosen = [candidate["service_id"] for candidate in candidates]
        if len(set(chosen)) != len(chosen) or not set(chosen) <= services:
            issues.append("Candidate services must be unique members of the incident inventory.")
        known = {item["evidence_id"] for item in evidence}
        refs = [eid for candidate in candidates for key in ("supporting_evidence_ids", "contradicting_evidence_ids") for eid in candidate[key]]
        refs += [eid for item in model["observations"] for eid in item["evidence_ids"]]
        if not set(refs) <= known:
            issues.append("Answer evidence references must belong to this run and incident.")
        if any(item["content_digest"] != fixture.digest(item["record"]) for item in evidence):
            issues.append("Preserved evidence content digest verification failed.")
        return issues

    def finalize_model_run(self, run_id, raw_analysis, usage_receipt, *, failure_code=None):
        """Preserve actual engine analysis and gate observations; never call a model."""
        if not isinstance(raw_analysis, str) or len(raw_analysis.encode("utf-8")) > 1_000_000:
            raise ValueError("A bounded actual analysis string is required")
        if failure_code is not None and (not isinstance(failure_code, str) or not 1 <= len(failure_code) <= 80
                or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_" for character in failure_code)):
            raise ValueError("A bounded safe failure code is required")
        raw_digest = self._preserve({"kind": "actual_holmes_analysis", "analysis": raw_analysis} if failure_code is None
                                    else {"kind": "actual_model_worker_failure", "code": failure_code, "analysis": raw_analysis})
        receipt_digest = self._preserve(usage_receipt)
        with self._db(write=True) as db:
            run = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if run is None:
                raise self._not_found()
            cfg = json.loads(db.execute("SELECT config_json FROM run_configs WHERE run_id=?", (run_id,)).fetchone()[0])
            grant = db.execute("SELECT * FROM inference_grants WHERE grant_id=?", (cfg["grant_id"],)).fetchone()
            if not cfg["inference_authorized"] or grant is None or not self._model_policy_active(db, cfg, grant):
                raise APIError(409, "INFERENCE_NOT_AUTHORIZED", "This is not a qualified model run.")
            self._event(db, run_id, "warning", {"code": "MODEL_ANALYSIS_PRESERVED", "message": "Actual engine analysis or failure record preserved with digest " + raw_digest})
            db.execute("SAVEPOINT model_usage")
            try:
                usage, complete_usage = self._mirror_model_usage(db, run, cfg, usage_receipt)
            except ValueError:
                db.execute("ROLLBACK TO model_usage")
                db.execute("RELEASE model_usage")
                # The separate authoritative gate ledger still retains its
                # conservative reservations. Do not invent zero usage here.
                self._event(db, run_id, "warning", {"code": "PROVIDER_RECEIPT_REJECTED", "message": "Provider usage receipt failed scope/accounting checks; preserved digest " + receipt_digest})
                db.execute("UPDATE inference_grants SET status='REVOKED' WHERE grant_id=?", (cfg["grant_id"],))
                if run["state"] not in TERMINAL and run["state"] != "CANCEL_REQUESTED":
                    self._transition(db, run_id, "FAILED", "Provider accounting could not be accepted; no answer was committed.", "PROVIDER_RECEIPT_REJECTED")
                return False
            db.execute("RELEASE model_usage")
            self._event(db, run_id, "usage.observed", usage)
            self._event(db, run_id, "warning", {"code": "PROVIDER_RECEIPT_PRESERVED", "message": "Actual gate receipt preserved with digest " + receipt_digest + "; billing cost is unknown rather than measured zero."})
            if run["state"] in TERMINAL or run["state"] == "CANCEL_REQUESTED":
                self._event(db, run_id, "late_observation", {"raw_event_digest": raw_digest, "original_event_type": "holmes.analysis", "reason": "The local run was already fenced; late output cannot become its answer."})
                return False
            if failure_code is not None:
                db.execute("UPDATE inference_grants SET status='REVOKED' WHERE grant_id=?", (cfg["grant_id"],))
                self._event(db, run_id, "warning", {"code": failure_code, "message": "Actual model worker stopped; safe failure record preserved with digest " + raw_digest})
                self._transition(db, run_id, "FAILED", "Actual model worker stopped without an accepted answer.", failure_code)
                return False
            deadline = db.execute("SELECT deadline_at FROM run_budgets WHERE run_id=?", (run_id,)).fetchone()[0]
            if timestamp(deadline) <= datetime.now(timezone.utc):
                self._transition(db, run_id, "TIMED_OUT", "Actual output arrived after the local deadline; no answer committed.", "MODEL_DEADLINE")
                return False
            if run["state"] not in ("RUNNING", "VALIDATING"):
                raise APIError(409, "CONFLICT", "Model output cannot be accepted in this run state.")
            if run["state"] == "RUNNING":
                self._transition(db, run_id, "VALIDATING", "Checking actual model structure and references; semantic support remains unassessed.")
            if not complete_usage:
                db.execute("UPDATE inference_grants SET status='REVOKED' WHERE grant_id=?", (cfg["grant_id"],))
                self._transition(db, run_id, "FAILED", "Provider observations are partial, mismatched, or unresolved; no canonical answer committed.", "PROVIDER_ACCOUNTING_INCOMPLETE")
                return False
            structure = False
            issues = []
            try:
                def unique_object(pairs):
                    result = {}
                    for key, value in pairs:
                        if key in result:
                            raise ValueError("Duplicate model output key")
                        result[key] = value
                    return result
                model = json.loads(raw_analysis, object_pairs_hook=unique_object,
                                   parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite model number")))
                self._validate("ModelAnswer", model)
                structure = True
                evidence = [self._evidence(item) for item in db.execute("SELECT * FROM evidence WHERE run_id=? AND incident_id=? ORDER BY evidence_id", (run_id, run["incident_id"])).fetchall()]
                issues = self._answer_reference_issues(model, run, set(self._incident(db, run["incident_id"])["service_ids"]), evidence)
            except (ValueError, TypeError, APIError):
                issues = ["Actual model output is not a valid strict ModelAnswer JSON object."]
            if issues:
                self._event(db, run_id, "validation.finished", {"structure_valid": structure, "reference_valid": False, "issues": issues})
                self._event(db, run_id, "warning", {"code": "MODEL_OUTPUT_REJECTED", "message": "Rejected actual analysis preserved with digest " + raw_digest})
                self._transition(db, run_id, "FAILED", "Actual model output failed structure or reference validation.", "MODEL_OUTPUT_REJECTED")
                return False
            if timestamp(deadline) <= datetime.now(timezone.utc):
                self._transition(db, run_id, "TIMED_OUT", "Output validation crossed the local deadline; no answer committed.", "MODEL_DEADLINE")
                return False
            return self._commit_answer(db, run, model, evidence,
                "Actual finite model output accepted mechanically; root cause and semantic support are not certified.",
                raw_digest=raw_digest, usage=usage, emit_usage=False)

    def fail_model_run(self, run_id, raw_analysis, usage_receipt, termination_code="MODEL_WORKER_FAILED"):
        return self.finalize_model_run(run_id, raw_analysis, usage_receipt, failure_code=termination_code)

    def _event(self, db, run_id, event_type, payload, call_id=None):
        sequence = db.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM events WHERE run_id=?", (run_id,)).fetchone()[0]
        event = self._validate("Event", {"schema_version": VERSION, "event_id": fixture.opaque("evt"), "run_id": run_id,
                "sequence": sequence, "received_at": now(), "upstream_observed_at": None,
                "event_type": event_type, "upstream_call_id": call_id, "payload": payload})
        db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)", (event["event_id"], run_id, sequence, VERSION, event["received_at"], None, event_type, call_id, encoded(payload)))
        return event

    def _transition(self, db, run_id, state, reason=None, termination_code=None):
        row = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise self._not_found()
        # The event and state CAS commit atomically. The event must precede a
        # terminal UPDATE because SQL forbids ordinary events after the fence.
        self._event(db, run_id, "run.state_changed", {"from_state": row["state"], "to_state": state, "reason": reason})
        changed = db.execute("UPDATE runs SET state=?,revision=revision+1,updated_at=?,termination_code=? WHERE run_id=? AND revision=?", (state, now(), termination_code, run_id, row["revision"])).rowcount
        if changed != 1:
            raise RuntimeError("Run revision changed during a serialized transaction")

    def get_events(self, actor, run_id, after=0):
        if isinstance(after, bool) or not isinstance(after, int) or after < 0:
            raise APIError(400, "INVALID_CURSOR", "The event cursor must be a non-negative integer.")
        with self._db() as db:
            self._run_row(db, actor, run_id)
            return [{"schema_version": r["schema_version"], "event_id": r["event_id"], "run_id": r["run_id"],
                     "sequence": r["sequence"], "received_at": r["received_at"], "upstream_observed_at": r["upstream_observed_at"],
                     "event_type": r["event_type"], "upstream_call_id": r["upstream_call_id"], "payload": json.loads(r["payload_json"])}
                    for r in db.execute("SELECT * FROM events WHERE run_id=? AND sequence>? ORDER BY sequence", (run_id, after)).fetchall()]

    @staticmethod
    def _evidence(row):
        return {"schema_version": VERSION, "evidence_id": row["evidence_id"], "run_id": row["run_id"],
                "incident_id": row["incident_id"], "query_id": row["query_id"], "service_id": row["service_id"],
                "modality": row["modality"], "kind": row["kind"], "source_artifact_digest": row["source_artifact_digest"],
                "record_locator": row["record_locator"], "content_digest": row["content_digest"],
                "observed_window": {"start": row["observed_start"], "end": row["observed_end"]},
                "preserved_at": row["preserved_at"], "unit": row["unit"], "record": json.loads(row["record_json"]),
                "derivation": json.loads(row["derivation_json"]) if row["derivation_json"] else None,
                "limitations": json.loads(row["limitations_json"])}

    def get_evidence(self, actor, run_id, evidence_id):
        with self._db() as db:
            run = self._run_row(db, actor, run_id)
            row = db.execute("SELECT * FROM evidence WHERE evidence_id=? AND run_id=? AND incident_id=?", (evidence_id, run_id, run["incident_id"])).fetchone()
            if row is None:
                raise self._not_found()
            return self._evidence(row)

    def get_query(self, actor, run_id, query_id):
        with self._db() as db:
            run = self._run_row(db, actor, run_id)
            q = db.execute("SELECT parameters_json FROM queries WHERE query_id=? AND run_id=? AND incident_id=?", (query_id, run_id, run["incident_id"])).fetchone()
            if q is None:
                raise self._not_found()
            result = db.execute("SELECT result_json FROM query_results WHERE query_id=?", (query_id,)).fetchone()
            return {"schema_version": VERSION, "query": json.loads(q[0]), "result": json.loads(result[0]) if result else None}

    def cancel_run(self, actor, run_id):
        with self._db(write=True) as db:
            row = self._run_row(db, actor, run_id)
            cfg = json.loads(db.execute("SELECT config_json FROM run_configs WHERE run_id=?", (run_id,)).fetchone()[0])
            remote = self._remote_state(db, row, cfg)
            actual = cfg["inference_authorized"]
            if row["state"] not in TERMINAL:
                db.execute("UPDATE gateway_capabilities SET revoked_at=? WHERE run_id=? AND revoked_at IS NULL", (now(), run_id))
                if actual:
                    db.execute("UPDATE inference_grants SET status='REVOKED' WHERE grant_id=?", (cfg["grant_id"],))
                if row["state"] != "CANCEL_REQUESTED":
                    self._transition(db, run_id, "CANCEL_REQUESTED", "Local cancellation requested.")
                reason = ("Local model work fenced; stopping an in-flight provider request is not established."
                          if actual and remote == "unknown" else "Local work cancelled; no pending external request is established.")
                self._transition(db, run_id, "CANCELLED", reason, "USER_CANCELLED")
            current = self._run(db, self._run_row(db, actor, run_id))
            return self._validate("CancelReceipt", {"schema_version": VERSION, "run_id": run_id, "state": current["state"],
                "logical_fence_active": True, "provider_dispatch_pending": remote == "unknown", "remote_stop_status": remote,
                "billing_may_continue": actual and remote == "unknown", "last_sequence": current["last_sequence"]})

    def start_fixture(self, run_id):
        with self._db(write=True) as db:
            row = self._admit_fixture(db, run_id, "QUEUED")
            if row is None:
                return False
            if self._incident(db, row["incident_id"])["mode"] != "fixture":
                raise RuntimeError("The fixture worker cannot process actual data")
            self._transition(db, run_id, "RUNNING", "Deterministic synthetic worker; no Holmes or provider execution.")
            return True

    def start_inspection(self, run_id):
        with self._db(write=True) as db:
            row = self._admit_fixture(db, run_id, "QUEUED")
            if row is None:
                return False
            cfg = json.loads(db.execute("SELECT config_json FROM run_configs WHERE run_id=?", (run_id,)).fetchone()[0])
            if cfg["policy_id"] != "offline_inspection" or cfg["budget"]["max_model_calls"] != 0:
                raise RuntimeError("Inspection worker requires the explicit zero-model policy")
            self._transition(db, run_id, "RUNNING", "Reading bounded original telemetry; no model reasoning or root-cause conclusion.")
            return True

    def _admit_fixture(self, db, run_id, required_state):
        row = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None or row["state"] != required_state:
            return None
        budget = db.execute("SELECT deadline_at FROM run_budgets WHERE run_id=?", (run_id,)).fetchone()
        if budget is None:
            raise RuntimeError("Fixture run has no budget")
        if datetime.now(timezone.utc) >= datetime.fromisoformat(budget[0]):
            self._transition(db, run_id, "TIMED_OUT", "The persisted local wall-time deadline expired.", "LOCAL_DEADLINE_EXCEEDED")
            return None
        return row

    def persist_fixture_query(self, run_id, modality):
        with self._db(write=True) as db:
            run = self._admit_fixture(db, run_id, "RUNNING")
            if run is None:
                return False
            inc = self._incident(db, run["incident_id"])
            query, result, evidence, source = fixture.query_bundle(run_id, inc, modality, now())
            budget = db.execute("SELECT * FROM run_budgets WHERE run_id=?", (run_id,)).fetchone()
            reply = self._fit_tool_reply(query, result, evidence,
                       budget["max_source_bytes"] - budget["used_source_bytes"],
                       budget["max_total_rows"] - budget["used_rows"])
            if budget["used_tool_calls"] + 1 > budget["max_tool_calls"] or reply is None:
                self._transition(db, run_id, "BUDGET_EXHAUSTED", "Local fixture aggregate budget exhausted.", "LOCAL_BUDGET_EXHAUSTED")
                return False
            result, evidence = reply["result"], reply["evidence"]
            # The private engine-facing object is fully materialized, validated,
            # preserved and charged before any success/event is committed. This
            # counts envelope, query, hashes, lineage and derived records too.
            reply_bytes = len(fixture.canonical(reply))
            self._preserve(reply)
            self._preserve(source)
            db.execute("INSERT INTO queries VALUES (?,?,?,?,?,?,?,?,?,?)", (query["query_id"], run_id, inc["incident_id"], query["tool_name"], modality, query["time_window"]["start"], query["time_window"]["end"], query["limit"], query["max_bytes"], encoded(query)))
            self._event(db, run_id, "tool.started", {"query_id": query["query_id"], "tool_name": query["tool_name"]}, query["query_id"])
            # Preserve the immutable result before advertising evidence IDs.
            db.execute("INSERT INTO query_results VALUES (?,?,?,?,?,?,?,?)", (result["query_id"], result["status"], result["availability"], result["returned_count"], result["matched_count"], int(result["truncated"]), int(result["result_complete"]), encoded(result)))
            for item in evidence:
                self._validate("Evidence", item)
                self._preserve(item["record"])
                db.execute("INSERT INTO evidence VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (item["evidence_id"], run_id, inc["incident_id"], query["query_id"], item["service_id"], modality, item["kind"], item["source_artifact_digest"], item["record_locator"], item["content_digest"], item["observed_window"]["start"], item["observed_window"]["end"], item["preserved_at"], item["unit"], encoded(item["record"]), encoded(item["derivation"]) if item["derivation"] else None, encoded(item["limitations"])))
                if item["derivation"]:
                    for parent in item["derivation"]["parent_evidence_ids"]:
                        db.execute("INSERT INTO evidence_parents VALUES (?,?,?,?)", (item["evidence_id"], parent, run_id, inc["incident_id"]))
            self._preserve_tool_reply(db, reply)
            db.execute("UPDATE run_budgets SET used_tool_calls=used_tool_calls+1,used_source_bytes=used_source_bytes+?,used_rows=used_rows+? WHERE run_id=?", (reply_bytes, result["returned_count"], run_id))
            if evidence:
                self._event(db, run_id, "evidence.added", {"evidence_ids": result["evidence_ids"]})
            self._event(db, run_id, "tool.finished", {"query_id": query["query_id"], "status": result["status"], "evidence_ids": result["evidence_ids"], "returned_count": result["returned_count"], "truncated": result["truncated"], "error": None}, query["query_id"])
            return True

    def _fit_tool_reply(self, query, result, evidence, available_bytes, available_rows):
        """Fit whole records deterministically; never retain orphan aggregates.

        Row ceilings count every returned Evidence object (raw and derived).
        Removing a parent also removes each dependent aggregate, so a retained
        mean never silently changes its declared input slice.
        """
        selected = list(evidence)
        while True:
            fitted = dict(result)
            fitted["evidence_ids"] = [item["evidence_id"] for item in selected]
            fitted["returned_count"] = len(selected)
            if len(selected) != len(evidence):
                fitted.update(status="truncated", truncated=True, result_complete=False,
                              limitations=result["limitations"] + ["Whole records were omitted to satisfy the serialized reply byte or row ceiling; dependent aggregates were omitted with missing parents."])
            reply = self._validate("ToolReply", {"schema_version": VERSION, "query": query, "result": fitted,
                        "evidence": selected, "manifest": None, "service_ids": []})
            fits = (len(selected) <= min(query["limit"], available_rows)
                    and len(fixture.canonical(reply)) <= min(query["max_bytes"], available_bytes))
            if fits and (selected or not evidence):
                return reply
            if not selected:
                return None
            removed = {selected[-1]["evidence_id"]}
            while True:
                dependent = {item["evidence_id"] for item in selected if item["derivation"]
                             and removed.intersection(item["derivation"]["parent_evidence_ids"])}
                if dependent <= removed:
                    break
                removed.update(dependent)
            selected = [item for item in selected if item["evidence_id"] not in removed]

    def _tool_reply(self, db, run_id, query_id):
        """Reopen admitted bytes without telemetry access or model execution."""
        row = db.execute("SELECT parameters_json FROM queries WHERE query_id=? AND run_id=?", (query_id, run_id)).fetchone()
        result = db.execute("SELECT result_json FROM query_results WHERE query_id=?", (query_id,)).fetchone()
        if row is None or result is None:
            raise self._not_found()
        query, value = json.loads(row[0]), json.loads(result[0])
        artifact = db.execute("SELECT artifact_digest,byte_length FROM query_reply_artifacts WHERE query_id=? AND run_id=? AND incident_id=?", (query_id, run_id, query["incident_id"])).fetchone()
        if artifact:
            raw = (self.assets_dir / (artifact["artifact_digest"] + ".json")).read_bytes()
            if len(raw) != artifact["byte_length"] or hashlib.sha256(raw).hexdigest() != artifact["artifact_digest"]:
                raise RuntimeError("Preserved tool reply digest or byte length changed")
            reply = self._validate("ToolReply", json.loads(raw))
            if reply["query"] != query or reply["result"] != value:
                raise RuntimeError("Preserved reply does not match its immutable scoped query")
            return reply
        evidence = []
        for eid in value["evidence_ids"]:
            item = db.execute("SELECT * FROM evidence WHERE evidence_id=? AND run_id=? AND incident_id=?", (eid, run_id, query["incident_id"])).fetchone()
            if item is None:
                raise RuntimeError("Preserved tool reply has a missing evidence record")
            evidence.append(self._evidence(item))
        # Legacy v0.1 replies already have content-addressed files but no link.
        # Reconstruct only to locate and verify those original bytes.
        manifest = self._incident(db, query["incident_id"]) if query["tool_name"] == "incident_manifest" else None
        services = query["service_ids"][:value["returned_count"]] if query["tool_name"] == "incident_services" else []
        reply = self._validate("ToolReply", {"schema_version": VERSION, "query": query, "result": value,
                    "evidence": evidence, "manifest": manifest, "service_ids": services})
        raw = fixture.canonical(reply)
        path = self.assets_dir / (hashlib.sha256(raw).hexdigest() + ".json")
        if not path.is_file() or path.read_bytes() != raw:
            raise RuntimeError("Original legacy tool reply is unavailable; it cannot be invented")
        return reply

    def begin_validation(self, run_id):
        with self._db(write=True) as db:
            if self._admit_fixture(db, run_id, "RUNNING") is None:
                return False
            self._transition(db, run_id, "VALIDATING", "Checking schema and reference scope only; semantic support is not assessed.")
            return True

    def complete_fixture(self, run_id):
        with self._db(write=True) as db:
            run = self._admit_fixture(db, run_id, "VALIDATING")
            if run is None:
                return False
            evidence = [self._evidence(e) for e in db.execute("SELECT * FROM evidence WHERE run_id=? ORDER BY evidence_id", (run_id,)).fetchall()]
            model = self._validate("ModelAnswer", fixture.model_answer(run_id, run["incident_id"], evidence))
            return self._commit_answer(db, run, model, evidence, "Synthetic fixture complete; schema/reference checks do not validate root cause.")

    def complete_inspection(self, run_id, observations, unknowns):
        """Store a deterministic inventory, explicitly withholding diagnosis."""
        with self._db(write=True) as db:
            run = self._admit_fixture(db, run_id, "VALIDATING")
            if run is None:
                return False
            cfg = json.loads(db.execute("SELECT config_json FROM run_configs WHERE run_id=?", (run_id,)).fetchone()[0])
            if cfg["policy_id"] != "offline_inspection":
                raise RuntimeError("Wrong policy for an inspection report")
            evidence = [self._evidence(e) for e in db.execute("SELECT * FROM evidence WHERE run_id=? ORDER BY evidence_id", (run_id,)).fetchall()]
            model = self._validate("ModelAnswer", {"schema_version": VERSION, "run_id": run_id, "incident_id": run["incident_id"],
                "status": "insufficient_evidence", "candidates": [], "observations": observations,
                "unknowns": unknowns, "next_checks": ["Review the preserved query scope and original records.",
                    "Authorize a finite model experiment before evaluating diagnosis accuracy."]})
            return self._commit_answer(db, run, model, evidence, "Read-only data inspection complete; no model diagnosis or confirmed root cause.")

    def _commit_answer(self, db, run, model, evidence, reason, *, raw_digest=None, usage=None, emit_usage=True):
        run_id = run["run_id"]
        # Deterministic fixture/inspection callers preserve their generated
        # object. Model callers pass the separately preserved actual analysis.
        if raw_digest is None:
            raw_digest = self._preserve(model)
        issues = self._answer_reference_issues(model, run, set(self._incident(db, run["incident_id"])["service_ids"]), evidence)
        if issues:
            raise RuntimeError("Answer failed scope, rank, status, or preserved-content checks")
        answer = dict(model, answer_id=fixture.opaque("ans"), raw_output_digest=raw_digest,
                      structure_valid=True, reference_valid=True, semantic_support_status="not_assessed", created_at=now())
        self._validate("Answer", answer)
        db.execute("INSERT INTO answers VALUES (?,?,?,?,?,?,?,?,?,?)", (answer["answer_id"], run_id, run["incident_id"], answer["status"], raw_digest, 1, 1, "not_assessed", encoded(answer), answer["created_at"]))
        for candidate in answer["candidates"]:
            db.execute("INSERT INTO answer_candidates VALUES (?,?,?,?,?,?)", (answer["answer_id"], candidate["rank"], run_id, run["incident_id"], candidate["service_id"], candidate["hypothesis"]))
            for key, relation in (("supporting_evidence_ids", "support"), ("contradicting_evidence_ids", "contradict")):
                for eid in candidate[key]:
                    db.execute("INSERT INTO answer_evidence_links VALUES (?,?,?,?,?,?)", (answer["answer_id"], candidate["rank"], eid, run_id, run["incident_id"], relation))
        self._event(db, run_id, "answer.received", {"answer_id": answer["answer_id"], "output_digest": raw_digest})
        self._event(db, run_id, "validation.finished", {"structure_valid": True, "reference_valid": True, "issues": []})
        if emit_usage:
            tools = db.execute("SELECT used_tool_calls FROM run_budgets WHERE run_id=?", (run_id,)).fetchone()[0]
            self._event(db, run_id, "usage.observed", usage if usage is not None else
                {"model_calls": 0, "tool_calls": tools, "input_tokens": 0, "output_tokens": 0, "provider_cost_usd": 0, "accounting_status": "observed"})
        self._transition(db, run_id, "COMPLETED", reason)
        return True

    def fail_fixture(self, run_id, code="FIXTURE_WORKER_FAILED"):
        with self._db(write=True) as db:
            row = db.execute("SELECT state FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row and row[0] not in TERMINAL:
                if row[0] == "CANCEL_REQUESTED":
                    self._transition(db, run_id, "CANCELLED", "Local cancellation finalized.", "USER_CANCELLED")
                else:
                    self._event(db, run_id, "warning", {"code": code, "message": "Local worker failed; preserved records remain inspectable."})
                    self._transition(db, run_id, "FAILED", "Local worker failed.", code)
