"""Runtime-reconfigurable legal-research agent provider.

Owns the live :class:`~legal_agent_core.agentic.loop.ToolResearchLoop` over
:class:`~legal_agent_core.agentic.composite.CompositeTools` (canonical research
tools + FalkorDB graph navigation) and rebuilds it from the current LLM
settings. When no gateway is configured it falls back to the deterministic demo
planner so the workspace live trace keeps working offline.
"""

from __future__ import annotations

import logging
from typing import Any

from .runtime_settings import LLMConfig, LLMSettingsStore

logger = logging.getLogger(__name__)


def _provider_config(cfg: LLMConfig, *, timeout_seconds: float = 90.0) -> Any:
    from .llm import EndpointStyle, ProviderConfig

    return ProviderConfig(
        "research-agent-llm",
        cfg.base_url,
        EndpointStyle.CHAT_COMPLETIONS,
        api_key=cfg.api_key,
        timeout_seconds=timeout_seconds,
    )


def _extra_body(cfg: LLMConfig) -> dict[str, Any]:
    """Disable reasoning for gateways that honor it.

    Reasoning models (deepseek-v4.1-flash, glm-5.3-flash) spend the first
    output tokens on ``reasoning_content``; with the long research prompt this
    intermittently leaves ``message.content`` empty ("chat completion contains
    no text"). The planner only emits strict JSON, so thinking buys little.
    """
    base = (cfg.base_url or "").lower()
    if "avalai" in base or "z.ai" in base:
        return {"thinking": {"type": "disabled"}}
    return {}


def _profile(cfg: LLMConfig, *, max_output_tokens: int = 8192) -> Any:
    from .llm import LLMTask, ModelProfile

    return ModelProfile(
        profile_id="research-agent",
        version=1,
        task=LLMTask.NAVIGATION,
        provider_id="research-agent-llm",
        model=cfg.model,
        prompt_id="research-agent",
        prompt_version=1,
        max_output_tokens=max_output_tokens,
        temperature=0.0,
        extra_body=_extra_body(cfg),
    )


class ResearchAgentProvider:
    """Builds a fresh research loop per run from the current LLM config."""

    def __init__(
        self,
        canonical_tools: Any,
        verifier: Any,
        store: LLMSettingsStore,
        *,
        graph_tools: Any | None = None,
        config: Any | None = None,
    ) -> None:
        from .agentic.composite import CompositeTools

        self.canonical_tools = canonical_tools
        self.graph_tools = graph_tools
        self.verifier = verifier
        self.store = store
        self.agentic_config = config
        self._composite = CompositeTools(canonical_tools, graph_tools)

    # -------------------------------------------------------------- lifecycle

    @property
    def config(self) -> LLMConfig:
        return self.store.load()

    @property
    def available(self) -> bool:
        return bool(self.config.configured)

    @property
    def loop(self) -> Any:
        """A fresh loop bound to the current config (cheap; built per run)."""
        from .agentic.loop import ToolResearchLoop

        return ToolResearchLoop(
            self._composite,
            self._planner(self.config),
            self.verifier,
            self.agentic_config,
        )

    def _planner(self, cfg: LLMConfig) -> Any:
        if cfg.configured:
            try:
                from .adapters import OpenAICompatibleGateway
                from .agentic.planner import LLMToolPlanner

                return LLMToolPlanner(
                    OpenAICompatibleGateway(),
                    _provider_config(cfg),
                    _profile(cfg),
                )
            except Exception as exc:  # noqa: BLE001 — degrade to the demo planner
                logger.warning("research agent LLM planner unavailable: %s", exc)
        from .demo import _DemoToolPlanner

        return _DemoToolPlanner()


__all__ = ["ResearchAgentProvider"]
