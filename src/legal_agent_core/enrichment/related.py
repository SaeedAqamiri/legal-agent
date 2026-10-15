"""Shared logic for the curated related-documents block and bullet matching.

Extracted from scripts/ingest_source_related.py so both the ingest script and
the enrichment quality cycle (legal_agent_core.enrichment.quality) operate on
one implementation. Pure functions only — no DB access here.
"""

from __future__ import annotations

import difflib
import re

from legal_agent_core.ingestion.normalization import normalize_legal_text

CATEGORY_LABELS = {
    "آیین‌نامه", "آیین نامه", "قوانین مرتبط", "رای وحدت رویه", "رأی وحدت رویه",
    "نظریه مشورتی",
}
RULING_RE = re.compile(r"^ر[أا]ی\s+وحدت\s+رویه")
LAW_RE = re.compile(r"^(قانون|لایحه)")
REGULATION_RE = re.compile(
    r"^(آیین[\u200c\s-]*نامه|دستورالعمل|تصویب[\u200c\s-]*نامه|بخشنامه|مصوبه|شیوه[\u200c\s]*نامه)"
)
RULING_NUMBER_RE = re.compile(r"شماره[\s\u200c]*([۰-۹0-9]+)")
RELATED_HEADING_RE = re.compile(r"^#+\s*مستندات\s+مرتبط")
BULLET_RE = re.compile(r"^[-*]\s+(.+)$")
# doc-kind prefixes: a «قانون …» bullet must not degrade to a «طرح …» or
# «آیین‌نامه …» candidate that merely mentions it
KIND_PATTERNS = (
    ("law", re.compile(r"^(قانون|لایحه)")),
    ("bill", re.compile(r"^طرح")),
    ("regulation", re.compile(
        r"^(آیین[\u200c\s-]*نامه|دستورالعمل|تصویب[\u200c\s-]*نامه|بخشنامه|مصوبه|شیوه[\u200c\s]*نامه)")),
)


def _kind(text: str) -> str | None:
    for kind, pattern in KIND_PATTERNS:
        if pattern.match(_fold(text).strip()):
            return kind
    return None


def parse_related_block(text: str) -> list[str]:
    """Bullets of the «مستندات مرتبط» block, category links removed."""
    lines = text.splitlines()
    bullets: list[str] = []
    in_block = False
    for line in lines:
        stripped = line.strip()
        if not in_block:
            if RELATED_HEADING_RE.match(stripped):
                in_block = True
            continue
        if not stripped:
            continue
        match = BULLET_RE.match(stripped)
        if match is None:
            break  # first non-bullet line ends the block (law body starts)
        item = match.group(1).strip()
        if item and item not in CATEGORY_LABELS:
            bullets.append(item)
    return bullets


def _fold(text: str) -> str:
    """Normalization for matching: fold ZWNJ/controls to spaces and collapse.
    Persian titles vary freely between «رایانه‌ای/رایانه ای» etc."""
    folded = re.sub(r"[\u200c\u200e\u200f_]+", " ", normalize_legal_text(text or ""))
    return re.sub(r"\s+", " ", folded).strip()


def _core_variants(key: str) -> list[str]:
    """Progressively shorter prefixes of a bullet (parentheticals removed) so a
    short bullet name can sit inside a longer catalog title
    («قانون اساسی» ⊆ «قانون اساسی جمهوری اسلامی ایران مصوب 1358»)."""
    key = re.sub(r"\([^)]*\)", " ", key)
    key = re.sub(r"\s+", " ", key).strip()
    tokens = key.split()
    variants = [key]
    for i in range(len(tokens) - 1, 2, -1):
        variants.append(" ".join(tokens[:i]))
    return variants


_FOLD_TITLE_CACHE: dict[str, str] = {}
_FLEX_CACHE: dict[str, re.Pattern] = {}


def _fold_title(norm_title: str) -> str:
    folded = _FOLD_TITLE_CACHE.get(norm_title)
    if folded is None:
        folded = _fold(norm_title)
        _FOLD_TITLE_CACHE[norm_title] = folded
    return folded


def _flex_pattern(core: str) -> re.Pattern:
    """Word-boundary-safe flexible-space containment: every space in the core
    matches zero-or-more spaces/ZWNJ, so «مالیات های» also finds «مالیاتهای»,
    while «قانون کار» still refuses «قانون کارگر»."""
    pattern = _FLEX_CACHE.get(core)
    if pattern is None:
        pattern = _FLEX_CACHE[core] = re.compile(
            r"[\s\u200c]*".join(map(re.escape, core.split()))
            + r"(?=$|[\s/٫،؛,.:»(\[])"
        )
    return pattern


