from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from typing import Any, Self

from ..canonical import (
    BoundingBox,
    CanonicalEdge,
    CanonicalNodeType,
    Citation,
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
from ..db import apply_migrations
from ..errors import ConflictError, NotFoundError
from ..repositories import CanonicalRepository


class PostgresCanonicalRepository(CanonicalRepository):
    """DB-API/psycopg adapter; transaction ownership stays with the caller."""

    def __init__(self, connection: Any) -> None:
        self.connection = connection

    @classmethod
    def connect(cls, dsn: str, *, migrate: bool = False, **kwargs: Any) -> PostgresCanonicalRepository:
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - depends on optional package
            raise RuntimeError("install the postgres extra: pip install -e '.[postgres]'") from exc
        connection = psycopg.connect(dsn, **kwargs)
        if migrate:
            apply_migrations(connection)
        return cls(connection)

    def add_source_document(self, item: SourceDocument) -> None:
        self._insert_immutable(
            """
            INSERT INTO canonical.source_documents (
                source_document_id, organization_id, filename, media_type, checksum,
                file_size, ingested_at, ingested_by, source_uri
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (source_document_id) DO NOTHING
            RETURNING source_document_id
            """,
            (
                item.source_document_id,
                item.organization_id,
                item.filename,
                item.media_type,
                item.checksum,
                item.file_size,
                item.ingested_at,
                item.ingested_by,
                item.source_uri,
            ),
            item,
            lambda: self.get_source_document(item.source_document_id),
        )

    def add_instrument(self, item: LegalInstrument) -> None:
        self._insert_immutable(
            """
            INSERT INTO canonical.legal_instruments (
                instrument_id, title, canonical_title, instrument_type, jurisdiction,
                issuer, authority_level, subject_domain, language, external_identifier
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (instrument_id) DO NOTHING
            RETURNING instrument_id
            """,
            (
                item.instrument_id,
                item.title,
                item.canonical_title,
                item.instrument_type.value,
                item.jurisdiction,
                item.issuer,
                item.authority_level,
                item.subject_domain,
                item.language,
                item.external_identifier,
            ),
            item,
            lambda: self.get_instrument(item.instrument_id),
        )

    def add_document_version(self, item: DocumentVersion) -> None:
        self._insert_immutable(
            """
            INSERT INTO canonical.document_versions (
                document_version_id, instrument_id, source_document_id, status,
                version_label, version_number, publication_date, enacted_at,
                effective_from, effective_to, repealed_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (document_version_id) DO NOTHING
            RETURNING document_version_id
            """,
            (
                item.document_version_id,
                item.instrument_id,
                item.source_document_id,
                item.status.value,
                item.version_label,
                item.version_number,
                item.publication_date,
                item.enacted_at,
                item.effective_from,
                item.effective_to,
                item.repealed_at,
            ),
            item,
            lambda: self.get_document_version(item.document_version_id),
        )

    def add_provision(self, item: Provision) -> None:
        self._insert_immutable(
            """
            INSERT INTO canonical.provisions (
                provision_id, instrument_id, provision_type, number, label,
                title, parent_provision_id, ordinal, depth
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (provision_id) DO NOTHING
            RETURNING provision_id
            """,
            (
                item.provision_id,
                item.instrument_id,
                item.provision_type.value,
                item.number,
                item.label,
                item.title,
                item.parent_provision_id,
                item.ordinal,
                item.depth,
            ),
            item,
            lambda: self.get_provision(item.provision_id),
        )

    def add_provision_version(self, item: ProvisionVersion) -> None:
        instrument_id = self.get_provision(item.provision_id).instrument_id
        self._insert_immutable(
            """
            INSERT INTO canonical.provision_versions (
                provision_version_id, provision_id, document_version_id, instrument_id,
                text, normalized_text, effective_from, effective_to, status,
                created_from, supersedes_version_id
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (provision_version_id) DO NOTHING
            RETURNING provision_version_id
            """,
            (
                item.provision_version_id,
                item.provision_id,
                item.document_version_id,
                instrument_id,
                item.text,
                item.normalized_text,
                item.effective_from,
                item.effective_to,
                item.status.value,
                item.created_from,
                item.supersedes_version_id,
            ),
            item,
            lambda: self.get_provision_version(item.provision_version_id),
        )

    def add_source_span(self, item: SourceSpan) -> None:
        bbox = item.bbox
        self._insert_immutable(
            """
            INSERT INTO canonical.source_spans (
                source_span_id, source_document_id, document_version_id,
                provision_version_id, page_number, bbox_x1, bbox_y1, bbox_x2,
                bbox_y2, char_start, char_end, raw_text
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (source_span_id) DO NOTHING
            RETURNING source_span_id
            """,
            (
                item.source_span_id,
                item.source_document_id,
                item.document_version_id,
                item.provision_version_id,
                item.page_number,
                bbox.x1 if bbox else None,
                bbox.y1 if bbox else None,
                bbox.x2 if bbox else None,
                bbox.y2 if bbox else None,
                item.char_start,
                item.char_end,
                item.raw_text,
            ),
            item,
            lambda: self.get_source_span(item.source_span_id),
        )

    def add_citation(self, item: Citation) -> None:
        parameters = (
            item.citation_id,
            item.instrument_id,
            item.document_version_id,
            item.provision_id,
            item.provision_version_id,
            item.source_span_id,
            item.page,
            item.quoted_text,
        )
        row = self._fetchone(
            """
            INSERT INTO canonical.citations (
                citation_id, instrument_id, document_version_id, provision_id,
                provision_version_id, source_span_id, page, quoted_text
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (citation_id) DO NOTHING
            RETURNING citation_id
            """,
            parameters,
        )
        if row is None:
            existing = self._fetchone(
                """
                SELECT citation_id, instrument_id, document_version_id, provision_id,
                       provision_version_id, source_span_id, page, quoted_text
                FROM canonical.citations WHERE citation_id = %s
                """,
                (item.citation_id,),
            )
            self._require_same_identity(item.citation_id, existing, parameters)

    def add_edge(self, item: CanonicalEdge) -> None:
        source_type = self._node_type(item.source_node_id)
        target_type = self._node_type(item.target_node_id)
        parameters = (
            item.edge_id,
            source_type.value,
            item.source_node_id,
            target_type.value,
            item.target_node_id,
            item.edge_type.value,
            item.source_span_id,
            item.provenance.creation_method.value,
            item.confidence,
            item.provenance.created_at,
            item.provenance.created_by,
            item.provenance.source_id,
            item.provenance.parser_version,
            item.provenance.model_id,
        )
        row = self._fetchone(
            """
            INSERT INTO canonical.graph_edges (
                edge_id, source_node_type, source_node_id, target_node_type,
                target_node_id, edge_type, source_span_id, extraction_method,
                confidence, created_at, created_by, source_id, parser_version, model_id
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (edge_id) DO NOTHING
            RETURNING edge_id
            """,
            parameters,
        )
        if row is None:
            existing = self._fetchone(
                """
                SELECT edge_id, source_node_type, source_node_id, target_node_type,
                       target_node_id, edge_type, source_span_id, extraction_method,
                       confidence, created_at, created_by, source_id, parser_version, model_id
                FROM canonical.graph_edges WHERE edge_id = %s
                """,
                (item.edge_id,),
            )
            self._require_same_identity(item.edge_id, existing, parameters)

    def add_explicit_reference(self, item: ExplicitReference) -> None:
        parameters = (
            item.reference_id,
            item.source_provision_version_id,
            item.source_span_id,
            item.target_text,
            item.resolved_target_provision_id,
            item.resolution_status.value,
            item.confidence,
            item.extraction_method.value,
        )
        row = self._fetchone(
            """
            INSERT INTO canonical.explicit_references (
                reference_id, source_provision_version_id, source_span_id,
                target_text, resolved_target_provision_id, resolution_status,
                confidence, extraction_method
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (reference_id) DO NOTHING
            RETURNING reference_id
            """,
            parameters,
        )
        if row is None:
            existing = self._fetchone(
                """
                SELECT reference_id, source_provision_version_id, source_span_id,
                       target_text, resolved_target_provision_id, resolution_status,
                       confidence, extraction_method
                FROM canonical.explicit_references WHERE reference_id = %s
                """,
                (item.reference_id,),
            )
            self._require_same_identity(item.reference_id, existing, parameters)

    def applicable_provision_versions(self, provision_id: str, at: date) -> tuple[ProvisionVersion, ...]:
        rows = self._fetchall(
            """
            SELECT provision_version_id, provision_id, document_version_id, text,
                   normalized_text, status, created_from, effective_from,
                   effective_to, supersedes_version_id
            FROM canonical.provision_versions
            WHERE provision_id = %s
              AND (effective_from IS NULL OR effective_from <= %s)
              AND (effective_to IS NULL OR effective_to >= %s)
            ORDER BY effective_from NULLS FIRST, provision_version_id
            """,
            (provision_id, at, at),
        )
        if not rows:
            self.get_provision(provision_id)
        return tuple(self._provision_version(row) for row in rows)

    def get_instrument(self, instrument_id: str) -> LegalInstrument:
        row = self._required_row(
            """
            SELECT instrument_id, title, canonical_title, instrument_type,
                   jurisdiction, issuer, authority_level, subject_domain,
                   language, external_identifier
            FROM canonical.legal_instruments WHERE instrument_id = %s
            """,
            (instrument_id,),
            f"instrument {instrument_id!r} not found",
        )
        return LegalInstrument(row[0], row[1], row[2], InstrumentType(row[3]), row[4], *row[5:])

    def get_source_document(self, source_document_id: str) -> SourceDocument:
        row = self._required_row(
            """
            SELECT source_document_id, organization_id, filename, media_type,
                   checksum, file_size, ingested_by, source_uri, ingested_at
            FROM canonical.source_documents WHERE source_document_id = %s
            """,
            (source_document_id,),
            f"source document {source_document_id!r} not found",
        )
        return SourceDocument(row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7], row[8])

    def get_document_version(self, document_version_id: str) -> DocumentVersion:
        row = self._required_row(
            """
            SELECT document_version_id, instrument_id, source_document_id,
                   status, version_label, version_number, publication_date,
                   enacted_at, effective_from, effective_to, repealed_at
            FROM canonical.document_versions WHERE document_version_id = %s
            """,
            (document_version_id,),
            f"document version {document_version_id!r} not found",
        )
        return DocumentVersion(row[0], row[1], row[2], DocumentStatus(row[3]), *row[4:])

    def get_provision(self, provision_id: str) -> Provision:
        row = self._required_row(
            """
            SELECT provision_id, instrument_id, provision_type, number, label,
                   title, parent_provision_id, ordinal, depth
            FROM canonical.provisions WHERE provision_id = %s
            """,
            (provision_id,),
            f"provision {provision_id!r} not found",
        )
        return Provision(row[0], row[1], ProvisionType(row[2]), *row[3:])

    def get_provision_version(self, provision_version_id: str) -> ProvisionVersion:
        row = self._required_row(
            """
            SELECT provision_version_id, provision_id, document_version_id, text,
                   normalized_text, status, created_from, effective_from,
                   effective_to, supersedes_version_id
            FROM canonical.provision_versions WHERE provision_version_id = %s
            """,
            (provision_version_id,),
            f"provision version {provision_version_id!r} not found",
        )
        return self._provision_version(row)

    def get_source_span(self, source_span_id: str) -> SourceSpan:
        row = self._required_row(
            """
            SELECT source_span_id, source_document_id, document_version_id,
                   provision_version_id, page_number, raw_text, bbox_x1, bbox_y1,
                   bbox_x2, bbox_y2, char_start, char_end
            FROM canonical.source_spans WHERE source_span_id = %s
            """,
            (source_span_id,),
            f"source span {source_span_id!r} not found",
        )
        return self._source_span(row)

    def references_from_version(self, provision_version_id: str) -> tuple[ExplicitReference, ...]:
        rows = self._fetchall(
            """
            SELECT reference_id, source_provision_version_id, target_text,
                   source_span_id, extraction_method, resolution_status,
                   resolved_target_provision_id, confidence
            FROM canonical.explicit_references
            WHERE source_provision_version_id = %s ORDER BY reference_id
            """,
            (provision_version_id,),
        )
        return tuple(
            ExplicitReference(
                row[0],
                row[1],
                row[2],
                row[3],
                CreationMethod(row[4]),
                ResolutionStatus(row[5]),
                row[6],
                row[7],
            )
            for row in rows
        )

    def list_provisions(self) -> tuple[Provision, ...]:
        rows = self._fetchall(
            """
            SELECT provision_id, instrument_id, provision_type, number, label,
                   title, parent_provision_id, ordinal, depth
            FROM canonical.provisions ORDER BY provision_id
            """
        )
        return tuple(Provision(row[0], row[1], ProvisionType(row[2]), *row[3:]) for row in rows)

    def list_provision_versions(self, ids: Any = None) -> tuple[ProvisionVersion, ...]:
        if ids is None:
            rows = self._fetchall(
                """
                SELECT provision_version_id, provision_id, document_version_id, text,
                       normalized_text, status, created_from, effective_from,
                       effective_to, supersedes_version_id
                FROM canonical.provision_versions ORDER BY provision_version_id
                """
            )
        else:
            selected = tuple(ids)
            if not selected:
                return ()
            rows = self._fetchall(
                """
                SELECT provision_version_id, provision_id, document_version_id, text,
                       normalized_text, status, created_from, effective_from,
                       effective_to, supersedes_version_id
                FROM canonical.provision_versions
                WHERE provision_version_id = ANY(%s)
                ORDER BY provision_version_id
                """,
                (list(selected),),
            )
        return tuple(self._provision_version(row) for row in rows)

    def fts_search(self, query: str, limit: int = 512) -> dict[str, float] | None:
        """GIN/tsvector prefilter over normalized_text (V0008).

        OR semantics: a superset of the tools' AND-token check, so the exact
        Python filter afterwards keeps precision while recall stays intact.
        """
        try:
            rows = self._fetchall(
                """
                SELECT provision_version_id, ts_rank(tsv, query) AS rank
                FROM canonical.provision_versions,
                     websearch_to_tsquery('simple', %s) query
                WHERE tsv @@ query
                ORDER BY rank DESC
                LIMIT %s
                """,
                (query, limit),
            )
        except Exception:  # noqa: BLE001 - prefilter is best-effort only
            return None
        return {row[0]: float(row[1]) for row in rows}

    def edges_touching(self, node_id: str) -> tuple[CanonicalEdge, ...]:
        from ..canonical import CanonicalEdgeType, CreationMethod, Provenance

        rows = self._fetchall(
            """
            SELECT edge_id, source_node_id, target_node_id, edge_type,
                   source_span_id, extraction_method, confidence, created_at,
                   created_by, source_id, parser_version, model_id
            FROM canonical.graph_edges
            WHERE source_node_id = %s OR target_node_id = %s
            ORDER BY edge_id
            """,
            (node_id, node_id),
        )
        edges = []
        for row in rows:
            provenance = Provenance(
                created_by=row[8],
                creation_method=CreationMethod(row[5]) if row[5] else CreationMethod.PARSER,
                source_id=row[9],
                created_at=row[7],
                parser_version=row[10],
                model_id=row[11],
            )
            edges.append(
                CanonicalEdge(
                    edge_id=row[0],
                    source_node_id=row[1],
                    target_node_id=row[2],
                    edge_type=CanonicalEdgeType(row[3]),
                    provenance=provenance,
                    source_span_id=row[4],
                    confidence=row[6],
                )
            )
        return tuple(edges)

    def source_spans_for_version(self, provision_version_id: str) -> tuple[SourceSpan, ...]:
        rows = self._fetchall(
            """
            SELECT source_span_id, source_document_id, document_version_id,
                   provision_version_id, page_number, raw_text, bbox_x1, bbox_y1,
                   bbox_x2, bbox_y2, char_start, char_end
            FROM canonical.source_spans
            WHERE provision_version_id = %s
            ORDER BY page_number, char_start NULLS FIRST, source_span_id
            """,
            (provision_version_id,),
        )
        return tuple(self._source_span(row) for row in rows)

    def _node_type(self, node_id: str) -> CanonicalNodeType:
        row = self._fetchone(
            """
            SELECT node_type FROM (
                SELECT 'source_document' AS node_type FROM canonical.source_documents WHERE source_document_id = %s
                UNION ALL SELECT 'legal_instrument' FROM canonical.legal_instruments WHERE instrument_id = %s
                UNION ALL SELECT 'document_version' FROM canonical.document_versions WHERE document_version_id = %s
                UNION ALL SELECT 'provision' FROM canonical.provisions WHERE provision_id = %s
                UNION ALL SELECT 'provision_version' FROM canonical.provision_versions WHERE provision_version_id = %s
            ) nodes LIMIT 1
            """,
            (node_id, node_id, node_id, node_id, node_id),
        )
        if row is None:
            raise NotFoundError(f"canonical node {node_id!r} not found")
        return CanonicalNodeType(row[0])

    def _insert_immutable(
        self,
        sql: str,
        parameters: Sequence[object],
        expected: object,
        load_existing: Callable[[], object],
    ) -> None:
        row = self._fetchone(sql, parameters)
        if row is None and load_existing() != expected:
            identity = parameters[0]
            raise ConflictError(f"canonical identity {identity!r} already exists with different data")

    def _execute(self, sql: str, parameters: Sequence[object] = ()) -> None:
        cursor = self.connection.cursor()
        try:
            cursor.execute(sql, tuple(parameters))
        finally:
            cursor.close()

    @staticmethod
    def _require_same_identity(
        identity: str,
        existing: Sequence[object] | None,
        expected: Sequence[object],
    ) -> None:
        if existing is None or tuple(existing) != tuple(expected):
            raise ConflictError(f"canonical identity {identity!r} already exists with different data")

    def _fetchone(self, sql: str, parameters: Sequence[object] = ()) -> tuple[Any, ...] | None:
        cursor = self.connection.cursor()
        try:
            cursor.execute(sql, tuple(parameters))
            return cursor.fetchone()
        finally:
            cursor.close()

    def _fetchall(self, sql: str, parameters: Sequence[object] = ()) -> list[tuple[Any, ...]]:
        cursor = self.connection.cursor()
        try:
            cursor.execute(sql, tuple(parameters))
            return list(cursor.fetchall())
        finally:
            cursor.close()

    def _required_row(
        self,
        sql: str,
        parameters: Sequence[object],
        message: str,
    ) -> tuple[Any, ...]:
        row = self._fetchone(sql, parameters)
        if row is None:
            raise NotFoundError(message)
        return row

    @staticmethod
    def _provision_version(row: Sequence[Any]) -> ProvisionVersion:
        return ProvisionVersion(
            row[0],
            row[1],
            row[2],
            row[3],
            row[4],
            DocumentStatus(row[5]),
            row[6],
            row[7],
            row[8],
            row[9],
        )

    @staticmethod
    def _source_span(row: Sequence[Any]) -> SourceSpan:
        bbox = BoundingBox(row[6], row[7], row[8], row[9]) if row[6] is not None else None
        return SourceSpan(row[0], row[1], row[2], row[3], row[4], row[5], bbox, row[10], row[11])


