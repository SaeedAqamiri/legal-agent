from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Protocol

from ..canonical import require_text
from ..errors import ConflictError, DomainError, NotFoundError
from .models import IngestionResult, ParsedDocument


class IngestionJobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"


class CorrectionStatus(StrEnum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"


class AmendmentRelationType(StrEnum):
    AMENDS = "amends"
    REPEALS = "repeals"
    REPLACES = "replaces"
    SUSPENDS = "suspends"
    RESTORES = "restores"


@dataclass(frozen=True, slots=True)
class IngestionJob:
    job_id: str
    organization_id: str | None
    idempotency_key: str
    source_uri: str
    parser_name: str
    parser_version: str
    status: IngestionJobStatus = IngestionJobStatus.QUEUED
    attempts: int = 0
    max_attempts: int = 3
    available_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    locked_at: datetime | None = None
    locked_by: str | None = None
    last_error: str | None = None
    payload: Mapping[str, object] = field(default_factory=dict)
    result: Mapping[str, object] | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        for field_name in ("job_id", "idempotency_key", "source_uri", "parser_name", "parser_version"):
            require_text(getattr(self, field_name), field_name)
        if self.max_attempts < 1 or not 0 <= self.attempts <= self.max_attempts:
            raise DomainError("ingestion attempts must be between zero and max_attempts")
        locked = self.locked_at is not None and self.locked_by is not None
        if (self.status == IngestionJobStatus.RUNNING) != locked:
            raise DomainError("only running ingestion jobs may hold a complete lock")
        if (self.locked_at is None) != (self.locked_by is None):
            raise DomainError("locked_at and locked_by must be supplied together")
        if self.status == IngestionJobStatus.SUCCEEDED and self.result is None:
            raise DomainError("successful ingestion job requires result metadata")


@dataclass(frozen=True, slots=True)
class SourceArtifact:
    source_uri: str
    content: bytes
    media_type: str
    checksum: str
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_text(self.source_uri, "source_uri")
        require_text(self.media_type, "media_type")
        require_text(self.checksum, "checksum")
        if not self.content:
            raise DomainError("source artifact content must not be empty")


@dataclass(frozen=True, slots=True)
class OCRResult:
    text: str
    engine: str
    engine_version: str
    confidence: float | None = None

    def __post_init__(self) -> None:
        require_text(self.text, "OCR text")
        require_text(self.engine, "OCR engine")
        require_text(self.engine_version, "OCR engine_version")
        if self.confidence is not None and not 0 <= self.confidence <= 1:
            raise DomainError("OCR confidence must be between zero and one")


class SourceLoader(Protocol):
    def load(self, job: IngestionJob) -> SourceArtifact: ...


class DocumentParser(Protocol):
    def parse(self, job: IngestionJob, artifact: SourceArtifact) -> ParsedDocument: ...


class OCRAdapter(Protocol):
    def extract(self, artifact: SourceArtifact) -> OCRResult: ...


class OCRTextMapper(Protocol):
    def map(self, job: IngestionJob, artifact: SourceArtifact, ocr: OCRResult) -> ParsedDocument: ...


class OCRDocumentParser:
    def __init__(self, ocr: OCRAdapter, mapper: OCRTextMapper) -> None:
        self.ocr = ocr
        self.mapper = mapper

    def parse(self, job: IngestionJob, artifact: SourceArtifact) -> ParsedDocument:
        return self.mapper.map(job, artifact, self.ocr.extract(artifact))


class IngestionJobRepository(Protocol):
    def enqueue(self, job: IngestionJob) -> IngestionJob: ...

    def get(self, job_id: str) -> IngestionJob: ...

    def claim_next(self, worker_id: str, now: datetime, stale_before: datetime) -> IngestionJob | None: ...

    def mark_succeeded(
        self,
        job_id: str,
        worker_id: str,
        attempt: int,
        result: Mapping[str, object],
        now: datetime,
    ) -> IngestionJob: ...

    def mark_failed(
        self,
        job_id: str,
        worker_id: str,
        attempt: int,
        error: str,
        retry_at: datetime,
        now: datetime,
    ) -> IngestionJob: ...


class InMemoryIngestionJobRepository:
    def __init__(self) -> None:
        self.jobs: dict[str, IngestionJob] = {}
        self.idempotency: dict[tuple[str | None, str], str] = {}

    def enqueue(self, job: IngestionJob) -> IngestionJob:
        key = (job.organization_id, job.idempotency_key)
        existing_id = self.idempotency.get(key)
        if existing_id is not None:
            existing = self.jobs[existing_id]
            identity = (existing.source_uri, existing.parser_name, existing.parser_version, existing.payload)
            requested = (job.source_uri, job.parser_name, job.parser_version, job.payload)
            if identity != requested:
                raise ConflictError("ingestion idempotency key was reused with different input")
            return existing
        if job.job_id in self.jobs and self.jobs[job.job_id] != job:
            raise ConflictError(f"ingestion job {job.job_id!r} already exists")
        self.jobs[job.job_id] = job
        self.idempotency[key] = job.job_id
        return job

    def get(self, job_id: str) -> IngestionJob:
        try:
            return self.jobs[job_id]
        except KeyError as exc:
            raise NotFoundError(f"ingestion job {job_id!r} not found") from exc

    def claim_next(self, worker_id: str, now: datetime, stale_before: datetime) -> IngestionJob | None:
        require_text(worker_id, "worker_id")
        candidates = [
            job
            for job in self.jobs.values()
            if job.attempts < job.max_attempts
            and (
                (
                    job.status in {IngestionJobStatus.QUEUED, IngestionJobStatus.FAILED}
                    and job.available_at <= now
                )
                or (
                    job.status == IngestionJobStatus.RUNNING
                    and job.locked_at is not None
                    and job.locked_at <= stale_before
                )
            )
        ]
        if not candidates:
            return None
        selected = min(candidates, key=lambda item: (item.available_at, item.created_at, item.job_id))
        claimed = replace(
            selected,
            status=IngestionJobStatus.RUNNING,
            attempts=selected.attempts + 1,
            locked_at=now,
            locked_by=worker_id,
            updated_at=now,
        )
        self.jobs[claimed.job_id] = claimed
        return claimed

    def mark_succeeded(
        self,
        job_id: str,
        worker_id: str,
        attempt: int,
        result: Mapping[str, object],
        now: datetime,
    ) -> IngestionJob:
        job = self._owned_running(job_id, worker_id, attempt)
        updated = replace(
            job,
            status=IngestionJobStatus.SUCCEEDED,
            locked_at=None,
            locked_by=None,
            last_error=None,
            result=dict(result),
            updated_at=now,
        )
        self.jobs[job_id] = updated
        return updated

    def mark_failed(
        self,
        job_id: str,
        worker_id: str,
        attempt: int,
        error: str,
        retry_at: datetime,
        now: datetime,
    ) -> IngestionJob:
        job = self._owned_running(job_id, worker_id, attempt)
        terminal = job.attempts >= job.max_attempts
        updated = replace(
            job,
            status=IngestionJobStatus.DEAD_LETTER if terminal else IngestionJobStatus.FAILED,
            available_at=retry_at,
            locked_at=None,
            locked_by=None,
            last_error=error[:2000],
            updated_at=now,
        )
        self.jobs[job_id] = updated
        return updated

    def _owned_running(self, job_id: str, worker_id: str, attempt: int) -> IngestionJob:
        job = self.get(job_id)
        if (
            job.status != IngestionJobStatus.RUNNING
            or job.locked_by != worker_id
            or job.attempts != attempt
        ):
            raise ConflictError("ingestion job lock was lost or attempt is stale")
        return job


@dataclass(frozen=True, slots=True)
class CorrectionAnnotation:
    annotation_id: str
    organization_id: str | None
    source_document_id: str
    source_span_id: str
    original_text: str
    corrected_text: str
    reason: str
    created_by: str
    status: CorrectionStatus = CorrectionStatus.PROPOSED
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    review_note: str | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "annotation_id",
            "source_document_id",
            "source_span_id",
            "original_text",
            "corrected_text",
            "reason",
            "created_by",
        ):
            require_text(getattr(self, field_name), field_name)
        if self.original_text == self.corrected_text:
            raise DomainError("correction must change the source text")
        reviewed = self.reviewed_by is not None and self.reviewed_at is not None
        if (self.status != CorrectionStatus.PROPOSED) != reviewed:
            raise DomainError("reviewed correction status requires reviewer and timestamp")

    def review(
        self,
        status: CorrectionStatus,
        reviewer: str,
        reviewed_at: datetime,
        note: str | None = None,
    ) -> CorrectionAnnotation:
        if self.status != CorrectionStatus.PROPOSED:
            raise ConflictError("correction annotation was already reviewed")
        if status not in {CorrectionStatus.APPROVED, CorrectionStatus.REJECTED}:
            raise DomainError("review must approve or reject a correction")
        require_text(reviewer, "reviewer")
        return replace(
            self,
            status=status,
            reviewed_by=reviewer,
            reviewed_at=reviewed_at,
            review_note=note,
        )


