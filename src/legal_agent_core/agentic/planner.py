"""Planner contract for the tool-driven research loop.

The planner receives the rolling research transcript and must answer with one
strict JSON object: a tool call, the final answer, or an explicit abstention.
Anything else is a contract violation that the loop feeds back verbatim.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

from ..llm import (
    GenerationRequest,
    LLMGateway,
    ModelProfile,
    ProviderConfig,
    ProviderResponse,
)


@dataclass(frozen=True, slots=True)
class PlannerDecision:
    action: str  # "tool" | "answer" | "abstain"
    tool: str | None = None
    args: dict[str, Any] | None = None
    thought: str | None = None
    answer: str | None = None
    claims: tuple[dict[str, Any], ...] = ()
    abstention_reason: str | None = None


@dataclass(frozen=True, slots=True)
class PlannerTurn:
    decision: PlannerDecision | None
    contract_error: str | None
    raw_text: str
    input_tokens: int
    output_tokens: int
    provider_id: str
    model: str
    model_profile_id: str
    model_profile_version: int
    prompt_id: str
    prompt_version: int
    agent_version: str
    response_id: str
    request_id: str | None = None


class ToolPlanner(Protocol):
    def decide(self, system_prompt: str, user_prompt: str) -> PlannerTurn: ...


class LLMToolPlanner:
    """Gateway-backed planner; JSON contract is parsed and enforced in code."""

    def __init__(
        self,
        gateway: LLMGateway,
        provider: ProviderConfig,
        profile: ModelProfile,
        agent_version: str = "legal-agent-core/0.1",
    ) -> None:
        self.gateway = gateway
        self.provider = provider
        self.profile = profile
        self.agent_version = agent_version

    def decide(self, system_prompt: str, user_prompt: str) -> PlannerTurn:
        response = self.gateway.generate(
            self.provider,
            self.profile,
            GenerationRequest(system_prompt, user_prompt),
        )
        decision, contract_error = _parse_decision(response)
        return PlannerTurn(
            decision=decision,
            contract_error=contract_error,
            raw_text=response.text,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            provider_id=self.provider.provider_id,
            model=response.model,
            model_profile_id=self.profile.profile_id,
            model_profile_version=self.profile.version,
            prompt_id=self.profile.prompt_id,
            prompt_version=self.profile.prompt_version,
            agent_version=self.agent_version,
            response_id=response.response_id,
            request_id=response.request_id,
        )


def _parse_decision(
    response: ProviderResponse,
) -> tuple[PlannerDecision | None, str | None]:
    raw = response.text
    start = raw.find("{")
    if start == -1:
        return None, "planner output contains no JSON object"
    # raw_decode parses the FIRST complete JSON object and ignores any
    # trailing commentary — models routinely append prose (sometimes with
    # braces) after the object, which broke the previous find/rfind span.
    try:
        payload, _ = json.JSONDecoder().raw_decode(raw[start:])
    except json.JSONDecodeError as exc:
        return None, f"planner output is not valid JSON: {exc.msg}"
    if not isinstance(payload, dict):
        return None, "planner output must be a JSON object"

    action = payload.get("action")
    if action == "tool":
        tool = payload.get("tool")
        args = payload.get("args", {})
        if not isinstance(tool, str) or not tool.strip():
            return None, "tool decision requires a non-empty 'tool' string"
        if not isinstance(args, dict):
            return None, "tool decision requires an object 'args'"
        thought = payload.get("thought")
        if thought is not None and not isinstance(thought, str):
            return None, "'thought' must be a string"
        return PlannerDecision("tool", tool=tool, args=args, thought=thought), None
    if action == "answer":
        answer = payload.get("answer")
        claims = payload.get("claims", [])
        if not isinstance(answer, str) or not answer.strip():
            return None, "answer decision requires non-empty 'answer' text"
        if not isinstance(claims, list) or not all(isinstance(item, dict) for item in claims):
            return None, "answer decision requires a list of claim objects"
        return PlannerDecision(
            "answer", answer=answer, claims=tuple(claims)
        ), None
    if action == "abstain":
        reason = payload.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            return None, "abstain decision requires a non-empty 'reason'"
        return PlannerDecision("abstain", abstention_reason=reason), None
    return None, f"unknown planner action {action!r}"


__all__ = [
    "LLMToolPlanner",
    "PlannerDecision",
    "PlannerTurn",
    "ToolPlanner",
]
