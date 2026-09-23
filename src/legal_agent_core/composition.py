"""Production composition root.

Reads ``Settings`` from the environment and wires the real PostgreSQL,
FalkorDB and OIDC infrastructure, falling back to in-memory/reference
implementations when a service is not configured. This is the entry point for
``uvicorn legal_agent_core.composition:app``.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import date
from itertools import count
from pathlib import Path

from fastapi import FastAPI

from .agentic import LegalResearchTools, ToolResearchLoop
from .answering import CitationPipeline
from .api import APIContainer, OIDCProvider, create_app
from .application import (
    InMemoryResearchRecordRepository,
    KnowledgeReviewApplicationService,
    ResearchApplicationService,
)
from .demo import _canonical_data, _DemoComposer, _DemoToolPlanner
from .errors import DomainError, NotFoundError
from .evaluation import EvaluationRunner
from .in_memory import InMemoryResearchGraphRepository
from .navigation import NavigationLoop
from .observability import MetricsRegistry
from .research import Actor, ProgressiveEdgeType, ProgressiveRelation, TruthClass
from .retrieval import MultiSignalEvidenceRetriever
from .security import AuthorizationService, Principal, Role
from .services import ProgressiveMemoryService
from .settings import Settings
from .verification import EvidenceVerifier


def _canonical_repository(settings: Settings):
    if not settings.postgres.enabled:
        return _canonical_data()
    import psycopg

    from .adapters import PostgresCanonicalRepository
    from .db import apply_migrations

    connection = psycopg.connect(settings.postgres.dsn)
    if settings.postgres.migrate:
        apply_migrations(connection)
    repository = PostgresCanonicalRepository(connection)
    seed_canonical_demo(repository, connection)
    seed_markdown_corpus(repository, connection, settings)
    connection.commit()
    connection.autocommit = True
    return repository


def _ensure(repository, exists: bool, add: Callable[[], None]) -> None:
    if not exists:
        add()


def seed_canonical_demo(
    repository, connection=None, organization_id: str = "org-a"
) -> None:
    """Idempotently insert the demo canonical set into ``repository``.

    Canonical tables are append-only and immutable; each row is guarded by an
    existence check so repeated startups never conflict.
    """
    from .canonical import (
        DocumentStatus,
        DocumentVersion,
        InstrumentType,
        LegalInstrument,
        Provision,
        ProvisionType,
        ProvisionVersion,
        SourceDocument,
        SourceSpan,
    )

    def exists(loader) -> bool:
        try:
            loader()
            return True
        except NotFoundError:
            return False

    source_text = "مهلت درخواست تجدیدنظر اشخاص مقیم ایران بیست روز است."
    _ensure(
        repository,
        exists(lambda: repository.get_source_document("source-demo")),
        lambda: repository.add_source_document(
            SourceDocument(
                "source-demo", None, "sample-appeal-law.pdf", "application/pdf",
                "sha256:demo-source", 2048, "demo",
                "https://example.invalid/sample-appeal-law.pdf",
            )
        ),
    )
    _ensure(
        repository,
        exists(lambda: repository.get_instrument("instrument-demo")),
        lambda: repository.add_instrument(
            LegalInstrument(
                "instrument-demo", "قانون نمونه آیین دادرسی", "قانون نمونه آیین دادرسی",
                InstrumentType.STATUTE, "IR",
            )
        ),
    )
    _ensure(
        repository,
        exists(lambda: repository.get_document_version("document-demo-v1")),
        lambda: repository.add_document_version(
            DocumentVersion(
                "document-demo-v1", "instrument-demo", "source-demo",
                DocumentStatus.EFFECTIVE, effective_from=_DEMO_EFFECTIVE,
            )
        ),
    )
    _ensure(
        repository,
        exists(lambda: repository.get_provision("provision-demo-336")),
        lambda: repository.add_provision(
            Provision(
                "provision-demo-336", "instrument-demo", ProvisionType.ARTICLE,
                "336", "ماده ۳۳۶",
            )
        ),
    )
    _ensure(
        repository,
        exists(lambda: repository.get_provision_version("provision-demo-336-v1")),
        lambda: repository.add_provision_version(
            ProvisionVersion(
                "provision-demo-336-v1", "provision-demo-336", "document-demo-v1",
                source_text, source_text, DocumentStatus.EFFECTIVE, "demo", _DEMO_EFFECTIVE,
            )
        ),
    )
    _ensure(
        repository,
        exists(
            lambda: repository.get_source_span("span-demo-336")
        ),
        lambda: repository.add_source_span(
            SourceSpan(
                "span-demo-336", "source-demo", "document-demo-v1",
                "provision-demo-336-v1", 42, source_text,
                char_start=0, char_end=len(source_text),
            )
        ),
    )
    if connection is not None:
        connection.commit()


_DEMO_EFFECTIVE = date(2024, 1, 1)

# OCR-free sample corpus: markdown text-layer of the Tamin long-document set.
_DEFAULT_CORPUS = Path(__file__).parent.parent.parent / "tests" / "tamin_longdocs_agentic_benchmark_v4" / "documents_md"
_KNOWN_SAMPLES = (
    "D01.md", "D02.md", "D03.md", "D04.md", "D05.md",
    "D06.md", "D07.md", "D08.md", "D09.md",
)


def seed_markdown_corpus(repository, connection=None, settings: Settings | None = None):
    """Idempotently ingest the OC-free markdown sample corpus on startup.

    Only runs against a persistent repository and only when a corpus directory is
    configured (env ``LEGAL_AGENT_SAMPLE_CORPUS_DIR``) or discoverable next to the
    package; documents already present are skipped. In-memory/demo repos skip it so
    unit tests stay small and deterministic.
    """
    if connection is None:
        return
    corpus_dir = Path(
        os.environ.get("LEGAL_AGENT_SAMPLE_CORPUS_DIR") or _DEFAULT_CORPUS
    )
    if not corpus_dir.is_dir():
        return

    import hashlib

    from .ingestion.markdown_parser import MarkdownDocumentParser
    from .ingestion.pipeline import CanonicalIngestionPipeline, stable_id

    parser = MarkdownDocumentParser()
    pipeline = CanonicalIngestionPipeline(repository)
    for path in sorted(corpus_dir.glob("*.md")):
        if path.name not in _KNOWN_SAMPLES:
            continue
        text = path.read_text(encoding="utf-8")
        checksum = hashlib.sha256(text.encode("utf-8")).hexdigest()
        source_document_id = stable_id("src", f"sha256:{checksum}")
        try:
            repository.get_source_document(source_document_id)
        except NotFoundError:
            pass
        else:
            continue
        parsed = parser.from_text(text, path.name)
        try:
            pipeline.ingest(parsed)
        except DomainError:  # parser/structural guard
            continue


def _graph_repository(settings: Settings):
    if settings.falkor.enabled:
        from .adapters import FalkorResearchGraphRepository

        repository = FalkorResearchGraphRepository.connect(
            graph_prefix=settings.falkor.graph_prefix,
            url=settings.falkor.url,
        )
        repository.ensure_indexes(settings.default_organization_id)
        return repository
    return InMemoryResearchGraphRepository()


def _history_store(settings: Settings):
    from .application import ResearchHistoryStore

    if settings.postgres.enabled:
        import psycopg

        from .adapters import PostgresResearchHistoryStore

        return PostgresResearchHistoryStore(
            connection_factory=lambda: psycopg.connect(settings.postgres.dsn)
        )
    return ResearchHistoryStore(settings.history_path)


def _seed_graph_memory(
    graph, organization_id: str, canonical
) -> ProgressiveMemoryService:
    memory = ProgressiveMemoryService(graph)
    try:
        graph.get_relation(organization_id, "relation-demo-1")
    except NotFoundError:
        memory.create_candidate(
            Actor("demo-agent", organization_id),
            ProgressiveRelation(
                "relation-demo-1", organization_id, "issue:appeal-deadline",
                "provision-demo-336", ProgressiveEdgeType.GOVERNED_BY,
                TruthClass.INFERRED, "episode-demo-seed", ("provision-demo-336-v1",),
                confidence=0.94, generated_by_model="demo-model",
                model_profile="navigation-demo", prompt_version="1", agent_version="0.1.0",
            ),
        )
    return memory


def create_app_from_env(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    canonical = _canonical_repository(settings)
    graph = _graph_repository(settings)
    history = _history_store(settings)
    authorization = AuthorizationService()
    metrics = MetricsRegistry()
    identifiers = count(1)

    research = ResearchApplicationService(
        NavigationLoop(
            MultiSignalEvidenceRetriever(canonical, graph),
            _DemoComposer(),
            EvidenceVerifier(canonical),
        ),

        CitationPipeline(canonical),
        InMemoryResearchRecordRepository(),
        authorization,
        metrics,
        id_factory=lambda: f"req-{next(identifiers)}",
        agentic=ToolResearchLoop(
            LegalResearchTools(canonical, graph),
            _DemoToolPlanner(),
            EvidenceVerifier(canonical),
        ),
    )
    memory = _seed_graph_memory(graph, settings.default_organization_id, canonical)
    reviews = KnowledgeReviewApplicationService(memory, authorization, metrics)

    token_verifier = _token_verifier(settings)
    oidc: OIDCProvider | None = None
    if settings.oidc.enabled:
        from .adapters.oidc import (
            OIDCAuthorizationClient,
            OIDCTokenVerifier,
            ProviderMetadata,
            SessionTokenVerifier,
        )

        provider = ProviderMetadata(
            issuer=settings.oidc.issuer,
            authorization_endpoint=settings.oidc.authorization_endpoint,
            token_endpoint=settings.oidc.token_endpoint,
            jwks_uri=settings.oidc.jwks_uri,
            client_id=settings.oidc.client_id,
            client_secret=settings.oidc.client_secret,
            redirect_uri=settings.oidc.redirect_uri,
        )
        oidc = OIDCProvider(
            client=OIDCAuthorizationClient(provider),
            verifier=OIDCTokenVerifier(
                issuer=settings.oidc.issuer,
                client_id=settings.oidc.client_id,
                jwks_uri=settings.oidc.jwks_uri,
            ),
            session=SessionTokenVerifier(
                _session_secret(settings),
            ),
        )

    return create_app(
        APIContainer(
            research,
            reviews,
            EvaluationRunner(research, authorization),
            token_verifier,
            authorization,
            metrics,
            history=history,
            oidc=oidc,
        )
    )


def _token_verifier(settings: Settings):
    """Bearer verifier: configured static tokens, else the local demo tokens."""
    from .security import StaticTokenVerifier

    static = settings.oidc.static_tokens or {}
    tokens: dict[str, object] = {}
    if settings.oidc.enabled:
        from .settings import build_token_verifier

        return build_token_verifier(settings)
    if not static:
        tokens["researcher-demo"] = Principal(
            "demo-researcher", settings.default_organization_id, frozenset({Role.RESEARCHER})
        )
        tokens["expert-demo"] = Principal(
            "demo-expert", settings.default_organization_id, frozenset({Role.LEGAL_EXPERT})
        )
        tokens["evaluator-demo"] = Principal(
            "demo-evaluator", settings.default_organization_id, frozenset({Role.EVALUATOR})
        )
    else:
        for token, principal_data in static.items():
            roles = frozenset(Role(role) for role in principal_data.get("roles", ()))
            tokens[token] = Principal(
                str(principal_data.get("actor_id", token)),
                str(principal_data.get("organization_id", settings.default_organization_id)),
                roles,
            )
    return StaticTokenVerifier(tokens)


def _session_secret(settings: Settings) -> str:
    from .settings import _env

    return _env("LEGAL_AGENT_SESSION_SECRET", "dev-session-secret-change-me") or ""


app = create_app_from_env()
