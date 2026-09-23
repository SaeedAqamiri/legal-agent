"""Markdown-to-canonical parser for OCR-free ingestion of text-layer documents.

The benchmark corpus ships ``Falkor``-decoupled ``.md`` files produced by
``pdftotext -layout``. They are not pure ``ماده`` headings: they mix a title
heading, ``## صفحه N`` page markers, ``*[صفحه بدون متن]*`` placeholders and
multi-column text. This parser extracts a deterministic set of ``ProvisionInput``
entries (page-anchored, ordered) plus document metadata, without any OCR.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ..canonical import DocumentStatus, InstrumentType
from ..errors import DomainError
from .models import (
    DocumentVersionInput,
    InstrumentInput,
    PageSegment,
    ParsedDocument,
    ProvisionInput,
    SourceInput,
)
from .operations import IngestionJob, SourceArtifact

_PAGE_MARKER = re.compile(r"^\s*#{1,3}\s*صفحه\s*([0-9۰-۹]+)")
_PLACEHOLDER = re.compile(r"^\s*\*\[[^\]]*\]\*")
# Ordinal words used by Persian legal drafting («ماده اول», «تبصره پنجم», ...).
_ORDINAL_WORDS = (
    r"(?:اول|دوم|سوم|چهارم|پنجم|ششم|هفتم|هشتم|نهم|دهم|یازدهم|دوازدهم"
    r"|سیزدهم|چهاردهم|پانزدهم|شانزدهم|هفدهم|هجدهم|نوزدهم|بیستم)"
)
# Structural legal-item markers that begin a provision. Besides numeric
# labels («ماده ۴») this recognises the single-article formula
# («ماده واحده») and ordinal-word labels («ماده اول»), plus Persian
# list delimiters such as «۱ ـ» (tatweel) and en/em dashes.
_STRUCTURAL = re.compile(
    r"^(?:\s|\u200c)*((?:بخش|ماده|تبصره|بند| آیین\u200cنامه)\s*"
    r"(?:[0-9۰-۹]+[\w۰-۹\-.]*|واحده?|" + _ORDINAL_WORDS + r"))"
    r"|^(?:\s|\u200c)*([0-9۰-۹]+)\s*[\)\-\.ـ–—]\s*"
    r"|^(?:\s|\u200c)*-\s*([0-9۰-۹]+)\s*",
    re.UNICODE,
)
_HEADING = re.compile(r"^\s*#\s+(.*)")


def _fa_digits_to_int(value: str) -> int:
    translation = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")
    return int(value.translate(translation))


@dataclass(frozen=True, slots=True)
class _AnnotatedLine:
    page: int
    text: str


@dataclass(slots=True)
class _ExtractedProvision:
    label: str
    text: list[str]
    page: int
    pages: list[int] = field(default_factory=list)


def parse_pages(content: str) -> Sequence[_AnnotatedLine]:
    lines: list[_AnnotatedLine] = []
    current_page = 0
    for raw in content.splitlines():
        match = _PAGE_MARKER.match(raw)
        if match:
            current_page = _fa_digits_to_int(match.group(1))
            continue
        if _PLACEHOLDER.match(raw):
            continue
        stripped = raw.strip()
        if not stripped:
            continue
        lines.append(_AnnotatedLine(max(current_page, 1), stripped))
    return lines


def extract_provisions(lines: Sequence[_AnnotatedLine]) -> tuple[_ExtractedProvision, ...]:
    provisions: list[_ExtractedProvision] = []
    current: _ExtractedProvision | None = None
    for line in lines:
        match = _STRUCTURAL.match(line.text)
        if match:
            if current is not None and current.text:
                provisions.append(current)
            label = next(
                (group for group in match.groups() if group and group.strip()), ""
            )
            remainder = line.text[match.end():].strip()
            current = _ExtractedProvision(
                label=(label or f"بند {len(provisions) + 1}"),
                text=[remainder] if remainder else [],
                page=line.page,
                pages=[line.page] if remainder else [],
            )
        elif current is not None:
            current.text.append(line.text)
            current.pages.append(line.page)
    if current is not None and current.text:
        provisions.append(current)
    return tuple(provisions)


def page_segments(pages: Sequence[int], raw_text: str) -> tuple[PageSegment, ...]:
    """Slice ``raw_text`` into page-sized segments at page transitions.

    ``pages`` holds the page of each line; the segments' char offsets are
    exact positions inside ``raw_text`` so every segment is a substring of it.
    """
    lines = raw_text.split("\n")
    if not lines:
        return ()
    segments: list[PageSegment] = []
    current_page = pages[0] if pages else 1
    current_start = 0
    current_lines: list[str] = []
    cursor = 0
    for line, page in zip(lines, pages):
        if page != current_page and current_lines:
            text = "\n".join(current_lines)
            segments.append(
                PageSegment(current_page, text, current_start, current_start + len(text))
            )
            current_start = cursor
            current_page = page
            current_lines = []
        current_lines.append(line)
        cursor += len(line) + 1
    if current_lines:
        text = "\n".join(current_lines)
        segments.append(
            PageSegment(current_page, text, current_start, current_start + len(text))
        )
    return tuple(segments)


def _first_heading(content: str) -> str | None:
    for raw in content.splitlines():
        match = _HEADING.match(raw)
        if match:
            return match.group(1).strip()
    return None


def markdown_title(heading: str | None, filename: str) -> str:
    if heading:
        cleaned = re.sub(r"^\s*D\d+\s*[—\-–:]\s*", "", heading).strip()
        if cleaned:
            return cleaned
    return re.sub(r"(?i)\.(md|markdown)$", "", filename).strip()


def _digitise(label: str) -> str | None:
    match = re.search(r"([0-9۰-۹]+)", label)
    if not match:
        return None
    return str(_fa_digits_to_int(match.group(1)))


def parse_markdown_document(
    content: str,
    *,
    filename: str,
    ingested_by: str = "markdown-loader",
) -> ParsedDocument:
    lines = parse_pages(content)
    extracted = extract_provisions(lines)
    if not extracted:
        raise DomainError(f"no provisions could be extracted from {filename!r}")

    checksum = hashlib.sha256(content.encode("utf-8")).hexdigest()
    title = markdown_title(_first_heading(content), filename)
    provisions = []
    for index, item in enumerate(extracted):
        raw_text = "\n".join(item.text).strip()[:12_000]
        segments = page_segments(item.pages[: len(raw_text.split("\n"))], raw_text)
        provisions.append(
            ProvisionInput(
                provision_type=_provision_type(item.label),
                label=item.label,
                text="\n".join(item.text[:120]).strip()[:12_000],
                normalized_text="\n".join(item.text[:60]).strip()[:6_000],
                raw_text=raw_text,
                page_number=item.page,
                page_segments=segments,
                ordinal=index,
                status=DocumentStatus.EFFECTIVE,
                created_from="markdown:v2",
            )
        )
    provisions = tuple(provisions)
    source = SourceInput(
        filename=filename,
        media_type="text/markdown",
        checksum=f"sha256:{checksum}",
        file_size=len(content.encode("utf-8")),
        ingested_at=datetime.now(UTC),
        ingested_by=ingested_by,
        source_uri=f"samples/{filename}",
    )
    instrument = InstrumentInput(
        title=title,
        canonical_title=title,
        instrument_type=InstrumentType.REGULATION,
        jurisdiction="IR",
        issuer=_issuer(title),
        language="fa",
    )
    version = DocumentVersionInput(
        status=DocumentStatus.EFFECTIVE,
        version_label="v1",
        version_number=1,
    )
    return ParsedDocument(
        source=source,
        instrument=instrument,
        version=version,
        provisions=provisions,
    )


def _provision_type(label: str) -> object:
    from ..canonical import ProvisionType

    lowered = label.strip()
    if lowered.startswith("تبصره"):
        return ProvisionType.NOTE
    if lowered.startswith("بخش"):
        return ProvisionType.PART
    if lowered.startswith("بند"):
        return ProvisionType.CLAUSE
    return ProvisionType.ARTICLE


def _issuer(title: str) -> str | None:
    if "تأمین اجتماعی" in title or "سازمان" in title:
        return "سازمان تأمین اجتماعی"
    return None


class MarkdownDocumentParser:
    """Implements the ``DocumentParser`` port against markdown text artifacts."""

    name = "markdown"
    version = "1"

    def parse(self, job: IngestionJob, artifact: SourceArtifact) -> ParsedDocument:
        try:
            content = artifact.content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DomainError("markdown artifact must be UTF-8 text") from exc
        return parse_markdown_document(
            content, filename=job.source_uri.rsplit("/", 1)[-1] or artifact.source_uri.rsplit("/", 1)[-1]
        )

    def parse_bytes(self, content: bytes, filename: str) -> ParsedDocument:
        return parse_markdown_document(content.decode("utf-8"), filename=filename)

    @staticmethod
    def from_text(content: str, filename: str) -> ParsedDocument:
        return parse_markdown_document(content, filename=filename)
