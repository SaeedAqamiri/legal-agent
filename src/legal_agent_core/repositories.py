from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from typing import Any

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
from .notebook import NotebookEvent, NotebookPage, NotebookRoute
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

    def list_instrument_titles(self) -> tuple[str, ...]:
        """Known instrument titles for cross-law reference resolution.

        Optional capability — the base implementation returns an empty
        catalog (the reference extractor then behaves exactly as before).
        """
        return ()

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
    def list_provision_versions(self, ids: Any = None) -> tuple[ProvisionVersion, ...]: ...

    @abstractmethod
    def edges_touching(self, node_id: str) -> tuple[CanonicalEdge, ...]: ...

    def fts_search(self, query: str, limit: int = 512) -> dict[str, float] | None:
        """Optional full-text prefilter: provision_version_id -> rank.

        Returns ``None`` when the store has no full-text capability; callers
        fall back to the exhaustive scan. Ranks are advisory — tools still
        apply their own scoring, temporal gates and exact token checks.
        """
        return None

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


class NotebookRepository(ABC):
    """Storage port for the organization-scoped law notebook (docs 06 §6)."""

    @abstractmethod
    def save_events(self, events: tuple[NotebookEvent, ...]) -> None: ...

    @abstractmethod
    def upsert_route(
        self,
        organization_id: str,
        topic_id: str,
        to_node_id: str,
        *,
        hit: bool,
        confirmed: bool,
        source_episode_id: str | None = None,
    ) -> None: ...

    @abstractmethod
    def get_page(self, organization_id: str, node_id: str) -> NotebookPage | None: ...

    @abstractmethod
    def pages(self, organization_id: str, kind: str | None = None) -> tuple[NotebookPage, ...]: ...

    @abstractmethod
    def routes(
        self, organization_id: str, topic_id: str | None = None
    ) -> tuple[NotebookRoute, ...]: ...

    def episode_questions(
        self, organization_id: str, episode_ids: tuple[str, ...]
    ) -> dict[str, str]:
        """Source question per confirmed episode, when the store can provide it.

        Optional capability: in-memory/fake stores may omit it. Used to match a
        new question against the question that produced a learned route.
        """
        return {}

    @abstractmethod
    def upsert_page(self, page: NotebookPage) -> None: ...
