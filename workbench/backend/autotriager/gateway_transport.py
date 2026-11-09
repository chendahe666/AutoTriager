"""Private loopback HTTP and stateless MCP StreamableHTTP transport.

MCP result text is the exact canonical ToolReply charged by Gateway. JSON-RPC
framing is protocol overhead, not source observation bytes. There are no model
IDs, run IDs, paths, command tools, resources or prompts in the tool surface.
"""
from __future__ import annotations

import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator, FormatChecker

from .gateway import ARGUMENTS, GatewayDenied, TOOLS, canonical

MAX_REQUEST_BYTES = 131072


def tool_schemas(gateway, names=TOOLS):
    defs = gateway.store.schema["$defs"]
    props = defs["ToolQuery"]["properties"]
    schemas = []
    for tool in TOOLS:
        if tool not in names:
            continue
        parameters = next(branch["then"]["properties"]["parameters"] for branch in defs["ToolQuery"]["allOf"]
                          if branch["if"]["properties"]["tool_name"]["const"] == tool)
        schema = {"type": "object", "properties": {key: copy.deepcopy(props[key]) for key in ARGUMENTS if key != "parameters"},
                  "additionalProperties": False}
        schema["properties"]["parameters"] = copy.deepcopy(parameters)
        schema["properties"]["time_window"] = copy.deepcopy(defs["TimeWindow"])
        if tool == "incident_manifest":
            schema["properties"] = {}
            description = "Call with {} to retrieve this run's authorized recorded incident, exact time_window, service_ids and coverage. No time or service arguments are accepted. Recorded incident dates can differ from the current wall clock."
        elif tool == "incident_services":
            schema["properties"] = {key: schema["properties"][key] for key in ("limit", "max_bytes")}
            description = "Retrieve this run's service catalog. Call with {} or optional limit/max_bytes only; no time, service or parameters arguments are accepted."
        else:
            description = ("Read authorized recorded " + tool.removeprefix("incident_")
                + ". Omit time_window and service_ids to use the bound incident interval and all its services. Never infer incident dates from the current wall clock. If supplied, use only dates and service names returned by incident_manifest. Empty service_ids is rejected; omit the field to select all. Missing/truncated data is not evidence of health.")
            schema["properties"]["time_window"]["description"] = "Optional half-open UTC interval wholly inside incident_manifest.time_window. Omit to use that recorded interval; do not use current dates."
            schema["properties"]["service_ids"].update(minItems=1,
                description="Optional nonempty subset of incident_manifest.service_ids. Omit for all authorized services. [] is invalid at this MCP boundary.")
            if tool == "incident_metrics":
                schema["properties"]["parameters"]["description"] = "Optional closed metric filters. aggregation supports only none, mean, max, p95; sum is unsupported. Omit metric_names to sample available source metrics; never guess generic names."
                schema["properties"]["parameters"]["properties"]["metric_names"]["description"] = "Exact source metric names obtained from admitted records; omit when the names are unknown."
                schema["properties"]["parameters"]["properties"]["metric_names"]["minItems"] = 1
                schema["properties"]["parameters"]["properties"]["aggregation"]["description"] = "none returns raw samples. mean/max/p95 require complete parent records; sum is invalid."
            elif tool == "incident_logs":
                schema["properties"]["parameters"]["description"] = "Optional closed filters: contains is a case-sensitive literal substring; severity is an exact normalized enum, not inferred from text."
            else:
                schema["properties"]["parameters"]["description"] = "Optional closed filters: trace_id and exact status any/ok/error. Unknown source status codes are never inferred to be ok."
        schemas.append({"name": tool, "description": description,
            "inputSchema": schema, "annotations": {"readOnlyHint": True, "destructiveHint": False,
                "idempotentHint": False, "openWorldHint": False}})
    return schemas


def query_rejection_reason(tool, arguments, schema):
    """Validate the advertised surface without returning authorized scope data."""
    errors = sorted(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(arguments), key=lambda error: str(error.path))
    if not errors:
        return None
    if tool == "incident_manifest":
        return "incident_manifest accepts {} only. Omit time_window, service_ids and parameters to discover the bound recorded incident."
    if tool == "incident_services":
        return "incident_services accepts only optional limit and max_bytes. Omit time_window, service_ids and parameters."
    if isinstance(arguments, dict) and arguments.get("service_ids") == []:
        return "service_ids must be nonempty when supplied. Omit it to select all bound services, or use names from incident_manifest."
    if tool == "incident_metrics" and isinstance(arguments, dict):
        parameters = arguments.get("parameters")
        if isinstance(parameters, dict) and parameters.get("metric_names") == []:
            return "metric_names must be nonempty when supplied. Omit the filter to sample available source metrics; do not guess names. No observations were returned."
        if isinstance(parameters, dict) and parameters.get("aggregation", "none") not in ("none", "mean", "max", "p95"):
            return "Metric aggregation supports only none, mean, max and p95. sum is invalid; no observations were returned."
    return "Arguments do not match this tool's closed schema. Use the advertised optional filters and bounds; no observations were returned."


