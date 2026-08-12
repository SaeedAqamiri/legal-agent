import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from legal_agent_core.adapters import FalkorResearchGraphRepository
from legal_agent_core.research import (
    Actor,
    ProgressiveEdgeType,
    ProgressiveRelation,
    TrustStatus,
    TruthClass,
)
from legal_agent_core.services import ProgressiveMemoryService


class LiveFalkorDBTests(unittest.TestCase):
    client: object
    temporary_directory: TemporaryDirectory[str] | None

    def setUp(self) -> None:
        self.temporary_directory = None
        url = os.environ.get("FALKORDB_URL")
        if url:
            from falkordb import FalkorDB

            self.client = FalkorDB.from_url(url)
        else:
            try:
                from redislite.falkordb_client import FalkorDB as FalkorDBLite
            except ImportError as exc:
                self.skipTest("set FALKORDB_URL or install the falkordb-lite extra on Linux/macOS")
                raise AssertionError from exc
            self.temporary_directory = TemporaryDirectory()
            database_path = Path(self.temporary_directory.name) / "integration.db"
            self.client = FalkorDBLite(str(database_path))

        suffix = uuid4().hex[:12]
        self.organization_id = f"integration-{suffix}"
        self.repository = FalkorResearchGraphRepository(self.client, graph_prefix=f"legal_it_{suffix}")
        self.graph_name = self.repository.graph_name_for(self.organization_id)

    def tearDown(self) -> None:
        if not hasattr(self, "client"):
            return
        try:
            self.client.select_graph(self.graph_name).delete()
        finally:
            close = getattr(self.client, "close", None)
            if close is not None:
                close()
            shutdown = getattr(self.client, "shutdown", None)
            if shutdown is not None:
                shutdown()
            if self.temporary_directory is not None:
                self.temporary_directory.cleanup()

    def test_approval_and_revalidation_round_trip(self) -> None:
        created_indexes = self.repository.ensure_indexes(self.organization_id)
        self.assertGreaterEqual(len(created_indexes), 1)
        service = ProgressiveMemoryService(self.repository)
        candidate = ProgressiveRelation(
            relation_id="rel-live-1",
            organization_id=self.organization_id,
            source_node_id="issue:live",
            target_node_id="provision:live-v1",
            edge_type=ProgressiveEdgeType.GOVERNED_BY,
            truth_class=TruthClass.INFERRED,
            source_episode_id="episode-live-1",
            evidence_version_ids=("provision:live-v1",),
            generated_by_model="integration-test",
        )

        service.create_candidate(Actor("agent-live", self.organization_id), candidate)
        self.assertEqual(self.repository.active_relations(self.organization_id), ())

        expert = Actor("expert-live", self.organization_id, frozenset({"legal_expert"}))
        approved = service.approve(expert, candidate.relation_id, "live integration")
        self.assertEqual(approved.status, TrustStatus.EXPERT_APPROVED)
        self.assertEqual(len(self.repository.active_relations(self.organization_id)), 1)
        self.assertEqual(len(self.repository.events(self.organization_id)), 2)

        impacted = service.mark_version_superseded(
            expert, "provision:live-v1", "integration supersession"
        )
        self.assertEqual(impacted[0].status, TrustStatus.NEEDS_REVALIDATION)
        self.assertEqual(self.repository.active_relations(self.organization_id), ())


if __name__ == "__main__":
    unittest.main()