@dataclass(frozen=True, slots=True)
class AmendmentLink:
    amendment_link_id: str
    amending_document_version_id: str
    amended_document_version_id: str
    relation_type: AmendmentRelationType
    created_by: str
    effective_from: date | None = None
    source_span_id: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        for field_name in (
            "amendment_link_id",
            "amending_document_version_id",
            "amended_document_version_id",
            "created_by",
        ):
            require_text(getattr(self, field_name), field_name)
        if self.amending_document_version_id == self.amended_document_version_id:
            raise DomainError("a document version cannot amend itself")


class IngestionOperationsRepository(Protocol):
    def add_correction(self, annotation: CorrectionAnnotation) -> None: ...

    def get_correction(self, annotation_id: str) -> CorrectionAnnotation: ...

    def save_reviewed_correction(self, annotation: CorrectionAnnotation) -> None: ...

    def add_amendment_link(self, link: AmendmentLink) -> None: ...

    def amendment_links_for(self, document_version_id: str) -> tuple[AmendmentLink, ...]: ...


class InMemoryIngestionOperationsRepository:
    def __init__(self) -> None:
        self.corrections: dict[str, CorrectionAnnotation] = {}
        self.amendments: dict[str, AmendmentLink] = {}

    def add_correction(self, annotation: CorrectionAnnotation) -> None:
        existing = self.corrections.get(annotation.annotation_id)
        if existing is not None and existing != annotation:
            raise ConflictError(f"correction {annotation.annotation_id!r} already exists")
        self.corrections[annotation.annotation_id] = annotation

    def get_correction(self, annotation_id: str) -> CorrectionAnnotation:
        try:
            return self.corrections[annotation_id]
        except KeyError as exc:
            raise NotFoundError(f"correction {annotation_id!r} not found") from exc

    def save_reviewed_correction(self, annotation: CorrectionAnnotation) -> None:
        existing = self.get_correction(annotation.annotation_id)
        if existing.status != CorrectionStatus.PROPOSED:
            raise ConflictError("correction annotation was already reviewed")
        if annotation.status == CorrectionStatus.PROPOSED:
            raise DomainError("reviewed correction cannot remain proposed")
        self.corrections[annotation.annotation_id] = annotation

    def add_amendment_link(self, link: AmendmentLink) -> None:
        existing = self.amendments.get(link.amendment_link_id)
        if existing is not None and existing != link:
            raise ConflictError(f"amendment link {link.amendment_link_id!r} already exists")
        self.amendments[link.amendment_link_id] = link

    def amendment_links_for(self, document_version_id: str) -> tuple[AmendmentLink, ...]:
        return tuple(
            sorted(
                (
                    item
                    for item in self.amendments.values()
                    if document_version_id
                    in {item.amending_document_version_id, item.amended_document_version_id}
                ),
                key=lambda item: (item.effective_from or date.min, item.amendment_link_id),
            )
        )


