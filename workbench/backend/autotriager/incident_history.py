"""Versioned triage snapshots and actor-scoped prior evidence lookup."""

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path
from uuid import uuid4

from .errors import APIError
from .triage import API_VERSION


_SUMMARY = re.compile(r"([\w.:-]+) median changed from (\S+) to (\S+)\.")
_ITEM_FIELDS = "id, case_id, title, reviewed_at, summary"


def _snapshot(case):
    payload = json.dumps(case, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    revision = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return payload, revision


def _item(row):
    return {key: row[key] for key in ("id", "case_id", "title", "reviewed_at", "summary")}


def _signals(case):
    """Compare observable metric changes published by the triage adapter."""
    signals = set()
    for candidate in case["candidates"]:
        match = _SUMMARY.fullmatch(candidate["summary"])
        if match:
            metric, before, after = match.groups()
            try:
                before, after = float(before), float(after)
            except ValueError:
                continue
            if isfinite(before) and isfinite(after) and before != after:
                direction = "increased" if after > before else "decreased"
                signals.add((candidate["service"], metric, direction))
    return signals


class IncidentHistory:
    """Store immutable snapshots of published, label-free triage cases."""

    def __init__(self, db_path, *, read_only=False):
        database = Path(db_path)
        self.path = database.with_name(database.name + ".incident-history.sqlite3").resolve()
        self.read_only = read_only

    def _rows(self, sql, parameters):
        if not self.path.exists():
            return []
        with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            if db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'incidents'").fetchone() is None:
                return []
            return db.execute(sql, parameters).fetchall()

    def list(self, actor):
        rows = self._rows(
            f"SELECT {_ITEM_FIELDS} FROM incidents WHERE actor = ? ORDER BY reviewed_at DESC, id DESC",
            (actor,),
        )
        return {"schema_version": API_VERSION, "items": [_item(row) for row in rows]}

    def exact_match(self, actor, case):
        _, revision = _snapshot(case)
        rows = self._rows(
            f"SELECT {_ITEM_FIELDS} FROM incidents WHERE actor = ? AND case_id = ? AND revision = ?",
            (actor, case["id"], revision),
        )
        return _item(rows[0]) if rows else None

    def similar(self, actor, case):
        signals = _signals(case)
        if not signals:
            return []

        rows = self._rows(
            """SELECT * FROM incidents WHERE actor = ? AND case_id != ?
               ORDER BY reviewed_at DESC, id DESC LIMIT 100""",
            (actor, case["id"]),
        )
        matches = []
        for row in rows:
            previous = _signals(json.loads(row["snapshot_json"]))
            shared = sorted(signals & previous)
            if shared:
                score = len(shared) / len(signals | previous)
                service, metric, direction = shared[0]
                reason = f"{metric} {direction} in {service} in both cases. Check current evidence before reusing this observation."
                matches.append((score, {**_item(row), "reason": reason}))

        distinct_cases = {}
        for _, item in sorted(matches, key=lambda match: match[0], reverse=True):
            distinct_cases.setdefault(item["case_id"], item)
        return list(distinct_cases.values())[:3]

    def _detail(self, actor, row):
        case = json.loads(row["snapshot_json"])
        return {"schema_version": API_VERSION, "item": _item(row), "case": case,
                "similar": self.similar(actor, case)}

    def detail(self, actor, incident_id):
        rows = self._rows("SELECT * FROM incidents WHERE actor = ? AND id = ?", (actor, incident_id))
        if not rows:
            raise APIError(404, "INCIDENT_NOT_FOUND", "This saved incident is unavailable.")
        return self._detail(actor, rows[0])

    def save(self, actor, case):
        if self.read_only:
            raise APIError(403, "INCIDENT_HISTORY_READ_ONLY", "Saved incident history is read-only.")

        payload, revision = _snapshot(case)
        candidates = case["candidates"]
        summary = candidates[0]["summary"] if candidates else "No candidate was prioritized in this triage case."
        if candidates:
            summary = f"{candidates[0]['service']}: {summary}"

        with closing(sqlite3.connect(self.path)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("""
                CREATE TABLE IF NOT EXISTS incidents (
                    id TEXT PRIMARY KEY,
                    actor TEXT NOT NULL,
                    case_id TEXT NOT NULL,
                    revision TEXT NOT NULL,
                    title TEXT NOT NULL,
                    reviewed_at TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    UNIQUE (actor, case_id, revision)
                )
            """)
            inserted = db.execute("""
                INSERT INTO incidents (id, actor, case_id, revision, title, reviewed_at, summary, snapshot_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (actor, case_id, revision) DO NOTHING
            """, (uuid4().hex, actor, case["id"], revision, case["title"],
                  datetime.now(timezone.utc).isoformat(timespec="microseconds"), summary, payload))
            row = db.execute(
                "SELECT * FROM incidents WHERE actor = ? AND case_id = ? AND revision = ?",
                (actor, case["id"], revision),
            ).fetchone()
            reused = inserted.rowcount == 0
        return {**self._detail(actor, row), "reused": reused}
