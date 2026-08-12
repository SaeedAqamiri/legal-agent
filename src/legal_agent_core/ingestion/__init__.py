"""Idempotent canonical document ingestion."""

from .models import (
    DocumentVersionInput,
    IngestionResult,
    InstrumentInput,
    ParsedDocument,
    ProvisionInput,
    SourceInput,
)
from .normalization import normalize_legal_text, normalize_number
from .operations import (
    AmendmentLink,
    AmendmentRelationType,
    CorrectionAnnotation,
    CorrectionStatus,
    IngestionJob,
    IngestionJobStatus,
    IngestionWorker,
    OCRDocumentParser,
    OCRResult,
    SourceArtifact,
)
from .pipeline import CanonicalIngestionPipeline
from .references import DeterministicReferenceExtractor, ReferenceMention

__all__ = [
    "AmendmentLink",
    "AmendmentRelationType",
    "CanonicalIngestionPipeline",
    "CorrectionAnnotation",
    "CorrectionStatus",
    "DeterministicReferenceExtractor",
    "DocumentVersionInput",
    "IngestionJob",
    "IngestionJobStatus",
    "IngestionResult",
    "IngestionWorker",
    "InstrumentInput",
    "OCRDocumentParser",
    "OCRResult",
    "ParsedDocument",
    "ProvisionInput",
    "ReferenceMention",
    "SourceArtifact",
    "SourceInput",
    "normalize_legal_text",
    "normalize_number",
]
