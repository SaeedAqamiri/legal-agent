import os
import unittest
import uuid
from dataclasses import replace
from datetime import UTC, datetime

from legal_agent_core.adapters import (
    PostgresCanonicalRepository,
    PostgresIngestionRepository,
)
from legal_agent_core.db import apply_migrations
from legal_agent_core.ingestion import CanonicalIngestionPipeline, IngestionJob
from tests.test_ingestion import parsed_document


@unittest.skipUnless(
    os.environ.get("LEGAL_AGENT_POSTGRES_DSN"),
    "set LEGAL_AGENT_POSTGRES_DSN to run the live PostgreSQL integration test",
)
class LivePostgresTests(unittest.TestCase):
    def test_migration_canonical_round_trip_and_job_claim(self) -> None:
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - environment dependent
            self.skipTest(f"psycopg unavailable: {exc}")
        connection = psycopg.connect(os.environ["LEGAL_AGENT_POSTGRES_DSN"])
        apply_migrations(connection)
        suffix = uuid.uuid4().hex
        parsed = parsed_document(checksum=f"sha256:{suffix}")
        parsed = replace(
            parsed,
            source=replace(parsed.source, source_document_id=f"source-{suffix}"),
            instrument=replace(
                parsed.instrument,
                instrument_id=f"instrument-{suffix}",
                external_identifier=f"external-{suffix}",
            ),
            version=replace(parsed.version, document_version_id=f"document-{suffix}"),
        )
        now = datetime.now(UTC)
        try:
            repository = PostgresCanonicalRepository(connection)
            result = CanonicalIngestionPipeline(repository).ingest(parsed)
            self.assertEqual(
                repository.get_document_version(result.document_version_id).instrument_id,
                result.instrument_id,
            )
            jobs = PostgresIngestionRepository(connection)
            jobs.enqueue(
                IngestionJob(
                    f"job-{suffix}",
                    "integration",
                    f"key-{suffix}",
                    parsed.source.source_uri,
                    "integration-parser",
                    "1",
                    available_at=now,
                )
            )
            claimed = jobs.claim_next("integration-worker", now, now)
            self.assertEqual(claimed.job_id, f"job-{suffix}")
        finally:
            connection.rollback()
            connection.close()


if __name__ == "__main__":
    unittest.main()
