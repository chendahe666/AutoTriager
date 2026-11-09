"""Project local offline pilot receipts into the read-only workbench view."""

import json
import math
from urllib.parse import urlsplit

from .contracts import ROOT


RECEIPTS = "verification/proactive-replay-pilot-20261106"
DATASETS = ("petshop", "eadro")


def _strings(row, fields):
    projected = {field: row[field] for field in fields}
    if any(not isinstance(value, str) for value in projected.values()):
        raise ValueError("Invalid receipt text")
    return projected


def _number(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("Invalid receipt number")
    return value


def _chart(chart):
    projected = _strings(chart, ("label", "unit"))
    if chart.get("threshold") is not None:
        projected["threshold"] = _number(chart["threshold"])
    projected["points"] = []
    for point in chart["points"]:
        row = _strings(point, ("time",))
        for field in ("observed", "forecast"):
            if point.get(field) is not None:
                row[field] = _number(point[field])
        projected["points"].append(row)
    return projected


def _experiment(value, dataset):
    projected = _strings(value, ("id", "source", "title", "scope", "summary"))
    if projected["id"] != dataset:
        raise ValueError("Mismatched dataset")
    projected["metrics"] = [_strings(row, ("label", "value", "note")) for row in value["metrics"]]
    projected["actions"] = [_strings(row, ("label", "detail", "status")) for row in value["actions"]]
    projected["evidence"] = []
    evidence_ids = set()
    for row in value["evidence"]:
        evidence = _strings(row, ("id", "title", "detail"))
        if not evidence["id"] or evidence["id"] in evidence_ids:
            raise ValueError("Missing or duplicate evidence ID")
        evidence_ids.add(evidence["id"])
        if row.get("source_url"):
            url = urlsplit(row["source_url"])
            if url.scheme == "https" and url.hostname and url.username is None:
                evidence["source_url"] = row["source_url"]
        projected["evidence"].append(evidence)
    projected["timeline"] = []
    for row in value["timeline"]:
        event = _strings(row, ("time", "title", "state"))
        refs = row["evidence_ids"]
        if not isinstance(refs, list) or any(ref not in evidence_ids for ref in refs):
            raise ValueError("Unresolved timeline evidence")
        event["evidence_ids"] = refs
        projected["timeline"].append(event)
    if value.get("chart") is not None:
        projected["chart"] = _chart(value["chart"])
    return projected


def replay_summary(root=ROOT):
    result = {"schema_version": "autotriager.api.v1", "replay_schema_version": "autotriager.proactive_replay.v1",
              "experiments": [], "pending": [], "errors": []}
    for dataset in DATASETS:
        try:
            path = root / RECEIPTS / f"{dataset}.json"
            with path.open(encoding="utf-8") as file:
                raw = file.read(2_000_001)
            if len(raw) > 2_000_000:
                raise ValueError("Receipt exceeds display limit")
            result["experiments"].append(_experiment(json.loads(raw), dataset))
        except FileNotFoundError:
            result["pending"].append(dataset)
        except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError):
            result["errors"].append(dataset)
    return result
