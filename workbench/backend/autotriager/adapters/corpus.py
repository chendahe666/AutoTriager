"""Explicit normalized JSONL input boundary, NOT a raw RCAEval importer.

Only manifest.json and exact neutral incident/modality files are read. No label
manifest, original case path or arbitrary user-selected file is exposed. Strict
schemas catch common leaks but cannot certify that free-text telemetry is safe;
a source/content audit remains mandatory before real investigations.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from datetime import datetime
from pathlib import Path

SCHEMA_VERSION = "autotriager.neutral_corpus.v1"
MODALITIES = ("metrics", "logs", "traces")
INCIDENT_ID = re.compile(r"incident-[0-9]{4,}\Z")
SERVICE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")


class CorpusError(ValueError):
    """Malformed, out-of-scope or unverified neutral input."""


def _keys(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= set(value) or set(value) - set(required) - set(optional):
        raise CorpusError("invalid normalized fields (labels and extra metadata are not admitted)")


def _time(value):
    if not isinstance(value, str):
        raise CorpusError("timestamp must be ISO 8601 text with timezone")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CorpusError("invalid timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CorpusError("timestamp requires explicit timezone")
    return parsed


def _text(value, name, limit=8192):
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > limit:
        raise CorpusError(f"invalid {name}")


def _number(value, name, nonnegative=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise CorpusError(f"invalid finite {name}")
    if nonnegative and value < 0:
        raise CorpusError(f"negative {name}")


def _read(root: Path, relative: str, limit: int) -> bytes:
    # Caller chooses from a fixed naming grammar; resolution also refuses links
    # escaping the root. Error messages deliberately omit original absolute paths.
    try:
        resolved = (root / relative).resolve(strict=True)
        if not resolved.is_relative_to(root) or not resolved.is_file():
            raise CorpusError("source outside authorized corpus scope")
        with resolved.open("rb") as source:
            data = source.read(limit + 1)
    except (OSError, RuntimeError) as exc:
        raise CorpusError("authorized neutral source unavailable") from exc
    if len(data) > limit:
        raise CorpusError("source exceeds byte limit")
    return data


def _json(data: bytes):
    def reject_constant(value):
        raise CorpusError("non-finite JSON number")

    def unique_object(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise CorpusError("duplicate JSON key")
            obj[key] = value
        return obj

    try:
        return json.loads(data.decode("utf-8"), parse_constant=reject_constant, object_pairs_hook=unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CorpusError("invalid UTF-8 JSON") from exc


def _validate_record(row, modality, services, start, end):
    common = {"timestamp", "service"}
    if modality == "metrics":
        _keys(row, common | {"metric", "value", "unit"})
        _text(row["metric"], "metric", 256)
        _text(row["unit"], "unit", 128)
        _number(row["value"], "metric value")
    elif modality == "logs":
        _keys(row, common | {"severity", "message"})
        _text(row["severity"], "severity", 32)
        _text(row["message"], "message")
    else:
        _keys(row, common | {"trace_id", "span_id", "parent_span_id", "duration_ms", "status"})
        for key in ("trace_id", "span_id", "status"):
            _text(row[key], key, 256)
        if row["parent_span_id"] is not None:
            _text(row["parent_span_id"], "parent_span_id", 256)
        _number(row["duration_ms"], "duration_ms", nonnegative=True)
    if not isinstance(row["service"], str) or row["service"] not in services:
        raise CorpusError("record service outside admissible inventory")
    if not start <= _time(row["timestamp"]) < end:
        raise CorpusError("record outside authorized time window")


class NeutralCorpus:
    """In-memory immutable-by-copy neutral records and content provenance.

    get_records returns unavailable for an absent modality and no_match for a
    present empty file. No original source identity, labels or evaluator scores
    are available through this object.
    """

    def __init__(self, manifest, records, manifest_sha256):
        self._manifest = copy.deepcopy(manifest)
        self._records = copy.deepcopy(records)
        self.manifest_sha256 = manifest_sha256
        self.incident_ids = tuple(item["incident_id"] for item in manifest["incidents"])

    def get_manifest(self):
        return copy.deepcopy(self._manifest)

    def get_records(self, incident_id: str, modality: str) -> dict:
        if incident_id not in self.incident_ids or modality not in MODALITIES:
            raise CorpusError("incident or modality outside authorized corpus scope")
        return copy.deepcopy(self._records[(incident_id, modality)])


def load_corpus(corpus_root, manifest_path="manifest.json", *, max_file_bytes=20_000_000,
                max_records=100_000) -> NeutralCorpus:
    """Load only explicit normalized input, validating hashes before parsing.

    manifest_path intentionally has no arbitrary path support. max_records is a
    total corpus cap, not a per-file allowance. No normalization rule is inferred
    from a dataset filename, fault name or evaluator label.
    """
    if manifest_path != "manifest.json":
        raise CorpusError("only the neutral manifest filename is admitted")
    if not isinstance(max_file_bytes, int) or max_file_bytes <= 0 or not isinstance(max_records, int) or max_records <= 0:
        raise CorpusError("positive byte/record bounds required")
    try:
        root = Path(corpus_root).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise CorpusError("authorized corpus unavailable") from exc
    raw_manifest = _read(root, "manifest.json", min(max_file_bytes, 1_000_000))
    manifest = _json(raw_manifest)
    _keys(manifest, {"schema_version", "normalization_version", "incidents"})
    if manifest["schema_version"] != SCHEMA_VERSION:
        raise CorpusError("unsupported neutral corpus schema")
    _text(manifest["normalization_version"], "normalization_version", 128)
    if not isinstance(manifest["incidents"], list) or not manifest["incidents"]:
        raise CorpusError("nonempty incident inventory required")
    records, seen, total = {}, set(), 0
    for incident in manifest["incidents"]:
        _keys(incident, {"incident_id", "services", "time_window", "modalities"})
        incident_id = incident["incident_id"]
        if not isinstance(incident_id, str) or not INCIDENT_ID.fullmatch(incident_id) or incident_id in seen:
            raise CorpusError("incident IDs must be unique neutral incident-NNNN values")
        seen.add(incident_id)
        services = incident["services"]
        if (not isinstance(services, list) or not services or
                any(not isinstance(s, str) or not SERVICE_ID.fullmatch(s) for s in services) or
                len(set(services)) != len(services)):
            raise CorpusError("unique admissible service inventory required")
        _keys(incident["time_window"], {"start", "end"})
        start, end = (_time(incident["time_window"][k]) for k in ("start", "end"))
        if start >= end:
            raise CorpusError("time window start must precede end")
        _keys(incident["modalities"], set(MODALITIES))
        for modality in MODALITIES:
            spec = incident["modalities"][modality]
            _keys(spec, {"availability"}, {"path", "sha256"})
            if spec["availability"] == "unavailable":
                if set(spec) != {"availability"}:
                    raise CorpusError("unavailable modality cannot carry a source")
                records[(incident_id, modality)] = {"availability": "unavailable", "query_status": "unavailable", "records": []}
                continue
            if spec["availability"] != "present" or set(spec) != {"availability", "path", "sha256"}:
                raise CorpusError("invalid modality source specification")
            relative = f"{incident_id}/{modality}.jsonl"
            if spec["path"] != relative:
                raise CorpusError("modality path must use the exact neutral incident/modality name")
            if not isinstance(spec["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", spec["sha256"]):
                raise CorpusError("lowercase source SHA-256 required")
            raw = _read(root, relative, max_file_bytes)
            if hashlib.sha256(raw).hexdigest() != spec["sha256"]:
                raise CorpusError("source digest mismatch")
            parsed = []
            for number, line in enumerate(raw.splitlines(), 1):
                if not line.strip():
                    raise CorpusError("blank JSONL records are not admitted")
                row = _json(line)
                _validate_record(row, modality, services, start, end)
                total += 1
                if total > max_records:
                    raise CorpusError("corpus record limit exceeded")
                parsed.append({"record": row, "source_hash": spec["sha256"],
                               "locator": f"{relative}:line:{number}"})
            records[(incident_id, modality)] = {"availability": "present",
                "query_status": "success" if parsed else "no_match", "records": parsed}
    return NeutralCorpus(manifest, records, hashlib.sha256(raw_manifest).hexdigest())
