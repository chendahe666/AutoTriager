"""Pure parser for the Holmes source-audited SSE boundary.

Reference: 869054f0058604109325bdc9e9d0d3d27ff883bd. No Holmes package,
transport, credentials, provider invocation or causal-truth validation exists here.
Raw optional fields are retained for storage by the caller; never render them as HTML.
"""
from __future__ import annotations

import codecs
import copy
import json
from dataclasses import dataclass
from typing import Any, Iterable

AUDITED_COMMIT = "869054f0058604109325bdc9e9d0d3d27ff883bd"


class ProtocolError(ValueError):
    """Malformed or forbidden upstream/configuration input."""


@dataclass(frozen=True)
class SSEEvent:
    event: str
    data: str
    event_id: str | None = None


class SSEDecoder:
    """Bounded incremental UTF-8 SSE decoder; a transport chunk is not a frame.

    Multiple data lines join with a newline. CR, LF and CRLF are supported even
    when CRLF or a multibyte character is split across transport chunks. An EOF
    does not dispatch an unterminated frame (the SSE blank line is required).
    """

    def __init__(self, max_frame_bytes: int = 2_000_000):
        if max_frame_bytes <= 0:
            raise ValueError("max_frame_bytes must be positive")
        self.max_frame_bytes = max_frame_bytes
        self._utf8 = codecs.getincrementaldecoder("utf-8-sig")("strict")
        self._buffer = ""
        self._data: list[str] = []
        self._event = "message"
        self._id: str | None = None
        self._frame_size = 0
        self._closed = False

    @property
    def pending(self) -> bool:
        # SSE comments (heartbeats) at EOF are not unfinished data frames.
        leftover = self._buffer and not self._buffer.startswith(":")
        return bool(leftover or self._data or self._event != "message" or self._id is not None)

    def feed(self, chunk: bytes) -> list[SSEEvent]:
        if self._closed:
            raise ProtocolError("decoder already finished")
        if not isinstance(chunk, bytes):
            raise TypeError("SSE transport chunks must be bytes")
        try:
            self._buffer += self._utf8.decode(chunk)
        except UnicodeDecodeError as exc:
            raise ProtocolError("invalid SSE UTF-8") from exc
        return self._drain(final=False)

    def finish(self) -> list[SSEEvent]:
        if self._closed:
            raise ProtocolError("decoder already finished")
        self._closed = True
        try:
            self._buffer += self._utf8.decode(b"", final=True)
        except UnicodeDecodeError as exc:
            raise ProtocolError("truncated SSE UTF-8") from exc
        return self._drain(final=True)

    def _drain(self, final: bool) -> list[SSEEvent]:
        output = []
        while True:
            positions = [p for p in (self._buffer.find("\n"), self._buffer.find("\r")) if p >= 0]
            if not positions:
                if len(self._buffer.encode("utf-8")) + self._frame_size > self.max_frame_bytes:
                    raise ProtocolError("SSE frame exceeds byte limit")
                return output
            p = min(positions)
            if self._buffer[p] == "\r" and p == len(self._buffer) - 1 and not final:
                return output
            end = p + (2 if self._buffer[p:p + 2] == "\r\n" else 1)
            line, self._buffer = self._buffer[:p], self._buffer[end:]
            self._frame_size += len(line.encode("utf-8")) + 1
            if self._frame_size > self.max_frame_bytes:
                raise ProtocolError("SSE frame exceeds byte limit")
            if not line:
                if self._data:
                    output.append(SSEEvent(self._event, "\n".join(self._data), self._id))
                self._data, self._event, self._id, self._frame_size = [], "message", None, 0
            elif not line.startswith(":"):
                key, separator, value = line.partition(":")
                if separator and value.startswith(" "):
                    value = value[1:]
                if key == "data":
                    self._data.append(value)
                elif key == "event":
                    self._event = value
                elif key == "id" and "\x00" not in value:
                    self._id = value


def _object(value: Any, field_name: str) -> dict:
    if not isinstance(value, dict):
        raise ProtocolError(f"{field_name} must be an object")
    return value


def _string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ProtocolError(f"{field_name} must be a nonempty string")
    return value


def _json_payload(raw: str) -> dict:
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ProtocolError("duplicate JSON key in upstream event")
            result[key] = value
        return result

    def reject_constant(_):
        raise ProtocolError("non-finite JSON in upstream event")

    return _object(json.loads(raw, object_pairs_hook=unique_object,
                              parse_constant=reject_constant), "event data")


def validate_dispatch_config(
    config: dict, allowed_models: Iterable[str], allowed_policies: Iterable[str]
) -> dict:
    """Validate server-owned selection before a caller could dispatch anything.

    This helper only admits exact registry/policy keys and the audited protocol
    identity. It grants no inference permission and constructs no HTTP request.
    """
    _object(config, "config")
    if set(config) != {"model", "policy", "engine_commit"}:
        raise ProtocolError("config must contain only model, policy, engine_commit")
    model = _string(config["model"], "model")
    policy = _string(config["policy"], "policy")
    if model not in frozenset(allowed_models):
        raise ProtocolError("unknown model key")
    if policy not in frozenset(allowed_policies):
        raise ProtocolError("unknown policy key")
    if config["engine_commit"] != AUDITED_COMMIT:
        raise ProtocolError("unsupported engine protocol identity")
    return copy.deepcopy(config)


@dataclass
class ToolCall:
    call_id: str
    tool_name: str
    start: dict | None = None
    result: dict | None = None


