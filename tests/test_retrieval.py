import unittest
from datetime import UTC, date, datetime

from legal_agent_core.canonical import (
    CreationMethod,
    DocumentStatus,
    DocumentVersion,
    ExplicitReference,
    InstrumentType,
    LegalInstrument,
    Provision,
    ProvisionType,
    ProvisionVersion,
    ResolutionStatus,
    SourceDocument,
    SourceSpan,
)
from legal_agent_core.in_memory import InMemoryCanonicalRepository
from legal_agent_core.navigation import NavigationLoop, ResearchRequest
from legal_agent_core.research import (
    Approval,
    GraphNamespace,
    ProgressiveEdgeType,
    ProgressiveRelation,
    TrustStatus,
    TruthClass,
)
from legal_agent_core.retrieval import (
    MultiSignalEvidenceRetriever,
    RetrievalSignal,
    normalize_search_text,
)
from legal_agent_core.verification import (
    AnswerClaim,
    ClaimImportance,
    DraftAnswer,
    EvidenceVerifier,
)

APPLICABLE_TIME = date(2025, 1, 1)


def research_request(question: str, scope: tuple[str, ...] = ()) -> ResearchRequest:
    return ResearchRequest(
        organization_id="org-1",
        question=question,
        applicable_time=APPLICABLE_TIME,
        document_scope=scope,
        iteration=1,
        unresolved_issues=(),
        visited_provision_ids=(),
    )


def retrieval_repository(*, with_reference: bool = False) -> InMemoryCanonicalRepository:
    repository = InMemoryCanonicalRepository()
    repository.add_source_document(
        SourceDocument(
            "source-1",
            None,
            "tax-law.pdf",
            "application/pdf",
            "sha256:tax",
            100,
            "test",
            "file:///tax-law.pdf",
        )
    )
    repository.add_instrument(
        LegalInstrument(
            "tax-law",
            "قانون مالیات",
            "قانون مالیات",
            InstrumentType.STATUTE,
            "IR",
            subject_domain="مالیات",
        )
    )
    repository.add_document_version(
        DocumentVersion(
            "tax-law-old",
            "tax-law",
            "source-1",
            DocumentStatus.SUPERSEDED,
            effective_from=date(2020, 1, 1),
            effective_to=date(2022, 12, 31),
        )
    )
    repository.add_document_version(
        DocumentVersion(
            "tax-law-current",
            "tax-law",
            "source-1",
            DocumentStatus.EFFECTIVE,
            effective_from=date(2023, 1, 1),
        )
    )
    repository.add_provision(Provision("article-5", "tax-law", ProvisionType.ARTICLE, "5", "ماده ۵"))
    repository.add_provision_version(
        ProvisionVersion(
            "article-5-old",
            "article-5",
            "tax-law-old",
            "نرخ مالیات شرکت ده درصد است.",
            "نرخ مالیات شرکت ده درصد است.",
            DocumentStatus.SUPERSEDED,
            "test",
            date(2020, 1, 1),
            date(2022, 12, 31),
        )
    )
    current_text = "طبق ماده ۴ این قانون، نرخ مالیات شرکت بیست درصد است."
    repository.add_provision_version(
        ProvisionVersion(
            "article-5-current",
            "article-5",
            "tax-law-current",
            current_text,
            current_text,
            DocumentStatus.EFFECTIVE,
            "test",
            date(2023, 1, 1),
            supersedes_version_id="article-5-old",
        )
    )
    repository.add_source_span(
        SourceSpan("span-5-old", "source-1", "tax-law-old", "article-5-old", 2, "نرخ مالیات شرکت ده درصد است.")
    )
    repository.add_source_span(
        SourceSpan("span-5-current", "source-1", "tax-law-current", "article-5-current", 3, current_text)
    )

    if with_reference:
        repository.add_provision(Provision("article-4", "tax-law", ProvisionType.ARTICLE, "4", "ماده ۴"))
        repository.add_provision_version(
            ProvisionVersion(
                "article-4-current",
                "article-4",
                "tax-law-current",
                "شرایط معافیت مالیاتی شرکت در این ماده تعیین می‌شود.",
                "شرایط معافیت مالیاتی شرکت در این ماده تعیین می‌شود.",
                DocumentStatus.EFFECTIVE,
                "test",
                date(2023, 1, 1),
            )
        )
        repository.add_source_span(
            SourceSpan(
                "span-4-current",
                "source-1",
                "tax-law-current",
                "article-4-current",
                2,
                "شرایط معافیت مالیاتی شرکت در این ماده تعیین می‌شود.",
            )
        )
        repository.add_explicit_reference(
            ExplicitReference(
                "reference-5-to-4",
                "article-5-current",
                "ماده ۴ این قانون",
                "span-5-current",
                CreationMethod.DETERMINISTIC_EXTRACTOR,
                ResolutionStatus.RESOLVED,
                "article-4",
                1.0,
            )
        )
    return repository


