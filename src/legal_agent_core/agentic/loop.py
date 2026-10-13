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
from dataclasses import dataclass, field, replace
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
from .envelope import RefStore, ToolStatus
from .graph_tools import harvest_refs
from .planner import PlannerTurn, ToolPlanner
from .skill_router import augment_system_prompt, skills_index
from .tools import LegalResearchTools, ToolArgumentError

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
۴. استناد دقیق و ناوبری سند: هر ادعا را به تکه‌ای وصل کن که واقعاً خوانده‌ای.
   نردبان اسناد بلند: (۱) get_document_structure → فهرست بخش‌ها با بازه‌ی صفحه؛
   (۲) بخشِ محتمل را از فهرست انتخاب کن؛ (۳) find_in_document با page_from/page_to
   همان بازه؛ (۴) تکه‌ی حاوی پاسخ را read_span کن. **قاعده‌ی cite:** evidence_id ای را
   بزن که متنِ نقل‌شده‌اش واقعاً حاوی جمله‌ی ادعاست — از search یا find یا read_span
   فرقی ندارد (صفحه‌ی hit جست‌وجو گاهی درست‌تر از تکه‌ی خوانده‌شده است). اگر پاسخ در آن
   بخش نبود، به فهرست برگرد و بخشِ محتمل بعدی را امتحان کن — مثل کسی که فهرست کتاب را
   دوباره ورق می‌زند.
۵. بهداشت کوئری: جست‌وجو فقط با ۲ تا ۴ کلیدواژه‌ی نهادی/حقوقی (نام مفاهیم، نه روایت).
   اسم اشخاص، جملات محاوره‌ای و تاریخ‌های محاوره‌ای را در کوئری نیاور؛ اعداد را رقمی
   بنویس (۳۰ نه «سی»). نردبان: search کوتاه → find_in_document روی سند مشخص →
   list_documents برای جهت‌یابی → بازنویسی با مترادف حقوقی. اگر نتایج EMPTY/PARTIAL
   ضعیف بود: expand_search_terms برای کوئری‌های جایگزین و semantic_search برای
   جست‌وجوی معنایی (اگر پاسخ ERROR داد یعنی در دسترس نیست، به واژگانی برگرد).
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
۸. به‌محض پاسخ‌پذیر بودن پرسش، پاسخ نهایی بده؛ ادامه‌ی جست‌وجو پس از کفایت ممنوع.
   EMPTY پیاپی یعنی همان کوئری را تکرار نکن — سند یا واژگان حقوقی متفاوت امتحان کن.
   بودجه: {budget_steps} فراخوانی ابزار/گام.
۹. لایه‌ی قضایی: وقتی قانون/ماده‌ی حاکم را یافتی و پرسش به اجرای عملی، تعارض آرا،
   یا اعتبار/ابطال مقرره مربوط است، با judgments_for_law روی همان قانون آرای وحدت
   رویه/دیوان و نظریات مشورتی مرتبط را ببین (یا با search_judgments موضوع را مستقیم
   جست‌وجو کن) و خلاصه/متن رأی را با get_judgment_text بخوان. رأی در استدلال می‌آید
   ولی ادعاها همچنان باید به evidence ماده مستند بمانند؛ لازم‌الاتباع (رأی وحدت رویه/
   هیئت عمومی) را از متقاعدکننده (نظریه مشورتی/رویه‌ی قضایی) جدا کن.
   مهم: شناسهٔ گره‌های گراف، document_uid رأی و provision_id هرگز evidence_id نیستند؛
   فقط مقدار فیلد evidence_id در خروجی ابزارهای کانونیکال شاهد است. اگر رأی به ماده‌ی
   X قانون Y استناد کرده (cited_laws)، همان ماده را با search_provisions پیدا کن و
   evidence_id آن را شاهد قرار بده؛ خودِ رأی را فقط در متن پاسخ نام ببر.
۱۰. ابزارهای موجود:
{tool_catalog}
{skills_index}