@dataclass(frozen=True)
class NormalizedEvent:
    event_type: str
    upstream_call_id: str | None
    payload: dict
    raw_event: SSEEvent


@dataclass(frozen=True)
class StreamResult:
    status: str  # candidate_received, incomplete, failed; never "verified"
    analysis: str | None
    conversation_history: list | None
    tools: dict[str, ToolCall]
    events: tuple[NormalizedEvent, ...]
    errors: tuple[str, ...]


class HolmesStream:
    """Collect raw candidate analysis and a separate tool ledger by call ID.

    Duplicate identical start/result events are tolerated, conflicting duplicates
    fail closed. Results may arrive before starts and complete out of order.
    Unknown optional metadata is preserved; unknown tool/approval controls fail closed.
    Unexpected approval/frontend pause fails closed for the read-only profile.
    """

    def __init__(self, max_frame_bytes: int = 2_000_000):
        self.decoder = SSEDecoder(max_frame_bytes)
        self.tools: dict[str, ToolCall] = {}
        self.events: list[NormalizedEvent] = []
        self.errors: list[str] = []
        self.analysis: str | None = None
        self.history: list | None = None
        self._answer: dict | None = None
        self._closed = False

    def feed(self, chunk: bytes) -> list[NormalizedEvent]:
        if self._closed:
            raise ProtocolError("stream already finished")
        before = len(self.events)
        try:
            for event in self.decoder.feed(chunk):
                self._accept(event)
        except ProtocolError as exc:
            self.errors.append(str(exc))
        return self.events[before:]

    def _accept(self, event: SSEEvent) -> None:
        try:
            payload = _json_payload(event.data)
        except (json.JSONDecodeError, ProtocolError) as exc:
            self.events.append(NormalizedEvent("protocol.invalid", None, {"raw_data": event.data}, event))
            self.errors.append(str(exc))
            return
        call_id = None
        normalized = {
            "start_tool_calling": "tool.started", "tool_calling_result": "tool.finished",
            "ai_message": "assistant.progress", "token_count": "usage.observed",
            "conversation_history_compaction_start": "context.compaction_started",
            "conversation_history_compacted": "context.compacted",
            "ai_answer_end": "candidate.answer_received", "error": "run.failed",
            "approval_required": "run.forbidden_approval",
        }.get(event.event, "upstream.unknown")
        try:
            if event.event in {"start_tool_calling", "tool_calling_result"}:
                key = "id" if event.event == "start_tool_calling" else "tool_call_id"
                call_id = _string(payload.get(key), key)
                name = _string(payload.get("tool_name"), "tool_name")
                call = self.tools.setdefault(call_id, ToolCall(call_id, name))
                if call.tool_name != name:
                    raise ProtocolError("tool name changed for call ID")
                attr = "start" if key == "id" else "result"
                if attr == "result":
                    _string(payload.get("name"), "name")
                    if not isinstance(payload.get("description"), str) or payload.get("role") != "tool":
                        raise ProtocolError("tool result requires description and tool role")
                    result = _object(payload.get("result"), "result")
                    status = _string(result.get("status"), "result.status")
                    if "data" not in result or (result["data"] is not None and not isinstance(result["data"], str)):
                        raise ProtocolError("tool result data must retain the upstream string or null")
                    if status in {"frontend_pause", "approval_required"}:
                        raise ProtocolError("approval/frontend pause is forbidden in read-only profile")
                    if status not in {"success", "error", "no_data"}:
                        raise ProtocolError("unknown tool result status")
                old = getattr(call, attr)
                if old is not None and old != payload:
                    raise ProtocolError("conflicting duplicate tool event")
                setattr(call, attr, copy.deepcopy(payload))
            elif event.event == "ai_answer_end":
                if not isinstance(payload.get("analysis"), str):
                    raise ProtocolError("analysis must be a raw string")
                if not isinstance(payload.get("conversation_history"), list):
                    raise ProtocolError("conversation_history must be a list")
                if self._answer is not None and self._answer != payload:
                    raise ProtocolError("conflicting terminal answers")
                self._answer = copy.deepcopy(payload)
                self.analysis = payload["analysis"]
                self.history = copy.deepcopy(payload["conversation_history"])
            elif event.event == "approval_required":
                raise ProtocolError("approval is forbidden in read-only profile")
            elif event.event == "error":
                self.errors.append("upstream error event")
            elif normalized == "upstream.unknown" and any(word in event.event.lower() for word in ("tool", "approval", "pause")):
                raise ProtocolError("unknown tool/approval control event")
        except ProtocolError as exc:
            self.errors.append(str(exc))
            normalized = "protocol.invalid"
        safe_payload = copy.deepcopy(payload)
        if event.event == "ai_message":
            # Retained in raw_event for audit, excluded from user-facing progress.
            safe_payload.pop("reasoning", None)
        self.events.append(NormalizedEvent(normalized, call_id, safe_payload, event))

    def finish(self, http_status: int = 200) -> StreamResult:
        if self._closed:
            raise ProtocolError("stream already finished")
        self._closed = True
        try:
            for event in self.decoder.finish():
                self._accept(event)
        except ProtocolError as exc:
            self.errors.append(str(exc))
        if http_status != 200:
            self.errors.append(f"upstream HTTP status {http_status}")
        incomplete = (self.decoder.pending or self._answer is None or
                      any(c.start is None or c.result is None for c in self.tools.values()))
        status = "failed" if self.errors else "incomplete" if incomplete else "candidate_received"
        return StreamResult(status, self.analysis, copy.deepcopy(self.history),
                            copy.deepcopy(self.tools), tuple(self.events), tuple(self.errors))
