"""Verify the live adapter against an actual local HTTP protocol fixture."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import parse_qs, urlsplit

import pytest

from autotriager import monitoring_prometheus as connector
from autotriager.monitoring import monitoring_data


def test_completed_live_intervals_and_missing_source(monkeypatch):
    end = 1_800_000_000
    calls = []
    fixtures = {"values": [[end - i * 300, str(15 - i)] for i in range(12, -1, -1)]}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urlsplit(self.path)
            calls.append((url.path, parse_qs(url.query)))
            body = json.dumps({"status": "success", "data": {"resultType": "matrix", "result": [fixtures]}}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            settings = {"PROMETHEUS": f"http://127.0.0.1:{server.server_port}", "QUERY": "sum(request_latency_ms)",
                        "SERVICE": "fixture", "METRIC": "latency", "UNIT": "ms", "THRESHOLD": "20"}
            for name, value in settings.items():
                monkeypatch.setenv(connector.PREFIX + name, value)
            monkeypatch.setattr(connector.time, "time", lambda: end + 5)
            sample = connector.live_metrics()
            assert sample["series"][-1] == (end - 300, 15.0)
            assert calls[0] == ("/api/v1/query_range", {"query": [settings["QUERY"]], "start": [str(end - 287 * 300)],
                                                        "end": [str(end)], "step": ["300"], "timeout": ["1s"], "lookback_delta": ["60s"]})
            assert monitoring_data()["freshness"] == "fresh"
            fixtures["values"].pop()
            assert monitoring_data()["freshness"] == "stale"
            fixtures["values"].append([end + 300, "16"])
            assert monitoring_data()["mode"] == "live_unavailable"
            monkeypatch.setenv(connector.PREFIX + "PROMETHEUS", "http://example.invalid")
            with pytest.raises(ValueError, match="Configured monitoring source is unavailable"):
                connector.live_metrics()
        finally:
            server.shutdown()
            thread.join(timeout=3)
