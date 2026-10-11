"""Read-only graph exploration tools over the FalkorDB laws projection.

A curated tool surface for the graph-guide chat agent: statistics, node
lookup, neighbourhood, paths, and a guarded read-only Cypher fallback.
Every tool returns the same honest :class:`Envelope` contract as the
research tools; no Evidence is produced (exploration, not citable research).

Schema of the mirrored graph (laws_canonical_v1):
  (:LegalInstrument {id,title,kind,tier,issuer,provisions,url,domain,summary})
  (:Provision {id,instrument_id,label,type,number,ordinal,summary})
  (:Judgment | :AdvisoryOpinion {id,title,category,summary})
  (:Authority {name})  (:LegalDomain {name})
  rels: CONTAINS, PART_OF, ISSUES, BELONGS_TO_DOMAIN, CITES plus the
  UPPER_SNAKE canonical edge types (EXPLICITLY_REFERENCES, AMENDS, REPEALS,
  IMPLEMENTS, SUBORDINATE_TO, ...).

FalkorDB gotchas honoured here: no slice syntax (``a[:b]``), no non-ASCII
column aliases, no parameters inside variable-length path bounds.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from ..errors import DomainError
from .envelope import Envelope, RefStore, ToolStatus, empty_envelope, make_envelope
from .tools import ToolSpec

GRAPH_TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "graph_stats",
        "آمار کلی گراف: تعداد گره به تفکیک نوع، توزیع tier قوانین، یال‌ها به تفکیک نوع و دامنه‌های پرتکرار",
        (),
    ),
    ToolSpec(
        "find_nodes",
        "یافتن گره با بخشی از عنوان/برچسب/شماره (قانون، ماده، رأی، نظریه، مرجع، دامنه) و برگرداندن شناسه گره",
        ("query", "label?", "limit?"),
    ),
    ToolSpec(
        "search_laws",
        "جست‌وجوی موضوعی در عنوان و خلاصه‌ی قوانین (و دامنه)؛ برای «قوانین مربوط به X را بیار» — "
        "خلاصه‌ی هر سند را هم برمی‌گرداند تا خودت مرتبط‌بودن را بسنجی",
        ("topic", "limit?"),
    ),
    ToolSpec(
        "list_provisions",
        "مواد یک قانون مشخص (با خلاصه، در صورت وجود) برای یافتن ماده‌های مرتبط با یک موضوع",
        ("instrument_id", "query?", "limit?"),
    ),
    ToolSpec(
        "get_provision_text",
        "متن کامل یک ماده از مخزن canonical (مکمل گراف: گراف فقط برچسب و خلاصه دارد)",
        ("provision_id",),
    ),
    ToolSpec(
        "search_judgments",
        "جست‌وجوی موضوعی در آرای قضایی (رأی دیوان عدالت اداری، رأی وحدت رویه، رویه قضایی، "
        "نشست قضایی) و نظریات مشورتی بر اساس عنوان/خلاصه/دسته؛ خلاصه‌ی هر رأی را هم برمی‌گرداند",
        ("topic", "kind?", "limit?"),
    ),
    ToolSpec(
        "judgments_for_law",
        "آرای قضایی و نظریات مشورتی که به یک قانون (با شناسه یا عنوان) استناد کرده‌اند — "
        "برای «کدام رویه برای این قانون»",
        ("law", "kind?", "limit?"),
    ),
    ToolSpec(
        "get_judgment_text",
        "متن کامل یک رأی/نظریه از مخزن منابع به همراه متادیتا و قوانین مستند "
        "(گراف فقط برچسب و خلاصه دارد)",
        ("judgment_id",),
    ),
    ToolSpec(
        "node_neighbors",
        "همسایه‌های یک گره با نوع رابطه و جهت (ورودی/خروجی)؛ برای «فلان ماده به چه چیزی وصله»",
        ("node_id", "rel_types?", "limit?"),
    ),
    ToolSpec(
        "find_paths",
        "کوتاه‌ترین مسیر بین دو گره تا عمق ۴ (قانون↔قانون، ماده↔قانون، رأی↔قانون)",
        ("source_id", "target_id", "max_depth?"),
    ),
    ToolSpec(
        "run_cypher",
        (
            "اجرای کوئری Cypher فقط-خواندنی روی گراف وقتی ابزارهای بالا کافی نیستند؛ "
            "فقط MATCH/OPTIONAL MATCH/WITH/RETURN/UNWIND/CALL db.* — ردیف‌ها محدود می‌شوند"
        ),
        ("query",),
    ),
)

_GRAPH_TOOL_NAMES = frozenset(spec.name for spec in GRAPH_TOOL_SPECS)

#: JSON-schema fragments per argument name, used to declare the tools to a
#: provider so it answers with native function calls instead of JSON text.
_ARG_SCHEMAS: dict[str, dict[str, Any]] = {
    "query": {"type": "string", "description": "عبارت جست‌وجو"},
    "label": {
        "type": "string",
        "description": "نوع گره: instrument | provision | judgment | opinion | authority | domain",
    },
    "limit": {"type": "integer", "description": "حداکثر تعداد نتایج"},
    "topic": {"type": "string", "description": "موضوع مورد جست‌وجو"},
    "instrument_id": {"type": "string", "description": "شناسهٔ قانون"},
    "provision_id": {"type": "string", "description": "شناسهٔ ماده"},
    "kind": {"type": "string", "description": "judgment یا opinion"},
    "law": {"type": "string", "description": "شناسه یا عنوان قانون"},
    "judgment_id": {"type": "string", "description": "شناسهٔ رأی/نظریه"},
    "node_id": {"type": "string", "description": "شناسهٔ گره"},
    "rel_types": {
        "type": "array",
        "items": {"type": "string"},
        "description": "فیلتر انواع یال",
    },
    "source_id": {"type": "string", "description": "شناسهٔ گرهٔ مبدأ"},
    "target_id": {"type": "string", "description": "شناسهٔ گرهٔ مقصد"},
    "max_depth": {"type": "integer", "description": "حداکثر عمق مسیر (۱ تا ۴)"},
}


def graph_tool_schemas() -> tuple[dict[str, Any], ...]:
    """OpenAI-style function schemas derived from :data:`GRAPH_TOOL_SPECS`.

    Declaring these to the provider makes tool-oriented reasoning models
    (deepseek-v4.1-flash, glm-5.3-flash) answer with native ``tool_calls``
    instead of the JSON-in-text contract, which they emit unreliably.
    """
    schemas: list[dict[str, Any]] = []
    for spec in GRAPH_TOOL_SPECS:
        properties: dict[str, Any] = {}
        required: list[str] = []
        for arg in spec.args:
            optional = arg.endswith("?")
            name = arg.rstrip("?")
            properties[name] = dict(_ARG_SCHEMAS.get(name, {"type": "string"}))
            if not optional:
                required.append(name)
        schemas.append(
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": required,
                    },
                },
            }
        )
    return tuple(schemas)


def search_graph(graph: Any, query: str, limit: int = 15) -> list[dict[str, Any]]:
    """Ranked keyword search across instruments, judgments and opinions.

    A read-only, presentation-oriented companion to the chat tools: the search
    box on the explore page calls the ``/graph/search`` endpoint which uses
    this. Tokens are matched against title/domain(category)/summary and scored
    (title +4, domain/category +3, summary +2) with a small per-type bonus, so
    the most specific document floats to the top. Returns plain dicts — not an
    Envelope — because there is no Evidence or pagination contract here.
    """
    text = (query or "").strip()
    if not text:
        return []
    tokens = [token for token in re.split(r"\s+", text.lower()) if len(token) >= 2]
    if not tokens:
        tokens = [text.lower()]
    params = {f"t{index}": token for index, token in enumerate(tokens)}

    def clause(fields: tuple[str, ...]) -> str:
        return " OR ".join(
            "(" + " OR ".join(f"toLower(x.{field}) CONTAINS $t{index}" for field in fields) + ")"
            for index in range(len(tokens))
        )

    plans = (
        (
            "instrument",
            2,
            "MATCH (x:LegalInstrument) WHERE "
            + clause(("title", "summary", "domain"))
            + " RETURN x.id, x.title, x.summary, x.domain, x.tier, x.kind"
            " ORDER BY size(x.title) LIMIT 400",
        ),
        (
            "judgment",
            1,
            "MATCH (x:Judgment) WHERE "
            + clause(("title", "summary", "category"))
            + " RETURN x.id, x.title, x.summary, x.category"
            " ORDER BY size(x.title) LIMIT 400",
        ),
        (
            "opinion",
            1,
            "MATCH (x:AdvisoryOpinion) WHERE "
            + clause(("title", "summary", "category"))
            + " RETURN x.id, x.title, x.summary, x.category"
            " ORDER BY size(x.title) LIMIT 400",
        ),
    )
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for label, bonus, cypher in plans:
        try:
            result = graph.ro_query(cypher, params)
        except Exception:  # noqa: BLE001, S112 — one label failing must not kill the rest
            continue
        for row in result.result_set:
            node_id = str(row[0])
            if not node_id or node_id in seen:
                continue
            title = (row[1] or "").lower()
            summary = (row[2] or "").lower()
            secondary = (row[3] or "").lower()
            score = bonus
            for token in tokens:
                if token in title:
                    score += 4
                if token in secondary:
                    score += 3
                if token in summary:
                    score += 2
            if score <= bonus:
                continue
            seen.add(node_id)
            item: dict[str, Any] = {
                "id": node_id,
                "label": label,
                "title": row[1],
                "summary": row[2],
                "score": score,
            }
            if label == "instrument":
                item["domain"] = row[3]
                item["tier"] = row[4]
                item["tier_label"] = _TIER_LABELS.get(str(row[4]))
                item["kind"] = row[5]
            else:
                item["category"] = row[3]
            results.append(item)
    results.sort(key=lambda item: (-item["score"], len(item["title"] or "")))
    return results[:limit]


_LABEL_ALIASES = {
    "instrument": "instrument",
    "legalinstrument": "instrument",
    "قانون": "instrument",
    "قوانین": "instrument",
    "سند": "instrument",
    "provision": "provision",
    "ماده": "provision",
    "مواد": "provision",
    "judgment": "judgment",
    "رأی": "judgment",
    "رای": "judgment",
    "آرا": "judgment",
    "opinion": "opinion",
    "advisoryopinion": "opinion",
    "نظریه": "opinion",
    "authority": "authority",
    "مرجع": "authority",
    "domain": "domain",
    "legaldomain": "domain",
    "دامنه": "domain",
    "موضوع": "domain",
}

_JUDGMENT_KIND_ALIASES = {
    "judgment": "judgment",
    "رأی": "judgment",
    "رای": "judgment",
    "آرا": "judgment",
    "دادنامه": "judgment",
    "opinion": "opinion",
    "advisoryopinion": "opinion",
    "advisory_opinion": "opinion",
    "نظریه": "opinion",
    "نظریات": "opinion",
}

_TIER_LABELS = {
    "1": "اساسی",
    "2": "خاص (منشور)",
    "3": "عادی (قانون)",
    "4": "مقررات (آیین‌نامه)",
    "5": "بخشنامه",
}

_ALLOWED_LABELS = frozenset(
    {"instrument", "provision", "judgment", "opinion", "authority", "domain"}
)

# Read-only statement prefixes; anything else is rejected before it reaches
# FalkorDB (GRAPH.RO_QUERY is the second line of defence).
_STATEMENT_PREFIXES = (
    "MATCH",
    "OPTIONAL MATCH",
    "WITH",
    "RETURN",
    "UNWIND",
    "CALL DB.",
)
_WRITE_KEYWORDS = re.compile(
    r"\b(CREATE|MERGE|DELETE|DETACH|SET|REMOVE|DROP|LOAD|FOREACH|INDEX)\b",
    re.IGNORECASE,
)


class GraphToolError(DomainError):
    """A chat-supplied tool argument failed validation."""


class GraphTools:
    """Deterministic read-only graph tools shared by the chat loop and MCP."""

    def __init__(
        self,
        graph: Any,
        items_per_page: int = 15,
        max_cypher_rows: int = 60,
        provision_text: Callable[[str], dict[str, Any] | None] | None = None,
        judgment_text: Callable[[str], dict[str, Any] | None] | None = None,
    ) -> None:
        if items_per_page < 1:
            raise DomainError("items_per_page must be positive")
        if max_cypher_rows < 1:
            raise DomainError("max_cypher_rows must be positive")
        self.graph = graph
        self.items_per_page = items_per_page
        self.max_cypher_rows = max_cypher_rows
        #: provision_id -> {text, label, ...} from the canonical store; the
        #: graph mirror only carries labels/summaries, so full text is fetched
        #: on demand (None disables get_provision_text honestly).
        self.provision_text = provision_text
        #: document_uid -> {text, title, ...} for judgments/advisory opinions
        #: from the sources side (None disables get_judgment_text honestly).
        self.judgment_text = judgment_text
        #: Cypher statements executed during the current dispatch; injected
        #: into the envelope meta so the UI can show the tool's real query.
        self._query_log: list[dict[str, Any]] = []

    # ------------------------------------------------------------- dispatch

    def dispatch(self, store: RefStore, name: str, args: dict[str, Any]) -> Envelope:
        if name not in _GRAPH_TOOL_NAMES:
            raise GraphToolError(f"unknown graph tool {name!r}")
        if not isinstance(args, dict):
            raise GraphToolError("tool args must be an object")
        self._query_log = []
        handler = getattr(self, f"_{name}")
        envelope = handler(store, args)
        if self._query_log:
            # Transparency only: the executed Cypher rides in meta for the UI.
            envelope = replace(
                envelope, meta={**envelope.meta, "cypher": list(self._query_log)}
            )
        return envelope

    def tool_catalog(self) -> str:
        return "\n".join(
            f"{spec.signature()} — {spec.description}" for spec in GRAPH_TOOL_SPECS
        )

    # ---------------------------------------------------------------- tools

    def _graph_stats(self, store: RefStore, args: dict[str, Any]) -> Envelope:
        self._no_args(args, ())
        items: list[dict[str, Any]] = []
        errors: list[str] = []
        nodes_query = (
            "MATCH (n) RETURN labels(n)[0] AS label, count(n) AS cnt ORDER BY cnt DESC"
        )
        tiers_query = (
            "MATCH (i:LegalInstrument) RETURN i.tier AS tier, count(i) AS cnt ORDER BY tier"
        )
        rels_query = (
            "MATCH ()-[r]->() RETURN type(r) AS rel, count(r) AS cnt "
            "ORDER BY cnt DESC LIMIT 25"
        )
        domains_query = (
            "MATCH (i:LegalInstrument)-[:BELONGS_TO_DOMAIN]->(d:LegalDomain) "
            "RETURN d.name AS domain, count(i) AS cnt ORDER BY cnt DESC LIMIT 15"
        )
        sections = (
            ("nodes", nodes_query, {}),
            ("tiers", tiers_query, {}),
            ("relationships", rels_query, {}),
            ("domains", domains_query, {}),
        )
        for key, query, params in sections:
            try:
                _, rows = self._run(query, params)
            except DomainError as exc:
                errors.append(f"{key}: {exc}")
                continue
            for row in rows:
                if key == "tiers":
                    items.append(
                        {
                            "metric": "instruments_by_tier",
                            "tier": row[0],
                            "tier_label": _TIER_LABELS.get(str(row[0])),
                            "count": row[1],
                        }
                    )
                else:
                    items.append({"metric": f"{key}", "name": row[0], "count": row[1]})
        if errors and not items:
            return empty_envelope(store, ToolStatus.ERROR, tuple(errors))
        status = ToolStatus.ERROR if errors else ToolStatus.OK
        return make_envelope(store, tuple(items), status=status, errors=tuple(errors))

    def _find_nodes(self, store: RefStore, args: dict[str, Any]) -> Envelope:
        query = self._require_query(args)
        label = self._optional_label(args)
        limit = self._optional_limit(args, self.items_per_page)
        per_label = max(3, min(limit, 10))
        lowered = query.lower()
        instrument_query = (
            "MATCH (i:LegalInstrument) WHERE toLower(i.title) CONTAINS $q "
            "RETURN i.id AS id, i.title AS title, i.tier AS tier, i.kind AS kind "
            "ORDER BY size(i.title) LIMIT $lim"
        )
        provision_query = (
            "MATCH (p:Provision) WHERE toLower(p.label) CONTAINS $q "
            "RETURN p.id AS id, p.label AS title, "
            "p.instrument_id AS instrument_id, p.number AS number "
            "ORDER BY size(p.label) LIMIT $lim"
        )
        judgment_query = (
            "MATCH (j:Judgment) WHERE toLower(j.title) CONTAINS $q "
            "RETURN j.id AS id, j.title AS title, j.category AS category, j.summary AS summary "
            "ORDER BY size(j.title) LIMIT $lim"
        )
        opinion_query = (
            "MATCH (o:AdvisoryOpinion) WHERE toLower(o.title) CONTAINS $q "
            "RETURN o.id AS id, o.title AS title, o.category AS category, o.summary AS summary "
            "ORDER BY size(o.title) LIMIT $lim"
        )
        authority_query = (
            "MATCH (a:Authority) WHERE toLower(a.name) CONTAINS $q "
            "RETURN a.name AS id, a.name AS title LIMIT $lim"
        )
        domain_query = (
            "MATCH (d:LegalDomain) WHERE toLower(d.name) CONTAINS $q "
            "RETURN d.name AS id, d.name AS title LIMIT $lim"
        )
        plans: tuple[tuple[str, str, dict, tuple[str, ...]], ...] = ()
        if label in (None, "instrument"):
            plans += (("instrument", instrument_query, {"q": lowered, "lim": per_label}, ("tier", "kind")),)
        if label in (None, "provision"):
            plans += (("provision", provision_query, {"q": lowered, "lim": per_label}, ("instrument_id", "number")),)
        if label in (None, "judgment"):
            plans += (("judgment", judgment_query, {"q": lowered, "lim": per_label}, ("category", "summary")),)
        if label in (None, "opinion"):
            plans += (("opinion", opinion_query, {"q": lowered, "lim": per_label}, ("category", "summary")),)
        if label in (None, "authority"):
            plans += (("authority", authority_query, {"q": lowered, "lim": per_label}, ()),)
        if label in (None, "domain"):
            plans += (("domain", domain_query, {"q": lowered, "lim": per_label}, ()),)
        items: list[dict[str, Any]] = []
        errors: list[str] = []
        seen: set[tuple[str, str]] = set()
        for node_label, cypher, params, columns in plans:
            try:
                _, rows = self._run(cypher, params)
            except DomainError as exc:
                errors.append(f"{node_label}: {exc}")
                continue
            for row in rows:
                key = (str(row[0]), node_label)
                if key in seen:
                    continue
                seen.add(key)
                item: dict[str, Any] = {
                    "id": str(row[0]),
                    "label": node_label,
                    "title": row[1],
                }
                for column, value in zip(columns, row[2:]):
                    if value is not None:
                        item[column] = value
                if node_label == "instrument" and "tier" in item:
                    item["tier_label"] = _TIER_LABELS.get(str(item["tier"]))
                items.append(item)
        if errors and not items:
            return empty_envelope(store, ToolStatus.ERROR, tuple(errors))
        if not items:
            return empty_envelope(
                store, ToolStatus.EMPTY, ("هیچ گره‌ای با این عبارت یافت نشد",)
            )
        status = ToolStatus.ERROR if errors else ToolStatus.OK
        return make_envelope(
            store,
            tuple(items[:limit]),
            status=status,
            total_count=len(items),
            errors=tuple(errors),
            complete=not errors and len(items) <= limit,
        )

    def _search_laws(self, store: RefStore, args: dict[str, Any]) -> Envelope:
        topic = require_str(args, "topic")
        unexpected = set(args) - {"topic", "limit"}
        if unexpected:
            raise GraphToolError(f"unexpected tool args: {', '.join(sorted(unexpected))}")
        limit = self._optional_int(args, "limit", 12, (1, 40))
        tokens = [token for token in re.split(r"\s+", topic.lower().strip()) if len(token) >= 2]
        if not tokens:
            tokens = [topic.lower().strip()]
        params: dict[str, Any] = {}
        clauses = []
        for index, token in enumerate(tokens):
            key = f"t{index}"
            params[key] = token
            clauses.append(
                f"(toLower(i.title) CONTAINS ${key} "
                f"OR toLower(i.summary) CONTAINS ${key} "
                f"OR toLower(i.domain) CONTAINS ${key})"
            )
        cypher = (
            f"MATCH (i:LegalInstrument) WHERE {' OR '.join(clauses)} "
            "RETURN i.id, i.title, i.summary, i.domain, i.tier, i.kind "
            "ORDER BY size(i.title) LIMIT 300"
        )
        try:
            _, rows = self._run(cypher, params)
        except DomainError as exc:
            return empty_envelope(store, ToolStatus.ERROR, (f"جست‌وجوی موضوعی شکست خورد: {exc}",))
        scored: list[tuple[int, list[Any]]] = []
        matched_domains: set[str] = set()
        for row in rows:
            title = (row[1] or "").lower()
            summary = (row[2] or "").lower()
            domain = (row[3] or "").lower()
            score = 0
            for token in tokens:
                if token in title:
                    score += 4
                if token in domain:
                    score += 3
                    matched_domains.add(str(row[3]))
                if token in summary:
                    score += 2
            if score:
                scored.append((score, row))
        if not scored:
            return empty_envelope(
                store,
                ToolStatus.EMPTY,
                (f"قانونی مرتبط با موضوع {topic!r} یافت نشد",),
                meta={"matched_domains": []},
            )
        scored.sort(key=lambda item: (-item[0], len(item[1][1] or "")))
        items = [
            {
                "id": str(row[0]),
                "title": row[1],
                "summary": row[2],
                "domain": row[3],
                "tier": row[4],
                "tier_label": _TIER_LABELS.get(str(row[4])),
                "kind": row[5],
            }
            for _, row in scored[:limit]
        ]
        truncated = len(scored) > limit
        return make_envelope(
            store,
            tuple(items),
            status=ToolStatus.PARTIAL if truncated else ToolStatus.OK,
            total_count=len(scored),
            complete=not truncated,
            meta={"matched_domains": sorted(matched_domains)},
        )

    def _list_provisions(self, store: RefStore, args: dict[str, Any]) -> Envelope:
        instrument_id = require_str(args, "instrument_id")
        query = args.get("query")
        if query is not None and not isinstance(query, str):
            raise GraphToolError("query must be a string")
        unexpected = set(args) - {"instrument_id", "query", "limit"}
        if unexpected:
            raise GraphToolError(f"unexpected tool args: {', '.join(sorted(unexpected))}")
        limit = self._optional_int(args, "limit", 20, (1, 60))
        where = "WHERE p.instrument_id = $iid"
        params: dict[str, Any] = {"iid": instrument_id, "lim": limit}
        if query and query.strip():
            params["q"] = query.strip().lower()
            where += " AND (toLower(p.label) CONTAINS $q OR toLower(p.summary) CONTAINS $q)"
        cypher = (
            f"MATCH (p:Provision) {where} "
            "RETURN p.id, p.label, p.number, p.summary ORDER BY p.ordinal LIMIT $lim"
        )
        try:
            _, rows = self._run(cypher, params)
            _, count_rows = self._run(
                f"MATCH (p:Provision) {where} RETURN count(p)",
                {key: value for key, value in params.items() if key != "lim"},
            )
        except DomainError as exc:
            return empty_envelope(store, ToolStatus.ERROR, (f"خواندن مواد شکست خورد: {exc}",))
        total = int(count_rows[0][0]) if count_rows else len(rows)
        if not rows:
            return empty_envelope(
                store,
                ToolStatus.EMPTY,
                (f"برای قانون {instrument_id!r} ماده‌ای یافت نشد",),
            )
        items = [
            {
                "id": str(row[0]),
                "label": row[1],
                "number": row[2],
                "summary": row[3],
            }
            for row in rows
        ]
        truncated = len(rows) >= limit and total > limit
        return make_envelope(
            store,
            tuple(items),
            status=ToolStatus.PARTIAL if truncated else ToolStatus.OK,
            total_count=total,
            complete=not truncated,
        )

    def _get_provision_text(self, store: RefStore, args: dict[str, Any]) -> Envelope:
        provision_id = require_str(args, "provision_id")
        unexpected = set(args) - {"provision_id"}
        if unexpected:
            raise GraphToolError(f"unexpected tool args: {', '.join(sorted(unexpected))}")
        if self.provision_text is None:
            return empty_envelope(
                store,
                ToolStatus.ERROR,
                ("دسترسی به متن کامل canonical در این نصب پیکربندی نشده است",),
            )
        try:
            record = self.provision_text(provision_id)
        except Exception as exc:  # noqa: BLE001 — surface lookup failures honestly
            return empty_envelope(store, ToolStatus.ERROR, (f"خواندن متن شکست خورد: {exc}",))
        if not record:
            return empty_envelope(
                store, ToolStatus.NOT_FOUND, (f"متن ماده {provision_id!r} یافت نشد",)
            )
        item = {
            "id": provision_id,
            "instrument_id": record.get("instrument_id"),
            "instrument_title": record.get("instrument_title"),
            "label": record.get("label"),
            "number": record.get("number"),
            "effective_from": record.get("effective_from"),
            "status": record.get("status"),
            "text": record.get("text") or "",
            "truncated": record.get("truncated", False),
        }
        return make_envelope(store, (item,))

    # --------------------------------------------------------- judicial layer

    def _search_judgments(self, store: RefStore, args: dict[str, Any]) -> Envelope:
        topic = require_str(args, "topic")
        kind = self._optional_judgment_kind(args)
        limit = self._optional_int(args, "limit", 12, (1, 40))
        tokens = [
            token for token in re.split(r"\s+", topic.lower().strip()) if len(token) >= 2
        ] or [topic.lower().strip()]
        params: dict[str, Any] = {}
        clauses = []
        for index, token in enumerate(tokens):
            key = f"t{index}"
            params[key] = token
            clauses.append(
                f"(toLower(x.title) CONTAINS ${key} "
                f"OR toLower(x.summary) CONTAINS ${key} "
                f"OR toLower(x.category) CONTAINS ${key})"
            )
        where = " OR ".join(clauses)
        plans = []
        if kind in (None, "judgment"):
            plans.append(
                (
                    "judgment",
                    "Judgment",
                    "MATCH (x:Judgment) WHERE "
                    + where
                    + " OPTIONAL MATCH (x)-[:CITES]->(i:LegalInstrument) "
                    "RETURN x.id, x.title, x.summary, x.category, count(i) "
                    "ORDER BY size(x.title) LIMIT 300",
                )
            )
        if kind in (None, "opinion"):
            plans.append(
                (
                    "opinion",
                    "AdvisoryOpinion",
                    "MATCH (x:AdvisoryOpinion) WHERE "
                    + where
                    + " OPTIONAL MATCH (x)-[:CITES]->(i:LegalInstrument) "
                    "RETURN x.id, x.title, x.summary, x.category, count(i) "
                    "ORDER BY size(x.title) LIMIT 300",
                )
            )
        scored: list[tuple[int, dict[str, Any]]] = []
        errors: list[str] = []
        for label, _falkor_label, cypher in plans:
            try:
                _, rows = self._run(cypher, params)
            except DomainError as exc:
                errors.append(f"{label}: {exc}")
                continue
            for row in rows:
                title = (row[1] or "").lower()
                summary = (row[2] or "").lower()
                category = (row[3] or "").lower()
                score = 0
                for token in tokens:
                    if token in title:
                        score += 4
                    if token in category:
                        score += 3
                    if token in summary:
                        score += 2
                if score:
                    scored.append(
                        (
                            score,
                            {
                                "id": str(row[0]),
                                "title": row[1],
                                "kind": label,
                                "label": label,
                                "category": row[3],
                                "summary": row[2],
                                "cites": int(row[4] or 0),
                            },
                        )
                    )
        if not scored:
            if errors:
                return empty_envelope(store, ToolStatus.ERROR, tuple(errors))
            return empty_envelope(
                store,
                ToolStatus.EMPTY,
                (f"رأی یا نظریه‌ای مرتبط با موضوع {topic!r} یافت نشد",),
            )
        scored.sort(key=lambda item: (-item[0], item[1]["title"] or ""))
        items = [item for _, item in scored[:limit]]
        truncated = len(scored) > limit
        return make_envelope(
            store,
            tuple(items),
            status=ToolStatus.PARTIAL if truncated else ToolStatus.OK,
            total_count=len(scored),
            complete=not truncated,
        )

    def _judgments_for_law(self, store: RefStore, args: dict[str, Any]) -> Envelope:
        law_ref = require_str(args, "law")
        kind = self._optional_judgment_kind(args)
        limit = self._optional_int(args, "limit", 12, (1, 40))
        law_id, law_title = self._resolve_law(law_ref)
        if law_id is None:
            return empty_envelope(
                store,
                ToolStatus.NOT_FOUND,
                (f"قانونی با شناسه یا عنوان {law_ref!r} یافت نشد",),
            )
        plans = []
        if kind in (None, "judgment"):
            plans.append(
                (
                    "judgment",
                    (
                        "MATCH (j:Judgment)-[c:CITES]->(i:LegalInstrument {id: $iid}) "
                        "RETURN j.id, j.title, j.summary, j.category, c.hits "
                        "ORDER BY c.hits DESC LIMIT $lim"
                    ),
                )
            )
        if kind in (None, "opinion"):
            plans.append(
                (
                    "opinion",
                    (
                        "MATCH (o:AdvisoryOpinion)-[c:CITES]->(i:LegalInstrument {id: $iid}) "
                        "RETURN o.id, o.title, o.summary, o.category, c.hits "
                        "ORDER BY c.hits DESC LIMIT $lim"
                    ),
                )
            )
        items: list[dict[str, Any]] = []
        errors: list[str] = []
        for label, cypher in plans:
            try:
                _, rows = self._run(cypher, {"iid": law_id, "lim": limit})
            except DomainError as exc:
                errors.append(f"{label}: {exc}")
                continue
            for row in rows:
                items.append(
                    {
                        "id": str(row[0]),
                        "title": row[1],
                        "kind": label,
                        "label": label,
                        "category": row[3],
                        "summary": row[2],
                        "hits": int(row[4] or 0),
                        "law_id": law_id,
                        "law_title": law_title,
                    }
                )
        if errors and not items:
            return empty_envelope(store, ToolStatus.ERROR, tuple(errors))
        if not items:
            return empty_envelope(
                store,
                ToolStatus.EMPTY,
                (f"رأی یا نظریه‌ای با استناد به {law_title or law_ref!r} یافت نشد",),
                meta={"law_id": law_id, "law_title": law_title},
            )
        items.sort(key=lambda item: (-item["hits"], item["title"] or ""))
        status = ToolStatus.ERROR if errors else ToolStatus.OK
        return make_envelope(
            store,
            tuple(items[:limit]),
            status=status,
            total_count=len(items),
            errors=tuple(errors),
            complete=not errors and len(items) <= limit,
            meta={"law_id": law_id, "law_title": law_title},
        )

    def _get_judgment_text(self, store: RefStore, args: dict[str, Any]) -> Envelope:
        judgment_id = require_str(args, "judgment_id")
        unexpected = set(args) - {"judgment_id"}
        if unexpected:
            raise GraphToolError(f"unexpected tool args: {', '.join(sorted(unexpected))}")
        if self.judgment_text is None:
            return empty_envelope(
                store,
                ToolStatus.ERROR,
                ("دسترسی به متن آرای قضایی در این نصب پیکربندی نشده است",),
            )
        try:
            record = self.judgment_text(judgment_id)
        except Exception as exc:  # noqa: BLE001 — surface lookup failures honestly
            return empty_envelope(store, ToolStatus.ERROR, (f"خواندن متن شکست خورد: {exc}",))
        if not record:
            return empty_envelope(
                store, ToolStatus.NOT_FOUND, (f"رأی/نظریه {judgment_id!r} یافت نشد",)
            )
        cited_laws: list[dict[str, Any]] = []
        try:
            _, rows = self._run(
                "MATCH (j {id: $jid})-[c:CITES]->(i:LegalInstrument) "
                "RETURN i.id, i.title, c.hits ORDER BY c.hits DESC LIMIT 30",
                {"jid": judgment_id},
            )
            cited_laws = [
                {"id": str(row[0]), "title": row[1], "hits": int(row[2] or 0)}
                for row in rows
            ]
        except DomainError:
            cited_laws = []
        item = {
            "id": judgment_id,
            "title": record.get("title"),
            "kind": record.get("kind") or "judgment",
            "label": record.get("kind") or "judgment",
            "category": record.get("category"),
            "issuer": record.get("issuer"),
            "date": record.get("date"),
            "summary": record.get("summary"),
            "text": record.get("text") or "",
            "truncated": record.get("truncated", False),
            "source_url": record.get("source_url"),
            "cited_laws": cited_laws,
            "evidence_id": None,
            "citation_note": (
                "متن رأی/نظریه شاهد قانونی (evidence) نیست؛ برای استناد، ماده‌های "
                "قانونیِ cited_laws را با search_provisions پیدا کن."
            ),
        }
        return make_envelope(store, (item,))

    @staticmethod
    def _optional_judgment_kind(args: dict[str, Any]) -> str | None:
        raw = args.get("kind")
        if raw is None:
            return None
        if not isinstance(raw, str):
            raise GraphToolError("kind must be a string")
        normalized = _JUDGMENT_KIND_ALIASES.get(raw.strip().lower(), raw.strip().lower())
        if normalized not in {"judgment", "opinion"}:
            raise GraphToolError("kind must be 'judgment' or 'opinion'")
        return normalized

    def _resolve_law(self, reference: str) -> tuple[str | None, str | None]:
        """Map a law id or title substring to (instrument_id, title)."""
        ref = reference.strip()
        params = {"ref": ref, "low": ref.lower()}
        try:
            _, rows = self._run(
                "MATCH (i:LegalInstrument) WHERE i.id = $ref "
                "RETURN i.id, i.title LIMIT 1",
                params,
            )
            if rows:
                return str(rows[0][0]), rows[0][1]
            _, rows = self._run(
                "MATCH (i:LegalInstrument) WHERE toLower(i.title) CONTAINS $low "
                "RETURN i.id, i.title ORDER BY size(i.title) LIMIT 1",
                params,
            )
        except DomainError:
            return None, None
        if not rows:
            return None, None
        return str(rows[0][0]), rows[0][1]

    def _node_neighbors(self, store: RefStore, args: dict[str, Any]) -> Envelope:
        node_id = require_str(args, "node_id")
        rel_types = self._optional_rel_types(args)
        limit = self._optional_limit(args, self.items_per_page)
        identity = "(n.id = $nid OR n.name = $nid)"
        rel_filter = " AND ($rels IS NULL OR type(r) IN $rels)"
        projection = (
            "RETURN DISTINCT type(r) AS rel, labels(m)[0] AS label, "
            "coalesce(m.id, m.name) AS id, coalesce(m.title, m.label, m.name) AS title "
            "ORDER BY rel, title LIMIT $lim"
        )
        out_query = (
            f"MATCH (n)-[r]->(m) WHERE {identity}{rel_filter} {projection}"
        )
        in_query = (
            f"MATCH (n)<-[r]-(m) WHERE {identity}{rel_filter} {projection}"
        )
        plans = (("out", out_query), ("in", in_query))
        params = {"nid": node_id, "rels": list(rel_types) if rel_types else None, "lim": limit}
        items: list[dict[str, Any]] = []
        errors: list[str] = []
        for direction, cypher in plans:
            try:
                _, rows = self._run(cypher, params)
            except DomainError as exc:
                errors.append(f"{direction}: {exc}")
                continue
            for row in rows:
                items.append(
                    {
                        "id": str(row[2]),
                        "label": (row[1] or "").lower() or "node",
                        "title": row[3],
                        "rel": row[0],
                        "direction": direction,
                    }
                )
        if errors and not items:
            return empty_envelope(store, ToolStatus.ERROR, tuple(errors))
        if not items:
            return empty_envelope(
                store,
                ToolStatus.NOT_FOUND,
                (f"گره {node_id!r} یافت نشد یا همسایه‌ای ندارد",),
            )
        status = ToolStatus.ERROR if errors else ToolStatus.OK
        return make_envelope(
            store,
            tuple(items[:limit]),
            status=status,
            total_count=len(items),
            errors=tuple(errors),
            complete=not errors and len(items) <= limit,
        )

    def _find_paths(self, store: RefStore, args: dict[str, Any]) -> Envelope:
        source_id = require_str(args, "source_id")
        target_id = require_str(args, "target_id")
        max_depth = self._optional_int(args, "max_depth", 3, (1, 4))
        unexpected = set(args) - {"source_id", "target_id", "max_depth"}
        if unexpected:
            raise GraphToolError(f"unexpected tool args: {', '.join(sorted(unexpected))}")
        # FalkorDB restrictions honoured here: shortestPath is only allowed in
        # WITH/RETURN, undirected traversals are unsupported, and parameters
        # cannot appear inside variable-length bounds — so the validated
        # integer is inlined and both directions are probed separately.
        template = (
            "MATCH (a), (b) WHERE (a.id = $src OR a.name = $src) "
            "AND (b.id = $dst OR b.name = $dst) "
            "WITH a, b, shortestPath((a)-[*1..{depth}]->(b)) AS p "
            "RETURN [x IN nodes(p) | coalesce(x.id, x.name)] AS ids, "
            "[x IN nodes(p) | coalesce(x.title, x.label, x.name)] AS titles, "
            "[x IN nodes(p) | toLower(labels(x)[0])] AS labels, "
            "[r IN relationships(p) | type(r)] AS rels "
            "LIMIT 1"
        )
        cypher = template.replace("{depth}", str(max_depth))
        try:
            rows = self._path_rows(cypher, source_id, target_id)
            direction = "forward"
            if not rows:
                rows = self._path_rows(cypher, target_id, source_id)
                direction = "reverse"
        except DomainError as exc:
            return empty_envelope(
                store,
                ToolStatus.ERROR,
                (f"یافتن مسیر شکست خورد: {exc}",),
            )
        if not rows or not rows[0][0]:
            return empty_envelope(
                store,
                ToolStatus.EMPTY,
                (
                    f"مسیری تا عمق {max_depth} بین {source_id!r} و {target_id!r} یافت نشد",
                ),
            )
        ids, titles, labels, rels = rows[0]
        if direction == "reverse":
            # Normalise the presentation to source→target.
            ids, titles, labels, rels = (
                list(reversed(ids)),
                list(reversed(titles)),
                list(reversed(labels)),
                list(reversed(rels)),
            )
        steps: list[dict[str, Any]] = [
            {"id": str(node_id), "label": label, "title": title}
            for node_id, label, title in zip(ids, labels, titles)
        ]
        item = {"path": steps, "rels": [str(rel) for rel in rels]}
        return make_envelope(store, (item,))

    def _path_rows(self, cypher: str, src: str, dst: str) -> list[list[Any]]:
        _, rows = self._run(cypher, {"src": src, "dst": dst})
        # FalkorDB yields one row of Nones when shortestPath finds nothing.
        return [row for row in rows if row and row[0]]

    def _run_cypher(self, store: RefStore, args: dict[str, Any]) -> Envelope:
        query = require_str(args, "query")
        unexpected = set(args) - {"query"}
        if unexpected:
            raise GraphToolError(f"unexpected tool args: {', '.join(sorted(unexpected))}")
        cleaned = query.strip().rstrip(";").strip()
        if ";" in cleaned:
            raise GraphToolError("only a single Cypher statement is allowed")
        if _WRITE_KEYWORDS.search(cleaned):
            raise GraphToolError(
                "write keywords are not allowed in the graph chat (read-only)"
            )
        upper = cleaned.upper()
        if not any(upper.startswith(prefix) for prefix in _STATEMENT_PREFIXES):
            raise GraphToolError(
                "statement must start with one of: " + ", ".join(_STATEMENT_PREFIXES)
            )
        try:
            columns, rows = self._run(cleaned, {})
        except DomainError as exc:
            return empty_envelope(store, ToolStatus.ERROR, (str(exc),))
        if not rows:
            return empty_envelope(
                store, ToolStatus.EMPTY, ("کوئری صفر ردیف برگرداند",)
            )
        truncated = len(rows) > self.max_cypher_rows
        items = [
            {column: _to_json(value) for column, value in zip(columns, row)}
            for row in rows[: self.max_cypher_rows]
        ]
        return make_envelope(
            store,
            tuple(items),
            status=ToolStatus.PARTIAL if truncated else ToolStatus.OK,
            total_count=len(rows),
            errors=("ردیف‌ها محدود شد" if truncated else ()),
            complete=not truncated,
            meta={"columns": columns},
        )

    # ------------------------------------------------------------- helpers

    def _run(self, query: str, params: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
        """Run a read-only query; every failure becomes a DomainError."""
        self._query_log.append({"query": query, "params": params or {}})
        try:
            result = self.graph.ro_query(query, params or None)
        except Exception as exc:
            raise DomainError(f"cypher failed: {str(exc)[:300]}") from exc
        header = getattr(result, "header", None) or []
        columns = [str(column[1]) for column in header]
        return columns, [list(row) for row in result.result_set]

    @staticmethod
    def _no_args(args: dict[str, Any], allowed: tuple[str, ...]) -> None:
        unexpected = set(args) - set(allowed)
        if unexpected:
            raise GraphToolError(f"unexpected tool args: {', '.join(sorted(unexpected))}")

    @staticmethod
    def _require_query(args: dict[str, Any]) -> str:
        query = require_str(args, "query")
        unexpected = set(args) - {"query", "label", "limit"}
        if unexpected:
            raise GraphToolError(f"unexpected tool args: {', '.join(sorted(unexpected))}")
        if len(query) < 2:
            raise GraphToolError("query must contain at least 2 characters")
        return query

    @staticmethod
    def _optional_label(args: dict[str, Any]) -> str | None:
        raw = args.get("label")
        if raw is None:
            return None
        if not isinstance(raw, str):
            raise GraphToolError("label must be a string")
        normalized = _LABEL_ALIASES.get(raw.strip().lower(), raw.strip().lower())
        if normalized not in _ALLOWED_LABELS:
            raise GraphToolError(
                "label must be one of: " + ", ".join(sorted(_ALLOWED_LABELS))
            )
        return normalized

    @staticmethod
    def _optional_rel_types(args: dict[str, Any]) -> tuple[str, ...]:
        raw = args.get("rel_types")
        if raw is None:
            return ()
        if not isinstance(raw, (list, tuple)) or not all(isinstance(item, str) for item in raw):
            raise GraphToolError("rel_types must be a list of strings")
        types = tuple(item.strip().upper() for item in raw if item.strip())
        for item in types:
            if not re.fullmatch(r"[A-Z_]{2,40}", item):
                raise GraphToolError(f"invalid relationship type {item!r}")
        return types

    def _optional_limit(self, args: dict[str, Any], default: int) -> int:
        return self._optional_int(args, "limit", default, (1, 50))

    @staticmethod
    def _optional_int(
        args: dict[str, Any], key: str, default: int, bounds: tuple[int, int]
    ) -> int:
        raw = args.get(key)
        if raw is None:
            return default
        if isinstance(raw, bool) or not isinstance(raw, int):
            raise GraphToolError(f"{key} must be an integer")
        low, high = bounds
        if not low <= raw <= high:
            raise GraphToolError(f"{key} must be between {low} and {high}")
        return raw


def require_str(args: dict[str, Any], key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise GraphToolError(f"{key} must be a non-empty string")
    return value.strip()


def _to_json(value: Any) -> Any:
    """Serialize FalkorDB entities the same way the /graph/cypher endpoint does."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    properties = getattr(value, "properties", None)
    if properties is not None:
        labels = getattr(value, "labels", None)
        if labels is not None:
            return {
                "id": str(getattr(value, "id", "")),
                "label": (
                    ",".join(sorted(labels))
                    if isinstance(labels, (set, frozenset, list, tuple))
                    else labels
                ),
                "properties": {str(k): _to_json(v) for k, v in dict(properties).items()},
            }
        return {
            "id": str(getattr(value, "id", "")),
            "label": getattr(value, "relation", "RELATES"),
            "from": str(getattr(value, "src_node", "")),
            "to": str(getattr(value, "dest_node", "")),
            "properties": {str(k): _to_json(v) for k, v in dict(properties).items()},
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_to_json(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _to_json(item) for key, item in value.items()}
    nodes = getattr(value, "nodes", None)
    if callable(nodes):
        return {
            "nodes": [_to_json(node) for node in value.nodes()],
            "relationships": [_to_json(edge) for edge in value.edges()],
        }
    return str(value)


#: Node type to assume for tools whose result items carry no explicit type.
_NODE_LABEL_DEFAULTS = {
    "search_laws": "instrument",
    "list_provisions": "provision",
    "get_provision_text": "provision",
    "search_judgments": "judgment",
    "judgments_for_law": "judgment",
    "get_judgment_text": "judgment",
}

#: Valid graph node types an item's ``label`` field may name directly.
_KNOWN_NODE_LABELS = frozenset(
    {"instrument", "provision", "judgment", "opinion", "authority", "domain"}
)


def harvest_refs(
    tool: str, envelope: Envelope, args: dict[str, Any] | None = None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Extract clickable nodes and relations from a tool envelope (UI only).

    Works for both graph tools (``id``/``label``/``rel``/``path`` items) and
    canonical research tools (``provision_id``/``instrument_id`` items). It is
    best-effort: unknown shapes are skipped, never fatal.
    """
    args = args or {}
    default_label = _NODE_LABEL_DEFAULTS.get(tool)
    focus_id = (
        args.get("node_id")
        or args.get("instrument_id")
        or args.get("provision_id")
        or args.get("judgment_id")
        or args.get("source_id")
    )
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add_node(node_id: Any, label: Any, title: Any) -> None:
        if not isinstance(node_id, str) or not node_id or node_id in seen:
            return
        seen.add(node_id)
        normalized = (
            label.lower()
            if isinstance(label, str) and label.lower() in _KNOWN_NODE_LABELS
            else (default_label or "node")
        )
        nodes.append(
            {"id": node_id, "label": normalized, "title": str(title or node_id)}
        )

    for item in envelope.items:
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        if isinstance(path, list):
            rels = item.get("rels") or []
            for step in path:
                if isinstance(step, dict):
                    add_node(step.get("id"), step.get("label"), step.get("title"))
            for index in range(len(path) - 1):
                left = path[index].get("id") if isinstance(path[index], dict) else None
                right = (
                    path[index + 1].get("id")
                    if isinstance(path[index + 1], dict)
                    else None
                )
                if left and right:
                    edges.append(
                        {
                            "source": left,
                            "target": right,
                            "rel": rels[index] if index < len(rels) else None,
                        }
                    )
            continue

        node_id = item.get("id")
        if node_id:
            add_node(node_id, item.get("label"), item.get("title") or item.get("number"))
            rel = item.get("rel")
            if rel and focus_id:
                if item.get("direction") == "in":
                    edges.append({"source": node_id, "target": focus_id, "rel": rel})
                else:
                    edges.append({"source": focus_id, "target": node_id, "rel": rel})
            law_id = item.get("law_id")
            if law_id and law_id != node_id:
                add_node(law_id, "instrument", item.get("law_title") or law_id)
                edges.append({"source": law_id, "target": node_id, "rel": "CITES"})
            continue

        # canonical research-tool items
        provision_id = item.get("provision_id")
        instrument_id = item.get("instrument_id")
        if provision_id:
            add_node(
                provision_id,
                "provision",
                item.get("label") or item.get("number") or provision_id,
            )
            if instrument_id:
                add_node(
                    instrument_id, "instrument", item.get("instrument_title") or instrument_id
                )
                edges.append(
                    {"source": instrument_id, "target": provision_id, "rel": "CONTAINS"}
                )
        elif instrument_id:
            add_node(instrument_id, "instrument", item.get("instrument_title") or instrument_id)

    return nodes, edges


__all__ = [
    "GRAPH_TOOL_SPECS",
    "GraphToolError",
    "GraphTools",
    "graph_tool_schemas",
    "harvest_refs",
    "search_graph",
]
