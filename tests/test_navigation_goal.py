"""ACCEPTANCE GATES — "did navigation become lawyer-like?".

These are the hard tests that must ALL pass for us to claim the goal is met:
an agent that navigates the law like a programmer walks a codebase —
descend the conceptual tree, follow the deterministic call graph, consult the
learned notebook, and record WHY each hop was taken.

They are intentionally stricter than the feature unit tests: they assert the
*shape* of navigation (typed reasons, tree paths, separated claims), not just
that a function returns without error.

DB-backed gates skip when Postgres is unreachable so the suite still runs
offline; run them where the corpus lives for the real verdict.
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

from legal_agent_core.agentic.composite import CompositeTools
from legal_agent_core.agentic.envelope import RefStore, ToolStatus
from legal_agent_core.agentic.notebook_tools import NotebookTools
from legal_agent_core.agentic.tree_tools import TreeTools
from legal_agent_core.notebook import (
    NotebookPage,
    NotebookRoute,
    derive_candidate_relations,
)
from legal_agent_core.research import ResearchEpisode

DISPLAY_TREE = Path("out/display_tree.json")
DSN = os.environ.get(
    "LEGAL_AGENT_POSTGRES_DSN",
    "postgresql://legal_agent:legal_agent@127.0.0.1:5432/legal_agent",
)


def _db():
    try:
        import psycopg

        return psycopg.connect(DSN, autocommit=True)
    except Exception:  # noqa: BLE001 - offline suite
        return None


def _tree() -> dict | None:
    if not DISPLAY_TREE.exists():
        return None
    return json.loads(DISPLAY_TREE.read_text(encoding="utf-8"))


class TreeNavigationGate(unittest.TestCase):
    """Gate 1: the conceptual tree is walkable like a filesystem."""

    def setUp(self) -> None:
        tree = _tree()
        if tree is None:
            self.skipTest("display tree not built (run scripts/build_display_tree.py)")
        self.tools = TreeTools(tree)

    def test_list_tree_descends_root_branch_domain(self) -> None:
        root = self.tools.dispatch(RefStore(), "list_tree", {})
        self.assertEqual(root.status, ToolStatus.OK)
        children = root.items[0]["children"]
        branch_ids = [child["id"] for child in children if child["kind"] == "branch"]
        self.assertTrue(branch_ids, "root exposes no branches")
        # descend one level: a branch exposes domains
        branch = self.tools.dispatch(RefStore(), "list_tree", {"node_id": branch_ids[0]})
        self.assertTrue(any(c["kind"] == "domain" for c in branch.items[0]["children"]))

    def test_walk_tree_returns_a_basis_labelled_path_to_a_document(self) -> None:
        result = self.tools.dispatch(RefStore(), "walk_tree", {"topic": "مهریه"})
        self.assertEqual(result.status, ToolStatus.OK)
        self.assertTrue(result.items, "no tree path found for مهریه")
        path = result.items[0]["path"]
        self.assertGreaterEqual(len(path), 2)
        self.assertEqual(path[0]["kind"], "root")
        # every hop carries a basis (root may be None); leaves are documents
        self.assertTrue(any(step.get("basis") == "topic" for step in path))
        self.assertIn(path[-1]["kind"], {"instrument", "domain"})

    def test_derivation_children_are_nested_under_their_law(self) -> None:
        # a law with implementing regulations must show them as derivation kids
        found = False
        for node in self._iter(self.tools.tree):
            if node.get("kind") == "instrument" and any(
                c.get("basis") == "derivation" for c in node.get("children", [])
            ):
                found = True
                break
        self.assertTrue(found, "no derivation nesting found anywhere in the tree")

    @staticmethod
    def _iter(node):
        yield node
        for child in node.get("children", []):
            yield from TreeNavigationGate._iter(child)


class InternalStructureGate(unittest.TestCase):
    """Gate 2: a code's own book/chapter structure is real, not flat."""

    def setUp(self) -> None:
        self.db = _db()
        if self.db is None:
            self.skipTest("postgres unavailable")

    def tearDown(self) -> None:
        if self.db is not None:
            self.db.close()

    def test_madani_article_has_book_chapter_ancestry(self) -> None:
        row = self.db.execute(
            "SELECT instrument_id FROM canonical.legal_instruments WHERE title = 'قانون مدنی'"
        ).fetchone()
        if row is None:
            self.skipTest("قانون مدنی not ingested")
        instrument_id = row[0]
        parents = {
            pid: (label, ptype, parent)
            for pid, label, ptype, parent in self.db.execute(
                """SELECT provision_id, label, provision_type, parent_provision_id
                     FROM canonical.provisions WHERE instrument_id = %s""",
                (instrument_id,),
            ).fetchall()
        }
        article = next(
            (pid for pid, (label, _t, _p) in parents.items() if label.startswith("ماده ۱۰۸۲")),
            None,
        )
        if article is None:
            self.skipTest("ماده ۱۰۸۲ not present")
        types: set[str] = set()
        node = article
        depth = 0
        while node is not None and depth < 20:
            _label, ptype, parent = parents[node]
            types.add(ptype)
            node = parent
            depth += 1
        self.assertIn("chapter", types, "no chapter ancestor — structure still flat")
        self.assertGreaterEqual(depth, 3, "ancestry too shallow for a real code")


