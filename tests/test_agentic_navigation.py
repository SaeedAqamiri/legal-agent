"""Tests for the navigation tool set (grep / read / toc / library / related)."""

from __future__ import annotations

import unittest

from agentic_fixture import APPLICABLE, build_canonical

from legal_agent_core.agentic.envelope import RefStore, ToolStatus
from legal_agent_core.agentic.tools import LegalResearchTools, tool_catalog
from legal_agent_core.canonical import (
    SourceSpan,
)


def _add_second_page_span(repository) -> str:
    """Simulate a page-level span: the tail of ماده ۳۳۶ v2 lives on page 18."""
    tail = "تبصره: مهلت مذکور برای اشخاص مقیم خارج سی روز دیگر است."
    span_id = "span-demo-336-v2-p18"
    repository.add_source_span(
        SourceSpan(
            span_id,
            "source-demo",
            "document-demo-v2",
            "provision-demo-336-v2",
            18,
            tail,
            char_start=58,
            char_end=58 + len(tail),
        )
    )
    return span_id


def _add_reference(repository) -> None:
    from agentic_fixture import add_resolved_reference

    add_resolved_reference(repository)


class NavigationToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = build_canonical()
        self.page18_span_id = _add_second_page_span(self.repository)
        _add_reference(self.repository)
        self.tools = LegalResearchTools(self.repository)
        self.store = RefStore()
        self.at = APPLICABLE.isoformat()

    def _dispatch(self, name: str, **args):
        return self.tools.dispatch(self.store, name, args)

    # ------------------------------------------------------------ find_in_document

    def test_find_in_document_returns_page_accurate_hits(self) -> None:
        result = self._dispatch(
            "find_in_document",
            query="مهلت تبصره خارج",
            applicable_time=self.at,
        )
        self.assertEqual(result.envelope.status, ToolStatus.OK)
        top = result.envelope.items[0]
        self.assertEqual(top["page"], 18)
        self.assertEqual(top["source_span_id"], self.page18_span_id)
        self.assertTrue(result.evidence)
        self.assertEqual(result.evidence[0].page, 18)
        self.assertEqual(result.evidence[0].retrieval_method, "agentic_grep")

    def test_find_in_document_requires_all_tokens(self) -> None:
        # AND-first: strict matches exist → they win. Zero strict matches →
        # graded PARTIAL leads (best token overlap) instead of a dead end.
        strict = self._dispatch(
            "find_in_document",
            query="مهلت مذکور خارج",
            applicable_time=self.at,
        )
        self.assertEqual(strict.envelope.status, ToolStatus.OK)  # exact hits win
        for item in strict.envelope.items:
            self.assertIn("خارج", item["text"])

        partial = self._dispatch(
            "find_in_document",
            query="مهلت زلزله سیلاب",
            applicable_time=self.at,
        )
        self.assertEqual(partial.envelope.status, ToolStatus.PARTIAL)
        self.assertFalse(partial.envelope.complete)
        self.assertTrue(partial.envelope.meta.get("partial_match"))
        self.assertTrue(partial.envelope.items)

    def test_find_in_document_page_range_filter(self) -> None:
        result = self._dispatch(
            "find_in_document",
            query="مهلت تجدیدنظر",
            applicable_time=self.at,
            page_from=20,
            page_to=40,
        )
        self.assertEqual(result.envelope.status, ToolStatus.EMPTY)

        inside = self._dispatch(
            "find_in_document",
            query="مهلت تجدیدنظر",
            applicable_time=self.at,
            page_from=10,
            page_to=20,
        )
        self.assertEqual(inside.envelope.status, ToolStatus.OK)

    def test_find_in_document_by_title_filter(self) -> None:
        result = self._dispatch(
            "find_in_document",
            query="مهلت تجدیدنظر",
            applicable_time=self.at,
            document="قانون نمونه آیین دادرسی",
        )
        self.assertEqual(result.envelope.status, ToolStatus.OK)
        for item in result.envelope.items:
            self.assertEqual(item["instrument_title"], "قانون نمونه آیین دادرسی")

    def test_find_in_document_unknown_document_is_not_found(self) -> None:
        result = self._dispatch(
            "find_in_document",
            query="مهلت",
            applicable_time=self.at,
            document="سند ناموجود",
        )
        self.assertEqual(result.envelope.status, ToolStatus.NOT_FOUND)

    # ---------------------------------------------------------------- read_span

    def test_read_span_returns_full_text_and_evidence(self) -> None:
        result = self._dispatch(
            "read_span",
            source_span_id=self.page18_span_id,
            applicable_time=self.at,
        )
        self.assertEqual(result.envelope.status, ToolStatus.OK)
        item = result.envelope.items[0]
        self.assertIn("تبصره", item["text"])
        self.assertEqual(item["page"], 18)
        self.assertEqual(len(result.evidence), 1)
        self.assertEqual(result.evidence[0].source_span_id, self.page18_span_id)

    def test_read_span_unknown_is_not_found(self) -> None:
        result = self._dispatch(
            "read_span", source_span_id="ghost", applicable_time=self.at
        )
        self.assertEqual(result.envelope.status, ToolStatus.NOT_FOUND)

    def test_read_span_accepts_evidence_id(self) -> None:
        # Regression: planners repeatedly passed evidence ids to read_span
        # (5 benchmark cases); the session index must resolve them.
        search = self._dispatch(
            "search_provisions",
            query="تبصره مقیم خارج",
            applicable_time=self.at,
        )
        evidence_id = search.envelope.items[0]["evidence_id"]
        expected_span = search.envelope.items[0]["source_span_id"]

        result = self._dispatch(
            "read_span",
            source_span_id=evidence_id,
            applicable_time=self.at,
        )
        self.assertEqual(result.envelope.status, ToolStatus.OK)
        item = result.envelope.items[0]
        self.assertEqual(item["source_span_id"], expected_span)
        self.assertEqual(item["page"], search.envelope.items[0]["page"])

    def test_read_span_unknown_evidence_id_gets_hint(self) -> None:
        result = self._dispatch(
            "read_span", source_span_id="ev-000000000000000000000000", applicable_time=self.at
        )
        self.assertEqual(result.envelope.status, ToolStatus.NOT_FOUND)
        self.assertIn("evidence_id", "; ".join(result.envelope.errors))

    # ---------------------------------------------------- list_document_contents

    def test_list_document_contents_by_title(self) -> None:
        result = self._dispatch(
            "list_document_contents",
            document="قانون نمونه آیین دادرسی",
            applicable_time=self.at,
        )
        self.assertEqual(result.envelope.status, ToolStatus.OK)
        labels = [item["label"] for item in result.envelope.items]
        self.assertIn("ماده ۳۳۶", labels)
        self.assertIn("ماده ۳۴۰", labels)
        pages = {
            item["label"]: item["page"] for item in result.envelope.items
        }
        self.assertEqual(pages["ماده ۳۳۶"], 17)

    def test_list_document_contents_unknown_document(self) -> None:
        result = self._dispatch(
            "list_document_contents",
            document="چیزی که نیست",
            applicable_time=self.at,
        )
        self.assertEqual(result.envelope.status, ToolStatus.NOT_FOUND)

    # ------------------------------------------------------------- list_documents

    def test_list_documents_lists_the_library(self) -> None:
        result = self._dispatch("list_documents", applicable_time=self.at)
        self.assertEqual(result.envelope.status, ToolStatus.OK)
        titles = [item["title"] for item in result.envelope.items]
        self.assertIn("قانون نمونه آیین دادرسی", titles)
        entry = next(
            item
            for item in result.envelope.items
            if item["title"] == "قانون نمونه آیین دادرسی"
        )
        self.assertTrue(entry["applies_at_time"])
        self.assertEqual(entry["provision_count"], 3)

    def test_list_documents_query_filters(self) -> None:
        result = self._dispatch(
            "list_documents", query="آیین دادرسی", applicable_time=self.at
        )
        titles = [item["title"] for item in result.envelope.items]
        self.assertEqual(titles, ["قانون نمونه آیین دادرسی"])

    def test_list_documents_no_match_is_empty(self) -> None:
        result = self._dispatch(
            "list_documents", query="کشتی بادبانی", applicable_time=self.at
        )
        self.assertEqual(result.envelope.status, ToolStatus.EMPTY)

    # ----------------------------------------------------------------- get_related

    def test_get_related_shows_outgoing_and_incoming(self) -> None:
        result = self._dispatch(
            "get_related",
            provision_id="provision-demo-340",
            applicable_time=self.at,
        )
        self.assertEqual(result.envelope.status, ToolStatus.OK)
        directions = [item["direction"] for item in result.envelope.items]
        self.assertIn("outgoing", directions)
        outgoing = next(
            item for item in result.envelope.items if item["direction"] == "outgoing"
        )
        self.assertEqual(outgoing["resolution_status"], "resolved")
        self.assertEqual(outgoing["resolved_target_provision_id"], "provision-demo-336")
        self.assertTrue(result.evidence)

        reverse = self._dispatch(
            "get_related",
            provision_id="provision-demo-336",
            applicable_time=self.at,
        )
        directions = [item["direction"] for item in reverse.envelope.items]
        self.assertIn("incoming", directions)

    def test_get_related_unknown_provision(self) -> None:
        result = self._dispatch(
            "get_related", provision_id="nope", applicable_time=self.at
        )
        self.assertEqual(result.envelope.status, ToolStatus.NOT_FOUND)

    # ------------------------------------------------- search uses best span

    def test_search_cites_best_matching_span(self) -> None:
        result = self._dispatch(
            "search_provisions",
            query="تبصره مقیم خارج",
            applicable_time=self.at,
        )
        self.assertEqual(result.envelope.status, ToolStatus.OK)
        top = result.envelope.items[0]
        self.assertEqual(top["source_span_id"], self.page18_span_id)
        self.assertEqual(top["page"], 18)
        self.assertIn("source_span_id", top)

    def test_catalog_includes_navigation_tools(self) -> None:
        catalog = tool_catalog()
        for name in (
            "find_in_document",
            "read_span",
            "list_document_contents",
            "list_documents",
            "get_related",
        ):
            self.assertIn(name, catalog)


if __name__ == "__main__":
    unittest.main()


class DynamicCatalogTests(unittest.TestCase):
    def test_unconfigured_tools_are_hidden(self) -> None:
        bare = LegalResearchTools(build_canonical())
        catalog = bare.tool_catalog()
        self.assertIn("search_provisions", catalog)
        self.assertNotIn("semantic_search", catalog)
        self.assertNotIn("expand_search_terms", catalog)

    def test_configured_tools_are_listed(self) -> None:
        from legal_agent_core.agentic.semantic import SemanticSpanIndex

        tools = LegalResearchTools(
            build_canonical(),
            term_expander=lambda query: [],
            semantic_index=SemanticSpanIndex(build_canonical(), FakeEmbeddingClient()),
        )
        catalog = tools.tool_catalog()
        self.assertIn("semantic_search", catalog)
        self.assertIn("expand_search_terms", catalog)


class FakeEmbeddingClient:
    model = "fake"

    def embed(self, texts):
        return [[1.0, 0.0] for _ in texts]
