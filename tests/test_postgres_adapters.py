import unittest
from datetime import UTC, date, datetime

from legal_agent_core.adapters.postgres import (
    PostgresCanonicalRepository,
    PostgresUnitOfWork,
)
from legal_agent_core.adapters.postgres_ingestion import (
    PostgresIngestionRepository,
    PostgresIngestionWorker,
)
from legal_agent_core.canonical import BoundingBox, SourceDocument
from legal_agent_core.errors import ConflictError
from legal_agent_core.ingestion import IngestionJobStatus

NOW = datetime(2026, 8, 11, 15, 0, tzinfo=UTC)


class ScriptedCursor:
    def __init__(self, connection, outcome) -> None:
        self.connection = connection
        self.outcome = outcome
        self.closed = False

    def execute(self, sql, parameters=()) -> None:
        self.connection.executed.append((sql, parameters))

    def fetchone(self):
        return self.outcome.get("one")

    def fetchall(self):
        return self.outcome.get("all", [])

    def close(self) -> None:
        self.closed = True


class ScriptedConnection:
    def __init__(self, *outcomes) -> None:
        self.outcomes = list(outcomes)
        self.executed = []
        self.commit_count = 0
        self.rollback_count = 0
        self.close_count = 0

    def cursor(self):
        outcome = self.outcomes.pop(0) if self.outcomes else {}
        return ScriptedCursor(self, outcome)

    def commit(self):
        self.commit_count += 1

    def rollback(self):
        self.rollback_count += 1

    def close(self):
        self.close_count += 1


def source_document(filename: str = "law.pdf") -> SourceDocument:
    return SourceDocument(
        "source-1",
        "org-1",
        filename,
        "application/pdf",
        "sha256:source",
        200,
        "test",
        "s3://legal/law.pdf",
        NOW,
    )


def job_row(status="running", attempts=1, result=None):
    return (
        "job-1",
        "org-1",
        "source-v1",
        "s3://legal/law.pdf",
        "parser",
        "1",
        status,
        attempts,
        3,
        NOW,
        NOW if status == "running" else None,
        "worker-1" if status == "running" else None,
        None,
        '{"language": "fa"}',
        result,
        NOW,
        NOW,
    )


class PostgresCanonicalRepositoryTests(unittest.TestCase):
    def test_insert_is_parameterized_and_read_after_write_decodes_domain(self) -> None:
        connection = ScriptedConnection(
            {"one": ("source-1",)},
            {
                "one": (
                    "source-1",
                    "org-1",
                    "law.pdf",
                    "application/pdf",
                    "sha256:source",
                    200,
                    "test",
                    "s3://legal/law.pdf",
                    NOW,
                )
            },
        )
        repository = PostgresCanonicalRepository(connection)

        repository.add_source_document(source_document())
        loaded = repository.get_source_document("source-1")

        insert_sql, insert_params = connection.executed[0]
        self.assertIn("ON CONFLICT (source_document_id) DO NOTHING", insert_sql)
        self.assertNotIn("law.pdf", insert_sql)
        self.assertEqual(insert_params[2], "law.pdf")
        self.assertEqual(loaded, source_document())

    def test_immutable_identity_conflict_is_not_silently_ignored(self) -> None:
        connection = ScriptedConnection(
            {"one": None},
            {
                "one": (
                    "source-1",
                    "org-1",
                    "other.pdf",
                    "application/pdf",
                    "sha256:source",
                    200,
                    "test",
                    "s3://legal/law.pdf",
                    NOW,
                )
            },
        )

        with self.assertRaises(ConflictError):
            PostgresCanonicalRepository(connection).add_source_document(source_document())

    def test_decodes_source_span_with_bounding_box(self) -> None:
        connection = ScriptedConnection(
            {
                "one": (
                    "span-1",
                    "source-1",
                    "document-1",
                    "provision-version-1",
                    3,
                    "متن خام",
                    0.1,
                    0.2,
                    0.8,
                    0.9,
                    10,
                    20,
                )
            }
        )

        span = PostgresCanonicalRepository(connection).get_source_span("span-1")

        self.assertEqual(span.bbox, BoundingBox(0.1, 0.2, 0.8, 0.9))
        self.assertEqual(span.char_start, 10)
        self.assertEqual(connection.executed[0][1], ("span-1",))

    def test_temporal_query_uses_bound_date_parameters(self) -> None:
        connection = ScriptedConnection(
            {
                "all": [
                    (
                        "pv-1",
                        "provision-1",
                        "document-1",
                        "متن",
                        "متن",
                        "effective",
                        "parser",
                        date(2025, 1, 1),
                        None,
                        None,
                    )
                ]
            }
        )
        applicable_time = date(2026, 1, 1)

        result = PostgresCanonicalRepository(connection).applicable_provision_versions(
            "provision-1", applicable_time
        )

        self.assertEqual(result[0].provision_version_id, "pv-1")
        self.assertEqual(connection.executed[0][1], ("provision-1", applicable_time, applicable_time))


