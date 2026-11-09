"""Additive HTTP routing hooks; legacy session/CSRF checks stay in server.py."""
import re
from .errors import APIError

ID = r"([A-Za-z0-9_-]{1,96})"
BASE = "/api/v1/investigations/" + ID + "/conversation"


def _service(handler):
    service = getattr(handler.server.app, "conversation", None)
    if service is None:
        raise APIError(409, "CONVERSATION_DISABLED", "Conversation is not enabled for this server profile.")
    return service


def handle_get(handler, path, query, actor):
    if path == "/api/v1/conversations":
        if query:
            raise APIError(400, "INVALID_REQUEST", "The conversations index returns the latest 100 threads without query parameters.")
        service = getattr(handler.server.app, "conversation", None)
        value = service.list_threads(actor) if service else {"enabled": False, "threads": [], "truncated": False}
        handler._json(200, value)
        return True
    if path == "/api/v1/conversation-capabilities":
        service = getattr(handler.server.app, "conversation", None)
        value = service.capabilities() if service else {
            "enabled": False, "model_id": None, "disabled_reason": "Conversation is not enabled for this server profile.",
            "max_question_chars": 4000, "max_history_turns": 12, "evidence_scope": "recorded_run",
            "history_scope": "same_context_run_completed_turns",
            "budget": {"max_calls": 0, "max_nominal_usd": 0, "used_calls": 0, "reserved_nominal_usd": 0}}
        handler._json(200, value)
        return True
    if match := re.fullmatch(BASE, path):
        if query:
            raise APIError(400, "INVALID_REQUEST", "Conversation history returns the latest 100 turns without query parameters.")
        handler._json(200, _service(handler).history(actor, match[1]))
        return True
    if match := re.fullmatch(BASE + "/turns/" + ID, path):
        if query:
            raise APIError(400, "INVALID_REQUEST", "A conversation turn takes no query parameters.")
        handler._json(200, {"turn": _service(handler).get_turn(actor, match[1], match[2])})
        return True
    return False


def handle_post(handler, path, actor):
    if match := re.fullmatch(BASE + "/turns", path):
        turn, replayed = _service(handler).create_turn(actor, match[1], handler._body(), handler._idempotency())
        handler._json(202, {"turn": turn})
        return True
    return False
