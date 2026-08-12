from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

from ..errors import ConflictError, NotFoundError
from ..ingestion.operations import (
    AmendmentLink,
    AmendmentRelationType,
    CorrectionAnnotation,
    CorrectionStatus,
    DocumentParser,
    IngestionJob,
    IngestionJobStatus,
    SourceLoader,
    WorkerOutcome,
)
from ..ingestion.pipeline import CanonicalIngestionPipeline
from .postgres import PostgresCanonicalRepository

_JOB_COLUMNS = """
job_id, organization_id, idempotency_key, source_uri, parser_name,
parser_version, status, attempts, max_attempts, available_at, locked_at,
locked_by, last_error, payload, result, created_at, updated_at
"""


class PostgresIngestionRepository:
    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def enqueue(self, job: IngestionJob) -> IngestionJob:
        row = self._fetchone(
            f"""
            INSERT INTO ingestion.jobs (
                job_id, organization_id, idempotency_key, source_uri, parser_name,
                parser_version, status, attempts, max_attempts, available_at,
                locked_at, locked_by, last_error, payload, result, created_at, updated_at
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s
            )
            ON CONFLICT DO NOTHING
            RETURNING {_JOB_COLUMNS}
            """,
            (
                job.job_id,
                job.organization_id,
                job.idempotency_key,
                job.source_uri,
                job.parser_name,
                job.parser_version,
                job.status.value,
                job.attempts,
                job.max_attempts,
                job.available_at,
                job.locked_at,
                job.locked_by,
                job.last_error,
                self._json(job.payload),
                self._json(job.result) if job.result is not None else None,
                job.created_at,
                job.updated_at,
            ),
        )
        if row is not None:
            return self._job(row)
        existing = self._fetchone(
            f"""
            SELECT {_JOB_COLUMNS} FROM ingestion.jobs
            WHERE COALESCE(organization_id, '__shared__') = COALESCE(%s, '__shared__')
              AND idempotency_key = %s
            """,
            (job.organization_id, job.idempotency_key),
        )
        if existing is None:
            raise ConflictError(f"ingestion job {job.job_id!r} conflicts with an existing identity")
        decoded = self._job(existing)
        identity = (decoded.source_uri, decoded.parser_name, decoded.parser_version, decoded.payload)
        requested = (job.source_uri, job.parser_name, job.parser_version, job.payload)
        if identity != requested:
            raise ConflictError("ingestion idempotency key was reused with different input")
        return decoded

    def get(self, job_id: str) -> IngestionJob:
        row = self._fetchone(
            f"SELECT {_JOB_COLUMNS} FROM ingestion.jobs WHERE job_id = %s",
            (job_id,),
        )
        if row is None:
            raise NotFoundError(f"ingestion job {job_id!r} not found")
        return self._job(row)

    def claim_next(self, worker_id: str, now: datetime, stale_before: datetime) -> IngestionJob | None:
        row = self._fetchone(
            f"""
            WITH candidate AS (
                SELECT job_id
                FROM ingestion.jobs
                WHERE attempts < max_attempts
                  AND (
                    (status IN ('queued', 'failed') AND available_at <= %s)
                    OR (status = 'running' AND locked_at <= %s)
                  )
                ORDER BY available_at, created_at, job_id
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            UPDATE ingestion.jobs AS jobs
            SET status = 'running', attempts = jobs.attempts + 1,
                locked_at = %s, locked_by = %s, updated_at = %s
            FROM candidate
            WHERE jobs.job_id = candidate.job_id
            RETURNING {_JOB_COLUMNS}
            """,
            (now, stale_before, now, worker_id, now),
        )
        return self._job(row) if row is not None else None

    def mark_succeeded(
        self,
        job_id: str,
        worker_id: str,
        attempt: int,
        result: Mapping[str, object],
        now: datetime,
    ) -> IngestionJob:
        row = self._fetchone(
            f"""
            UPDATE ingestion.jobs
            SET status = 'succeeded', locked_at = NULL, locked_by = NULL,
                last_error = NULL, result = %s::jsonb, updated_at = %s
            WHERE job_id = %s AND status = 'running'
              AND locked_by = %s AND attempts = %s
            RETURNING {_JOB_COLUMNS}
            """,
            (self._json(result), now, job_id, worker_id, attempt),
        )
        if row is None:
            raise ConflictError("ingestion job lock was lost or attempt is stale")
        return self._job(row)

    def mark_failed(
        self,
        job_id: str,
        worker_id: str,
        attempt: int,
        error: str,
        retry_at: datetime,
        now: datetime,
    ) -> IngestionJob:
        row = self._fetchone(
            f"""
            UPDATE ingestion.jobs
            SET status = CASE WHEN attempts >= max_attempts THEN 'dead_letter' ELSE 'failed' END,
                available_at = %s, locked_at = NULL, locked_by = NULL,
                last_error = %s, updated_at = %s
            WHERE job_id = %s AND status = 'running'
              AND locked_by = %s AND attempts = %s
            RETURNING {_JOB_COLUMNS}
            """,
            (retry_at, error[:2000], now, job_id, worker_id, attempt),
        )
        if row is None:
            raise ConflictError("ingestion job lock was lost or attempt is stale")
        return self._job(row)

    def add_correction(self, annotation: CorrectionAnnotation) -> None:
        row = self._fetchone(
            """
            INSERT INTO ingestion.correction_annotations (
                annotation_id, organization_id, source_document_id, source_span_id,
                original_text, corrected_text, reason, status, created_by,
                created_at, reviewed_by, reviewed_at, review_note
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (annotation_id) DO NOTHING RETURNING annotation_id
            """,
            (
                annotation.annotation_id,
                annotation.organization_id,
                annotation.source_document_id,
                annotation.source_span_id,
                annotation.original_text,
                annotation.corrected_text,
                annotation.reason,
                annotation.status.value,
                annotation.created_by,
                annotation.created_at,
                annotation.reviewed_by,
                annotation.reviewed_at,
                annotation.review_note,
            ),
        )
        if row is None and self.get_correction(annotation.annotation_id) != annotation:
            raise ConflictError(f"correction {annotation.annotation_id!r} already exists")

    def get_correction(self, annotation_id: str) -> CorrectionAnnotation:
        row = self._fetchone(
            """
            SELECT annotation_id, organization_id, source_document_id, source_span_id,
                   original_text, corrected_text, reason, created_by, status,
                   created_at, reviewed_by, reviewed_at, review_note
            FROM ingestion.correction_annotations WHERE annotation_id = %s
            """,
            (annotation_id,),
        )
        if row is None:
            raise NotFoundError(f"correction {annotation_id!r} not found")
        return CorrectionAnnotation(
            row[0], row[1], row[2], row[3], row[4], row[5], row[6], row[7],
            CorrectionStatus(row[8]), row[9], row[10], row[11], row[12]
        )

    def save_reviewed_correction(self, annotation: CorrectionAnnotation) -> None:
        row = self._fetchone(
            """
            UPDATE ingestion.correction_annotations
            SET status = %s, reviewed_by = %s, reviewed_at = %s, review_note = %s
            WHERE annotation_id = %s AND status = 'proposed'
            RETURNING annotation_id
            """,
            (
                annotation.status.value,
                annotation.reviewed_by,
                annotation.reviewed_at,
                annotation.review_note,
                annotation.annotation_id,
            ),
        )
        if row is None:
            raise ConflictError("correction annotation was already reviewed or does not exist")

    def add_amendment_link(self, link: AmendmentLink) -> None:
        row = self._fetchone(
            """
            INSERT INTO ingestion.amendment_links (
                amendment_link_id, amending_document_version_id,
                amended_document_version_id, source_span_id, relation_type,
                effective_from, created_by, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (amendment_link_id) DO NOTHING RETURNING amendment_link_id
            """,
            (
                link.amendment_link_id,
                link.amending_document_version_id,
                link.amended_document_version_id,
                link.source_span_id,
                link.relation_type.value,
                link.effective_from,
                link.created_by,
                link.created_at,
            ),
        )
        if row is None:
            existing = self._amendment_by_id(link.amendment_link_id)
            if existing != link:
                raise ConflictError(f"amendment link {link.amendment_link_id!r} already exists")

    def amendment_links_for(self, document_version_id: str) -> tuple[AmendmentLink, ...]:
        rows = self._fetchall(
            """
            SELECT amendment_link_id, amending_document_version_id,
                   amended_document_version_id, relation_type, created_by,
                   effective_from, source_span_id, created_at
            FROM ingestion.amendment_links
            WHERE amending_document_version_id = %s OR amended_document_version_id = %s
            ORDER BY effective_from NULLS FIRST, amendment_link_id
            """,
            (document_version_id, document_version_id),
        )
        return tuple(self._amendment(row) for row in rows)

    def _amendment_by_id(self, amendment_link_id: str) -> AmendmentLink:
        row = self._fetchone(
            """
            SELECT amendment_link_id, amending_document_version_id,
                   amended_document_version_id, relation_type, created_by,
                   effective_from, source_span_id, created_at
            FROM ingestion.amendment_links WHERE amendment_link_id = %s
            """,
            (amendment_link_id,),
        )
        if row is None:
            raise NotFoundError(f"amendment link {amendment_link_id!r} not found")
        return self._amendment(row)

    def _fetchone(self, sql: str, parameters: Sequence[object]) -> tuple[Any, ...] | None:
        cursor = self.connection.cursor()
        try:
            cursor.execute(sql, tuple(parameters))
            return cursor.fetchone()
        finally:
            cursor.close()

    def _fetchall(self, sql: str, parameters: Sequence[object]) -> list[tuple[Any, ...]]:
        cursor = self.connection.cursor()
        try:
            cursor.execute(sql, tuple(parameters))
            return list(cursor.fetchall())
        finally:
            cursor.close()

    @staticmethod
    def _json(value: Mapping[str, object] | None) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def _json_object(value: object) -> Mapping[str, object]:
        if isinstance(value, str):
            decoded = json.loads(value)
            return decoded if isinstance(decoded, dict) else {}
        return value if isinstance(value, dict) else {}

    @classmethod
    def _job(cls, row: Sequence[Any]) -> IngestionJob:
        return IngestionJob(
            row[0], row[1], row[2], row[3], row[4], row[5],
            IngestionJobStatus(row[6]), row[7], row[8], row[9], row[10],
            row[11], row[12], cls._json_object(row[13]),
            cls._json_object(row[14]) if row[14] is not None else None,
            row[15], row[16]
        )

    @staticmethod
    def _amendment(row: Sequence[Any]) -> AmendmentLink:
        return AmendmentLink(
            row[0], row[1], row[2], AmendmentRelationType(row[3]), row[4],
            row[5], row[6], row[7]
        )


