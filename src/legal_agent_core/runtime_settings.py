"""Runtime-editable LLM gateway settings (workspace settings panel).

The graph-guide chat and the LLM-backed tools need an OpenAI-compatible
gateway. Historically that came only from environment variables; this module
lets the operator set it from the workspace UI instead, persisted to a small
JSON file. Environment variables remain the fallback when no file exists, so
existing deployments keep working unchanged.

Secrets: the API key is written to a 0600 file and never returned in full —
``public()`` exposes only a masked hint.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: AvalAI is the default gateway; both models are OpenAI-compatible chat models.
DEFAULT_BASE_URL = "https://api.avalai.ir/v1"
DEFAULT_MODEL = "deepseek-v4.1-flash"
AVAILABLE_MODELS = ("deepseek-v4.1-flash", "glm-5.3-flash")


@dataclass(frozen=True, slots=True)
class LLMConfig:
    base_url: str
    model: str
    api_key: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.base_url and self.model)

    def public(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url,
            "model": self.model,
            "models": list(AVAILABLE_MODELS),
            "api_key_set": bool(self.api_key),
            "api_key_hint": _mask(self.api_key),
            "configured": self.configured,
            "default_base_url": DEFAULT_BASE_URL,
            "default_model": DEFAULT_MODEL,
        }


def _mask(key: str | None) -> str | None:
    if not key:
        return None
    if len(key) <= 8:
        return "•••"
    return f"{key[:4]}…{key[-4:]}"


class LLMSettingsStore:
    """Reads/writes the persisted gateway config; env vars are the fallback."""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path) if path else None

    def load(self) -> LLMConfig:
        return self._read_file() or self._from_env()

    def save(self, config: LLMConfig) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "base_url": config.base_url,
            "model": config.model,
            "api_key": config.api_key,
        }
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temporary, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:  # pragma: no cover — best-effort on odd filesystems
            pass

    def _from_env(self) -> LLMConfig:
        return LLMConfig(
            base_url=os.environ.get("LEGAL_AGENT_LLM_BASE_URL") or DEFAULT_BASE_URL,
            model=os.environ.get("LEGAL_AGENT_LLM_MODEL") or DEFAULT_MODEL,
            api_key=os.environ.get("LEGAL_AGENT_LLM_API_KEY") or None,
        )

    def _read_file(self) -> LLMConfig | None:
        if self.path is None or not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        api_key = data.get("api_key")
        return LLMConfig(
            base_url=str(data.get("base_url") or DEFAULT_BASE_URL),
            model=str(data.get("model") or DEFAULT_MODEL),
            api_key=str(api_key) if api_key else None,
        )


def default_settings_path() -> str:
    return os.environ.get("LEGAL_AGENT_LLM_SETTINGS_PATH", "out/llm_settings.json")
