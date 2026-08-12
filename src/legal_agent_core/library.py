from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .canonical import ProvisionVersion
from .errors import NotFoundError
from .repositories import CanonicalRepository
from .retrieval import search_tokens
from .security import AuthorizationService, Permission, Principal


@dataclass(frozen=True, slots=True)
class LibraryDocument:
    document_version_id: str
    instrument_id: str
    title: str
    canonical_title: str
    instrument_type: str
    jurisdiction: str
    issuer: str | None
    subject_domain: str | None
    status: str
    version_label: str | None
    version_number: int | None
    publication_date: date | None
    effective_from: date | None
    effective_to: date | None
    source_document_id: str
    filename: str
    media_type: str
    source_uri: str
    provision_count: int
    page_count: int


@dataclass(frozen=True, slots=True)
class LibrarySpan:
    source_span_id: str
    page_number: int
    raw_text: str
    char_start: int | None
    char_end: int | None
    bbox: tuple[float, float, float, float] | None


@dataclass(frozen=True, slots=True)
class LibraryProvision:
    provision_id: str
    provision_version_id: str
    provision_type: str
    number: str | None
    label: str
    title: str | None
    parent_provision_id: str | None
    ordinal: int
    depth: int
    status: str
    effective_from: date | None
    effective_to: date | None
    text: str
    spans: tuple[LibrarySpan, ...]


@dataclass(frozen=True, slots=True)
class LibraryDocumentDetail:
    document: LibraryDocument
    provisions: tuple[LibraryProvision, ...]


class DocumentLibraryService:
    def __init__(
        self,
        canonical: CanonicalRepository,
        authorization: AuthorizationService,
    ) -> None:
        self.canonical = canonical
        self.authorization = authorization

    def search(
        self,
        principal: Principal,
        organization_id: str,
        *,
        query: str = "",
        applicable_time: date | None = None,
    ) -> tuple[LibraryDocument, ...]:
        self.authorization.require(principal, Permission.READ_LIBRARY, organization_id)
        query_terms = search_tokens(query)
        grouped = self._grouped_versions()
        results: list[LibraryDocument] = []
        for document_version_id, versions in grouped.items():
            document = self.canonical.get_document_version(document_version_id)
            source = self.canonical.get_source_document(document.source_document_id)
            if source.organization_id not in {None, organization_id}:
                continue
            if applicable_time is not None and not document.applies_at(applicable_time):
                continue
            applicable_versions = tuple(
                version
                for version in versions
                if applicable_time is None or version.applies_at(applicable_time)
            )
            if not applicable_versions:
                continue
            instrument = self.canonical.get_instrument(document.instrument_id)
            searchable = " ".join(
                (
                    instrument.title,
                    instrument.canonical_title,
                    instrument.subject_domain or "",
                    *(
                        self.canonical.get_provision(item.provision_id).label
                        for item in applicable_versions
                    ),
                    *(item.normalized_text for item in applicable_versions),
                )
            )
            if query_terms and not query_terms <= search_tokens(searchable):
                continue
            results.append(self._summary(document_version_id, applicable_versions))
        return tuple(
            sorted(results, key=lambda item: (item.title, item.document_version_id))
        )

    def get_document(
        self,
        principal: Principal,
        organization_id: str,
        document_version_id: str,
    ) -> LibraryDocumentDetail:
        self.authorization.require(principal, Permission.READ_LIBRARY, organization_id)
        document = self.canonical.get_document_version(document_version_id)
        source = self.canonical.get_source_document(document.source_document_id)
        if source.organization_id not in {None, organization_id}:
            raise NotFoundError(
                f"document version {document_version_id!r} was not found"
            )
        versions = tuple(
            version
            for version in self.canonical.list_provision_versions()
            if version.document_version_id == document_version_id
        )
        if not versions:
            raise NotFoundError(
                f"document version {document_version_id!r} has no provisions"
            )
        provisions = tuple(
            sorted(
                (self._provision(version) for version in versions),
                key=lambda item: (item.ordinal, item.depth, item.provision_id),
            )
        )
        return LibraryDocumentDetail(
            self._summary(document_version_id, versions), provisions
        )

    def _grouped_versions(self) -> dict[str, tuple[ProvisionVersion, ...]]:
        grouped: dict[str, list[ProvisionVersion]] = {}
        for version in self.canonical.list_provision_versions():
            grouped.setdefault(version.document_version_id, []).append(version)
        return {key: tuple(value) for key, value in grouped.items()}

    def _summary(
        self,
        document_version_id: str,
        versions: tuple[ProvisionVersion, ...],
    ) -> LibraryDocument:
        document = self.canonical.get_document_version(document_version_id)
        instrument = self.canonical.get_instrument(document.instrument_id)
        source = self.canonical.get_source_document(document.source_document_id)
        pages = [
            span.page_number
            for version in versions
            for span in self.canonical.source_spans_for_version(
                version.provision_version_id
            )
        ]
        return LibraryDocument(
            document.document_version_id,
            instrument.instrument_id,
            instrument.title,
            instrument.canonical_title,
            instrument.instrument_type.value,
            instrument.jurisdiction,
            instrument.issuer,
            instrument.subject_domain,
            document.status.value,
            document.version_label,
            document.version_number,
            document.publication_date,
            document.effective_from,
            document.effective_to,
            source.source_document_id,
            source.filename,
            source.media_type,
            source.source_uri,
            len(versions),
            max(pages, default=0),
        )

    def _provision(self, version: ProvisionVersion) -> LibraryProvision:
        provision = self.canonical.get_provision(version.provision_id)
        spans = tuple(
            LibrarySpan(
                span.source_span_id,
                span.page_number,
                span.raw_text,
                span.char_start,
                span.char_end,
                (
                    (span.bbox.x1, span.bbox.y1, span.bbox.x2, span.bbox.y2)
                    if span.bbox is not None
                    else None
                ),
            )
            for span in self.canonical.source_spans_for_version(
                version.provision_version_id
            )
        )
        return LibraryProvision(
            provision.provision_id,
            version.provision_version_id,
            provision.provision_type.value,
            provision.number,
            provision.label,
            provision.title,
            provision.parent_provision_id,
            provision.ordinal,
            provision.depth,
            version.status.value,
            version.effective_from,
            version.effective_to,
            version.text,
            spans,
        )
