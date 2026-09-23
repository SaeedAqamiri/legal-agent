from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from ..canonical import (
    CanonicalEdge,
    CanonicalEdgeType,
    CreationMethod,
    DocumentVersion,
    ExplicitReference,
    LegalInstrument,
    Provenance,
    Provision,
    ProvisionType,
    ProvisionVersion,
    SourceDocument,
    SourceSpan,
)
from ..errors import NotFoundError
from ..repositories import CanonicalRepository
from .models import IngestionResult, ParsedDocument, ProvisionInput
from .normalization import normalize_legal_text, normalize_number
from .references import DeterministicReferenceExtractor


def stable_id(prefix: str, *parts: Any) -> str:
    serialized = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return f"{prefix}_{sha256(serialized.encode('utf-8')).hexdigest()[:24]}"


@dataclass(frozen=True, slots=True)
class _IngestedProvision:
    source: ProvisionInput
    provision: Provision
    version: ProvisionVersion
    span: SourceSpan
    spans: tuple[SourceSpan, ...] = ()


class CanonicalIngestionPipeline:
    """Deterministic ingestion from parser-neutral input into the canonical repository."""

    def __init__(
        self,
        repository: CanonicalRepository,
        reference_extractor: DeterministicReferenceExtractor | None = None,
    ) -> None:
        self.repository = repository
        self.reference_extractor = reference_extractor or DeterministicReferenceExtractor()

    def ingest(self, parsed: ParsedDocument) -> IngestionResult:
        source_document_id = parsed.source.source_document_id or stable_id("src", parsed.source.checksum)
        instrument_id = parsed.instrument.instrument_id or self._instrument_id(parsed)
        document_version_id = parsed.version.document_version_id or self._document_version_id(
            parsed, instrument_id, source_document_id
        )

        source_document = SourceDocument(
            source_document_id=source_document_id,
            organization_id=parsed.source.organization_id,
            filename=parsed.source.filename,
            media_type=parsed.source.media_type,
            checksum=parsed.source.checksum,
            file_size=parsed.source.file_size,
            ingested_at=parsed.source.ingested_at,
            ingested_by=parsed.source.ingested_by,
            source_uri=parsed.source.source_uri,
        )
        instrument = LegalInstrument(
            instrument_id=instrument_id,
            title=parsed.instrument.title,
            canonical_title=parsed.instrument.canonical_title,
            instrument_type=parsed.instrument.instrument_type,
            jurisdiction=parsed.instrument.jurisdiction,
            issuer=parsed.instrument.issuer,
            authority_level=parsed.instrument.authority_level,
            subject_domain=parsed.instrument.subject_domain,
            language=parsed.instrument.language,
            external_identifier=parsed.instrument.external_identifier,
        )
        document_version = DocumentVersion(
            document_version_id=document_version_id,
            instrument_id=instrument_id,
            source_document_id=source_document_id,
            status=parsed.version.status,
            version_label=parsed.version.version_label,
            version_number=parsed.version.version_number,
            publication_date=parsed.version.publication_date,
            enacted_at=parsed.version.enacted_at,
            effective_from=parsed.version.effective_from,
            effective_to=parsed.version.effective_to,
            repealed_at=parsed.version.repealed_at,
        )

        self.repository.add_source_document(source_document)
        # Reuse an already-known instrument identity instead of re-adding it:
        # variant documents (same normalized canonical title) share one
        # instrument and only add new document versions.
        try:
            self.repository.get_instrument(instrument_id)
        except NotFoundError:
            self.repository.add_instrument(instrument)
        self.repository.add_document_version(document_version)

        ingested: list[_IngestedProvision] = []
        for position, provision_input in enumerate(parsed.provisions):
            self._ingest_provision(
                parsed,
                provision_input,
                instrument_id=instrument_id,
                document_version_id=document_version_id,
                source_document_id=source_document_id,
                parent_id=None,
                parent_path=(),
                depth=0,
                fallback_ordinal=position,
                output=ingested,
            )

        article_targets: dict[str, list[str]] = {}
        for item in ingested:
            if item.provision.provision_type == ProvisionType.ARTICLE and item.provision.number:
                article_targets.setdefault(item.provision.number, []).append(item.provision.provision_id)

        references: list[ExplicitReference] = []
        edges: list[CanonicalEdge] = []
        aliases = (
            parsed.instrument.canonical_title,
            parsed.instrument.title,
            *parsed.instrument.aliases,
        )
        for item in ingested:
            mentions = self.reference_extractor.extract(
                item.version.normalized_text,
                article_targets=article_targets,
                instrument_aliases=aliases,
                source_provision_id=item.provision.provision_id,
            )
            for mention in mentions:
                reference_id = stable_id(
                    "ref",
                    item.version.provision_version_id,
                    item.span.source_span_id,
                    mention.start,
                    mention.end,
                    mention.target_text,
                )
                reference = ExplicitReference(
                    reference_id=reference_id,
                    source_provision_version_id=item.version.provision_version_id,
                    target_text=mention.target_text,
                    source_span_id=item.span.source_span_id,
                    extraction_method=CreationMethod.DETERMINISTIC_EXTRACTOR,
                    resolution_status=mention.resolution_status,
                    resolved_target_provision_id=mention.resolved_target_provision_id,
                    confidence=mention.confidence,
                )
                self.repository.add_explicit_reference(reference)
                references.append(reference)
                if mention.resolved_target_provision_id is None:
                    continue
                edge = CanonicalEdge(
                    edge_id=stable_id("edge", reference_id, CanonicalEdgeType.EXPLICITLY_REFERENCES.value),
                    source_node_id=item.version.provision_version_id,
                    target_node_id=mention.resolved_target_provision_id,
                    edge_type=CanonicalEdgeType.EXPLICITLY_REFERENCES,
                    provenance=Provenance(
                        created_by="deterministic-reference-extractor",
                        creation_method=CreationMethod.DETERMINISTIC_EXTRACTOR,
                        source_id=item.span.source_span_id,
                        created_at=parsed.source.ingested_at,
                        parser_version=item.source.created_from,
                    ),
                    source_span_id=item.span.source_span_id,
                    confidence=mention.confidence,
                )
                self.repository.add_edge(edge)
                edges.append(edge)

        return IngestionResult(
            source_document_id=source_document_id,
            instrument_id=instrument_id,
            document_version_id=document_version_id,
            provision_ids=tuple(item.provision.provision_id for item in ingested),
            provision_version_ids=tuple(item.version.provision_version_id for item in ingested),
            source_span_ids=tuple(
                item_span.source_span_id
                for item in ingested
                for item_span in (item.spans or (item.span,))
            ),
            explicit_references=tuple(references),
            canonical_edges=tuple(edges),
        )

    @staticmethod
    def _instrument_id(parsed: ParsedDocument) -> str:
        identity = parsed.instrument.external_identifier or normalize_legal_text(parsed.instrument.canonical_title)
        return stable_id(
            "inst",
            parsed.instrument.jurisdiction,
            parsed.instrument.instrument_type.value,
            identity,
        )

    @staticmethod
    def _document_version_id(parsed: ParsedDocument, instrument_id: str, source_document_id: str) -> str:
        return stable_id(
            "docv",
            instrument_id,
            parsed.version.version_label,
            parsed.version.version_number,
            parsed.version.publication_date,
            parsed.version.effective_from,
            source_document_id,
        )

    def _ingest_provision(
        self,
        parsed: ParsedDocument,
        provision_input: ProvisionInput,
        *,
        instrument_id: str,
        document_version_id: str,
        source_document_id: str,
        parent_id: str | None,
        parent_path: tuple[str, ...],
        depth: int,
        fallback_ordinal: int,
        output: list[_IngestedProvision],
    ) -> None:
        ordinal = provision_input.ordinal if provision_input.ordinal is not None else fallback_ordinal
        number = normalize_number(provision_input.number)
        segment = f"{provision_input.provision_type.value}:{number or f'ordinal-{ordinal}'}"
        path = (*parent_path, segment)
        provision_id = stable_id("prov", instrument_id, *path)
        normalized_text = provision_input.normalized_text or normalize_legal_text(provision_input.text)
        effective_from = provision_input.effective_from or parsed.version.effective_from
        effective_to = provision_input.effective_to or parsed.version.effective_to
        status = provision_input.status or parsed.version.status
        provision_version_id = stable_id(
            "provv",
            provision_id,
            document_version_id,
            normalized_text,
            effective_from,
            effective_to,
        )
        raw_text = provision_input.raw_text if provision_input.raw_text is not None else provision_input.text
        source_span_id = stable_id(
            "span",
            provision_version_id,
            provision_input.page_number,
            provision_input.char_start,
            provision_input.char_end,
            provision_input.bbox,
            raw_text,
        )
        provision = Provision(
            provision_id=provision_id,
            instrument_id=instrument_id,
            provision_type=provision_input.provision_type,
            number=number,
            label=provision_input.label,
            title=provision_input.title,
            parent_provision_id=parent_id,
            ordinal=ordinal,
            depth=depth,
        )
        version = ProvisionVersion(
            provision_version_id=provision_version_id,
            provision_id=provision_id,
            document_version_id=document_version_id,
            text=provision_input.text,
            normalized_text=normalized_text,
            effective_from=effective_from,
            effective_to=effective_to,
            status=status,
            created_from=provision_input.created_from,
        )
        span = SourceSpan(
            source_span_id=source_span_id,
            source_document_id=source_document_id,
            document_version_id=document_version_id,
            provision_version_id=provision_version_id,
            page_number=provision_input.page_number,
            bbox=provision_input.bbox,
            char_start=provision_input.char_start,
            char_end=provision_input.char_end,
            raw_text=raw_text,
        )
        spans: tuple[SourceSpan, ...] = (span,)
        if provision_input.page_segments:
            # One span per page so citations can point at the exact page a
            # passage lives on. Each segment is a substring of raw_text and
            # carries its own deterministic id.
            page_spans: list[SourceSpan] = []
            for page_segment in provision_input.page_segments:
                segment_id = stable_id(
                    "span",
                    provision_version_id,
                    page_segment.page_number,
                    page_segment.char_start,
                    page_segment.char_end,
                    page_segment.text,
                )
                page_spans.append(
                    SourceSpan(
                        source_span_id=segment_id,
                        source_document_id=source_document_id,
                        document_version_id=document_version_id,
                        provision_version_id=provision_version_id,
                        page_number=page_segment.page_number,
                        char_start=page_segment.char_start,
                        char_end=page_segment.char_end,
                        raw_text=page_segment.text,
                    )
                )
            spans = (page_spans[0], *page_spans)
            span = page_spans[0]
        self.repository.add_provision(provision)
        self.repository.add_provision_version(version)
        for item_span in spans:
            self.repository.add_source_span(item_span)
        output.append(_IngestedProvision(provision_input, provision, version, span, spans))

        for position, child in enumerate(provision_input.children):
            self._ingest_provision(
                parsed,
                child,
                instrument_id=instrument_id,
                document_version_id=document_version_id,
                source_document_id=source_document_id,
                parent_id=provision_id,
                parent_path=path,
                depth=depth + 1,
                fallback_ordinal=position,
                output=output,
            )
