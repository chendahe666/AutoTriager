"""Finite, durable W07 Gemini admission and native/OpenAI protocol translation.

Only NativeGeminiTransport holds the real key. No retries, arbitrary endpoints,
model aliases, cloud SDK discovery, paid fallback, or automatic tool execution.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import sqlite3
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .safe_config import TOOLS

MODEL = "gemini-3.1-flash-lite"
ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/" + MODEL
PAID_MODEL = "gemini-3.5-flash-lite"
PAID_GRANT_REFERENCE = "human:2026-11-03:small-scale-end-to-end-after-usd2-pilot-estimate"
PAID_PROFILE_ID = "paid-gemini-3.5-flash-lite-pilot-v1"
PAID_CASE_DIGEST = "c5e764e916014a623f652f23713420b978e3d428d690f892a832971128296f78"
PAID_RATE_CARD = {"currency": "USD", "input_nano_usd_per_token": 300, "output_nano_usd_per_token": 2500,
                  "source": "https://aistudio.google.com/docs/pricing#gemini-3.5-flash-lite", "mode": "Standard"}
PAID_RATE_CARD_SHA256 = hashlib.sha256(json.dumps(PAID_RATE_CARD, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
PAID_SLOT_NANO_USD = 8000 * 300 + 1024 * 2500
MAX_BODY = 256_000
SERVICE503_OWNER_REFERENCE = "human:call_111de2b3c9e1486896635dc5ba1fb133:one503recovery-same-ceiling"
SERVICE503_REASON = "One owner-approved recovery after known HTTP503"
TEMPLATE_REPAIR_REASON = "Final routine repair of literal schema version and empty metric filter"
RAW_METRICS_PROJECTION_LABEL = "w07_final_raw_metrics_only_v1"
RAW_METRICS_SCHEMA = {"type": "object", "properties": {"limit": {"type": "integer", "enum": [2]}, "max_bytes": {"type": "integer", "enum": [4096]}}, "required": ["limit", "max_bytes"], "additionalProperties": False}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


class GateDenied(ValueError):
    pass


class ProviderHTTPError(RuntimeError):
    def __init__(self, status: int):
        super().__init__(f"Native provider HTTP {status}; no retry")
        self.status = status


class RejectRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise GateDenied("provider redirects forbidden")


class NativeGeminiTransport:
    """Fixed HTTPS methods, header-only key, no ambient proxies or retries."""
    def __init__(self, api_key: str, timeout=60, *, model=MODEL):
        if not isinstance(api_key, str) or not api_key:
            raise GateDenied("provider key missing")
        if model not in (MODEL, PAID_MODEL):
            raise GateDenied("provider model outside the two explicit profiles")
        self.__api_key = api_key
        self.model = model
        self.timeout = timeout
        self.opener = build_opener(ProxyHandler({}), RejectRedirect())

    def _post(self, method: str, body: dict):
        if method not in ("countTokens", "generateContent"):
            raise GateDenied("provider method forbidden")
        native = body.get("generateContentRequest", {}) if method == "countTokens" else body
        if native.get("model") != "models/" + self.model:
            raise GateDenied("native request model differs from fixed transport profile")
        endpoint = "https://generativelanguage.googleapis.com/v1beta/models/" + self.model
        request = Request(endpoint + ":" + method, data=canonical(body), method="POST", headers={"Content-Type": "application/json", "x-goog-api-key": self.__api_key})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                encoded = response.read(2_000_001)
                if len(encoded) > 2_000_000:
                    raise GateDenied("provider response exceeds bound")
                return json.loads(encoded)
        except HTTPError as exc:
            # Do not serialize request headers, provider error body or key.
            raise ProviderHTTPError(exc.code) from None
        except (URLError, OSError) as exc:
            raise RuntimeError("provider transport outcome uncertain") from None

    def count(self, native: dict):
        return self._post("countTokens", {"generateContentRequest": native})

    def generate(self, native: dict):
        return self._post("generateContent", native)


@dataclass(frozen=True)
class Grant:
    case_digest: str
    incident_id: str
    grant_id: str = "human:call_90d00ad9e8d643bf8342f66b4190c74d:model-replacement-same-ceiling"
    model: str = MODEL
    max_requests: int = 10
    max_input_per_request: int = 8000
    max_output_per_request: int = 1024
    max_total_input: int = 80000
    max_total_output: int = 10240
    paid_fallback: bool = False

    def validate(self):
        if (self.model != MODEL or self.max_requests != 10 or self.max_input_per_request != 8000
                or self.max_output_per_request != 1024 or self.max_total_input != 80000
                or self.max_total_output != 10240 or self.paid_fallback is not False
                or len(self.case_digest) != 64 or not self.incident_id):
            raise GateDenied("grant differs from finite user authorization")


@dataclass(frozen=True)
class PaidGrant:
    """The separate, fixed paid pilot; never a reopening of the Free grant."""
    case_digest: str
    incident_id: str
    grant_id: str = PAID_GRANT_REFERENCE
    model: str = PAID_MODEL
    max_requests: int = 10
    max_input_per_request: int = 8000
    max_output_per_request: int = 1024
    max_total_input: int = 80000
    max_total_output: int = 10240
    paid_fallback: bool = False
    profile_id: str = PAID_PROFILE_ID
    rate_card_sha256: str = PAID_RATE_CARD_SHA256
    max_cost_nano_usd: int = 2_000_000_000
    input_nano_usd_per_token: int = 300
    output_nano_usd_per_token: int = 2500
    max_metadata_requests: int = 20

    def validate(self):
        if self.paid_fallback is not False or asdict(self) != asdict(PaidGrant(PAID_CASE_DIGEST, "incident-0001")):
            raise GateDenied("paid grant differs from the explicit finite pilot")
        if any(type(getattr(self, field)) is not int for field in (
                "max_requests", "max_input_per_request", "max_output_per_request", "max_total_input",
                "max_total_output", "max_cost_nano_usd", "input_nano_usd_per_token",
                "output_nano_usd_per_token", "max_metadata_requests")):
            raise GateDenied("paid grant accounting requires exact integers")


class ProviderLedger:
    """Own SQLite ledger: a restart never resets a grant or ambiguous work."""
    def __init__(self, path: Path, *, grant: Grant):
        grant.validate()
        if isinstance(grant, PaidGrant) and not isinstance(self, PaidProviderLedger):
            raise GateDenied("paid pilot requires its monetary ledger")
        self.path, self.grant = Path(path), grant
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS provider_grant(
                  singleton INTEGER PRIMARY KEY CHECK(singleton=1),config_json TEXT NOT NULL,
                  state TEXT NOT NULL,calls_reserved INTEGER NOT NULL DEFAULT 0,
                  input_reserved INTEGER NOT NULL DEFAULT 0,output_reserved INTEGER NOT NULL DEFAULT 0,
                  reason TEXT, CHECK(calls_reserved BETWEEN 0 AND 10),
                  CHECK(input_reserved BETWEEN 0 AND 80000),CHECK(output_reserved BETWEEN 0 AND 10240));
                CREATE TABLE IF NOT EXISTS provider_attempt(
                  attempt_id TEXT PRIMARY KEY,state TEXT NOT NULL,created_at REAL NOT NULL,
                  request_digest TEXT NOT NULL,preflight_tokens INTEGER,actual_input INTEGER,
                  actual_output INTEGER,model_version TEXT,response_digest TEXT,error_code TEXT);
                CREATE TABLE IF NOT EXISTS provider_call_parts(
                  call_id TEXT PRIMARY KEY,parts_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS provider_review_resume(
                  resume_number INTEGER PRIMARY KEY CHECK(resume_number=1),
                  review_reference TEXT NOT NULL,previous_snapshot_digest TEXT NOT NULL,
                  parent_run_id TEXT NOT NULL,reason TEXT NOT NULL,created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS provider_service503_recovery(
                  recovery_number INTEGER PRIMARY KEY CHECK(recovery_number=1),
                  review_reference TEXT NOT NULL,owner_reference TEXT NOT NULL,
                  previous_snapshot_digest TEXT NOT NULL,parent_run_id TEXT NOT NULL,
                  reason TEXT NOT NULL,created_at REAL NOT NULL);
                CREATE TRIGGER IF NOT EXISTS provider503_recovery_no_update BEFORE UPDATE ON provider_service503_recovery
                  BEGIN SELECT RAISE(ABORT,'provider503_recovery_immutable'); END;
                CREATE TRIGGER IF NOT EXISTS provider503_recovery_no_delete BEFORE DELETE ON provider_service503_recovery
                  BEGIN SELECT RAISE(ABORT,'provider503_recovery_immutable'); END;
                CREATE TABLE IF NOT EXISTS provider_template_repair(
                  repair_number INTEGER PRIMARY KEY CHECK(repair_number=1),
                  review_reference TEXT NOT NULL,previous_snapshot_digest TEXT NOT NULL,
                  parent_run_id TEXT NOT NULL,reason TEXT NOT NULL,created_at REAL NOT NULL);
                CREATE TRIGGER IF NOT EXISTS provider_template_repair_no_update BEFORE UPDATE ON provider_template_repair
                  BEGIN SELECT RAISE(ABORT,'provider_template_repair_immutable'); END;
                CREATE TRIGGER IF NOT EXISTS provider_template_repair_no_delete BEFORE DELETE ON provider_template_repair
                  BEGIN SELECT RAISE(ABORT,'provider_template_repair_immutable'); END;
            """)
        with self.transaction() as db:
            row = db.execute("SELECT config_json FROM provider_grant WHERE singleton=1").fetchone()
            config = canonical(asdict(grant)).decode()
            if row is None:
                db.execute("INSERT INTO provider_grant(singleton,config_json,state) VALUES(1,?,'ACTIVE')", (config,))
            elif row[0] != config:
                raise GateDenied("existing grant identity cannot be reset or changed")
            # COUNTING cannot have dispatched generation. Intent may have, so
            # hold its full credit and block the grant after process restart.
            if db.execute("SELECT 1 FROM provider_attempt WHERE state='DISPATCH_INTENT'").fetchone():
                db.execute("UPDATE provider_grant SET state='UNRESOLVED',reason='restart with unresolved generation' WHERE singleton=1")
            db.execute("UPDATE provider_attempt SET state='ABORTED_PREFLIGHT',error_code='restart_before_generation' WHERE state='COUNTING'")

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self):
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def _active(self, db):
        row = db.execute("SELECT * FROM provider_grant WHERE singleton=1").fetchone()
        if row["state"] != "ACTIVE":
            raise GateDenied("provider grant fenced: " + row["state"])
        return row

    def begin_count(self, request_digest: str):
        with self.transaction() as db:
            row = self._active(db)
            if row["calls_reserved"] >= self.grant.max_requests:
                raise GateDenied("generation request grant exhausted")
            if db.execute("SELECT 1 FROM provider_attempt WHERE state IN ('COUNTING','DISPATCH_INTENT')").fetchone():
                raise GateDenied("one provider slot already pending")
            attempt = "pva_" + secrets.token_hex(16)
            db.execute("INSERT INTO provider_attempt(attempt_id,state,created_at,request_digest) VALUES(?,'COUNTING',?,?)", (attempt, time.time(), request_digest))
            return attempt

    def reserve_generation(self, attempt: str, tokens: int):
        if isinstance(tokens, bool) or not isinstance(tokens, int) or not 0 <= tokens <= 8000:
            self.stop(attempt, "INPUT_LIMIT", "REJECTED_PREFLIGHT")
            raise GateDenied("native exact preflight input exceeds grant")
        with self.transaction() as db:
            row = self._active(db)
            pending = db.execute("SELECT state FROM provider_attempt WHERE attempt_id=?", (attempt,)).fetchone()
            if not pending or pending[0] != "COUNTING":
                raise GateDenied("preflight reservation missing")
            if row["calls_reserved"] + 1 > 10 or row["input_reserved"] + 8000 > 80000 or row["output_reserved"] + 1024 > 10240:
                raise GateDenied("aggregate generation grant exhausted")
            db.execute("UPDATE provider_grant SET calls_reserved=calls_reserved+1,input_reserved=input_reserved+8000,output_reserved=output_reserved+1024 WHERE singleton=1")
            db.execute("UPDATE provider_attempt SET state='DISPATCH_INTENT',preflight_tokens=? WHERE attempt_id=?", (tokens, attempt))

    def stop(self, attempt: str, reason: str, state: str):
        with self.transaction() as db:
            db.execute("UPDATE provider_attempt SET state=?,error_code=? WHERE attempt_id=? AND state IN ('COUNTING','DISPATCH_INTENT')", (state, reason, attempt))
            db.execute("UPDATE provider_attempt SET error_code=COALESCE(error_code,?) WHERE attempt_id=?", (reason, attempt))
            grant_state = "RATE_LIMITED" if reason == "HTTP_429" else "UNRESOLVED" if state == "UNRESOLVED" else "STOPPED"
            db.execute("UPDATE provider_grant SET state=?,reason=? WHERE singleton=1 AND state='ACTIVE'", (grant_state, reason))

    def cancel(self, reason="local cancellation"):
        with self.transaction() as db:
            db.execute("UPDATE provider_grant SET state='CANCELLED',reason=? WHERE singleton=1", (reason,))

    def finish(self, attempt, response, input_tokens, output_tokens):
        if response.get("modelVersion") != self.grant.model:
            self.stop(attempt, "RESOLVED_MODEL_MISMATCH", "INVALID_MODEL")
            raise GateDenied("actual provider model differs from authorized exact model")
        if (isinstance(input_tokens, bool) or not isinstance(input_tokens, int) or not 0 <= input_tokens <= 8000
                or isinstance(output_tokens, bool) or not isinstance(output_tokens, int) or not 0 <= output_tokens <= 1024):
            self.stop(attempt, "USAGE_LIMIT_OR_INVALID", "INVALID_USAGE")
            raise GateDenied("provider usage missing/invalid or exceeds grant; future calls stopped")
        with self.transaction() as db:
            active = db.execute("SELECT state FROM provider_grant WHERE singleton=1").fetchone()[0] == "ACTIVE"
            db.execute("UPDATE provider_attempt SET state=?,actual_input=?,actual_output=?,model_version=?,response_digest=? WHERE attempt_id=? AND state='DISPATCH_INTENT'", ("SUCCEEDED" if active else "LATE_RESPONSE", input_tokens, output_tokens, str(response.get("modelVersion", "unknown")), digest(response), attempt))
            return active

    def parts(self, call_id):
        with self._db() as db:
            row = db.execute("SELECT parts_json FROM provider_call_parts WHERE call_id=?", (call_id,)).fetchone()
            if not row:
                raise GateDenied("unknown provider tool-call history")
            return json.loads(row[0])

    def store_parts(self, call_id, parts):
        with self.transaction() as db:
            db.execute("INSERT INTO provider_call_parts VALUES(?,?)", (call_id, canonical(parts).decode()))

    def snapshot(self):
        with self._db() as db:
            return {"grant": dict(db.execute("SELECT * FROM provider_grant WHERE singleton=1").fetchone()), "attempts": [dict(row) for row in db.execute("SELECT * FROM provider_attempt ORDER BY created_at,attempt_id")], "reviewed_resumes": [dict(row) for row in db.execute("SELECT * FROM provider_review_resume ORDER BY resume_number")], "reviewed_503_recoveries": [dict(row) for row in db.execute("SELECT * FROM provider_service503_recovery ORDER BY recovery_number")], "reviewed_template_repairs": [dict(row) for row in db.execute("SELECT * FROM provider_template_repair ORDER BY repair_number")]}

    def continue_known_qualification(self, *, expected_snapshot_digest, review_reference, parent_run_id, reason):
        """One trusted reviewed R1 continuation; never an HTTP/model operation.

        A cancelled/uncertain provider operation, 429, changed source snapshot,
        user cancellation, or prior continuation cannot be reopened here.
        Counters and every existing attempt remain unchanged.
        """
        if (not isinstance(review_reference, str) or not review_reference.startswith("root-review-sha256:")
                or len(review_reference) != len("root-review-sha256:") + 64
                or any(char not in "0123456789abcdef" for char in review_reference.removeprefix("root-review-sha256:"))
                or not isinstance(parent_run_id, str) or not parent_run_id.startswith("run_")
                or reason != "R1 bounded repair of rejected model query arguments"):
            raise GateDenied("explicit reviewed continuation identity required")
        with self.transaction() as db:
            grant = dict(db.execute("SELECT * FROM provider_grant WHERE singleton=1").fetchone())
            attempts = [dict(row) for row in db.execute("SELECT * FROM provider_attempt ORDER BY created_at,attempt_id")]
            resumes = [dict(row) for row in db.execute("SELECT * FROM provider_review_resume ORDER BY resume_number")]
            recoveries = [dict(row) for row in db.execute("SELECT * FROM provider_service503_recovery ORDER BY recovery_number")]
            repairs = [dict(row) for row in db.execute("SELECT * FROM provider_template_repair ORDER BY repair_number")]
            snapshot = {"grant": grant, "attempts": attempts, "reviewed_resumes": resumes, "reviewed_503_recoveries": recoveries, "reviewed_template_repairs": repairs}
            if digest(snapshot) != expected_snapshot_digest or resumes or recoveries or repairs:
                raise GateDenied("continuation snapshot changed or R1 already consumed")
            generated = [row for row in attempts if row["preflight_tokens"] is not None]
            if (grant["state"] != "CANCELLED" or grant["reason"] != "qualification stopped; no further provider admission"
                    or not 1 <= len(generated) < 10 or len(generated) != grant["calls_reserved"]
                    or grant["input_reserved"] != 8000 * len(generated) or grant["output_reserved"] != 1024 * len(generated)
                    or len(generated) != len(attempts)
                    or any(row["state"] != "SUCCEEDED" or row["model_version"] != MODEL
                        or not isinstance(row["actual_input"], int) or not 0 < row["actual_input"] <= 8000
                        or row["preflight_tokens"] != row["actual_input"]
                        or not isinstance(row["actual_output"], int) or not 0 <= row["actual_output"] <= 1024
                        or not row["response_digest"] or row["error_code"] is not None for row in generated)):
                raise GateDenied("prior attempt is not a fully observed known qualification failure")
            db.execute("INSERT INTO provider_review_resume VALUES(1,?,?,?,?,?)", (review_reference, expected_snapshot_digest, parent_run_id, reason, time.time()))
            db.execute("UPDATE provider_grant SET state='ACTIVE',reason='one reviewed R1 continuation' WHERE singleton=1")

    def recover_known_service503(self, *, expected_snapshot_digest, review_reference, parent_run_id, owner_reference, reason):
        """One separately owner-approved exception for the observed HTTP503.

        The prior unknown token usage remains fully reserved; no call/token
        credit or technical expiry is reset. No generic retry is exposed.
        """
        prefix = "root-review-sha256:"
        if (owner_reference != SERVICE503_OWNER_REFERENCE or reason != SERVICE503_REASON
                or not isinstance(review_reference, str) or not review_reference.startswith(prefix)
                or len(review_reference) != len(prefix) + 64
                or any(char not in "0123456789abcdef" for char in review_reference[len(prefix):])
                or not isinstance(parent_run_id, str) or not parent_run_id.startswith("run_")):
            raise GateDenied("explicit owner and root review required for the one503 recovery")
        with self.transaction() as db:
            grant = dict(db.execute("SELECT * FROM provider_grant WHERE singleton=1").fetchone())
            attempts = [dict(row) for row in db.execute("SELECT * FROM provider_attempt ORDER BY created_at,attempt_id")]
            resumes = [dict(row) for row in db.execute("SELECT * FROM provider_review_resume ORDER BY resume_number")]
            recoveries = [dict(row) for row in db.execute("SELECT * FROM provider_service503_recovery ORDER BY recovery_number")]
            repairs = [dict(row) for row in db.execute("SELECT * FROM provider_template_repair ORDER BY repair_number")]
            snapshot = {"grant": grant, "attempts": attempts, "reviewed_resumes": resumes, "reviewed_503_recoveries": recoveries, "reviewed_template_repairs": repairs}
            if digest(snapshot) != expected_snapshot_digest or recoveries or repairs:
                raise GateDenied("503 snapshot changed or recovery already consumed")
            if (grant["state"] != "CANCELLED" or grant["reason"] != "qualification stopped; no further provider admission"
                    or grant["calls_reserved"] != 3 or grant["input_reserved"] != 24000
                    or grant["output_reserved"] != 3072 or len(resumes) != 1 or len(attempts) != 3):
                raise GateDenied("503 recovery must preserve the exact prior three-call allocation")
            if review_reference == resumes[0]["review_reference"]:
                raise GateDenied("503 recovery requires a fresh root review, not the prior R1 approval")
            prior, failed = attempts[:2], attempts[2]
            if any(row["state"] != "SUCCEEDED" or row["model_version"] != MODEL or row["error_code"] is not None
                    or not isinstance(row["actual_input"], int) or isinstance(row["actual_input"], bool) or not 0 < row["actual_input"] <= 8000
                    or row["preflight_tokens"] != row["actual_input"]
                    or not isinstance(row["actual_output"], int) or isinstance(row["actual_output"], bool) or not 0 <= row["actual_output"] <= 1024
                    or not row["response_digest"] for row in prior):
                raise GateDenied("the two earlier provider successes must remain fully observed")
            if (failed["state"] != "FAILED_KNOWN" or failed["error_code"] != "HTTP_503"
                    or not isinstance(failed["preflight_tokens"], int) or isinstance(failed["preflight_tokens"], bool)
                    or not 0 < failed["preflight_tokens"] <= 8000
                    or any(failed[key] is not None for key in ("actual_input", "actual_output", "model_version", "response_digest"))):
                raise GateDenied("only the known503 response with retained unknown usage is eligible")
            db.execute("INSERT INTO provider_service503_recovery VALUES(1,?,?,?,?,?,?)", (review_reference, owner_reference, expected_snapshot_digest, parent_run_id, reason, time.time()))
            db.execute("UPDATE provider_grant SET state='ACTIVE',reason='one owner-approved503 recovery' WHERE singleton=1")

    def repair_known_template_failure(self, *, expected_snapshot_digest, review_reference, parent_run_id, reason):
        """One final reviewed software-template repair within the original ten."""
        prefix = "root-review-sha256:"
        if (reason != TEMPLATE_REPAIR_REASON or not isinstance(review_reference, str) or not review_reference.startswith(prefix)
                or len(review_reference) != len(prefix) + 64 or any(c not in "0123456789abcdef" for c in review_reference[len(prefix):])
                or not isinstance(parent_run_id, str) or not parent_run_id.startswith("run_")):
            raise GateDenied("fresh root review is required for the final template repair")
        with self.transaction() as db:
            grant = dict(db.execute("SELECT * FROM provider_grant WHERE singleton=1").fetchone())
            attempts = [dict(row) for row in db.execute("SELECT * FROM provider_attempt ORDER BY created_at,attempt_id")]
            resumes = [dict(row) for row in db.execute("SELECT * FROM provider_review_resume ORDER BY resume_number")]
            recoveries = [dict(row) for row in db.execute("SELECT * FROM provider_service503_recovery ORDER BY recovery_number")]
            repairs = [dict(row) for row in db.execute("SELECT * FROM provider_template_repair ORDER BY repair_number")]
            snapshot = {"grant": grant, "attempts": attempts, "reviewed_resumes": resumes, "reviewed_503_recoveries": recoveries, "reviewed_template_repairs": repairs}
            if digest(snapshot) != expected_snapshot_digest or repairs:
                raise GateDenied("template snapshot changed or final repair already consumed")
            if (grant["state"] != "CANCELLED" or grant["reason"] != "qualification stopped; no further provider admission"
                    or grant["calls_reserved"] != 6 or grant["input_reserved"] != 48000 or grant["output_reserved"] != 6144
                    or len(attempts) != 6 or len(resumes) != 1 or len(recoveries) != 1):
                raise GateDenied("final template repair requires the exact prior six-slot allocation")
            if review_reference in {resumes[0]["review_reference"], recoveries[0]["review_reference"]}:
                raise GateDenied("template repair requires a new review distinct from prior approvals")
            failed = attempts[2]
            if (failed["state"] != "FAILED_KNOWN" or failed["error_code"] != "HTTP_503"
                    or not isinstance(failed["preflight_tokens"], int) or isinstance(failed["preflight_tokens"], bool)
                    or not 0 < failed["preflight_tokens"] <= 8000
                    or any(failed[k] is not None for k in ("actual_input", "actual_output", "model_version", "response_digest"))):
                raise GateDenied("the original unknown503 credit must remain unchanged")
            known = attempts[:2] + attempts[3:]
            if any(row["state"] != "SUCCEEDED" or row["model_version"] != MODEL or row["error_code"] is not None
                    or not isinstance(row["actual_input"], int) or isinstance(row["actual_input"], bool) or not 0 < row["actual_input"] <= 8000
                    or row["preflight_tokens"] != row["actual_input"] or not isinstance(row["actual_output"], int)
                    or isinstance(row["actual_output"], bool) or not 0 <= row["actual_output"] <= 1024 or not row["response_digest"] for row in known):
                raise GateDenied("only the observed template failure with five known successes is eligible")
            db.execute("INSERT INTO provider_template_repair VALUES(1,?,?,?,?,?)", (review_reference, expected_snapshot_digest, parent_run_id, reason, time.time()))
            db.execute("UPDATE provider_grant SET state='ACTIVE',reason='one final reviewed template repair' WHERE singleton=1")


