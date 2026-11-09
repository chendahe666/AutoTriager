"""Actual loopback HTTP tests. All investigations use explicitly synthetic data."""
import http.client
import json
import threading
import time
from pathlib import Path
import pytest
from autotriager.server import make_server
from autotriager.contracts import validate


class Client:
    def __init__(self, server):
        self.server = server
        self.cookie = None
        self.csrf = None
        self.origin = f"http://127.0.0.1:{server.server_port}"

    def request(self, method, path, body=None, headers=None):
        head = {"Origin": self.origin}
        if self.cookie:
            head["Cookie"] = self.cookie
        if self.csrf:
            head["X-CSRF-Token"] = self.csrf
        raw = None
        if body is not None:
            raw = json.dumps(body).encode()
            head["Content-Type"] = "application/json"
        head.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=8)
        conn.request(method, path, raw, head)
        response = conn.getresponse()
        data = response.read()
        cookie = response.getheader("Set-Cookie")
        if cookie:
            self.cookie = cookie.split(";",1)[0]
        result = (response.status, dict(response.getheaders()), data)
        conn.close()
        return result

    def json(self, method, path, body=None, headers=None):
        status, _, raw = self.request(method, path, body, headers)
        return status, json.loads(raw)

    def bootstrap(self):
        assert self.request("GET", "/")[0] == 200
        status, caps = self.json("GET", "/api/v1/capabilities")
        assert status == 200
        self.csrf = caps["csrf_token"]
        return caps

    def investigation(self, key="inv-test"):
        incident = self.json("GET", "/api/v1/incidents")[1]["items"][0]
        status, inv = self.json("POST", "/api/v1/investigations", {"incident_id":incident["incident_id"]}, {"Idempotency-Key":key})
        assert status == 201, inv
        return inv

    def run(self, inv, question="Inspect the retained synthetic observations.", parent=None, key="run-test"):
        body = {"question":question}
        if parent:
            body["parent_run_id"] = parent
        status, run = self.json("POST", f"/api/v1/investigations/{inv['investigation_id']}/runs", body, {"Idempotency-Key":key})
        assert status == 202, run
        return run

    def complete(self, run):
        until = time.monotonic()+8
        while time.monotonic()<until:
            status, value = self.json("GET", "/api/v1/runs/"+run["run_id"])
            assert status == 200, value
            if value["state"] in {"COMPLETED","FAILED","CANCELLED"}:
                assert value["state"] == "COMPLETED", value
                return value
            time.sleep(.05)
        pytest.fail("Fixture worker did not complete within the engineering test deadline")


@pytest.fixture
def app(tmp_path):
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<!doctype html><title>API test shell</title>",encoding="utf-8")
    server = make_server(db_path=tmp_path/"app.sqlite3",web_root=web)
    thread = threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    try:
        yield Client(server), server, tmp_path, web
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_health_and_session_bootstrap(app):
    c, _, _, _ = app
    status, health = c.json("GET","/api/v1/health")
    assert status == 200
    validate("Health",health)
    assert c.json("GET","/api/v1/capabilities")[0] == 401
    status, headers, _ = c.request("GET","/")
    assert status == 200
    assert "HttpOnly" in headers["Set-Cookie"] and "SameSite=Strict" in headers["Set-Cookie"]
    caps = c.bootstrap()
    validate("Capabilities",caps)
    assert caps["inference_authorized"] is False and caps["model_configured"] is False
    assert caps["budget"]["max_model_calls"] == 0


