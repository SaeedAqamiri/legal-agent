"""Read-only research tools over the canonical and research-graph ports.

Every tool returns a :class:`ToolResult` — an honest pagination envelope plus
the canonical ``Evidence`` objects built from what the tool actually returned.
Ordering is deterministic everywhere so identical questions page identically.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from ..errors import DomainError, NotFoundError
from ..repositories import CanonicalRepository, ResearchGraphRepository
from ..research import Evidence
from ..retrieval import search_tokens
from .envelope import Envelope, RefStore, ToolStatus, empty_envelope, make_envelope

EXCERPT_CHARS = 280


class ToolArgumentError(DomainError):
    """A planner-supplied tool argument failed strict validation."""


@dataclass(frozen=True, slots=True)
class ToolResult:
    envelope: Envelope
    evidence: tuple[Evidence, ...] = ()


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    args: tuple[str, ...]

    def signature(self) -> str:
        return f"{self.name}({', '.join(self.args)})"


TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "search_provisions",
        "جست‌وجوی متنی مواد/متن‌های قانونی با gate زمانی و scope؛ مرتب‌سازی قطعی",
        ("query", "applicable_time", "document_scope?", "cursor?"),
    ),
    ToolSpec(
        "get_provision_version",
        "نمایش کامل یک نسخه‌ی ماده با متن، span و وضعیت",
        ("provision_version_id", "applicable_time"),
    ),
    ToolSpec(
        "get_version_history",
        "فهرست همه‌ی نسخه‌های یک ماده با بازه‌ی اعتبار و زنجیره‌ی نسخه‌گذاری",
        ("provision_id", "applicable_time"),
    ),
    ToolSpec(
        "get_references",
        "ارجاع‌های صریح یک نسخه و اهداف resolve‌شده‌ی آن‌ها",
        ("provision_version_id", "applicable_time"),
    ),
    ToolSpec(
        "temporal_check",
        "نسخه‌های قابل اعمال یک ماده در تاریخ مشخص؛ EMPTY یعنی هیچ نسخه‌ای معتبر نیست",
        ("provision_id", "applicable_time"),
    ),
    ToolSpec(
        "aggregate_count",
        "شمارش قطعی نسخه‌های منطبق با پرسش (سمت سرور، بدون صفحه‌بندی توسط مدل)",
        ("query", "applicable_time", "document_scope?"),
    ),
    ToolSpec(
        "find_in_document",
        "grep داخل یک سند (یا کل کورپوس) با صفحه‌ی دقیق؛ هر واژه‌ی پرسش باید در تکه باشد",
        ("query", "applicable_time", "document?", "page_from?", "page_to?", "cursor?"),
    ),
    ToolSpec(
        "read_span",
        "خواندن کامل یک تکه با صفحه‌ی دقیق؛ source_span_id یا evidence_id می‌پذیرد — برای cite از evidence_id برگشتی استفاده کنید",
        ("source_span_id", "applicable_time"),
    ),
    ToolSpec(
        "list_document_contents",
        "فهرست مواد و بخش‌های یک سند با صفحه (فهرست محتوا)",
        ("document", "applicable_time"),
    ),
    ToolSpec(
        "list_documents",
        "مرور کتابخانه اسناد بر اساس عنوان/موضوع",
        ("query?", "applicable_time"),
    ),
    ToolSpec(
        "get_related",
        "همسایه‌های یک ماده در گراف: ارجاع‌های صریح خروجی و ورودی",
        ("provision_id", "applicable_time"),
    ),
    ToolSpec(
        "expand_search_terms",
        "تولید کوئری‌های جایگزین با واژگان حقوقی وقتی جست‌وجوی واژگانی بن‌بست شد",
        ("query",),
    ),
    ToolSpec(
        "semantic_search",
        "جست‌وجوی معنایی برداری روی تکه‌ها؛ برای وقتی واژه‌های پرسش در متن نیستند",
        ("query", "applicable_time", "document?", "cursor?"),
    ),
)

_TOOL_NAMES = frozenset(spec.name for spec in TOOL_SPECS)


def tool_catalog(tools: LegalResearchTools | None = None) -> str:
    """Planner-facing catalog appended to the system prompt.

    With a ``LegalResearchTools`` instance, capability-gated tools are listed
    only when configured (an unconfigured tool just wastes planner steps on
    guaranteed ERROR envelopes). Without an instance, every tool is listed.
    """
    lines = []
    for item in TOOL_SPECS:
        if tools is not None and not tools.spec_available(item.name):
            continue
        lines.append(item.signature() + " — " + item.description)
    return "\n".join(lines)


def is_known_tool(name: str) -> bool:
    return name in _TOOL_NAMES


class LegalResearchTools:
    """Deterministic read-only tool surface shared by the loop and MCP."""

    def __init__(
        self,
        canonical: CanonicalRepository,
        research_graph: ResearchGraphRepository | None = None,
        items_per_page: int = 10,
        term_expander: Any | None = None,
        semantic_index: Any | None = None,
    ) -> None:
        if items_per_page < 1:
            raise DomainError("items_per_page must be positive")
        self.canonical = canonical
        self.research_graph = research_graph
        self.items_per_page = items_per_page
        self.term_expander = term_expander
        self.semantic_index = semantic_index
        #: evidence_id -> source_span_id, populated as tools build evidence so
        #: read_span can accept either identifier.
        self.evidence_index: dict[str, str] = {}

    def dispatch(self, store: RefStore, name: str, args: dict[str, Any]) -> ToolResult:
        if not is_known_tool(name):
            raise ToolArgumentError(f"unknown tool {name!r}")
        if not isinstance(args, dict):
            raise ToolArgumentError("tool args must be an object")
        handler = getattr(self, f"_{name}")
        return handler(store, args)

    def spec_available(self, name: str) -> bool:
        """Capability gates: LLM-dependent and embedding-dependent tools are
        only advertised when their backing service is configured."""
        if name == "semantic_search":
            return self.semantic_index is not None
        if name == "expand_search_terms":
            return self.term_expander is not None
        return True

    def tool_catalog(self) -> str:
        return tool_catalog(self)

    # ------------------------------------------------------------------ tools

    def _search_provisions(self, store: RefStore, args: dict[str, Any]) -> ToolResult:
        query = require_str(args, "query")
        applicable_time = require_date(args, "applicable_time")
        scope = optional_scope(args)
        cursor = optional_cursor(args)
        allowed = set(args) - {"query", "applicable_time", "document_scope", "cursor"}
        if allowed:
            raise ToolArgumentError(f"unexpected tool args: {', '.join(sorted(allowed))}")

        query_tokens = search_tokens(query)
        if not query_tokens:
            raise ToolArgumentError("query must contain searchable text")

        scored: list[tuple[float, str]] = []
        versions: dict[str, tuple[Any, Any, Any]] = {}
        best_spans: dict[str, Any] = {}
        scope_unresolved = set(scope)
        for version in self.canonical.list_provision_versions():
            if not self._applicable(version, applicable_time):
                continue
            provision = self.canonical.get_provision(version.provision_id)
            document_version = self.canonical.get_document_version(version.document_version_id)
            if not document_version.applies_at(applicable_time):
                continue
            instrument = self.canonical.get_instrument(provision.instrument_id)
            allowed, matched = self._scope_allows(
                scope, provision, version, document_version.source_document_id, instrument
            )
            if not allowed:
                continue
            scope_unresolved -= matched
            spans = self.canonical.source_spans_for_version(version.provision_version_id)
            if not spans:
                continue
            # Cite the page the passage actually lives on, not the provision's
            # first page: pick the span with the best token overlap.
            span = max(
                spans,
                key=lambda item: len(search_tokens(item.raw_text) & query_tokens),
            )
            lexical = _overlap(query_tokens, search_tokens(version.normalized_text))
            lexical = max(lexical, _overlap(query_tokens, search_tokens(span.raw_text)))
            metadata_text = " ".join(
                filter(
                    None,
                    (
                        provision.label,
                        provision.number,
                        provision.title or "",
                        instrument.title,
                        instrument.canonical_title,
                        instrument.subject_domain or "",
                    ),
                )
            )
            metadata = _overlap(query_tokens, search_tokens(metadata_text))
            score = 0.8 * lexical + 0.2 * metadata
            if score <= 0:
                continue
            versions[version.provision_version_id] = (version, provision, instrument)
            best_spans[version.provision_version_id] = span
            scored.append((score, version.provision_version_id))

        scored.sort(key=lambda item: (-item[0], item[1]))
        total = len(scored)
        page = cursor or 1
        start = (page - 1) * self.items_per_page
        page_rows = scored[start : start + self.items_per_page]
        if not page_rows:
            empty_meta: dict[str, Any] = {"query": query, "page": page}
            if scope and scope_unresolved:
                empty_meta["scope_unresolved"] = sorted(scope_unresolved)
                empty_meta["scope_hint"] = (
                    "این مقادیر document_scope با هیچ شناسه یا عنوان سندی تطبیق نشدند؛ "
                    "عنوان دقیق را از نتایج جست‌وجو بردارید یا scope را حذف کنید."
                )
            return ToolResult(
                empty_envelope(
                    store,
                    ToolStatus.EMPTY,
                    meta=empty_meta,
                )
                if total == 0
                else empty_envelope(
                    store,
                    ToolStatus.NOT_FOUND,
                    (f"page {page} is beyond {total} results",),
                )
            )

        items: list[dict[str, Any]] = []
        evidence: list[Evidence] = []
        referenced: list[dict[str, Any]] = []
        for score, version_id in page_rows:
            version, provision, instrument = versions[version_id]
            span = best_spans[version_id]
            # reason must stay constant per (version|span|time|method): the
            # ledger deduplicates by that identity and compares full equality,
            # so query-dependent reasons would raise a conflict on re-retrieval.
            ev, aux_evidence, aux_items = self._evidence_for_version(
                version,
                applicable_time,
                method="agentic_search",
                reason="lexical search match",
                preferred_span=span,
            )
            evidence.append(ev)
            evidence.extend(aux_evidence)
            items.append(
                self._compact_item(version, provision, instrument, span, ev, score)
            )
            referenced.extend(aux_items)

        has_next = start + self.items_per_page < total
        meta: dict[str, Any] = {"query": query, "page": page, "referenced": referenced}
        if scope and scope_unresolved:
            meta["scope_unresolved"] = sorted(scope_unresolved)
            meta["scope_hint"] = (
                "این مقادیر document_scope با هیچ شناسه یا عنوان سندی تطبیق نشدند؛ "
                "عنوان دقیق را از نتایج جست‌وجو بردارید یا scope را حذف کنید."
            )
        envelope = make_envelope(
            store,
            tuple(items),
            total_count=total,
            has_next_page=has_next,
            next_cursor=f"p{page + 1}" if has_next else None,
            meta=meta,
        )
        return ToolResult(envelope, tuple(evidence))

    def _get_provision_version(self, store: RefStore, args: dict[str, Any]) -> ToolResult:
        version_id = require_str(args, "provision_version_id")
        applicable_time = require_date(args, "applicable_time")
        if set(args) - {"provision_version_id", "applicable_time"}:
            raise ToolArgumentError("unexpected tool args")
        try:
            version = self.canonical.get_provision_version(version_id)
        except NotFoundError:
            return ToolResult(
                empty_envelope(store, ToolStatus.NOT_FOUND, (f"unknown version {version_id!r}",))
            )
        provision = self.canonical.get_provision(version.provision_id)
        instrument = self.canonical.get_instrument(provision.instrument_id)
        document_version = self.canonical.get_document_version(version.document_version_id)
        spans = self.canonical.source_spans_for_version(version_id)
        span = spans[0] if spans else None
        ev, aux_evidence, aux_items = self._evidence_for_version(
            version,
            applicable_time,
            method="agentic_direct",
            reason="direct open",
        )
        item = {
            "evidence_id": ev.evidence_id if ev else None,
            "provision_version_id": version.provision_version_id,
            "provision_id": provision.provision_id,
            "document_version_id": document_version.document_version_id,
            "source_span_id": span.source_span_id if span else None,
            "label": provision.label,
            "number": provision.number,
            "provision_type": provision.provision_type.value,
            "instrument_title": instrument.title,
            "status": version.status.value,
            "effective_from": _iso(version.effective_from),
            "effective_to": _iso(version.effective_to),
            "applies_at_time": version.applies_at(applicable_time),
            "text": version.text,
            "page": span.page_number if span else None,
            # All pages this provision occupies (page-level spans), so the
            # planner can cite or read the exact page a passage lives on.
            "spans": [
                {
                    "source_span_id": item_span.source_span_id,
                    "page": item_span.page_number,
                    "char_start": item_span.char_start,
                    "char_end": item_span.char_end,
                }
                for item_span in spans
            ],
        }
        envelope = make_envelope(
            store,
            (item,),
            meta={"referenced": aux_items},
        )
        return ToolResult(envelope, tuple(e for e in (ev, *aux_evidence) if e is not None))

    def _get_version_history(self, store: RefStore, args: dict[str, Any]) -> ToolResult:
        provision_id = require_str(args, "provision_id")
        applicable_time = require_date(args, "applicable_time")
        if set(args) - {"provision_id", "applicable_time"}:
            raise ToolArgumentError("unexpected tool args")
        try:
            provision = self.canonical.get_provision(provision_id)
        except NotFoundError:
            return ToolResult(
                empty_envelope(store, ToolStatus.NOT_FOUND, (f"unknown provision {provision_id!r}",))
            )
        instrument = self.canonical.get_instrument(provision.instrument_id)
        rows = [
            version
            for version in self.canonical.list_provision_versions()
            if version.provision_id == provision_id
        ]
        rows.sort(key=lambda item: (item.effective_from or date.min, item.provision_version_id))
        if not rows:
            return ToolResult(empty_envelope(store, ToolStatus.EMPTY))
        items = []
        for version in rows:
            items.append(
                {
                    "provision_version_id": version.provision_version_id,
                    "document_version_id": version.document_version_id,
                    "status": version.status.value,
                    "effective_from": _iso(version.effective_from),
                    "effective_to": _iso(version.effective_to),
                    "supersedes_version_id": version.supersedes_version_id,
                    "applies_at_time": version.applies_at(applicable_time),
                    "text_excerpt": _excerpt(version.text),
                }
            )
        envelope = make_envelope(
            store,
            tuple(items),
            total_count=len(items),
            meta={"provision_id": provision_id, "instrument_title": instrument.title},
        )
        return ToolResult(envelope)

    def _get_references(self, store: RefStore, args: dict[str, Any]) -> ToolResult:
        from ..canonical import ResolutionStatus

        version_id = require_str(args, "provision_version_id")
        applicable_time = require_date(args, "applicable_time")
        if set(args) - {"provision_version_id", "applicable_time"}:
            raise ToolArgumentError("unexpected tool args")
        try:
            self.canonical.get_provision_version(version_id)
        except NotFoundError:
            return ToolResult(
                empty_envelope(store, ToolStatus.NOT_FOUND, (f"unknown version {version_id!r}",))
            )
        references = self.canonical.references_from_version(version_id)
        if not references:
            return ToolResult(empty_envelope(store, ToolStatus.EMPTY))

        items: list[dict[str, Any]] = []
        evidence: list[Evidence] = []
        referenced: list[dict[str, Any]] = []
        for reference in sorted(references, key=lambda item: item.reference_id):
            items.append(
                {
                    "reference_id": reference.reference_id,
                    "target_text": reference.target_text,
                    "resolution_status": reference.resolution_status.value,
                    "resolved_target_provision_id": reference.resolved_target_provision_id,
                }
            )
            if reference.resolution_status == ResolutionStatus.RESOLVED and reference.resolved_target_provision_id:
                for target_version in self.canonical.applicable_provision_versions(
                    reference.resolved_target_provision_id, applicable_time
                ):
                    ev, aux_evidence, aux_items = self._evidence_for_version(
                        target_version,
                        applicable_time,
                        method="agentic_reference",
                        reason="resolved explicit reference target",
                    )
                    if ev is None:
                        continue
                    provision = self.canonical.get_provision(target_version.provision_id)
                    instrument = self.canonical.get_instrument(provision.instrument_id)
                    span = self.canonical.source_spans_for_version(
                        target_version.provision_version_id
                    )[0]
                    evidence.append(ev)
                    evidence.extend(aux_evidence)
                    referenced.append(
                        self._compact_item(
                            target_version, provision, instrument, span, ev, None
                        )
                    )
                    referenced.extend(aux_items)
        envelope = make_envelope(
            store,
            tuple(items),
            total_count=len(items),
            meta={"referenced": referenced},
        )
        return ToolResult(envelope, tuple(evidence))

    def _temporal_check(self, store: RefStore, args: dict[str, Any]) -> ToolResult:
        provision_id = require_str(args, "provision_id")
        applicable_time = require_date(args, "applicable_time")
        if set(args) - {"provision_id", "applicable_time"}:
            raise ToolArgumentError("unexpected tool args")
        try:
            self.canonical.get_provision(provision_id)
        except NotFoundError:
            return ToolResult(
                empty_envelope(store, ToolStatus.NOT_FOUND, (f"unknown provision {provision_id!r}",))
            )
        applicable = self.canonical.applicable_provision_versions(provision_id, applicable_time)
        if not applicable:
            return ToolResult(
                empty_envelope(
                    store,
                    ToolStatus.EMPTY,
                    meta={
                        "provision_id": provision_id,
                        "applicable_time": applicable_time.isoformat(),
                        "meaning": "هیچ نسخه‌ای در این تاریخ اعتبار ندارد",
                    },
                )
            )
        items = []
        evidence: list[Evidence] = []
        referenced: list[dict[str, Any]] = []
        for version in sorted(applicable, key=lambda item: item.provision_version_id):
            ev, aux_evidence, aux_items = self._evidence_for_version(
                version,
                applicable_time,
                method="agentic_temporal",
                reason="applicable version check",
            )
            provision = self.canonical.get_provision(version.provision_id)
            instrument = self.canonical.get_instrument(provision.instrument_id)
            spans = self.canonical.source_spans_for_version(version.provision_version_id)
            if ev is not None and spans:
                evidence.append(ev)
                evidence.extend(aux_evidence)
                items.append(
                    self._compact_item(version, provision, instrument, spans[0], ev, None)
                )
                referenced.extend(aux_items)
            else:
                items.append(
                    {
                        "evidence_id": None,
                        "provision_version_id": version.provision_version_id,
                        "document_version_id": version.document_version_id,
                        "label": provision.label,
                        "number": provision.number,
                        "instrument_title": instrument.title,
                        "status": version.status.value,
                        "text_excerpt": _excerpt(version.text),
                    }
                )
        envelope = make_envelope(
            store,
            tuple(items),
            total_count=len(items),
            meta={"applicable_time": applicable_time.isoformat(), "referenced": referenced},
        )
        return ToolResult(envelope, tuple(evidence))

    def _aggregate_count(self, store: RefStore, args: dict[str, Any]) -> ToolResult:
        query = require_str(args, "query")
        applicable_time = require_date(args, "applicable_time")
        scope = optional_scope(args)
        allowed = set(args) - {"query", "applicable_time", "document_scope"}
        if allowed:
            raise ToolArgumentError(f"unexpected tool args: {', '.join(sorted(allowed))}")
        query_tokens = search_tokens(query)
        if not query_tokens:
            raise ToolArgumentError("query must contain searchable text")
        matched = 0
        for version in self.canonical.list_provision_versions():
            if not self._applicable(version, applicable_time):
                continue
            provision = self.canonical.get_provision(version.provision_id)
            document_version = self.canonical.get_document_version(version.document_version_id)
            if not document_version.applies_at(applicable_time):
                continue
            instrument = self.canonical.get_instrument(provision.instrument_id)
            allowed, _ = self._scope_allows(
                scope, provision, version, document_version.source_document_id, instrument
            )
            if not allowed:
                continue
            if _overlap(query_tokens, search_tokens(version.normalized_text)) > 0:
                matched += 1
        envelope = make_envelope(
            store,
            (
                {
                    "query": query,
                    "applicable_time": applicable_time.isoformat(),
                    "matched_provision_versions": matched,
                },
            ),
            total_count=1,
            meta={"deterministic": True},
        )
        return ToolResult(envelope)

    def _find_in_document(self, store: RefStore, args: dict[str, Any]) -> ToolResult:
        """grep over one document (or the whole corpus) with page-accurate hits."""
        query = require_str(args, "query")
        applicable_time = require_date(args, "applicable_time")
        document = optional_str(args, "document")
        page_from = optional_int(args, "page_from")
        page_to = optional_int(args, "page_to")
        cursor = optional_cursor(args)
        allowed = set(args) - {"query", "applicable_time", "document", "page_from", "page_to", "cursor"}
        if allowed:
            raise ToolArgumentError(f"unexpected tool args: {', '.join(sorted(allowed))}")
        query_tokens = search_tokens(query)
        if not query_tokens:
            raise ToolArgumentError("query must contain searchable text")
        if page_from is not None and page_from < 1:
            raise ToolArgumentError("page_from must be one-based")
        if page_to is not None and page_from is not None and page_to < page_from:
            raise ToolArgumentError("page_to must not be before page_from")

        allowed_versions = self._resolve_document_versions(document, applicable_time)
        if document is not None and not allowed_versions:
            return ToolResult(
                empty_envelope(
                    store,
                    ToolStatus.NOT_FOUND,
                    (f"no document matched {document!r}",),
                    meta={"document": document},
                )
            )

        strict_matches: list[tuple[float, Any, Any, Any, Any]] = []
        partial_matches: list[tuple[float, Any, Any, Any, Any]] = []
        for version in self.canonical.list_provision_versions():
            if allowed_versions is not None and version.document_version_id not in allowed_versions:
                continue
            if not self._applicable(version, applicable_time):
                continue
            document_version = self.canonical.get_document_version(version.document_version_id)
            if not document_version.applies_at(applicable_time):
                continue
            provision = self.canonical.get_provision(version.provision_id)
            instrument = self.canonical.get_instrument(provision.instrument_id)
            for span in self.canonical.source_spans_for_version(version.provision_version_id):
                if page_from is not None and span.page_number < page_from:
                    continue
                if page_to is not None and span.page_number > page_to:
                    continue
                span_tokens = search_tokens(span.raw_text)
                overlap = query_tokens & span_tokens
                if not overlap:
                    continue
                score = _overlap(query_tokens, span_tokens)
                if query_tokens <= span_tokens:
                    strict_matches.append((score, span, version, provision, instrument))
                else:
                    # Vocabulary mismatch (noisy OCR, alternate legal terms):
                    # keep the best partial leads instead of a dead end.
                    partial_matches.append((score, span, version, provision, instrument))

        matches = strict_matches or sorted(
            partial_matches,
            key=lambda item: (
                -item[0],
                item[1].page_number,
                item[2].provision_version_id,
                item[1].source_span_id,
            ),
        )
        partial_mode = not strict_matches and bool(partial_matches)
        matches.sort(
            key=lambda item: (
                -item[0],
                item[1].page_number,
                item[2].provision_version_id,
                item[1].source_span_id,
            )
        )
        total = len(matches)
        page = cursor or 1
        start = (page - 1) * self.items_per_page
        rows = matches[start : start + self.items_per_page]
        if not rows:
            status = ToolStatus.EMPTY if total == 0 else ToolStatus.NOT_FOUND
            errors = () if total == 0 else (f"page {page} is beyond {total} results",)
            meta: dict[str, Any] = {"query": query, "page": page}
            if document is not None:
                meta["document"] = document
            if total == 0:
                meta["hint"] = (
                    "هیچ تکه‌ای حتی یک واژه از پرسش را نداشت؛ واژه‌ها را کلی‌تر کنید "
                    "یا با list_documents جهت‌یابی کنید."
                )
            return ToolResult(empty_envelope(store, status, errors, meta=meta))

        items: list[dict[str, Any]] = []
        evidence: list[Evidence] = []
        for score, span, version, provision, instrument in rows:
            ev = self._evidence_for_span(
                span,
                version,
                applicable_time,
                method="agentic_grep",
                reason="in-document passage match",
            )
            evidence.append(ev)
            items.append(
                self._grep_item(span, version, provision, instrument, ev, score, query_tokens)
            )
        if partial_mode:
            items = [dict(item, matched_tokens="partial") for item in items]
        has_next = start + self.items_per_page < total
        meta = {
            "query": query,
            "page": page,
            "deterministic": True,
            **(
                {
                    "partial_match": True,
                    "note": (
                        "هیچ تکه‌ای همه‌ی واژه‌های پرسش را نداشت؛ این‌ها نزدیک‌ترین نتایج "
                        "بر حسب تعداد واژه‌ی حاضرند. واژه‌ها را بازنویسی کنید یا همین‌ها را بخوانید."
                    ),
                }
                if partial_mode
                else {}
            ),
        }
        envelope = make_envelope(
            store,
            tuple(items),
            total_count=total,
            has_next_page=has_next,
            next_cursor=f"p{page + 1}" if has_next else None,
            status=ToolStatus.PARTIAL if partial_mode else ToolStatus.OK,
            meta=meta,
        )
        return ToolResult(envelope, tuple(evidence))

    def _read_span(self, store: RefStore, args: dict[str, Any]) -> ToolResult:
        source_span_id = require_str(args, "source_span_id")
        applicable_time = require_date(args, "applicable_time")
        if set(args) - {"source_span_id", "applicable_time"}:
            raise ToolArgumentError("unexpected tool args")
        span = None
        try:
            span = self.canonical.get_source_span(source_span_id)
        except NotFoundError:
            # Planners often hand an evidence_id here (observed in benchmark
            # logs); resolve it through the index this session populated.
            resolved = self.evidence_index.get(source_span_id)
            if resolved:
                try:
                    span = self.canonical.get_source_span(resolved)
                except NotFoundError:
                    span = None
        if span is None:
            hint = (
                "این شناسه یک evidence_id ناشناخته است؛ source_span_id را از "
                "items خروجی ابزارها بردارید (به شکل span_...) — یا ابتدا "
                "find_in_document/search_provisions صدا بزنید."
                if source_span_id.startswith("ev-")
                else f"span {source_span_id!r} در مخزن نیست"
            )
            return ToolResult(
                empty_envelope(store, ToolStatus.NOT_FOUND, (hint,))
            )
        version = self.canonical.get_provision_version(span.provision_version_id)
        provision = self.canonical.get_provision(version.provision_id)
        instrument = self.canonical.get_instrument(provision.instrument_id)
        evidence = self._evidence_for_span(
            span,
            version,
            applicable_time,
            method="agentic_read",
            reason="span read",
        )
        item = {
            "evidence_id": evidence.evidence_id,
            "source_span_id": span.source_span_id,
            "provision_version_id": version.provision_version_id,
            "provision_id": provision.provision_id,
            "document_version_id": version.document_version_id,
            "label": provision.label,
            "number": provision.number,
            "instrument_title": instrument.title,
            "status": version.status.value,
            "applies_at_time": version.applies_at(applicable_time),
            "page": span.page_number,
            "char_start": span.char_start,
            "char_end": span.char_end,
            "text": span.raw_text,
        }
        envelope = make_envelope(store, (item,), total_count=1)
        return ToolResult(envelope, (evidence,))

    def _list_document_contents(self, store: RefStore, args: dict[str, Any]) -> ToolResult:
        document = require_str(args, "document")
        applicable_time = require_date(args, "applicable_time")
        if set(args) - {"document", "applicable_time"}:
            raise ToolArgumentError("unexpected tool args")
        allowed_versions = self._resolve_document_versions(document, applicable_time)
        if not allowed_versions:
            return ToolResult(
                empty_envelope(
                    store,
                    ToolStatus.NOT_FOUND,
                    (f"no document matched {document!r}",),
                    meta={"document": document},
                )
            )
        rows = []
        for version in self.canonical.list_provision_versions():
            if version.document_version_id not in allowed_versions:
                continue
            if not self._applicable(version, applicable_time):
                continue
            provision = self.canonical.get_provision(version.provision_id)
            spans = self.canonical.source_spans_for_version(version.provision_version_id)
            rows.append(
                (
                    provision.ordinal,
                    provision.provision_id,
                    version,
                    provision,
                    spans[0] if spans else None,
                )
            )
        if not rows:
            return ToolResult(
                empty_envelope(
                    store,
                    ToolStatus.EMPTY,
                    meta={
                        "document": document,
                        "hint": "هیچ ماده‌ای از این سند در این تاریخ معتبر نیست.",
                    },
                )
            )
        rows.sort(key=lambda item: (item[0], item[1]))
        items = [
            {
                "provision_id": provision.provision_id,
                "provision_version_id": version.provision_version_id,
                "label": provision.label,
                "number": provision.number,
                "provision_type": provision.provision_type.value,
                "title": provision.title,
                "ordinal": provision.ordinal,
                "page": span.page_number if span else None,
                "source_span_id": span.source_span_id if span else None,
                "text_excerpt": _excerpt(version.text),
            }
            for _, _, version, provision, span in rows
        ]
        # The chapter/section map always rides along in meta so one call
        # doubles as the document's table of contents, independent of paging.
        sections = [
            {
                "label": provision.label,
                "title": provision.title,
                "page": span.page_number if span else None,
                "provision_id": provision.provision_id,
            }
            for _, _, version, provision, span in rows
            if provision.title and provision.provision_type in (
                provision.provision_type.CHAPTER,
                provision.provision_type.PART,
            )
        ]
        envelope = make_envelope(
            store,
            tuple(items),
            total_count=len(items),
            meta={
                "document": document,
                "document_version_ids": sorted(allowed_versions),
                "sections": sections,
                "hint": "برای متن کامل، read_span با source_span_id را صدا بزنید؛ "
                "برای جست‌وجو در محدوده‌ی یک بخش، page_from/page_to را با صفحه‌ی فصل تنظیم کنید.",
            },
        )
        return ToolResult(envelope)

    def _list_documents(self, store: RefStore, args: dict[str, Any]) -> ToolResult:
        query = optional_str(args, "query")
        applicable_time = require_date(args, "applicable_time")
        if set(args) - {"query", "applicable_time"}:
            raise ToolArgumentError("unexpected tool args")
        query_tokens = search_tokens(query) if query else frozenset()
        instruments: dict[str, dict[str, Any]] = {}
        for version in self.canonical.list_provision_versions():
            document_version = self.canonical.get_document_version(version.document_version_id)
            instrument = self.canonical.get_instrument(document_version.instrument_id)
            entry = instruments.setdefault(
                instrument.instrument_id,
                {
                    "instrument_id": instrument.instrument_id,
                    "title": instrument.title,
                    "canonical_title": instrument.canonical_title,
                    "instrument_type": instrument.instrument_type.value,
                    "issuer": instrument.issuer,
                    "subject_domain": instrument.subject_domain,
                    "document_version_ids": set(),
                    "applies": False,
                    "provision_count": 0,
                },
            )
            entry["document_version_ids"].add(version.document_version_id)
            entry["provision_count"] += 1
            if document_version.applies_at(applicable_time) and self._applicable(
                version, applicable_time
            ):
                entry["applies"] = True
        rows = []
        for entry in instruments.values():
            if query_tokens:
                metadata = search_tokens(
                    " ".join(
                        filter(
                            None,
                            (
                                entry["title"],
                                entry["canonical_title"],
                                entry["subject_domain"] or "",
                                entry["issuer"] or "",
                            ),
                        )
                    )
                )
                if not query_tokens & metadata:
                    continue
            rows.append(entry)
        rows.sort(key=lambda entry: entry["instrument_id"])
        items = []
        for entry in rows:
            versions = sorted(entry["document_version_ids"])
            items.append(
                {
                    "instrument_id": entry["instrument_id"],
                    "title": entry["title"],
                    "instrument_type": entry["instrument_type"],
                    "subject_domain": entry["subject_domain"],
                    "issuer": entry["issuer"],
                    "applies_at_time": entry["applies"],
                    "provision_count": entry["provision_count"],
                    "document_version_ids": versions,
                    "first_document_version_id": versions[0] if versions else None,
                }
            )
        if not items:
            return ToolResult(
                empty_envelope(
                    store,
                    ToolStatus.EMPTY,
                    meta={
                        "query": query or "",
                        "hint": "سندی با این عنوان/واژه‌ها در کتابخانه نیست.",
                    },
                )
            )
        envelope = make_envelope(
            store,
            tuple(items),
            total_count=len(items),
            meta={
                "applicable_time": applicable_time.isoformat(),
                "hint": "برای فهرست مواد یک سند، list_document_contents را صدا بزنید.",
            },
        )
        return ToolResult(envelope)

    def _get_related(self, store: RefStore, args: dict[str, Any]) -> ToolResult:
        from ..canonical import ResolutionStatus

        provision_id = require_str(args, "provision_id")
        applicable_time = require_date(args, "applicable_time")
        if set(args) - {"provision_id", "applicable_time"}:
            raise ToolArgumentError("unexpected tool args")
        try:
            provision = self.canonical.get_provision(provision_id)
        except NotFoundError:
            return ToolResult(
                empty_envelope(
                    store, ToolStatus.NOT_FOUND, (f"unknown provision {provision_id!r}",)
                )
            )
        instrument = self.canonical.get_instrument(provision.instrument_id)
        versions = self.canonical.applicable_provision_versions(provision_id, applicable_time)
        if not versions:
            return ToolResult(
                empty_envelope(
                    store,
                    ToolStatus.EMPTY,
                    meta={
                        "provision_id": provision_id,
                        "meaning": "هیچ نسخه‌ای در این تاریخ اعتبار ندارد",
                    },
                )
            )

        items: list[dict[str, Any]] = []
        evidence: list[Evidence] = []
        referenced: list[dict[str, Any]] = []
        outgoing = 0
        for version in sorted(versions, key=lambda item: item.provision_version_id):
            for reference in self.canonical.references_from_version(
                version.provision_version_id
            ):
                outgoing += 1
                items.append(
                    {
                        "direction": "outgoing",
                        "reference_id": reference.reference_id,
                        "from_provision_version_id": version.provision_version_id,
                        "target_text": reference.target_text,
                        "resolution_status": reference.resolution_status.value,
                        "resolved_target_provision_id": reference.resolved_target_provision_id,
                    }
                )
                if (
                    reference.resolution_status == ResolutionStatus.RESOLVED
                    and reference.resolved_target_provision_id
                ):
                    for target_version in self.canonical.applicable_provision_versions(
                        reference.resolved_target_provision_id, applicable_time
                    ):
                        target_spans = self.canonical.source_spans_for_version(
                            target_version.provision_version_id
                        )
                        if not target_spans:
                            continue
                        target_evidence = self._evidence_for_span(
                            target_spans[0],
                            target_version,
                            applicable_time,
                            method="agentic_reference",
                            reason=f"related via {reference.reference_id}",
                        )
                        evidence.append(target_evidence)
                        target_provision = self.canonical.get_provision(
                            target_version.provision_id
                        )
                        target_instrument = self.canonical.get_instrument(
                            target_provision.instrument_id
                        )
                        referenced.append(
                            self._compact_item(
                                target_version,
                                target_provision,
                                target_instrument,
                                target_spans[0],
                                target_evidence,
                                None,
                            )
                        )

        incoming = 0
        seen_incoming: set[str] = set()
        for version in self.canonical.list_provision_versions():
            if version.provision_id == provision_id:
                continue
            for reference in self.canonical.references_from_version(
                version.provision_version_id
            ):
                if reference.resolved_target_provision_id != provision_id:
                    continue
                incoming += 1
                key = f"{version.provision_version_id}|{reference.reference_id}"
                if key in seen_incoming:
                    continue
                seen_incoming.add(key)
                source_provision = self.canonical.get_provision(version.provision_id)
                items.append(
                    {
                        "direction": "incoming",
                        "reference_id": reference.reference_id,
                        "from_provision_version_id": version.provision_version_id,
                        "from_label": source_provision.label,
                        "from_instrument_title": self.canonical.get_instrument(
                            source_provision.instrument_id
                        ).title,
                        "target_text": reference.target_text,
                        "resolution_status": reference.resolution_status.value,
                    }
                )

        if not items:
            return ToolResult(
                empty_envelope(
                    store,
                    ToolStatus.EMPTY,
                    meta={
                        "provision_id": provision_id,
                        "instrument_title": instrument.title,
                        "hint": "نه ارجاع صریحی از این ماده ثبت شده و نه ارجاعی به آن.",
                    },
                )
            )
        envelope = make_envelope(
            store,
            tuple(items),
            total_count=len(items),
            meta={
                "provision_id": provision_id,
                "outgoing": outgoing,
                "incoming": incoming,
                "referenced": referenced,
            },
        )
        return ToolResult(envelope, tuple(evidence))

    def _expand_search_terms(self, store: RefStore, args: dict[str, Any]) -> ToolResult:
        """LLM-generated legal-register rephrasings for lexical dead-ends."""
        query = require_str(args, "query")
        if set(args) - {"query"}:
            raise ToolArgumentError("unexpected tool args")
        if self.term_expander is None:
            message = (
                "term expansion سرویس تنظیم نشده است (LEGAL_AGENT_LLM_* env)؛ "
                "خودتان واژه‌ها را بازنویسی کنید"
            )
            return ToolResult(empty_envelope(store, ToolStatus.ERROR, (message,)))
        try:
            terms = self.term_expander.expand(query)
        except DomainError as exc:
            return ToolResult(
                empty_envelope(
                    store,
                    ToolStatus.ERROR,
                    (f"term expansion failed: {exc}",),
                )
            )
        if not terms:
            return ToolResult(empty_envelope(store, ToolStatus.EMPTY))
        envelope = make_envelope(
            store,
            tuple({"suggested_query": term} for term in terms),
            total_count=len(terms),
            meta={"hint": "این کوئری‌ها را با search_provisions/find_in_document امتحان کنید."},
        )
        return ToolResult(envelope)

    def _semantic_search(self, store: RefStore, args: dict[str, Any]) -> ToolResult:
        """Vector similarity over page-level spans; canonical evidence out."""
        query = require_str(args, "query")
        applicable_time = require_date(args, "applicable_time")
        document = optional_str(args, "document")
        cursor = optional_cursor(args)
        if set(args) - {"query", "applicable_time", "document", "cursor"}:
            raise ToolArgumentError("unexpected tool args")
        if self.semantic_index is None:
            message = (
                "semantic search تنظیم نشده است (LEGAL_AGENT_EMBEDDINGS_* env)؛ "
                "از search_provisions/find_in_document استفاده کنید"
            )
            return ToolResult(empty_envelope(store, ToolStatus.ERROR, (message,)))
        try:
            top = self.semantic_index.query(query, top_k=64)
        except DomainError as exc:
            return ToolResult(
                empty_envelope(store, ToolStatus.ERROR, (f"semantic index failed: {exc}",))
            )
        allowed_versions = self._resolve_document_versions(document, applicable_time)
        if document is not None and not allowed_versions:
            return ToolResult(
                empty_envelope(
                    store,
                    ToolStatus.NOT_FOUND,
                    (f"no document matched {document!r}",),
                    meta={"document": document},
                )
            )

        rows: list[tuple[float, Any, Any, Any, Any, Any]] = []
        for hit in top:
            version = self.canonical.get_provision_version(hit.span.provision_version_id)
            if allowed_versions is not None and version.document_version_id not in allowed_versions:
                continue
            if not self._applicable(version, applicable_time):
                continue
            provision = self.canonical.get_provision(version.provision_id)
            instrument = self.canonical.get_instrument(provision.instrument_id)
            spans = self.canonical.source_spans_for_version(version.provision_version_id)
            span = next(
                (item for item in spans if item.source_span_id == hit.span.source_span_id),
                spans[0] if spans else None,
            )
            if span is None:
                continue
            rows.append((hit.similarity, span, version, provision, instrument, hit))

        total = len(rows)
        page = cursor or 1
        start = (page - 1) * self.items_per_page
        rows_page = rows[start : start + self.items_per_page]
        if not rows_page:
            return ToolResult(
                empty_envelope(
                    store,
                    ToolStatus.EMPTY if total == 0 else ToolStatus.NOT_FOUND,
                    () if total == 0 else (f"page {page} is beyond {total} results",),
                    meta={"query": query, "page": page},
                )
            )
        items: list[dict[str, Any]] = []
        evidence: list[Evidence] = []
        for similarity, span, version, provision, instrument, _ in rows_page:
            ev = self._evidence_for_span(
                span,
                version,
                applicable_time,
                method="agentic_semantic",
                reason="semantic similarity match",
            )
            evidence.append(ev)
            item = self._compact_item(version, provision, instrument, span, ev, similarity)
            items.append(item)
        has_next = start + self.items_per_page < total
        envelope = make_envelope(
            store,
            tuple(items),
            total_count=total,
            has_next_page=has_next,
            next_cursor=f"p{page + 1}" if has_next else None,
            meta={"query": query, "page": page, "ranking": "cosine_similarity"},
        )
        return ToolResult(envelope, tuple(evidence))

    # ---------------------------------------------------------------- helpers

    def _resolve_document_versions(
        self, document: str | None, applicable_time: date
    ) -> set[str] | None:
        """Map a human document reference onto applicable document-version ids.

        Accepts a document_version_id, instrument_id, source_document_id or an
        instrument-title substring (planners pass titles). ``None`` (no filter)
        means every document. An empty set means the reference matched nothing.
        """
        index: dict[str, dict[str, Any]] = {}
        for version in self.canonical.list_provision_versions():
            document_version = self.canonical.get_document_version(version.document_version_id)
            instrument = self.canonical.get_instrument(document_version.instrument_id)
            entry = index.setdefault(
                version.document_version_id,
                {
                    "instrument_id": instrument.instrument_id,
                    "source_document_id": document_version.source_document_id,
                    "titles": (
                        instrument.title,
                        instrument.canonical_title,
                        instrument.subject_domain or "",
                    ),
                    "applies": document_version.applies_at(applicable_time)
                    and version.applies_at(applicable_time),
                },
            )
            entry["applies"] = entry["applies"] or (
                document_version.applies_at(applicable_time)
                and version.applies_at(applicable_time)
            )
        if document is None:
            return None
        candidate = document.strip()
        folded = _fold_title(candidate)
        allowed: set[str] = set()
        for document_version_id, entry in index.items():
            if candidate in {
                document_version_id,
                entry["instrument_id"],
                entry["source_document_id"],
            }:
                if entry["applies"]:
                    allowed.add(document_version_id)
                continue
            if any(
                folded in _fold_title(title) for title in entry["titles"] if title
            ) and entry["applies"]:
                allowed.add(document_version_id)
        return allowed

    @staticmethod
    def _grep_item(
        span: Any,
        version: Any,
        provision: Any,
        instrument: Any,
        evidence: Evidence,
        score: float,
        query_tokens: frozenset[str],
    ) -> dict[str, Any]:
        return {
            "evidence_id": evidence.evidence_id,
            "source_span_id": span.source_span_id,
            "provision_version_id": version.provision_version_id,
            "provision_id": provision.provision_id,
            "document_version_id": version.document_version_id,
            "label": provision.label,
            "number": provision.number,
            "instrument_title": instrument.title,
            "page": span.page_number,
            "char_start": span.char_start,
            "char_end": span.char_end,
            "text": _match_excerpt(span.raw_text, query_tokens),
            "score": round(score, 4),
        }

    def _evidence_for_version(
        self,
        version: Any,
        applicable_time: date,
        *,
        method: str,
        reason: str,
        preferred_span: Any | None = None,
    ) -> tuple[Evidence | None, tuple[Evidence, ...], tuple[dict[str, Any], ...]]:
        """Claim evidence for one version + aux evidence for its resolved refs.

        Following resolved references here mirrors the navigation retriever and
        keeps the verifier's ``unfollowed_explicit_reference`` check satisfiable
        for agent-cited evidence.
        """
        from ..canonical import ResolutionStatus

        spans = self.canonical.source_spans_for_version(version.provision_version_id)
        if not spans:
            return None, (), ()
        span = preferred_span if preferred_span is not None else spans[0]
        evidence = self._evidence_for_span(
            span, version, applicable_time, method=method, reason=reason
        )
        aux_evidence: list[Evidence] = []
        aux_items: list[dict[str, Any]] = []
        for reference in self.canonical.references_from_version(version.provision_version_id):
            if (
                reference.resolution_status != ResolutionStatus.RESOLVED
                or reference.resolved_target_provision_id is None
            ):
                continue
            for target_version in self.canonical.applicable_provision_versions(
                reference.resolved_target_provision_id, applicable_time
            ):
                target_spans = self.canonical.source_spans_for_version(
                    target_version.provision_version_id
                )
                if not target_spans:
                    continue
                target_span = target_spans[0]
                target_evidence = self._evidence_for_span(
                    target_span,
                    target_version,
                    applicable_time,
                    method="agentic_reference",
                    reason="resolved explicit reference target",
                )
                aux_evidence.append(target_evidence)
                provision = self.canonical.get_provision(target_version.provision_id)
                instrument = self.canonical.get_instrument(provision.instrument_id)
                aux_items.append(
                    self._compact_item(
                        target_version, provision, instrument, target_span, target_evidence, None
                    )
                )
        return evidence, tuple(aux_evidence), tuple(aux_items)

    def _evidence_for_span(
        self,
        span: Any,
        version: Any,
        applicable_time: date,
        *,
        method: str,
        reason: str,
    ) -> Evidence:
        import hashlib

        identity = (
            f"{version.provision_version_id}|{span.source_span_id}|"
            f"{applicable_time.isoformat()}|{method}"
        )
        evidence = Evidence(
            evidence_id=f"ev-{hashlib.sha256(identity.encode()).hexdigest()[:24]}",
            document_id=span.source_document_id,
            document_version_id=version.document_version_id,
            provision_id=version.provision_id,
            provision_version_id=version.provision_version_id,
            source_span_id=span.source_span_id,
            page=span.page_number,
            text=span.raw_text,
            retrieval_method=method,
            reason_selected=reason,
            applicable_time=applicable_time,
        )
        # Models frequently pass evidence ids where span ids are expected
        # (observed in 5 benchmark cases); keep a reverse index so
        # read_span can resolve either identifier.
        self.evidence_index[evidence.evidence_id] = span.source_span_id
        return evidence

    def _compact_item(
        self,
        version: Any,
        provision: Any,
        instrument: Any,
        span: Any,
        evidence: Evidence | None,
        score: float | None,
    ) -> dict[str, Any]:
        item = {
            "evidence_id": evidence.evidence_id if evidence else None,
            "provision_version_id": version.provision_version_id,
            "provision_id": provision.provision_id,
            "document_version_id": version.document_version_id,
            "source_span_id": span.source_span_id,
            "label": provision.label,
            "number": provision.number,
            "instrument_title": instrument.title,
            "status": version.status.value,
            "effective_from": _iso(version.effective_from),
            "effective_to": _iso(version.effective_to),
            "page": span.page_number,
            "text": _excerpt(span.raw_text),
        }
        if score is not None:
            item["score"] = round(score, 4)
        return item

    def _applicable(self, version: Any, applicable_time: date) -> bool:
        return version.applies_at(applicable_time)

    @staticmethod
    def _scope_allows(
        scope: tuple[str, ...],
        provision: Any,
        version: Any,
        source_document_id: str,
        instrument: Any,
    ) -> tuple[bool, set[str]]:
        """Match scope tokens by canonical id OR human-readable title.

        Planners naturally pass document titles (e.g. «دستورالعمل اجرایی
        هیئت‌های تشخیص مطالبات») rather than opaque ids; both must work.
        Returns (allowed, matched_tokens) so callers can report unresolved scope.
        """
        if not scope:
            return True, set()
        identifiers = {
            provision.instrument_id,
            provision.provision_id,
            version.document_version_id,
            version.provision_version_id,
            source_document_id,
        }
        titles = (
            instrument.title,
            instrument.canonical_title,
            provision.label,
            provision.title or "",
            instrument.subject_domain or "",
        )
        matched: set[str] = set()
        for token in scope:
            candidate = token.strip()
            if not candidate:
                continue
            if candidate in identifiers:
                matched.add(token)
                continue
            folded = _fold_title(candidate)
            if any(folded in _fold_title(title) for title in titles if title):
                matched.add(token)
        return bool(matched), matched


def require_str(args: dict[str, Any], key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ToolArgumentError(f"tool arg {key!r} must be a non-empty string")
    return value


def require_date(args: dict[str, Any], key: str) -> date:
    value = args.get(key)
    if not isinstance(value, str):
        raise ToolArgumentError(f"tool arg {key!r} must be an ISO date string")
    text = value.strip().translate(
        str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
    )
    parts = text.replace("/", "-").split("-")
    if len(parts) == 3 and all(part.isdigit() for part in parts):
        year, month, day = (int(part) for part in parts)
        # Model-facing contract is Gregorian ISO, but Persian models sometimes
        # emit Jalali dates; a year below 1700 is unambiguously Jalali.
        if 1200 <= year <= 1600:
            from ..jalali import jalali_to_gregorian

            try:
                return jalali_to_gregorian(year, month, day)
            except DomainError as exc:
                raise ToolArgumentError(
                    f"tool arg {key!r} is not a valid Jalali date: {value!r}"
                ) from exc
        try:
            return date(year, month, day)
        except ValueError as exc:
            raise ToolArgumentError(
                f"tool arg {key!r} is not a valid date: {value!r}"
            ) from exc
    raise ToolArgumentError(f"tool arg {key!r} must be an ISO date string")


def optional_scope(args: dict[str, Any]) -> tuple[str, ...]:
    if "document_scope" not in args:
        return ()
    value = args["document_scope"]
    if value is None:
        return ()
    # Be forgiving: planners often pass a single title string instead of a list.
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if not isinstance(value, (list, tuple)) or not all(
        isinstance(item, str) for item in value
    ):
        raise ToolArgumentError("tool arg 'document_scope' must be a string or list of strings")
    return tuple(value)


def optional_str(args: dict[str, Any], key: str) -> str | None:
    if key not in args:
        return None
    value = args[key]
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ToolArgumentError(f"tool arg {key!r} must be a non-empty string")
    return value.strip()


def optional_int(args: dict[str, Any], key: str) -> int | None:
    if key not in args:
        return None
    value = args[key]
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolArgumentError(f"tool arg {key!r} must be an integer")
    return value


def optional_cursor(args: dict[str, Any]) -> int | None:
    value = args.get("cursor")
    if value is None:
        return None
    if not isinstance(value, str) or not value.startswith("p") or not value[1:].isdigit():
        raise ToolArgumentError("tool arg 'cursor' must look like 'p2'")
    page = int(value[1:])
    if page < 1:
        raise ToolArgumentError("cursor page must be one-based")
    return page


def _overlap(query: frozenset[str], document: frozenset[str]) -> float:
    if not query or not document:
        return 0.0
    return len(query & document) / len(query)


def _fold_title(text: str) -> str:
    """Casefold + drop invisible joiners so model-typed titles match.

    Planners omit the Persian zero-width non-joiner («بیمهای» vs
    «بیمه‌ای»); casefold alone does not reconcile them.
    """
    return text.casefold().replace("\u200c", "").replace("\u200f", "").strip()


def _excerpt(text: str, limit: int = EXCERPT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _match_excerpt(
    text: str, query_tokens: frozenset[str], limit: int = EXCERPT_CHARS
) -> str:
    """Excerpt centred on the earliest query-token occurrence in the raw text."""
    lowered = text.casefold()
    positions = [lowered.find(token) for token in query_tokens]
    positions = [position for position in positions if position >= 0]
    start = 0
    if positions:
        start = max(0, min(positions) - 60)
    excerpt = text[start : start + limit]
    prefix = "…" if start else ""
    suffix = "…" if start + len(excerpt) < len(text) else ""
    return prefix + excerpt + suffix


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value else None
