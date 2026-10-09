"""Deterministic parent assignment for structural legal provisions.

Iranian legal drafting is hierarchical («بخش» > «فصل» > «ماده» > «تبصره/بند»)
but the parsers produce a flat document-ordered list. This module derives the
forest with a rank-based stack walk:

1. بخش (PART) — top container
2. فصل (CHAPTER)
3. ماده (ARTICLE, incl. «ماده واحده» / ordinal «آیین‌نامه» labels)
4. تبصره/بند/شماره‌گذاری‌های پراکنده (NOTE/CLAUSE/list items)

A new item attaches to the nearest open item of strictly lower rank, so
materials nest under the current chapter, notes and clauses under the current
article, and a new chapter/part closes the previous one. Parents always
precede children in document order, so a pre-order walk of the forest equals
the original flat order (ordinals stay stable).

The same rules are shared by the markdown parser (index-time) and the
backfill script for already-ingested corpora (``scripts/backfill_provision_parents.py``).
"""

from __future__ import annotations

from collections.abc import Sequence

RANK_PART = 1
RANK_CHAPTER = 2
RANK_ARTICLE = 3
RANK_SUB = 4

_RANK_NAMES = {
    RANK_PART: "part",
    RANK_CHAPTER: "chapter",
    RANK_ARTICLE: "article",
    RANK_SUB: "sub",
}


def hierarchy_rank(provision_type_value: str, label: str) -> int:
    """Rank of one provision from its canonical type and raw label.

    Bare numbered list items («۱ ـ») are typed ``article`` but carry no
    «ماده» prefix; they behave like clauses and nest under the current article.
    """
    cleaned = label.strip()
    if provision_type_value == "part":
        return RANK_PART
    if provision_type_value == "chapter":
        return RANK_CHAPTER
    if provision_type_value == "article" and not cleaned.startswith(("ماده", "آیین‌نامه")):
        return RANK_SUB
    if provision_type_value == "article":
        return RANK_ARTICLE
    return RANK_SUB


def rank_name(rank: int) -> str:
    return _RANK_NAMES[rank]


def assign_parents(ranks: Sequence[int]) -> tuple[int | None, ...]:
    """Parent index per item (document order), or None for forest roots."""
    parents: list[int | None] = []
    stack: list[tuple[int, int]] = []
    for index, rank in enumerate(ranks):
        while stack and stack[-1][1] >= rank:
            stack.pop()
        parents.append(stack[-1][0] if stack else None)
        stack.append((index, rank))
    return tuple(parents)


def depths_from_parents(parents: Sequence[int | None]) -> tuple[int, ...]:
    """Depth per item; roots are depth 0."""
    depths: list[int] = []
    for index, parent in enumerate(parents):
        depths.append(0 if parent is None else depths[parent] + 1)
    return tuple(depths)
