"""Database migration support for legal-agent-core."""

from .migrator import Migration, MigrationError, apply_migrations, load_migrations

__all__ = ["Migration", "MigrationError", "apply_migrations", "load_migrations"]

