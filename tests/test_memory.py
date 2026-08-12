import unittest

from legal_agent_core.errors import AuthorizationError, DomainError, NotFoundError
from legal_agent_core.in_memory import InMemoryResearchGraphRepository
from legal_agent_core.research import (
    Actor,
    ProgressiveEdgeType,
    ProgressiveRelation,
    TrustStatus,
    TruthClass,
)
from legal_agent_core.services import ProgressiveMemoryService


def proposal(organization_id: str = "org-a") -> ProgressiveRelation:
    return ProgressiveRelation(
        relation_id="rel-1",
        organization_id=organization_id,
        source_node_id="issue:signing-authority",
        target_node_id="prov-4",
        edge_type=ProgressiveEdgeType.CONSTRAINED_BY,
        truth_class=TruthClass.INFERRED,
        source_episode_id="episode-182",
        evidence_version_ids=("prov-4-v2",),
        confidence=0.9,
    )


class ProgressiveMemoryTests(unittest.TestCase):
    def test_interpretive_relation_requires_versioned_evidence(self) -> None:
        with self.assertRaisesRegex(DomainError, "evidence"):
            ProgressiveRelation(
                "rel-1",
                "org-a",
                "issue",
                "prov",
                ProgressiveEdgeType.GOVERNED_BY,
                TruthClass.INFERRED,
                "episode-1",
                (),
            )

    def test_agent_cannot_activate_memory_without_expert_approval(self) -> None:
        repository = InMemoryResearchGraphRepository()
        service = ProgressiveMemoryService(repository)
        agent = Actor("agent-1", "org-a", frozenset({"agent"}))
        service.create_candidate(agent, proposal())

        self.assertEqual(repository.active_relations("org-a"), ())
        with self.assertRaises(AuthorizationError):
            service.approve(agent, "rel-1")

    def test_expert_approval_activates_relation_and_is_audited(self) -> None:
        repository = InMemoryResearchGraphRepository()
        service = ProgressiveMemoryService(repository)
        service.create_candidate(Actor("agent-1", "org-a"), proposal())
        approved = service.approve(
            Actor("expert-1", "org-a", frozenset({"legal_expert"})), "rel-1", "بررسی شد"
        )

        self.assertEqual(approved.status, TrustStatus.EXPERT_APPROVED)
        self.assertIsNotNone(approved.approval)
        assert approved.approval is not None
        self.assertEqual(approved.approval.approved_by, "expert-1")
        self.assertEqual(repository.active_relations("org-a"), (approved,))
        self.assertEqual(
            [event.action.value for event in repository.events("org-a")],
            ["candidate_created", "candidate_approved"],
        )

    def test_organization_overlay_is_isolated(self) -> None:
        repository = InMemoryResearchGraphRepository()
        service = ProgressiveMemoryService(repository)
        service.create_candidate(Actor("agent-1", "org-a"), proposal())

        with self.assertRaises(NotFoundError):
            repository.get_relation("org-b", "rel-1")
        with self.assertRaises(AuthorizationError):
            service.create_candidate(Actor("agent-2", "org-b"), proposal())

    def test_review_queue_filters_status_and_tenant(self) -> None:
        repository = InMemoryResearchGraphRepository()
        service = ProgressiveMemoryService(repository)
        service.create_candidate(Actor("agent-a", "org-a"), proposal("org-a"))
        service.create_candidate(Actor("agent-b", "org-b"), proposal("org-b"))

        queue = repository.list_relations("org-a", frozenset({TrustStatus.CANDIDATE}))

        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0].organization_id, "org-a")

    def test_superseded_source_marks_approved_knowledge_for_revalidation(self) -> None:
        repository = InMemoryResearchGraphRepository()
        service = ProgressiveMemoryService(repository)
        service.create_candidate(Actor("agent-1", "org-a"), proposal())
        expert = Actor("expert-1", "org-a", frozenset({"knowledge_steward"}))
        service.approve(expert, "rel-1")

        impacted = service.mark_version_superseded(
            expert, "prov-4-v2", "prov-4-v3 supersedes source"
        )

        self.assertEqual(impacted[0].status, TrustStatus.NEEDS_REVALIDATION)
        self.assertIsNotNone(impacted[0].approval)
        assert impacted[0].approval is not None
        self.assertEqual(impacted[0].approval.approved_by, "expert-1")
        self.assertEqual(repository.active_relations("org-a"), ())


if __name__ == "__main__":
    unittest.main()
