"""Audited, offline conversion of one official RCAEval development case.

This module does not import RCAEval's runners, execute baselines, or call a
provider. The complete maintainer-published Parquet files are verified before
conversion. Original answer-bearing paths and labels belong only to the private
receipt. Unknown source units/severity/status remain explicitly unknown.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .corpus import INCIDENT_ID, SCHEMA_VERSION, SERVICE_ID, CorpusError

SOURCE_COMMIT = "dc9f48786db50cf9469f2c596b79f01907d4ac91"
DATA_REVISION = "afeacb11bcc94dadfd1c8f483ee4377b2b8b614e"
DATA_REPOSITORY = "phamquiluan/RCAEval"
CASE_NAME = "re2ob_checkoutservice_cpu_1"
NORMALIZATION_VERSION = "rcaeval-re2ob-parquet-v1"
INDEX_SHA256 = "c49a288920dbba2e8e724679a14636d5c7eb2b45426bba14007ef79a6c0ab1bb"
CASE_FILES = {
    "inject_time.txt": (10, "c17fcaa1047c07738176849000c1b88e1f0ddda57a2a73a42ab0c8c58c83e0a3"),
    "metrics.parquet": (157212, "f46b3d354b234e37c424f54a005329b61aac83d15dcdd46b5642e55f390ab25a"),
    "logs.parquet": (336876, "e1a25e50c8b0beae4b2886df54d86d2f28877d0b611fed98411e9c0b0c77dad3"),
    "traces.parquet": (10146857, "56ca8f6dbeb76fab2d33faeca54bce8f40e9c5491ebd76404ebb2dfd83409367"),
}
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _integer(value, name):
    if isinstance(value, bool) or not isinstance(value, int):
        raise CorpusError(f"source {name} must be an integer")
    return value


def source_timestamp(value, unit):
    """Convert the declared source unit exactly; never infer by magnitude."""
    value = _integer(value, "timestamp")
    if unit == "s":
        microseconds = value * 1_000_000
    elif unit == "us":
        microseconds = value
    else:
        raise CorpusError("unsupported audited timestamp unit")
    try:
        return (EPOCH + timedelta(microseconds=microseconds)).isoformat(timespec="microseconds").replace("+00:00", "Z")
    except (ValueError, OverflowError) as exc:
        raise CorpusError("source timestamp outside supported calendar") from exc


def _service(value):
    if not isinstance(value, str) or not SERVICE_ID.fullmatch(value):
        raise CorpusError("invalid source service")
    return value


def metric_record(timestamp, column, value):
    """None denotes a missing observation, never an observed zero."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise CorpusError("non-finite source metric")
    if not isinstance(column, str) or "_" not in column or len(column) > 256:
        raise CorpusError("metric lacks the audited service_metric name")
    service, _metric = column.split("_", 1)
    return {"timestamp": source_timestamp(timestamp, "s"), "service": _service(service),
            "metric": column, "value": value, "unit": "unknown"}


def log_record(row):
    message = row.get("message")
    if not isinstance(message, str) or not message or len(message.encode("utf-8")) > 8192:
        raise CorpusError("invalid source log message")
    return {"timestamp": source_timestamp(row["timestamp"], "s"),
            "service": _service(row["container_name"]), "severity": "unknown", "message": message}


def trace_record(row):
    start = _integer(row["startTime"], "span startTime")
    millis = _integer(row["startTimeMillis"], "span startTimeMillis")
    if start // 1000 != millis:
        raise CorpusError("source trace timestamp units disagree")
    duration = _integer(row["duration"], "span duration")
    if duration < 0:
        raise CorpusError("negative source span duration")
    for name in ("traceID", "spanID"):
        if not isinstance(row.get(name), str) or not row[name] or len(row[name]) > 256:
            raise CorpusError("invalid source trace identifier")
    parent = row.get("parentSpanID")
    if parent is not None and (not isinstance(parent, str) or not parent or len(parent) > 256):
        raise CorpusError("invalid source parent span identifier")
    status = row.get("statusCode")
    if status is not None:
        _integer(status, "span statusCode")
    return {"timestamp": source_timestamp(start, "us"), "service": _service(row["serviceName"]),
            "trace_id": row["traceID"], "span_id": row["spanID"], "parent_span_id": parent,
            "duration_ms": duration / 1000, "status": str(status) if status is not None else "unknown"}