class StaticResearchGraph:
    def __init__(self, *relations: ProgressiveRelation) -> None:
        self.relations = relations

    def active_relations(self, organization_id: str) -> tuple[ProgressiveRelation, ...]:
        return tuple(
            relation
            for relation in self.relations
            if relation.organization_id == organization_id and relation.active_for_navigation
        )


class EvidenceComposer:
    def compose(self, question, applicable_time, evidence):
        return DraftAnswer(
            "answer-retrieval",
            "پاسخ مبتنی بر منابع بازیابی‌شده",
            (
                AnswerClaim(
                    "claim-retrieval",
                    "حکم مالیاتی",
                    tuple(item.evidence_id for item in evidence),
                    ClaimImportance.CRITICAL,
                ),
            ),
        )


class MultiSignalRetrievalTests(unittest.TestCase):
    def test_normalizes_persian_arabic_variants_and_digits(self) -> None:
        self.assertEqual(normalize_search_text("مادۀ ٥ كیفری"), "ماده 5 کیفری")

    def test_temporal_gate_excludes_obsolete_matching_version(self) -> None:
        retriever = MultiSignalEvidenceRetriever(retrieval_repository())

        batch = retriever.retrieve(research_request("نرخ مالیات شرکت"))

        self.assertEqual([item.provision_version_id for item in batch.evidence], ["article-5-current"])
        self.assertIn(RetrievalSignal.LEXICAL, retriever.last_ranked[0].signals)
        self.assertIn("lexical=", batch.evidence[0].reason_selected)

    def test_expands_resolved_explicit_reference_with_auditable_trace(self) -> None:
        retriever = MultiSignalEvidenceRetriever(retrieval_repository(with_reference=True))

        batch = retriever.retrieve(research_request("نرخ مالیات شرکت"))

        self.assertEqual(
            {item.provision_id for item in batch.evidence},
            {"article-4", "article-5"},
        )
        self.assertEqual(batch.followed_reference_ids, ("reference-5-to-4",))
        target = next(item for item in retriever.last_ranked if item.evidence.provision_id == "article-4")
        self.assertIn(RetrievalSignal.EXPLICIT_REFERENCE, target.signals)

    def test_document_scope_is_a_hard_filter(self) -> None:
        repository = retrieval_repository()

        batch = MultiSignalEvidenceRetriever(repository).retrieve(
            research_request("نرخ مالیات شرکت", ("outside-scope",))
        )

        self.assertEqual(batch.evidence, ())
        self.assertEqual(batch.identified_issues, ("no_retrieval_candidates",))

    def test_expert_approved_memory_is_only_a_ranking_signal(self) -> None:
        repository = retrieval_repository(with_reference=True)
        approval = Approval("expert-1", datetime(2025, 1, 1, tzinfo=UTC), "org-1", None, "episode-1")
        relation = ProgressiveRelation(
            "relation-1",
            "org-1",
            "issue-tax",
            "article-4",
            ProgressiveEdgeType.RELEVANT_TO,
            TruthClass.EXPERT_ASSERTED,
            "episode-1",
            ("article-4-current",),
            target_namespace=GraphNamespace.CANONICAL,
            confidence=1.0,
            status=TrustStatus.EXPERT_APPROVED,
            approval=approval,
        )
        retriever = MultiSignalEvidenceRetriever(repository, StaticResearchGraph(relation))

        retriever.retrieve(research_request("شرکت"))

        target = next(item for item in retriever.last_ranked if item.evidence.provision_id == "article-4")
        self.assertIn(RetrievalSignal.APPROVED_MEMORY, target.signals)

    def test_integrates_with_navigation_and_verification(self) -> None:
        repository = retrieval_repository(with_reference=True)
        retriever = MultiSignalEvidenceRetriever(repository)
        loop = NavigationLoop(retriever, EvidenceComposer(), EvidenceVerifier(repository))

        outcome = loop.run(
            episode_id="episode-retrieval",
            organization_id="org-1",
            user_id="user-1",
            conversation_id="conversation-1",
            question="نرخ مالیات شرکت",
            applicable_time=APPLICABLE_TIME,
        )

        self.assertTrue(outcome.completed)
        self.assertEqual(outcome.iterations, 1)
        self.assertEqual(set(outcome.episode.visited_provisions), {"article-4", "article-5"})


if __name__ == "__main__":
    unittest.main()
