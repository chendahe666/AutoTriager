"""Build an explicit, credential-free environment for a local compatibility run."""
from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

SOURCE_COMMIT = "869054f0058604109325bdc9e9d0d3d27ff883bd"
MODEL_KEY = "autotriager-local-stub"
TOOLS = ("incident_manifest", "incident_services", "incident_metrics", "incident_logs", "incident_traces")
# Enumerated from load_builtin_toolsets(None) at SOURCE_COMMIT. Some Python
# constructors enable tools independently of ENABLED_BY_DEFAULT_TOOLSETS.
BUILTIN_TOOLSETS = (
    "aks/node-health", "aks/core", "argocd/core", "cilium/core", "hubble/observability",
    "crossplane/core", "docker/core", "helm/core", "inspektor-gadget/node", "inspektor-gadget/tcpdump",
    "kubernetes/core", "kubernetes/live-metrics", "kubernetes/kube-prometheus-stack", "kubevela/core",
    "openshift/core", "openshift/logs", "openshift/live-metrics", "openshift/security", "slab",
    "core_investigation", "internet", "connectivity_check", "robusta", "grafana/loki", "grafana/tempo",
    "newrelic", "grafana/dashboards", "notion", "kafka/admin", "datadog/logs", "datadog/general",
    "datadog/metrics", "datadog/traces", "opensearch/query_assist", "coralogix", "rabbitmq/core", "bash",
    "confluence", "MongoDBAtlas", "skills", "servicenow/tables", "victorialogs", "database/sql",
    "elasticsearch/data", "elasticsearch/cluster", "prometheus/metrics", "prometheus/multi-tenant-metrics",
    "kubernetes/logs",
)


def require_loopback_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme != "http" or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("expected plain loopback HTTP URL without credentials/query/fragment")
    try:
        allowed = ipaddress.ip_address(parsed.hostname or "").is_loopback
    except ValueError:
        allowed = False
    if not allowed or not parsed.port:
        raise ValueError("only explicit numeric loopback hosts with a port are permitted")
    return url.rstrip("/")


def runtime_environment(root: Path, *, port: int, gateway_token: str = "") -> dict[str, str]:
    """Allowlist instead of inheriting provider, proxy, cloud or kube credentials.

    This also excludes dotenv files, host HOME config, Supabase, Robusta,
    OpenTelemetry and Braintrust settings. Tokens are local capabilities only.
    """
    runtime = root / ".runtime" / "holmes"
    for name in ("tmp", "home", "config", "token-cache", "guard"):
        (runtime / name).mkdir(parents=True, exist_ok=True)
    env = {key: os.environ[key] for key in ("SystemRoot", "WINDIR", "COMSPEC", "SYSTEMDRIVE") if key in os.environ}
    env.update({
        "PATH": str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"),
        "TEMP": str(runtime / "tmp"), "TMP": str(runtime / "tmp"),
        "USERPROFILE": str(runtime / "home"), "APPDATA": str(runtime / "home"),
        "LOCALAPPDATA": str(runtime / "home"), "PYTHONNOUSERSITE": "1", "PYTHONUTF8": "1",
        "PYTHONUNBUFFERED": "1", "HOLMES_CONFIGPATH_DIR": str(runtime / "config"),
        "HOLMES_HOST": "127.0.0.1", "HOLMES_PORT": str(port),
        "MODEL_LIST_FILE_LOCATION": str(runtime / "config" / "model_list.yaml"),
        "ENABLED_BY_DEFAULT_TOOLSETS": "", "ENABLE_TELEMETRY": "false",
        "SENTRY_DSN": "", "ENABLE_CONVERSATION_WORKER": "false", "ENABLED_SCHEDULED_PROMPTS": "false",
        "ROBUSTA_AI": "false", "LOAD_ALL_ROBUSTA_MODELS": "false", "LOAD_CONFIG_FROM_ENV": "false",
        "TOOLSET_STATUS_REFRESH_INTERVAL_SECONDS": "0", "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        "OVERRIDE_MAX_OUTPUT_TOKEN": "1024", "OVERRIDE_MAX_CONTENT_SIZE": "32768",
        "LITELLM_TELEMETRY": "False", "DO_NOT_TRACK": "1", "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1", "TIKTOKEN_CACHE_DIR": str(runtime / "token-cache"),
        "KUBECONFIG": str(runtime / "home" / "absent-kubeconfig"),
        "AWS_SHARED_CREDENTIALS_FILE": str(runtime / "home" / "absent-aws-credentials"),
        "AWS_CONFIG_FILE": str(runtime / "home" / "absent-aws-config"), "AWS_EC2_METADATA_DISABLED": "true",
        "AUTOTRIAGER_GATEWAY_TOKEN": gateway_token,
        "AUTOTRIAGER_EGRESS_LOG": str(runtime / "egress-denied.jsonl"),
        "PYTHONPATH": os.pathsep.join((str(runtime / "guard"), str(root / ".vendor" / "holmesgpt"), str(root / "backend"))),
    })
    return env


