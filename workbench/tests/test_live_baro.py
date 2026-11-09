from datetime import UTC, datetime
import json
import sqlite3

from autotriager.live_baro import LiveBaro, read_samples


LAB = "a" * 32
CASE = {"id": "prom-test", "triggered_at": datetime.fromtimestamp(100, UTC).isoformat(),
        "matched_request": {"lab_id": LAB}}


def row(at, lab=LAB):
    return {"request_id": f"{at:032x}", "observed_at_unix": at, "service": "checkout", "dependency": "payment",
            "route": "/checkout", "dependency_route": "/authorize", "latency_ms": (20 if at < 100 else 250) + at % 7,
            "dependency_latency_ms": (15 if at < 100 else 240) + at % 5, "http_status": 200,
            "control": {"lab_id": lab, "generation": 4, "delay_ms": 240}}


def write_journal(path, rows):
    path.write_text("".join(json.dumps(value) + "\n" for value in rows), encoding="utf-8")


def test_input_excludes_guard_other_lab_control_fields_and_after_cutoff(tmp_path):
    journal = tmp_path / "requests.jsonl"
    write_journal(journal, [row(at) for at in (74, 75, 94, 95, 99, 100, 104, 105)] + [row(102, lab="b" * 32)])
    samples = read_samples(journal, CASE, end=105)
    assert [value["observed_at_unix"] for value in samples] == [75, 94, 100, 104]
    assert all("control" not in value for value in samples)


def test_automatic_result_is_frozen_when_approval_and_journal_change(tmp_path):
    journal = tmp_path / "requests.jsonl"
    write_journal(journal, [row(at) for at in range(75, 111)])
    database = tmp_path / "alerts.sqlite3"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE alerts (id TEXT, triggered_at TEXT, snapshot TEXT)")
        db.execute("CREATE TABLE incident_actions (incident_id TEXT, result TEXT)")
        db.execute("INSERT INTO alerts VALUES (?, ?, ?)", (CASE["id"], CASE["triggered_at"], json.dumps(CASE)))
    engine = LiveBaro(database, journal)
    assert engine.view(CASE["id"])["status"] == "waiting"
    engine.capture_ready()
    view = engine.view(CASE["id"])
    assert view["status"] == "completed"
    assert (view["reference_samples"], view["incident_samples"]) == (20, 10)
    assert {item["service"] for item in view["ranking"]} == {"payment", "checkout"}
    key = view["ranking"][0]["evidence_id"]
    evidence = engine.evidence(CASE["id"], key)
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO incident_actions VALUES (?, ?)", (CASE["id"], json.dumps({"approved_at_unix": 101})))
    journal.write_text("", encoding="utf-8")
    engine.capture_ready()
    assert engine.view(CASE["id"]) == view
    assert engine.evidence(CASE["id"], key) == evidence