## قرارداد خروجی (الزامی) — دقیقاً یک شیء JSON:
فراخوانی ابزار: {{"action":"tool","tool":"نام","args":{{...}},"thought":"..."}}
پاسخ نهایی: {{"action":"answer","answer":"متن فارسی","claims":[{{"claim_id":"c1","text":"...","evidence_ids":["ev-..."],"importance":"critical|major|minor"}}],"sufficiency":{{"level":"full|partial|insufficient","basis":"...","gaps":["..."]}}}}
امتناع: {{"action":"abstain","reason":"..."}}

## سطح کفایت (sufficiency) — الزامی برای هر answer:
- "full": همه‌ی وجوه پرسش با evidence تأییدشده جواب داده‌اند. در این حالت gaps باید خالی باشد.
- "partial": بخشی جواب داده و بخشی ناقص است — در gaps بنویس دقیقاً چه وجهی کامل نیست (پاسخ منتشر می‌شود ولی برچسب پوشش ناقص می‌خورد).
- "insufficient": ادله برای نتیجه‌گیری کافی نیست — در gaps نام ببر چه سند/داده‌ای مفقود است. این سطح یعنی پاسخ منتشر نمی‌شود (مثل امتناع)؛ پس آن را فقط وقتی اعلام کن که واقعاً نتوانسته‌ای حتی پاسخ partial بدهی.
- **اولویت صداقت:** وقتی هیچ وجه قابل‌پاسخی مستند نشد، insufficient اعلام کن؛ اما
  اگر حتی یک وجه را با evidence پوشش دادی، همان را partial منتشر کن — امتناعِ اضافه
  روی پرسشِ نیمهقابل‌پاسخ هم خطاست.
