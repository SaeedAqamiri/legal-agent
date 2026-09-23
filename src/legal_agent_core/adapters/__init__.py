"""Infrastructure adapters for domain repository ports."""

from .falkordb import FalkorResearchGraphRepository
from .llm_http import OpenAICompatibleGateway, UrllibJsonTransport
from .oidc import (
    OIDCAuthorizationClient,
    OIDCTokenVerifier,
    SessionTokenVerifier,
)
from .postgres import (
    PostgresCanonicalRepository,
    PostgresResearchHistoryStore,
    PostgresUnitOfWork,
)
from .postgres_ingestion import PostgresIngestionRepository, PostgresIngestionWorker

__all__ = [
    "FalkorResearchGraphRepository",
    "OIDCAuthorizationClient",
    "OIDCTokenVerifier",
    "OpenAICompatibleGateway",
    "PostgresCanonicalRepository",
    "PostgresIngestionRepository",
    "PostgresIngestionWorker",
    "PostgresResearchHistoryStore",
    "PostgresUnitOfWork",
    "SessionTokenVerifier",
    "UrllibJsonTransport",
]