class PostgresResearchHistoryStore:
    """Durable, tenant-isolated research-history store backed by PostgreSQL.

    Mirrors the contract of ``application.ResearchHistoryStore`` but persists
    the response payloads in ``research_history.episodes``. Every operation
    scopes to the organization via RLS and ``SET LOCAL``.
    """

    def __init__(self, connection: Any | None = None, connection_factory: Callable[[], Any] | None = None) -> None:
        self.connection = connection
        self.connection_factory = connection_factory

    def _op(self) -> Any:
        if self.connection is not None:
            return self.connection
        assert self.connection_factory is not None
        connection = self.connection_factory()
        return connection

    @staticmethod
    def _finish(connection: Any, *, managed: bool, exc: BaseException | None) -> None:
        if not managed:
            return
        try:
            if exc is None:
                connection.commit()
            else:
                connection.rollback()
        finally:
            connection.close()

    def save(self, organization_id: str, episode_id: str, payload: dict) -> None:
        derived = _history_derived(payload)
        managed = self.connection is None
        connection = self._op()
        error: BaseException | None = None
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT set_config('legal_agent.organization_id', %s, true)",
                    (organization_id,),
                )
                cursor.execute(
                    """
                    INSERT INTO research_history.episodes (
                        organization_id, episode_id, conversation_id, user_id, question,
                        applicable_time, created_at, completed, iterations, has_answer, payload
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (organization_id, episode_id) DO UPDATE
                    SET payload = EXCLUDED.payload,
                        completed = EXCLUDED.completed,
                        iterations = EXCLUDED.iterations,
                        has_answer = EXCLUDED.has_answer
                    """,
                    (
                        organization_id,
                        episode_id,
                        derived["conversation_id"],
                        derived["user_id"],
                        derived["question"],
                        derived["applicable_time"],
                        derived["created_at"],
                        derived["completed"],
                        derived["iterations"],
                        derived["has_answer"],
                        json.dumps(payload, ensure_ascii=False),
                    ),
                )
        except Exception as exc:
            error = exc
            raise
        finally:
            self._finish(connection, managed=managed, exc=error)

    def get(self, organization_id: str, episode_id: str) -> dict:
        result = self._query(
            organization_id,
            "SELECT payload FROM research_history.episodes WHERE episode_id = %s",
            (episode_id,),
        )
        if not result:
            raise NotFoundError(f"research episode {episode_id!r} was not found")
        return _coerce_payload(result[0][0])

    def list(self, organization_id: str, limit: int = 100) -> tuple[dict, ...]:
        rows = self._query(
            organization_id,
            """
            SELECT payload FROM research_history.episodes
            ORDER BY created_at DESC, episode_id LIMIT %s
            """,
            (limit,),
        )
        return tuple(_coerce_payload(row[0]) for row in rows)

    def _query(
        self, organization_id: str, sql: str, parameters: Sequence[object] = ()
    ) -> Sequence[Any]:
        managed = self.connection is None
        connection = self._op()
        error: BaseException | None = None
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT set_config('legal_agent.organization_id', %s, true)",
                    (organization_id,),
                )
                cursor.execute(sql, tuple(parameters))
                return list(cursor.fetchall())
        except Exception as exc:
            error = exc
            raise
        finally:
            self._finish(connection, managed=managed, exc=error)


