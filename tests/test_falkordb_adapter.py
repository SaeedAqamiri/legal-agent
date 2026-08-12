import unittest
from datetime import UTC, datetime

from legal_agent_core.adapters.falkordb import FalkorResearchGraphRepository
from legal_agent_core.errors import ConflictError, DomainError, NotFoundError
from legal_agent_core.research import (
    Approval,
    MemoryEvent,
    MemoryEventType,
    ProgressiveEdgeType,
    ProgressiveRelation,
    TrustStatus,
    TruthClass,
)


class FakeResult:
    def __init__(self, rows: list[list[object]] | None = None) -> None:
        self.result_set = rows or []


class FakeGraph:
    def __init__(self) -> None:
        self.write_results: list[FakeResult] = []
        self.read_results: list[FakeResult] = []
        self.writes: list[tuple[str, dict[str, object] | None]] = []
        self.reads: list[tuple[str, dict[str, object] | None]] = []

    def query(self, query: str, params: dict[str, object] | None = None) -> FakeResult:
        self.writes.append((query, params))
        return self.write_results.pop(0) if self.write_results else FakeResult()

    def ro_query(
        self, query: str, params: dict[str, object] | None = None
    ) -> FakeResult:
        self.reads.append((query, params))
        return self.read_results.pop(0) if self.read_results else FakeResult()


class FakeClient:
    def __init__(self) -> None:
        self.graphs: dict[str, FakeGraph] = {}
        self.selected: list[str] = []

    def select_graph(self, graph_id: str) -> FakeGraph:
        self.selected.append(graph_id)
        return self.graphs.setdefault(graph_id, FakeGraph())


def candidate() -> ProgressiveRelation:
    return ProgressiveRelation(
        relation_id="rel-1",
        organization_id="org-a",
        source_node_id="issue:signing-authority",
        target_node_id="prov-4",
        edge_type=ProgressiveEdgeType.CONSTRAINED_BY,
        truth_class=TruthClass.INFERRED,
        source_episode_id="episode-182",
        evidence_version_ids=("prov-4-v2",),
        confidence=0.91,
        generated_by_model="qwen",
        model_profile="navigation",
        prompt_version="p1",
        agent_version="a1",
    )


def event(
    status: TrustStatus = TrustStatus.CANDIDATE, previous: TrustStatus | None = None
) -> MemoryEvent:
    return MemoryEvent(
        event_id="event-1",
        organization_id="org-a",
        actor_id="agent-1",
        action=(
            MemoryEventType.CANDIDATE_CREATED
            if previous is None
            else MemoryEventType.CANDIDATE_APPROVED
        ),
        target_id="rel-1",
        previous_state=previous,
        new_state=status,
        reason=None,
        source_episode_id="episode-182",
        timestamp=datetime(2026, 8, 11, 12, 0, tzinfo=UTC),
    )


def relation_row(status: TrustStatus = TrustStatus.CANDIDATE) -> list[object]:
    approved = status == TrustStatus.EXPERT_APPROVED
    return [
        "rel-1",
        "org-a",
        "issue:signing-authority",
        "prov-4",
        "constrained_by",
        "inferred",
        "episode-182",
        ["prov-4-v2"],
        "research",
        "canonical",
        0.91,
        status.value,
        "expert-1" if approved else None,
        "2026-08-11T12:00:00+00:00" if approved else None,
        "reviewed" if approved else None,
        "org-a" if approved else None,
        "qwen",
        "navigation",
        "p1",
        "a1",
    ]


class FalkorAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = FakeClient()
        self.repository = FalkorResearchGraphRepository(self.client)

    def graph(self, organization_id: str = "org-a") -> FakeGraph:
        return self.client.select_graph(self.repository.graph_name_for(organization_id))

    def test_graph_names_are_safe_deterministic_and_tenant_specific(self) -> None:
        first = self.repository.graph_name_for("org-a")

        self.assertEqual(first, self.repository.graph_name_for("org-a"))
        self.assertNotEqual(first, self.repository.graph_name_for("org-b"))
        self.assertNotIn("org-a", first)
        with self.assertRaises(DomainError):
            FalkorResearchGraphRepository(self.client, "bad prefix")

    def test_get_relation_uses_read_only_parameterized_query(self) -> None:
        graph = self.graph()
        graph.read_results.append(FakeResult([relation_row()]))

        relation = self.repository.get_relation("org-a", "rel-1")

        self.assertEqual(relation.evidence_version_ids, ("prov-4-v2",))
        query, params = graph.reads[0]
        self.assertIn("$organization_id", query)
        self.assertNotIn("org-a", query)
        self.assertEqual(params, {"organization_id": "org-a", "relation_id": "rel-1"})

    def test_get_missing_relation_raises_not_found(self) -> None:
        with self.assertRaises(NotFoundError):
            self.repository.get_relation("org-a", "missing")

    def test_create_is_atomic_parameterized_and_audited(self) -> None:
        graph = self.graph()
        graph.write_results.append(FakeResult([relation_row()]))
        proposal = candidate()

        self.repository.save_relation(proposal, event())

        query, params = graph.writes[0]
        self.assertIn("MERGE (r:ResearchRelation", query)
        self.assertIn("CREATE (event:MemoryEvent", query)
        self.assertNotIn("org-a", query)
        assert params is not None
        self.assertEqual(params["organization_id"], "org-a")
        self.assertEqual(params["evidence_version_ids"], ["prov-4-v2"])

    def test_optimistic_write_conflict_is_reported(self) -> None:
        with self.assertRaisesRegex(ConflictError, "already exists"):
            self.repository.save_relation(candidate(), event())

    def test_active_relation_decodes_retained_approval(self) -> None:
        graph = self.graph()
        graph.read_results.append(
            FakeResult([relation_row(TrustStatus.EXPERT_APPROVED)])
        )

        relations = self.repository.active_relations("org-a")

        self.assertEqual(len(relations), 1)
        self.assertIsInstance(relations[0].approval, Approval)
        assert relations[0].approval is not None
        self.assertEqual(relations[0].approval.approved_by, "expert-1")
        self.assertEqual(graph.reads[0][1]["status"], "expert_approved")

    def test_review_queue_query_is_tenant_and_status_scoped(self) -> None:
        graph = self.graph()
        graph.read_results.append(FakeResult([relation_row()]))

        relations = self.repository.list_relations(
            "org-a",
            frozenset({TrustStatus.CANDIDATE, TrustStatus.NEEDS_REVALIDATION}),
        )

        self.assertEqual(relations[0].relation_id, "rel-1")
        query, params = graph.reads[0]
        self.assertIn("r.status IN $statuses", query)
        self.assertNotIn("org-a", query)
        self.assertEqual(params["organization_id"], "org-a")
        self.assertEqual(params["statuses"], ["candidate", "needs_revalidation"])

    def test_version_dependency_query_uses_array_membership_and_status(self) -> None:
        graph = self.graph()
        graph.read_results.append(
            FakeResult([relation_row(TrustStatus.EXPERT_APPROVED)])
        )

        relations = self.repository.relations_using_version(
            "org-a", "prov-4-v2", TrustStatus.EXPERT_APPROVED
        )

        self.assertEqual(relations[0].relation_id, "rel-1")
        query, params = graph.reads[0]
        self.assertIn("$provision_version_id IN r.evidence_version_ids", query)
        self.assertEqual(params["status"], "expert_approved")

    def test_events_are_decoded_as_structured_audit_records(self) -> None:
        graph = self.graph()
        graph.read_results.append(
            FakeResult(
                [
                    [
                        "event-1",
                        "org-a",
                        "agent-1",
                        "candidate_created",
                        "rel-1",
                        None,
                        "candidate",
                        None,
                        "episode-182",
                        "2026-08-11T12:00:00+00:00",
                    ]
                ]
            )
        )

        events = self.repository.events("org-a")

        self.assertEqual(events[0].action, MemoryEventType.CANDIDATE_CREATED)
        self.assertEqual(events[0].timestamp.tzinfo, UTC)

    def test_event_and_relation_must_share_tenant_and_state(self) -> None:
        mismatched = MemoryEvent(
            "event-x",
            "org-b",
            "agent",
            MemoryEventType.CANDIDATE_CREATED,
            "rel-1",
            None,
            TrustStatus.CANDIDATE,
            None,
            "episode-182",
        )
        with self.assertRaisesRegex(ConflictError, "identity"):
            self.repository.save_relation(candidate(), mismatched)

    def test_ensure_indexes_only_creates_missing_indexes(self) -> None:
        graph = self.graph()
        graph.write_results.append(FakeResult([[1]]))
        graph.read_results.append(FakeResult([["ResearchRelation", ["relation_id"]]]))

        created = self.repository.ensure_indexes("org-a")

        self.assertEqual(len(created), 5)
        self.assertTrue(
            all(statement.startswith("CREATE INDEX FOR") for statement in created)
        )
        self.assertEqual(len(graph.writes), 6)


if __name__ == "__main__":
    unittest.main()
