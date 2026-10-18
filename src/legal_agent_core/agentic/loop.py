"""Tool-driven research loop (ReAct over read-only legal tools).

Produces the same :class:`ResearchOutcome` as ``NavigationLoop`` so the
application service, verifier and citation publish gate apply unchanged.
Budget exhaustion and honest abstention are reported explicitly — the loop
never invents completion it does not have.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import queue
import re
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from itertools import count
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
from .skill_router import augment_system_prompt
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
۵. انتخاب سند و بهداشت کوئری: اگر «جهت‌گیری اولیه» target_id سند یا ساختار سند داده است،
   نخست همان شناسه را با get_document_structure/find_in_document به‌کار ببر. اگر نامزدها
   نامرتبط یا ناکافی بودند، جست‌وجوی سراسری را به‌عنوان مسیر بازیابی انجام بده. جست‌وجو
   فقط با ۲ تا ۴ کلیدواژه‌ی نهادی/حقوقی باشد (نام مفاهیم، نه روایت).
   اسم اشخاص، جملات محاوره‌ای و تاریخ‌های محاوره‌ای را در کوئری نیاور؛ اعداد را رقمی
   بنویس (۳۰ نه «سی»). نردبان: search کوتاه → find_in_document روی سند مشخص →
   list_documents برای جهت‌یابی → بازنویسی با مترادف حقوقی. اگر نتایج EMPTY/PARTIAL
   ضعیف بود: expand_search_terms برای کوئری‌های جایگزین و semantic_search برای
   جست‌وجوی معنایی (اگر پاسخ ERROR داد یعنی در دسترس نیست، به واژگانی برگرد).
۶. پرسش‌های ترکیبی/پرونده‌ای را به وجوه بشکن (هر شخص، سند یا حکم یک وجه)؛ هر وجه را
   جدا تحقیق کن و claim جدا با evidence خودش بساز. پیش از answer پوشش همه‌ی وجوه را
   کنترل کن؛ وجهِ بدون اثر را یا ادامه بده یا در متن پاسخ صریحاً «پوشش ناقص» اعلام کن —
   پاسخ قطعیِ ناقص ممنوع.
۷. امتناع آخرین گزینه است: اگر راه معناداری برای بررسی شکاف شواهد باقی مانده،
   همان را دنبال کن. پس از بررسی مسیرهای مرتبط، اگر هنوز سند لازم در مخزن یافت نشد،
   با ذکر دقیق سند/دادهٔ مفقود abstain کن. حدس و دانش بیرونی همیشه ممنوع است.
   برای واقعه‌ی تاریخی (سال Y): با list_documents(applicable_time=تاریخ واقعه) کنترل کن
   آیا منبع هم‌دوره در مجموعه هست؛ نبودِ منبع هم‌دوره = امتناعِ مستند با همین استدلال.
۸. به‌محض پاسخ‌پذیر بودن پرسش، پاسخ نهایی بده؛ ادامه‌ی جست‌وجو پس از کفایت ممنوع.
   EMPTY پیاپی یعنی همان کوئری را تکرار نکن — سند یا واژگان حقوقی متفاوت امتحان کن.
   {budget_policy}
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
 ۹.۵ ناوبری مفهومی: کنترل‌کننده پیش از نخستین تصمیم، درخت و در صورت وجود دفترچه را بررسی
    و target_idهای قابل اقدام را در «جهت‌گیری اولیه» تزریق می‌کند. همان موضوع را
    دوباره به walk_tree نده؛ اگر جست‌وجوی اولیه نامرتبط بود، یک مفهوم حقوقی دقیق‌تر
    استخراج کن و با آن مسیر تازه‌ای بگیر. این لایه فقط راهنماست و هرگز شاهد نیست.
 ۱۰. ابزارهای موجود:
{tool_catalog}
راهنمای لازم هر مرحله در متن «مرحلهٔ فعلی» آمده است؛ برای روند عادی ابزار skill را صدا نزن.

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
MAX_STEP_TEXT = 4000
MAX_EVIDENCE_MEMORY = 6
MAX_EVIDENCE_EXCERPT = 600


class IncompleteReason(StrEnum):
    BUDGET_STEPS = "budget_steps_exhausted"
    BUDGET_WALL = "budget_wall_clock_exceeded"
    BUDGET_TOKENS = "budget_tokens_exhausted"
    LLM_ERROR = "llm_error"
    DECLARED_ABSTENTION = "declared_abstention"
    MAX_STEPS_WITHOUT_ANSWER = "max_steps_without_accepted_answer"


@dataclass(frozen=True, slots=True)
class AgenticConfig:
    max_steps: int | None = None
    # Emergency circuit breaker only. Normal routing follows evidence
    # progress and changes course when a search repeats its earlier result.
    wall_seconds: float = 900.0
    max_total_tokens: int = 120000
    #: How many of the most recent transcript steps render in full; older ones
    #: collapse to one-line summaries (evidence ids preserved) so long research
    #: runs do not drown the planner in stale tool envelopes.
    recent_full_steps: int = 2
    max_search_steps: int | None = None
    max_tool_calls: int | None = None
    answer_reserve_steps: int = 2
    max_tool_seconds: float = 60.0

    def __post_init__(self) -> None:
        if self.max_steps is not None and self.max_steps < 1:
            raise DomainError("max_steps must be positive")
        if self.wall_seconds <= 0:
            raise DomainError("wall_seconds must be positive")
        if self.max_total_tokens < 1:
            raise DomainError("max_total_tokens must be positive")
        if self.recent_full_steps < 0:
            raise DomainError("recent_full_steps cannot be negative")
        if self.max_search_steps is not None and self.max_search_steps < 1:
            raise DomainError("max_search_steps must be positive")
        if self.max_tool_calls is not None and self.max_tool_calls < 1:
            raise DomainError("max_tool_calls must be positive")
        if self.answer_reserve_steps < 1:
            raise DomainError("answer_reserve_steps must be positive")
        if self.max_tool_seconds <= 0:
            raise DomainError("max_tool_seconds must be positive")


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
    #: Typed reason for the move (docs 06 §11): structure | citation |
    #: statutory_basis | precedent | version | search | open | read | notebook
    #: | graph | other. Lets a trace be read as a reasoned navigation path.
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class PlannerCallTrace:
    step: int
    attempt: int
    ok: bool
    latency_ms: int
    input_tokens: int = 0
    output_tokens: int = 0
    provider_id: str | None = None
    model: str | None = None
    action: str | None = None
    error: str | None = None
    tool: str | None = None


#: Tool -> typed navigation reason. The point of a "lawyer-like" trace is that
#: every hop says WHY it was taken (structure/citation/basis/...), not just which
#: tool ran.
_TOOL_REASON: dict[str, str] = {
    "search_provisions": "search",
    "aggregate_count": "search",
    "find_in_document": "search",
    "list_documents": "search",
    "expand_search_terms": "search",
    "semantic_search": "search",
    "get_provision_version": "open",
    "get_provision_text": "open",
    "read_span": "read",
    "get_version_history": "version",
    "temporal_check": "version",
    "get_references": "citation",
    "get_related": "citation",
    "node_neighbors": "citation",
    "find_paths": "citation",
    "get_statute_family": "statutory_basis",
    "get_document_structure": "structure",
    "list_document_contents": "structure",
    "list_children": "structure",
    "search_in_subtree": "search",
    "list_tree": "structure",
    "walk_tree": "structure",
    "search_judgments": "precedent",
    "judgments_for_law": "precedent",
    "get_judgment_text": "precedent",
    "notebook_index": "notebook",
    "notebook_route": "notebook",
    "open_notebook": "notebook",
    "graph_stats": "graph",
    "find_nodes": "graph",
    "search_laws": "graph",
    "list_provisions": "graph",
    "run_cypher": "graph",
}

_BROAD_SEARCH_TOOLS = frozenset(
    {
        "search_provisions",
        "semantic_search",
        "expand_search_terms",
        "list_documents",
        "search_judgments",
    }
)

# Atomic legal phrases lose their meaning when a small model paraphrases them
# into a bag of generic words. Preserve phrases already present in the user's
# question; this is query hygiene, not domain inference.
_LEGAL_QUERY_ANCHORS = (
    "مسئولیت محدود",
    "مستثنیات دین",
    "ضمانت نامه",
    "سابقه غیرقطعی",
    "سابقه قطعی",
    "کارت هوشمند",
    "دفترچه کار",
    "تشخیص مطالبات",
    "هیئت بدوی",
    "بیمه بیکاری",
    "آخرین قسط",
    "پنج درصد",
    "شخصیت حقوقی",
    "حق بیمه",
    "مدیرعامل",
    "مفاصاحساب",
    "وصول اجباری",
    "سرقفلی",
)


def _tool_reason(tool: str) -> str:
    return _TOOL_REASON.get(tool, "other")


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
    planner_trace: list[PlannerCallTrace] = field(default_factory=list)
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
    #: Stable, model-visible evidence memory. It survives transcript compaction
    #: and is keyed by canonical evidence_id rather than by prompt position.
    evidence_memory: dict[str, dict[str, Any]] = field(default_factory=dict)
    orientation_targets: list[dict[str, str]] = field(default_factory=list)
    search_steps: int = 0
    tool_calls: int = 0
    last_search_signature: tuple[str, frozenset[str], tuple[str, ...]] | None = None
    last_search_candidates: tuple[str, ...] = ()
    last_search_evidence_ids: frozenset[str] | None = None
    duplicate_blocks: int = 0
    deadline_exceeded: bool = False
    query_anchors: tuple[str, ...] = ()


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
        self.last_planner_trace: tuple[PlannerCallTrace, ...] = ()

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
        state.query_anchors = _query_anchors(question)
        self.last_trace = ()
        self.last_planner_trace = ()
        deadline = time.monotonic() + self.config.wall_seconds
        self._seed_orientation(state, question, applicable_time, progress, deadline)
        system_prompt = augment_system_prompt(self._system_prompt(), question)
        draft: DraftAnswer | None = None
        report = None
        dispositions: set[tuple[str, EvidenceDisposition]] = set()

        for step in count(1):
            if self.config.max_steps is not None and step > self.config.max_steps:
                break
            if time.monotonic() >= deadline:
                return self._incomplete(
                    episode, ledger, state, draft, report, IncompleteReason.BUDGET_WALL, step - 1
                )
            turn = self._call_planner(
                system_prompt,
                question,
                applicable_time,
                state,
                episode,
                ledger,
                deadline,
                step,
                progress,
            )
            if turn is None:
                if state.deadline_exceeded:
                    return self._incomplete(
                        episode,
                        ledger,
                        state,
                        draft,
                        report,
                        IncompleteReason.BUDGET_WALL,
                        step,
                    )
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
                    deadline=deadline,
                )
                if time.monotonic() >= deadline:
                    return self._incomplete(
                        episode,
                        ledger,
                        state,
                        draft,
                        report,
                        IncompleteReason.BUDGET_WALL,
                        step,
                    )
                if state.consecutive_empty >= 3 and state.consecutive_empty % 3 == 0:
                    state.steps.append(
                        _StepRecord(step, "dead_end_warning", _DEAD_END_WARNING)
                    )
                if (
                    self.config.max_steps is not None
                    and self.config.max_steps - step == self.config.answer_reserve_steps
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
                self.last_trace = tuple(state.trace)
                self.last_planner_trace = tuple(state.planner_trace)
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
                # Unknown or unevidenced claims must be rewritten, not silently
                # dropped: their prose may still remain in the answer body.
                known_ids = {entry.evidence.evidence_id for entry in ledger.entries}
                unknown: set[str] = set()
                for claim in claims:
                    unknown |= set(claim.evidence_ids) - known_ids
                unevidenced = [claim.claim_id for claim in claims if not claim.evidence_ids]
                if unknown or unevidenced:
                    details: list[str] = []
                    if unknown:
                        details.append(
                            "شناسه‌های ناشناخته: " + ", ".join(sorted(unknown)[:8])
                        )
                    if unevidenced:
                        details.append(
                            "claimهای بدون شاهد: " + ", ".join(unevidenced[:8])
                        )
                    state.steps.append(
                        _StepRecord(
                            step,
                            "contract_error",
                            "CONTRACT-ERROR: پاسخ شامل claim نامعتبر است؛ متن پاسخ و claims "
                            "را با هم بازنویسی کن و هیچ ادعای بدون شاهد را در متن باقی نگذار.\n"
                            + "؛ ".join(details)
                            + "\nشناسهٔ گره‌های گراف، document_uid آرا و provision_id شاهد "
                            "نیستند؛ فقط مقدار فیلد evidence_id در خروجی ابزارهای کانونیکال "
                            "معتبر است.\n"
                            + _CONTRACT_REMINDER,
                            summary="CONTRACT-ERROR: claim ناشناخته یا بدون شاهد",
                        )
                    )
                    _emit(
                        progress,
                        {
                            "type": "contract_error",
                            "step": step,
                            "message": "unknown or missing evidence ids",
                        },
                    )
                    continue
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
                    self.last_trace = tuple(state.trace)
                    self.last_planner_trace = tuple(state.planner_trace)
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
                    self.last_planner_trace = tuple(state.planner_trace)
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
                        + "\n".join(self._verification_issue_text(issue) for issue in report.issues)
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
                            {
                                "code": issue.code.value,
                                "message": issue.message,
                                "evidence_id": issue.evidence_id,
                                "target_id": issue.target_id,
                            }
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
        self.last_planner_trace = tuple(state.planner_trace)
        return self._incomplete(episode, ledger, state, draft, report, reason, self.config.max_steps or step - 1)

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
        deadline: float | None = None,
    ) -> None:
        started = time.monotonic()
        if tool == "search_provisions":
            args = self._preserve_query_anchor(args, state.query_anchors)
        key = (tool, json.dumps(args, ensure_ascii=False, sort_keys=True))
        cached = state.repeat_cache.get(key)
        if cached is not None:
            state.duplicate_blocks += 1
            # Re-render the cached payload. The original step may have been
            # compacted, so a warning without content would make the requested
            # evidence impossible for the planner to inspect.
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
                    envelope.returned_count, _tool_reason(tool),
                )
            )
            state.steps.append(
                _StepRecord(
                    step,
                    "repeat",
                    f"REPLAY-CACHED {tool} {json.dumps(args, ensure_ascii=False, sort_keys=True)}\n"
                    + json.dumps(envelope.to_payload(), ensure_ascii=False, sort_keys=True)
                    + "\nاین نتیجه از کش بازپخش شد؛ فراخوانی دوبارهٔ همین ابزار و ورودی "
                    "اطلاعات تازه نمی‌دهد. یک سند/ماده را باز کن یا مسیر جست‌وجو را عوض کن.",
                    summary=self._tool_summary(tool, args, envelope) + " [cached]",
                )
            )
            _emit(
                progress,
                {
                    "type": "tool_call", "step": step, "tool": tool,
                    "args": dict(args), "thought": thought, "ok": True,
                    "status": envelope.status.value, "total_count": envelope.total_count,
                    "returned_count": envelope.returned_count, "complete": envelope.complete,
                    "latency_ms": latency, "evidence_count": 0,
                    "cypher": envelope.meta.get("cypher"), "nodes": [], "edges": [],
                    "repeat": True, "reason": _tool_reason(tool),
                },
            )
            return
        signature = self._search_signature(tool, args) if tool in _BROAD_SEARCH_TOOLS else None
        previous = state.last_search_signature
        if (
            signature is not None
            and previous is not None
            and signature[0] == previous[0]
            and signature[2] == previous[2]
            and signature[1]
            and previous[1]
            and len(signature[1] & previous[1]) / len(signature[1] | previous[1]) >= 0.75
        ):
            state.duplicate_blocks += 1
            candidates = "؛ ".join(state.last_search_candidates) or "نامزد قابل اتکا ثبت نشد"
            message = (
                "NEAR-DUPLICATE-SEARCH: این کوئری از نظر واژگان و محدوده تقریباً "
                "همان جست‌وجوی قبلی است و اجرا نشد. نامزدهای قبلی: "
                f"{candidates}. اگر مرتبط‌اند سند/ماده را با find_in_document یا "
                "get_document_structure بررسی کن؛ اگر نامرتبط‌اند مفهوم یا مسیر "
                "را واقعاً عوض کن (مثلاً walk_tree، سند دیگر یا واژگان متفاوت)."
            )
            state.steps.append(_StepRecord(step, "search_pivot", message, summary=message))
            state.trace.append(
                ToolCallTrace(step, tool, dict(args), False, ToolStatus.ERROR.value,
                              "near-duplicate broad search", 0, 0, _tool_reason(tool))
            )
            _emit(progress, {
                "type": "tool_error", "step": step, "tool": tool,
                "error": "near-duplicate broad search",
            })
            return
        if self.config.max_tool_calls is not None and state.tool_calls >= self.config.max_tool_calls:
            state.steps.append(
                _StepRecord(
                    step,
                    "tool_budget",
                    "TOOL-BUDGET-EXHAUSTED: سقف کل فراخوانی ابزار مصرف شده است. "
                    "با شواهد موجود answer بده یا صریحاً abstain کن.",
                    summary="TOOL-BUDGET-EXHAUSTED",
                )
            )
            state.trace.append(
                ToolCallTrace(
                    step,
                    tool,
                    dict(args),
                    False,
                    ToolStatus.ERROR.value,
                    "tool-call budget exhausted",
                    0,
                    0,
                    _tool_reason(tool),
                )
            )
            _emit(
                progress,
                {
                    "type": "tool_error",
                    "step": step,
                    "tool": tool,
                    "error": "tool-call budget exhausted",
                },
            )
            return
        if (
            self.config.max_search_steps is not None
            and state.search_steps >= self.config.max_search_steps
        ):
            state.steps.append(
                _StepRecord(
                    step,
                    "search_budget",
                    "SEARCH-BUDGET-EXHAUSTED: سقف جست‌وجوی سراسری مصرف شده است. "
                    "ابزار دیگری اجرا نمی‌شود؛ با بستهٔ شواهد answer بده یا "
                    "صریحاً abstain کن.",
                    summary="SEARCH-BUDGET-EXHAUSTED",
                )
            )
            state.trace.append(
                ToolCallTrace(
                    step,
                    tool,
                    dict(args),
                    False,
                    ToolStatus.ERROR.value,
                    "broad search budget exhausted",
                    0,
                    0,
                    _tool_reason(tool),
                )
            )
            _emit(
                progress,
                {
                    "type": "tool_error",
                    "step": step,
                    "tool": tool,
                    "error": "broad search budget exhausted",
                },
            )
            return
        state.tool_calls += 1
        if signature is not None:
            state.last_search_signature = signature
        else:
            state.last_search_signature = None
        state.duplicate_blocks = 0
        # The budget limits attempts, not just successful result sets. A timed
        # out or failing broad query still consumed a planner turn and database
        # work; counting only successes lets a small model repeat the same slow
        # search until the global wall-clock deadline.
        if tool in _BROAD_SEARCH_TOOLS:
            state.search_steps += 1
        try:
            wall_remaining = (
                max(0.0, deadline - time.monotonic())
                if deadline is not None
                else self.config.wall_seconds
            )
            remaining = min(wall_remaining, self.config.max_tool_seconds)
            result = self._dispatch_tool_with_deadline(
                state.store, tool, args, remaining
            )
            if result is None:
                hit_wall = wall_remaining <= self.config.max_tool_seconds
                state.deadline_exceeded = hit_wall
                latency = int((time.monotonic() - started) * 1000)
                message = (
                    "research wall-clock deadline exceeded during tool call"
                    if hit_wall
                    else "per-tool time budget exceeded"
                )
                state.trace.append(
                    ToolCallTrace(
                        step,
                        tool,
                        dict(args),
                        False,
                        None,
                        message,
                        latency,
                        0,
                        _tool_reason(tool),
                    )
                )
                state.steps.append(
                    _StepRecord(
                        step,
                        "tool_error",
                        f"مهلت کل پژوهش هنگام اجرای ابزار {tool} پایان یافت.",
                        summary=f"TOOL-TIMEOUT {tool}",
                    )
                )
                _emit(
                    progress,
                    {
                        "type": "tool_error",
                        "step": step,
                        "tool": tool,
                        "args": dict(args),
                        "error": message,
                        "latency_ms": latency,
                    },
                )
                return
        except ToolArgumentError as exc:
            latency = int((time.monotonic() - started) * 1000)
            state.trace.append(
                ToolCallTrace(step, tool, dict(args), False, None, str(exc), latency, 0, _tool_reason(tool))
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
                ToolCallTrace(step, tool, dict(args), False, None, str(exc), latency, 0, _tool_reason(tool))
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
        if signature is not None:
            evidence_ids = frozenset(item.evidence_id for item in result.evidence)
            previous_ids = state.last_search_evidence_ids
            if (
                previous is not None
                and signature[0] == "search_provisions"
                and previous[0] == signature[0]
                and previous_ids is not None
                and not evidence_ids - previous_ids
            ):
                # A different wording that returns the same evidence is still
                # the same route. Restrict this search tool for the next turn,
                # while keeping document reads and alternative routes open.
                state.duplicate_blocks = 1
            state.last_search_evidence_ids = evidence_ids
            state.last_search_candidates = tuple(
                dict.fromkeys(
                    str(item.get("instrument_title") or item.get("title") or "")
                    for item in envelope.items[:3]
                    if isinstance(item, dict)
                    and (item.get("instrument_title") or item.get("title"))
                )
            )
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
            # visited ids feed the notebook capture (docs 06 §7): provisions for
            # canonical tools, graph node ids for the graph tools below.
            if evidence.provision_id not in episode.visited_provisions:
                episode.visited_provisions.append(evidence.provision_id)
            if evidence.provision_id not in episode.visited_nodes:
                episode.visited_nodes.append(evidence.provision_id)
        # Results are ranked best-first. Insert the bounded slice in reverse so
        # the dictionary's newest entries (the ones rendered after compaction)
        # are the highest-ranked evidence, not the tail of a ten-hit page.
        for evidence in reversed(result.evidence[:MAX_EVIDENCE_MEMORY]):
            self._remember_evidence(state, evidence, tool)
        for node in nodes:
            node_id = node.get("id") if isinstance(node, dict) else None
            if isinstance(node_id, str) and node_id and node_id not in episode.visited_nodes:
                episode.visited_nodes.append(node_id)
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
                _tool_reason(tool),
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
        if signature is not None:
            candidates = "؛ ".join(state.last_search_candidates) or "هیچ نامزد مشخصی"
            novelty = (
                "نتیجهٔ تازه‌ای نسبت به جست‌وجوی قبلی نداشت؛ روش یا سند را عوض کن. "
                if state.duplicate_blocks else ""
            )
            state.steps.append(
                _StepRecord(
                    step,
                    "search_assessment",
                    f"بازبینی مسیر: جست‌وجوی اخیر status={envelope.status.value} "
                    f"و {len(result.evidence)} شاهد داد. نامزدهای بالای فهرست: {candidates}. "
                    + novelty
                    + "مرتبط بودن نامزدها را با خود پرسش بسنج؛ اگر مرتبط‌اند به سند/ماده "
                    "برو و متن را بررسی کن. اگر نامرتبط‌اند علت را مشخص کن و مسیر "
                    "یا مفاهیم جست‌وجو را عوض کن. بازنویسی جزئی همین کوئری پیشرفت نیست.",
                    summary=f"SEARCH-ASSESSMENT: {envelope.status.value}; candidates={candidates}",
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
                "reason": _tool_reason(tool),
            },
        )

    def _dispatch_tool_with_deadline(
        self,
        store: RefStore,
        tool: str,
        args: dict[str, Any],
        remaining: float,
    ) -> Any | None:
        """Bound every tool, including tools backed by another LLM/HTTP call."""

        if remaining <= 0:
            return None
        result: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

        def invoke() -> None:
            try:
                result.put((True, self.tools.dispatch(store, tool, args)))
            except Exception as exc:  # noqa: BLE001 - relay on caller thread
                result.put((False, exc))

        threading.Thread(
            target=invoke,
            daemon=True,
            name=f"legal-agent-tool-{tool}",
        ).start()
        try:
            ok, value = result.get(timeout=max(0.001, remaining))
        except queue.Empty:
            return None
        if ok:
            return value
        raise value

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
        # Keep the actual text of read steps in the compacted history: once a
        # step is older than recent_full_steps its full payload is dropped, and
        # without the excerpt the model re-reads the same span again and again.
        excerpt = ""
        if tool in {"read_span", "get_provision_version", "get_provision_text"}:
            for item in envelope.items:
                if isinstance(item, dict) and item.get("text"):
                    text = " ".join(str(item["text"]).split())[:240]
                    excerpt = f" | متن: {text}"
                    break
        return f"TOOL {tool} {args_blob} → {envelope.status.value} ({coverage}){suffix}{excerpt}"

    @classmethod
    def _preserve_query_anchor(
        cls, args: dict[str, Any], anchors: tuple[str, ...]
    ) -> dict[str, Any]:
        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            return args
        folded_query = cls._fold(query)
        missing = next(
            (anchor for anchor in anchors if cls._fold(anchor) not in folded_query),
            None,
        )
        if missing is None:
            return args
        anchored = dict(args)
        anchored["query"] = f"{query.strip()} {missing}"
        return anchored

    @staticmethod
    def _search_signature(
        tool: str, args: dict[str, Any]
    ) -> tuple[str, frozenset[str], tuple[str, ...]] | None:
        query = args.get("query", args.get("topic"))
        if not isinstance(query, str) or not query.strip():
            return None
        terms = frozenset(
            re.findall(r"[\w\u0621-\u06cc]+", query.casefold().replace("\u200c", " "))
        )
        scope = args.get("document_scope") or ()
        if isinstance(scope, str):
            scope = (scope,)
        return tool, terms, tuple(sorted(str(item) for item in scope))

    @staticmethod
    def _remember_evidence(state: _RunState, evidence: Any, tool: str) -> None:
        """Keep a compact canonical excerpt independent of transcript steps."""
        text = (evidence.text or "").strip()
        state.evidence_memory.pop(evidence.evidence_id, None)
        state.evidence_memory[evidence.evidence_id] = {
            "evidence_id": evidence.evidence_id,
            "document_id": evidence.document_id,
            "provision_id": evidence.provision_id,
            "source_span_id": evidence.source_span_id,
            "page": evidence.page,
            "retrieval_method": evidence.retrieval_method,
            "seen_via": tool,
            "text": text[:MAX_EVIDENCE_EXCERPT],
            "truncated": len(text) > MAX_EVIDENCE_EXCERPT,
        }

    @staticmethod
    def _evidence_memory(state: _RunState) -> str:
        if not state.evidence_memory:
            return "بستهٔ شواهد پایدار: خالی است."
        rows = list(state.evidence_memory.values())[-MAX_EVIDENCE_MEMORY:]
        return (
            "بستهٔ شواهد پایدار (متن این بخش حتی پس از خلاصه‌سازی معتبر است):\n"
            + json.dumps(rows, ensure_ascii=False, sort_keys=True)
        )

    def _stage_directive(self, state: _RunState) -> str:
        if state.duplicate_blocks >= 2:
            return (
                "مرحلهٔ فعلی: مسیر فعلی پیشرفت نکرده است. ابزار یا سند دیگری را "
                "با هدف مشخص انتخاب کن؛ جست‌وجوی تقریباً یکسان را تکرار نکن. "
                "اگر هیچ مسیر معنادار دیگری نمانده، امتناع مستدل بده."
            )
        if not state.evidence_memory:
            if state.orientation_targets:
                has_strong = any(
                    item.get("confidence") == "strong"
                    for item in state.orientation_targets
                )
                if not has_strong:
                    return (
                        "مرحلهٔ فعلی: بازیابی سند. نامزدهای جهت‌گیری confidence=weak "
                        "دارند؛ آن‌ها را سند منتخب فرض نکن. یک جست‌وجوی محدود با ۲ تا ۴ "
                        "مفهوم حقوقی متمایز انجام بده و سپس داخل سند نتیجه پیمایش کن."
                    )
                return (
                    "مرحلهٔ فعلی: انتخاب و بازکردن سند. از target_id/عنوان نامزدهای "
                    "جهت‌گیری برای get_document_structure یا find_in_document استفاده کن. "
                    "اگر نامزدها نامرتبط بودند، یک جست‌وجوی محدود بازیابی انجام بده."
                )
            return "مرحلهٔ فعلی: بازیابی سند مرتبط با یک کوئری کوتاه حقوقی."
        if (
            self.config.max_search_steps is not None
            and state.search_steps >= self.config.max_search_steps
        ):
            return (
                "مرحلهٔ فعلی: تکمیل و پاسخ. بودجهٔ جست‌وجو مصرف شده است؛ جست‌وجوی "
                "یا ابزار دیگری نزن. متن شواهد پایدار را ارزیابی کن و همین حالا "
                "answer یا abstain بده."
            )
        return (
            "مرحلهٔ فعلی: ارزیابی کفایت شواهد. اگر متن موجود همهٔ وجوه را پوشش می‌دهد "
            "answer بده؛ در غیر این صورت فقط شکاف مشخص بعدی را در همان سند هدف بگیر."
        )

    @staticmethod
    def _verification_issue_text(issue: Any) -> str:
        details = [f"code={issue.code.value}", issue.message]
        if issue.evidence_id:
            details.append(f"evidence_id={issue.evidence_id}")
        if issue.target_id:
            details.append(f"target_id={issue.target_id}")
        return "- " + " | ".join(details)

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
        deadline: float,
        step: int,
        progress: Callable[[dict], None] | None,
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
                "معتبرند؛ متن شواهد منتخب در بستهٔ پایدار پایین باقی مانده است.)\n"
                if recent > 0
                else ""
            )
            transcript = "\n\n".join(
                (
                    header,
                    "تاریخچه‌ی پژوهش تا این لحظه:\n" + notice,
                    *lines,
                    self._stage_directive(state),
                    self._evidence_memory(state),
                    self._evidence_nudge(ledger),
                )
            )
        else:
            transcript = "\n\n".join(
                (header, "هیچ گامی انجام نشده؛ شروع کن.", self._stage_directive(state))
            )
        last_error: DomainError | None = None
        for attempt in range(3):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                state.deadline_exceeded = True
                return None
            started = time.monotonic()
            try:
                turn = self._planner_decide_with_deadline(
                    system_prompt,
                    transcript,
                    remaining,
                    excluded_tools=(
                        frozenset({state.last_search_signature[0]})
                        if state.duplicate_blocks
                        and state.last_search_signature is not None
                        else frozenset()
                    ),
                    force_answer=(
                        (
                            self.config.max_steps is not None
                            and step >= self.config.max_steps
                        )
                        # Explicit evaluation caps remain optional. Normal
                        # runs only force finalisation after a repeated,
                        # unproductive route ignores the pivot feedback.
                        or (
                            self.config.max_search_steps is not None
                            and state.search_steps >= self.config.max_search_steps
                        )
                        or (
                            self.config.max_tool_calls is not None
                            and state.tool_calls >= self.config.max_tool_calls
                        )
                        or state.duplicate_blocks >= 3
                    ),
                )
                if turn is None:
                    latency = int((time.monotonic() - started) * 1000)
                    trace = PlannerCallTrace(
                        step,
                        attempt + 1,
                        False,
                        latency,
                        error="research wall-clock deadline exceeded",
                    )
                    state.planner_trace.append(trace)
                    _emit(progress, _planner_event(trace))
                    state.deadline_exceeded = True
                    return None
                if time.monotonic() >= deadline:
                    latency = int((time.monotonic() - started) * 1000)
                    trace = PlannerCallTrace(
                        step,
                        attempt + 1,
                        False,
                        latency,
                        provider_id=turn.provider_id,
                        model=turn.model,
                        error="research wall-clock deadline exceeded after response",
                    )
                    state.planner_trace.append(trace)
                    _emit(progress, _planner_event(trace))
                    state.deadline_exceeded = True
                    return None
                latency = int((time.monotonic() - started) * 1000)
                trace = PlannerCallTrace(
                    step,
                    attempt + 1,
                    True,
                    latency,
                    turn.input_tokens,
                    turn.output_tokens,
                    turn.provider_id,
                    turn.model,
                    turn.decision.action if turn.decision is not None else None,
                    turn.contract_error,
                    tool=(
                        turn.decision.tool
                        if turn.decision is not None
                        and turn.decision.action == "tool"
                        else None
                    ),
                )
                state.planner_trace.append(trace)
                _emit(progress, _planner_event(trace))
                return turn
            except DomainError as exc:
                last_error = exc
                latency = int((time.monotonic() - started) * 1000)
                trace = PlannerCallTrace(
                    step,
                    attempt + 1,
                    False,
                    latency,
                    error=str(exc),
                )
                state.planner_trace.append(trace)
                _emit(progress, _planner_event(trace))
                if attempt < 2:
                    delay = min(0.5 * (attempt + 1), max(0.0, deadline - time.monotonic()))
                    if delay <= 0:
                        state.deadline_exceeded = True
                        return None
                    time.sleep(delay)
                    continue
        episode.identified_issues.append(IncompleteReason.LLM_ERROR.value)
        state.steps.append(
            _StepRecord(0, "llm_error", f"planner failure after retry: {last_error}")
        )
        return None

    def _planner_decide_with_deadline(
        self,
        system_prompt: str,
        transcript: str,
        remaining: float,
        *,
        excluded_tools: frozenset[str] = frozenset(),
        force_answer: bool = False,
    ) -> PlannerTurn | None:
        """Hard wall-clock guard around providers whose streams keep sockets alive.

        The provider still receives the bounded socket timeout. The daemon
        wrapper handles the harder case where continuous bytes prevent the HTTP
        iterator from returning control before the research deadline.
        """
        result: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

        def invoke() -> None:
            try:
                bounded_decide = getattr(self.planner, "decide_with_timeout", None)
                if callable(bounded_decide):
                    kwargs: dict[str, Any] = {"timeout_seconds": remaining}
                    parameters = inspect.signature(bounded_decide).parameters
                    if force_answer and "force_answer" in parameters:
                        kwargs["force_answer"] = True
                    if excluded_tools and "excluded_tools" in parameters:
                        kwargs["excluded_tools"] = excluded_tools
                    value = bounded_decide(system_prompt, transcript, **kwargs)
                else:
                    kwargs = {}
                    parameters = inspect.signature(self.planner.decide).parameters
                    if force_answer and "force_answer" in parameters:
                        kwargs["force_answer"] = True
                    if excluded_tools and "excluded_tools" in parameters:
                        kwargs["excluded_tools"] = excluded_tools
                    value = self.planner.decide(system_prompt, transcript, **kwargs)
                result.put((True, value))
            except Exception as exc:  # noqa: BLE001 - relay on caller thread
                result.put((False, exc))

        threading.Thread(target=invoke, daemon=True, name="legal-agent-planner").start()
        try:
            ok, value = result.get(timeout=max(0.001, remaining))
        except queue.Empty:
            return None
        if ok:
            return value
        raise value

    def _seed_orientation(
        self,
        state: _RunState,
        question: str,
        applicable_time: Any,
        progress: Any,
        deadline: float,
    ) -> None:
        """Prepend a deterministic orientation map to the transcript.

        Like OpenCode injecting notebook memory into context, this guarantees
        the agent *sees* the conceptual tree and learned routes even if it would
        otherwise jump straight to lexical search. Best-effort; never raises.
        The agent is free to ignore it when it is not useful.
        """
        blocks: list[str] = []
        walk = self._safe_tool(
            state,
            "walk_tree",
            {"topic": question, "limit": 5},
            timeout_seconds=min(1.0, max(0.001, deadline - time.monotonic())),
        )
        if walk is not None and walk.status is ToolStatus.OK and walk.items:
            lines = []
            for item in walk.items:
                path = " / ".join(step.get("label") or "?" for step in item.get("path", []))
                target_id = item.get("target_id")
                target_label = item.get("target_label")
                kind = item.get("kind")
                confidence = item.get("route_confidence", "weak")
                matched = ", ".join(item.get("matched_tokens") or [])
                lines.append(
                    f"- target_id={target_id} | kind={kind} | confidence={confidence} "
                    f"| matched={matched or '-'} | title={target_label} | path={path}"
                )
                if isinstance(target_id, str) and isinstance(target_label, str):
                    state.orientation_targets.append(
                        {
                            "target_id": target_id,
                            "title": target_label,
                            "kind": str(kind or ""),
                            "confidence": str(confidence),
                        }
                    )
            if lines:
                blocks.append("مسیر(های) درخت مفهومی برای این موضوع:\n" + "\n".join(lines))
            # Pre-open the best instrument outline. This turns the orientation
            # from a prose suggestion into an actionable document route without
            # spending another model decision.
            first = state.orientation_targets[0] if state.orientation_targets else None
            best = (
                first
                if first is not None
                and first["kind"] == "instrument"
                and first["confidence"] == "strong"
                else None
            )
            if best is not None:
                structure = self._safe_tool(
                    state,
                    "get_document_structure",
                    {
                        "document": best["target_id"],
                        "applicable_time": applicable_time.isoformat(),
                    },
                    timeout_seconds=min(3.0, max(0.001, deadline - time.monotonic())),
                )
                if structure is not None and structure.status is ToolStatus.OK:
                    blocks.append(
                        "ساختار نامزد اول با شناسهٔ مستقیم:\n"
                        + json.dumps(structure.to_payload(), ensure_ascii=False, sort_keys=True)
                    )

            # Notebook data is a navigation hint only. Open the best candidate
            # deterministically so a small model need not spend a turn loading
            # a skill/page before it can act on the route.
            if best is not None:
                notebook = self._safe_tool(
                    state,
                    "open_notebook",
                    {"node_id": best["target_id"]},
                    timeout_seconds=min(1.0, max(0.001, deadline - time.monotonic())),
                )
                if notebook is not None and notebook.status is ToolStatus.OK and notebook.items:
                    blocks.append(
                        "راهنمای دفترچه برای نامزد اول (فقط مسیر، نه شاهد):\n"
                        + json.dumps(notebook.to_payload(), ensure_ascii=False, sort_keys=True)
                    )
        if blocks:
            state.steps.append(
                _StepRecord(
                    0,
                    "orientation",
                    "## جهت‌گیری اولیه (نقشهٔ ناوبری)\n"
                    + "\n\n".join(blocks)
                    + "\n\nاگر یکی از این اسناد مربوط بود، مستقیم با "
                    "`get_document_structure(document=\"<target_id>\")` بازش کن و با "
                    "list_children/search_in_subtree داخلش پایین برو؛ سپس ادعا را با evidence "
                    "مستند کن. اگر این نقشه به پرسش مربوط نبود، نادیده بگیر و مسیر معمول را ادامه بده.",
                    summary="ORIENTATION: actionable tree targets + notebook hint",
                )
            )
            _emit(progress, {"type": "orientation", "text": " | ".join(blocks)[:800]})

    def _safe_tool(
        self,
        state: _RunState,
        name: str,
        args: dict[str, Any],
        *,
        timeout_seconds: float = 2.0,
    ) -> Any:
        available = getattr(self.tools, "spec_available", None)
        if callable(available) and not available(name):
            return None
        result: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

        def invoke() -> None:
            try:
                # Orientation is advisory and never evidence; isolate its refs
                # so a timed-out worker cannot mutate the live research store.
                result.put((True, self.tools.dispatch(RefStore(), name, args).envelope))
            except Exception as exc:  # noqa: BLE001 - relay below
                result.put((False, exc))

        threading.Thread(
            target=invoke,
            daemon=True,
            name=f"legal-agent-orientation-{name}",
        ).start()
        try:
            ok, value = result.get(timeout=max(0.001, timeout_seconds))
        except queue.Empty:
            return None
        return value if ok else None

    def _system_prompt(self) -> str:
        native_schemas = tuple(getattr(self.planner, "tools", ()))
        native_names = frozenset(
            str(schema.get("function", {}).get("name"))
            for schema in native_schemas
            if isinstance(schema, dict) and schema.get("function", {}).get("name")
        )
        compact_catalog = getattr(self.tools, "compact_tool_catalog", None)
        catalog = (
            compact_catalog(native_names)
            if native_names and callable(compact_catalog)
            else self.tools.tool_catalog()
        )
        return _SYSTEM_TEMPLATE.format(
            budget_policy=(
                "سقفِ پیکربندی‌شده: "
                + ", ".join(
                    item
                    for item in (
                        f"{self.config.max_search_steps} جست‌وجوی سراسری"
                        if self.config.max_search_steps is not None else "",
                        f"{self.config.max_tool_calls} فراخوانی ابزار"
                        if self.config.max_tool_calls is not None else "",
                        f"{self.config.max_steps} تصمیم مدل"
                        if self.config.max_steps is not None else "",
                    )
                    if item
                )
                + "."
                if any(
                    value is not None
                    for value in (
                        self.config.max_search_steps,
                        self.config.max_tool_calls,
                        self.config.max_steps,
                    )
                )
                else "تا زمانی که شواهد تازه یا مسیر معنادار پیدا می‌شود تحقیق را ادامه بده؛ "
                "کوئری‌های تقریباً یکسان را تکرار نکن. پس از بررسی مسیرهای مرتبط، "
                "پاسخ مستند یا امتناع مستدل بده."
            ),
            tool_catalog=catalog,
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
            # notebook navigation tools (shortcut layer, never evidence)
            "open_notebook": ResearchActionType.OPENED,
            "notebook_route": ResearchActionType.FOLLOWED_REFERENCE,
            "notebook_index": ResearchActionType.SEARCHED,
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
        self.last_planner_trace = tuple(state.planner_trace)
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


def _planner_event(trace: PlannerCallTrace) -> dict[str, Any]:
    return {
        "type": "planner_call",
        "step": trace.step,
        "attempt": trace.attempt,
        "ok": trace.ok,
        "latency_ms": trace.latency_ms,
        "input_tokens": trace.input_tokens,
        "output_tokens": trace.output_tokens,
        "provider_id": trace.provider_id,
        "model": trace.model,
        "action": trace.action,
        "tool": trace.tool,
        "error": trace.error,
    }


def _query_anchors(question: str) -> tuple[str, ...]:
    folded = question.casefold().replace("\u200c", "").replace("\u200f", "")
    return tuple(
        anchor
        for anchor in _LEGAL_QUERY_ANCHORS
        if anchor.casefold().replace("\u200c", "") in folded
    )


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