@dataclass(frozen=True, slots=True)
class WorkerOutcome:
    job: IngestionJob | None
    ingestion_result: IngestionResult | None
    error: str | None = None


class IngestionWorker:
    """Provider-neutral worker. Production DB transactions are supplied by repositories/UoW."""

    def __init__(
        self,
        jobs: IngestionJobRepository,
        loader: SourceLoader,
        parser: DocumentParser,
        pipeline: object,
        *,
        lock_timeout: timedelta = timedelta(minutes=15),
        retry_base: timedelta = timedelta(seconds=30),
    ) -> None:
        self.jobs = jobs
        self.loader = loader
        self.parser = parser
        self.pipeline = pipeline
        self.lock_timeout = lock_timeout
        self.retry_base = retry_base

    def run_once(self, worker_id: str, now: datetime | None = None) -> WorkerOutcome:
        current = now or datetime.now(UTC)
        job = self.jobs.claim_next(worker_id, current, current - self.lock_timeout)
        if job is None:
            return WorkerOutcome(None, None)
        try:
            artifact = self.loader.load(job)
            parsed = self.parser.parse(job, artifact)
            result = self.pipeline.ingest(parsed)
            completed = self.jobs.mark_succeeded(
                job.job_id,
                worker_id,
                job.attempts,
                self._result_payload(result),
                current,
            )
            return WorkerOutcome(completed, result)
        except Exception as exc:  # noqa: BLE001 - arbitrary adapter failures are retryable job outcomes
            delay = self.retry_base * (2 ** (job.attempts - 1))
            error = f"{type(exc).__name__}: {exc}"[:2000]
            failed = self.jobs.mark_failed(
                job.job_id,
                worker_id,
                job.attempts,
                error,
                current + delay,
                current,
            )
            return WorkerOutcome(failed, None, error)

    @staticmethod
    def _result_payload(result: IngestionResult) -> dict[str, object]:
        return {
            "source_document_id": result.source_document_id,
            "instrument_id": result.instrument_id,
            "document_version_id": result.document_version_id,
            "provision_ids": list(result.provision_ids),
            "provision_version_ids": list(result.provision_version_ids),
            "source_span_ids": list(result.source_span_ids),
        }
