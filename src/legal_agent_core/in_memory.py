from __future__ import annotations

from collections import defaultdict
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
from .errors import ConflictError, NotFoundError
from .repositories import CanonicalRepository, ResearchGraphRepository
from .research import MemoryEvent, ProgressiveRelation, TrustStatus


class InMemoryCanonicalRepository(CanonicalRepository):
    """Strict reference implementation; deliberately exposes no delete/overwrite API."""

    def __init__(self) -> None:
        self.source_documents: dict[str, SourceDocument] = {}
        self.instruments: dict[str, LegalInstrument] = {}
        self.document_versions: dict[str, DocumentVersion] = {}
        self.provisions: dict[str, Provision] = {}
        self.provision_versions: dict[str, ProvisionVersion] = {}
        self.source_spans: dict[str, SourceSpan] = {}
        self.citations: dict[str, Citation] = {}
        self.edges: dict[str, CanonicalEdge] = {}
        self.explicit_references: dict[str, ExplicitReference] = {}

    @staticmethod
    def _insert(store: dict[str, object], key: str, item: object) -> None:
        existing = store.get(key)
        if existing is not None and existing != item:
            raise ConflictError(f"canonical identity {key!r} already exists")
        store[key] = item

    def add_source_document(self, item: SourceDocument) -> None:
        self._insert(self.source_documents, item.source_document_id, item)

    def add_instrument(self, item: LegalInstrument) -> None:
        self._insert(self.instruments, item.instrument_id, item)

    def add_document_version(self, item: DocumentVersion) -> None:
        if item.instrument_id not in self.instruments:
            raise NotFoundError(f"instrument {item.instrument_id!r} not found")
        if item.source_document_id not in self.source_documents:
            raise NotFoundError(
                f"source document {item.source_document_id!r} not found"
            )
        self._insert(self.document_versions, item.document_version_id, item)

    def add_provision(self, item: Provision) -> None:
        if item.instrument_id not in self.instruments:
            raise NotFoundError(f"instrument {item.instrument_id!r} not found")
        if item.parent_provision_id:
            parent = self.provisions.get(item.parent_provision_id)
            if parent is None:
                raise NotFoundError(
                    f"parent provision {item.parent_provision_id!r} not found"
                )
            if parent.instrument_id != item.instrument_id:
                raise ConflictError(
                    "parent and child provisions must belong to the same instrument"
                )
            if item.depth != parent.depth + 1:
                raise ConflictError("child provision depth must be parent depth + 1")
        self._insert(self.provisions, item.provision_id, item)

    def add_provision_version(self, item: ProvisionVersion) -> None:
        provision = self.provisions.get(item.provision_id)
        if provision is None:
            raise NotFoundError(f"provision {item.provision_id!r} not found")
        document_version = self.document_versions.get(item.document_version_id)
        if document_version is None:
            raise NotFoundError(
                f"document version {item.document_version_id!r} not found"
            )
        if document_version.instrument_id != provision.instrument_id:
            raise ConflictError(
                "provision and document version must belong to the same instrument"
            )
        if item.supersedes_version_id:
            previous = self.provision_versions.get(item.supersedes_version_id)
            if previous is None:
                raise NotFoundError(
                    f"superseded version {item.supersedes_version_id!r} not found"
                )
            if previous.provision_id != item.provision_id:
                raise ConflictError(
                    "a version may only supersede a version of the same provision"
                )
        self._insert(self.provision_versions, item.provision_version_id, item)

    def add_source_span(self, item: SourceSpan) -> None:
        version = self.provision_versions.get(item.provision_version_id)
        if version is None:
            raise NotFoundError(
                f"provision version {item.provision_version_id!r} not found"
            )
        if version.document_version_id != item.document_version_id:
            raise ConflictError(
                "source span points to inconsistent document/provision versions"
            )
        document_version = self.document_versions[item.document_version_id]
        if document_version.source_document_id != item.source_document_id:
            raise ConflictError("source span points to an inconsistent source document")
        self._insert(self.source_spans, item.source_span_id, item)

    def add_citation(self, item: Citation) -> None:
        version = self.provision_versions.get(item.provision_version_id)
        span = self.source_spans.get(item.source_span_id)
        if version is None or span is None:
            raise NotFoundError(
                "citation must resolve to a provision version and source span"
            )
        provision = self.provisions[version.provision_id]
        if (
            item.provision_id != provision.provision_id
            or item.instrument_id != provision.instrument_id
            or item.document_version_id != version.document_version_id
            or span.provision_version_id != version.provision_version_id
            or item.page != span.page_number
        ):
            raise ConflictError(
                "citation identity is inconsistent with its canonical source"
            )
        self._insert(self.citations, item.citation_id, item)

    def add_edge(self, item: CanonicalEdge) -> None:
        known_nodes = (
            self.source_documents
            | self.instruments
            | self.document_versions
            | self.provisions
            | self.provision_versions
        )
        if item.source_node_id not in known_nodes:
            raise NotFoundError(
                f"canonical source node {item.source_node_id!r} not found"
            )
        if item.target_node_id not in known_nodes:
            raise NotFoundError(
                f"canonical target node {item.target_node_id!r} not found"
            )
        if item.source_span_id and item.source_span_id not in self.source_spans:
            raise NotFoundError(f"source span {item.source_span_id!r} not found")
        self._insert(self.edges, item.edge_id, item)

    def add_explicit_reference(self, item: ExplicitReference) -> None:
        version = self.provision_versions.get(item.source_provision_version_id)
        span = self.source_spans.get(item.source_span_id)
        if version is None or span is None:
            raise NotFoundError(
                "explicit reference must resolve to a provision version and source span"
            )
        if span.provision_version_id != version.provision_version_id:
            raise ConflictError(
                "explicit reference source span belongs to another provision version"
            )
        if (
            item.resolved_target_provision_id
            and item.resolved_target_provision_id not in self.provisions
        ):
            raise NotFoundError(
                f"target provision {item.resolved_target_provision_id!r} not found"
            )
        self._insert(self.explicit_references, item.reference_id, item)

    def applicable_provision_versions(
        self, provision_id: str, at: date
    ) -> tuple[ProvisionVersion, ...]:
        if provision_id not in self.provisions:
            raise NotFoundError(f"provision {provision_id!r} not found")
        result = [
            version
            for version in self.provision_versions.values()
            if version.provision_id == provision_id and version.applies_at(at)
        ]
        return tuple(
            sorted(
                result,
                key=lambda item: (
                    item.effective_from or date.min,
                    item.provision_version_id,
                ),
            )
        )

    def get_instrument(self, instrument_id: str) -> LegalInstrument:
        try:
            return self.instruments[instrument_id]
        except KeyError as exc:
            raise NotFoundError(f"instrument {instrument_id!r} not found") from exc

    def get_source_document(self, source_document_id: str) -> SourceDocument:
        try:
            return self.source_documents[source_document_id]
        except KeyError as exc:
            raise NotFoundError(
                f"source document {source_document_id!r} not found"
            ) from exc

    def get_document_version(self, document_version_id: str) -> DocumentVersion:
        try:
            return self.document_versions[document_version_id]
        except KeyError as exc:
            raise NotFoundError(
                f"document version {document_version_id!r} not found"
            ) from exc

    def get_provision(self, provision_id: str) -> Provision:
        try:
            return self.provisions[provision_id]
        except KeyError as exc:
            raise NotFoundError(f"provision {provision_id!r} not found") from exc

    def get_provision_version(self, provision_version_id: str) -> ProvisionVersion:
        try:
            return self.provision_versions[provision_version_id]
        except KeyError as exc:
            raise NotFoundError(
                f"provision version {provision_version_id!r} not found"
            ) from exc

    def get_source_span(self, source_span_id: str) -> SourceSpan:
        try:
            return self.source_spans[source_span_id]
        except KeyError as exc:
            raise NotFoundError(f"source span {source_span_id!r} not found") from exc

    def references_from_version(
        self, provision_version_id: str
    ) -> tuple[ExplicitReference, ...]:
        return tuple(
            sorted(
                (
                    reference
                    for reference in self.explicit_references.values()
                    if reference.source_provision_version_id == provision_version_id
                ),
                key=lambda item: item.reference_id,
            )
        )

    def list_provisions(self) -> tuple[Provision, ...]:
        return tuple(
            sorted(self.provisions.values(), key=lambda item: item.provision_id)
        )

    def list_provision_versions(self) -> tuple[ProvisionVersion, ...]:
        return tuple(
            sorted(
                self.provision_versions.values(),
                key=lambda item: item.provision_version_id,
            )
        )

    def source_spans_for_version(
        self, provision_version_id: str
    ) -> tuple[SourceSpan, ...]:
        return tuple(
            sorted(
                (
                    span
                    for span in self.source_spans.values()
                    if span.provision_version_id == provision_version_id
                ),
                key=lambda item: (
                    item.page_number,
                    item.char_start or 0,
                    item.source_span_id,
                ),
            )
        )