def write_configuration(root: Path, *, provider_url: str, gateway_url: str | None = None) -> dict:
    provider_url = require_loopback_url(provider_url)
    if gateway_url:
        gateway_url = require_loopback_url(gateway_url)
    runtime = root / ".runtime" / "holmes"
    config_dir = runtime / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    # JSON is also valid YAML; no user-controlled executable YAML constructors.
    models = {MODEL_KEY: {"model": "openai/gpt-4.1", "api_key": "local-stub-not-a-secret", "api_base": provider_url + "/v1", "max_tokens": 1024, "custom_args": {"max_context_size": 32768}}}
    config = {"model": MODEL_KEY, "max_steps": 4, "toolsets": {name: {"enabled": False} for name in BUILTIN_TOOLSETS}, "custom_toolsets": [], "skill_repos": [], "custom_skill_paths": []}
    if gateway_url:
        config["mcp_servers"] = {"autotriager-readonly": {
            "enabled": True, "description": "Bounded authorized incident evidence; read only.",
            "config": {"url": gateway_url + "/mcp", "mode": "streamable-http", "headers": {"Authorization": "Bearer {{ env.AUTOTRIAGER_GATEWAY_TOKEN }}"}, "icon_url": ""},
            "allowed_tools": list(TOOLS), "llm_instructions": "Preserve evidence IDs. This local deterministic stub run is a transport compatibility test, not a diagnosis.",
        }}
    (config_dir / "model_list.yaml").write_text(json.dumps(models, indent=2), encoding="utf-8")
    (config_dir / "config.yaml").write_text(json.dumps(config, indent=2), encoding="utf-8")
    # Runtime boundary only: official source remains byte-for-byte unchanged.
    guard = '''"""Local compatibility harness: reject all non-loopback socket connects."""
import ipaddress, json, os, socket
_connect = socket.socket.connect
_connect_ex = socket.socket.connect_ex
_getaddrinfo = socket.getaddrinfo
def _check(address):
    if not isinstance(address, tuple):
        return
    host = address[0]
    try:
        allowed = ipaddress.ip_address(host).is_loopback
    except ValueError:
        allowed = host == "localhost"
    if not allowed:
        with open(os.environ["AUTOTRIAGER_EGRESS_LOG"], "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"blocked_host": str(host), "port": address[1]}) + "\\n")
        raise PermissionError("W06 compatibility runtime permits loopback sockets only")
def connect(self, address):
    _check(address)
    return _connect(self, address)
def connect_ex(self, address):
    _check(address)
    return _connect_ex(self, address)
def getaddrinfo(host, port, *args, **kwargs):
    if host is not None:
        _check((host, port))
    return _getaddrinfo(host, port, *args, **kwargs)
socket.socket.connect = connect
socket.socket.connect_ex = connect_ex
socket.getaddrinfo = getaddrinfo
'''
    (runtime / "guard").mkdir(exist_ok=True)
    (runtime / "guard" / "sitecustomize.py").write_text(guard, encoding="utf-8")
    return {"model_key": MODEL_KEY, "provider": "deterministic-local-stub", "tools": list(TOOLS) if gateway_url else [], "source_commit": SOURCE_COMMIT}