class ClaimSeparationGate(unittest.TestCase):
    """Gate 3: legal relations and display hints are separated by `claim`."""

    def setUp(self) -> None:
        self.db = _db()
        if self.db is None:
            self.skipTest("postgres unavailable")

    def tearDown(self) -> None:
        if self.db is not None:
            self.db.close()

    def test_every_edge_carries_a_claim_and_review_status(self) -> None:
        rows = self.db.execute(
            """SELECT edge_type, claim, review_status, count(*)
                 FROM canonical.graph_edges GROUP BY 1,2,3"""
        ).fetchall()
        self.assertTrue(rows)
        for edge_type, claim, review_status, _count in rows:
            self.assertIsNotNone(claim, f"{edge_type} has no claim label")
            self.assertIsNotNone(review_status, f"{edge_type} has no review_status")

    def test_display_hint_is_never_a_legal_relation(self) -> None:
        # subordinate_to must be labelled display_hint, not statutory_basis
        rows = dict(
            self.db.execute(
                """SELECT edge_type, claim FROM canonical.graph_edges
                    WHERE edge_type IN ('subordinate_to', 'implements')
                    GROUP BY 1,2"""
            ).fetchall()
        )
        self.assertEqual(rows.get("subordinate_to"), "display_hint")
        self.assertEqual(rows.get("implements"), "statutory_basis")

    def test_no_reference_edges_were_lost(self) -> None:
        total = self.db.execute("SELECT count(*) FROM canonical.graph_edges").fetchone()[0]
        # corpus baseline: the deterministic reference graph must survive intact
        self.assertGreaterEqual(total, 21000)


class NotebookGate(unittest.TestCase):
    """Gate 4: learned routes are recorded and read back."""

    ORG = "org-gate-test"

    def setUp(self) -> None:
        self.db = _db()
        if self.db is None:
            self.skipTest("postgres unavailable")

    def tearDown(self) -> None:
        if self.db is None:
            return
        with self.db.cursor() as cur:
            cur.execute("DELETE FROM notebook.routes WHERE organization_id = %s", (self.ORG,))
            cur.execute("DELETE FROM notebook.events WHERE organization_id = %s", (self.ORG,))
            cur.execute("DELETE FROM notebook.pages WHERE organization_id = %s", (self.ORG,))
        self.db.close()

    def test_confirmed_route_roundtrips(self) -> None:
        from legal_agent_core.adapters.postgres_notebook import (
            PostgresNotebookRepository,
        )

        repo = PostgresNotebookRepository(self.db)
        repo.upsert_route(
            self.ORG, "domain:خانواده", "inst_demo", hit=True, confirmed=True,
            source_episode_id="ep-gate",
        )
        repo.upsert_page(
            NotebookPage(self.ORG, "domain:خانواده", "domain", summary="مهریه و نکاح")
        )
        tools = NotebookTools(repo, self.ORG)
        routes = tools.dispatch(RefStore(), "notebook_route", {"topic_id": "domain:خانواده"})
        self.assertEqual(routes.items[0]["to_node_id"], "inst_demo")
        self.assertEqual(routes.items[0]["confirmed_hits"], 1)
        page = tools.dispatch(RefStore(), "open_notebook", {"node_id": "domain:خانواده"})
        self.assertEqual(page.status, ToolStatus.OK)

    def test_implicit_relations_are_derived_from_co_use(self) -> None:
        episode = ResearchEpisode(
            "ep-gate", self.ORG, "u", "c", "مهریه", __import__("datetime").date(2024, 1, 1)
        )
        candidates = derive_candidate_relations(episode, ("p1", "p2", "p3"))
        self.assertEqual(len(candidates), 3)  # C(3,2)
        self.assertTrue(all(c.edge_type.value == "commonly_check_with" for c in candidates))
        self.assertTrue(all(c.status.value == "candidate" for c in candidates))
        # deterministic ids make re-capture idempotent
        again = derive_candidate_relations(episode, ("p1", "p2", "p3"))
        self.assertEqual(
            [c.relation_id for c in candidates], [c.relation_id for c in again]
        )


class TypedReasonGate(unittest.TestCase):
    """Gate 5: every navigation tool declares a typed reason for the hop."""

    def test_key_tools_have_typed_reasons(self) -> None:
        from legal_agent_core.agentic.loop import _tool_reason

        expected = {
            "walk_tree": "structure",
            "list_tree": "structure",
            "get_document_structure": "structure",
            "get_related": "citation",
            "get_statute_family": "statutory_basis",
            "notebook_route": "notebook",
            "search_provisions": "search",
            "get_judgment_text": "precedent",
        }
        for tool, reason in expected.items():
            with self.subTest(tool=tool):
                self.assertEqual(_tool_reason(tool), reason)


class CompositeSurfaceGate(unittest.TestCase):
    """Gate 6: the agent's one surface exposes tree + notebook + graph."""

    def test_tree_and_notebook_are_advertised_and_routed(self) -> None:
        class _Canonical:
            hidden_tools = frozenset()

            def dispatch(self, store, name, args):
                raise AssertionError("must not fall through to canonical")

            def spec_available(self, name):
                return True

            def tool_catalog(self):
                return "canonical"

        tree = TreeTools(_tree()) if _tree() else None
        notebook = NotebookTools(_FakeRepo(), "org-a")
        tools = CompositeTools(_Canonical(), None, notebook, tree)
        self.assertIn("درخت مفهومی", tools.tool_catalog())
        self.assertIn("دفترچهٔ پژوهش", tools.tool_catalog())
        env = tools.dispatch(RefStore(), "notebook_index", {})
        self.assertEqual(env.envelope.status, ToolStatus.OK)


class _FakeRepo:
    def pages(self, organization_id, kind=None):
        return (NotebookPage(organization_id, "domain:خانواده", "domain", summary="مهریه"),)

    def get_page(self, organization_id, node_id):
        return NotebookPage(organization_id, node_id, "domain", summary="مهریه")

    def routes(self, organization_id, topic_id=None):
        return (NotebookRoute(organization_id, "domain:خانواده", "inst_demo", weight=1.0),)


if __name__ == "__main__":
    unittest.main()
