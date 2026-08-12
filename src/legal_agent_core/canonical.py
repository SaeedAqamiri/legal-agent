from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import StrEnum

from .errors import DomainError


def utc_now() -> datetime:
    return datetime.now(UTC)


def require_text(value: str, field_name: str) -> None:
    if not value or not value.strip():
        raise DomainError(f"{field_name} must not be blank")


def validate_confidence(value: float | None) -> None:
    if value is not None and not 0 <= value <= 1:
        raise DomainError("confidence must be between 0 and 1")


def validate_period(effective_from: date | None, effective_to: date | None) -> None:
    if effective_from and effective_to and effective_to < effective_from:
        raise DomainError("effective_to cannot be before effective_from")


class InstrumentType(StrEnum):
    CONSTITUTION = "constitution"
    STATUTE = "statute"
    REGULATION = "regulation"
    BYLAW = "bylaw"
    DIRECTIVE = "directive"
    CIRCULAR = "circular"
    RESOLUTION = "resolution"
    JUDGMENT = "judgment"
    CONTRACT = "contract"
    POLICY = "policy"
    PROCEDURE = "procedure"
    GUIDELINE = "guideline"
    AMENDMENT = "amendment"
    APPENDIX = "appendix"
    OTHER = "other"


class DocumentStatus(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    EFFECTIVE = "effective"
    SUSPENDED = "suspended"
    EXPIRED = "expired"
    REPEALED = "repealed"
    SUPERSEDED = "superseded"
    UNKNOWN = "unknown"


class ProvisionType(StrEnum):
    BOOK = "book"
    PART = "part"
    CHAPTER = "chapter"
    SECTION = "section"
    ARTICLE = "article"
    CLAUSE = "clause"
    PARAGRAPH = "paragraph"
    SUBPARAGRAPH = "subparagraph"
    NOTE = "note"
    EXCEPTION = "exception"
    APPENDIX = "appendix"
    SCHEDULE = "schedule"
    DEFINITION = "definition"


class CreationMethod(StrEnum):
    PARSER = "parser"
    OCR = "ocr"
    DETERMINISTIC_EXTRACTOR = "deterministic_extractor"
    LLM_EXTRACTOR = "llm_extractor"
    HUMAN = "human"
    IMPORT = "import"


class ResolutionStatus(StrEnum):
    UNRESOLVED = "unresolved"
    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    INVALID = "invalid"


class CanonicalNodeType(StrEnum):
    JURISDICTION = "jurisdiction"
    LEGAL_DOMAIN = "legal_domain"
    LEGAL_INSTRUMENT = "legal_instrument"
    DOCUMENT_VERSION = "document_version"
    PROVISION = "provision"
    PROVISION_VERSION = "provision_version"
    AUTHORITY = "authority"
    ORGANIZATION = "organization"
    SOURCE_DOCUMENT = "source_document"


class CanonicalEdgeType(StrEnum):
    CONTAINS = "contains"
    HAS_VERSION = "has_version"
    VERSION_OF = "version_of"
    ISSUED_BY = "issued_by"
    BELONGS_TO_DOMAIN = "belongs_to_domain"
    BELONGS_TO_JURISDICTION = "belongs_to_jurisdiction"
    EXPLICITLY_REFERENCES = "explicitly_references"
    CITES = "cites"
    AMENDS = "amends"
    REPEALS = "repeals"
    SUPERSEDES = "supersedes"
    REPLACES = "replaces"
    SUSPENDS = "suspends"
    RESTORES = "restores"
    IMPLEMENTS = "implements"


@dataclass(frozen=True, slots=True)
class Provenance:
    created_by: str
    creation_method: CreationMethod
    source_id: str
    created_at: datetime = field(default_factory=utc_now)
    parser_version: str | None = None
    model_id: str | None = None

    def __post_init__(self) -> None:
        require_text(self.created_by, "created_by")
        require_text(self.source_id, "source_id")


@dataclass(frozen=True, slots=True)
class BoundingBox:
    x1: float
    y1: float
    x2: float
    y2: float

    def __post_init__(self) -> None:
        if not (0 <= self.x1 < self.x2 <= 1 and 0 <= self.y1 < self.y2 <= 1):
            raise DomainError("bbox must be normalized to 0..1 with positive area")


@dataclass(frozen=True, slots=True)
class SourceDocument:
    source_document_id: str
    organization_id: str | None
    filename: str
    media_type: str
    checksum: str
    file_size: int
    ingested_by: str
    source_uri: str
    ingested_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        for field_name in ("source_document_id", "filename", "media_type", "checksum"):
            require_text(getattr(self, field_name), field_name)
        if self.file_size < 0:
            raise DomainError("file_size cannot be negative")


@dataclass(frozen=True, slots=True)
class LegalInstrument:
    instrument_id: str
    title: str
    canonical_title: str
    instrument_type: InstrumentType
    jurisdiction: str
    issuer: str | None = None
    authority_level: str | None = None
    subject_domain: str | None = None
    language: str = "fa"
    external_identifier: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("instrument_id", "title", "canonical_title", "jurisdiction"):
            require_text(getattr(self, field_name), field_name)


@dataclass(frozen=True, slots=True)
class DocumentVersion:
    document_version_id: str
    instrument_id: str
    source_document_id: str
    status: DocumentStatus
    version_label: str | None = None
    version_number: int | None = None
    publication_date: date | None = None
    enacted_at: date | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    repealed_at: date | None = None

    def __post_init__(self) -> None:
        validate_period(self.effective_from, self.effective_to)
        if self.version_number is not None and self.version_number < 1:
            raise DomainError("version_number must be positive")

    def applies_at(self, applicable_time: date) -> bool:
        return (
            (self.effective_from is None or self.effective_from <= applicable_time)
            and (self.effective_to is None or applicable_time <= self.effective_to)
        )


@dataclass(frozen=True, slots=True)
class Provision:
    provision_id: str
    instrument_id: str
    provision_type: ProvisionType
    number: str | None
    label: str
    title: str | None = None
    parent_provision_id: str | None = None
    ordinal: int = 0
    depth: int = 0

    def __post_init__(self) -> None:
        require_text(self.provision_id, "provision_id")
        require_text(self.instrument_id, "instrument_id")
        require_text(self.label, "label")
        if self.ordinal < 0 or self.depth < 0:
            raise DomainError("ordinal and depth cannot be negative")
        if self.parent_provision_id == self.provision_id:
            raise DomainError("a provision cannot contain itself")


@dataclass(frozen=True, slots=True)
class ProvisionVersion:
    provision_version_id: str
    provision_id: str
    document_version_id: str
    text: str
    normalized_text: str
    status: DocumentStatus
    created_from: str
    effective_from: date | None = None
    effective_to: date | None = None
    supersedes_version_id: str | None = None

    def __post_init__(self) -> None:
        require_text(self.provision_version_id, "provision_version_id")
        require_text(self.provision_id, "provision_id")
        require_text(self.text, "text")
        require_text(self.normalized_text, "normalized_text")
        validate_period(self.effective_from, self.effective_to)
        if self.supersedes_version_id == self.provision_version_id:
            raise DomainError("a provision version cannot supersede itself")

    def applies_at(self, applicable_time: date) -> bool:
        return (
            (self.effective_from is None or self.effective_from <= applicable_time)
            and (self.effective_to is None or applicable_time <= self.effective_to)
        )


@dataclass(frozen=True, slots=True)
class SourceSpan:
    source_span_id: str
    source_document_id: str
    document_version_id: str
    provision_version_id: str
    page_number: int
    raw_text: str
    bbox: BoundingBox | None = None
    char_start: int | None = None
    char_end: int | None = None

    def __post_init__(self) -> None:
        if self.page_number < 1:
            raise DomainError("page_number must be one-based")
        require_text(self.raw_text, "raw_text")
        if (self.char_start is None) != (self.char_end is None):
            raise DomainError("char_start and char_end must be supplied together")
        if self.char_start is not None and not 0 <= self.char_start <= self.char_end:
            raise DomainError("invalid source character range")


@dataclass(frozen=True, slots=True)
class ExplicitReference:
    reference_id: str
    source_provision_version_id: str
    target_text: str
    source_span_id: str
    extraction_method: CreationMethod
    resolution_status: ResolutionStatus = ResolutionStatus.UNRESOLVED
    resolved_target_provision_id: str | None = None
    confidence: float | None = None

    def __post_init__(self) -> None:
        require_text(self.target_text, "target_text")
        require_text(self.source_span_id, "source_span_id")
        validate_confidence(self.confidence)
        if self.resolution_status == ResolutionStatus.RESOLVED and not self.resolved_target_provision_id:
            raise DomainError("resolved references require a canonical target")
        if self.resolution_status != ResolutionStatus.RESOLVED and self.resolved_target_provision_id:
            raise DomainError("only resolved references may have a canonical target")


@dataclass(frozen=True, slots=True)
class CanonicalEdge:
    edge_id: str
    source_node_id: str
    target_node_id: str
    edge_type: CanonicalEdgeType
    provenance: Provenance
    source_span_id: str | None = None
    confidence: float | None = None

    def __post_init__(self) -> None:
        require_text(self.edge_id, "edge_id")
        require_text(self.source_node_id, "source_node_id")
        require_text(self.target_node_id, "target_node_id")
        if self.source_node_id == self.target_node_id:
            raise DomainError("a canonical edge cannot point to itself")
        validate_confidence(self.confidence)
        if self.edge_type == CanonicalEdgeType.EXPLICITLY_REFERENCES and not self.source_span_id:
            raise DomainError("an explicit reference edge requires an exact source span")


@dataclass(frozen=True, slots=True)
class Citation:
    citation_id: str
    instrument_id: str
    document_version_id: str
    provision_id: str
    provision_version_id: str
    source_span_id: str
    page: int
    quoted_text: str

    def __post_init__(self) -> None:
        for field_name in (
            "citation_id",
            "instrument_id",
            "document_version_id",
            "provision_id",
            "provision_version_id",
            "source_span_id",
            "quoted_text",
        ):
            require_text(getattr(self, field_name), field_name)
        if self.page < 1:
            raise DomainError("citation page must be one-based")
