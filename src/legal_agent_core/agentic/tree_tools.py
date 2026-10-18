"""Read-only display-tree navigation tools (docs/qavanin/06_law_notebook.md §4).

These let the agent descend the conceptual tree the way a programmer walks a
codebase: ``list_tree`` shows the children of a folder/law, ``walk_tree`` maps a
topic to the path(s) from the root down to the relevant documents, each hop
labelled with its ``basis`` (``derivation`` from an implements edge, or
``topic``). The tree is a *projection* — navigation only, never a legal claim
and never Evidence.
"""

from __future__ import annotations

import re
from itertools import pairwise
from typing import Any

from ..ingestion.normalization import normalize_legal_text
from .envelope import RefStore, ToolStatus, empty_envelope, make_envelope
from .graph_tools import GraphToolError, require_str
from .tools import ToolSpec

TREE_TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "list_tree",
        "فرزندان یک گرهٔ درخت مفهومی (شاخه/موضوع/سند) با خلاصه و برچسب basis — "
        "برای پیمایش پوشه‌به‌پوشه مثل cd کردن",
        ("node_id?",),
    ),
    ToolSpec(
        "walk_tree",
        "مسیر(های) از ریشهٔ درخت تا اسناد مرتبط با یک موضوع، با دلیل هر پرش "
        "(derivation|topic) — کوتاه‌ترین راه رسیدن به قانون درست",
        ("topic", "limit?"),
    ),
)

TREE_TOOL_NAMES = frozenset(spec.name for spec in TREE_TOOL_SPECS)

_MAX_PATH_STEPS = 12

# Question prose contains many high-frequency words that happen to occur in
# generated document summaries.  Counting them made an unrelated instrument
# with «از / به / در» outrank the law named by the legal concepts.  This list
# deliberately contains only function/question words; legal nouns remain
# searchable and are still weighted by inverse document frequency below.
_TOPIC_STOPWORDS = frozenset(
    {
        "از",
        "به",
        "با",
        "بر",
        "برای",
        "در",
        "را",
        "که",
        "تا",
        "یا",
        "اگر",
        "آیا",
        "اما",
        "پس",
        "چون",
        "این",
        "آن",
        "یک",
        "هر",
        "چه",
        "چند",
        "چگونه",
        "کدام",
        "است",
        "نیست",
        "باشد",
        "بودن",
        "شود",
        "شده",
        "شوند",
        "دارد",
        "کند",
        "کرد",
        "می",
        "تواند",
        "صرفا",
        "بعد",
        "قبل",
        "مورد",
        "نوع",
        "هم",
        "خود",
        "ای",
        "های",
        "هایی",
        "داشته",
        "ندارد",
        "قابل",
        "مهم",
        "زمانی",
    }
)


def _topic_tokens(value: str) -> tuple[str, ...]:
    """Return stable, content-bearing tokens for conceptual navigation."""

    normalized = normalize_legal_text(value).casefold().replace("\u200c", " ")
    # Avoid the whole Arabic Unicode block: it also contains punctuation such
    # as U+060C, which previously produced tokens like «بدهی،» beside «بدهی».
    raw = re.findall(
        r"[0-9\u0621-\u063a\u0641-\u064a\u067e\u0686\u0698\u06a9\u06af\u06cc]+",
        normalized,
    )
    # Preserve order because it gives useful, readable diagnostics.  Repeated
    # words (for example «اعتراض» twice in a question) must not double-score.
    return tuple(
        dict.fromkeys(
            _stem_token(token)
            for token in raw
            if len(token) >= 2 and token not in _TOPIC_STOPWORDS
        )
    )


def _token_in(query_token: str, field_tokens: set[str]) -> bool:
    """Match normalized tokens without substring noise."""

    return query_token in field_tokens


def _stem_token(token: str) -> str:
    """Fold a few productive Persian plural suffixes used in legal titles."""

    if len(token) > 6 and token.endswith("گان"):
        return token[:-3] + "ه"
    if len(token) > 5 and token.endswith("یان"):
        return token[:-3]
    if len(token) > 5 and token.endswith("های"):
        return token[:-3]
    if len(token) > 4 and token.endswith("ها"):
        return token[:-2]
    return token


