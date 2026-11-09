"""Read-only publication boundary for prepared, label-free triage cases."""

import json
import re
from datetime import datetime
from urllib.parse import urlsplit

from .contracts import ROOT
from .errors import APIError


CASES_PATH = "verification/unified-triage-20261106/cases.json"
CASES_VERSION = "autotriager.triage_cases.v1"
API_VERSION = "autotriager.api.v1"
CATALOG_FIELDS = ("id", "title", "start", "end")
BARO_REPLAYS = {
    "rcaeval-baro-0001": ("verification/baro-incident-replay/replay.json", "Metric BARO"),
    "rcaeval-mmbaro-0001": ("verification/baro-incident-replay/multisource-replay.json", "Multi-source BARO"),
    "rcaeval-b1-0001": ("verification/baro-incident-replay/b1-replay.json", "B1"),
}


def _text_fields(row, fields):
    projected = {key: row[key] for key in fields}
    if any(not isinstance(value, str) or not value.strip() for value in projected.values()):
        raise ValueError("Invalid triage display text")
    return projected


def _project_case(row):
    case = _text_fields(row, CATALOG_FIELDS)
    start = datetime.fromisoformat(case["start"])
    end = datetime.fromisoformat(case["end"])
    if (not re.fullmatch(r"[A-Za-z0-9_-]{1,96}", case["id"])
            or start.utcoffset() is None or end.utcoffset() is None or start >= end):
        raise ValueError("Invalid case ID or window")
    if not isinstance(row["evidence"], list) or not isinstance(row["candidates"], list):
        raise ValueError("Invalid triage evidence or candidates")

    evidence = []
    evidence_ids = set()
    for item in row["evidence"]:
        entry = _text_fields(item, ("id", "title", "detail"))
        if entry["id"] in evidence_ids:
            raise ValueError("Duplicate triage evidence ID")
        evidence_ids.add(entry["id"])
        if source := item.get("source_url"):
            url = urlsplit(source)
            if url.scheme != "https" or not url.hostname or url.username is not None:
                raise ValueError("Invalid source link")
            entry["source_url"] = source
        evidence.append(entry)

    candidates = []
    services = set()
    for item in row["candidates"]:
        candidate = _text_fields(item, ("service", "summary", "next_check"))
        refs = item["evidence_ids"]
        if (not isinstance(refs, list) or not refs or any(ref not in evidence_ids for ref in refs)
                or candidate["service"] in services):
            raise ValueError("Invalid triage candidate or evidence reference")
        services.add(candidate["service"])
        candidates.append({**candidate, "evidence_ids": refs})
    limits = row["limits"]
    if (len(candidates) > 3 or not isinstance(limits, list)
            or any(not isinstance(limit, str) or not limit.strip() for limit in limits)):
        raise ValueError("Invalid triage coverage or candidate count")
    return {**case, "candidates": candidates, "evidence": evidence, "limits": limits}


def _load_cases(root):
    try:
        with (root / CASES_PATH).open(encoding="utf-8") as stream:
            raw = stream.read(1_000_001)
    except FileNotFoundError:
        return {}
    except UnicodeDecodeError as error:
        raise APIError(503, "TRIAGE_INVALID", "Recorded cases did not pass validation.") from error
    except OSError as error:
        raise APIError(503, "TRIAGE_UNAVAILABLE", "Recorded cases could not be opened.") from error

    try:
        if len(raw) > 1_000_000:
            raise ValueError("Case file exceeds size limit")
        payload = json.loads(raw)
        if payload["schema_version"] != CASES_VERSION or not isinstance(payload["cases"], list):
            raise ValueError("Invalid triage receipt")
        cases = {}
        for row in payload["cases"]:
            case = _project_case(row)
            if case["id"] in cases:
                raise ValueError("Duplicate triage case ID")
            cases[case["id"]] = case
        return cases
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        raise APIError(503, "TRIAGE_INVALID", "Recorded cases did not pass validation.") from error


def _replay_methods(root):
    return [{"case_id": case_id, "label": method} for case_id, (path, method) in BARO_REPLAYS.items()
            if (root / path).is_file()]


def triage_catalog(root=ROOT):
    cases = _load_cases(root)
    methods = _replay_methods(root)
    if methods and (replay := load_baro_replay(methods[0]["case_id"], root)):
        case = replay["case"]
        cases[case["id"]] = case
    return {"schema_version": API_VERSION,
            "cases": [{key: case[key] for key in CATALOG_FIELDS} for case in cases.values()]}


def triage_detail(case_id, root=ROOT):
    if case_id in BARO_REPLAYS:
        replay = load_baro_replay(case_id, root)
        case = replay["case"] if replay else None
        if case:
            methods = _replay_methods(root)
            case["baro"].update(methods=methods, primary_case_id=methods[0]["case_id"] if methods else case_id)
    else:
        case = _load_cases(root).get(case_id)
    if case is None:
        raise APIError(404, "TRIAGE_CASE_NOT_FOUND", "This recorded case is unavailable.")
    return {"schema_version": API_VERSION, "case": case}


def load_baro_replay(case_id, root=ROOT):
    """Read the bounded, locally generated public replay as a single snapshot."""
    if case_id not in BARO_REPLAYS:
        return None
    path, method = BARO_REPLAYS[case_id]
    try:
        with (root / path).open(encoding="utf-8") as stream:
            raw = stream.read(1_000_001)
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as error:
        raise APIError(503, "BARO_REPLAY_UNAVAILABLE", "The BARO replay could not be opened.") from error
    try:
        if len(raw) > 1_000_000:
            raise ValueError("Replay exceeds the read limit")
        payload = json.loads(raw)
        case, records, metrics = (payload[key] for key in ("case", "records", "metrics"))
        if (payload["schema_version"] != "autotriager.baro_replay.v1" or case["id"] != case_id
                or metrics["incident_id"] != case_id or case["baro"]["method"] != method):
            raise ValueError("Replay identity changed")
        projected = _project_case(case)
        refs = {item["id"] for item in projected["evidence"]}
        ranking = case["baro"]["ranking"]
        minimum = 1 if method == "B1" else 3
        if (not minimum <= len(ranking) <= 3
                or [item["service"] for item in ranking] != [item["service"] for item in projected["candidates"]]
                or set(records) != refs or any(item["evidence_id"] not in refs for item in ranking)
                or any(record["case_id"] != case_id or record["evidence_id"] != key for key, record in records.items())
                or any(item["id"] not in refs for item in case["topology_edges"])):
            raise ValueError("Replay ranking or evidence identity changed")
        for item, candidate in zip(ranking, projected["candidates"]):
            evidence_ids = item.get("evidence_ids", [item["evidence_id"]])
            if evidence_ids != candidate["evidence_ids"] or evidence_ids[0] != item["evidence_id"]:
                raise ValueError("Replay citation order changed")
        return payload
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        raise APIError(503, "BARO_REPLAY_INVALID", "The BARO replay did not pass validation.") from error


def baro_evidence(case_id, evidence_id):
    replay = load_baro_replay(case_id)
    record = replay["records"].get(evidence_id) if replay else None
    if record is None:
        raise APIError(404, "BARO_EVIDENCE_NOT_FOUND", "This replay evidence is unavailable.")
    return record
