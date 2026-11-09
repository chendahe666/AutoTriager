"""Invented telemetry and a deterministic demonstration, with no inference.

Nothing in this module is RCAEval data, a gold label, or an engine response.
The fixed latency ordering exercises citations; it is not a diagnosis method.
"""
import hashlib
import json
import uuid

VERSION = "autotriager.api.v1"
ENGINE_COMMIT = "869054f0058604109325bdc9e9d0d3d27ff883bd"
POLICY_ID = "synthetic_readonly"
WINDOW = {"start": "2026-11-02T12:00:00Z", "end": "2026-11-02T12:02:00Z"}
LIMITATION = "Invented fixture records; no real incident, RCAEval label, or causal validation."
BUDGET = {
    "max_model_calls": 0, "max_tool_calls": 5, "max_source_bytes": 16384,
    "max_output_tokens": 0, "max_wall_time_seconds": 120,
    "paid_fallback_allowed": False, "max_total_rows": 100,
    "max_total_input_tokens": 0, "max_total_output_tokens": 0,
}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def opaque(prefix):
    return prefix + "_" + uuid.uuid4().hex


METRICS = [
    {"service_id": service, "timestamp": stamp, "metric": "request_duration_ms",
     "value": value, "unit": "ms", "synthetic": True}
    for service, values in (("svc_a", (140, 150)), ("svc_b", (820, 920)))
    for stamp, value in zip(("2026-11-02T12:00:30Z", "2026-11-02T12:01:30Z"), values)
]
TRACES = [
    {"service_id": "svc_a", "timestamp": "2026-11-02T12:01:30Z",
     "trace_id": "invented_trace_01", "span_id": "invented_span_a", "parent_span_id": None,
     "operation": "service_a.request", "duration_ms": 990, "status": "error", "synthetic": True},
    {"service_id": "svc_b", "timestamp": "2026-11-02T12:01:30Z",
     "trace_id": "invented_trace_01", "span_id": "invented_span_b", "parent_span_id": "invented_span_a",
     "operation": "service_b.process", "duration_ms": 920, "status": "error", "synthetic": True},
]


def incident():
    value = {
        "schema_version": VERSION, "incident_id": "fixture_inc_01", "system_id": "synthetic_two_service",
        "display_name": "Synthetic multi-service investigation", "mode": "fixture",
        "data_revision": "invented-fixture-v1", "time_window": dict(WINDOW),
        "normalized_timestamp_unit": "iso8601_utc", "service_ids": ["svc_a", "svc_b"],
        "modalities": {
            "metrics": {"availability": "present", "record_count": len(METRICS),
                        "original_timestamp_unit": "iso8601", "value_units": ["ms"], "reason": None},
            "logs": {"availability": "absent", "record_count": 0,
                     "original_timestamp_unit": "unknown", "value_units": [],
                     "reason": "Logs are deliberately absent from this invented fixture."},
            "traces": {"availability": "present", "record_count": len(TRACES),
                       "original_timestamp_unit": "iso8601", "value_units": ["ms"], "reason": None},
        },
        "coverage_warnings": [LIMITATION, "No healthy reference or logs are available."],
    }
    value["manifest_digest"] = digest(value)
    return value


def config(run_id, inc):
    policy = {"policy_id": POLICY_ID, "version": "fixture-v1", "budget": BUDGET,
              "algorithm": "fixed synthetic latency ordering; no inference", "modalities": ["metrics", "traces", "logs"]}
    value = {
        "schema_version": VERSION, "run_id": run_id, "incident_id": inc["incident_id"],
        "mode": "fixture", "incident_manifest_digest": inc["manifest_digest"],
        "data_revision": inc["data_revision"], "engine_source_commit": ENGINE_COMMIT,
        "engine_build_id": None, "adapter_version": "fixture-no-engine-v1", "model_key": None,
        "resolved_model_id": None, "policy_id": POLICY_ID, "policy_version": "fixture-v1",
        "policy_digest": digest(policy), "inference_authorized": False,
        "grant_id": None, "grant_expires_at": None, "budget": dict(BUDGET),
    }
    value["config_digest"] = digest(value)
    return value


