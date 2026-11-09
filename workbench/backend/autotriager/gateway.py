"""Run-bound read-only queries over an explicitly admitted NeutralCorpus.

No model, path, label lookup, shell, network fetch or provider is available here.
The SQLite write transaction is the admission, cancellation and budget fence.
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import secrets

from . import fixture
from .errors import APIError
from .store import VERSION, encoded, now

TOOLS = ("incident_manifest", "incident_services", "incident_metrics", "incident_logs", "incident_traces")
ARGUMENTS = frozenset(("time_window", "service_ids", "parameters", "limit", "max_bytes"))
TRUNCATED = "Complete records were omitted under the reply byte/row ceiling. Aggregates with any omitted parent were also omitted."


class GatewayDenied(ValueError):
    """Admission failure with a safe code and no telemetry or secret in text."""

    def __init__(self, code, message="Read-only tool admission denied."):
        super().__init__(message)
        self.code = code


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def utc(value):
    if not isinstance(value, str):
        raise GatewayDenied("scope_denied", "A timestamp must be explicit timezone text.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GatewayDenied("scope_denied", "A timestamp is invalid.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise GatewayDenied("scope_denied", "A timestamp requires an explicit timezone.")
    return parsed.astimezone(timezone.utc)


def utc_text(value):
    return utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def neutral_incidents(corpus, *, system_id="rcaeval_offline", original_timestamp_units=None):
    """Build source-counted API manifests, without original case IDs or labels.

    Original timestamp units are unknown unless the caller supplies audited
    modality metadata. This function never infers units from numeric magnitudes.
    """
    source = corpus.get_manifest()
    incidents = []
    for item in source["incidents"]:
        summaries, warnings = {}, ["Offline normalized observations; no healthy reference or causal validation is implied."]
        for modality in ("metrics", "logs", "traces"):
            bundle = corpus.get_records(item["incident_id"], modality)
            present = bundle["availability"] == "present"
            records = [row["record"] for row in bundle["records"]]
            units = sorted({row["unit"] for row in records}) if modality == "metrics" else ["ms"] if modality == "traces" and records else []
            original_unit = (original_timestamp_units or {}).get(modality, "unknown")
            summaries[modality] = {"availability": "present" if present else "absent",
                "record_count": len(records), "original_timestamp_unit": original_unit,
                "value_units": units, "reason": None if present else "This modality is not available in the admitted neutral corpus."}
            if present and original_unit == "unknown":
                warnings.append(f"Original {modality} timestamp unit is not supplied in this API manifest; normalized timestamps are explicit UTC.")
            if "unknown" in units:
                warnings.append(f"Some {modality} measurement units are undocumented; no conversion or dimensional interpretation is assumed.")
        value = {"schema_version": VERSION, "incident_id": item["incident_id"], "system_id": system_id,
            "display_name": "Offline incident " + item["incident_id"], "mode": "offline_replay",
            "data_revision": source["normalization_version"] + ":" + corpus.manifest_sha256,
            "manifest_digest": corpus.manifest_sha256,
            "time_window": {key: utc_text(item["time_window"][key]) for key in ("start", "end")},
            "normalized_timestamp_unit": "iso8601_utc", "service_ids": sorted(item["services"]),
            "modalities": summaries, "coverage_warnings": warnings}
        incidents.append(value)
    return incidents


def aggregate_values(values, method):
    """Arithmetic mean, numeric max or nearest-rank p95, with explicit counts."""
    if method not in ("mean", "max", "p95"):
        raise GatewayDenied("scope_denied", "Unsupported aggregation.")
    valid = [value for value in values if not isinstance(value, bool)
             and isinstance(value, (int, float)) and math.isfinite(value)]
    result = {"statistic": method, "sample_count": len(values), "valid_count": len(valid),
              "excluded_count": len(values) - len(valid), "statistic_status": "ok" if valid else "no_data"}
    if not valid:
        result["value"] = None
    elif method == "mean":
        # Divide before summing to avoid an overflowing intermediate finite sum.
        result["value"] = math.fsum(value / len(valid) for value in valid)
    elif method == "max":
        result["value"] = max(valid)
    else:
        result["value"] = sorted(valid)[math.ceil(.95 * len(valid)) - 1]
    return result


class Gateway:
    def __init__(self, store, corpus):
        self.store, self.corpus = store, corpus
        self.catalog = {item["incident_id"]: item for item in corpus.get_manifest()["incidents"]}

    def issue_capability(self, run_id, *, ttl_seconds=300, tools=TOOLS):
        """Trusted worker operation; never expose this method as a model tool."""
        if (isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int)
                or not 1 <= ttl_seconds <= 300 or not tools or len(set(tools)) != len(tools)
                or not set(tools) <= set(TOOLS)):
            raise GatewayDenied("scope_denied", "Invalid capability duration or tool allowlist.")
        token = secrets.token_urlsafe(32)
        with self.store._db(write=True) as db:
            run = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if run is None or run["state"] != "RUNNING" or run["incident_id"] not in self.catalog:
                raise GatewayDenied("scope_denied")
            expires = (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat().replace("+00:00", "Z")
            db.execute("INSERT INTO gateway_capabilities VALUES (?,?,?,?,?,?,?,NULL)",
                (fixture.opaque("cap"), hashlib.sha256(token.encode()).hexdigest(), run_id,
                 run["incident_id"], run["run_generation"], encoded(list(tools)), expires))
        return token

    def revoke_capability(self, token):
        with self.store._db(write=True) as db:
            db.execute("UPDATE gateway_capabilities SET revoked_at=? WHERE token_digest=? AND revoked_at IS NULL",
                       (now(), hashlib.sha256(token.encode()).hexdigest()))

    def _scope(self, db, token, tool):
        if not isinstance(token, str) or not token or len(token) > 512:
            raise GatewayDenied("scope_denied")
        cap = db.execute("SELECT * FROM gateway_capabilities WHERE token_digest=?",
                         (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
        if (cap is None or cap["revoked_at"] is not None or utc(cap["expires_at"]) <= datetime.now(timezone.utc)
                or (tool is not None and tool not in json.loads(cap["tool_allowlist_json"]))):
            raise GatewayDenied("scope_denied")
        run = db.execute("SELECT * FROM runs WHERE run_id=? AND incident_id=?", (cap["run_id"], cap["incident_id"])).fetchone()
        if run is None or cap["run_generation"] != run["run_generation"]:
            raise GatewayDenied("scope_denied")
        if run["state"] in ("CANCEL_REQUESTED", "CANCELLED"):
            raise GatewayDenied("cancelled")
        if run["state"] != "RUNNING":
            raise GatewayDenied("budget_exhausted" if run["state"] == "BUDGET_EXHAUSTED" else "scope_denied")
        if run["incident_id"] not in self.catalog:
            raise GatewayDenied("scope_denied")
        budget = db.execute("SELECT * FROM run_budgets WHERE run_id=?", (run["run_id"],)).fetchone()
        if budget is None:
            raise GatewayDenied("scope_denied")
        return run, budget

    def authorize(self, token):
        """Authenticate protocol discovery without reading telemetry or charging."""
        expired = False
        with self.store._db(write=True) as db:
            run, budget = self._scope(db, token, None)
            if utc(budget["deadline_at"]) <= datetime.now(timezone.utc):
                self._fence(db, run["run_id"], "TIMED_OUT", "TOOL_DEADLINE")
                expired = True
            row = db.execute("SELECT tool_allowlist_json FROM gateway_capabilities WHERE token_digest=?",
                             (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
            allowed = json.loads(row[0])
        if expired:
            raise GatewayDenied("cancelled")
        return allowed

    def _query(self, run, incident, tool, arguments):
        if tool not in TOOLS or not isinstance(arguments, dict) or set(arguments) - ARGUMENTS:
            raise GatewayDenied("scope_denied", "Only closed read-only tool arguments are admitted.")
        window = arguments.get("time_window", incident["time_window"])
        if not isinstance(window, dict) or set(window) != {"start", "end"}:
            raise GatewayDenied("scope_denied", "A query needs a closed time window.")
        start, end = utc(window["start"]), utc(window["end"])
        if not utc(incident["time_window"]["start"]) <= start < end <= utc(incident["time_window"]["end"]):
            raise GatewayDenied("scope_denied", "Query window exceeds the bound incident.")
        services = arguments.get("service_ids", incident["service_ids"])
        if (not isinstance(services, list) or any(not isinstance(s, str) for s in services)
                or len(set(services)) != len(services) or not set(services) <= set(incident["service_ids"])):
            raise GatewayDenied("scope_denied", "Service selection exceeds the bound incident.")
        query = {"schema_version": VERSION, "query_id": fixture.opaque("qry"), "run_id": run["run_id"],
            "incident_id": run["incident_id"], "tool_name": tool,
            "modality": tool.removeprefix("incident_") if tool in TOOLS[2:] else None,
            "time_window": {"start": utc_text(window["start"]), "end": utc_text(window["end"])},
            "service_ids": sorted(services), "limit": arguments.get("limit", 2000),
            "max_bytes": arguments.get("max_bytes", 1048576), "parameters": copy.deepcopy(arguments.get("parameters", {}))}
        try:
            self.store._validate("ToolQuery", query)
        except APIError as exc:
            raise GatewayDenied("scope_denied", "Tool arguments do not match the closed query schema.") from exc
        return query

    def execute(self, token, tool_name, arguments=None):
        """Return immutable ToolReply after commit, or deny without observations.

        No network activity is performed while the short SQLite transaction is
        held. The immutable corpus is already loaded and digest-checked.
        """
        denial = None
        with self.store._db(write=True) as db:
            run, budget = self._scope(db, token, tool_name)
            incident = self.store._incident(db, run["incident_id"])
            admitted = self.catalog[run["incident_id"]]
            if (set(incident["service_ids"]) != set(admitted["services"])
                    or any(utc(incident["time_window"][k]) != utc(admitted["time_window"][k]) for k in ("start", "end"))
                    or incident["manifest_digest"] != self.corpus.manifest_sha256):
                raise GatewayDenied("scope_denied", "Stored incident does not match the admitted corpus revision.")
            query = self._query(run, incident, tool_name, {} if arguments is None else arguments)
            remaining_calls = budget["max_tool_calls"] - budget["used_tool_calls"] - budget["reserved_tool_calls"]
            remaining_bytes = budget["max_source_bytes"] - budget["used_source_bytes"] - budget["reserved_source_bytes"]
            remaining_rows = budget["max_total_rows"] - budget["used_rows"] - budget["reserved_rows"]
            if utc(budget["deadline_at"]) <= datetime.now(timezone.utc):
                self._fence(db, run["run_id"], "TIMED_OUT", "TOOL_DEADLINE")
                denial = "cancelled"
            elif remaining_calls < 1:
                self._fence(db, run["run_id"], "BUDGET_EXHAUSTED", "TOOL_CALL_BUDGET")
                denial = "budget_exhausted"
            else:
                reply = self._materialize(query, incident, remaining_bytes, remaining_rows)
                # Filtering, sorting, aggregation and exact serialization can
                # consume the remaining wall time. Recheck before preserving
                # or charging any observations, then commit only the fence.
                if utc(budget["deadline_at"]) <= datetime.now(timezone.utc):
                    self._fence(db, run["run_id"], "TIMED_OUT", "TOOL_DEADLINE")
                    denial = "cancelled"
                elif reply is None:
                    self._fence(db, run["run_id"], "BUDGET_EXHAUSTED", "TOOL_REPLY_BUDGET")
                    denial = "budget_exhausted"
                else:
                    self._persist(db, reply)
        if denial:
            raise GatewayDenied(denial, "Run fenced before tool observations could be delivered.")
        return reply

    def _fence(self, db, run_id, state, code):
        db.execute("UPDATE gateway_capabilities SET revoked_at=? WHERE run_id=? AND revoked_at IS NULL", (now(), run_id))
        self.store._transition(db, run_id, state, "Read-only gateway resource ceiling reached.", code)

    def _raw_evidence(self, query, source, stamp):
        record = copy.deepcopy(source["record"])
        record["timestamp"] = utc_text(record["timestamp"])
        unit = record.get("unit", "ms" if query["modality"] == "traces" else "not_applicable")
        limitations = []
        if unit == "unknown":
            limitations.append("Source measurement unit is undocumented; no conversion is inferred.")
        return {"schema_version": VERSION, "evidence_id": fixture.opaque("ev"),
            "run_id": query["run_id"], "incident_id": query["incident_id"], "query_id": query["query_id"],
            "service_id": record["service"], "modality": query["modality"], "kind": "raw",
            "source_artifact_digest": source["source_hash"], "record_locator": source["locator"],
            "content_digest": digest(record), "observed_window": {"start": record["timestamp"], "end": record["timestamp"]},
            "preserved_at": stamp, "unit": unit, "record": record, "derivation": None, "limitations": limitations}

    def _observations(self, query):
        bundle = self.corpus.get_records(query["incident_id"], query["modality"])
        if bundle["availability"] != "present":
            return [], None, "modality_absent", "absent", ["Telemetry modality is unavailable; this is not a negative finding."]
        start, end = (utc(query["time_window"][key]) for key in ("start", "end"))
        params, services, modality = query["parameters"], set(query["service_ids"]), query["modality"]
        rows, unknown_status, unknown_severity = [], 0, 0
        for source in bundle["records"]:
            row = source["record"]
            if row["service"] not in services or not start <= utc(row["timestamp"]) < end:
                continue
            if modality == "traces" and row["status"] not in ("ok", "error"):
                unknown_status += 1
            if modality == "logs" and row["severity"] not in ("debug", "info", "warning", "error", "critical"):
                unknown_severity += 1
            if modality == "metrics" and "metric_names" in params and row["metric"] not in params["metric_names"]:
                continue
            if modality == "logs" and (params.get("contains", "") not in row["message"]
                    or ("severity" in params and row["severity"] != params["severity"])):
                continue
            if modality == "traces" and (("trace_id" in params and row["trace_id"] != params["trace_id"])
                    or (params.get("status", "any") != "any" and row["status"] != params["status"])):
                continue
            rows.append(source)
        rows.sort(key=lambda s: (utc(s["record"]["timestamp"]), s["record"]["service"],
                                 int(s["locator"].rsplit(":", 1)[-1])))
        stamp = now()
        # No reply can admit more than 2,000 Evidence objects. Count/filter the
        # complete immutable slice, but do not allocate hundreds of thousands
        # of undisplayable evidence dictionaries for a full RCAEval case.
        evidence = [self._raw_evidence(query, row, stamp) for row in rows[:2000]]
        matched = len(rows)
        limitations = ["Counts include raw records and any derived summaries; undocumented units are retained without conversion."]
        if unknown_status:
            limitations.append(f"{unknown_status} scoped source span statuses are outside normalized ok/error enums; an exact-filter no_match does not establish the absence of errors.")
        if unknown_severity:
            limitations.append(f"{unknown_severity} scoped source log severities are undocumented; an exact-filter no_match does not establish the absence of error logs.")
        if modality == "metrics" and params.get("aggregation", "none") != "none":
            groups = {}
            for source in rows:
                row = source["record"]
                groups.setdefault((row["service"], row["metric"]), []).append(source)
            matched += len(groups)
            raw_by_locator = {item["record_locator"]: item for item in evidence}
            for (service, metric), sources in sorted(groups.items()):
                units = {source["record"]["unit"] for source in sources}
                if len(units) != 1:
                    return [], len(rows), "error", "present", ["Mixed units within a service/metric group; no conversion is guessed."]
                if len(rows) >= 2000 or not all(source["locator"] in raw_by_locator for source in sources):
                    continue
                parents = [raw_by_locator[source["locator"]] for source in sources]
                unit = next(iter(units))
                method = params["aggregation"]
                record = aggregate_values([parent["record"]["value"] for parent in parents], method)
                record.update(service=service, metric=metric, unit=unit)
                evidence.append({"schema_version": VERSION, "evidence_id": fixture.opaque("ev"),
                    "run_id": query["run_id"], "incident_id": query["incident_id"], "query_id": query["query_id"],
                    "service_id": service, "modality": "metrics", "kind": "derived",
                    "source_artifact_digest": parents[0]["source_artifact_digest"],
                    "record_locator": "aggregate:" + method + ":" + digest({"service": service, "metric": metric})[:24],
                    "content_digest": digest(record), "observed_window": copy.deepcopy(query["time_window"]),
                    "preserved_at": stamp, "unit": unit, "record": record,
                    "derivation": {"parent_evidence_ids": [parent["evidence_id"] for parent in parents],
                        "method_id": {"mean": "arithmetic_mean", "max": "numeric_max", "p95": "nearest_rank_p95"}[method],
                        "method_version": "neutral-gateway-v1", "missing_value_policy": "exclude_null_nonfinite_report_counts", "unit": unit},
                    "limitations": ["Summary describes the full permitted source slice; it is not a healthy baseline or a causal test."]
                        + (["Source measurement unit is undocumented; no conversion is inferred."] if unit == "unknown" else [])})
        return evidence, matched, "success" if rows else "no_match", "present", limitations

    def _materialize(self, query, incident, available_bytes, available_rows):
        tool = query["tool_name"]
        manifest, services, limitations = None, [], []
        if tool == "incident_manifest":
            evidence, matched, status, availability = [], 1, "success", "present"
            manifest = copy.deepcopy(incident)
        elif tool == "incident_services":
            evidence, matched, status, availability = [], len(query["service_ids"]), "success", "present"
            services = list(query["service_ids"])
            if not services:
                status = "no_match"
        else:
            evidence, matched, status, availability, limitations = self._observations(query)
        # Binary search a complete prefix. Aggregates only appear after all raw
        # parents, and every retained aggregate is checked again below.
        total = (1 if manifest is not None else len(services) if tool == "incident_services"
                 else matched if status == "success" else len(evidence))
        ceiling = min(query["limit"], max(0, available_rows), 2000)
        def build(count):
            chosen = evidence[:count] if tool in TOOLS[2:] else []
            ids = {item["evidence_id"] for item in chosen}
            chosen = [item for item in chosen if item["derivation"] is None
                      or set(item["derivation"]["parent_evidence_ids"]) <= ids]
            count = len(chosen) if tool in TOOLS[2:] else count
            omitted = count < total
            result = {"schema_version": VERSION, "query_id": query["query_id"], "run_id": query["run_id"],
                "incident_id": query["incident_id"], "status": "truncated" if omitted else status,
                "availability": availability, "evidence_ids": [item["evidence_id"] for item in chosen],
                "returned_count": count, "matched_count": matched, "truncated": omitted,
                "result_complete": not omitted and status in ("success", "no_match"),
                "limitations": limitations + ([TRUNCATED] if omitted else []), "error": None}
            return {"schema_version": VERSION, "query": query, "result": result, "evidence": chosen,
                    "manifest": manifest if count == 1 else None,
                    "service_ids": services[:count] if tool == "incident_services" else []}
        # A complete result drops the truncation limitation, so its envelope
        # can be smaller than a truncated prefix. Test this discontinuity
        # before binary searching the monotone family of truncated replies.
        maximum = min(total, ceiling)
        if maximum == total:
            complete = build(total)
            if len(canonical(complete)) <= min(query["max_bytes"], max(0, available_bytes)):
                self.store._validate("ToolReply", complete)
                return complete
        low, high, best = 0, min(maximum, total - 1), None
        while low <= high:
            middle = (low + high) // 2
            reply = build(middle)
            if len(canonical(reply)) <= min(query["max_bytes"], max(0, available_bytes)):
                best, low = reply, middle + 1
            else:
                high = middle - 1
        # Zero observations are truthful for absent/no_match/error. A nonempty
        # match must deliver at least one whole record or fence the run.
        if best is None or (total and best["result"]["returned_count"] == 0):
            return None
        self.store._validate("ToolReply", best)
        return best

    def _persist(self, db, reply):
        query, result, evidence = reply["query"], reply["result"], reply["evidence"]
        size = len(canonical(reply))
        db.execute("INSERT INTO queries VALUES (?,?,?,?,?,?,?,?,?,?)", (query["query_id"], query["run_id"], query["incident_id"],
            query["tool_name"], query["modality"], query["time_window"]["start"], query["time_window"]["end"],
            query["limit"], query["max_bytes"], encoded(query)))
        self.store._event(db, query["run_id"], "tool.started", {"query_id": query["query_id"], "tool_name": query["tool_name"]}, query["query_id"])
        db.execute("INSERT INTO query_results VALUES (?,?,?,?,?,?,?,?)", (result["query_id"], result["status"], result["availability"],
            result["returned_count"], result["matched_count"], int(result["truncated"]), int(result["result_complete"]), encoded(result)))
        for item in evidence:
            self.store._validate("Evidence", item)
            self.store._preserve(item["record"])
            db.execute("INSERT INTO evidence VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (item["evidence_id"], item["run_id"], item["incident_id"],
                item["query_id"], item["service_id"], item["modality"], item["kind"], item["source_artifact_digest"], item["record_locator"],
                item["content_digest"], item["observed_window"]["start"], item["observed_window"]["end"], item["preserved_at"], item["unit"],
                encoded(item["record"]), encoded(item["derivation"]) if item["derivation"] else None, encoded(item["limitations"])))
            for parent in item["derivation"]["parent_evidence_ids"] if item["derivation"] else []:
                db.execute("INSERT INTO evidence_parents VALUES (?,?,?,?)", (item["evidence_id"], parent, item["run_id"], item["incident_id"]))
        self.store._preserve_tool_reply(db, reply)
        db.execute("UPDATE run_budgets SET used_tool_calls=used_tool_calls+1,used_source_bytes=used_source_bytes+?,used_rows=used_rows+? WHERE run_id=?",
                   (size, result["returned_count"], query["run_id"]))
        if evidence:
            self.store._event(db, query["run_id"], "evidence.added", {"evidence_ids": result["evidence_ids"]})
        self.store._event(db, query["run_id"], "tool.finished", {"query_id": query["query_id"], "status": result["status"],
            "evidence_ids": result["evidence_ids"], "returned_count": result["returned_count"],
            "truncated": result["truncated"], "error": None}, query["query_id"])