class InMemoryResearchGraphRepository(ResearchGraphRepository):
    def __init__(self) -> None:
        self._relations: dict[tuple[str, str], ProgressiveRelation] = {}
        self._events: dict[str, list[MemoryEvent]] = defaultdict(list)

    def get_relation(
        self, organization_id: str, relation_id: str
    ) -> ProgressiveRelation:
        try:
            return self._relations[(organization_id, relation_id)]
        except KeyError as exc:
            raise NotFoundError(
                f"relation {relation_id!r} not found in organization"
            ) from exc

    def save_relation(self, relation: ProgressiveRelation, event: MemoryEvent) -> None:
        if (
            relation.organization_id != event.organization_id
            or relation.relation_id != event.target_id
        ):
            raise ConflictError("event and relation tenancy/identity must match")
        key = (relation.organization_id, relation.relation_id)
        existing = self._relations.get(key)
        if existing is not None and event.previous_state != existing.status:
            raise ConflictError("event previous_state does not match persisted state")
        if existing is None and event.previous_state is not None:
            raise ConflictError("new relation event cannot have previous_state")
        self._relations[key] = relation
        self._events[relation.organization_id].append(event)

    def active_relations(self, organization_id: str) -> tuple[ProgressiveRelation, ...]:
        return tuple(
            relation
            for (tenant, _), relation in self._relations.items()
            if tenant == organization_id and relation.active_for_navigation
        )

    def list_relations(
        self,
        organization_id: str,
        statuses: frozenset[TrustStatus] | None = None,
    ) -> tuple[ProgressiveRelation, ...]:
        return tuple(
            sorted(
                (
                    relation
                    for (tenant, _), relation in self._relations.items()
                    if tenant == organization_id
                    and (statuses is None or relation.status in statuses)
                ),
                key=lambda relation: relation.relation_id,
            )
        )

    def relations_using_version(
        self,
        organization_id: str,
        provision_version_id: str,
        status: TrustStatus | None = None,
    ) -> tuple[ProgressiveRelation, ...]:
        return tuple(
            relation
            for (tenant, _), relation in self._relations.items()
            if tenant == organization_id
            and provision_version_id in relation.evidence_version_ids
            and (status is None or relation.status == status)
        )

    def events(self, organization_id: str) -> tuple[MemoryEvent, ...]:
        return tuple(self._events[organization_id])