def match_bullet(phrase: str, catalog_by_key: dict[str, str], sorted_titles: list[str]) -> str | None:
    """Bullet title -> instrument_id. Everything is compared on FOLDED strings
    (ZWNJ -> space) — catalog titles themselves vary in ZWNJ/space usage.
      1. longest catalog title flexibly contained in the bullet (no amendment
         guard: «قانون اصلاح X» names the amending law itself), preferring
         titles that carry one of the bullet's hijri years;
      2. bullet prefix flexibly contained in a title, year-disambiguated
         («قانون مجازات عمومی مصوب ۱۳۵۲» -> the 1352 title);
      3. difflib fuzzy >= 0.85 (spelling variants «آئین‌نامه/آیین‌نامه»).
    """
    key = _fold(phrase)
    if not key:
        return None
    years = set(re.findall(r"\b(1[0-9]{3})\b", key))
    bullet_kind = _kind(phrase)
    titles = [
        (ft, t) for ft, t in ((_fold_title(t), t) for t in sorted_titles)
        if bullet_kind is None or _kind(t) in (bullet_kind, None)
    ]  # (folded, original_key), kind-compatible candidates only

    contained = [(ft, t) for ft, t in titles if ft and _flex_pattern(ft).search(key)]
    tier1_fallback = None
    if contained:
        dated = [(ft, t) for ft, t in contained if any(y in ft for y in years)] if years else []
        if dated:
            return catalog_by_key[max(dated, key=lambda p: len(p[0]))[1]]
        if not years:
            return catalog_by_key[max(contained, key=lambda p: len(p[0]))[1]]
        # the bullet carries a hijri year none of the contained titles has
        # («قانون کار (منسوخ) مصوب ۱۳۳۷…» also contains plain «قانون کار») —
        # the intended doc is likely a longer title; try tier 2 first. A LONG
        # contained title already identifies the law («قانون رفع موانع تولید
        # رقابت‌پذیر …» + «مصوب ۱۳۹۴» bullet) — the year is just the bullet's
        # date annotation there
        longest = max(contained, key=lambda p: len(p[0]))
        if len(longest[0]) >= 15:
            return catalog_by_key[longest[1]]
        tier1_fallback = catalog_by_key[longest[1]]

    weak = None
    weak_ambig = None
    bullet_tokens = len(key.split())
    for core in _core_variants(key):
        if len(core) < 8:
            break
        hits = [(ft, t) for ft, t in titles if _flex_pattern(core).search(ft)]
        if not hits:
            continue
        if years:
            # a unique year match is the strongest signal — allow it even on
            # heavily trimmed cores (word order in titles/bullets varies)
            dated = [(ft, t) for ft, t in hits if any(y in ft for y in years)]
            if len(dated) == 1:
                return catalog_by_key[dated[0][1]]
        # a trimmed core must still cover most of the bullet's words for the
        # weaker heuristics, else the match degrades to a generic sibling law
        # («قانون آیین دادرسی …» -> «قانون آیین دادرسی کیفری» while the bullet
        # meant the absent مدنی one)
        if bullet_tokens and len(core.split()) / bullet_tokens < 0.6:
            break
        # no title carries the bullet's year — the year is just the bullet's
        # date annotation; the weaker heuristics still apply (they resolve
        # after tier1_fallback below)
        if len(hits) == 1:
            weak = weak or catalog_by_key[hits[0][1]]
        elif len(core) >= 10:
            # ambiguous but specific enough: the shortest title is the most
            # specific one («قانون اساسی» -> «قانون اساسی جمهوری …»)
            weak_ambig = weak_ambig or catalog_by_key[min(hits, key=lambda p: len(p[0]))[1]]

    if tier1_fallback is not None:
        return tier1_fallback
    if weak is not None:
        return weak
    if weak_ambig is not None:
        return weak_ambig
    best_title, best_ratio = None, 0.0
    for ft, t in titles:
        ratio = difflib.SequenceMatcher(None, key, ft).ratio()
        if ratio > best_ratio:
            best_title, best_ratio = t, ratio
    if best_ratio >= 0.85:
        return catalog_by_key[best_title]
    return None


def pick_ruling(bullet: str, cands: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    """Court filter: the bullet names its court («دیوان عالی کشور» /
    «دیوان عدالت اداری»); prefer matching-court candidates, keep the rest
    only when the preferred court yields nothing."""
    folded = _fold(bullet)
    court = "عدالت" if "عدالت" in folded else "عالی"
    preferred = [c for c in cands if court in _fold(c[2])]
    return preferred or cands