def test_recorded_incident_history_http(app, monkeypatch):
    from autotriager import triage

    cases = {
        key: {"id": key, "title": key, "candidates": [{"service": "inventory", "summary": "requests median changed from 10 to 20."}],
              "evidence": [{"id": "E1", "detail": f"retained record {key}"}]}
        for key in ("case-a", "case-b")
    }
    monkeypatch.setattr(triage, "triage_detail", lambda key: {"schema_version": "autotriager.api.v1", "case": cases[key]})
    c, _, _, _ = app
    assert c.bootstrap()["operations_views"] is True
    assert c.json("GET", "/api/v1/incident-history")[1]["items"] == []
    assert c.json("POST", "/api/v1/incident-history", {"case_id": "case-a", "summary": "client value"})[0] == 400
    status, saved = c.json("POST", "/api/v1/incident-history", {"case_id": "case-a"})
    assert status == 201 and not saved["reused"]
    record_id = saved["item"]["id"]
    status, repeated = c.json("POST", "/api/v1/incident-history", {"case_id": "case-a"})
    assert status == 200 and repeated["reused"] and repeated["item"]["id"] == record_id
    assert c.json("GET", "/api/v1/triage/case-a")[1]["exact"]["id"] == record_id
    assert c.json("GET", "/api/v1/triage/case-b")[1]["similar"][0]["id"] == record_id
    status, reopened = c.json("GET", f"/api/v1/incident-history/{record_id}")
    assert status == 200 and reopened["case"]["evidence"] == cases["case-a"]["evidence"]


def test_recorded_evaluation_summary_requires_session_and_starts_no_work(app):
    c, server, _, _ = app
    assert c.json("GET", "/api/v1/evaluation-summary")[0] == 401
    c.bootstrap()
    status, summary = c.json("GET", "/api/v1/evaluation-summary")
    assert status == 200 and isinstance(summary["available"], bool)
    if summary["available"]:
        assert summary["cohort_size"] == 90 and summary["usage"]["generations"] == 288
        assert 'private_labels' not in json.dumps(summary)
    assert server.app.store.list_investigations("local-owner")["items"] == []


def test_parallel_operator_profile_does_not_overwrite_legacy_cookie(app):
    c, _, tmp_path, web = app
    c.bootstrap()
    other = make_server(db_path=tmp_path/'operator.sqlite3', web_root=web, conversation_profile=True)
    thread = threading.Thread(target=other.serve_forever, daemon=True)
    thread.start()
    try:
        operator = Client(other)
        operator.bootstrap()
        assert c.cookie.startswith('at_session=')
        assert operator.cookie.startswith('at_session_conversation=')
        assert operator.json('GET', '/api/v1/capabilities', headers={'Cookie': c.cookie})[0] == 401
        assert c.json('GET', '/api/v1/capabilities', headers={'Cookie': operator.cookie})[0] == 401
        assert c.json('GET', '/api/v1/capabilities')[0] == 200
        assert operator.json('GET', '/api/v1/capabilities')[0] == 200
    finally:
        other.shutdown()
        other.server_close()
        thread.join(timeout=2)


def test_origin_csrf_and_authority_rejection(app):
    c, _, _, _ = app
    c.bootstrap()
    body = {"incident_id":c.json("GET","/api/v1/incidents")[1]["items"][0]["incident_id"]}
    path = "/api/v1/investigations"
    for headers, code in [({"Origin":"https://example.invalid"},"ORIGIN_DENIED"),({"X-CSRF-Token":"wrong"},"CSRF_FAILED")]:
        status, error = c.json("POST",path,body,{"Idempotency-Key":"a",**headers})
        assert status == 403 and error["error"]["code"] == code
        validate("Error",error)
    assert c.json("POST",path,{**body,"actor_scope":"someone-else"},{"Idempotency-Key":"a"})[0] == 400
    assert c.request("GET","/api/v1/health",headers={"Host":"evil.invalid"})[0] == 403


