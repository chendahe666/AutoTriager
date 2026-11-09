"""The preserved CLI history viewer cannot become a model-dispatch surface."""
import http.client
import json
import threading

from autotriager.server import make_server
from autotriager.sessions import SessionManager
from autotriager.store import Store
from autotriager.server import Application
import pytest
import sqlite3


def test_viewer_has_separate_cookie_scope_and_rejects_mutations(tmp_path):
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<!doctype html><title>Qualification viewer test</title>")
    server = make_server(db_path=tmp_path / "view.sqlite3", web_root=web, qualification_view=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    cookie = None

    def request(method, path, body=None, csrf=None):
        nonlocal cookie
        headers = {"Origin": origin}
        if cookie:
            headers["Cookie"] = cookie
        if csrf:
            headers["X-CSRF-Token"] = csrf
        raw = json.dumps(body).encode() if body is not None else None
        if raw:
            headers.update({"Content-Type": "application/json", "Idempotency-Key": "blocked-viewer"})
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        conn.request(method, path, raw, headers)
        response = conn.getresponse()
        data = response.read()
        if response.getheader("Set-Cookie"):
            cookie = response.getheader("Set-Cookie").split(";", 1)[0]
        conn.close()
        return response.status, data

    try:
        assert request("GET", "/")[0] == 200
        assert cookie.startswith("at_session_qualification=")
        status, data = request("GET", "/api/v1/capabilities")
        caps = json.loads(data)
        assert status == 200 and caps["allowed_policy_ids"] == []
        store = server.app.store
        inc = store.list_incidents()["items"][0]
        visible, _ = store.create_investigation("w07-qualified-cli", {"incident_id": inc["incident_id"]}, "cli-inv")
        foreign, _ = store.create_investigation("local-owner", {"incident_id": inc["incident_id"]}, "normal-inv")
        status, data = request("GET", "/api/v1/investigations")
        assert status == 200
        assert [item["investigation_id"] for item in json.loads(data)["items"]] == [visible["investigation_id"]]
        assert request("GET", "/api/v1/investigations/" + foreign["investigation_id"])[0] == 404
        status, data = request("POST", "/api/v1/investigations", {"incident_id": inc["incident_id"]}, caps["csrf_token"])
        assert status == 403 and json.loads(data)["error"]["code"] == "QUALIFICATION_READ_ONLY"
        normal_sessions = SessionManager(store.db_path, tmp_path)
        assert normal_sessions.lookup(cookie) is None
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_viewer_refuses_unfinished_history_before_restart_recovery(tmp_path):
    path = tmp_path / "unfinished.sqlite3"
    store = Store(path)
    incident = store.seed_fixture()
    inv, _ = store.create_investigation("w07-qualified-cli", {"incident_id": incident["incident_id"]}, "pending-inv")
    run, _ = store.create_run("w07-qualified-cli", inv["investigation_id"], {"question": "Synthetic unfinished run"}, "pending-run")
    store.start_fixture(run["run_id"])
    store.close()
    with pytest.raises(RuntimeError, match="unfinished"):
        Application(path, qualification_view=True)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT state FROM runs WHERE run_id=?", (run["run_id"],)).fetchone()[0] == "RUNNING"
