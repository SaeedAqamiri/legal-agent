import unittest
from datetime import date

from test_verification import (
    APPLICABLE_TIME,
    canonical_repository,
    evidence_current,
    evidence_old,
)

from legal_agent_core.navigation import (
    NavigationConfig,
    NavigationLoop,
    ResearchRequest,
    RetrievalBatch,
)
from legal_agent_core.research import Evidence, ResearchActionType
from legal_agent_core.verification import (
    AnswerClaim,
    DraftAnswer,
    EvidenceVerifier,
    VerificationCode,
)


class SequenceRetriever:
    def __init__(self, batches: list[RetrievalBatch]) -> None:
        self.batches = batches
        self.requests: list[ResearchRequest] = []

    def retrieve(self, request: ResearchRequest) -> RetrievalBatch:
        self.requests.append(request)
        if self.batches:
            return self.batches.pop(0)
        return RetrievalBatch(())


class EvidenceClaimComposer:
    def __init__(self) -> None:
        self.calls: list[tuple[Evidence, ...]] = []

    def compose(
        self,
        question: str,
        applicable_time: date,
        evidence: tuple[Evidence, ...],
    ) -> DraftAnswer:
        self.calls.append(evidence)
        return DraftAnswer(
            f"answer-{len(self.calls)}",
            "پاسخ بر اساس شواهد موجود",
            (AnswerClaim("claim-main", "حکم ماده پنج", tuple(item.evidence_id for item in evidence)),),
        )


class NavigationLoopTests(unittest.TestCase):
    def test_continues_after_temporal_rejection_and_completes_with_current_version(self) -> None:
        retriever = SequenceRetriever(
            [
                RetrievalBatch((evidence_old(),), visited_provision_ids=("prov-5",)),
                RetrievalBatch((evidence_current(),), visited_provision_ids=("prov-5",)),
            ]
        )
        composer = EvidenceClaimComposer()
        loop = NavigationLoop(
            retriever,
            composer,
            EvidenceVerifier(canonical_repository()),
            NavigationConfig(max_iterations=3),
        )

        outcome = loop.run(
            episode_id="episode-1",
            organization_id="org-a",
            user_id="user-a",
            conversation_id="conversation-a",
            question="حکم ماده پنج در سال ۲۰۲۵ چیست؟",
            applicable_time=APPLICABLE_TIME,
        )

        self.assertTrue(outcome.completed)
        self.assertEqual(outcome.iterations, 2)
        self.assertEqual(outcome.episode.evidence_ids, ["ev-current"])
        self.assertIn(
            VerificationCode.TEMPORAL_MISMATCH,
            {issue.code for issue in retriever.requests[1].unresolved_issues},
        )
        action_types = [action.action_type for action in outcome.episode.actions]
        self.assertIn(ResearchActionType.REJECTED_EVIDENCE, action_types)
        self.assertIn(ResearchActionType.ACCEPTED_EVIDENCE, action_types)
        self.assertEqual(tuple(item.evidence_id for item in composer.calls[1]), ("ev-current",))

    def test_stops_when_research_stalls_without_new_evidence(self) -> None:
        retriever = SequenceRetriever([RetrievalBatch(())])
        loop = NavigationLoop(
            retriever,
            EvidenceClaimComposer(),
            EvidenceVerifier(canonical_repository()),
            NavigationConfig(max_iterations=5, stop_on_stall=True),
        )

        outcome = loop.run(
            episode_id="episode-2",
            organization_id="org-a",
            user_id="user-a",
            conversation_id="conversation-a",
            question="سؤال بدون سند",
            applicable_time=APPLICABLE_TIME,
        )

        self.assertFalse(outcome.completed)
        self.assertEqual(outcome.iterations, 1)
        self.assertIn(VerificationCode.MISSING_EVIDENCE, {issue.code for issue in outcome.verification.issues})

    def test_records_followed_references_as_structured_trace(self) -> None:
        retriever = SequenceRetriever(
            [RetrievalBatch((evidence_current(),), followed_reference_ids=("ref-1",))]
        )
        outcome = NavigationLoop(
            retriever,
            EvidenceClaimComposer(),
            EvidenceVerifier(canonical_repository()),
        ).run(
            episode_id="episode-3",
            organization_id="org-a",
            user_id="user-a",
            conversation_id="conversation-a",
            question="سؤال",
            applicable_time=APPLICABLE_TIME,
        )

        followed = [
            action.target_id
            for action in outcome.episode.actions
            if action.action_type == ResearchActionType.FOLLOWED_REFERENCE
        ]
        self.assertEqual(followed, ["ref-1"])


if __name__ == "__main__":
    unittest.main()

