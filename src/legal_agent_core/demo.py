"""Runnable, in-memory showcase for the research workspace.

This module contains public demo credentials and must not be used as a
production composition root.
"""

from __future__ import annotations

from datetime import date
from itertools import count

from fastapi import FastAPI

from .answering import CitationPipeline
from .api import APIContainer, create_app
from .application import (
    InMemoryResearchRecordRepository,
    KnowledgeReviewApplicationService,
    ResearchApplicationService,
)
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
from .evaluation import EvaluationRunner
from .in_memory import InMemoryCanonicalRepository, InMemoryResearchGraphRepository
from .navigation import NavigationLoop
from .observability import MetricsRegistry
from .research import (
    Actor,
    ProgressiveEdgeType,
    ProgressiveRelation,
    TruthClass,
)
from .retrieval import MultiSignalEvidenceRetriever
from .security import AuthorizationService, Principal, Role, StaticTokenVerifier
from .services import ProgressiveMemoryService
from .verification import AnswerClaim, ClaimImportance, DraftAnswer, EvidenceVerifier


class _DemoComposer:
    def compose(
        self, question: str, applicable_time: date, evidence: tuple
    ) -> DraftAnswer:
        del question, applicable_time
        return DraftAnswer(
            "answer-demo",
            "بر پایه ماده ۳۳۶ قانون نمونه، مهلت اعتراض برای اشخاص مقیم ایران بیست روز است.",
            (
                AnswerClaim(
                    "claim-demo",
                    "مهلت اعتراض بیست روز است.",
                    tuple(item.evidence_id for item in evidence),
                    ClaimImportance.CRITICAL,
                ),
            ),
        )


def _canonical_data() -> InMemoryCanonicalRepository:
    repository = InMemoryCanonicalRepository()
    repository.add_source_document(
        SourceDocument(
            "source-demo",
            None,
            "sample-appeal-law.pdf",
            "application/pdf",
            "sha256:demo-source",
            2048,
            "demo",
            "https://example.invalid/sample-appeal-law.pdf",
        )
    )
    repository.add_instrument(
        LegalInstrument(
            "instrument-demo",
            "قانون نمونه آیین دادرسی",
            "قانون نمونه آیین دادرسی",
            InstrumentType.STATUTE,
            "IR",
        )
    )
    repository.add_document_version(
        DocumentVersion(
            "document-demo-v1",
            "instrument-demo",
            "source-demo",
            DocumentStatus.EFFECTIVE,
            effective_from=date(2024, 1, 1),
        )
    )
    repository.add_provision(
        Provision(
            "provision-demo-336",
            "instrument-demo",
            ProvisionType.ARTICLE,
            "336",
            "ماده ۳۳۶",
        )
    )
    source_text = "مهلت درخواست تجدیدنظر اشخاص مقیم ایران بیست روز است."
    repository.add_provision_version(
        ProvisionVersion(
            "provision-demo-336-v1",
            "provision-demo-336",
            "document-demo-v1",
            source_text,
            source_text,
            DocumentStatus.EFFECTIVE,
            "demo",
            date(2024, 1, 1),
        )
    )
    repository.add_source_span(
        SourceSpan(
            "span-demo-336",
            "source-demo",
            "document-demo-v1",
            "provision-demo-336-v1",
            42,
            source_text,
            char_start=0,
            char_end=len(source_text),
        )
    )
    return repository


def create_demo_app() -> FastAPI:
    canonical = _canonical_data()
    graph = InMemoryResearchGraphRepository()
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
        id_factory=lambda: f"demo-{next(identifiers)}",
    )
    memory = ProgressiveMemoryService(graph)
    memory.create_candidate(
        Actor("demo-agent", "org-a"),
        ProgressiveRelation(
            "relation-demo-1",
            "org-a",
            "issue:appeal-deadline",
            "provision-demo-336",
            ProgressiveEdgeType.GOVERNED_BY,
            TruthClass.INFERRED,
            "episode-demo-seed",
            ("provision-demo-336-v1",),
            confidence=0.94,
            generated_by_model="demo-model",
            model_profile="navigation-demo",
            prompt_version="1",
            agent_version="0.1.0",
        ),
    )
    reviews = KnowledgeReviewApplicationService(memory, authorization, metrics)
    verifier = StaticTokenVerifier(
        {
            "researcher-demo": Principal(
                "demo-researcher", "org-a", frozenset({Role.RESEARCHER})
            ),
            "expert-demo": Principal(
                "demo-expert", "org-a", frozenset({Role.LEGAL_EXPERT})
            ),
        }
    )
    return create_app(
        APIContainer(
            research,
            reviews,
            EvaluationRunner(research, authorization),
            verifier,
            authorization,
            metrics,
        )
    )


app = create_demo_app()
