import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from legal_agent_core.db import MigrationError, apply_migrations, load_migrations


class FakeCursor:
    def __init__(self, applied_rows: list[tuple[int, str, str]] | None = None) -> None:
        self.applied_rows = applied_rows or []
        self.executed: list[tuple[str, tuple[object, ...] | None]] = []
        self.closed = False

    def execute(self, sql: str, parameters: tuple[object, ...] | None = None) -> None:
        self.executed.append((sql, parameters))

    def fetchall(self) -> list[tuple[int, str, str]]:
        return self.applied_rows

    def close(self) -> None:
        self.closed = True


class FakeConnection:
    def __init__(self, applied_rows: list[tuple[int, str, str]] | None = None) -> None:
        self.fake_cursor = FakeCursor(applied_rows)
        self.commit_count = 0
        self.rollback_count = 0

    def cursor(self) -> FakeCursor:
        return self.fake_cursor

    def commit(self) -> None:
        self.commit_count += 1

    def rollback(self) -> None:
        self.rollback_count += 1


class MigrationTests(unittest.TestCase):
    def test_loads_versioned_migration_with_stable_checksum(self) -> None:
        migrations = load_migrations()

        self.assertEqual([item.version for item in migrations], [1, 2, 3, 4])
        self.assertEqual(migrations[0].name, "canonical_and_research_graph")
        self.assertEqual(len(migrations[0].checksum), 64)

    def test_schema_contains_adr_boundaries_and_invariants(self) -> None:
        sql = load_migrations()[0].sql.lower()

        required_fragments = (
            "create schema if not exists canonical",
            "create schema if not exists research",
            "effective_to is null or effective_from is null or effective_to >= effective_from",
            "canonical.provision_versions",
            "canonical.graph_edges",
            "research.progressive_relations",
            "research.memory_events",
            "foreign key (organization_id, source_episode_id)",
            "enable row level security",
            "create policy tenant_isolation",
            "deferrable initially deferred",
            "interpretive relation % requires versioned evidence",
            "provision text is immutable",
            "audit history is append-only",
        )
        for fragment in required_fragments:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, sql)

    def test_runner_applies_pending_migration_and_records_checksum(self) -> None:
        connection = FakeConnection()

        applied = apply_migrations(connection)

        self.assertEqual(applied, (1, 2, 3, 4))
        self.assertEqual(connection.commit_count, 1)
        self.assertEqual(connection.rollback_count, 0)
        insert_calls = [call for call in connection.fake_cursor.executed if "insert into" in call[0].lower()]
        self.assertEqual(insert_calls[0][1][0:2], (1, "canonical_and_research_graph"))
        self.assertTrue(connection.fake_cursor.closed)

    def test_ingestion_operations_schema_has_retry_and_review_invariants(self) -> None:
        migration = load_migrations()[1]
        sql = migration.sql.lower()

        self.assertEqual(migration.name, "ingestion_operations")
        for fragment in (
            "create schema if not exists ingestion",
            "create table ingestion.jobs",
            "dead_letter",
            "create unique index uq_ingestion_job_idempotency",
            "create table ingestion.correction_annotations",
            "original_text <> corrected_text",
            "create table ingestion.amendment_links",
            "amending_document_version_id <> amended_document_version_id",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, sql)

    def test_runner_is_idempotent_for_applied_migration(self) -> None:
        migration = load_migrations()[0]
        connection = FakeConnection([(migration.version, migration.name, migration.checksum)])

        applied = apply_migrations(connection, (migration,))

        self.assertEqual(applied, ())
        self.assertEqual(connection.commit_count, 1)
        self.assertFalse(any("insert into" in sql.lower() for sql, _ in connection.fake_cursor.executed))

    def test_runner_rolls_back_on_checksum_drift(self) -> None:
        migration = load_migrations()[0]
        connection = FakeConnection([(migration.version, migration.name, "0" * 64)])

        with self.assertRaisesRegex(MigrationError, "drift"):
            apply_migrations(connection, (migration,))

        self.assertEqual(connection.commit_count, 0)
        self.assertEqual(connection.rollback_count, 1)

    def test_rejects_non_contiguous_migration_versions(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "V0001__first.sql").write_text("SELECT 1;", encoding="utf-8")
            (root / "V0003__third.sql").write_text("SELECT 3;", encoding="utf-8")

            with self.assertRaisesRegex(MigrationError, "contiguous"):
                load_migrations(root)


if __name__ == "__main__":
    unittest.main()
