from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from hashlib import sha256
from importlib.resources import files
from pathlib import Path
from typing import Any

MIGRATION_PATTERN = re.compile(r"^V(?P<version>[0-9]{4})__(?P<name>[a-z0-9_]+)\.sql$")


class MigrationError(RuntimeError):
    """Raised when migration history is invalid or has drifted."""


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    sql: str
    checksum: str


def _migration_from_path(path: Any) -> Migration:
    match = MIGRATION_PATTERN.match(path.name)
    if match is None:
        raise MigrationError(f"invalid migration filename: {path.name}")
    sql = path.read_text(encoding="utf-8")
    return Migration(
        version=int(match.group("version")),
        name=match.group("name"),
        sql=sql,
        checksum=sha256(sql.encode("utf-8")).hexdigest(),
    )


def load_migrations(directory: str | Path | None = None) -> tuple[Migration, ...]:
    root = Path(directory) if directory is not None else files("legal_agent_core.db").joinpath("migrations")
    migrations = sorted(
        (_migration_from_path(path) for path in root.iterdir() if path.name.endswith(".sql")),
        key=lambda item: item.version,
    )
    versions = [migration.version for migration in migrations]
    if len(versions) != len(set(versions)):
        raise MigrationError("duplicate migration version")
    if versions and versions != list(range(versions[0], versions[-1] + 1)):
        raise MigrationError("migration versions must be contiguous")
    return tuple(migrations)


LEDGER_SQL = """
CREATE TABLE IF NOT EXISTS public.legal_agent_schema_migrations (
    version integer PRIMARY KEY,
    name text NOT NULL,
    checksum char(64) NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
)
"""


def apply_migrations(connection: Any, migrations: Iterable[Migration] | None = None) -> tuple[int, ...]:
    """Apply pending migrations through a PEP-249/psycopg compatible connection."""
    selected = tuple(migrations if migrations is not None else load_migrations())
    cursor = connection.cursor()
    applied_now: list[int] = []
    try:
        cursor.execute(LEDGER_SQL)
        cursor.execute(
            "SELECT version, name, checksum FROM public.legal_agent_schema_migrations ORDER BY version"
        )
        applied = {int(version): (name, checksum.strip()) for version, name, checksum in cursor.fetchall()}
        known = {migration.version: migration for migration in selected}

        unknown_versions = set(applied) - set(known)
        if unknown_versions:
            raise MigrationError(f"database contains unknown migrations: {sorted(unknown_versions)}")

        for version, (name, checksum) in applied.items():
            migration = known[version]
            if migration.name != name or migration.checksum != checksum:
                raise MigrationError(f"checksum/name drift detected for migration V{version:04d}")

        for migration in selected:
            if migration.version in applied:
                continue
            cursor.execute(migration.sql)
            cursor.execute(
                "INSERT INTO public.legal_agent_schema_migrations (version, name, checksum) VALUES (%s, %s, %s)",
                (migration.version, migration.name, migration.checksum),
            )
            applied_now.append(migration.version)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        close = getattr(cursor, "close", None)
        if close is not None:
            close()
    return tuple(applied_now)

