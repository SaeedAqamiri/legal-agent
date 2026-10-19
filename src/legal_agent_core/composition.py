"""Production composition root.

Reads ``Settings`` from the environment and wires the real PostgreSQL,
FalkorDB and OIDC infrastructure, falling back to in-memory/reference
implementations when a service is not configured. This is the entry point for
``uvicorn legal_agent_core.composition:app``.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from datetime import date
from itertools import count
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import FastAPI

from .answering import CitationPipeline
from .api import APIContainer, OIDCProvider, create_app
from .application import (
    InMemoryResearchRecordRepository,
    KnowledgeReviewApplicationService,
    ResearchApplicationService,
)
from .demo import _canonical_data, _DemoComposer
from .errors import DomainError, NotFoundError
from .evaluation import EvaluationRunner
from .in_memory import InMemoryResearchGraphRepository
from .library import DocumentLibraryService
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


def _structure_summaries(settings: Settings) -> Any:
    """Lazy loader for offline container-node summaries (V0007 table).

    Loaded once on first get_document_structure call; empty mapping when the
    table or rows are absent (demo/in-memory mode).
    """
    if not settings.postgres.enabled:
        return {}
    import threading

    import psycopg

    lock = threading.Lock()
    cache: dict[str, str] | None = None

    def query() -> dict[str, str]:
        nonlocal cache
        with lock:
            if cache is not None:
                return cache
            merged: dict[str, str] = {}
            try:
                with psycopg.connect(settings.postgres.dsn) as db:
                    for provision_id, summary in db.execute(
                        "SELECT provision_id, summary FROM canonical.provision_summaries"
                    ).fetchall():
                        merged[provision_id] = summary
            except Exception:  # noqa: BLE001 - optional enrichment must never break tools
                return {}
            cache = merged
            return merged

    class _LazySummaries:
        def get(self, key: str) -> str | None:
            return query().get(key)

    return _LazySummaries()


def _agentic_extras(settings: Settings, canonical: Any) -> dict[str, Any]:
    """Optional capability-gated tool backends, mirroring run_benchmark wiring.

    - semantic_search: LEGAL_AGENT_EMBEDDINGS_BASE_URL (+ API_KEY/MODEL)
    - expand_search_terms: LEGAL_AGENT_LLM_BASE_URL (+ LEGAL_AGENT_LLM_API_KEY)

    Absent env = capability hidden from the planner catalog (spec_available).
    Construction failures degrade to disabled, never break startup.
    """
    import os

    logger = logging.getLogger(__name__)
    extras: dict[str, Any] = {}
    if os.environ.get("LEGAL_AGENT_EMBEDDINGS_BASE_URL"):
        try:
            from .agentic.semantic import OpenAIEmbeddingClient, SemanticSpanIndex

            client = OpenAIEmbeddingClient.from_env()
            extras["semantic_index"] = SemanticSpanIndex(canonical, client)
        except Exception as exc:  # noqa: BLE001
            logger.warning("semantic search disabled: %s", exc)
    llm_key = os.environ.get("LEGAL_AGENT_LLM_API_KEY")
    llm_base = os.environ.get("LEGAL_AGENT_LLM_BASE_URL")
    if llm_key and llm_base:
        try:
            from .adapters import OpenAICompatibleGateway
            from .agentic.term_expander import LLMTermExpander
            from .llm import (
                EndpointStyle,
                LLMTask,
                ModelProfile,
                ProviderConfig,
            )

            provider = ProviderConfig(
                "composition-llm",
                llm_base,
                EndpointStyle.CHAT_COMPLETIONS,
                api_key=llm_key,
            )
            profile = ModelProfile(
                profile_id="composition-expander",
                version=1,
                task=LLMTask.NAVIGATION,
                provider_id="composition-llm",
                model=os.environ.get("LEGAL_AGENT_LLM_MODEL", "glm-5.3-flash"),
                prompt_id="term-expansion",
                prompt_version=1,
                max_output_tokens=1024,
                temperature=0.1,
            )
            extras["term_expander"] = LLMTermExpander(
                OpenAICompatibleGateway(), provider, profile
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("term expansion disabled: %s", exc)
    return extras


def _connect_laws_graph(settings: Settings) -> Any:
    """Open the shared FalkorDB laws projection (best-effort at boot)."""
    if not settings.falkor.enabled:
        return None
    from .adapters.falkordb import connect_laws_graph

    try:
        return connect_laws_graph(settings.falkor.url)
    except Exception:  # noqa: BLE001 — projection is optional at boot
        return None


#: Tools hidden from the planner catalog (docs 06 §8): their behaviour is
#: covered by a kept tool, so exposing them only adds selection burden. They
#: remain callable for back-compat and direct use.
_PLANNER_HIDDEN_TOOLS: frozenset[str] = frozenset(
    {
        "list_document_contents",  # covered by get_document_structure / list_children
        "get_provision_version",  # covered by read_span
        "find_nodes",  # covered by search_laws
        "list_provisions",  # covered by get_document_structure / list_children
        "node_neighbors",  # covered by get_related
        "find_paths",  # covered by get_related
        "graph_stats",  # rarely needed by the research agent
    }
)


def _research_agent_provider(
    settings: Settings, canonical: Any, research_graph: Any, laws_graph: Any
) -> Any:
    """Build the runtime-configurable research agent (canonical + graph tools)."""
    from .adapters.postgres import PostgresCanonicalRepository
    from .agentic import LegalResearchTools
    from .agentic.graph_tools import GraphTools
    from .law_text import fetch_judgment_text, fetch_provision_text
    from .research_agent_provider import ResearchAgentProvider
    from .runtime_settings import LLMSettingsStore, default_settings_path
    from .verification import EvidenceVerifier

    notebook_repo = None
    if isinstance(canonical, PostgresCanonicalRepository):
        from .adapters.postgres_notebook import PostgresNotebookRepository

        notebook_repo = PostgresNotebookRepository(canonical.connection)

    def notebook_lookup(instrument_id: str) -> str | None:  # type: ignore[misc]
        if notebook_repo is None:
            return None
        page = notebook_repo.get_page(settings.default_organization_id, instrument_id)
        return page.summary if page is not None else None

    canonical_tools = LegalResearchTools(
        canonical,
        research_graph,
        structure_summaries=_structure_summaries(settings),
        hidden_tools=_PLANNER_HIDDEN_TOOLS,
        notebook_lookup=notebook_lookup,
        **_agentic_extras(settings, canonical),
    )
    graph_tools = None
    notebook_tools = None
    if laws_graph is not None:
        provision_text = judgment_text = None
        if isinstance(canonical, PostgresCanonicalRepository):
            connection = canonical.connection

            def provision_text(provision_id: str) -> dict | None:  # type: ignore[misc]
                return fetch_provision_text(connection, provision_id)

            def judgment_text(document_uid: str) -> dict | None:  # type: ignore[misc]
                return fetch_judgment_text(connection, document_uid)

        graph_tools = GraphTools(
            laws_graph,
            provision_text=provision_text,
            judgment_text=judgment_text,
        )
    if notebook_repo is not None:
        from .agentic.notebook_tools import NotebookTools

        notebook_tools = NotebookTools(notebook_repo, settings.default_organization_id)
    tree_tools = None
    from .agentic.tree_tools import TreeTools

    tree = _load_display_tree()
    if tree is not None:
        tree_tools = TreeTools(tree)
    return ResearchAgentProvider(
        canonical_tools,
        EvidenceVerifier(canonical),
        LLMSettingsStore(default_settings_path()),
        graph_tools=graph_tools,
        notebook_tools=notebook_tools,
        tree_tools=tree_tools,
        config=_agentic_config(),
    )

def _agentic_config() -> Any:
    """Production research budget (reading rulings is token-heavy).

    Fixed step/search/tool limits are opt-in for evaluation. Production follows
    evidence progress; wall and token limits remain emergency safeguards.
    """
    import os

    from .agentic.loop import AgenticConfig

    return AgenticConfig(
        max_steps=(
            int(value)
            if (value := os.environ.get("LEGAL_AGENT_AGENTIC_MAX_STEPS"))
            else None
        ),
        wall_seconds=float(os.environ.get("LEGAL_AGENT_AGENTIC_WALL", "900")),
        max_total_tokens=int(
            os.environ.get("LEGAL_AGENT_AGENTIC_MAX_TOTAL_TOKENS", "120000")
        ),
        recent_full_steps=1,
        max_search_steps=(
            int(value)
            if (value := os.environ.get("LEGAL_AGENT_AGENTIC_MAX_SEARCH_STEPS"))
            else None
        ),
        max_tool_calls=(
            int(value)
            if (value := os.environ.get("LEGAL_AGENT_AGENTIC_MAX_TOOL_CALLS"))
            else None
        ),
        answer_reserve_steps=int(
            os.environ.get("LEGAL_AGENT_AGENTIC_ANSWER_RESERVE_STEPS", "2")
        ),
        max_tool_seconds=float(
            os.environ.get("LEGAL_AGENT_AGENTIC_MAX_TOOL_SECONDS", "60")
        ),
    )


def _graph_chat_provider(settings: Settings, laws_graph: Any, canonical: Any = None) -> Any:
    """Runtime-configurable graph-guide chat agent over the laws projection.

    Always returns a provider so the workspace settings panel can configure the
    LLM gateway at runtime; the provider reports ``available=False`` until an
    API key is saved (or found in the environment).
    """
    from .graph_chat_provider import GraphChatProvider
    from .runtime_settings import LLMSettingsStore, default_settings_path

    store = LLMSettingsStore(default_settings_path())
    return GraphChatProvider(laws_graph, canonical, store)


def _load_display_tree(tree_path: str = "out/display_tree.json") -> Any:
    """Load the display-tree projection once (None when not built yet)."""
    import json
    from pathlib import Path

    path = Path(tree_path)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _notebook_topic_resolver(tree_path: str = "out/display_tree.json") -> Any:
    """node_id -> notebook topic id, from the display tree (docs 06 §4/§7).

    Instruments map to their domain topic; branch/domain ids map to themselves;
    anything unknown falls back to the root topic. Returns None if the tree is
    not present so notebook capture degrades to the root topic.
    """
    tree = _load_display_tree(tree_path)
    if tree is None:
        return None
    node_topic: dict[str, str] = {}

    def walk(node: dict, topic: str | None) -> None:
        current = node["id"] if node.get("kind") in {"branch", "domain"} else topic
        if node.get("kind") == "instrument":
            node_topic[node["id"]] = current or "root"
        for child in node.get("children", []):
            walk(child, current)

    walk(tree, "root")

    def resolve(node_id: str) -> str:
        return node_topic.get(node_id, "root")

    return resolve


def create_app_from_env(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    canonical = _canonical_repository(settings)
    graph = _graph_repository(settings)
    history = _history_store(settings)
    authorization = AuthorizationService()
    metrics = MetricsRegistry()
    identifiers = count(1)
    # Connect the laws mirror before building the research agent so its graph
    # tools can navigate the projection from the first run.
    laws_graph = _connect_laws_graph(settings)
    start_laws_mirror_sync(settings)

    notebook_repo = None
    notebook_resolver = None
    from .adapters.postgres import PostgresCanonicalRepository

    if isinstance(canonical, PostgresCanonicalRepository):
        from .adapters.postgres_notebook import PostgresNotebookRepository

        notebook_repo = PostgresNotebookRepository(canonical.connection)
        notebook_resolver = _notebook_topic_resolver()

    memory = _seed_graph_memory(graph, settings.default_organization_id, canonical)

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
        id_factory=lambda: f"req-{next(identifiers)}-{uuid4().hex[:8]}",
        agentic=_research_agent_provider(settings, canonical, graph, laws_graph),
        notebook=notebook_repo,
        notebook_topic_resolver=notebook_resolver,
        memory=memory,
    )
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

    default_principal = Principal(
        "local-user",
        settings.default_organization_id,
        frozenset({
            Role.RESEARCHER,
            Role.LEGAL_EXPERT,
            Role.KNOWLEDGE_STEWARD,
            Role.EVALUATOR,
            Role.INGESTION_OPERATOR,
            Role.ADMIN,
        }),
    )

    graph_chat = _graph_chat_provider(settings, laws_graph, canonical)

    return create_app(
        APIContainer(
            research,
            reviews,
            EvaluationRunner(research, authorization),
            token_verifier,
            authorization,
            metrics,
            library=DocumentLibraryService(canonical, authorization),
            history=history,
            oidc=oidc,
            default_principal=default_principal,
            laws_graph=laws_graph,
            graph_chat=graph_chat,
            llm_settings=graph_chat,
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


def start_laws_mirror_sync(settings: Settings) -> None:
    """Rebuild the FalkorDB laws projection in a background thread at boot.

    Postgres stays the source of truth; the mirror only serves Cypher queries
    (read-only endpoint + console), so a failed sync degrades gracefully.
    Opens its own Postgres connection: psycopg connections are not thread-safe.
    """
    if not settings.falkor.enabled or not settings.postgres.enabled:
        return
    import os

    if os.environ.get("LEGAL_AGENT_SKIP_MIRROR_SYNC", "").lower() in {"1", "true", "yes"}:
        # Eval/dev restarts: skip the ~7-9 min full rebuild when the graph is
        # already in sync (avoids racing restarts with in-flight requests).
        return

    def _run() -> None:
        try:
            import psycopg

            from .adapters.falkordb import connect_laws_graph
            from .laws_mirror import sync_laws_mirror

            connection = psycopg.connect(settings.postgres.dsn)
            graph = connect_laws_graph(settings.falkor.url)
            sync_laws_mirror(connection, graph)
        except Exception as exc:  # noqa: BLE001 — mirror is best-effort at boot
            logging.getLogger(__name__).warning(
                "laws mirror sync failed: %s: %s", type(exc).__name__, exc
            )

    import threading

    threading.Thread(target=_run, daemon=True, name="laws-mirror-sync").start()


app = create_app_from_env()
