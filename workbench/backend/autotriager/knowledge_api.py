"""Owned knowledge routes; server retains session/origin/CSRF enforcement."""
import re
from .errors import APIError

ID = r"([A-Za-z0-9_-]{1,96})"


def _service(handler):
    service = getattr(handler.server.app, "memory", None)
    if service is None:
        raise APIError(409, "KNOWLEDGE_DISABLED", "Reviewed incident memory is not enabled for this profile.")
    return service


def handle_get(handler, path, query, actor):
    if path == "/api/v1/knowledge":
        if query:
            raise APIError(400, "INVALID_REQUEST", "Knowledge lists the latest 100 entries without query parameters.")
        service = getattr(handler.server.app, "memory", None)
        value = service.list_entries(actor) if service else {"enabled": False, "items": [], "truncated": False, "counts": {}, "reason": "Reviewed incident memory is not enabled."}
        conversation = getattr(handler.server.app, 'conversation', None)
        value['answer_injection_enabled'] = service is not None and getattr(conversation, 'context_enricher', None) is not None
        handler._json(200, value)
        return True
    if match := re.fullmatch("/api/v1/knowledge/" + ID, path):
        if query:
            raise APIError(400, "INVALID_REQUEST", "A knowledge entry takes no query parameters.")
        handler._json(200, {"entry": _service(handler).get_entry(actor, match[1])})
        return True
    return False


def handle_post(handler, path, actor):
    operation = None
    if path == "/api/v1/knowledge/capture":
        service, body = _service(handler), handler._body()
        if set(body) != {"run_id"} or not isinstance(body["run_id"], str):
            raise APIError(400, "INVALID_REQUEST", "Supply only one recorded run_id.")
        operation = lambda: {"entry": service.capture_run(actor, body["run_id"], automatic=False)}
    elif match := re.fullmatch("/api/v1/knowledge/" + ID + "/(review|publish|revoke)", path):
        service, body = _service(handler), handler._body()
        action, memory_id = match[2], match[1]
        field = "note" if action == "review" else "reason"
        if set(body) != {"expected_revision", field} or type(body["expected_revision"]) is not int:
            raise APIError(400, "INVALID_REQUEST", "Supply the current revision and an explicit decision note or reason.")
        method = getattr(service, action)
        operation = lambda: {"entry": method(actor, memory_id, body["expected_revision"], body[field])}
    if operation is None:
        return False
    response = service.action(actor, path, handler._idempotency(), body, operation)
    handler._json(200, response)
    return True
