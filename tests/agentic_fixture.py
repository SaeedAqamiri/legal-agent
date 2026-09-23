"""Shared canonical fixture for agentic package tests.

Builds a small two-instrument corpus with one explicit reference so tools,
loop and verifier can be exercised without any LLM or external service.
"""

from __future__ import annotations

from datetime import date

from legal_agent_core.canonical import (
    CreationMethod,
    DocumentStatus,
    DocumentVersion,
    ExplicitReference,
    InstrumentType,
    LegalInstrument,
    Provision,
    ProvisionType,
    ProvisionVersion,
    ResolutionStatus,
    SourceDocument,
    SourceSpan,
)
from legal_agent_core.in_memory import InMemoryCanonicalRepository

APPLICABLE = date(2025, 6, 1)
V1_TEXT = "مهلت درخواست تجدیدنظر اشخاص مقیم ایران بیست روز است."
V2_TEXT = "مهلت درخواست تجدیدنظر اشخاص مقیم ایران سی روز است."
NOTE_TEXT = "اجرای این ماده تابع مقررات فصل دوم همان قانون است."


def build_canonical() -> InMemoryCanonicalRepository:
    repository = InMemoryCanonicalRepository()
    repository.add_source_document(
        SourceDocument(
            "source-demo",
            None,
            "sample-appeal-law.pdf",
            "application/pdf",
            "sha256:demo-source",
            2048,
            "demo",
            "https://example.invalid/sample-appeal-law.pdf",
        )
    )
    repository.add_instrument(
        LegalInstrument(
            "instrument-demo",
            "قانون نمونه آیین دادرسی",
            "قانون نمونه آیین دادرسی",
            InstrumentType.STATUTE,
            "IR",
        )
    )
    repository.add_document_version(
        DocumentVersion(
            "document-demo-v1",
            "instrument-demo",
            "source-demo",
            DocumentStatus.EFFECTIVE,
            effective_from=date(2024, 1, 1),
        )
    )
    repository.add_document_version(
        DocumentVersion(
            "document-demo-v2",
            "instrument-demo",
            "source-demo",
            DocumentStatus.EFFECTIVE,
            effective_from=date(2025, 1, 1),
        )
    )
    repository.add_provision(
        Provision(
            "provision-demo-336",
            "instrument-demo",
            ProvisionType.ARTICLE,
            "336",
            "ماده ۳۳۶",
        )
    )
    repository.add_provision(
        Provision(
            "provision-demo-340",
            "instrument-demo",
            ProvisionType.ARTICLE,
            "340",
            "ماده ۳۴۰",
        )
    )
    repository.add_provision_version(
        ProvisionVersion(
            "provision-demo-336-v1",
            "provision-demo-336",
            "document-demo-v1",
            V1_TEXT,
            V1_TEXT,
            DocumentStatus.EFFECTIVE,
            "demo",
            date(2024, 1, 1),
            date(2024, 12, 31),
        )
    )
    repository.add_provision_version(
        ProvisionVersion(
            "provision-demo-336-v2",
            "provision-demo-336",
            "document-demo-v2",
            V2_TEXT,
            V2_TEXT,
            DocumentStatus.EFFECTIVE,
            "demo",
            date(2025, 1, 1),
            supersedes_version_id="provision-demo-336-v1",
        )
    )
    repository.add_provision_version(
        ProvisionVersion(
            "provision-demo-340-v2",
            "provision-demo-340",
            "document-demo-v2",
            NOTE_TEXT,
            NOTE_TEXT,
            DocumentStatus.EFFECTIVE,
            "demo",
            date(2025, 1, 1),
        )
    )
    repository.add_source_span(
        SourceSpan(
            "span-demo-336-v1",
            "source-demo",
            "document-demo-v1",
            "provision-demo-336-v1",
            42,
            V1_TEXT,
            char_start=0,
            char_end=len(V1_TEXT),
        )
    )
    repository.add_source_span(
        SourceSpan(
            "span-demo-336-v2",
            "source-demo",
            "document-demo-v2",
            "provision-demo-336-v2",
            17,
            V2_TEXT,
            char_start=0,
            char_end=len(V2_TEXT),
        )
    )
    repository.add_source_span(
        SourceSpan(
            "span-demo-340-v2",
            "source-demo",
            "document-demo-v2",
            "provision-demo-340-v2",
            19,
            NOTE_TEXT,
            char_start=0,
            char_end=len(NOTE_TEXT),
        )
    )
    return repository


def add_resolved_reference(repository: InMemoryCanonicalRepository) -> None:
    """«فصل دوم همان قانون» از ماده ۳۴۰ به ماده ۳۳۶ resolve شده است."""
    repository.add_explicit_reference(
        ExplicitReference(
            "reference-demo-1",
            "provision-demo-340-v2",
            "فصل دوم همان قانون",
            "span-demo-340-v2",
            CreationMethod.DETERMINISTIC_EXTRACTOR,
            ResolutionStatus.RESOLVED,
            "provision-demo-336",
            1.0,
        )
    )