class TreeTools:
    """Deterministic read-only navigation over the display-tree projection."""

    def __init__(self, tree: dict[str, Any] | None) -> None:
        self.tree = tree or {}
        self._by_id: dict[str, dict[str, Any]] = {}
        self._parent: dict[str, str] = {}
        self._search_candidates: list[
            tuple[dict[str, Any], dict[str, set[str]]]
        ] = []
        if self.tree:
            self._index(self.tree, None)
            self._build_search_candidates()

    def _build_search_candidates(self) -> None:
        """Tokenize the static tree once, rather than once per question."""

        for node in self._by_id.values():
            if node.get("kind") not in {"instrument", "domain", "branch"}:
                continue
            fields = {
                field: set(_topic_tokens(str(node.get(field) or "")))
                for field in ("label", "title", "summary", "domain")
            }
            self._search_candidates.append((node, fields))

    def _index(self, node: dict[str, Any], parent_id: str | None) -> None:
        node_id = node.get("id")
        if isinstance(node_id, str):
            self._by_id[node_id] = node
            if parent_id is not None:
                self._parent[node_id] = parent_id
        for child in node.get("children", []):
            self._index(child, node_id)

    # ------------------------------------------------------------- dispatch
    def dispatch(self, store: RefStore, name: str, args: dict[str, Any]) -> Any:
        if name not in TREE_TOOL_NAMES:
            raise GraphToolError(f"unknown tree tool {name!r}")
        if not self.tree:
            return empty_envelope(
                store,
                ToolStatus.ERROR,
                ("درخت نمایشی در دسترس نیست؛ scripts/build_display_tree.py را اجرا کنید",),
            )
        return getattr(self, f"_{name}")(store, args)

    def tool_catalog(self) -> str:
        return "\n".join(
            f"{spec.signature()} — {spec.description}" for spec in TREE_TOOL_SPECS
        )

    # ---------------------------------------------------------------- tools
    def _list_tree(self, store: RefStore, args: dict[str, Any]) -> Any:
        node_id = args.get("node_id")
        if node_id is not None and not isinstance(node_id, str):
            raise GraphToolError("node_id must be a string")
        node = self._by_id.get(node_id) if node_id else self.tree
        if node is None:
            return empty_envelope(
                store, ToolStatus.NOT_FOUND, (f"گره {node_id!r} در درخت یافت نشد",)
            )
        children = node.get("children", [])
        items = tuple(self._node_item(child) for child in children)
        item = {
            "node_id": node.get("id"),
            "kind": node.get("kind"),
            "label": node.get("label"),
            "summary": node.get("summary"),
            "basis": node.get("basis"),
            "child_count": len(children),
            "children": items,
        }
        return make_envelope(store, (item,), total_count=len(children))

    def _walk_tree(self, store: RefStore, args: dict[str, Any]) -> Any:
        topic = require_str(args, "topic")
        limit = args.get("limit", 5)
        if not isinstance(limit, int) or isinstance(limit, bool):
            raise GraphToolError("limit must be an integer")
        limit = max(1, min(limit, 20))
        tokens = list(_topic_tokens(topic))
        if not tokens:
            tokens = [normalize_legal_text(topic).casefold().strip()]
        query_bigrams = set(pairwise(tokens))

        # Candidate nodes with field-aware token sets.  Titles are stronger
        # than generated summaries, while domains are useful only as a weak
        # fallback.
        candidates = self._search_candidates

        # Inverse-document-frequency weighting: a rare token like «مفاصاحساب»
        # must outweigh a ubiquitous one like «بدهی». Without this the ranking
        # collapses onto whichever documents share the most common words.
        import math

        doc_freq = {token: 0 for token in tokens}
        for _node, fields in candidates:
            haystack = set().union(*fields.values())
            for token in tokens:
                if _token_in(token, haystack):
                    doc_freq[token] += 1
        total = max(len(candidates), 1)

        def token_weight(token: str) -> float:
            df = doc_freq[token]
            if df == 0:
                return 0.0
            return math.log(1 + total / df)

        scored: list[tuple[float, dict[str, Any], tuple[str, ...]]] = []
        for node, fields in candidates:
            score = 0.0
            matched_tokens: list[str] = []
            for token in tokens:
                field_weight = 0.0
                if _token_in(token, fields["label"]) or _token_in(
                    token, fields["title"]
                ):
                    field_weight = 4.5
                elif _token_in(token, fields["summary"]):
                    field_weight = 0.7
                elif _token_in(token, fields["domain"]):
                    field_weight = 0.6
                if field_weight:
                    matched_tokens.append(token)
                    kind_weight = 1.3 if node.get("kind") == "instrument" else 1.0
                    score += token_weight(token) * field_weight * kind_weight
            if score <= 0:
                continue
            # A long narrative must share at least two distinct legal concepts
            # with a candidate.  A single-concept topic such as «مهریه» remains
            # valid, preserving the filesystem-style navigation use case.
            minimum_matches = 1 if len(tokens) <= 3 else 2
            if len(matched_tokens) < minimum_matches:
                continue
            # reward covering more distinct query tokens (a document matching
            # both «مفاصاحساب» and «وصول» beats one matching «بدهی» twice)
            score *= 1.0 + 0.25 * len(matched_tokens)
            scored.append((score, node, tuple(matched_tokens)))
        if not scored:
            return empty_envelope(
                store, ToolStatus.EMPTY, (f"درختی برای موضوع {topic!r} یافت نشد",)
            )
        scored.sort(key=lambda item: (-item[0], len(str(item[1].get("label") or ""))))
        items: list[dict[str, Any]] = []
        seen_paths: set[tuple[str, ...]] = set()
        for score, node, matched_tokens in scored:
            path = self._path(node)
            key = tuple(step["id"] for step in path)
            if key in seen_paths:
                continue
            seen_paths.add(key)
            title_token_sequence = _topic_tokens(
                " ".join(
                    str(node.get(field) or "") for field in ("label", "title")
                )
            )
            title_tokens = set(title_token_sequence)
            title_matches = [
                token for token in matched_tokens if _token_in(token, title_tokens)
            ]
            matched_phrases = [
                " ".join(pair)
                for pair in query_bigrams.intersection(
                    pairwise(title_token_sequence)
                )
            ]
            coverage = len(matched_tokens) / max(len(tokens), 1)
            confidence = (
                "strong"
                if matched_phrases or len(title_matches) >= 4 or coverage >= 0.4
                else "weak"
            )
            items.append(
                {
                    "target_id": node.get("id"),
                    "target_label": node.get("label"),
                    "kind": node.get("kind"),
                    "matched_tokens": list(matched_tokens),
                    "title_matched_tokens": title_matches,
                    "title_matched_phrases": sorted(matched_phrases),
                    "coverage": round(coverage, 3),
                    "route_confidence": confidence,
                    "rank_score": round(score, 3),
                    "path": path,
                }
            )
            if len(items) >= limit:
                break
        return make_envelope(store, tuple(items), total_count=len(items))

    # -------------------------------------------------------------- helpers
    def _path(self, node: dict[str, Any]) -> list[dict[str, Any]]:
        chain: list[dict[str, Any]] = []
        current: dict[str, Any] | None = node
        guard = 0
        while current is not None and guard < _MAX_PATH_STEPS:
            chain.append(
                {
                    "id": current.get("id"),
                    "label": current.get("label"),
                    "kind": current.get("kind"),
                    "basis": current.get("basis"),
                }
            )
            parent_id = self._parent.get(str(current.get("id")))
            current = self._by_id.get(parent_id) if parent_id else None
            guard += 1
        chain.reverse()
        return chain

    @staticmethod
    def _node_item(node: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": node.get("id"),
            "kind": node.get("kind"),
            "label": node.get("label"),
            "tier": node.get("tier"),
            "basis": node.get("basis"),
            "summary": node.get("summary"),
            "child_count": len(node.get("children", [])),
        }


__all__ = ["TREE_TOOL_NAMES", "TREE_TOOL_SPECS", "TreeTools"]
