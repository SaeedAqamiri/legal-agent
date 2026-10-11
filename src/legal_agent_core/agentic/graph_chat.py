"""Multi-turn graph-guide chat loop (lightweight ReAct over graph tools).

A conversational companion to :mod:`.loop`: same planner contract and honest
envelope tooling, but tuned for exploring the laws graph (counts, relations,
paths) instead of producing evidence-bound research answers. No Evidence
ledger, no verification gate — every answer carries the node ids it talked
about so the UI can focus the graph on them.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..errors import DomainError
from .envelope import RefStore, ToolStatus
from .graph_tools import GraphTools, harvest_refs
from .loop import ToolCallTrace
from .planner import PlannerTurn, ToolPlanner, parse_node_refs

_SYSTEM_TEMPLATE = """شما «راهنمای کاوش گراف قوانین» هستید؛ یک دستیار گفت‌وگومحور که روی گراف \
دانش قوانین (FalkorDB، فقط-خواندن) کاوش می‌کند: آمار، یافتن گره‌ها، همسایه‌ها و مسیرها.

## شمای گراف
- گره‌ها:
  - LegalInstrument {{id, title, kind, tier, issuer, provisions, domain, summary}} — قوانین و مقررات
  - Provision {{id, instrument_id, label, type, number, summary}} — مواد/تبصره‌ها
  - Judgment {{id, title, category, summary}} — آرای قضایی
  - AdvisoryOpinion {{id, title, category, summary}} — نظریات مشورتی
  - Authority {{name}} — مرجع تصویب‌کننده   - LegalDomain {{name}} — حوزه موضوعی
- یال‌ها: CONTAINS (قانون→ماده)، PART_OF (ماده→ماده)، ISSUES (مرجع→قانون)، \
BELONGS_TO_DOMAIN (قانون→دامنه)، CITES (رأی→قانون)، و انواع canonical: \
SUBORDINATE_TO، EXPLICITLY_REFERENCES، AMENDS، REPEALS، IMPLEMENTS
- tierها: 1 اساسی، 2 خاص (منشور)، 3 عادی (قانون)، 4 مقررات (آیین‌نامه)، 5 بخشنامه

## قواعد
۱. هر عدد و نامی فقط از خروجی ابزارها؛ هیچ شمارش یا شناسه‌ای از خودت نساز.
۲. ابزارها را با «فراخوانی تابع» (tool call) صدا بزن؛ برای «چند تا» از graph_stats، برای \
«فلان قانون/ماده/رأی» اول find_nodes (شناسه گره لازم داری)، برای «چه چیزی به فلان وصل است» \
node_neighbors و برای «رابطه بین دو چیز» find_paths. اگر هیچ‌کدام کافی نبود run_cypher (فقط MATCH/RETURN).
۳. گردش‌کار موضوع (مثل «قوانین مربوط به محیط زیست را بیار») ناوبری روی درخت است، نه شباهت‌یابی:
   الف) search_laws(موضوع) → قوانین مرتبط همراه «خلاصه‌ی» هرکدام؛ خلاصه‌ها را بخوان و مرتبط‌بودن را خودت بسنج.
   ب) برای یک قانونِ مشخص، list_provisions(instrument_id, موضوع) → مواد با خلاصه‌شان.
   ج) برای متن دقیق یک ماده، get_provision_text(provision_id) (گراف فقط برچسب/خلاصه دارد؛ متن از canonical می‌آید).
   د) برای رویه‌ی قضایی: judgments_for_law(قانون) یا search_judgments(موضوع) و سپس get_judgment_text(judgment_id).