"""

_CONTRACT_REMINDER = (
    "یادآوری قرارداد: فقط یک شیء JSON برگردان — "
    '{"action":"tool",...} یا {"action":"answer",...} یا {"action":"abstain",...}.'
)

#: Hard cap on one transcript step's rendered text (defence in depth: a tool
#: payload gone wild must not eat the whole token budget by itself).
MAX_STEP_TEXT = 6000


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
        if len(body) > MAX_STEP_TEXT:
            body = (
                body[:MAX_STEP_TEXT]
                + f"\n…(بریده شد؛ {len(body) - MAX_STEP_TEXT} نویسهٔ بعدی حذف شد — "
                "برای متن کامل، read_span را روی همین span تکرار کن)"
            )
        return f"[گام {self.step}] {body}"


_BUDGET_WARNING = (
    "هشدار بودجه: تنها دو گام باقی مانده است. جست‌وجوی جدید آغاز نکن؛ "
    "از بهترین یافته‌های همین تاریخچه پاسخ مستند بساز (evidence_idهای ذکرشده معتبرند) "
    "یا اگر ادله واقعاً کافی نیست، امتناع مستدل با ذکر دقیق مفقود."
)

_DEAD_END_WARNING = (
    "هشدار بن‌بست: سه جست‌وجوی پیاپی بدون نتیجه انجام شده است. همین کوئری را با "
    "چند واژه‌ی متفاوت تکرار نکن؛ یکی از این مسیرها: (۱) سند/فصل دیگری امتحان کن، "
    "(۲) واژه‌های نهادی متفاوت به‌کار ببر، (۳) از بهتری یافته‌ها پاسخ بساز، یا (۴) "
    "امتناع مستند با فهرست تلاش‌های انجام‌شده."
)


@dataclass(slots=True)
class _RunState:
    store: RefStore = field(default_factory=RefStore)
    steps: list[_StepRecord] = field(default_factory=list)
    trace: list[ToolCallTrace] = field(default_factory=list)
    total_tokens: int = 0
    last_turn: PlannerTurn | None = None
    consecutive_empty: int = 0
    #: Deterministic tools make repeated (tool, args) calls pointless; cache
    #: the first envelope per key and replay it so the model cannot burn the
    #: step budget re-reading the same span.
    repeat_cache: dict[tuple[str, str], Any] = field(default_factory=dict)
    #: How many successful calls touched each instrument (for the structure
    #: hint) and which documents already had their outline requested/hinted.
    doc_touch: dict[str, int] = field(default_factory=dict)
    structure_keys: set[str] = field(default_factory=set)
    nav_hinted: set[str] = field(default_factory=set)


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
            turn = self._call_planner(
                system_prompt, question, applicable_time, state, episode, ledger
            )
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
                    thought=decision.thought,
                )
                if state.consecutive_empty >= 3 and state.consecutive_empty % 3 == 0:
                    state.steps.append(
                        _StepRecord(step, "dead_end_warning", _DEAD_END_WARNING)
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
                # Only evidence ids that actually entered the ledger can back a
                # claim. Models routinely cite graph node ids (a judgment's
                # document_uid) or stale ids; dropping those here and feeding
                # the mistake back avoids a wasted verification-rejection loop.
                known_ids = {entry.evidence.evidence_id for entry in ledger.entries}
                filtered: list[AnswerClaim] = []
                unknown: set[str] = set()
                for claim in claims:
                    if not claim.evidence_ids:
                        # No evidence at all: leave it for the unevidenced-claim
                        # handling below (keeps the honest dropped-claims signal).
                        filtered.append(claim)
                        continue
                    valid = tuple(eid for eid in claim.evidence_ids if eid in known_ids)
                    unknown |= set(claim.evidence_ids) - set(valid)
                    if valid:
                        filtered.append(replace(claim, evidence_ids=valid))
                if unknown and not filtered:
                    state.steps.append(
                        _StepRecord(
                            step,
                            "contract_error",
                            "CONTRACT-ERROR: unknown evidence ids — این شناسه‌ها evidence_id "
                            "معتبر نیستند (در دفتر شواهد نیستند): "
                            + ", ".join(sorted(unknown)[:8])
                            + "\nشناسهٔ گره‌های گراف، document_uid آرا و provision_id شاهد "
                            "نیستند؛ فقط مقدار فیلد evidence_id در خروجی ابزارهای کانونیکال "
                            "(search_provisions/read_span/...) معتبر است. برای پرسش مبتنی بر رأی، "
                            "مواد قانونی که رأی به آن‌ها استناد کرده (cited_laws) را با "
                            "search_provisions پیدا کن و همان‌ها را شاهد قرار بده.\n"
                            + _CONTRACT_REMINDER,
                            summary="CONTRACT-ERROR: unknown evidence ids",
                        )
                    )
                    _emit(
                        progress,
                        {
                            "type": "contract_error",
                            "step": step,
                            "message": "unknown evidence ids",
                        },
                    )
                    continue
                if unknown:
                    episode.identified_issues.append("dropped_unknown_evidence_ids")
                claims = tuple(filtered)
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
                level = decision.sufficiency_level
                if level == "insufficient":
                    # Declared insufficiency routes to the honest incomplete
                    # outcome: the assessment itself is the deliverable.
                    reason = decision.sufficiency_basis or "; ".join(
                        decision.sufficiency_gaps
                    )
                    draft = DraftAnswer(
                        _answer_id(episode_id, step, f"insufficient|{reason}"),
                        f"پاسخ صادر نشد — سطح کفایت اعلام‌شده: ناکافی. {reason}",
                        (),
                    )
                    episode.identified_issues.append(
                        IncompleteReason.DECLARED_ABSTENTION.value
                    )
                    episode.identified_issues.append("sufficiency_insufficient")
                    report = self.verifier.verify(draft, ledger, applicable_time)
                    self._record_dispositions(episode, ledger, dispositions)
                    _emit(
                        progress,
                        {
                            "type": "abstained",
                            "step": step,
                            "reason": reason or "insufficient",
                        },
                    )
                    return ResearchOutcome(episode, ledger, draft, report, False, step)
                if level == "full" and decision.sufficiency_gaps:
                    # Declaring full coverage while listing gaps is a
                    # contradiction the planner must resolve.
                    state.steps.append(
                        _StepRecord(
                            step,
                            "contract_error",
                            "sufficiency=full هم‌زمان با gaps ناسازگار است؛ "
                            "یا level=partial/insufficient بده یا gaps را بردار.\n"
                            f"gaps: {json.dumps(decision.sufficiency_gaps, ensure_ascii=False)}",
                            summary="CONTRACT-ERROR: sufficiency=full با gaps ناسازگار",
                        )
                    )
                    continue
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
                    if level is not None:
                        # Record the accepted answer's self-assessment so the
                        # grader can score sufficiency agreement.
                        episode.identified_issues.append(f"sufficiency_{level}")
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
        thought: str | None = None,
    ) -> None:
        started = time.monotonic()
        key = (tool, json.dumps(args, ensure_ascii=False, sort_keys=True))
        cached = state.repeat_cache.get(key)
        if cached is not None:
            # Deterministic tool + identical args = identical result. Replay it
            # and tell the model plainly that repeating buys nothing.
            latency = int((time.monotonic() - started) * 1000)
            envelope = cached
            if envelope.status is ToolStatus.EMPTY:
                state.consecutive_empty += 1
            else:
                state.consecutive_empty = 0
            state.trace.append(
                ToolCallTrace(
                    step, tool, dict(args), True, envelope.status.value,
                    "repeat — identical call replayed from cache", latency,
                    envelope.returned_count,
                )
            )
            state.steps.append(
                _StepRecord(
                    step,
                    "repeat",
                    f"فراخوانی تکراری {tool} با دقیقاً همین آرگومان‌ها — همان نتیجهٔ "
                    f"پیشین برگشت (status {envelope.status.value}). تکرار بی‌فایده است: "
                    "با evidence_idهای موجود answer بده، یا آرگومان/ابزار متفاوتی بزن.",
                    summary=f"REPEAT {tool} — همان نتیجهٔ قبلی",
                )
            )
            _emit(
                progress,
                {
                    "type": "tool_call",
                    "step": step,
                    "tool": tool,
                    "args": dict(args),
                    "thought": thought,
                    "ok": True,
                    "status": envelope.status.value,
                    "total_count": envelope.total_count,
                    "returned_count": envelope.returned_count,
                    "complete": envelope.complete,
                    "latency_ms": latency,
                    "evidence_count": 0,
                    "cypher": envelope.meta.get("cypher"),
                    "nodes": [],
                    "edges": [],
                    "repeat": True,
                },
            )
            return
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
        state.repeat_cache[key] = envelope
        nodes, edges = harvest_refs(tool, envelope, args)
        if envelope.status is ToolStatus.EMPTY:
            state.consecutive_empty += 1
        else:
            state.consecutive_empty = 0
        nav_hint = self._maybe_nav_hint(tool, args, envelope, state)
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
        if nav_hint:
            state.steps.append(
                _StepRecord(
                    step,
                    "nav_hint",
                    nav_hint,
                    summary="NAV-HINT: get_document_structure",
                )
            )
        _emit(
            progress,
            {
                "type": "tool_call",
                "step": step,
                "tool": tool,
                "args": dict(args),
                "thought": thought,
                "ok": True,
                "status": envelope.status.value,
                "total_count": envelope.total_count,
                "returned_count": envelope.returned_count,
                "complete": envelope.complete,
                "latency_ms": latency,
                "evidence_count": len(result.evidence),
                "cypher": envelope.meta.get("cypher"),
                "nodes": nodes,
                "edges": edges,
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

    #: Tools whose successful hits mean "we are grinding inside one document".
    _NAV_TOOLS = frozenset(
        {"search_provisions", "find_in_document", "read_span", "list_document_contents"}
    )

    @staticmethod
    def _fold(text: str) -> str:
        return (text or "").casefold().replace("\u200c", "").strip()

    @classmethod
    def _maybe_nav_hint(
        cls, tool: str, args: dict, envelope: Any, state: _RunState
    ) -> str | None:
        """One-time per document: push the PageIndex ladder when the model is
        grinding inside a long document without ever opening its outline.

        The rule exists in the prompt, but models ignore it under keyword
        habits (76-case run: get_document_structure called twice); a targeted
        transcript note is what actually changes behaviour — same mechanism as
        the evidence nudge.
        """
        if envelope.status in (ToolStatus.EMPTY, ToolStatus.ERROR, ToolStatus.NOT_FOUND):
            return None
        if tool == "get_document_structure":
            document = args.get("document")
            if isinstance(document, str) and document.strip():
                state.structure_keys.add(cls._fold(document))
            return None
        if tool not in cls._NAV_TOOLS or not envelope.items:
            return None
        touched: set[str] = set()
        for item in envelope.items:
            if not isinstance(item, dict):
                continue
            title = item.get("instrument_title")
            if isinstance(title, str) and title.strip():
                touched.add(title.strip())
        hint: str | None = None
        for title in touched:
            key = cls._fold(title)
            state.doc_touch[key] = state.doc_touch.get(key, 0) + 1
            if (
                state.doc_touch[key] >= 2
                and key not in state.structure_keys
                and title not in state.nav_hinted
            ):
                state.nav_hinted.add(title)
                hint = (
                    f"نکتهٔ ناوبری: روی سند «{title}» {state.doc_touch[key]} بار "
                    "جست‌وجو/خواندن شده ولی فهرست بخش‌هایش دیده نشده. "
                    f"get_document_structure(document=«{title}») نقشهٔ بخش‌ها را با "
                    "بازهٔ صفحه می‌دهد؛ بخشِ محتمل را انتخاب کن، find_in_document با "
                    "page_from/page_to همان بازه بزن، تکهٔ حاوی پاسخ را read_span کن و "
                    "evidence_id ای را cite کن که متنش حاوی جملهٔ ادعاست. اگر پاسخ نبود، "
                    "به فهرست برگرد و بخشِ بعدی را امتحان کن."
                )
        return hint

    @staticmethod
    def _evidence_nudge(ledger: EvidenceLedger) -> str:
        """Explicit, model-visible stop signal once evidence exists."""
        evidence_ids = [entry.evidence.evidence_id for entry in ledger.entries]
        if not evidence_ids:
            return "هنوز هیچ evidence معتبری جمع نشده؛ با ابزارها شروع کن."
        shown = ", ".join(evidence_ids[:12])
        more = "…" if len(evidence_ids) > 12 else ""
        return (
            "شواهد در دسترس (evidence_id معتبر): "
            + shown
            + more
            + "\nاگر با همین شواهد می‌توانی پرسش را مستند پاسخ دهی، همین حالا "
            "action=answer بده و جست‌وجوی جدید نکن. فقط اگر وجهی از پرسش هنوز "
            "بی‌پاسخ است، همان وجه را هدف بگیر."
        )

    def _call_planner(
        self,
        system_prompt: str,
        question: str,
        applicable_time: date,
        state: _RunState,
        episode: ResearchEpisode,
        ledger: EvidenceLedger,
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
                    self._evidence_nudge(ledger),
                )
            )
        else:
            transcript = f"{header}\n\nهیچ گامی انجام نشده؛ شروع کن."
        last_error: DomainError | None = None
        for attempt in range(3):
            try:
                return self.planner.decide(system_prompt, transcript)
            except DomainError as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(0.5 * (attempt + 1))
                    continue
        episode.identified_issues.append(IncompleteReason.LLM_ERROR.value)
        state.steps.append(
            _StepRecord(0, "llm_error", f"planner failure after retry: {last_error}")
        )
        return None

    def _system_prompt(self) -> str:
        return _SYSTEM_TEMPLATE.format(
            budget_steps=self.config.max_steps,
            tool_catalog=self.tools.tool_catalog(),
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
            # graph navigation tools
            "get_provision_text": ResearchActionType.OPENED,
            "list_provisions": ResearchActionType.OPENED,
            "get_judgment_text": ResearchActionType.OPENED,
            "node_neighbors": ResearchActionType.FOLLOWED_REFERENCE,
            "find_paths": ResearchActionType.FOLLOWED_REFERENCE,
            "judgments_for_law": ResearchActionType.FOLLOWED_REFERENCE,
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
