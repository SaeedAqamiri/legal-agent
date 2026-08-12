from __future__ import annotations

import re
import unicodedata

_DIGIT_TRANSLATION = str.maketrans(
    "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
    "01234567890123456789",
)
_ARABIC_TO_PERSIAN = str.maketrans({"ي": "ی", "ى": "ی", "ك": "ک"})
_WHITESPACE = re.compile(r"\s+")


def normalize_legal_text(value: str) -> str:
    """Normalize for identity/search while preserving source text separately."""
    normalized = unicodedata.normalize("NFKC", value)
    normalized = normalized.translate(_ARABIC_TO_PERSIAN).translate(_DIGIT_TRANSLATION)
    return _WHITESPACE.sub(" ", normalized).strip()


def normalize_number(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = normalize_legal_text(value)
    return normalized or None