def test_idempotency_and_conflicting_body(app):
    c, _, _, _ = app
    c.bootstrap()
    inv = c.investigation()
    assert c.investigation()["investigation_id"] == inv["investigation_id"]
    status, error = c.json("POST","/api/v1/investigations",{"incident_id":inv["incident_id"],"title":"different"},{"Idempotency-Key":"inv-test"})
    assert status == 409 and error["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    run = c.run(inv)
    again = c.run(inv)
    assert again["run_id"] == run["run_id"]
    assert len(c.json("GET",f"/api/v1/investigations/{inv['investigation_id']}/runs")[1]["items"]) == 1


def test_complete_evidence_query_and_sse_replay(app):
    c, _, _, _ = app
    c.bootstrap()
    completed = c.complete(c.run(c.investigation()))
    validate("Run",completed)
    answer = completed["answer"]
    assert answer and answer["semantic_support_status"] == "not_assessed"
    eid = answer["candidates"][0]["supporting_evidence_ids"][0]
    base = "/api/v1/runs/"+completed["run_id"]
    status, evidence = c.json("GET",base+"/evidence/"+eid)
    assert status == 200
    validate("Evidence",evidence)
    status, detail = c.json("GET",base+"/queries/"+evidence["query_id"])
    assert status == 200
    validate("QueryDetail",detail)
    assert eid in detail["result"]["evidence_ids"]
    assert detail["query"]["incident_id"] == completed["incident_id"]
    status, _, raw = c.request("GET",base+"/events?after=0")
    assert status == 200
    frames = [json.loads(line[6:]) for line in raw.decode().splitlines() if line.startswith("data: ")]
    assert [x["sequence"] for x in frames] == list(range(1,completed["last_sequence"]+1))
    for event in frames:
        validate("Event",event)
    cursor = frames[-2]["sequence"]
    status, _, resumed = c.request("GET",base+f"/events?after={cursor}")
    assert status == 200 and f"id: {cursor+1}\n" in resumed.decode()
    assert c.json("GET",base)[1]["last_sequence"] == completed["last_sequence"]
    assert c.json("GET",base+"/events?after=999999")[0] == 400
    assert c.json("GET",base+"/events?after=0",headers={"Last-Event-ID":"1"})[0] == 400


def test_followup_branch_history_and_cross_run_evidence(app):
    c, _, _, _ = app
    c.bootstrap()
    inv = c.investigation()
    parent = c.complete(c.run(inv))
    a = c.run(inv,"First follow-up.",parent["run_id"],"child-a")
    b = c.run(inv,"Second follow-up.",parent["run_id"],"child-b")
    status, history = c.json("GET",f"/api/v1/investigations/{inv['investigation_id']}/runs")
    assert status == 200 and {r["run_id"] for r in history["items"]} == {parent["run_id"],a["run_id"],b["run_id"]}
    eid = parent["answer"]["candidates"][0]["supporting_evidence_ids"][0]
    assert c.json("GET",f"/api/v1/runs/{a['run_id']}/evidence/{eid}")[0] in (403,404)
    assert c.json("GET","/api/v1/runs/"+parent["run_id"])[1] == parent


def test_cancellation_fences_late_answer(app):
    c, _, _, _ = app
    c.bootstrap()
    run = c.run(c.investigation())
    path = "/api/v1/runs/"+run["run_id"]
    status, cancelled = c.json("POST",path+"/cancel")
    assert status == 200
    validate("CancelReceipt",cancelled)
    time.sleep(.5)
    stopped = c.json("GET",path)[1]
    assert stopped["state"] == "CANCELLED" and stopped["answer"] is None
    assert c.json("POST",path+"/cancel")[1]["state"] == "CANCELLED"


def test_second_application_cannot_recover_live_store(app):
    c, server, state, web = app
    c.bootstrap()
    run = c.run(c.investigation())
    with pytest.raises(RuntimeError,match="already open"):
        make_server(db_path=state/"app.sqlite3",web_root=web)
    assert c.json("GET","/api/v1/runs/"+run["run_id"])[1]["state"] != "CANCELLED"


def test_static_path_and_unknown_route(app):
    c, _, _, _ = app
    c.bootstrap()
    assert c.request("GET","/%2e%2e/session.key")[0] == 404
    assert c.json("GET","/api/v1/not-an-operation")[0] == 404
    assert c.json("GET","/api/v1/incidents?limit=101")[0] == 400