۴. پاسخ فارسی، کوتاه و گفت‌وگویی؛ اعداد را با جداکننده بنویس و منبعِ عدد (کدام ابزار) را ضمنی روشن کن. \
اگر نتیجه EMPTY بود صادقانه بگو یافت نشد و پیشنهاد بعدی بده؛ حدس ممنوع.
۵. مهم: به‌محض آنکه اطلاعات برای پاسخ کافی شد، دیگر هیچ ابزاری صدا نزن و پاسخ نهایی را \
به‌صورت متن فارسی بنویس.
۶. پرسش مبهم را با یک فرض روشن جلو ببر؛ برای شمارش دقیق ابتدا تعریفت را در یک جمله بگو.
۷. بودجه: {budget_steps} فراخوانی ابزار در هر نوبت؛ در آخرین گام باید پاسخ نهایی را بنویسی.
"""

_CONTRACT_REMINDER = (
    "یادآوری: یا یک ابزار را با tool call صدا بزن، یا اگر اطلاعات کافی است پاسخ نهایی را "
    "به‌صورت متن فارسی بنویس."
)

_FALLBACK_ANSWER = (
    "این نوبت گفتگو بدون پاسخ نهایی به پایان بودجه رسید. لطفاً پرسش را کمی دقیق‌تر بپرسید "
    "یا دوباره تلاش کنید."
)

_PLANNER_FAILURE_ANSWER = (
    "فعلاً نمی‌توانم پاسخ بدهم (خطای سرویس زبان). لطفاً چند لحظه بعد دوباره تلاش کنید."
)


@dataclass(frozen=True, slots=True)
class GraphChatConfig:
    max_steps: int = 6
    wall_seconds: float = 300.0
    max_total_tokens: int = 150_000
    #: Previous user/assistant turns kept verbatim in the transcript.
    history_turns: int = 8

    def __post_init__(self) -> None:
        if self.max_steps < 1:
            raise DomainError("max_steps must be positive")
        if self.wall_seconds <= 0:
            raise DomainError("wall_seconds must be positive")
        if self.max_total_tokens < 1:
            raise DomainError("max_total_tokens must be positive")
        if self.history_turns < 0:
            raise DomainError("history_turns cannot be negative")


@dataclass(slots=True)
class _TurnState:
    steps: list[str] = field(default_factory=list)
    trace: list[ToolCallTrace] = field(default_factory=list)
    total_tokens: int = 0
    consecutive_empty: int = 0
    #: Graph node refs harvested from this turn's tool results — used as the
    #: clickable-nodes fallback when the planner omits them.
    node_candidates: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class GraphChatResult:
    answer: str
    nodes: tuple[dict[str, Any], ...] = ()
    trace: tuple[ToolCallTrace, ...] = ()
    completed: bool = False

    def to_payload(self) -> dict[str, Any]:
        return {
            "answer": self.answer,
            "nodes": list(self.nodes),
            "completed": self.completed,
            "steps": [
                {
                    "step": item.step,
                    "tool": item.tool,
                    "args": item.args,
                    "ok": item.ok,
                    "status": item.status,
                    "error": item.error,
                    "latency_ms": item.latency_ms,
                }
                for item in self.trace
            ],
        }


class GraphChatLoop:
    """One assistant turn: ReAct over :class:`GraphTools` with conversation memory."""

    def __init__(
        self,
        tools: GraphTools,
        planner: ToolPlanner,
        config: GraphChatConfig | None = None,
    ) -> None:
        self.tools = tools
        self.planner = planner
        self.config = config or GraphChatConfig()

    def run(
        self,
        *,
        message: str,
        history: Sequence[dict[str, str]] = (),
        progress: Callable[[dict], None] | None = None,
    ) -> GraphChatResult:
        if not message.strip():
            raise DomainError("chat message must not be blank")
        system_prompt = self._system_prompt()
        state = _TurnState()
        deadline = time.monotonic() + self.config.wall_seconds

        for step in range(1, self.config.max_steps + 1):
            turn = self._call_planner(
                system_prompt,
                message,
                history,
                state,
                force_answer=step >= self.config.max_steps,
            )
            if turn is None:
                return GraphChatResult(
                    _PLANNER_FAILURE_ANSWER, (), tuple(state.trace), False
                )
            state.total_tokens += turn.input_tokens + turn.output_tokens
            if state.total_tokens > self.config.max_total_tokens or (
                time.monotonic() > deadline
            ):
                return GraphChatResult(
                    _FALLBACK_ANSWER, (), tuple(state.trace), False
                )
            if turn.contract_error is not None or turn.decision is None:
                message_text = turn.contract_error or "planner returned no decision"
                state.steps.append(
                    f"[گام {step}] CONTRACT-ERROR: {message_text}\n{_CONTRACT_REMINDER}"
                )
                _emit(progress, {"type": "contract_error", "step": step, "message": message_text})
                continue

            decision = turn.decision
            if decision.action == "tool":
                self._run_tool_step(decision.tool or "", decision.args or {}, step, state, progress)
                continue
            if decision.action == "answer":
                _emit(progress, {"type": "answering", "step": step})
                nodes = parse_node_refs(decision.nodes)
                if not nodes:
                    nodes = tuple(state.node_candidates[:8])
                return GraphChatResult(
                    decision.answer or "",
                    nodes,
                    tuple(state.trace),
                    True,
                )
            state.steps.append(
                f"[گام {step}] CONTRACT-ERROR: action ناشناخته {decision.action!r}\n{_CONTRACT_REMINDER}"
            )
            _emit(progress, {"type": "contract_error", "step": step, "message": "unknown action"})
        return GraphChatResult(_FALLBACK_ANSWER, (), tuple(state.trace), False)

    # ------------------------------------------------------------------ steps

    def _run_tool_step(
        self,
        tool: str,
        args: dict[str, Any],
        step: int,
        state: _TurnState,
        progress: Callable[[dict], None] | None,
    ) -> None:
        store = RefStore()
        started = time.monotonic()
        try:
            envelope = self.tools.dispatch(store, tool, args)
        except DomainError as exc:
            latency = int((time.monotonic() - started) * 1000)
            state.trace.append(ToolCallTrace(step, tool, dict(args), False, None, str(exc), latency, 0))
            state.steps.append(f"[گام {step}] TOOL-ERROR {tool}: {exc}")
            _emit(progress, {"type": "tool_error", "step": step, "tool": tool, "error": str(exc)})
            return
        latency = int((time.monotonic() - started) * 1000)
        if envelope.status is ToolStatus.EMPTY:
            state.consecutive_empty += 1
        else:
            state.consecutive_empty = 0
        nodes, edges = harvest_refs(tool, envelope, args)
        if envelope.status not in (
            ToolStatus.EMPTY,
            ToolStatus.ERROR,
            ToolStatus.NOT_FOUND,
        ):
            known = {ref["id"] for ref in state.node_candidates}
            for ref in nodes:
                if ref["id"] not in known:
                    state.node_candidates.append(ref)
                    known.add(ref["id"])
        state.trace.append(
            ToolCallTrace(
                step,
                tool,
                dict(args),
                True,
                envelope.status.value,
                "; ".join(envelope.errors) or None,
                latency,
                envelope.returned_count,
            )
        )
        state.steps.append(
            f"[گام {step}] TOOL {tool} {json.dumps(args, ensure_ascii=False)}\n"
            + json.dumps(envelope.to_payload(), ensure_ascii=False)
        )
        _emit(
            progress,
            {
                "type": "tool_call",
                "step": step,
                "tool": tool,
                "args": dict(args),
                "status": envelope.status.value,
                "returned_count": envelope.returned_count,
                "total_count": envelope.total_count,
                "complete": envelope.complete,
                "latency_ms": latency,
                "cypher": envelope.meta.get("cypher"),
                "nodes": nodes,
                "edges": edges,
            },
        )

    def _call_planner(
        self,
        system_prompt: str,
        message: str,
        history: Sequence[dict[str, str]],
        state: _TurnState,
        *,
        force_answer: bool = False,
    ) -> PlannerTurn | None:
        transcript = self._transcript(message, history, state)
        if force_answer:
            transcript += (
                "\n\nاین آخرین گام است؛ دیگر ابزاری صدا نزن و همین حالا پاسخ نهایی را "
                "به‌صورت متن فارسی بنویس."
            )
        for attempt in range(2):
            try:
                return self.planner.decide(
                    system_prompt, transcript, force_answer=force_answer
                )
            except DomainError:
                if attempt == 0:
                    time.sleep(0.5)
        return None  # planner failure is reported as an honest non-answer

    def _transcript(
        self,
        message: str,
        history: Sequence[dict[str, str]],
        state: _TurnState,
    ) -> str:
        parts: list[str] = []
        turns = [
            item
            for item in history
            if isinstance(item, dict)
            and item.get("role") in ("user", "assistant")
            and isinstance(item.get("content"), str)
            and item["content"].strip()
        ]
        if turns and self.config.history_turns > 0:
            recent = turns[-self.config.history_turns :]
            lines = [
                f"{'کاربر' if item['role'] == 'user' else 'راهنما'}: {item['content'].strip()}"
                for item in recent
            ]
            parts.append("گفتگوی پیشین:\n" + "\n\n".join(lines))
        parts.append(f"پرسش کنونی کاربر: {message.strip()}")
        if state.steps:
            parts.append("گام‌های این نوبت:\n" + "\n\n".join(state.steps))
        else:
            parts.append("هیچ گامی انجام نشده؛ با ابزارها شروع کن.")
        return "\n\n".join(parts)

    def _system_prompt(self) -> str:
        return _SYSTEM_TEMPLATE.format(budget_steps=self.config.max_steps)


class GraphChatService:
    """API-facing wrapper: builds fresh loop state per turn and streams events."""

    def __init__(self, loop: GraphChatLoop) -> None:
        self.loop = loop

    def stream(
        self,
        message: str,
        history: Sequence[dict[str, str]] = (),
    ):
        """Yield progress events; the last one carries the completed payload."""
        events: list[dict[str, Any]] = []

        def _collect(event: dict) -> None:
            events.append(event)

        result = self.loop.run(message=message, history=history, progress=_collect)
        yield from events
        yield {"type": "completed", "result": result.to_payload()}


def _emit(progress: Callable[[dict], None] | None, event: dict) -> None:
    if progress is None:
        return
    try:
        progress(event)
    except Exception:  # noqa: BLE001 — listeners must never break the chat
        return


#: Node type to assume for tools whose result items lack a graph label.
__all__ = [
    "GraphChatConfig",
    "GraphChatLoop",
    "GraphChatResult",
    "GraphChatService",
]
