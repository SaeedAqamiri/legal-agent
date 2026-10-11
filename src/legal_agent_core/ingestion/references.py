from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from ..canonical import ResolutionStatus
from .normalization import normalize_legal_text, normalize_number

_ARTICLE_REFERENCE = re.compile(r"ماده[\s\u200c]*[\(]?\s*(?P<number>[0-9]+)(?:\s*[\)])?")
_LOCAL_SCOPE = re.compile(r"^\s+این\s+(?:قانون|آیین[‌-]?نامه|دستورالعمل|بخشنامه|مصوبه)")


@dataclass(frozen=True, slots=True)
class ReferenceMention:
    start: int
    end: int
    target_text: str
    number: str
    resolution_status: ResolutionStatus
    resolved_target_provision_id: str | None
    confidence: float


class DeterministicReferenceExtractor:
    """Conservative Persian article-reference extraction; never guesses a target instrument."""

    def extract(
        self,
        text: str,
        *,
        article_targets: Mapping[str, Sequence[str]],
        instrument_aliases: Sequence[str],
        source_provision_id: str,
        known_titles: Sequence[str] = (),
    ) -> tuple[ReferenceMention, ...]:
        normalized = normalize_legal_text(text)
        aliases = tuple(
            sorted(
                {normalize_legal_text(alias) for alias in instrument_aliases if normalize_legal_text(alias)},
                key=len,
                reverse=True,
            )
        )
        titles = tuple(
            sorted(
                {normalize_legal_text(title) for title in known_titles if normalize_legal_text(title)},
                key=len,
                reverse=True,
            )
        )
        mentions: list[ReferenceMention] = []
        for match in _ARTICLE_REFERENCE.finditer(normalized):
            number = normalize_number(match.group("number")) or match.group("number")
            suffix = normalized[match.end() : match.end() + 120]
            scope_end = self._scope_end(suffix, aliases)
            title_end = self._known_title_end(suffix, titles) if titles else 0
            # A known *other* law name extends the captured mention (so the
            # target law survives in target_text) but never enables
            # provision-level resolution — only a self-scope does.
            explicit_scope_end = max(scope_end, title_end)
            candidates = [item for item in article_targets.get(number, ()) if item != source_provision_id]
            is_explicit_scope = scope_end > 0
            resolved = candidates[0] if is_explicit_scope and len(candidates) == 1 else None
            if resolved:
                status = ResolutionStatus.RESOLVED
                confidence = 1.0
            elif is_explicit_scope and len(candidates) > 1:
                status = ResolutionStatus.AMBIGUOUS
                confidence = 0.95
            else:
                status = ResolutionStatus.UNRESOLVED
                confidence = 0.85
            end = match.end() + explicit_scope_end
            head = " ".join(
                normalized[match.start() : match.end()]
                .replace("(", " ")
                .replace(")", " ")
                .split()
            )
            tail = normalized[match.end() : end].strip()
            target_text = f"{head} {tail}".strip() if tail else head
            mentions.append(
                ReferenceMention(
                    start=match.start(),
                    end=end,
                    target_text=target_text,
                    number=number,
                    resolution_status=status,
                    resolved_target_provision_id=resolved,
                    confidence=confidence,
                )
            )
        return tuple(mentions)

    @staticmethod
    def _scope_end(suffix: str, aliases: Sequence[str]) -> int:
        local = _LOCAL_SCOPE.match(suffix)
        if local is not None:
            return local.end()
        stripped = suffix.lstrip()
        leading_space_count = len(suffix) - len(stripped)
        for alias in aliases:
            if stripped.startswith(alias):
                return leading_space_count + len(alias)
        return 0

    @staticmethod
    def _known_title_end(suffix: str, titles: Sequence[str]) -> int:
        """Longest known-instrument title that the suffix starts with — captures
        «ماده N قانون کار …» mentions whose target is another instrument."""
        stripped = suffix.lstrip()
        leading_space_count = len(suffix) - len(stripped)
        for title in titles:  # longest first
            if stripped.startswith(title):
                return leading_space_count + len(title)
        return 0

