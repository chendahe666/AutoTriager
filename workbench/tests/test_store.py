"""Offline integration checks over the copied SQLite and v1 contracts."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time

import pytest
from jsonschema import Draft202012Validator, FormatChecker

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from autotriager.errors import APIError
from autotriager import fixture
from autotriager.store import Store
from autotriager.worker import FixtureWorker

ACTOR = "local-owner"


@pytest.fixture
def store(tmp_path):
    instance = Store(tmp_path / "workbench.sqlite3")
    instance.seed_fixture()
    yield instance
    instance.close()


def investigate(store, suffix="one"):
    incident_id = store.list_incidents()["items"][0]["incident_id"]
    inv, _ = store.create_investigation(ACTOR, {"incident_id": incident_id}, "inv-" + suffix)
    return inv


def run(store, inv, suffix="one", **fields):
    body = {"question": "Which invented records should be examined?", **fields}
    result, _ = store.create_run(ACTOR, inv["investigation_id"], body, "run-" + suffix)
    return result


def complete(store, record):
    worker = FixtureWorker(store, stage_delay=0.005)
    try:
        worker.submit(record["run_id"]).result(timeout=5)
    finally:
        worker.close()
    result = store.get_run(ACTOR, record["run_id"])
    assert result["state"] == "COMPLETED"
    return result


def check(store, name, value):
    schema = {"$ref": "#/$defs/" + name, "$defs": store.schema["$defs"]}
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(value)


def test_completed_objects_durable_queries_lineage_raw_output_and_schema(store):
    inc = store.list_incidents()["items"][0]
    check(store, "Incident", inc)
    inv = investigate(store)
    check(store, "Investigation", inv)
    initial = run(store, inv)
    check(store, "Run", initial)
    result = complete(store, initial)
    check(store, "Run", result)
    assert result["config"]["inference_authorized"] is False
    assert result["config"]["model_key"] is None
    assert result["answer"]["semantic_support_status"] == "not_assessed"
    assert [c["service_id"] for c in result["answer"]["candidates"]] == ["svc_b", "svc_a"]
    events = store.get_events(ACTOR, result["run_id"])
    assert [e["sequence"] for e in events] == list(range(1, len(events) + 1))
    for event in events:
        check(store, "Event", event)
    assert store.get_events(ACTOR, result["run_id"], events[-2]["sequence"]) == [events[-1]]
    queries = [e["payload"]["query_id"] for e in events if e["event_type"] == "tool.finished"]
    details = [store.get_query(ACTOR, result["run_id"], qid) for qid in queries]
    for detail in details:
        check(store, "QueryDetail", detail)
    absent = [d for d in details if d["query"]["modality"] == "logs"][0]["result"]
    assert absent["status"] == "modality_absent"
    assert absent["availability"] == "absent" and absent["result_complete"] is False
    evidence = [store.get_evidence(ACTOR, result["run_id"], eid)
                for detail in details for eid in detail["result"]["evidence_ids"]]
    assert len(evidence) == 8
    by_id = {e["evidence_id"]: e for e in evidence}
    for item in evidence:
        check(store, "Evidence", item)
        assert item["content_digest"] == fixture.digest(item["record"])
        assert hashlib.sha256((store.assets_dir / (item["source_artifact_digest"] + ".json")).read_bytes()).hexdigest() == item["source_artifact_digest"]
        if item["derivation"]:
            parents = [by_id[eid] for eid in item["derivation"]["parent_evidence_ids"]]
            assert all(p["kind"] == "raw" and p["service_id"] == item["service_id"] for p in parents)
            assert item["record"]["value"] == sum(p["record"]["value"] for p in parents) / len(parents)
    raw_path = store.assets_dir / (result["answer"]["raw_output_digest"] + ".json")
    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    check(store, "ModelAnswer", raw)
    assert "answer_id" not in raw
    assert hashlib.sha256(raw_path.read_bytes()).hexdigest() == result["answer"]["raw_output_digest"]
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM evidence_parents").fetchone()[0] == 4
        assert db.execute("SELECT used_model_calls,used_tool_calls FROM run_budgets").fetchone() == (0, 3)
        assert db.execute("SELECT COUNT(*) FROM dispatch_reservations").fetchone()[0] == 0
    store.close()
    reopened = Store(store.db_path)
    try:
        assert reopened.get_run(ACTOR, result["run_id"]) == result
        assert reopened.get_evidence(ACTOR, result["run_id"], evidence[0]["evidence_id"]) == evidence[0]
        assert reopened.get_events(ACTOR, result["run_id"]) == events
    finally:
        reopened.close()


def test_idempotency_atomic_replay_and_changed_body_conflict(store):
    inv = investigate(store)
    body = {"question": "same request"}
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(store.create_run, ACTOR, inv["investigation_id"], body, "simultaneous") for _ in range(2)]
        replies = [future.result() for future in futures]
    assert sorted(replayed for _, replayed in replies) == [False, True]
    assert replies[0][0] == replies[1][0]
    before = replies[0][0]
    complete(store, before)
    response, replayed = store.create_run(ACTOR, inv["investigation_id"], body, "simultaneous")
    assert replayed and response == before  # replay the original 202 snapshot
    with pytest.raises(APIError) as failure:
        store.create_run(ACTOR, inv["investigation_id"], {"question": "changed"}, "simultaneous")
    assert failure.value.status == 409
    assert failure.value.code == "IDEMPOTENCY_CONFLICT"
    assert len(store.list_runs(ACTOR, inv["investigation_id"])["items"]) == 1


def test_scope_rejects_other_actor_other_run_and_parent_in_other_investigation(store):
    inv = investigate(store)
    first = complete(store, run(store, inv))
    eid = first["answer"]["candidates"][0]["supporting_evidence_ids"][0]
    qid = store.get_evidence(ACTOR, first["run_id"], eid)["query_id"]
    for read in (lambda: store.get_investigation("other", inv["investigation_id"]),
                 lambda: store.get_run("other", first["run_id"]),
                 lambda: store.get_events("other", first["run_id"]),
                 lambda: store.get_evidence("other", first["run_id"], eid),
                 lambda: store.get_query("other", first["run_id"], qid),
                 lambda: store.cancel_run("other", first["run_id"]),
                 lambda: store.list_runs("other", inv["investigation_id"])):
        with pytest.raises(APIError) as failure:
            read()
        assert failure.value.status == 404
    inv2 = investigate(store, "second")
    second = run(store, inv2, "second")
    for read in (lambda: store.get_evidence(ACTOR, second["run_id"], eid),
                 lambda: store.get_query(ACTOR, second["run_id"], qid),
                 lambda: run(store, inv2, "bad-parent", parent_run_id=first["run_id"])):
        with pytest.raises(APIError) as failure:
            read()
        assert failure.value.status == 404


def test_cancellation_fence_during_validation_and_terminal_immutability(store):
    record = run(store, investigate(store))
    worker = FixtureWorker(store, stage_delay=0.06)
    future = worker.submit(record["run_id"])
    try:
        deadline = time.monotonic() + 5
        while store.get_run(ACTOR, record["run_id"])["state"] != "VALIDATING":
            assert time.monotonic() < deadline
            time.sleep(0.005)
        receipt = store.cancel_run(ACTOR, record["run_id"])
        check(store, "CancelReceipt", receipt)
        assert receipt["state"] == "CANCELLED" and receipt["remote_stop_status"] == "not_dispatched"
        future.result(timeout=5)
    finally:
        worker.close()
    current = store.get_run(ACTOR, record["run_id"])
    assert current["state"] == "CANCELLED" and current["answer"] is None
    assert store.complete_fixture(record["run_id"]) is False
    assert store.cancel_run(ACTOR, record["run_id"]) == receipt
    events = store.get_events(ACTOR, record["run_id"])
    assert events[-1]["payload"]["to_state"] == "CANCELLED"
    assert not any(e["event_type"] == "answer.received" for e in events)
    with sqlite3.connect(store.db_path) as db:
        with pytest.raises(sqlite3.IntegrityError, match="terminal_run_is_immutable"):
            db.execute("UPDATE runs SET revision=revision+1,updated_at='2026-11-02T23:59:59Z' WHERE run_id=?", (record["run_id"],))


def test_followup_preserves_parent_frozen_configuration_and_cursor_pages(store):
    inv = investigate(store)
    parent_initial = run(store, inv)
    with pytest.raises(APIError) as failure:
        run(store, inv, "early-child", parent_run_id=parent_initial["run_id"])
    assert failure.value.code == "CONFLICT"
    parent = complete(store, parent_initial)
    child = complete(store, run(store, inv, "child", parent_run_id=parent["run_id"]))
    assert child["parent_run_id"] == parent["run_id"]
    assert child["config_digest"] != parent["config_digest"]
    assert store.get_run(ACTOR, parent["run_id"]) == parent
    page = store.list_runs(ACTOR, inv["investigation_id"], limit=1)
    assert page["items"] == [parent] and page["next_cursor"]
    last = store.list_runs(ACTOR, inv["investigation_id"], limit=1, cursor=page["next_cursor"])
    assert last["items"] == [child] and last["next_cursor"] is None
    with pytest.raises(APIError) as failure:
        store.list_investigations(ACTOR, limit=1, cursor=page["next_cursor"])
    assert failure.value.code == "INVALID_CURSOR"
    assert store.get_investigation(ACTOR, inv["investigation_id"])["latest_run_id"] == child["run_id"]


def test_restart_fences_incomplete_work_and_preserves_collected_evidence(store):
    record = run(store, investigate(store))
    assert store.start_fixture(record["run_id"])
    assert store.persist_fixture_query(record["run_id"], "metrics")
    events = store.get_events(ACTOR, record["run_id"])
    eid = next(e["payload"]["evidence_ids"][0] for e in events if e["event_type"] == "evidence.added")
    preserved = store.get_evidence(ACTOR, record["run_id"], eid)
    store.close()
    restarted = Store(store.db_path)
    try:
        result = restarted.get_run(ACTOR, record["run_id"])
        assert result["state"] == "CANCELLED" and result["termination_code"] == "PROCESS_RESTARTED"
        assert restarted.get_evidence(ACTOR, record["run_id"], eid) == preserved
        assert restarted.start_fixture(record["run_id"]) is False
        assert restarted.get_events(ACTOR, record["run_id"])[:len(events)] == events
    finally:
        restarted.close()


def test_unknown_fields_disabled_policy_and_offline_replay_are_rejected(store):
    inv = investigate(store)
    for body, status in (({"question": "x", "model_key": "override"}, 400),
                         ({"question": "x", "policy_id": "unconfigured"}, 409),
                         ({"question": " "}, 400)):
        with pytest.raises(APIError) as failure:
            store.create_run(ACTOR, inv["investigation_id"], body, "rejected")
        assert failure.value.status == status
    inc = store.get_incident(inv["incident_id"])
    inc["incident_id"] = "unavailable_replay"
    inc["mode"] = "offline_replay"
    with sqlite3.connect(store.db_path) as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("INSERT INTO incidents VALUES (?,?,?,?,?,?,?,?,?)", (inc["incident_id"], inc["system_id"], "Unavailable replay placeholder for a denial test", "offline_replay", inc["data_revision"], inc["manifest_digest"], inc["time_window"]["start"], inc["time_window"]["end"], json.dumps(inc)))
    off, _ = store.create_investigation(ACTOR, {"incident_id": inc["incident_id"]}, "off-investigation")
    with pytest.raises(APIError) as failure:
        store.create_run(ACTOR, off["investigation_id"], {"question": "No inference may be dispatched"}, "off-run")
    # Recorded-data inspection is now a separate zero-model policy. A missing
    # adapter remains unavailable, independently of any provider grant.
    assert failure.value.status == 409 and failure.value.code == "CONFIGURATION_NOT_READY"


def test_persisted_wall_time_and_aggregate_query_budgets_fence_work(store, monkeypatch):
    inv = investigate(store)
    monkeypatch.setitem(fixture.BUDGET, "max_wall_time_seconds", 0)
    expired = run(store, inv, "deadline")
    assert store.start_fixture(expired["run_id"]) is False
    assert store.get_run(ACTOR, expired["run_id"])["state"] == "TIMED_OUT"
    monkeypatch.setitem(fixture.BUDGET, "max_wall_time_seconds", 120)
    monkeypatch.setitem(fixture.BUDGET, "max_tool_calls", 1)
    limited = run(store, inv, "limited")
    assert store.start_fixture(limited["run_id"])
    assert store.persist_fixture_query(limited["run_id"], "metrics")
    assert store.persist_fixture_query(limited["run_id"], "traces") is False
    assert store.get_run(ACTOR, limited["run_id"])["state"] == "BUDGET_EXHAUSTED"


def test_ledger_charges_exact_preserved_tool_reply_bytes_and_derived_rows(store):
    completed = complete(store, run(store, investigate(store)))
    query_ids = [event["payload"]["query_id"] for event in store.get_events(ACTOR, completed["run_id"])
                 if event["event_type"] == "tool.finished"]
    exact_bytes = 0
    rows = 0
    with store._db() as db:
        for query_id in query_ids:
            reply = store._tool_reply(db, completed["run_id"], query_id)
            check(store, "ToolReply", reply)
            if reply["query"]["modality"] == "metrics":
                assert reply["query"]["parameters"]["aggregation"] == "mean"
            serialized = fixture.canonical(reply)
            path = store.assets_dir / (hashlib.sha256(serialized).hexdigest() + ".json")
            assert path.read_bytes() == serialized
            assert len(serialized) <= reply["query"]["max_bytes"]
            assert reply["result"]["evidence_ids"] == [e["evidence_id"] for e in reply["evidence"]]
            assert reply["result"]["returned_count"] == len(reply["evidence"])
            exact_bytes += len(serialized)
            rows += len(reply["evidence"])
        ledger = db.execute("SELECT * FROM run_budgets WHERE run_id=?", (completed["run_id"],)).fetchone()
    assert ledger["used_source_bytes"] == exact_bytes
    assert exact_bytes > len(fixture.canonical(fixture.METRICS)) + len(fixture.canonical(fixture.TRACES))
    assert exact_bytes <= ledger["max_source_bytes"] == 16384
    assert ledger["used_rows"] == rows == 8  # six metric objects include two derived means


def test_reply_limits_truncate_whole_records_and_never_hide_derivation_parents(store):
    query, result, evidence, _ = fixture.query_bundle("synthetic_run", fixture.incident(), "metrics", "2026-11-02T12:00:00Z")
    query["limit"] = 5
    reply = store._fit_tool_reply(query, result, evidence, 16384, 100)
    assert reply["result"]["status"] == "truncated" and reply["result"]["truncated"] is True
    assert reply["result"]["result_complete"] is False
    assert reply["result"]["returned_count"] == 5 and reply["result"]["matched_count"] == 6
    ids = {item["evidence_id"] for item in reply["evidence"]}
    assert all(set(item["derivation"]["parent_evidence_ids"]) <= ids for item in reply["evidence"] if item["derivation"])
    # A row ceiling applies to materialized derived objects too.
    query["limit"] = 100
    reduced = store._fit_tool_reply(query, result, evidence, 16384, 2)
    assert reduced["result"]["returned_count"] == 2
    assert all(item["kind"] == "raw" for item in reduced["evidence"])
    full = store._fit_tool_reply(query, result, evidence, 16384, 100)
    byte_limited = store._fit_tool_reply(query, result, evidence, len(fixture.canonical(full)) - 1, 100)
    assert byte_limited["result"]["truncated"] is True
    assert len(fixture.canonical(byte_limited)) <= len(fixture.canonical(full)) - 1
    assert store._fit_tool_reply(query, result, evidence, 1, 100) is None


def test_no_minimal_tool_reply_fits_fences_without_publishing_observations(store, monkeypatch):
    monkeypatch.setitem(fixture.BUDGET, "max_source_bytes", 100)
    record = run(store, investigate(store), "insufficient-bytes")
    assert store.start_fixture(record["run_id"])
    assert store.persist_fixture_query(record["run_id"], "metrics") is False
    result = store.get_run(ACTOR, record["run_id"])
    assert result["state"] == "BUDGET_EXHAUSTED" and result["answer"] is None
    assert not any(e["event_type"] in {"tool.finished", "evidence.added"} for e in store.get_events(ACTOR, record["run_id"]))
    with store._db() as db:
        assert db.execute("SELECT used_source_bytes,used_rows,used_tool_calls FROM run_budgets WHERE run_id=?", (record["run_id"],)).fetchone()[:] == (0, 0, 0)
        assert db.execute("SELECT COUNT(*) FROM queries WHERE run_id=?", (record["run_id"],)).fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM evidence WHERE run_id=?", (record["run_id"],)).fetchone()[0] == 0
