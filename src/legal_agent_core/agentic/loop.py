"""Budgeted tool-driven research loop (ReAct over read-only legal tools).

Produces the same :class:`ResearchOutcome` as ``NavigationLoop`` so the
application service, verifier and citation publish gate apply unchanged.
Budget exhaustion and honest abstention are reported explicitly — the loop
never invents completion it does not have.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Any

from ..errors import DomainError
from ..navigation import ResearchOutcome
from ..research import ResearchAction, ResearchActionType, ResearchEpisode
from ..verification import (
    AnswerClaim,
    ClaimImportance,
    DraftAnswer,
    EvidenceDisposition,
    EvidenceLedger,
    EvidenceVerifier,
    GenerationMetadata,
    VerificationReport,
)
from .envelope import RefStore
from .planner import PlannerTurn, ToolPlanner
from .skill_router import augment_system_prompt, skills_index
from .tools import LegalResearchTools, ToolArgumentError, tool_catalog

_SYSTEM_TEMPLATE = """شما ایجنت پژوهش حقوقی فقط-خواندن روی مخزن canonical هستید.

قواعد:
۱. فقط از داده‌های برگشتی ابزارها استفاده کن؛ هیچ شناسه، عدد، مبلغ یا نسخه‌ای از خودت نساز.
۲. هر ادعا باید evidence_id موجود در پاسخ ابزارها را استناد کند؛ ادعای بی‌مدرک مجاز نیست.
۳. پاسخ ابزارها «پاکت» است: status، total_count، has_next_page، complete.
   - اگر has_next_page=true است و پرسش پوشش کامل می‌خواهد، همان ابزار را با cursor بعدی صدا بزن.
   - تله‌ی صفحه‌بندی: اگر total_count بزرگ است (بیش از ~۵۰)، صفحه‌بندی بی‌فایده است؛ پرسش را با
     کلیدواژه‌های خاص‌تر بازنویسی کن، از document_scope استفاده کن، یا بر بهترین نتایج صفحه‌ی اول
     پاسخ بده و محدود بودن پوشش را صریح اعلام کن. بیش از ۲ صفحه پیاپی از یک جست‌وجوی یکسان نگیر.
   - EMPTY یعنی صفر نتیجه برای ورودی معتبر؛ NOT_FOUND یعنی شناسه وجود ندارد؛ ERROR/PARTIAL یعنی شکست یا نتیجه‌ی ناقص — یک بار تلاش مجدد کن یا صریح اعلام کن. شکست ابزار هرگز به معنی «یافت نشد» نیست.
۴. استناد دقیق: هر ادعا را به تکه‌ای وصل کن که واقعاً خوانده‌ای. برای صفحه‌ی دقیق از
   find_in_document (grep با شماره صفحه) و read_span استفاده کن؛ صفحات ذکرشده در
   search فقط صفحه‌ی تکه‌ی منطبق است.
۵. بهداشت کوئری: جست‌وجو فقط با ۲ تا ۴ کلیدواژه‌ی نهادی/حقوقی (نام مفاهیم، نه روایت).
   اسم اشخاص، جملات محاوره‌ای و تاریخ‌های محاوره‌ای را در کوئری نیاور؛ اعداد را رقمی
   بنویس (۳۰ نه «سی»). نردبان: search کوتاه → find_in_document روی سند مشخص →
   list_documents برای جهت‌یابی → بازنویسی با مترادف حقوقی.
۶. پرسش‌های ترکیبی/پرونده‌ای را به وجوه بشکن (هر شخص، سند یا حکم یک وجه)؛ هر وجه را
   جدا تحقیق کن و claim جدا با evidence خودش بساز. پیش از answer پوشش همه‌ی وجوه را
   کنترل کن؛ وجهِ بدون اثر را یا ادامه بده یا در متن پاسخ صریحاً «پوشش ناقص» اعلام کن —
   پاسخ قطعیِ ناقص ممنوع.
