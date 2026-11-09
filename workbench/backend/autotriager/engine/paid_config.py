"""Independent paid-pilot model alias; reuse the tested isolated engine setup."""
from __future__ import annotations

import json
from pathlib import Path

from .provider_gate import PAID_MODEL
from .real_config import configuration as isolated_configuration

MODEL_KEY = "autotriager-gemini-paid-pilot"


def configuration(root: Path, runtime: Path, *, holmes_port: int, provider_port: int,
                  gateway_port: int, provider_token: str, gateway_token: str):
    env, config_path = isolated_configuration(
        root, runtime, holmes_port=holmes_port, provider_port=provider_port,
        gateway_port=gateway_port, provider_token=provider_token, gateway_token=gateway_token)
    config = json.loads((config_path / "config.yaml").read_text(encoding="utf-8"))
    config["model"] = MODEL_KEY
    (config_path / "config.yaml").write_text(json.dumps(config, indent=2), encoding="utf-8")
    models = {MODEL_KEY: {"model": "openai/" + PAID_MODEL,
                        "api_key": "{{ env.AUTOTRIAGER_PROVIDER_TOKEN }}",
                        "api_base": f"http://127.0.0.1:{provider_port}/v1",
                        "max_tokens": 1024, "num_retries": 0, "max_retries": 0,
                        "custom_args": {"max_context_size": 9024}}}
    (config_path / "model_list.yaml").write_text(json.dumps(models, indent=2), encoding="utf-8")
    return env, config_path
