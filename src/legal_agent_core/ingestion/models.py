from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from ..canonical import (
    BoundingBox,
    CanonicalEdge,
    DocumentStatus,
    ExplicitReference,
    InstrumentType,
    ProvisionType,
)
from ..errors import DomainError

__all__ = [
    "DocumentVersionInput",
    "IngestionResult",
    "InstrumentInput",
    "PageSegment",
    "ParsedDocument",
    "ProvisionInput",
    "SourceInput",
]


@dataclass(frozen=True, slots=True)
class SourceInput:
    filename: str
    media_type: str
    checksum: str
    file_size: int
    ingested_at: datetime
    ingested_by: str
    source_uri: str
    organization_id: str | None = None
    source_document_id: str | None = None


@dataclass(frozen=True, slots=True)
class InstrumentInput:
    title: str
    canonical_title: str
    instrument_type: InstrumentType
    jurisdiction: str
    instrument_id: str | None = None
    issuer: str | None = None
    authority_level: str | None = None
    subject_domain: str | None = None
    language: str = "fa"
    external_identifier: str | None = None
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DocumentVersionInput:
    status: DocumentStatus
    document_version_id: str | None = None
    version_label: str | None = None
    version_number: int | None = None
    publication_date: date | None = None
    enacted_at: date | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    repealed_at: date | None = None


@dataclass(frozen=True, slots=True)
class PageSegment:
    """A page-sized slice of one provision's text.

    ``char_start``/``char_end`` are offsets into the provision's ``raw_text``
    so the segment is always an exact substring of it.
    """

    page_number: int
    text: str
    char_start: int
    char_end: int

    def __post_init__(self) -> None:
        if self.page_number < 1:
            raise DomainError("page segment page_number must be one-based")
        if not self.text.strip():
            raise DomainError("page segment text must not be blank")
        if not 0 <= self.char_start <= self.char_end:
            raise DomainError("page segment char range must be valid")


@dataclass(frozen=True, slots=True)
class ProvisionInput:
    provision_type: ProvisionType
    label: str
    text: str
    page_number: int
    number: str | None = None
    title: str | None = None
    normalized_text: str | None = None
    raw_text: str | None = None
    bbox: BoundingBox | None = None
    char_start: int | None = None
    char_end: int | None = None
    ordinal: int | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    status: DocumentStatus | None = None
    created_from: str = "parser:v1"
    children: tuple[ProvisionInput, ...] = ()
    #: Optional page-sized slices of ``raw_text``; when present the pipeline
    #: creates one SourceSpan per segment so citations can point at the exact
    #: page a passage lives on. Empty means a single span (legacy behaviour).
    page_segments: tuple[PageSegment, ...] = ()

    def __post_init__(self) -> None:
        if self.page_number < 1:
            raise DomainError("provision page_number must be one-based")
        if self.ordinal is not None and self.ordinal < 0:
            raise DomainError("provision ordinal cannot be negative")
        if not self.text.strip():
            raise DomainError("provision text must not be blank")


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    source: SourceInput
    instrument: InstrumentInput
    version: DocumentVersionInput
    provisions: tuple[ProvisionInput, ...]

    def __post_init__(self) -> None:
        if not self.provisions:
            raise DomainError("parsed document must contain at least one provision")


@dataclass(frozen=True, slots=True)
class IngestionResult:
    source_document_id: str
    instrument_id: str
    document_version_id: str
    provision_ids: tuple[str, ...]
    provision_version_ids: tuple[str, ...]
    source_span_ids: tuple[str, ...]
    explicit_references: tuple[ExplicitReference, ...]
    canonical_edges: tuple[CanonicalEdge, ...]