def query_bundle(run_id, inc, modality, preserved_at):
    query_id = opaque("qry")
    query = {
        "schema_version": VERSION, "query_id": query_id, "run_id": run_id,
        "incident_id": inc["incident_id"], "tool_name": "incident_" + modality,
        "modality": modality, "time_window": dict(inc["time_window"]),
        "service_ids": list(inc["service_ids"]), "limit": 100, "max_bytes": 16384,
        "parameters": {"metric_names": ["request_duration_ms"], "aggregation": "mean"}
        if modality == "metrics" else {"status": "any"} if modality == "traces" else {},
    }
    source = METRICS if modality == "metrics" else TRACES if modality == "traces" else []
    evidence = []
    source_digest = digest(source)
    for index, record in enumerate(source):
        evidence.append({
            "schema_version": VERSION, "evidence_id": opaque("ev"), "run_id": run_id,
            "incident_id": inc["incident_id"], "query_id": query_id, "service_id": record["service_id"],
            "modality": modality, "kind": "raw", "source_artifact_digest": source_digest,
            "record_locator": f"fixture_{modality}_v1:record_{index + 1}", "content_digest": digest(record),
            "observed_window": {"start": record["timestamp"], "end": record["timestamp"]},
            "preserved_at": preserved_at, "unit": "ms", "record": dict(record), "derivation": None,
            "limitations": [LIMITATION],
        })
    if modality == "metrics":
        for service in inc["service_ids"]:
            parents = [item for item in evidence if item["service_id"] == service and item["kind"] == "raw"]
            record = {"statistic": "mean", "value": sum(p["record"]["value"] for p in parents) / len(parents),
                      "sample_count": len(parents), "unit": "ms", "synthetic": True}
            evidence.append({
                "schema_version": VERSION, "evidence_id": opaque("ev"), "run_id": run_id,
                "incident_id": inc["incident_id"], "query_id": query_id, "service_id": service,
                "modality": modality, "kind": "derived", "source_artifact_digest": source_digest,
                "record_locator": f"fixture_mean_v1:{service}", "content_digest": digest(record),
                "observed_window": {"start": parents[0]["record"]["timestamp"], "end": parents[-1]["record"]["timestamp"]},
                "preserved_at": preserved_at, "unit": "ms", "record": record,
                "derivation": {"parent_evidence_ids": [p["evidence_id"] for p in parents],
                               "method_id": "arithmetic_mean", "method_version": "fixture-v1",
                               "missing_value_policy": "exclude_null_report_count", "unit": "ms"},
                "limitations": [LIMITATION, "Two invented samples are not a healthy baseline or a causal test."],
            })
    result = {
        "schema_version": VERSION, "query_id": query_id, "run_id": run_id, "incident_id": inc["incident_id"],
        "status": "modality_absent" if modality == "logs" else "success",
        "availability": "absent" if modality == "logs" else "present",
        "evidence_ids": [item["evidence_id"] for item in evidence], "returned_count": len(evidence),
        "matched_count": None if modality == "logs" else len(evidence), "truncated": False,
        "result_complete": modality != "logs",
        "limitations": ["Logs were not collected; absence is not a negative log finding."] if modality == "logs" else [LIMITATION, "Returned and matched row counts include both raw and derived evidence objects."],
        "error": None,
    }
    return query, result, evidence, source


def model_answer(run_id, incident_id, evidence):
    means = {e["service_id"]: e for e in evidence if e["kind"] == "derived"}
    trace = {e["service_id"]: e for e in evidence if e["modality"] == "traces"}
    if set(means) != {"svc_a", "svc_b"} or set(trace) != {"svc_a", "svc_b"}:
        return {
            "schema_version": VERSION, "run_id": run_id, "incident_id": incident_id,
            "status": "insufficient_evidence", "candidates": [],
            "observations": [{"text": "Only partial synthetic records were retained; inspect them before interpreting the example.",
                              "evidence_ids": [item["evidence_id"] for item in evidence]}] if evidence else [],
            "unknowns": [LIMITATION, "The bounded reply did not retain all records required by this deterministic demonstration.",
                         "Logs and a healthy reference are absent; causal origin is unknown."],
            "next_checks": ["Inspect retained evidence and obtain the missing observations before comparison."],
        }
    candidates = []
    for rank, service in enumerate(sorted(means, key=lambda sid: (-means[sid]["record"]["value"], sid)), 1):
        candidates.append({
            "rank": rank, "service_id": service,
            "hypothesis": f"Synthetic examination priority only: {service} has an invented mean request duration of {means[service]['record']['value']:g} ms. This fixed fixture ordering does not establish root cause.",
            "supporting_evidence_ids": [means[service]["evidence_id"], trace[service]["evidence_id"]],
            "contradicting_evidence_ids": [],
            "missing_checks": ["Collect a healthy reference.", "Obtain logs and independently inspect the failure mechanism."],
        })
    return {
        "schema_version": VERSION, "run_id": run_id, "incident_id": incident_id,
        "status": "ranked_candidates", "candidates": candidates,
        "observations": [
            {"text": "Invented trace spans share a trace ID and report errors in both services.",
             "evidence_ids": [trace[s]["evidence_id"] for s in sorted(trace)]},
            {"text": "Invented means are 145 ms for svc_a and 870 ms for svc_b; these samples do not provide a healthy comparison.",
             "evidence_ids": [means[s]["evidence_id"] for s in sorted(means)]},
        ],
        "unknowns": [LIMITATION, "Logs and a healthy reference are absent; causal origin is unknown.",
                     "This deterministic fixture does not interpret the user's question or follow-up history."],
        "next_checks": ["Inspect preserved original records and derivations.",
                        "Audit and ingest an authorized real incident before any diagnostic evaluation."],
    }
