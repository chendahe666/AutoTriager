"""Reviewed source observations, not learned weights or certified root causes.

No model, credential discovery, evaluator input or arbitrary path upload is used.
Public imports are reconstructed by an operator-supplied, source-checking loader.
"""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import threading
import uuid

from .errors import APIError
from .process_lock import ProcessLock
from .store import TERMINAL

STATES = ("CANDIDATE", "REVIEWED", "PUBLISHED", "QUARANTINED", "REVOKED")
SHA = re.compile(r"[0-9a-f]{64}\Z")
IDENT = re.compile(r"[A-Za-z0-9_.:-]{1,256}\Z")
INJECTION = re.compile(r"ignore\s+(?:all\s+|the\s+)?(?:previous|prior)\s+instructions|system\s+prompt|developer\s+message|exfiltrat|api[_ -]?key\s*[:=]|reveal\s+(?:the\s+)?api[_ -]?key|publish\s+(?:this|all)\s+memor", re.I)
FORBIDDEN_FIELDS = {"root_cause_service", "ground_truth", "private_labels", "evaluator_answer", "api_key", "access_token", "secret_key"}
# Inspectable domain vocabulary, never case IDs, fault labels or outcomes.
QUERY_ALIASES = {"90分位延迟": "latency-90", "50分位延迟": "latency-50", "商品目录": "productcatalogservice",
                 "购物车": "cartservice", "结账": "checkoutservice", "支付": "paymentservice", "推荐": "recommendationservice",
                 "货币": "currencyservice", "广告": "adservice", "邮件": "emailservice", "前端": "frontendservice",
                 "内存": "mem", "处理器": "cpu", "延迟": "latency", "工作负载": "workload", "连接数": "socket"}