def verify_case(case_root):
    """Reject partial, substituted or altered input before producing a corpus."""
    root = Path(case_root).resolve(strict=True)
    verified = {}
    for name, (size, expected_hash) in CASE_FILES.items():
        path = (root / name).resolve(strict=True)
        if not path.is_relative_to(root) or not path.is_file():
            raise CorpusError("raw source outside declared case")
        if path.stat().st_size != size or sha256_file(path) != expected_hash:
            raise CorpusError("raw source size or digest mismatch")
        verified[name] = {"bytes": size, "sha256": expected_hash}
    return verified


def _dump(path, value):
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


def normalize_case(case_root, analysis_root, private_root, *, incident_id="incident-0001",
                   view_start=None, view_end=None, max_output_records=750_000):
    """Write a neutral view and private row lineage from verified real files.

    view_start/end, when given, are explicit integer Unix seconds and half-open.
    With no override the full observed metric coverage is used, inclusive of its
    final sampled second. No filtering by root cause or anomaly score occurs.
    The complete original case is retained regardless of the view window.
    """
    if not isinstance(incident_id, str) or not INCIDENT_ID.fullmatch(incident_id):
        raise CorpusError("neutral incident ID required")
    if isinstance(max_output_records, bool) or not isinstance(max_output_records, int) or max_output_records < 1:
        raise CorpusError("positive output record bound required")
    raw_root = Path(case_root).resolve(strict=True)
    analysis = Path(analysis_root).resolve()
    private = Path(private_root).resolve()
    if analysis == private or analysis in private.parents or private in analysis.parents:
        raise CorpusError("analysis and evaluator-private directories must be separate")
    if raw_root == analysis or raw_root in analysis.parents or analysis in raw_root.parents:
        raise CorpusError("raw and analysis directories must be separate")
    if raw_root == private or raw_root in private.parents or private in raw_root.parents:
        raise CorpusError("raw and evaluator-private directories must be separate")
    if (analysis / "manifest.json").exists():
        raise CorpusError("refusing to overwrite an existing neutral corpus")
    verified = verify_case(raw_root)
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise CorpusError("PyArrow is required for this audited Parquet conversion") from exc
    index_path = raw_root.parent / "cases.parquet"
    if not index_path.is_file() or sha256_file(index_path) != INDEX_SHA256:
        raise CorpusError("official pinned case index is missing or altered")
    source_index = pq.read_table(index_path, filters=[("case", "=", CASE_NAME)]).to_pylist()
    if len(source_index) != 1:
        raise CorpusError("official index must identify exactly one selected case")
    source_index = source_index[0]
    inject_time = int((raw_root / "inject_time.txt").read_text(encoding="utf-8"))
    if inject_time != source_index["inject_time"]:
        raise CorpusError("official index and injection-time file disagree")
    tables = {m: pq.read_table(raw_root / f"{m}.parquet") for m in ("metrics", "logs", "traces")}
    required = {"logs": {"timestamp", "container_name", "message"},
                "traces": {"time", "traceID", "spanID", "serviceName", "methodName", "operationName",
                           "parentSpanID", "startTimeMillis", "startTime", "duration", "statusCode"}}
    for modality, columns in required.items():
        if set(tables[modality].column_names) != columns:
            raise CorpusError("source table schema differs from audited revision")
    metrics = tables["metrics"]
    if "time" not in metrics.column_names or len(metrics.column_names) != 73 or metrics.num_rows != 1441:
        raise CorpusError("source metric shape differs from audited revision")
    times = metrics["time"].to_pylist()
    if times != sorted(set(times)) or any(not isinstance(t, int) for t in times):
        raise CorpusError("source metric timestamps must be sorted unique integers")
    observed_start, observed_end = min(times), max(times) + 1
    start = observed_start if view_start is None else _integer(view_start, "view start")
    end = observed_end if view_end is None else _integer(view_end, "view end")
    if not observed_start <= start < end <= observed_end:
        raise CorpusError("view must be within observed source metric coverage")
    columns = [c for c in metrics.column_names if c != "time"]
    services = {_service(c.split("_", 1)[0]) for c in columns}
    services.update(_service(s) for s in tables["logs"]["container_name"].unique().to_pylist())
    services.update(_service(s) for s in tables["traces"]["serviceName"].unique().to_pylist())
    destination = analysis / incident_id
    destination.mkdir(parents=True, exist_ok=True)
    private.mkdir(parents=True, exist_ok=True)
    lineage_path = private / f"{incident_id}-lineage.jsonl"
    counts, missing, outside, total = {}, {}, {}, 0
    specs = {}
    start_iso, end_iso = source_timestamp(start, "s"), source_timestamp(end, "s")
    try:
        with lineage_path.open("w", encoding="utf-8", newline="\n") as lineage:
            for modality in ("metrics", "logs", "traces"):
                count, missing_count, outside_count = 0, 0, 0
                path = destination / f"{modality}.jsonl"
                with path.open("w", encoding="utf-8", newline="\n") as output:
                    source_row = 0
                    for batch in tables[modality].to_batches(max_chunksize=4096):
                        # Source row numbering is stable across batches; Parquet
                        # rows are 1-based, excluding any schema/header metadata.
                        for row in batch.to_pylist():
                            source_row += 1
                            timestamp = row["time"] if modality == "metrics" else row["timestamp"] if modality == "logs" else row["startTime"] // 1_000_000
                            if not start <= timestamp < end:
                                outside_count += len(columns) if modality == "metrics" else 1
                                continue
                            if modality == "metrics":
                                items = [(metric_record(row["time"], c, row[c]), c) for c in columns]
                            else:
                                items = [((log_record(row) if modality == "logs" else trace_record(row)), None)]
                            for record, column in items:
                                if record is None:
                                    missing_count += 1
                                    continue
                                total += 1
                                if total > max_output_records:
                                    raise CorpusError("normalized output exceeds declared record bound")
                                count += 1
                                output.write(json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n")
                                origin = {"modality": modality, "neutral_line": count, "source_row": source_row}
                                if column is not None:
                                    origin["source_column"] = column
                                lineage.write(json.dumps(origin, separators=(",", ":")) + "\n")
                counts[modality], missing[modality], outside[modality] = count, missing_count, outside_count
                specs[modality] = {"availability": "present", "path": f"{incident_id}/{modality}.jsonl", "sha256": sha256_file(path)}
        if not all(counts.values()):
            raise CorpusError("selected real development view must contain every provided modality")
        manifest = {"schema_version": SCHEMA_VERSION, "normalization_version": NORMALIZATION_VERSION,
                    "incidents": [{"incident_id": incident_id, "services": sorted(services),
                                   "time_window": {"start": start_iso, "end": end_iso}, "modalities": specs}]}
        report = {"incident_id": incident_id, "split": "development-already-inspected", "held_out": False,
                  "source_repository": "https://github.com/phamquiluan/RCAEval", "source_commit": SOURCE_COMMIT,
                  "data_repository": DATA_REPOSITORY, "data_revision": DATA_REVISION, "source_case": CASE_NAME,
                  "raw_case_path": str(raw_root), "files": verified,
                  "label": {"service": source_index["root_cause_service"], "fault": source_index["fault"], "indicator": "cpu", "inject_time": inject_time},
                  "official_index_row": source_index, "official_index_sha256": INDEX_SHA256,
                  "source_counts": {m: t.num_rows for m, t in tables.items()}, "metric_columns": columns,
                  "full_service_inventory": sorted(services), "normalized_counts": counts,
                  "missing_metric_cells_omitted": missing["metrics"], "outside_view_observations": outside,
                  "full_source_missing_metric_cells": sum(metrics[c].null_count for c in columns),
                  "source_observed_window_seconds": {"start": observed_start, "end": observed_end},
                  "selected_window_seconds": {"start": start, "end": end},
                  "view_policy": "explicit half-open engineering preview; known injection time used to center default CLI window",
                  "official_standard_window_seconds": {"start": inject_time - 600, "end": inject_time + 600},
                  "timestamp_units": {"metrics.time": "s", "logs.timestamp": "s", "traces.startTime": "us", "traces.startTimeMillis": "ms"},
                  "span_duration_source_unit": "us", "span_duration_output_unit": "ms",
                  "metric_units": "unknown: upstream tables carry no per-metric unit metadata; no scaling performed",
                  "severity_rule": "unknown: no source severity column",
                  "status_rule": "decimal statusCode text, or unknown for null; no ok/error protocol inferred",
                  "lineage": {"path": lineage_path.name, "sha256": sha256_file(lineage_path), "source_row_index": "1-based Parquet table row"},
                  "normalization_version": NORMALIZATION_VERSION,
                  "excluded_source_trace_columns": ["time", "methodName", "operationName", "startTimeMillis"],
                  "limitations": ["one inspected development case; no benchmark or generalization result", "known onset is exposed through preview placement; unsuitable for held-out performance claims", "service names are preserved; frontend/frontendservice alias is unresolved", "neutral schema omits trace operations; no complete causal chain or topology claimed"]}
        _dump(private / f"{incident_id}-audit.json", report)
        _dump(analysis / "manifest.json", manifest)  # publication boundary: last file
        report["manifest_sha256"] = sha256_file(analysis / "manifest.json")
        _dump(private / f"{incident_id}-audit.json", report)
        return report
    except Exception:
        # No manifest means consumers fail closed; retain partial files for an
        # auditable failure rather than presenting partial telemetry as complete.
        raise
