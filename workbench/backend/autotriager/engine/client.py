"""POST-capable loopback client around the existing exact upstream SSE parser."""
from __future__ import annotations

import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from autotriager.adapters.holmes import HolmesStream, ProtocolError
from .safe_config import MODEL_KEY, require_loopback_url


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ProtocolError("redirects are forbidden at the internal engine boundary")


class HolmesClient:
    def __init__(self, base_url: str, allowed_models=(MODEL_KEY,), timeout: float = 60):
        self.base_url = require_loopback_url(base_url)
        self.allowed_models = frozenset(allowed_models)
        self.timeout = timeout
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def get_json(self, path: str):
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("relative API path required")
        with self.opener.open(self.base_url + path, timeout=self.timeout) as response:
            return json.load(response)

    def model_registry(self) -> list[str]:
        payload = self.get_json("/api/model")
        value = payload.get("model_name")
        if not isinstance(value, str):
            raise ProtocolError("pinned upstream model_name must be a JSON encoded string")
        models = json.loads(value)
        if not isinstance(models, list) or any(not isinstance(item, str) for item in models):
            raise ProtocolError("invalid upstream model registry")
        return models

    def chat(self, *, model: str = MODEL_KEY, ask: str, raw_path: Path, conversation_history: list | None = None,
             conversation_id: str = "w06-local-compatibility", request_source: str = "autotriager_w06_local_stub"):
        # Fail before any dispatch; upstream itself can silently fall back.
        if model not in self.allowed_models:
            raise ProtocolError("unknown model key")
        if model not in self.model_registry():
            raise ProtocolError("allowed model missing from actual upstream registry")
        if any(not isinstance(value, str) or not 1 <= len(value) <= 128 for value in (conversation_id, request_source)):
            raise ProtocolError("bounded server-owned trace identities required")
        body = {"ask": ask, "stream": True, "model": model, "enable_tool_approval": False, "conversation_id": conversation_id, "request_source": request_source}
        if conversation_history is not None:
            if not conversation_history or conversation_history[0].get("role") != "system":
                raise ProtocolError("server-owned history must start with system")
            body["conversation_history"] = conversation_history
        request = Request(self.base_url + "/api/chat", data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "Accept": "text/event-stream"}, method="POST")
        stream = HolmesStream()
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.opener.open(request, timeout=self.timeout) as response, raw_path.open("wb") as raw:
                if not response.headers.get("Content-Type", "").startswith("text/event-stream"):
                    raise ProtocolError("expected actual upstream SSE content type")
                while chunk := response.read(2048):
                    raw.write(chunk)
                    raw.flush()
                    stream.feed(chunk)
                return stream.finish(response.status)
        except HTTPError as exc:
            raw_path.write_bytes(exc.read())
            raise
