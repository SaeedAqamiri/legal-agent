import unittest
from datetime import UTC, date, datetime, timedelta

from legal_agent_core.errors import ConflictError
from legal_agent_core.in_memory import InMemoryCanonicalRepository
from legal_agent_core.ingestion import (
    AmendmentLink,
    AmendmentRelationType,
    CanonicalIngestionPipeline,
    CorrectionAnnotation,
    CorrectionStatus,
    IngestionJob,
    IngestionJobStatus,
    IngestionWorker,
    OCRDocumentParser,
    OCRResult,
    SourceArtifact,
)
from legal_agent_core.ingestion.operations import (
    InMemoryIngestionJobRepository,
    InMemoryIngestionOperationsRepository,
)
from tests.test_ingestion import parsed_document

NOW = datetime(2026, 8, 11, 14, 0, tzinfo=UTC)


def job(*, max_attempts: int = 3, idempotency_key: str = "source-v1") -> IngestionJob:
    return IngestionJob(
        "job-1",
        "org-1",
        idempotency_key,
        "s3://legal/source.pdf",
        "test-parser",
        "1.0",
        max_attempts=max_attempts,
        available_at=NOW,
        created_at=NOW,
        updated_at=NOW,
        payload={"language": "fa"},
    )


class StaticLoader:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    def load(self, selected_job):
        if self.error:
            raise self.error
        return SourceArtifact(selected_job.source_uri, b"pdf", "application/pdf", "sha256:abc")


class StaticParser:
    def parse(self, selected_job, artifact):
        return parsed_document()


class FakeOCR:
    def extract(self, artifact):
        return OCRResult("متن OCR", "fake-ocr", "1", 0.9)


class RecordingMapper:
    def __init__(self) -> None:
        self.ocr = None

    def map(self, selected_job, artifact, ocr):
        self.ocr = ocr
        return parsed_document()


class IngestionJobTests(unittest.TestCase):
    def test_enqueue_is_idempotent_but_rejects_reused_key_with_other_input(self) -> None:
        repository = InMemoryIngestionJobRepository()

        self.assertEqual(repository.enqueue(job()), repository.enqueue(job()))
        conflicting = IngestionJob(
            "job-2",
            "org-1",
            "source-v1",
            "s3://legal/other.pdf",
            "test-parser",
            "1.0",
            available_at=NOW,
        )
        with self.assertRaises(ConflictError):
            repository.enqueue(conflicting)

    def test_worker_ingests_and_records_structured_result(self) -> None:
        jobs = InMemoryIngestionJobRepository()
        jobs.enqueue(job())
        canonical = InMemoryCanonicalRepository()
        worker = IngestionWorker(
            jobs,
            StaticLoader(),
            StaticParser(),
            CanonicalIngestionPipeline(canonical),
        )

        outcome = worker.run_once("worker-1", NOW)

        self.assertEqual(outcome.job.status, IngestionJobStatus.SUCCEEDED)
        self.assertEqual(outcome.job.attempts, 1)
        self.assertEqual(outcome.job.result["document_version_id"], outcome.ingestion_result.document_version_id)
        self.assertEqual(len(canonical.provisions), 3)

    def test_failure_retries_then_moves_to_dead_letter(self) -> None:
        jobs = InMemoryIngestionJobRepository()
        jobs.enqueue(job(max_attempts=2))
        worker = IngestionWorker(
            jobs,
            StaticLoader(RuntimeError("OCR unavailable")),
            StaticParser(),
            CanonicalIngestionPipeline(InMemoryCanonicalRepository()),
            retry_base=timedelta(seconds=10),
        )

        first = worker.run_once("worker-1", NOW)
        second = worker.run_once("worker-1", NOW + timedelta(seconds=11))

        self.assertEqual(first.job.status, IngestionJobStatus.FAILED)
        self.assertEqual(first.job.available_at, NOW + timedelta(seconds=10))
        self.assertEqual(second.job.status, IngestionJobStatus.DEAD_LETTER)
        self.assertEqual(second.job.attempts, 2)
        self.assertIn("OCR unavailable", second.error)

    def test_stale_lock_is_reclaimed_and_old_worker_is_fenced(self) -> None:
        jobs = InMemoryIngestionJobRepository()
        jobs.enqueue(job())
        first = jobs.claim_next("worker-old", NOW, NOW - timedelta(minutes=15))
        second = jobs.claim_next(
            "worker-new",
            NOW + timedelta(minutes=20),
            NOW + timedelta(minutes=5),
        )

        self.assertEqual(second.attempts, 2)
        with self.assertRaises(ConflictError):
            jobs.mark_succeeded("job-1", "worker-old", first.attempts, {"ok": True}, NOW)

    def test_ocr_adapter_is_composed_without_binding_pipeline_to_vendor(self) -> None:
        mapper = RecordingMapper()
        parser = OCRDocumentParser(FakeOCR(), mapper)

        parsed = parser.parse(job(), SourceArtifact("file:///x", b"x", "application/pdf", "sha256:x"))

        self.assertEqual(parsed.instrument.external_identifier, "IR-EXAMPLE-LAW")
        self.assertEqual(mapper.ocr.engine, "fake-ocr")


class CorrectionAndAmendmentTests(unittest.TestCase):
    def test_correction_is_reviewed_without_overwriting_canonical_text(self) -> None:
        repository = InMemoryIngestionOperationsRepository()
        correction = CorrectionAnnotation(
            "correction-1",
            "org-1",
            "source-1",
            "span-1",
            "متن خام",
            "متن صحیح",
            "خطای OCR",
            "reviewer-requester",
            created_at=NOW,
        )
        repository.add_correction(correction)

        reviewed = correction.review(CorrectionStatus.APPROVED, "expert-1", NOW, "تطبیق با تصویر")
        repository.save_reviewed_correction(reviewed)

        self.assertEqual(repository.get_correction("correction-1").status, CorrectionStatus.APPROVED)
        with self.assertRaises(ConflictError):
            repository.save_reviewed_correction(reviewed)

    def test_amendment_links_are_versioned_and_queryable_from_both_sides(self) -> None:
        repository = InMemoryIngestionOperationsRepository()
        link = AmendmentLink(
            "amendment-1",
            "document-amending",
            "document-old",
            AmendmentRelationType.AMENDS,
            "parser-1",
            date(2026, 1, 1),
            "span-amendment",
            NOW,
        )
        repository.add_amendment_link(link)

        self.assertEqual(repository.amendment_links_for("document-old"), (link,))
        self.assertEqual(repository.amendment_links_for("document-amending"), (link,))


if __name__ == "__main__":
    unittest.main()
