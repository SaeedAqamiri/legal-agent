"""LLM-assisted query expansion for lexical dead-ends.

A retrieval *aid*, not evidence: the suggested terms are returned to the
planner, which still must run deterministic tools and cite canonical spans.
"""

from __future__ import annotations

import json
from typing import Protocol

from ..errors import DomainError
from ..llm import GenerationRequest, LLMGateway, ModelProfile, ProviderConfig

_PROMPT = (
    "شما مولد کلیدواژه برای جست‌وجو در متن قانون‌ها و بخشنامه‌های فارسی هستید.\n"
    "برای پرسش حقوقی زیر، حداکثر ۵ کوئری جایگزین بنویسید که با واژگان رسمی/حقوقی "
    "و مترادف‌های رایجِ متون قانونی جست‌وجو می‌کنند (نه واژه‌های روایی پرسش).\n"
    "فقط یک شیء JSON برگردانید: {\"queries\": [\"...\", \"...\"]}"
)


class TermExpander(Protocol):
    def expand(self, query: str) -> list[str]: ...


class LLMTermExpander:
    """Gateway-backed expander; strict JSON contract parsed in code."""

    def __init__(
        self,
        gateway: LLMGateway,
        provider: ProviderConfig,
        profile: ModelProfile,
        max_terms: int = 5,
    ) -> None:
        self.gateway = gateway
        self.provider = provider
        self.profile = profile
        self.max_terms = max_terms

    def expand(self, query: str) -> list[str]:
        response = self.gateway.generate(
            self.provider,
            self.profile,
            GenerationRequest(_PROMPT, f"پرسش: {query}"),
        )
        raw = response.text
        start = raw.find("{")
        if start == -1:
            raise DomainError("term expander returned no JSON object")
        try:
            payload, _ = json.JSONDecoder().raw_decode(raw[start:])
        except json.JSONDecodeError as exc:
            raise DomainError(f"term expander output invalid: {exc.msg}") from exc
        queries = payload.get("queries") if isinstance(payload, dict) else None
        if not isinstance(queries, list):
            raise DomainError("term expander payload missing 'queries' list")
        cleaned = [item for item in queries if isinstance(item, str) and item.strip()]
        return cleaned[: self.max_terms]


__all__ = ["LLMTermExpander", "TermExpander"]