GENERIC_TERMS = set("a an the is are was were for of to in on this that these those and or not no what which how can should may current recorded records record incident investigate especially observed field service services summarize summary establish establishes cause root unconfirmed supplied published history prior observation observations involving exact cite memory id state concrete historical explain whether actually proves relevant explicitly unavailable invent past case with without mean max min population_std n unknown formula query grouping time_window onset end start pre post source modality unit".split())
DDL = (
    "CREATE TABLE IF NOT EXISTS knowledge_meta(name TEXT PRIMARY KEY,identity TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS knowledge_entries(
        memory_id TEXT PRIMARY KEY,actor_scope TEXT NOT NULL,fingerprint TEXT NOT NULL,
        revision INTEGER NOT NULL,state TEXT NOT NULL,updated_at TEXT NOT NULL,
        entry_json TEXT NOT NULL,origin_json TEXT NOT NULL,
        UNIQUE(actor_scope,fingerprint))""",
    """CREATE TABLE IF NOT EXISTS knowledge_versions(
        memory_id TEXT NOT NULL REFERENCES knowledge_entries(memory_id),revision INTEGER NOT NULL,
        entry_json TEXT NOT NULL,PRIMARY KEY(memory_id,revision))""",
    """CREATE TABLE IF NOT EXISTS knowledge_events(
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,memory_id TEXT NOT NULL,
        revision INTEGER NOT NULL,action TEXT NOT NULL,actor_type TEXT NOT NULL,
        at TEXT NOT NULL,basis_json TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS knowledge_actions(
        actor_scope TEXT NOT NULL,route TEXT NOT NULL,key TEXT NOT NULL,
        request_digest TEXT NOT NULL,response_json TEXT NOT NULL,
        PRIMARY KEY(actor_scope,route,key))""",
    """CREATE TABLE IF NOT EXISTS knowledge_captures(
        actor_scope TEXT NOT NULL,run_id TEXT NOT NULL,memory_id TEXT,
        status TEXT NOT NULL,detail TEXT NOT NULL,at TEXT NOT NULL,
        PRIMARY KEY(actor_scope,run_id))""",
    """CREATE TABLE IF NOT EXISTS knowledge_snapshots(
        snapshot_id TEXT PRIMARY KEY,actor_scope TEXT NOT NULL,at TEXT NOT NULL,
        snapshot_json TEXT NOT NULL)""",
    """CREATE TRIGGER IF NOT EXISTS knowledge_version_no_update BEFORE UPDATE ON knowledge_versions
        BEGIN SELECT RAISE(ABORT,'memory versions are immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS knowledge_version_no_delete BEFORE DELETE ON knowledge_versions
        BEGIN SELECT RAISE(ABORT,'memory versions are immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS knowledge_event_no_update BEFORE UPDATE ON knowledge_events
        BEGIN SELECT RAISE(ABORT,'memory decisions are append only'); END""",
    """CREATE TRIGGER IF NOT EXISTS knowledge_event_no_delete BEFORE DELETE ON knowledge_events
        BEGIN SELECT RAISE(ABORT,'memory decisions are append only'); END""",
)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def content_fingerprint(fact):
    return digest({key: fact[key] for key in ("modality", "service_id", "record", "unit")})


def stable_fact_id(fact):
    # Run/evidence IDs are deliberately excluded: reopening a source is not a
    # new independent observation or a way around duplicate-source exclusion.
    return "fact_" + digest({"content": content_fingerprint(fact),
                            "source_artifact_digest": fact["source_artifact_digest"],
                            "record_locator": fact["record_locator"]})[:32]


def expanded_query(query):
    result = query.lower()
    used = []
    for original in sorted(QUERY_ALIASES, key=lambda value: (-len(value), value)):
        if original in result:
            result = result.replace(original, " " + QUERY_ALIASES[original] + " ")
            used.append({"from": original, "to": QUERY_ALIASES[original]})
    return result, used


def sparse_terms(value):
    tokens = set(re.findall(r"[a-z0-9]+(?:[_.-][a-z0-9]+)*", value.lower()))
    parts = {part for token in tokens for part in re.split(r"[_.-]", token)}
    return (tokens | parts) - GENERIC_TERMS


def value_fact(fact):
    value = deepcopy(fact)
    value["record"] = {key: item for key, item in value["record"].items() if key not in {"query", "formula"}}
    return value


def intent_score(fact, query_text, tokens, requested_services):
    projected = value_fact(fact)
    record = projected["record"]
    service_match = fact["service_id"].lower() in requested_services
    metric = record.get("metric")
    direct_metric = False
    if isinstance(metric, str):
        metric = metric.lower()
        suffix = metric.removeprefix(fact["service_id"].lower() + "_")
        direct_metric = metric in tokens or (service_match and sparse_terms(suffix) <= tokens and bool(sparse_terms(suffix)))
    lexical = len(tokens & sparse_terms(canonical({"record": record, "service_id": fact["service_id"], "modality": fact["modality"]})))
    return {"score": 50 * int(direct_metric) + 5 * int(service_match) + lexical,
            "explicit_metric_match": int(direct_metric), "requested_service_match": int(service_match), "lexical": lexical}


def _keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _keys(item)


class MemoryService:
    def __init__(self, store, source_loader=None, auto_publish_observations=True):
        if source_loader is not None and not callable(source_loader):
            raise ValueError("source_loader must be a checked operator callback")
        self.store, self.source_loader = store, source_loader
        self.auto_publish_observations = bool(auto_publish_observations)
        self._lock, self._local, self._closed = threading.RLock(), threading.local(), False
        self._process_lock = None
        try:
            if store.db_path != ":memory:":
                self._process_lock = ProcessLock(str(store.db_path) + ".knowledge.lock")
            with self._db(write=True) as db:
                for sql in DDL:
                    db.execute(sql)
                identity = digest(list(DDL))
                row = db.execute("SELECT identity FROM knowledge_meta WHERE name='v1'").fetchone()
                if row and row[0] != identity:
                    raise RuntimeError("Knowledge schema changed; reconcile state before startup")
                db.execute("INSERT OR IGNORE INTO knowledge_meta VALUES ('v1',?)", (identity,))
        except Exception:
            if self._process_lock:
                self._process_lock.close()
            raise

    @contextmanager
    def _db(self, write=False):
        if getattr(self._local, "db", None) is not None:
            yield self._local.db
        else:
            with self.store._db(write=write) as db:
                yield db

    def _row(self, db, actor, memory_id):
        row = db.execute("SELECT * FROM knowledge_entries WHERE memory_id=? AND actor_scope=?", (memory_id, actor)).fetchone()
        if row is None:
            raise APIError(404, "NOT_FOUND", "This memory is not available in your scope.")
        return row

    def get_entry(self, actor, memory_id):
        with self._db() as db:
            return json.loads(self._row(db, actor, memory_id)["entry_json"])

    def list_entries(self, actor):
        with self._db() as db:
            rows = db.execute("SELECT entry_json FROM knowledge_entries WHERE actor_scope=? ORDER BY updated_at DESC,memory_id DESC LIMIT 101", (actor,)).fetchall()
            counts = {state: 0 for state in STATES}
            counts.update({r[0]: r[1] for r in db.execute("SELECT state,COUNT(*) FROM knowledge_entries WHERE actor_scope=? GROUP BY state", (actor,))})
        items = []
        for row in rows[:100]:
            entry = json.loads(row[0]); entry["fact_count"] = len(entry.pop("facts"))
            items.append(entry)
        return {"enabled": True, "items": items, "truncated": len(rows) > 100, "counts": counts}

    def action(self, actor, route, key, body, operation):
        """Atomically bind an HTTP action, its state change and its replay result."""
        if (not isinstance(key, str) or not 1 <= len(key) <= 128
                or any(ord(c) < 33 or ord(c) > 126 for c in key)):
            raise APIError(400, "INVALID_REQUEST", "A printable bounded Idempotency-Key is required.")
        request_digest = digest(body)
        with self._lock, self._db(write=True) as db:
            previous = db.execute("SELECT request_digest,response_json FROM knowledge_actions WHERE actor_scope=? AND route=? AND key=?", (actor, route, key)).fetchone()
            if previous:
                if previous[0] != request_digest:
                    raise APIError(409, "IDEMPOTENCY_CONFLICT", "This key was already used with another knowledge request.")
                return json.loads(previous[1])
            self._local.db = db
            try:
                result = operation()
                db.execute("INSERT INTO knowledge_actions VALUES (?,?,?,?,?)", (actor, route, key, request_digest, canonical(result)))
                return result
            finally:
                self._local.db = None

    def _normalize(self, payload):
        value = json.loads(canonical(payload))
        source, scope, originals = value["source"], value["scope"], value["facts"]
        if not isinstance(source, dict) or not isinstance(scope, dict) or not isinstance(originals, list) or len(originals) > 200:
            raise ValueError("invalid bounded reconstructed source")
        if source.get("kind") not in {"recorded_run", "public_packet"}:
            raise ValueError("unapproved source kind")
        if not isinstance(source.get("incident_id"), str) or not IDENT.fullmatch(source["incident_id"]):
            raise ValueError("neutral source incident required")
        if not isinstance(scope.get("system_id"), str) or not isinstance(scope.get("service_ids"), list):
            raise ValueError("system and service scope required")
        if any(not isinstance(x, str) or not IDENT.fullmatch(x) for x in scope["service_ids"]):
            raise ValueError("invalid service scope")
        facts = []
        for original in originals:
            fact = {key: deepcopy(original[key]) for key in ("modality", "service_id", "evidence_id", "record", "unit", "source_artifact_digest", "record_locator")}
            if (fact["modality"] not in {"metrics", "logs", "traces"} or fact["service_id"] not in scope["service_ids"]
                    or not isinstance(fact["record"], dict) or not isinstance(fact["unit"], str)
                    or not isinstance(fact["evidence_id"], str) or not IDENT.fullmatch(fact["evidence_id"])
                    or not isinstance(fact["source_artifact_digest"], str) or not SHA.fullmatch(fact["source_artifact_digest"])
                    or not isinstance(fact["record_locator"], str) or not 1 <= len(fact["record_locator"]) <= 1024):
                raise ValueError("invalid reconstructed fact")
            fact["fact_id"] = stable_fact_id(fact)
            facts.append(fact)
        facts = sorted({f["fact_id"]: f for f in facts}.values(), key=lambda f: f["fact_id"])
        if len(canonical(facts).encode()) > 1_000_000:
            raise ValueError("reconstructed facts exceed the memory bound")
        refs = [{"evidence_id": f["evidence_id"], "source_artifact_digest": f["source_artifact_digest"],
                 "record_locator": f["record_locator"], "content_digest": digest(f["record"])} for f in facts]
        normalized_source = {key: source.get(key) for key in ("kind", "investigation_id", "run_id", "incident_id", "data_revision", "group_id")}
        declared_digests = source.get("source_artifact_digests", [])
        if not isinstance(declared_digests, list) or any(not isinstance(x, str) or not SHA.fullmatch(x) for x in declared_digests):
            raise ValueError("invalid declared source digests")
        normalized_source.update(digest=digest(facts), evidence_refs=refs,
                                 source_artifact_digests=sorted(set(declared_digests) | {f["source_artifact_digest"] for f in facts}))
        normalized_scope = {"system_id": scope["system_id"], "service_ids": sorted(set(scope["service_ids"])),
                            "data_revision": scope.get("data_revision", source.get("data_revision"))}
        return {"source": normalized_source, "scope": normalized_scope, "facts": facts}

    def _recorded_payload(self, actor, run_id):
        run = self.store.get_run(actor, run_id)
        if run["state"] not in TERMINAL:
            raise APIError(409, "SOURCE_NOT_TERMINAL", "Only a terminal recorded run can be captured.")
        if run["mode"] == "fixture":
            raise APIError(409, "SYNTHETIC_SOURCE", "Synthetic fixtures are excluded from incident memory.")
        incident = self.store.get_incident(run["incident_id"])
        with self.store._db() as db:
            rows = db.execute("SELECT evidence_id FROM evidence WHERE run_id=? AND incident_id=? ORDER BY evidence_id LIMIT 201", (run_id, run["incident_id"])).fetchall()
        if len(rows) > 200:
            raise ValueError("recorded source exceeds the first-release fact bound")
        facts = []
        for row in rows:
            evidence = self.store.get_evidence(actor, run_id, row[0])
            if evidence["kind"] != "raw":
                # Derived application facts require a separate qualified
                # reconstruction policy; do not silently certify them here.
                raise ValueError("derived recorded evidence has no admission policy")
            content = digest(evidence["record"])
            path = self.store.assets_dir / (content + ".json")
            if (content != evidence["content_digest"] or not path.is_file()
                    or hashlib.sha256(path.read_bytes()).hexdigest() != content
                    or json.loads(path.read_bytes()) != evidence["record"]):
                raise ValueError("preserved recorded source content failed its hash check")
            facts.append({key: evidence[key] for key in ("modality", "service_id", "evidence_id", "record", "unit", "source_artifact_digest", "record_locator")})
        return {"source": {"kind": "recorded_run", "investigation_id": run["investigation_id"], "run_id": run_id,
                            "incident_id": run["incident_id"], "data_revision": incident["data_revision"], "group_id": None},
                "scope": {"system_id": incident["system_id"], "service_ids": incident["service_ids"], "data_revision": incident["data_revision"]},
                "facts": facts}

    def _load(self, actor, origin):
        if origin["kind"] == "recorded_run":
            return self._normalize(self._recorded_payload(actor, origin["run_id"]))
        if self.source_loader is None:
            raise APIError(409, "EXTERNAL_INGESTION_DISABLED", "A checked public source loader is not configured.")
        return self._normalize(self.source_loader(deepcopy(origin["episode"])))

    @staticmethod
    def _policy(payload, proposal=False):
        facts = payload["facts"]
        checks = [{"id": "source_content", "status": "pass", "detail": "Exact facts reconstructed from owned preserved records or the checked public-source callback."},
                  {"id": "observation_type", "status": "fail" if proposal else "pass", "detail": "Only deterministic recorded observations may be published; model, root-cause, recovery and procedure claims are not established."},
                  {"id": "coverage", "status": "pass" if facts else "fail", "detail": "Only the listed recorded facts are covered; completion does not prove incident resolution."}]
        prohibited = bool(set(_keys(facts)) & FORBIDDEN_FIELDS) or bool(INJECTION.search(canonical(facts)))
        checks.append({"id": "untrusted_instructions", "status": "fail" if prohibited else "pass", "detail": "Instruction-like content or evaluator/credential fields are excluded from reusable memory."})
        return checks

    def _fresh_checks(self, actor, row, entry):
        try:
            loaded = self._load(actor, json.loads(row["origin_json"]))
            same = (loaded["source"]["digest"] == entry["source"]["digest"] and loaded["scope"] == entry["scope"]
                    and loaded["source"] == entry["source"] and loaded["facts"] == entry["facts"]
                    and digest(entry["facts"]) == entry["source"]["digest"] and entry["causal_status"] == "not_assessed")
            checks = self._policy(loaded, entry["producer"] != "deterministic" or entry["claim_kind"] != "recorded_observation")
            if not same:
                checks[0].update(status="fail", detail="Source facts, provenance or scope changed since capture.")
            return checks
        except Exception:
            return [{"id": "source_content", "status": "fail", "detail": "The source could not be reconstructed with its recorded scope and content hashes."}]

    @staticmethod
    def _pass(checks):
        return bool(checks) and all(c["status"] == "pass" for c in checks)

    def _save(self, db, actor, entry, origin, action, actor_type, basis, fingerprint=None):
        if fingerprint is not None:
            db.execute("INSERT INTO knowledge_entries VALUES (?,?,?,?,?,?,?,?)", (entry["memory_id"], actor, fingerprint,
                       entry["revision"], entry["state"], now(), canonical(entry), canonical(origin)))
        else:
            db.execute("UPDATE knowledge_entries SET revision=?,state=?,updated_at=?,entry_json=? WHERE memory_id=? AND actor_scope=?", (entry["revision"], entry["state"], now(), canonical(entry), entry["memory_id"], actor))
        db.execute("INSERT INTO knowledge_versions VALUES (?,?,?)", (entry["memory_id"], entry["revision"], canonical(entry)))
        db.execute("INSERT INTO knowledge_events(memory_id,revision,action,actor_type,at,basis_json) VALUES (?,?,?,?,?,?)", (entry["memory_id"], entry["revision"], action, actor_type, now(), canonical(basis)))

    def _capture(self, actor, origin, automatic, proposal=False):
        try:
            payload = self._load(actor, origin)
            checks = self._policy(payload, proposal)
        except APIError:
            raise
        except Exception:
            raise APIError(409, "SOURCE_VERIFICATION_FAILED", "Source reconstruction failed. No observation was admitted.") from None
        fingerprint_scope = {key: payload["scope"].get(key) for key in ("system_id", "data_revision")}
        fingerprint = digest({"scope": fingerprint_scope, "contents": sorted(content_fingerprint(f) for f in payload["facts"]), "proposal": proposal,
                              **({"empty_source": origin} if not payload["facts"] else {})})
        with self._lock, self._db(write=True) as db:
            previous = db.execute("SELECT entry_json FROM knowledge_entries WHERE actor_scope=? AND fingerprint=?", (actor, fingerprint)).fetchone()
            if previous:
                return json.loads(previous[0])
            source, scope, facts = payload["source"], payload["scope"], payload["facts"]
            entry = {"memory_id": "mem_" + uuid.uuid4().hex, "revision": 1,
                     "state": "CANDIDATE" if self._pass(checks) else "QUARANTINED",
                     "title": "Recorded observations · " + source["incident_id"],
                     "summary": f"{len(facts)} source-bound historical observations for {', '.join(scope['service_ids'][:6])}. Units and relationships retain their recorded meaning; no root cause or recovery is certified.",
                     "producer": "ai_proposed" if proposal else "deterministic",
                     "claim_kind": "proposed_claim" if proposal else "recorded_observation", "causal_status": "not_assessed",
                     "source": source, "scope": scope, "facts": facts, "checks": checks,
                     "capture": {"automatic": bool(automatic), "at": now()}, "review": None, "publication": None}
            self._save(db, actor, entry, origin, "capture", "policy", checks, fingerprint)
        if self.auto_publish_observations and entry["state"] == "CANDIDATE":
            entry = self.review(actor, entry["memory_id"], entry["revision"], "Deterministic observation admission policy; no semantic or causal certification.", reviewer_type="policy")
            if entry["state"] == "REVIEWED":
                entry = self.publish(actor, entry["memory_id"], entry["revision"], "Automatically published exact source observations under policy.")
        return entry

    def capture_run(self, actor, run_id, automatic=True):
        # Ownership, terminal state and fixture exclusion are checked before
        # creating an entry, including on explicit recapture.
        with self._lock:
            self._recorded_payload(actor, run_id)
            return self._capture(actor, {"kind": "recorded_run", "run_id": run_id}, automatic)

    def import_episode(self, actor, episode):
        if not isinstance(episode, dict):
            raise APIError(400, "INVALID_REQUEST", "A checked source episode object is required.")
        proposal = (episode.get("producer", "deterministic") != "deterministic"
                    or episode.get("claim_kind", "recorded_observation") != "recorded_observation"
                    or episode.get("causal_status", "not_assessed") != "not_assessed"
                    or any(key in episode for key in ("summary", "root_cause", "recovery", "procedure", "model_answer")))
        with self._lock:
            return self._capture(actor, {"kind": "public_packet", "episode": json.loads(canonical(episode))}, False, proposal)

    def _revision(self, db, actor, memory_id, expected_revision):
        row = self._row(db, actor, memory_id)
        if type(expected_revision) is not int or expected_revision != row["revision"]:
            raise APIError(409, "REVISION_CONFLICT", "Refresh the memory before applying another decision.")
        return row, json.loads(row["entry_json"])

    @staticmethod
    def _note(value):
        if not isinstance(value, str) or not value.strip() or len(value) > 2000:
            raise APIError(400, "INVALID_REQUEST", "A visible note or reason of at most 2000 characters is required.")

    def review(self, actor, memory_id, expected_revision, note, reviewer_type="human"):
        self._note(note)
        if reviewer_type not in {"human", "policy"}:
            raise APIError(400, "INVALID_REQUEST", "Unsupported reviewer type.")
        with self._lock, self._db(write=True) as db:
            row, entry = self._revision(db, actor, memory_id, expected_revision)
            if entry["state"] in {"PUBLISHED", "REVOKED"}:
                raise APIError(409, "STATE_CONFLICT", "Published content is immutable; revoke rather than rewriting its review.")
            checks = self._fresh_checks(actor, row, entry)
            entry.update(revision=entry["revision"] + 1, state="REVIEWED" if self._pass(checks) else "QUARANTINED", checks=checks,
                         review={"actor_type": reviewer_type, "note": note, "at": now(), "basis": checks})
            self._save(db, actor, entry, None, "review", reviewer_type, checks)
            return entry

    def publish(self, actor, memory_id, expected_revision, reason):
        self._note(reason)
        with self._lock, self._db(write=True) as db:
            row, entry = self._revision(db, actor, memory_id, expected_revision)
            if entry["state"] != "REVIEWED":
                raise APIError(409, "STATE_CONFLICT", "Only a successfully reviewed observation may be published.")
            checks = self._fresh_checks(actor, row, entry)
            entry.update(revision=entry["revision"] + 1, checks=checks)
            if self._pass(checks):
                entry.update(state="PUBLISHED", publication={"published_at": now(), "revoked_at": None, "reason": reason})
            else:
                entry.update(state="QUARANTINED")
            actor_type = entry["review"]["actor_type"]
            self._save(db, actor, entry, None, "publish" if entry["state"] == "PUBLISHED" else "publication_blocked", actor_type, checks)
            return entry

    def revoke(self, actor, memory_id, expected_revision, reason, reviewer_type="human"):
        self._note(reason)
        if reviewer_type not in {"human", "policy"}:
            raise APIError(400, "INVALID_REQUEST", "Unsupported revocation actor type.")
        with self._lock, self._db(write=True) as db:
            row, entry = self._revision(db, actor, memory_id, expected_revision)
            if entry["state"] != "PUBLISHED":
                raise APIError(409, "STATE_CONFLICT", "Only a published memory can be revoked.")
            entry.update(revision=entry["revision"] + 1, state="REVOKED")
            entry["publication"].update(revoked_at=now(), reason=reason)
            self._save(db, actor, entry, None, "revoke", reviewer_type, {"reason": reason})
            return entry

    def retrieve(self, actor, query, scope, exclude_incident_id=None, exclude_run_id=None,
                 exclude_group_id=None, limit=3, max_bytes=12000):
        if (not isinstance(query, str) or len(query) > 4000 or not isinstance(scope, dict)
                or not isinstance(scope.get("system_id"), str) or type(limit) is not int or not 1 <= limit <= 3
                or type(max_bytes) is not int or not 1 <= max_bytes <= 12000):
            raise APIError(400, "INVALID_REQUEST", "A bounded query and system scope are required.")
        exclude_incident_id = exclude_incident_id or scope.get("incident_id")
        exclude_run_id = exclude_run_id or scope.get("run_id") or scope.get("context_run_id")
        exclude_group_id = exclude_group_id or scope.get("group_id")
        query_artifacts = set(scope.get("source_artifact_digests", [])) | set(scope.get("exclude_artifact_digests", [])) | set(scope.get("current_source_artifact_digests", []))
        query_contents = set(scope.get("content_fingerprints", []))
        query_services = set(scope.get("service_ids", []))
        tokens = set(re.findall(r"[\w.-]+", query.lower()))
        policy = scope.get("retrieval_policy", "intent_structured_lexical_v2")
        if policy not in {"structured_plus_lexical_v1", "intent_structured_lexical_v2"}:
            raise APIError(400, "INVALID_REQUEST", "The retrieval policy is not a declared qualified variant.")
        intent_text, aliases = expanded_query(query)
        intent_tokens = sparse_terms(intent_text)
        excluded, ranked = Counter(), []
        with self._lock, self._db() as db:
            rows = db.execute("SELECT * FROM knowledge_entries WHERE actor_scope=? AND state='PUBLISHED' ORDER BY memory_id", (actor,)).fetchall()
            corpus = digest([(r["memory_id"], r["revision"], digest(json.loads(r["entry_json"]))) for r in rows])
            for row in rows:
                entry = json.loads(row["entry_json"]); source = entry["source"]
                reason = None
                if entry["scope"]["system_id"] != scope["system_id"]:
                    reason = "different_system"
                elif exclude_incident_id and source["incident_id"] == exclude_incident_id:
                    reason = "same_incident"
                elif exclude_run_id and source["run_id"] == exclude_run_id:
                    reason = "same_run"
                elif exclude_group_id and source["group_id"] == exclude_group_id:
                    reason = "same_group"
                elif query_artifacts & set(source["source_artifact_digests"]):
                    reason = "duplicate_source_artifact"
                elif entry["facts"] and {content_fingerprint(f) for f in entry["facts"]} <= query_contents:
                    reason = "duplicate_source_content"
                if reason:
                    excluded[reason] += 1
                    continue
                if policy == "structured_plus_lexical_v1":
                    fact_terms = set(re.findall(r"[\w.-]+", canonical(entry["facts"]).lower()))
                    lexical = len(tokens & fact_terms)
                    services = len(query_services & set(entry["scope"]["service_ids"]))
                    score = lexical + 2 * services
                    matched_facts = sorted(entry["facts"], key=lambda f: (
                        -(len(tokens & set(re.findall(r"[\w.-]+", canonical(f).lower()))) + 2 * int(f["service_id"] in query_services)), f["fact_id"]))[:3]
                    match_reason = {"lexical_token_overlap": lexical, "shared_service_count": services}
                else:
                    requested_services = {service.lower() for service in entry["scope"]["service_ids"] if service.lower() in intent_tokens}
                    scored_facts = [(intent_score(f, intent_text, intent_tokens, requested_services), f) for f in entry["facts"]]
                    scored_facts.sort(key=lambda pair: (-pair[0]["score"], pair[1]["fact_id"]))
                    strongest = scored_facts[0][0]
                    score = strongest["score"]
                    matched_facts = [value_fact(f) for _, f in scored_facts[:3]]
                    match_reason = {"explicit_metric_match": strongest["explicit_metric_match"],
                                    "requested_service_match": strongest["requested_service_match"],
                                    "filtered_lexical_overlap": strongest["lexical"], "query_aliases": aliases,
                                    "record_keys_omitted": ["query", "formula"],
                                    "review_projection": "actor_type,at,check_ids_and_statuses; full notes retained in entry",
                                    "reference_projection": "evidence_id,source_artifact_digest,record_locator"}
                if score <= 0:
                    excluded["no_match"] += 1
                    continue
                item = {key: deepcopy(entry[key]) for key in ("memory_id", "revision", "title", "summary", "source", "scope", "review")}
                selected_ids = {f["evidence_id"] for f in matched_facts}
                item["source"]["evidence_refs"] = [ref for ref in item["source"]["evidence_refs"] if ref["evidence_id"] in selected_ids]
                if policy == "intent_structured_lexical_v2":
                    item["source"]["evidence_refs"] = [{key: ref[key] for key in ("evidence_id", "source_artifact_digest", "record_locator")} for ref in item["source"]["evidence_refs"]]
                    item["review"] = {"actor_type": entry["review"]["actor_type"], "at": entry["review"]["at"],
                                      "basis": [{"id": c["id"], "status": c["status"]} for c in entry["review"]["basis"]]}
                item.update(facts=matched_facts, score=score,
                            match_reason={**match_reason, "facts_selected": len(matched_facts), "facts_total": len(entry["facts"]),
                                          "projection": "top_three_query_matched_facts_with_projected_references",
                                          "tie_break": "system,source_incident,source_digest,memory_id" if policy == "intent_structured_lexical_v2" else "memory_id",
                                          "policy": policy})
                ranked.append((item, row, entry))
        ranked.sort(key=lambda bundle: (-bundle[0]["score"],
                    bundle[0]["scope"]["system_id"] if policy == "intent_structured_lexical_v2" else "",
                    bundle[0]["source"]["incident_id"] if policy == "intent_structured_lexical_v2" else "",
                    bundle[0]["source"]["digest"] if policy == "intent_structured_lexical_v2" else "",
                    bundle[0]["memory_id"]))
        selected = []
        for item, row, original_entry in ranked:
            if len(selected) >= limit:
                excluded["top_k_omitted"] += 1
            elif len(canonical(selected + [item]).encode()) > max_bytes:
                excluded["byte_cap_omitted"] += 1
            elif not self._pass(self._fresh_checks(actor, row, original_entry)):
                excluded["source_check_failed"] += 1
            else:
                selected.append(item)
        limitations = ["Historical source observations are not current-run evidence, root-cause certification or proof that a proposed check was executed.",
                       "Sparse structured/lexical matching is a bounded baseline; relevance and diagnostic benefit require separate evaluation.",
                       "Each retrieved memory projects at most three query-matched facts and their references; the published full entry is unchanged."]
        if not exclude_group_id:
            limitations.append("The query repetition group is unknown; group-level exclusion cannot be certified.")
        if not exclude_incident_id:
            limitations.append("The query incident identity was not supplied; self-case exclusion cannot be certified.")
        result = {"snapshot_id": "ksnap_" + uuid.uuid4().hex, "items": selected, "excluded": dict(excluded), "limitations": limitations,
                  "corpus_digest": corpus, "policy": policy}
        # Publication/revocation linearizes with snapshot admission. Source
        # checks can be slow, so recheck current state/revision/content under
        # the write lock immediately before admitting this new snapshot.
        with self._lock, self._db(write=True) as db:
            admitted = []
            originals = {entry["memory_id"]: entry for _, _, entry in ranked}
            for item in selected:
                current = self._row(db, actor, item["memory_id"])
                if (current["state"] != "PUBLISHED" or current["revision"] != item["revision"]
                        or digest(json.loads(current["entry_json"])) != digest(originals[item["memory_id"]])):
                    excluded["changed_before_snapshot"] += 1
                else:
                    admitted.append(item)
            result["items"], result["excluded"] = admitted, dict(excluded)
            current_rows = db.execute("SELECT memory_id,revision,entry_json FROM knowledge_entries WHERE actor_scope=? AND state='PUBLISHED' ORDER BY memory_id", (actor,)).fetchall()
            result["corpus_digest"] = digest([(r["memory_id"], r["revision"], digest(json.loads(r["entry_json"]))) for r in current_rows])
            db.execute("INSERT INTO knowledge_snapshots VALUES (?,?,?,?)", (result["snapshot_id"], actor, now(), canonical(result)))
        return result

    def capture_eligible_runs(self):
        with self._db() as db:
            rows = db.execute("""SELECT r.run_id,i.actor_scope FROM runs r
                JOIN investigations i ON i.investigation_id=r.investigation_id
                JOIN run_configs c ON c.run_id=r.run_id
                LEFT JOIN knowledge_captures k ON k.actor_scope=i.actor_scope AND k.run_id=r.run_id
                WHERE r.state IN ('COMPLETED','FAILED','TIMED_OUT','BUDGET_EXHAUSTED','CANCELLED')
                  AND c.mode!='fixture' AND k.run_id IS NULL
                ORDER BY r.created_at,r.run_id LIMIT 50""").fetchall()
        receipt = {"scanned": len(rows), "captured": 0, "failed": 0, "errors": [], "model_calls": 0}
        for row in rows:
            memory_id, status, detail = None, "FAILED", "Source capture failed its bounded checks."
            try:
                entry = self.capture_run(row["actor_scope"], row["run_id"], automatic=True)
                memory_id, status, detail = entry["memory_id"], entry["state"], "Source observations captured; no incident resolution is inferred."
                receipt["captured"] += 1
            except Exception:
                receipt["failed"] += 1
                receipt["errors"].append({"run_id": row["run_id"], "code": "SOURCE_CAPTURE_FAILED"})
            with self._db(write=True) as db:
                db.execute("INSERT OR IGNORE INTO knowledge_captures VALUES (?,?,?,?,?,?)", (row["actor_scope"], row["run_id"], memory_id, status, detail, now()))
        return receipt

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._process_lock:
                self._process_lock.close()


class MemoryWorker:
    def __init__(self, service, interval=3):
        if not isinstance(interval, (int, float)) or not 0 < interval <= 60:
            raise ValueError("A bounded positive memory scan interval is required")
        self.service, self.interval = service, interval
        self.last_receipt = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="autotriager-memory", daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            try:
                self.last_receipt = self.service.capture_eligible_runs()
            except Exception:
                self.last_receipt = {"failed": 1, "code": "MEMORY_SCAN_FAILED", "model_calls": 0}
            self._stop.wait(self.interval)

    def close(self):
        self._stop.set()
        self._thread.join()
