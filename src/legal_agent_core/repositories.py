from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

from .canonical import (
    CanonicalEdge,
    Citation,
    DocumentVersion,
    ExplicitReference,
    LegalInstrument,
    Provision,
    ProvisionVersion,
    SourceDocument,
    SourceSpan,
)
from .research import MemoryEvent, ProgressiveRelation, TrustStatus


class CanonicalRepository(ABC):
    """Storage port for immutable/source-backed legal data."""

    @abstractmethod
    def add_source_document(self, item: SourceDocument) -> None: ...

    @abstractmethod
    def add_instrument(self, item: LegalInstrument) -> None: ...

    @abstractmethod
    def add_document_version(self, item: DocumentVersion) -> None: ...

    @abstractmethod
    def add_provision(self, item: Provision) -> None: ...

    @abstractmethod
    def add_provision_version(self, item: ProvisionVersion) -> None: ...

    @abstractmethod
    def add_source_span(self, item: SourceSpan) -> None: ...

    @abstractmethod
    def add_citation(self, item: Citation) -> None: ...

    @abstractmethod
    def add_edge(self, item: CanonicalEdge) -> None: ...

    @abstractmethod
    def add_explicit_reference(self, item: ExplicitReference) -> None: ...

    @abstractmethod
    def applicable_provision_versions(
        self, provision_id: str, at: date
    ) -> tuple[ProvisionVersion, ...]: ...

    @abstractmethod
    def get_instrument(self, instrument_id: str) -> LegalInstrument: ...

    @abstractmethod
    def get_source_document(self, source_document_id: str) -> SourceDocument: ...

    @abstractmethod
    def get_document_version(self, document_version_id: str) -> DocumentVersion: ...

    @abstractmethod
    def get_provision(self, provision_id: str) -> Provision: ...

    @abstractmethod
    def get_provision_version(self, provision_version_id: str) -> ProvisionVersion: ...

    @abstractmethod
    def get_source_span(self, source_span_id: str) -> SourceSpan: ...

    @abstractmethod
    def references_from_version(
        self, provision_version_id: str
    ) -> tuple[ExplicitReference, ...]: ...

    @abstractmethod
    def list_provisions(self) -> tuple[Provision, ...]: ...

    @abstractmethod
    def list_provision_versions(self) -> tuple[ProvisionVersion, ...]: ...

    @abstractmethod
    def source_spans_for_version(
        self, provision_version_id: str
    ) -> tuple[SourceSpan, ...]: ...


class ResearchGraphRepository(ABC):
    """Storage port for organization-scoped learned overlays and audit events."""

    @abstractmethod
    def get_relation(
        self, organization_id: str, relation_id: str
    ) -> ProgressiveRelation: ...

    @abstractmethod
    def save_relation(
        self, relation: ProgressiveRelation, event: MemoryEvent
    ) -> None: ...

    @abstractmethod
    def active_relations(
        self, organization_id: str
    ) -> tuple[ProgressiveRelation, ...]: ...

    @abstractmethod
    def list_relations(
        self,
        organization_id: str,
        statuses: frozenset[TrustStatus] | None = None,
    ) -> tuple[ProgressiveRelation, ...]: ...

    @abstractmethod
    def relations_using_version(
        self,
        organization_id: str,
        provision_version_id: str,
        status: TrustStatus | None = None,
    ) -> tuple[ProgressiveRelation, ...]: ...

    @abstractmethod
    def events(self, organization_id: str) -> tuple[MemoryEvent, ...]: ...
