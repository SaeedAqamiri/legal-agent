import os
import unittest
import uuid
from dataclasses import replace
from datetime import UTC, datetime

from legal_agent_core.adapters import (
    PostgresCanonicalRepository,
    PostgresIngestionRepository,
    PostgresResearchHistoryStore,
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

    def test_research_history_persists_and_is_tenant_isolated(self) -> None:
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - environment dependent
            self.skipTest(f"psycopg unavailable: {exc}")
        connection = psycopg.connect(os.environ["LEGAL_AGENT_POSTGRES_DSN"])
        apply_migrations(connection)
        suffix = uuid.uuid4().hex
        org = f"org-{suffix}"
        try:
            store = PostgresResearchHistoryStore(connection)
            connection.commit()
            store.save(
                org,
                f"episode-{suffix}",
                {
                    "organization_id": org,
                    "episode_id": f"episode-{suffix}",
                    "conversation_id": f"conversation-{suffix}",
                    "user_id": "researcher-1",
                    "completed": True,
                    "iterations": 2,
                    "trace": {
                        "created_at": "2026-08-19T10:00:00+00:00",
                        "question": "مهلت اعتراض چقدر است؟",
                        "applicable_time": "2026-08-11",
                    },
                    "answer": {"answer_id": "answer-x"},
                },
            )
            connection.commit()
            stored = store.get(org, f"episode-{suffix}")
            self.assertEqual(stored["answer"]["answer_id"], "answer-x")
            self.assertEqual(len(store.list(org)), 1)
            other = store.list(f"org-ghost-{suffix}")
            self.assertEqual(len(other), 0)
            # RLS forbids writing another tenant while scoped to OrgA reference.
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT set_config('legal_agent.organization_id', 'org-a', true)"
                )
            with self.assertRaises(psycopg.errors.InsufficientPrivilege), connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO research_history.episodes (
                        organization_id, episode_id, conversation_id, user_id,
                        question, applicable_time, created_at, completed,
                        iterations, has_answer, payload
                    ) VALUES ('org-b', %s, 'c', 'u', 'q', '2026-08-11', now(), true, 1, false, '{}')
                    """,
                        (f"episode-{suffix}-sneak",),
                    )
        finally:
            connection.rollback()
            connection.close()


if __name__ == "__main__":
    unittest.main()
