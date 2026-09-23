"""Deterministic Persian question → structured search filters (NLQ mode).

The model never pages through results to build a filter: this parser emits an
executable filter object that code validates and runs. It is intentionally
rule-based (regex over Persian/digit forms) so the contract is testable; an
LLM-backed parser can be swapped in later behind the same output contract.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date

from ..errors import DomainError
from ..jalali import parse_jalali_date

_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")

_PROVISION_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bتبصره\s*([0-9]+)"), "note"),
    (re.compile(r"\bماده\s*([0-9]+)"), "article"),
    (re.compile(r"\bبند\s*([0-9]+)"), "clause"),
    (re.compile(r"\bفصل\s*([0-9]+)"), "chapter"),
    (re.compile(r"\bبخش\s*([0-9]+)"), "section"),
)

_ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_JALALI_DATE = re.compile(r"\b(\d{3,4})[/‌.](\d{1,2})[/‌.](\d{1,2})\b")
_RELATIVE_YEARS = re.compile(r"\bسال\s+(\d{3,4})\b")

_KNOWN_KEYWORDS: tuple[str, ...] = (
    "مهلت",
    "اعتراض",
    "تجدیدنظر",
    "بدوی",
    "مستمری",
    "بیمه",
    "سابقه",
    "بازمانده",
    "ازکارافتادگی",
    "بیکاری",
    "مقاطعه‌کار",
    "راننده",
    "مفاصاحساب",
    "خسارت",
    "معوقات",
)


@dataclass(frozen=True, slots=True)
class SearchFilters:
    """Executable, code-validated search filter object (NLQ output contract)."""

    query: str
    provision_type: str | None = None
    provision_number: str | None = None
    applicable_time: str | None = None
    document_scope: tuple[str, ...] = field(default_factory=tuple)
    keywords: tuple[str, ...] = field(default_factory=tuple)

    def to_payload(self) -> dict:
        return {
            "query": self.query,
            "provision_type": self.provision_type,
            "provision_number": self.provision_number,
            "applicable_time": self.applicable_time,
            "document_scope": list(self.document_scope),
            "keywords": list(self.keywords),
        }


def parse_search_filters(
    question: str,
    known_documents: Mapping[str, str] | None = None,
    default_applicable_time: date | None = None,
) -> SearchFilters:
    """Extract filters from a Persian question.

    ``known_documents`` maps known instrument titles/aliases to document or
    instrument ids used as ``document_scope``.
    """
    if not question or not question.strip():
        raise DomainError("question must not be blank to parse search filters")
    text = question.translate(_DIGITS)

    provision_type: str | None = None
    provision_number: str | None = None
    for pattern, kind in _PROVISION_RULES:
        match = pattern.search(text)
        if match:
            provision_type = kind
            provision_number = match.group(1)
            break

    applicable_time: str | None = None
    iso = _ISO_DATE.search(text)
    jalali = _JALALI_DATE.search(text)
    if iso:
        try:
            applicable_time = date(
                int(iso.group(1)), int(iso.group(2)), int(iso.group(3))
            ).isoformat()
        except ValueError as exc:
            raise DomainError(f"invalid gregorian date in question: {iso.group(0)}") from exc
    elif jalali:
        try:
            applicable_time = parse_jalali_date(jalali.group(0)).isoformat()
        except DomainError:
            applicable_time = None
    if applicable_time is None and default_applicable_time is not None:
        applicable_time = default_applicable_time.isoformat()

    scope: list[str] = []
    if known_documents:
        lowered = text.casefold()
        for title, identifier in known_documents.items():
            if title.strip() and title.casefold() in lowered and identifier not in scope:
                scope.append(identifier)

    keywords = tuple(
        keyword for keyword in _KNOWN_KEYWORDS if keyword in text
    )

    return SearchFilters(
        query=question.strip(),
        provision_type=provision_type,
        provision_number=provision_number,
        applicable_time=applicable_time,
        document_scope=tuple(scope),
        keywords=keywords,
    )
