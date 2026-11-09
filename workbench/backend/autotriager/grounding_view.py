"""Lossless evidence rendering and a bounded lexical unit-claim guard.

This guard does not establish numerical entailment, unit ownership by a field,
causality, or correctness of a diagnosis. It rejects recognized numeric unit
claims when no supplied source explicitly declares that exact unit.
"""
from __future__ import annotations

import copy
import re


_COMMON_VIEW = "autotriager.w08.common-view.v1"


def _paths(columns):
    if not isinstance(columns, list):
        raise ValueError("Invalid compact evidence columns.")
    seen = []
    for path in columns:
        if (not isinstance(path, list) or not path or
                any(not isinstance(k, str) and type(k) is not int for k in path) or
                any(type(k) is int and k < 0 for k in path)):
            raise ValueError("Invalid compact evidence column path.")
        value = tuple(path)
        if any(value[:len(other)] == other or other[:len(value)] == value for other in seen):
            raise ValueError("Conflicting compact evidence column paths.")
        seen.append(value)


def _record(columns, row):
    if not isinstance(row, list) or len(columns) != len(row):
        raise ValueError("Compact evidence row width mismatch.")
    result = {}
    for path, value in zip(columns, row):
        target = result
        for index, key in enumerate(path):
            last = index == len(path) - 1
            if isinstance(target, list):
                if type(key) is not int:
                    raise ValueError("Invalid compact evidence list path.")
                while len(target) <= key:
                    target.append(None)
                if last:
                    target[key] = copy.deepcopy(value)
                elif target[key] is None:
                    target[key] = [] if type(path[index + 1]) is int else {}
            else:
                if not isinstance(key, str):
                    raise ValueError("Invalid compact evidence object path.")
                if last:
                    target[key] = copy.deepcopy(value)
                elif key not in target:
                    target[key] = [] if type(path[index + 1]) is int else {}
            if not last:
                target = target[key]
    return result


def _walk(value, reverse=False):
    if isinstance(value, list):
        return [_walk(item, reverse) for item in value]
    if not isinstance(value, dict):
        return copy.deepcopy(value)
    result = {key: _walk(item, reverse) for key, item in value.items()}
    if value.get("schema_version") != _COMMON_VIEW:
        return result
    for name, original in value.get("tables", {}).items():
        table = result["tables"][name]
        columns = original.get("columns")
        _paths(columns)
        if not reverse and "rows" in original and "records" not in original:
            table["records"] = [_record(columns, row) for row in original["rows"]]
            del table["rows"]
        elif reverse and "records" in original and "rows" not in original:
            rows = []
            for record in original["records"]:
                row = []
                for path in columns:
                    field = record
                    for key in path:
                        field = field[key]
                    row.append(copy.deepcopy(field))
                rows.append(row)
            table["rows"] = rows
            del table["records"]
    return result


def prepare_model_context(context):
    """Return a copy with compact packet tables rendered as nested records.

    Column paths and all original values remain available; coverage, provenance,
    unknown units, and ordinary live evidence are unchanged. No data is inferred.
    """
    return _walk(context)


def restore_packet_tables(context):
    """Inverse rendering, primarily for exact source-preservation checks."""
    return _walk(context, reverse=True)


# Long names precede short symbols. Case-sensitive byte/bit symbols avoid
# treating an arbitrary final letter in prose as a measurement unit.
_BASE_UNITS = (
    r"microseconds?|milliseconds?|nanoseconds?|seconds?|minutes?|hours?|"
    r"megabytes?|gigabytes?|kilobytes?|terabytes?|bytes?|bits?|percent(?:age)?|"
    r"requests?|operations?|cores?|[KMGT]iB|[kKMGT]B|[kKMGT]bps|Mbps|Gbps|"
    r"[uµμ]s|ms|ns|sec|secs|min|mins|Hz|hz|B|s|h|%|rps|ops"
)
_UNIT_RE = re.compile(
    r"(?P<base>" + _BASE_UNITS + r")"
    r"(?P<rate>\s*(?:/\s*(?:seconds?|secs?|s)|per\s+second))?"
    r"(?![A-Za-zµμ])"
)
_NUMERIC_UNIT_RE = re.compile(
    r"(?<![A-Za-z0-9_])[-+]?(?:\d[\d,]*(?:\.\d+)?|\.\d+)"
    r"(?:[eE][-+]?\d+)?\s*" + _UNIT_RE.pattern
)
_ALIASES = {
    "microsecond": "us", "microseconds": "us", "µs": "us", "μs": "us",
    "millisecond": "ms", "milliseconds": "ms", "nanosecond": "ns", "nanoseconds": "ns",
    "second": "s", "seconds": "s", "sec": "s", "secs": "s",
    "minute": "min", "minutes": "min", "mins": "min", "hour": "h", "hours": "h",
    "byte": "B", "bytes": "B", "kilobyte": "KB", "kilobytes": "KB", "kB": "KB",
    "megabyte": "MB", "megabytes": "MB", "gigabyte": "GB", "gigabytes": "GB",
    "terabyte": "TB", "terabytes": "TB", "bit": "bit", "bits": "bit",
    "percent": "%", "percentage": "%", "request": "request", "requests": "request",
    "operation": "operation", "operations": "operation", "core": "core", "cores": "core",
    "hz": "Hz", "rps": "request/s", "ops": "operation/s",
}


def _unit(match):
    base = match.group("base")
    canonical = _ALIASES.get(base, base)
    return canonical + ("/s" if match.group("rate") else "")


def _declared_units(value):
    units = set()
    if isinstance(value, list):
        for item in value:
            units.update(_declared_units(item))
    elif isinstance(value, dict):
        for key, item in value.items():
            if key in {"unit", "duration_unit"} and isinstance(item, str):
                match = _UNIT_RE.fullmatch(item.strip())
                if match:
                    units.add(_unit(match))
            elif key == "columns" and isinstance(item, list) and "rows" in value:
                for position, path in enumerate(item):
                    if isinstance(path, list) and path and path[-1] in {"unit", "duration_unit"}:
                        for row in value["rows"]:
                            if isinstance(row, list) and position < len(row):
                                units.update(_declared_units({"unit": row[position]}))
            units.update(_declared_units(item))
    return units


def validate_reported_units(text, source_context, historical_projection=None):
    """Reject unsupported recognized numeric-unit claims; return None otherwise.

    Only explicit unit/duration_unit fields authorize a unit. Equivalent word
    spellings are accepted, but scaling (us -> ms, bytes -> MB) is not inferred.
    This is deliberately bounded lexical validation, not semantic verification.
    """
    if not isinstance(text, str):
        raise ValueError("Answer text must be a string.")
    allowed = _declared_units(source_context) | _declared_units(historical_projection)
    for match in _NUMERIC_UNIT_RE.finditer(text):
        if _unit(match) not in allowed:
            raise ValueError("Answer reports a numeric unit not explicitly declared by the supplied evidence.")