class PaidProviderLedger(ProviderLedger):
    """Separate paid identity and integer nominal-fee reservations.

    Published Standard token rates bound only this pilot's nominal API fee.
    Observed token fees are not an invoice, promotional offset or tax receipt.
    """
    def __init__(self, path: Path, *, grant: PaidGrant):
        if type(grant) is not PaidGrant:
            raise GateDenied("explicit paid grant required")
        grant.validate()
        path = Path(path)
        # Identity must be read without writes *before* base schema/recovery.
        # immutable mode cannot create WAL/SHM files alongside a legacy DB.
        if path.exists():
            try:
                with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
                    config = db.execute("SELECT config_json FROM provider_grant WHERE singleton=1").fetchone()
                    budget = db.execute("SELECT profile_id,rate_card_sha256 FROM paid_provider_budget WHERE singleton=1").fetchone()
                    if (config is None or config[0] != canonical(asdict(grant)).decode()
                            or budget != (PAID_PROFILE_ID, PAID_RATE_CARD_SHA256)):
                        raise GateDenied("existing ledger is not this independent paid pilot")
            except sqlite3.DatabaseError:
                raise GateDenied("existing ledger is not this independent paid pilot") from None
        super().__init__(path, grant=grant)
        with self.transaction() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS paid_provider_budget(
                  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                  profile_id TEXT NOT NULL CHECK(profile_id='paid-gemini-3.5-flash-lite-pilot-v1'),
                  rate_card_sha256 TEXT NOT NULL,currency TEXT NOT NULL CHECK(currency='USD'),
                  input_nano_usd_per_token INTEGER NOT NULL CHECK(input_nano_usd_per_token=300),
                  output_nano_usd_per_token INTEGER NOT NULL CHECK(output_nano_usd_per_token=2500),
                  max_cost_nano_usd INTEGER NOT NULL CHECK(max_cost_nano_usd=2000000000),
                  max_metadata_requests INTEGER NOT NULL CHECK(max_metadata_requests=20),
                  metadata_requests INTEGER NOT NULL DEFAULT 0 CHECK(metadata_requests BETWEEN 0 AND 20),
                  money_reserved_nano_usd INTEGER NOT NULL DEFAULT 0 CHECK(money_reserved_nano_usd BETWEEN 0 AND 2000000000),
                  known_fee_nano_usd INTEGER NOT NULL DEFAULT 0 CHECK(known_fee_nano_usd BETWEEN 0 AND money_reserved_nano_usd),
                  created_at REAL NOT NULL,expires_at REAL NOT NULL CHECK(expires_at>created_at AND expires_at<=created_at+3600));
                CREATE TABLE IF NOT EXISTS paid_provider_attempt_charge(
                  attempt_id TEXT PRIMARY KEY REFERENCES provider_attempt(attempt_id),
                  money_reserved_nano_usd INTEGER NOT NULL CHECK(money_reserved_nano_usd=4960000),
                  known_fee_nano_usd INTEGER CHECK(known_fee_nano_usd BETWEEN 0 AND money_reserved_nano_usd));
                CREATE TRIGGER IF NOT EXISTS paid_profile_no_rebind BEFORE UPDATE OF
                  profile_id,rate_card_sha256,currency,input_nano_usd_per_token,output_nano_usd_per_token,
                  max_cost_nano_usd,max_metadata_requests,created_at,expires_at ON paid_provider_budget
                  BEGIN SELECT RAISE(ABORT,'paid_profile_immutable'); END;
                CREATE TRIGGER IF NOT EXISTS paid_profile_no_delete BEFORE DELETE ON paid_provider_budget
                  BEGIN SELECT RAISE(ABORT,'paid_profile_immutable'); END;
            """)
            if not db.execute("SELECT 1 FROM paid_provider_budget WHERE singleton=1").fetchone():
                created = time.time()
                db.execute("INSERT INTO paid_provider_budget(singleton,profile_id,rate_card_sha256,currency,input_nano_usd_per_token,output_nano_usd_per_token,max_cost_nano_usd,max_metadata_requests,created_at,expires_at) VALUES(1,?,?,'USD',300,2500,2000000000,20,?,?)", (PAID_PROFILE_ID, PAID_RATE_CARD_SHA256, created, created + 3600))

    def _active(self, db):
        row = super()._active(db)
        budget = db.execute("SELECT expires_at FROM paid_provider_budget WHERE singleton=1").fetchone()
        if not budget or time.time() >= budget[0]:
            raise GateDenied("paid pilot technical deadline expired")
        return row

    def begin_count(self, request_digest: str):
        with self.transaction() as db:
            row = self._active(db)
            budget = db.execute("SELECT * FROM paid_provider_budget WHERE singleton=1").fetchone()
            if row["calls_reserved"] >= self.grant.max_requests:
                raise GateDenied("generation request grant exhausted")
            if budget["metadata_requests"] >= self.grant.max_metadata_requests:
                raise GateDenied("paid metadata request grant exhausted")
            if db.execute("SELECT 1 FROM provider_attempt WHERE state IN ('COUNTING','DISPATCH_INTENT')").fetchone():
                raise GateDenied("one provider slot already pending")
            attempt = "pva_" + secrets.token_hex(16)
            db.execute("INSERT INTO provider_attempt(attempt_id,state,created_at,request_digest) VALUES(?,'COUNTING',?,?)", (attempt, time.time(), request_digest))
            db.execute("UPDATE paid_provider_budget SET metadata_requests=metadata_requests+1 WHERE singleton=1")
            return attempt

    def reserve_generation(self, attempt: str, tokens: int):
        if type(tokens) is not int or not 0 <= tokens <= self.grant.max_input_per_request:
            self.stop(attempt, "INPUT_LIMIT", "REJECTED_PREFLIGHT")
            raise GateDenied("native exact preflight input exceeds grant")
        with self.transaction() as db:
            row = self._active(db)
            pending = db.execute("SELECT state FROM provider_attempt WHERE attempt_id=?", (attempt,)).fetchone()
            budget = db.execute("SELECT * FROM paid_provider_budget WHERE singleton=1").fetchone()
            if not pending or pending[0] != "COUNTING":
                raise GateDenied("preflight reservation missing")
            if (row["calls_reserved"] + 1 > self.grant.max_requests
                    or row["input_reserved"] + 8000 > self.grant.max_total_input
                    or row["output_reserved"] + 1024 > self.grant.max_total_output
                    or budget["money_reserved_nano_usd"] + PAID_SLOT_NANO_USD > self.grant.max_cost_nano_usd):
                raise GateDenied("aggregate paid token or monetary grant exhausted")
            db.execute("UPDATE provider_grant SET calls_reserved=calls_reserved+1,input_reserved=input_reserved+8000,output_reserved=output_reserved+1024 WHERE singleton=1")
            db.execute("UPDATE paid_provider_budget SET money_reserved_nano_usd=money_reserved_nano_usd+? WHERE singleton=1", (PAID_SLOT_NANO_USD,))
            db.execute("INSERT INTO paid_provider_attempt_charge(attempt_id,money_reserved_nano_usd) VALUES(?,?)", (attempt, PAID_SLOT_NANO_USD))
            db.execute("UPDATE provider_attempt SET state='DISPATCH_INTENT',preflight_tokens=? WHERE attempt_id=?", (tokens, attempt))

    def finish_metadata(self, attempt: str, tokens: int):
        """Record a root-run compatibility preflight without generation credit.

        It must use begin_count before the sole countTokens request. The state
        distinguishes metadata-only usage from a reserved generation intent.
        """
        if type(tokens) is not int or not 0 <= tokens <= 8000:
            self.stop(attempt, "INPUT_LIMIT", "REJECTED_PREFLIGHT")
            raise GateDenied("native exact preflight input exceeds grant")
        with self.transaction() as db:
            self._active(db)
            updated = db.execute("UPDATE provider_attempt SET state='METADATA_ONLY',preflight_tokens=? WHERE attempt_id=? AND state='COUNTING'", (tokens, attempt)).rowcount
            if updated != 1:
                raise GateDenied("metadata-only preflight missing or already finalized")

    def finish(self, attempt, response, input_tokens, output_tokens):
        if response.get("modelVersion") != self.grant.model:
            self.stop(attempt, "RESOLVED_MODEL_MISMATCH", "INVALID_MODEL")
            raise GateDenied("actual provider model differs from authorized exact model")
        if (type(input_tokens) is not int or not 0 <= input_tokens <= 8000
                or type(output_tokens) is not int or not 0 <= output_tokens <= 1024):
            self.stop(attempt, "USAGE_LIMIT_OR_INVALID", "INVALID_USAGE")
            raise GateDenied("provider usage missing/invalid or exceeds grant; future calls stopped")
        fee = input_tokens * 300 + output_tokens * 2500
        with self.transaction() as db:
            state = db.execute("SELECT state FROM provider_grant WHERE singleton=1").fetchone()[0]
            expires = db.execute("SELECT expires_at FROM paid_provider_budget WHERE singleton=1").fetchone()[0]
            active = state == "ACTIVE" and time.time() < expires
            if not active and state == "ACTIVE":
                db.execute("UPDATE provider_grant SET state='CANCELLED',reason='paid pilot expired while provider was in flight' WHERE singleton=1")
            updated = db.execute("UPDATE provider_attempt SET state=?,actual_input=?,actual_output=?,model_version=?,response_digest=? WHERE attempt_id=? AND state='DISPATCH_INTENT'", ("SUCCEEDED" if active else "LATE_RESPONSE", input_tokens, output_tokens, self.grant.model, digest(response), attempt)).rowcount
            if updated != 1:
                raise GateDenied("paid generation intent missing or already finalized")
            charged = db.execute("UPDATE paid_provider_attempt_charge SET known_fee_nano_usd=? WHERE attempt_id=? AND known_fee_nano_usd IS NULL", (fee, attempt)).rowcount
            if charged != 1:
                raise GateDenied("paid monetary reservation missing or already finalized")
            db.execute("UPDATE paid_provider_budget SET known_fee_nano_usd=known_fee_nano_usd+? WHERE singleton=1", (fee,))
            return active

    def snapshot(self):
        with self._db() as db:
            db.execute("BEGIN")
            snapshot = {"grant": dict(db.execute("SELECT * FROM provider_grant WHERE singleton=1").fetchone()),
                        "attempts": [dict(row) for row in db.execute("SELECT * FROM provider_attempt ORDER BY created_at,attempt_id")],
                        "reviewed_resumes": [dict(row) for row in db.execute("SELECT * FROM provider_review_resume ORDER BY resume_number")],
                        "reviewed_503_recoveries": [dict(row) for row in db.execute("SELECT * FROM provider_service503_recovery ORDER BY recovery_number")],
                        "reviewed_template_repairs": [dict(row) for row in db.execute("SELECT * FROM provider_template_repair ORDER BY repair_number")]}
            budget = dict(db.execute("SELECT * FROM paid_provider_budget WHERE singleton=1").fetchone())
            budget.pop("singleton")
            snapshot["paid_budget"] = budget
            charges = {row["attempt_id"]: dict(row) for row in db.execute("SELECT * FROM paid_provider_attempt_charge")}
        for row in snapshot["attempts"]:
            charge = charges.get(row["attempt_id"], {})
            row["money_reserved_nano_usd"] = charge.get("money_reserved_nano_usd", 0)
            row["known_fee_nano_usd"] = charge.get("known_fee_nano_usd")
        return snapshot

    def continue_known_qualification(self, **_):
        raise GateDenied("paid pilot has no Free-grant continuation")

    def recover_known_service503(self, **_):
        raise GateDenied("paid pilot has no automatic or reviewed503 recovery")

    def repair_known_template_failure(self, **_):
        raise GateDenied("paid pilot has no legacy template recovery")


def to_native(request: dict, ledger: ProviderLedger, *, raw_metrics_projection=False, output_ceiling=1024):
    if not isinstance(request, dict) or len(canonical(request)) > MAX_BODY:
        raise GateDenied("bounded JSON request required")
    allowed = {"model", "messages", "tools", "tool_choice", "max_tokens", "max_completion_tokens", "temperature", "stream", "top_p", "n"}
    if set(request) - allowed or request.get("model") != ledger.grant.model or request.get("stream", False) is not False or request.get("n", 1) != 1:
        raise GateDenied("unsupported model or provider request fields")
    output = request.get("max_completion_tokens", request.get("max_tokens", 1024))
    if (type(output_ceiling) is not int or output_ceiling not in (1024, 4096)
            or isinstance(output, bool) or not isinstance(output, int) or not 1 <= output <= output_ceiling):
        raise GateDenied("response output limit outside grant")
    messages = request.get("messages")
    if not isinstance(messages, list) or not messages or len(messages) > 100:
        raise GateDenied("bounded messages required")
    contents, systems = [], []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in ("system", "user", "assistant", "tool"):
            raise GateDenied("unsupported message role")
        role, content, parts = message["role"], message.get("content"), []
        if role == "system":
            if contents or not isinstance(content, str):
                raise GateDenied("system messages must precede text-only conversation")
            systems.append({"text": content})
            continue
        if role == "tool":
            call_id = message.get("tool_call_id")
            original = ledger.parts(call_id)
            name = original["functionCall"]["name"]
            if not isinstance(content, str):
                raise GateDenied("tool result must be preserved string")
            response = {"name": name, "response": {"result": content}}
            if "id" in original["functionCall"]:
                response["id"] = original["functionCall"]["id"]
            parts.append({"functionResponse": response})
            native_role = "user"
        else:
            if content is not None:
                if not isinstance(content, str):
                    raise GateDenied("only plain text messages admitted")
                if content:
                    parts.append({"text": content})
            for call in message.get("tool_calls", []):
                original = ledger.parts(call.get("id"))
                function = call.get("function", {})
                if function.get("name") != original["functionCall"]["name"] or json.loads(function.get("arguments", "{}")) != original["functionCall"].get("args", {}):
                    raise GateDenied("tool call history differs from actual provider response")
                parts.append(original)
            native_role = "model" if role == "assistant" else "user"
        if not parts:
            raise GateDenied("empty content message")
        if contents and contents[-1]["role"] == native_role:
            contents[-1]["parts"].extend(parts)
        else:
            contents.append({"role": native_role, "parts": parts})
    declarations = []
    for tool in request.get("tools", []):
        if tool.get("type") != "function" or tool.get("function", {}).get("name") not in TOOLS:
            raise GateDenied("tool outside read-only gateway surface")
        function = tool["function"]
        declaration = {"name": function["name"], "description": function.get("description", "Read-only incident evidence")}
        parameters = function.get("parameters", {"type": "object", "properties": {}})
        # Native FunctionDeclaration parameters are optional for zero-argument
        # functions; omit them instead of sending an empty OBJECT schema.
        if parameters.get("properties") or parameters.get("required"):
            declaration["parametersJsonSchema"] = parameters
        if raw_metrics_projection and function["name"] == "incident_metrics":
            declaration["description"] = "Read exactly two raw metric samples from the server-bound historical incident. Supply only limit=2 and max_bytes=4096; no filters, time, service or aggregation arguments."
            declaration["parametersJsonSchema"] = json.loads(canonical(RAW_METRICS_SCHEMA))
        declarations.append(declaration)
    if len(declarations) > 5 or len({item["name"] for item in declarations}) != len(declarations):
        raise GateDenied("duplicate or excessive function declarations")
    native = {"model": "models/" + ledger.grant.model, "contents": contents, "generationConfig": {"candidateCount": 1, "maxOutputTokens": output, "thinkingConfig": {"thinkingLevel": "minimal"}}}
    if systems:
        native["systemInstruction"] = {"parts": systems}
    if declarations:
        native["tools"] = [{"functionDeclarations": declarations}]
        choice = request.get("tool_choice", "auto")
        if choice not in ("auto", "none", "required"):
            raise GateDenied("unsupported tool choice")
        native["toolConfig"] = {"functionCallingConfig": {"mode": {"auto": "AUTO", "none": "NONE", "required": "ANY"}[choice]}}
    if not contents:
        raise GateDenied("conversation contents missing")
    return native


def to_openai(native: dict, ledger: ProviderLedger, attempt: str, *, raw_metrics_projection=False):
    candidates = native.get("candidates", [])
    if len(candidates) != 1:
        raise GateDenied("provider returned no single candidate")
    candidate, text, calls = candidates[0], [], []
    for part in candidate.get("content", {}).get("parts", []):
        if part.get("thought"):
            continue  # Preserve raw response; never render hidden reasoning.
        if "text" in part:
            text.append(part["text"])
        if "functionCall" in part:
            function = part["functionCall"]
            if (function.get("name") not in TOOLS or not isinstance(function.get("args", {}), dict)
                    or not isinstance(function.get("id"), str) or not 1 <= len(function["id"]) <= 256):
                raise GateDenied("provider requested invalid or unrelated tool")
            if raw_metrics_projection and function["name"] == "incident_metrics":
                arguments = function.get("args", {})
                if arguments != {"limit": 2, "max_bytes": 4096} or any(type(value) is not int for value in arguments.values()):
                    raise GateDenied("final raw-metrics projection rejected generated arguments; nothing was rewritten")
            call_id = "call_" + secrets.token_hex(16)
            ledger.store_parts(call_id, part)  # Retain any opaque thought signature.
            calls.append({"id": call_id, "type": "function", "function": {"name": function["name"], "arguments": canonical(function.get("args", {})).decode()}})
    finish = candidate.get("finishReason")
    if finish not in ("STOP", "MAX_TOKENS") or (not text and not calls):
        raise GateDenied("provider candidate blocked or incomplete")
    usage = native["usageMetadata"]
    output = usage.get("candidatesTokenCount", 0) + usage.get("thoughtsTokenCount", 0)
    message = {"role": "assistant", "content": "".join(text) if text else None}
    if calls:
        message["tool_calls"] = calls
    return {"id": "chatcmpl-" + attempt, "object": "chat.completion", "created": int(time.time()), "model": ledger.grant.model, "choices": [{"index": 0, "message": message, "finish_reason": "length" if finish == "MAX_TOKENS" else "tool_calls" if calls else "stop"}], "usage": {"prompt_tokens": usage["promptTokenCount"], "completion_tokens": output, "total_tokens": usage["promptTokenCount"] + output}}


class ProviderGate:
    def __init__(self, ledger: ProviderLedger, *, transport, artifacts: Path, admission=lambda: True, expected_system_policy=None, raw_metrics_projection=False):
        self.ledger, self.transport, self.artifacts = ledger, transport, Path(artifacts)
        self.admission = admission
        self.expected_system_policy = expected_system_policy
        self.raw_metrics_projection = raw_metrics_projection
        self.artifacts.mkdir(parents=True, exist_ok=True)

    def _admit(self):
        if not self.admission():
            self.ledger.cancel("application run/grant is no longer active")
            raise GateDenied("application cancelled or fenced provider run")

    def build_native(self, request: dict):
        """Protected profile hook; the default preserves the historical wire."""
        return to_native(request, self.ledger, raw_metrics_projection=self.raw_metrics_projection)

    def complete(self, request: dict):
        self._admit()
        native = self.build_native(request)
        if self.expected_system_policy is not None and native.get("systemInstruction") != {"parts": [{"text": self.expected_system_policy}]}:
            raise GateDenied("engine system prompt differs from the reviewed server-owned policy")
        request_hash = digest(native)
        attempt = self.ledger.begin_count(request_hash)
        folder = self.artifacts / attempt
        folder.mkdir(exist_ok=False)
        (folder / "native-request.json").write_bytes(canonical(native))
        generated = False
        try:
            counted = self.transport.count(native)
            (folder / "native-count.json").write_bytes(canonical(counted))
            if digest(native) != request_hash:
                raise GateDenied("count transport mutated the generation request")
            self._admit()
            self.ledger.reserve_generation(attempt, counted.get("totalTokens"))
            generated = True
            self._admit()
            response = self.transport.generate(native)
            (folder / "native-response.json").write_bytes(canonical(response))
            usage = response.get("usageMetadata", {})
            prompt = usage.get("promptTokenCount")
            candidates, thoughts = usage.get("candidatesTokenCount"), usage.get("thoughtsTokenCount", 0)
            if isinstance(candidates, bool) or not isinstance(candidates, int) or isinstance(thoughts, bool) or not isinstance(thoughts, int):
                raise GateDenied("invalid output token usage")
            if candidates < 0 or thoughts < 0 or candidates + thoughts > native["generationConfig"]["maxOutputTokens"]:
                raise GateDenied("actual output exceeds requested bound")
            if prompt != counted.get("totalTokens") or usage.get("toolUsePromptTokenCount", 0) != 0 or usage.get("totalTokenCount") != prompt + candidates + thoughts:
                raise GateDenied("native preflight and actual complete usage do not match")
            if not self.admission():
                self.ledger.cancel("application cancelled while provider was in flight")
            if not self.ledger.finish(attempt, response, prompt, candidates + thoughts):
                raise GateDenied("local run cancelled; late provider response retained")
            result = to_openai(response, self.ledger, attempt, raw_metrics_projection=self.raw_metrics_projection)
            (folder / "openai-response.json").write_bytes(canonical(result))
            return result
        except ProviderHTTPError as exc:
            self.ledger.stop(attempt, f"HTTP_{exc.status}", "FAILED_KNOWN")
            raise
        except GateDenied:
            self.ledger.stop(attempt, "BOUND_OR_PROTOCOL_FAILURE", "FAILED_KNOWN" if generated else "REJECTED_PREFLIGHT")
            raise
        except BaseException:
            self.ledger.stop(attempt, "TRANSPORT_OR_PROCESS_UNCERTAIN" if generated else "PREFLIGHT_FAILURE", "UNRESOLVED" if generated else "ABORTED_PREFLIGHT")
            raise RuntimeError("provider attempt stopped; inspect safe ledger, no automatic retry") from None


def make_provider_server(gate: ProviderGate, token: str, *, port=8842):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            if (self.path != "/v1/chat/completions" or self.headers.get("Origin")
                    or self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}"
                    or not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token)):
                return self.reply(403, {"error": {"message": "private provider admission denied"}})
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= MAX_BODY or self.headers.get("Transfer-Encoding"):
                    raise GateDenied("request bound")
                request = json.loads(self.rfile.read(size), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                self.reply(200, gate.complete(request))
            except ProviderHTTPError as exc:
                self.reply(exc.status, {"error": {"message": "native provider HTTP error; grant stopped", "type": "provider_error"}})
            except (ValueError, TypeError):
                self.reply(400, {"error": {"message": "provider request denied or grant fenced", "type": "admission_error"}})
            except BaseException:
                self.reply(502, {"error": {"message": "provider outcome uncertain; grant stopped", "type": "unresolved_error"}})

        def reply(self, status, value):
            body = canonical(value)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server
