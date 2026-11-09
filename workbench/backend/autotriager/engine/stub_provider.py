"""Deterministic loopback OpenAI-compatible test fixture, never an LLM.

It requests one declared incident_manifest tool if present, then emits a
clearly marked compatibility answer. It makes no root-cause claims.
"""
from __future__ import annotations

import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class StubHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def _reply(self, status: int, body: dict):
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):
        if self.path == "/health":
            self._reply(200, {"status": "ok", "kind": "deterministic-local-stub"})
        else:
            self._reply(404, {"error": "unknown path"})

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            return self._reply(404, {"error": "unknown path"})
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length <= 4_000_000:
            return self._reply(413, {"error": "bounded request required"})
        request = json.loads(self.rfile.read(length))
        tools = [item["function"]["name"] for item in request.get("tools", [])]
        messages = request.get("messages", [])
        tool_messages = [item for item in messages if item.get("role") == "tool"]
        tool_name = next((name for name in tools if name == "incident_manifest"), None)
        if tool_name and len(tool_messages) < 2:
            if tool_messages and "incident_metrics" in tools:
                tool_name, arguments, call_id = "incident_metrics", '{"limit":2}', "call_w06_local_metrics"
            else:
                arguments, call_id = "{}", "call_w06_local_manifest"
            message = {"role": "assistant", "content": None, "tool_calls": [{"id": call_id, "type": "function", "function": {"name": tool_name, "arguments": arguments}}]}
            finish_reason = "tool_calls"
        else:
            message = {"role": "assistant", "content": json.dumps({"kind": "deterministic-local-stub", "compatibility_only": True, "tool_result_received": bool(tool_messages), "root_cause": None, "limitations": ["No external model inference or diagnostic evaluation was performed."]})}
            finish_reason = "stop"
        response = {"id": "chatcmpl-w06-local-stub", "object": "chat.completion", "created": int(time.time()), "model": "gpt-4.1", "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}], "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}
        receipt = {"received_at_unix": time.time(), "path": self.path, "kind": "deterministic-local-stub", "token_usage_kind": "fixture_zero_not_measured", "request": request, "response": response}
        with self.server.receipt_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(receipt, ensure_ascii=False) + "\n")
        self._reply(200, response)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8842)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), StubHandler)
    server.receipt_path = args.receipt
    server.serve_forever()


if __name__ == "__main__":
    main()
