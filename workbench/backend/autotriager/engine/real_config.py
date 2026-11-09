"""W07-only config: official Holmes sees a private loopback provider token."""
from __future__ import annotations

import json
from pathlib import Path
from .safe_config import runtime_environment, write_configuration
from .provider_gate import MODEL

MODEL_KEY = "autotriager-gemini-qualified"


def configuration(root: Path, runtime: Path, *, holmes_port: int, provider_port: int, gateway_port: int, provider_token: str, gateway_token: str):
    """Create configuration under a separate root, keeping W06 files unchanged."""
    # Reuse tested safe generator with a private virtual root; only config is
    # copied into runtime. Upstream source and Python environment stay pinned.
    virtual = runtime / "configuration-root"
    env = runtime_environment(virtual, port=holmes_port, gateway_token=gateway_token)
    write_configuration(virtual, provider_url=f"http://127.0.0.1:{provider_port}", gateway_url=f"http://127.0.0.1:{gateway_port}")
    generated = virtual / ".runtime/holmes"
    config_path = generated / "config"
    config = json.loads((config_path / "config.yaml").read_text())
    config["model"], config["max_steps"] = MODEL_KEY, 6
    (config_path / "config.yaml").write_text(json.dumps(config, indent=2), encoding="utf-8")
    # Upstream context window includes its reserved output. The owner's input
    # ceiling remains 8000, enforced exactly by ProviderGate; total is 9024.
    models = {MODEL_KEY: {"model": "openai/" + MODEL, "api_key": "{{ env.AUTOTRIAGER_PROVIDER_TOKEN }}", "api_base": f"http://127.0.0.1:{provider_port}/v1", "max_tokens": 1024, "num_retries": 0, "max_retries": 0, "custom_args": {"max_context_size": 9024}}}
    (config_path / "model_list.yaml").write_text(json.dumps(models, indent=2), encoding="utf-8")
    env.update({"AUTOTRIAGER_PROVIDER_TOKEN": provider_token, "OVERRIDE_MAX_CONTENT_SIZE": "9024", "OVERRIDE_MAX_OUTPUT_TOKEN": "1024", "LLM_RATE_LIMIT_MAX_WAIT_SECONDS": "0", "LLM_REQUEST_TIMEOUT": "75", "AUTOTRIAGER_EGRESS_LOG": str(runtime / "holmes-egress-denied.jsonl")})
    # Official component control: build_system_prompt returns None, preserving
    # the explicit application-owned historical/read-only system history.
    env["ENABLED_PROMPTS"] = "none"
    # Tokenizer cache is immutable public setup data shared with W06.
    env["TIKTOKEN_CACHE_DIR"] = str(root / ".runtime/holmes/token-cache")
    import os
    env["PYTHONPATH"] = os.pathsep.join((str(generated / "guard"), str(root / ".vendor/holmesgpt"), str(root / "backend")))
    return env, config_path