def _coerce_payload(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    return json.loads(value) if isinstance(value, (str, bytes)) else dict(value)


def _history_derived(payload: dict) -> dict[str, object]:
    trace = payload.get("trace", {})
    created_at: datetime | None = None
    try:
        created_at = datetime.fromisoformat(trace.get("created_at", ""))
    except ValueError:
        created_at = None
    applicable: date | None = None
    try:
        applicable = date.fromisoformat(trace.get("applicable_time", ""))
    except ValueError:
        applicable = None
    return {
        "conversation_id": payload.get("conversation_id", f"conversation-{payload.get('episode_id', '')}"),
        "user_id": payload.get("user_id", ""),
        "question": trace.get("question", ""),
        "applicable_time": applicable or date(1970, 1, 1),
        "created_at": created_at or datetime(1970, 1, 1, tzinfo=UTC),
        "completed": bool(payload.get("completed")),
        "iterations": int(payload.get("iterations", 0)),
        "has_answer": payload.get("answer") is not None,
    }


class PostgresUnitOfWork:
    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        self.connection_factory = connection_factory
        self.connection: Any | None = None
        self.canonical: PostgresCanonicalRepository | None = None

    def __enter__(self) -> Self:
        self.connection = self.connection_factory()
        self.canonical = PostgresCanonicalRepository(self.connection)
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        assert self.connection is not None
        try:
            if exc_type is None:
                self.connection.commit()
            else:
                self.connection.rollback()
        finally:
            self.connection.close()
        return False