۷. امتناع آخرین گزینه است و مشروط: پیش از {{"action":"abstain","reason":"..."}} باید
   دست‌کم ۲ جست‌وجوی بازنویسی‌شده با واژه‌های متفاوت + یک find_in_document با واژه‌های
   کلیدی + یک list_documents انجام داده باشی و در reason بگویی دقیقاً چه سند/داده‌ای
   مفقود است. امتناع بدون این جست‌وجوها پذیرفته نیست. حدس و دانش بیرونی همیشه ممنوع است.
   برای واقعه‌ی تاریخی (سال Y): با list_documents(applicable_time=تاریخ واقعه) کنترل کن
   آیا منبع هم‌دوره در مجموعه هست؛ نبودِ منبع هم‌دوره = امتناعِ مستند با همین استدلال.
۸. به‌محض پاسخ‌پذیر بودن پرسش، پاسخ نهایی را بده. بودجه: {budget_steps} فراخوانی ابزار/گام.
۹. ابزارهای موجود:
{tool_catalog}

{skills_index}

## قرارداد خروجی (الزامی) — دقیقاً یک شیء JSON:
فراخوانی ابزار: {{"action":"tool","tool":"نام","args":{{...}},"thought":"..."}}
پاسخ نهایی: {{"action":"answer","answer":"متن فارسی","claims":[{{"claim_id":"c1","text":"...","evidence_ids":["ev-..."],"importance":"critical|major|minor"}}]}}
امتناع: {{"action":"abstain","reason":"..."}}
"""

_CONTRACT_REMINDER = (
    "یادآوری قرارداد: فقط یک شیء JSON برگردان — "
    '{"action":"tool",...} یا {"action":"answer",...} یا {"action":"abstain",...}.'
)


class IncompleteReason(StrEnum):
    BUDGET_STEPS = "budget_steps_exhausted"
    BUDGET_WALL = "budget_wall_clock_exceeded"
    BUDGET_TOKENS = "budget_tokens_exhausted"
    LLM_ERROR = "llm_error"
    DECLARED_ABSTENTION = "declared_abstention"
    MAX_STEPS_WITHOUT_ANSWER = "max_steps_without_accepted_answer"


@dataclass(frozen=True, slots=True)
class AgenticConfig:
    max_steps: int = 12
    wall_seconds: float = 120.0
    max_total_tokens: int = 60000
    #: How many of the most recent transcript steps render in full; older ones
    #: collapse to one-line summaries (evidence ids preserved) so long research
    #: runs do not drown the planner in stale tool envelopes.
    recent_full_steps: int = 2

    def __post_init__(self) -> None:
        if self.max_steps < 1:
            raise DomainError("max_steps must be positive")
        if self.wall_seconds <= 0:
            raise DomainError("wall_seconds must be positive")
        if self.max_total_tokens < 1:
            raise DomainError("max_total_tokens must be positive")
        if self.recent_full_steps < 0:
            raise DomainError("recent_full_steps cannot be negative")


@dataclass(frozen=True, slots=True)
class ToolCallTrace:
    step: int
    tool: str
    args: dict
    ok: bool
    status: str | None
    error: str | None
    latency_ms: int
    evidence_count: int


@dataclass(slots=True)
class _StepRecord:
    step: int
    kind: str
    text: str = ""
    summary: str = ""

    def render(self, compact: bool = False) -> str:
        body = self.summary if compact and self.summary else self.text
        return f"[گام {self.step}] {body}"


_BUDGET_WARNING = (
    "هشدار بودجه: تنها دو گام باقی مانده است. جست‌وجوی جدید آغاز نکن؛ "
    "از بهترین یافته‌های همین تاریخچه پاسخ مستند بساز (evidence_idهای ذکرشده معتبرند) "
    "یا اگر ادله واقعاً کافی نیست، امتناع مستدل با ذکر دقیق مفقود."
)


@dataclass(slots=True)
class _RunState:
    store: RefStore = field(default_factory=RefStore)
    steps: list[_StepRecord] = field(default_factory=list)
    trace: list[ToolCallTrace] = field(default_factory=list)
    total_tokens: int = 0
    last_turn: PlannerTurn | None = None


class ToolResearchLoop:
    def __init__(
        self,
        tools: LegalResearchTools,
        planner: ToolPlanner,
        verifier: EvidenceVerifier,
        config: AgenticConfig | None = None,
    ) -> None:
        self.tools = tools
        self.planner = planner
        self.verifier = verifier
        self.config = config or AgenticConfig()
        self.last_trace: tuple[ToolCallTrace, ...] = ()

    def run(
        self,
        *,
        episode_id: str,
        organization_id: str,
        user_id: str,
        conversation_id: str,
        question: str,
        applicable_time: date,
        document_scope: tuple[str, ...] = (),
        progress: Callable[[dict], None] | None = None,
    ) -> ResearchOutcome:
        if not question.strip():
            raise DomainError("research question must not be blank")
        episode = ResearchEpisode(
            episode_id=episode_id,
            organization_id=organization_id,
            user_id=user_id,
            conversation_id=conversation_id,
            question=question,
            applicable_time=applicable_time,
            document_scope=document_scope,
        )
        ledger = EvidenceLedger()
        state = _RunState()
        system_prompt = augment_system_prompt(self._system_prompt(), question)
        deadline = time.monotonic() + self.config.wall_seconds
        draft: DraftAnswer | None = None
        report = None
        dispositions: set[tuple[str, EvidenceDisposition]] = set()

        for step in range(1, self.config.max_steps + 1):
            turn = self._call_planner(system_prompt, question, applicable_time, state, episode)
            if turn is None:
                break
            state.last_turn = turn
            state.total_tokens += turn.input_tokens + turn.output_tokens
            if state.total_tokens > self.config.max_total_tokens:
                return self._incomplete(
                    episode,
                    ledger,
                    state,
                    draft,
                    report,
                    IncompleteReason.BUDGET_TOKENS,
                    step,
                )
            if time.monotonic() > deadline:
                return self._incomplete(
                    episode, ledger, state, draft, report, IncompleteReason.BUDGET_WALL, step
                )

            if turn.contract_error is not None or turn.decision is None:
                message = turn.contract_error or "planner returned no decision"
                state.steps.append(
                    _StepRecord(
                    step,
                    "contract_error",
                    f"{message}\n{_CONTRACT_REMINDER}",
                    summary=f"CONTRACT-ERROR: {message[:120]} — {_CONTRACT_REMINDER}",
                )
                )
                episode.record(
                    ResearchAction(
                        ResearchActionType.CHECKED_DEFINITION,
                        metadata={"step": step, "contract_error": message},
                    )
                )
                _emit(
                    progress,
                    {
                        "type": "contract_error",
                        "step": step,
                        "message": message,
                    },
                )
                continue

            decision = turn.decision
            if decision.action == "tool":
                self._run_tool_step(
                    decision.tool or "",
                    decision.args or {},
                    step,
                    state,
                    episode,
                    ledger,
                    progress,
                )
                if (
                    self.config.max_steps - step == 2
                    and not any(item.kind == "budget_warning" for item in state.steps)
                ):
                    state.steps.append(
                        _StepRecord(step, "budget_warning", _BUDGET_WARNING)
                    )
                continue
            if decision.action == "abstain":
                draft = DraftAnswer(
                    f"answer-{hashlib.sha256(f'{episode_id}|abstain|{step}'.encode()).hexdigest()[:24]}",
                    f"پاسخ صادر نشد — امتناع صریح: {decision.abstention_reason}",
                    (),
                )
                episode.identified_issues.append(IncompleteReason.DECLARED_ABSTENTION.value)
                report = self.verifier.verify(draft, ledger, applicable_time)
                self._record_dispositions(episode, ledger, dispositions)
                _emit(progress, {"type": "abstained", "step": step, "reason": decision.abstention_reason})
                return ResearchOutcome(episode, ledger, draft, report, False, step)
            if decision.action == "answer":
                try:
                    claims = _parse_claims(decision.claims)
                except DomainError as exc:
                    state.steps.append(
                        _StepRecord(
                            step,
                            "contract_error",
                            f"claims نامعتبر: {exc}\n{_CONTRACT_REMINDER}",
                            summary=f"CONTRACT-ERROR: claims نامعتبر: {exc}",
                        )
                    )
                    continue
                # A claim without evidence can never be published, so drop it
                # here instead of letting the publish gate crash the run. The
                # dropped text stays in the answer body.
                publishable = tuple(claim for claim in claims if claim.evidence_ids)
                dropped = len(claims) - len(publishable)
                if dropped:
                    episode.identified_issues.append("dropped_unevidenced_claims")
                    state.steps.append(
                        _StepRecord(
                            step,
                            "dropped_claims",
                            f"{dropped} ادعای بدون شاهد از فهرست claimها حذف شد.",
                            summary=f"{dropped} ادعای بدون شاهد حذف شد.",
                        )
                    )
                claims = publishable
                draft = DraftAnswer(
                    _answer_id(episode_id, step, decision.answer or ""),
                    decision.answer or "",
                    claims,
                    self._generation_metadata(turn),
                )
                report = self.verifier.verify(draft, ledger, applicable_time)
                self._record_dispositions(episode, ledger, dispositions)
                episode.evidence_ids = list(report.verified_evidence_ids)
                episode.answer_id = draft.answer_id
                if report.accepted:
                    self.last_trace = tuple(state.trace)
                    _emit(progress, {"type": "verification_accepted", "step": step})
                    return ResearchOutcome(episode, ledger, draft, report, True, step)
                state.steps.append(
                    _StepRecord(
                        step,
                        "rejected",
                        "پاسخ راستی‌آزمایی نشد؛ issueها:\n"
                        + "\n".join(f"- {issue.code.value}: {issue.message}" for issue in report.issues)
                        + "\nبا ابزارها اثر موردنیاز را پیدا و باز کن، سپس پاسخ را بازنویسی کن.",
                        summary="پاسخ راستی‌آزمایی نشد: "
                        + ", ".join(issue.code.value for issue in report.issues),
                    )
                )
                _emit(
                    progress,
                    {
                        "type": "verification_rejected",
                        "step": step,
                        "issues": [
                            {"code": issue.code.value, "message": issue.message}
                            for issue in report.issues
                        ],
                    },
                )
                continue

        reason = (
            IncompleteReason.BUDGET_STEPS
            if any(item.ok for item in state.trace)
            else IncompleteReason.MAX_STEPS_WITHOUT_ANSWER
        )
        self.last_trace = tuple(state.trace)
        return self._incomplete(episode, ledger, state, draft, report, reason, self.config.max_steps)

    # ------------------------------------------------------------------ steps

    def _run_tool_step(
        self,
        tool: str,
        args: dict,
        step: int,
        state: _RunState,
        episode: ResearchEpisode,
        ledger: EvidenceLedger,
        progress: Callable[[dict], None] | None = None,
    ) -> None:
        started = time.monotonic()
        try:
            result = self.tools.dispatch(state.store, tool, args)
        except ToolArgumentError as exc:
            latency = int((time.monotonic() - started) * 1000)
            state.trace.append(
                ToolCallTrace(step, tool, dict(args), False, None, str(exc), latency, 0)
            )
            state.steps.append(
                _StepRecord(
                    step,
                    "tool_error",
                    f"args نامعتبر برای {tool}: {exc}",
                    summary=f"TOOL-ERROR {tool}: {str(exc)[:100]}",
                )
            )
            _emit(progress, {"type": "tool_error", "step": step, "tool": tool, "error": str(exc)})
            return
        except DomainError as exc:
            latency = int((time.monotonic() - started) * 1000)
            state.trace.append(
                ToolCallTrace(step, tool, dict(args), False, None, str(exc), latency, 0)
            )
            state.steps.append(
                _StepRecord(
                    step,
                    "tool_error",
                    f"خطای ابزار {tool}: {exc}",
                    summary=f"TOOL-ERROR {tool}: {str(exc)[:100]}",
                )
            )
            _emit(progress, {"type": "tool_error", "step": step, "tool": tool, "error": str(exc)})
            return
        latency = int((time.monotonic() - started) * 1000)
        envelope = result.envelope
        for evidence in result.evidence:
            if ledger.add(evidence):
                episode.record(
                    ResearchAction(
                        ResearchActionType.OPENED,
                        evidence.provision_version_id,
                        {"step": step, "tool": tool},
                    )
                )
        self._record_tool_action(episode, tool, args, step, envelope.status.value)
        state.trace.append(
            ToolCallTrace(
                step,
                tool,
                dict(args),
                True,
                envelope.status.value,
                "; ".join(envelope.errors) or None,
                latency,
                len(result.evidence),
            )
        )
        state.steps.append(
            _StepRecord(
                step,
                "tool",
                f"TOOL {tool} {json.dumps(args, ensure_ascii=False, sort_keys=True)}\n"
                + json.dumps(envelope.to_payload(), ensure_ascii=False, sort_keys=True),
                summary=self._tool_summary(tool, args, envelope),
            )
        )
        _emit(
            progress,
            {
                "type": "tool_call",
                "step": step,
                "tool": tool,
                "args": dict(args),
                "ok": True,
                "status": envelope.status.value,
                "total_count": envelope.total_count,
                "returned_count": envelope.returned_count,
                "complete": envelope.complete,
                "latency_ms": latency,
                "evidence_count": len(result.evidence),
            },
        )

    @staticmethod
    def _tool_summary(tool: str, args: dict, envelope: Any) -> str:
        args_blob = json.dumps(args, ensure_ascii=False, sort_keys=True)
        if len(args_blob) > 100:
            args_blob = args_blob[:99] + "…"
        coverage = (
            f"{envelope.returned_count}/{envelope.total_count}"
            if envelope.total_count is not None
            else str(envelope.returned_count)
        )
        if not envelope.complete:
            coverage += "، ناکامل"
        evidence_ids = [
            item["evidence_id"]
            for item in envelope.items
            if isinstance(item, dict) and item.get("evidence_id")
        ]
        id_blob = ",".join(str(value) for value in evidence_ids[:5])
        if len(evidence_ids) > 5:
            id_blob += "…"
        suffix = f" ev={id_blob}" if id_blob else ""
        return f"TOOL {tool} {args_blob} → {envelope.status.value} ({coverage}){suffix}"

    def _call_planner(
        self,
        system_prompt: str,
        question: str,
        applicable_time: date,
        state: _RunState,
        episode: ResearchEpisode,
    ) -> PlannerTurn | None:
        header = (
            f"پرسش: {question}\n"
            f"تاریخ قابل اعمال (applicable_time): {applicable_time.isoformat()}"
        )
        if document_scope := tuple(episode.document_scope):
            header += f"\nمحدوده اسناد (document_scope): {', '.join(document_scope)}"
        transcript: str
        if state.steps:
            recent = max(0, len(state.steps) - self.config.recent_full_steps)
            lines = [
                item.render(compact=index < recent)
                for index, item in enumerate(state.steps)
            ]
            notice = (
                "(گام‌های قدیمی خلاصه شده‌اند؛ evidence_id و result_ref ذکرشده همچنان "
                "معتبرند — برای متن کامل، read_span یا تکرار ابزار)\n"
                if recent > 0
                else ""
            )
            transcript = "\n\n".join(
                (
                    header,
                    "تاریخچه‌ی پژوهش تا این لحظه:\n" + notice,
                    *lines,
                )
            )
        else:
            transcript = f"{header}\n\nهیچ گامی انجام نشده؛ شروع کن."
        last_error: DomainError | None = None
        for attempt in range(2):
            try:
                return self.planner.decide(system_prompt, transcript)
            except DomainError as exc:
                last_error = exc
                if attempt == 0:
                    time.sleep(0.5)
                    continue
        episode.identified_issues.append(IncompleteReason.LLM_ERROR.value)
        state.steps.append(
            _StepRecord(0, "llm_error", f"planner failure after retry: {last_error}")
        )
        return None

    def _system_prompt(self) -> str:
        return _SYSTEM_TEMPLATE.format(
            budget_steps=self.config.max_steps,
            tool_catalog=tool_catalog(),
            skills_index=skills_index(),
        )

    @staticmethod
    def _record_tool_action(
        episode: ResearchEpisode,
        tool: str,
        args: dict,
        step: int,
        status: str,
    ) -> None:
        action_type = {
            "search_provisions": ResearchActionType.SEARCHED,
            "aggregate_count": ResearchActionType.SEARCHED,
            "get_provision_version": ResearchActionType.OPENED,
            "get_version_history": ResearchActionType.CHECKED_VERSION,
            "temporal_check": ResearchActionType.CHECKED_VERSION,
            "get_references": ResearchActionType.FOLLOWED_REFERENCE,
        }.get(tool, ResearchActionType.SEARCHED)
        target = (
            args.get("provision_version_id")
            or args.get("provision_id")
            or args.get("query")
        )
        episode.record(
            ResearchAction(
                action_type,
                target if isinstance(target, str) else None,
                {"step": step, "tool": tool, "status": status},
            )
        )

    @staticmethod
    def _record_dispositions(
        episode: ResearchEpisode,
        ledger: EvidenceLedger,
        recorded: set[tuple[str, EvidenceDisposition]],
    ) -> None:
        for entry in ledger.entries:
            if entry.disposition == EvidenceDisposition.PENDING:
                continue
            key = (entry.evidence.evidence_id, entry.disposition)
            if key in recorded:
                continue
            action_type = (
                ResearchActionType.ACCEPTED_EVIDENCE
                if entry.disposition == EvidenceDisposition.ACCEPTED
                else ResearchActionType.REJECTED_EVIDENCE
            )
            episode.record(
                ResearchAction(
                    action_type,
                    entry.evidence.evidence_id,
                    {"reason": entry.reason},
                )
            )
            recorded.add(key)

    # -------------------------------------------------------------- outcomes

    def _incomplete(
        self,
        episode: ResearchEpisode,
        ledger: EvidenceLedger,
        state: _RunState,
        draft: DraftAnswer | None,
        report: VerificationReport | None,
        reason: IncompleteReason,
        iterations: int,
    ) -> ResearchOutcome:
        self.last_trace = tuple(state.trace)
        if reason.value not in episode.identified_issues:
            episode.identified_issues.append(reason.value)
        if draft is None:
            draft = DraftAnswer(
                _answer_id(episode.episode_id, iterations, reason.value),
                f"INCOMPLETE: پژوهش ناتمام ماند ({reason.value}).",
                (),
            )
        verification = report or self.verifier.verify(
            draft, ledger, episode.applicable_time
        )
        return ResearchOutcome(episode, ledger, draft, verification, False, iterations)

    @staticmethod
    def _generation_metadata(turn: PlannerTurn) -> GenerationMetadata:
        return GenerationMetadata(
            provider_id=turn.provider_id,
            model=turn.model,
            model_profile_id=turn.model_profile_id,
            model_profile_version=turn.model_profile_version,
            prompt_id=turn.prompt_id,
            prompt_version=turn.prompt_version,
            agent_version=turn.agent_version,
            provider_response_id=turn.response_id,
            provider_request_id=turn.request_id,
        )


def _emit(progress: Callable[[dict], None] | None, event: dict) -> None:
    if progress is None:
        return
    try:
        progress(event)
    except Exception:  # noqa: BLE001 — progress listeners must never break research
        return


def _parse_claims(raw_claims: Sequence[dict]) -> tuple[AnswerClaim, ...]:
    claims: list[AnswerClaim] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_claims, start=1):
        claim_id = raw.get("claim_id")
        text = raw.get("text")
        evidence_ids = raw.get("evidence_ids", [])
        importance = raw.get("importance", "major")
        if not isinstance(claim_id, str) or not isinstance(text, str):
            raise DomainError(f"claim #{index} requires string claim_id and text")
        if claim_id in seen:
            raise DomainError(f"duplicate claim_id {claim_id!r}")
        seen.add(claim_id)
        if not isinstance(evidence_ids, list) or not all(
            isinstance(item, str) for item in evidence_ids
        ):
            raise DomainError(f"claim {claim_id!r} evidence_ids must be a string array")
        if not isinstance(importance, str):
            raise DomainError(f"claim {claim_id!r} importance must be a string")
        try:
            parsed_importance = ClaimImportance(importance)
        except ValueError as exc:
            raise DomainError(f"claim {claim_id!r} importance is invalid") from exc
        claims.append(AnswerClaim(claim_id, text, tuple(evidence_ids), parsed_importance))
    return tuple(claims)


def _answer_id(episode_id: str, step: int, text: str) -> str:
    identity = f"{episode_id}|{step}|{text}"
    return f"answer-{hashlib.sha256(identity.encode()).hexdigest()[:24]}"