class PostgresIngestionWorker:
    """Claims durably, then atomically commits canonical data and job success."""

    def __init__(
        self,
        connection_factory: Callable[[], Any],
        loader: SourceLoader,
        parser: DocumentParser,
        *,
        lock_timeout: timedelta = timedelta(minutes=15),
        retry_base: timedelta = timedelta(seconds=30),
    ) -> None:
        self.connection_factory = connection_factory
        self.loader = loader
        self.parser = parser
        self.lock_timeout = lock_timeout
        self.retry_base = retry_base

    def run_once(self, worker_id: str, now: datetime) -> WorkerOutcome:
        claim_connection = self.connection_factory()
        try:
            job = PostgresIngestionRepository(claim_connection).claim_next(
                worker_id, now, now - self.lock_timeout
            )
            claim_connection.commit()
        except Exception:
            claim_connection.rollback()
            raise
        finally:
            claim_connection.close()
        if job is None:
            return WorkerOutcome(None, None)

        processing_connection = None
        try:
            artifact = self.loader.load(job)
            parsed = self.parser.parse(job, artifact)
            processing_connection = self.connection_factory()
            pipeline = CanonicalIngestionPipeline(PostgresCanonicalRepository(processing_connection))
            result = pipeline.ingest(parsed)
            completed = PostgresIngestionRepository(processing_connection).mark_succeeded(
                job.job_id,
                worker_id,
                job.attempts,
                self._result_payload(result),
                now,
            )
            processing_connection.commit()
            return WorkerOutcome(completed, result)
        except Exception as exc:  # noqa: BLE001 - provider/parser failures must transition the durable job
            if processing_connection is not None:
                processing_connection.rollback()
            error = f"{type(exc).__name__}: {exc}"[:2000]
            failure_connection = self.connection_factory()
            try:
                failed = PostgresIngestionRepository(failure_connection).mark_failed(
                    job.job_id,
                    worker_id,
                    job.attempts,
                    error,
                    now + self.retry_base * (2 ** (job.attempts - 1)),
                    now,
                )
                failure_connection.commit()
            except Exception:
                failure_connection.rollback()
                raise
            finally:
                failure_connection.close()
            return WorkerOutcome(failed, None, error)
        finally:
            if processing_connection is not None:
                processing_connection.close()

    @staticmethod
    def _result_payload(result: Any) -> dict[str, object]:
        return {
            "source_document_id": result.source_document_id,
            "instrument_id": result.instrument_id,
            "document_version_id": result.document_version_id,
            "provision_ids": list(result.provision_ids),
            "provision_version_ids": list(result.provision_version_ids),
            "source_span_ids": list(result.source_span_ids),
        }
