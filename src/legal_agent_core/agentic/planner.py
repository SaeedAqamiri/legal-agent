"""Planner contract for the tool-driven research loop.

The planner receives the rolling research transcript and must answer with one
strict JSON object: a tool call, the final answer, or an explicit abstention.
Anything else is a contract violation that the loop feeds back verbatim.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from ..llm import (
    GenerationRequest,
    LLMGateway,
    ModelProfile,
    ProviderConfig,
    ProviderResponse,
    ToolCall,
)

SUFFICIENCY_LEVELS = ("full", "partial", "insufficient")


@dataclass(frozen=True, slots=True)
class PlannerDecision:
    action: str  # "tool" | "answer" | "abstain"
    tool: str | None = None
    args: dict[str, Any] | None = None
    thought: str | None = None
    answer: str | None = None
    claims: tuple[dict[str, Any], ...] = ()
    abstention_reason: str | None = None
    #: Structured self-assessment attached to an "answer" decision.
    sufficiency_level: str | None = None
    sufficiency_basis: str | None = None
    sufficiency_gaps: tuple[str, ...] = ()
    #: Graph-guide chat only: node references for clickable graph focus.
    nodes: tuple[dict[str, Any], ...] = ()


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
    def decide(
        self, system_prompt: str, user_prompt: str, *, force_answer: bool = False
    ) -> PlannerTurn: ...


class LLMToolPlanner:
    """Gateway-backed planner; JSON contract is parsed and enforced in code."""

    def __init__(
        self,
        gateway: LLMGateway,
        provider: ProviderConfig,
        profile: ModelProfile,
        agent_version: str = "legal-agent-core/0.1",
        *,
        tools: Sequence[Mapping[str, Any]] = (),
        allow_text_answer: bool = False,
    ) -> None:
        self.gateway = gateway
        self.provider = provider
        self.profile = profile
        self.agent_version = agent_version
        #: Native tool schemas declared to the provider. When present the model
        #: answers with native ``tool_calls`` instead of the JSON text contract.
        self.tools = tuple(tools)
        #: Accept a plain-text model response as the final answer (used by the
        #: graph-guide chat, whose answers are prose, not the JSON contract).
        self.allow_text_answer = allow_text_answer

    def decide(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        force_answer: bool = False,
    ) -> PlannerTurn:
        request = GenerationRequest(
            system_prompt,
            user_prompt,
            tools=self.tools,
            tool_choice="none" if force_answer else None,
        )
        response = self.gateway.generate(self.provider, self.profile, request)
        if response.tool_calls:
            decision, contract_error = _decision_from_tool_calls(response.tool_calls)
        else:
            decision, contract_error = _parse_decision(
                response, allow_text_answer=self.allow_text_answer
            )
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


def _decision_from_tool_calls(
    calls: tuple[ToolCall, ...],
) -> tuple[PlannerDecision | None, str | None]:
    """Honour a provider-native tool call as a planner tool decision.

    Some reasoning models (e.g. deepseek-v4.1-flash) answer a tool-style prompt
    with native function calls and an empty ``content`` instead of the
    JSON-in-text contract. Only the first call is consumed; the loop runs one
    tool per step.
    """
    call = calls[0]
    raw = call.arguments.strip()
    if not raw:
        return PlannerDecision("tool", tool=call.name, args={}), None
    try:
        args = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f"tool call arguments are not valid JSON: {exc.msg}"
    if not isinstance(args, dict):
        return None, "tool call arguments must be a JSON object"
    return PlannerDecision("tool", tool=call.name, args=args), None


def _parse_decision(
    response: ProviderResponse,
    *,
    allow_text_answer: bool = False,
) -> tuple[PlannerDecision | None, str | None]:
    raw = response.text
    start = raw.find("{")
    if start == -1:
        if allow_text_answer and raw.strip():
            return PlannerDecision("answer", answer=raw.strip()), None
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
        level = basis = None
        gaps: tuple[str, ...] = ()
        sufficiency = payload.get("sufficiency")
        if sufficiency is not None:
            if not isinstance(sufficiency, dict):
                return None, "'sufficiency' must be an object"
            level = sufficiency.get("level")
            if level not in SUFFICIENCY_LEVELS:
                return None, (
                    f"sufficiency.level must be one of {SUFFICIENCY_LEVELS}"
                )
            basis = sufficiency.get("basis")
            if basis is not None and not isinstance(basis, str):
                return None, "'sufficiency.basis' must be a string"
            raw_gaps = sufficiency.get("gaps", [])
            if not isinstance(raw_gaps, list) or not all(
                isinstance(item, str) for item in raw_gaps
            ):
                return None, "'sufficiency.gaps' must be a list of strings"
            gaps = tuple(item for item in raw_gaps if item.strip())
            if level == "insufficient" and not gaps and not (basis or "").strip():
                return None, (
                    "sufficiency=insufficient requires 'gaps' or 'basis' "
                    "naming what the corpus is missing"
                )
        return PlannerDecision(
            "answer",
            answer=answer,
            claims=tuple(claims),
            sufficiency_level=level,
            sufficiency_basis=basis,
            sufficiency_gaps=gaps,
            nodes=parse_node_refs(payload.get("nodes", [])),
        ), None
    if action == "abstain":
        reason = payload.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            return None, "abstain decision requires a non-empty 'reason'"
        return PlannerDecision("abstain", abstention_reason=reason), None
    return None, f"unknown planner action {action!r}"


def parse_node_refs(raw: object) -> tuple[dict[str, Any], ...]:
    """Best-effort node references on an answer (graph-guide chat only).

    Malformed entries are dropped rather than rejected: nodes are a UI
    affordance, never the substance of the answer.
    """
    if not isinstance(raw, list):
        return ()
    nodes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        node_id = item.get("id")
        if not isinstance(node_id, str) or not node_id.strip() or node_id in seen:
            continue
        seen.add(node_id)
        nodes.append(
            {
                "id": node_id.strip(),
                "label": item.get("label") if isinstance(item.get("label"), str) else "node",
                "title": item.get("title") if isinstance(item.get("title"), str) else node_id,
            }
        )
    return tuple(nodes[:20])


__all__ = [
    "LLMToolPlanner",
    "PlannerDecision",
    "PlannerTurn",
    "ToolPlanner",
    "parse_node_refs",
]