class PostgresIngestionRepositoryTests(unittest.TestCase):
    def test_claim_uses_skip_locked_and_attempt_fencing(self) -> None:
        connection = ScriptedConnection({"one": job_row()})

        claimed = PostgresIngestionRepository(connection).claim_next(
            "worker-1",
            NOW,
            NOW,
        )

        sql, parameters = connection.executed[0]
        self.assertIn("FOR UPDATE SKIP LOCKED", sql)
        self.assertIn("attempts = jobs.attempts + 1", sql)
        self.assertEqual(parameters[3], "worker-1")
        self.assertEqual(claimed.status, IngestionJobStatus.RUNNING)
        self.assertEqual(claimed.payload, {"language": "fa"})

    def test_success_update_is_fenced_by_worker_and_attempt(self) -> None:
        result_json = '{"document_version_id": "document-1"}'
        connection = ScriptedConnection({"one": job_row("succeeded", 1, result_json)})

        completed = PostgresIngestionRepository(connection).mark_succeeded(
            "job-1",
            "worker-1",
            1,
            {"document_version_id": "document-1"},
            NOW,
        )

        sql, parameters = connection.executed[0]
        self.assertIn("locked_by = %s AND attempts = %s", sql)
        self.assertEqual(parameters[-2:], ("worker-1", 1))
        self.assertEqual(completed.status, IngestionJobStatus.SUCCEEDED)

    def test_lost_lock_is_reported_as_conflict(self) -> None:
        connection = ScriptedConnection({"one": None})

        with self.assertRaises(ConflictError):
            PostgresIngestionRepository(connection).mark_failed(
                "job-1",
                "worker-old",
                1,
                "error",
                NOW,
                NOW,
            )


class PostgresUnitOfWorkTests(unittest.TestCase):
    def test_commits_success_and_rolls_back_failure(self) -> None:
        success = ScriptedConnection()
        with PostgresUnitOfWork(lambda: success) as unit:
            self.assertIsNotNone(unit.canonical)
        self.assertEqual(success.commit_count, 1)
        self.assertEqual(success.close_count, 1)

        failure = ScriptedConnection()
        with self.assertRaisesRegex(RuntimeError, "boom"), PostgresUnitOfWork(lambda: failure):
            raise RuntimeError("boom")
        self.assertEqual(failure.rollback_count, 1)
        self.assertEqual(failure.close_count, 1)


class PostgresIngestionWorkerTests(unittest.TestCase):
    def test_loader_failure_is_recorded_in_separate_committed_transaction(self) -> None:
        claim_connection = ScriptedConnection({"one": job_row()})
        failure_connection = ScriptedConnection({"one": job_row("failed", 1)})
        connections = iter((claim_connection, failure_connection))

        class FailingLoader:
            def load(self, selected_job):
                raise RuntimeError("object storage unavailable")

        class UnusedParser:
            def parse(self, selected_job, artifact):
                raise AssertionError("parser must not run")

        outcome = PostgresIngestionWorker(
            lambda: next(connections),
            FailingLoader(),
            UnusedParser(),
        ).run_once("worker-1", NOW)

        self.assertEqual(outcome.job.status, IngestionJobStatus.FAILED)
        self.assertIn("object storage unavailable", outcome.error)
        self.assertEqual(claim_connection.commit_count, 1)
        self.assertEqual(failure_connection.commit_count, 1)
        self.assertEqual(claim_connection.close_count, 1)
        self.assertEqual(failure_connection.close_count, 1)


if __name__ == "__main__":
    unittest.main()