def make_server(gateway, host="127.0.0.1", port=8843):
    if host != "127.0.0.1":
        raise ValueError("The private query gateway binds only IPv4 loopback.")

    class Handler(BaseHTTPRequestHandler):
        server_version = "AutoTriagerGateway/1"

        def log_message(self, *args):
            # Bearers and telemetry never enter a default request logger.
            pass

        def _send(self, status, value=None, *, raw=None):
            body = b"" if value is None and raw is None else canonical(value) if raw is None else raw
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _local(self):
            # The engine client has no browser Origin. Reject browser requests
            # and DNS-rebinding Host values even on a loopback socket.
            allowed = {"127.0.0.1", "localhost"}
            try:
                hostname = urlsplit("//" + self.headers.get("Host", "")).hostname
            except ValueError:
                hostname = None
            return hostname in allowed and not self.headers.get("Origin")

        def _token(self):
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Bearer "):
                raise GatewayDenied("scope_denied")
            return auth[7:]

        def do_GET(self):
            if not self._local():
                return self._send(403, {"code": "scope_denied"})
            if self.path == "/health":
                return self._send(200, {"status": "ready", "mode": "readonly", "transport": "stateless-mcp-json"})
            return self._send(405, {"code": "method_not_allowed"})

        def do_DELETE(self):
            self._send(405, {"code": "method_not_allowed"})

        def do_POST(self):
            if not self._local():
                return self._send(403, {"code": "scope_denied"})
            if self.path != "/mcp" and self.path not in {"/tools/" + tool for tool in TOOLS}:
                return self._send(404, {"code": "not_found"})
            try:
                token = self._token()
                allowed = gateway.authorize(token)
                if self.headers.get("Transfer-Encoding"):
                    return self._send(400, {"code": "invalid_request"})
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_REQUEST_BYTES:
                    return self._send(413, {"code": "request_size"})
                if self.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json":
                    return self._send(415, {"code": "content_type"})
                payload = json.loads(self.rfile.read(length), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                if not isinstance(payload, dict):
                    return self._send(400, {"code": "invalid_request"})
                if self.path != "/mcp":
                    return self._send(200, raw=canonical(gateway.execute(token, self.path.removeprefix("/tools/"), payload)))
                self._mcp(token, allowed, payload)
            except GatewayDenied as exc:
                self._send(403 if exc.code == "scope_denied" else 409, {"code": exc.code})
            except (ValueError, UnicodeDecodeError, TypeError):
                self._send(400, {"code": "invalid_request"})
            except Exception:
                self._send(500, {"code": "gateway_error"})

        def _mcp(self, token, allowed, request):
            request_id = request.get("id")
            if request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
                return self._send(400, {"jsonrpc": "2.0", "id": request_id,
                    "error": {"code": -32600, "message": "Invalid request"}})
            method, params = request["method"], request.get("params", {})
            if "id" not in request:
                if method in ("notifications/initialized", "notifications/cancelled"):
                    return self._send(202)
                return self._send(400, {"code": "unsupported_notification"})
            result, error = None, None
            if not isinstance(params, dict):
                error = {"code": -32602, "message": "Invalid parameters"}
            elif method == "initialize":
                version = params.get("protocolVersion")
                result = {"protocolVersion": version if version in ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25") else "2025-03-26",
                    "capabilities": {"tools": {"listChanged": False}}, "serverInfo": {"name": "autotriager-readonly", "version": "1.0.0"}}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": tool_schemas(gateway, allowed)}
            elif method == "tools/call":
                if set(params) - {"name", "arguments", "_meta"} or params.get("name") not in allowed:
                    error = {"code": -32602, "message": "Tool outside the bound allowlist"}
                else:
                    arguments = params.get("arguments", {})
                    schema = tool_schemas(gateway, (params["name"],))[0]["inputSchema"]
                    rejected = query_rejection_reason(params["name"], arguments, schema)
                    if rejected:
                        error = {"code": -32602, "message": "Query rejected: " + rejected, "data": {"code": "scope_denied", "category": "query_rejected", "reason": rejected}}
                    else:
                        try:
                            reply = gateway.execute(token, params["name"], arguments)
                            result = {"content": [{"type": "text", "text": canonical(reply).decode("utf-8")}], "isError": False}
                        except GatewayDenied as exc:
                            reason = str(exc)
                            if reason == "Query window exceeds the bound incident.":
                                reason = "time_window must be inside incident_manifest.time_window. Omit it to use the bound recorded interval; do not use current wall-clock dates. No observations were returned."
                            elif reason == "Service selection exceeds the bound incident.":
                                reason = "Use only service IDs returned by incident_manifest, or omit service_ids for all bound services. No observations were returned."
                            elif reason not in ("A query needs a closed time window.", "A timestamp must be explicit timezone text.", "A timestamp is invalid.", "A timestamp requires an explicit timezone.", "Tool arguments do not match the closed query schema."):
                                reason = "Run admission was denied; no observations were returned."
                            error = {"code": -32001, "message": "Query rejected: " + reason,
                                     "data": {"code": exc.code, "reason": reason}}
            else:
                error = {"code": -32601, "message": "Method not found"}
            value = {"jsonrpc": "2.0", "id": request_id, "error" if error else "result": error or result}
            self._send(200, value)

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server
