"""Runtime-reconfigurable graph-guide chat provider.

Holds the current :class:`GraphChatService` and rebuilds it whenever the
workspace settings panel changes the LLM gateway configuration. Construction
never fails at boot: an unconfigured provider reports ``available=False`` and
the API returns a clear error until an API key is saved.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from typing import Any

from .errors import DomainError
from .runtime_settings import LLMConfig, LLMSettingsStore

logger = logging.getLogger(__name__)


def _build_provider_config(cfg: LLMConfig) -> Any:
    from .llm import EndpointStyle, ProviderConfig

    return ProviderConfig(
        "graph-chat-llm",
        cfg.base_url,
        EndpointStyle.CHAT_COMPLETIONS,
        api_key=cfg.api_key,
    )


def _build_profile(cfg: LLMConfig, *, max_output_tokens: int = 2048) -> Any:
    from .llm import LLMTask, ModelProfile

    return ModelProfile(
        profile_id="graph-chat",
        version=1,
        task=LLMTask.NAVIGATION,
        provider_id="graph-chat-llm",
        model=cfg.model,
        prompt_id="graph-chat",
        prompt_version=1,
        max_output_tokens=max_output_tokens,
        temperature=0.2,
    )


class GraphChatProvider:
    """Owns the live GraphChatService and the settings that build it."""

    def __init__(self, laws_graph: Any, canonical: Any, store: LLMSettingsStore) -> None:
        self.laws_graph = laws_graph
        self.canonical = canonical
        self.store = store
        self._service: Any | None = None
        self.reload()

    # -------------------------------------------------------------- lifecycle

    @property
    def config(self) -> LLMConfig:
        return self.store.load()

    @property
    def available(self) -> bool:
        return self._service is not None

    def reload(self) -> None:
        self._service = self._build(self.config)

    def _build(self, cfg: LLMConfig) -> Any | None:
        if self.laws_graph is None or not cfg.configured:
            return None
        try:
            from .adapters import OpenAICompatibleGateway
            from .adapters.postgres import PostgresCanonicalRepository
            from .agentic.graph_chat import GraphChatLoop, GraphChatService
            from .agentic.graph_tools import GraphTools
            from .agentic.planner import LLMToolPlanner
            from .law_text import fetch_provision_text

            planner = LLMToolPlanner(
                OpenAICompatibleGateway(),
                _build_provider_config(cfg),
                _build_profile(cfg),
            )
            provision_text = None
            if isinstance(self.canonical, PostgresCanonicalRepository):
                connection = self.canonical.connection

                def provision_text(provision_id: str) -> dict | None:
                    return fetch_provision_text(connection, provision_id)

            tools = GraphTools(self.laws_graph, provision_text=provision_text)
            return GraphChatService(GraphChatLoop(tools, planner))
        except Exception as exc:  # noqa: BLE001 — optional feature must never break boot
            logger.warning("graph chat build failed: %s", exc)
            return None

    # ---------------------------------------------------------------- runtime

    def stream(self, message: str, history: Sequence[dict[str, str]] = ()):
        if self._service is None:
            raise DomainError(
                "graph chat is not configured (set the LLM API key in تنظیمات)"
            )
        return self._service.stream(message, history)

    def probe(self) -> dict[str, Any]:
        """Make one tiny gateway call to verify the saved configuration."""
        cfg = self.config
        if not cfg.configured:
            raise DomainError("ابتدا کلید API را ذخیره کنید")
        from .adapters import OpenAICompatibleGateway
        from .llm import GenerationRequest, LLMTask, ModelProfile

        gateway = OpenAICompatibleGateway()
        profile = ModelProfile(
            profile_id="graph-chat-probe",
            version=1,
            task=LLMTask.NAVIGATION,
            provider_id="graph-chat-llm",
            model=cfg.model,
            prompt_id="graph-chat-probe",
            prompt_version=1,
            # Reasoning models spend the first tokens on reasoning_content;
            # a tiny budget leaves content empty ("no text"), so keep headroom.
            max_output_tokens=512,
            temperature=0.0,
        )
        started = time.monotonic()
        try:
            response = gateway.generate(
                _build_provider_config(cfg),
                profile,
                GenerationRequest(
                    "You are a connectivity probe.",
                    "پاسخ فقط با کلمهٔ ok.",
                ),
            )
        except DomainError as exc:
            return {"ok": False, "error": str(exc)[:300]}
        return {
            "ok": True,
            "model": response.model or cfg.model,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "sample": (response.text or "").strip()[:80],
        }
