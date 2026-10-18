from __future__ import annotations

import unittest

from legal_agent_core.agentic.envelope import RefStore, ToolStatus
from legal_agent_core.agentic.notebook_tools import NOTEBOOK_TOOL_SPECS, NotebookTools
from legal_agent_core.notebook import NotebookPage, NotebookRoute


class _FakeRepo:
    def __init__(self, pages=(), routes=()):
        self._pages = {p.node_id: p for p in pages}
        self._routes = list(routes)

    def pages(self, organization_id, kind=None):
        return tuple(self._pages.values())

    def get_page(self, organization_id, node_id):
        return self._pages.get(node_id)

    def routes(self, organization_id, topic_id=None):
        if topic_id is None:
            return tuple(self._routes)
        return tuple(r for r in self._routes if r.topic_id == topic_id)


class NotebookToolsTests(unittest.TestCase):
    def _tools(self):
        pages = [
            NotebookPage("org-a", "branch:family", "branch", summary="خانواده و مدنی"),
            NotebookPage("org-a", "domain:خانواده", "domain", summary="مهریه و نکاح"),
        ]
        routes = [
            NotebookRoute("org-a", "domain:خانواده", "inst_law", weight=2.0, confirmed_hits=2),
        ]
        return NotebookTools(_FakeRepo(pages, routes), "org-a")

    def test_specs_present(self) -> None:
        names = {spec.name for spec in NOTEBOOK_TOOL_SPECS}
        self.assertEqual(names, {"notebook_index", "open_notebook", "notebook_route"})

    def test_index_returns_branch_and_domain_pages(self) -> None:
        env = self._tools().dispatch(RefStore(), "notebook_index", {})
        self.assertEqual(env.status, ToolStatus.OK)
        self.assertEqual(env.total_count, 2)

    def test_route_lists_confirmed_paths(self) -> None:
        env = self._tools().dispatch(RefStore(), "notebook_route", {"topic_id": "domain:خانواده"})
        self.assertEqual(env.total_count, 1)
        self.assertEqual(env.items[0]["to_node_id"], "inst_law")
        self.assertEqual(env.items[0]["confirmed_hits"], 2)

    def test_missing_page_is_not_found(self) -> None:
        env = self._tools().dispatch(RefStore(), "open_notebook", {"node_id": "nope"})
        self.assertEqual(env.status, ToolStatus.NOT_FOUND)

    def test_composite_routes_notebook_tools_and_advertises_them(self) -> None:
        from legal_agent_core.agentic.composite import CompositeTools

        class _Canonical:
            hidden_tools = frozenset()

            def dispatch(self, store, name, args):
                raise AssertionError("canonical must not handle notebook tools")

            def spec_available(self, name):
                return True

            def tool_catalog(self):
                return "canonical"

        tools = CompositeTools(_Canonical(), None, self._tools())
        env = tools.dispatch(RefStore(), "notebook_index", {})
        self.assertEqual(env.envelope.status, ToolStatus.OK)
        self.assertIn("دفترچهٔ پژوهش", tools.tool_catalog())
        self.assertTrue(tools.spec_available("notebook_route"))


if __name__ == "__main__":
    unittest.main()
