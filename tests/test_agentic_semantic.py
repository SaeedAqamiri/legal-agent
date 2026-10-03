"""Tests for the semantic layer: expand_search_terms + semantic_search."""

from __future__ import annotations

import unittest

from agentic_fixture import APPLICABLE, build_canonical

from legal_agent_core.agentic import LegalResearchTools
from legal_agent_core.agentic.envelope import RefStore, ToolStatus
from legal_agent_core.agentic.semantic import SemanticSpanIndex
from legal_agent_core.errors import DomainError


class FakeExpander:
    def __init__(self, terms: list[str] | None = None, fail: bool = False) -> None:
        self.terms = terms or ["حق کسب و پیشه", "محل کسب"]
        self.fail = fail

    def expand(self, query: str) -> list[str]:
        if self.fail:
            raise DomainError("gateway down")
        return self.terms


class FakeEmbeddingClient:
    """Deterministic 2-D embeddings driven by keyword presence."""

    model = "fake-embeddings"

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            vectors.append(
                [
                    1.0 if ("بازداشت" in text or "اموال" in text) else 0.0,
                    1.0 if ("بازداشت" in text or "اموال" in text) else 0.0,
                ]
            )
        return vectors


class ExpandSearchTermsTests(unittest.TestCase):
    def test_without_expander_returns_error_envelope(self) -> None:
        tools = LegalResearchTools(build_canonical())
        result = tools.dispatch(RefStore(), "expand_search_terms", {"query": "سرقفلی"})
        self.assertEqual(result.envelope.status, ToolStatus.ERROR)

    def test_with_expander_returns_terms(self) -> None:
        tools = LegalResearchTools(build_canonical(), term_expander=FakeExpander())
        result = tools.dispatch(RefStore(), "expand_search_terms", {"query": "سرقفلی"})
        self.assertEqual(result.envelope.status, ToolStatus.OK)
        queries = [item["suggested_query"] for item in result.envelope.items]
        self.assertIn("حق کسب و پیشه", queries)

    def test_expander_failure_is_error_envelope(self) -> None:
        tools = LegalResearchTools(build_canonical(), term_expander=FakeExpander(fail=True))
        result = tools.dispatch(RefStore(), "expand_search_terms", {"query": "سرقفلی"})
        self.assertEqual(result.envelope.status, ToolStatus.ERROR)


class SemanticSearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = build_canonical()
        self.index = SemanticSpanIndex(self.repository, FakeEmbeddingClient())
        self.tools = LegalResearchTools(self.repository, semantic_index=self.index)
        self.store = RefStore()
        self.at = APPLICABLE.isoformat()

    def test_without_index_returns_error_envelope(self) -> None:
        tools = LegalResearchTools(build_canonical())
        result = tools.dispatch(
            RefStore(), "semantic_search", {"query": "مهلت", "applicable_time": self.at}
        )
        self.assertEqual(result.envelope.status, ToolStatus.ERROR)
        self.assertIn("LEGAL_AGENT_EMBEDDINGS", "; ".join(result.envelope.errors))

    def test_semantic_search_ranks_and_builds_evidence(self) -> None:
        result = self.tools.dispatch(
            RefStore(),
            "semantic_search",
            {"query": "بازداشت اموال", "applicable_time": self.at},
        )
        self.assertEqual(result.envelope.status, ToolStatus.OK)
        self.assertTrue(result.envelope.items)
        self.assertTrue(result.evidence)
        top = result.envelope.items[0]
        self.assertIn("source_span_id", top)
        self.assertEqual(result.evidence[0].retrieval_method, "agentic_semantic")
        # ranked by similarity: first item has the highest score
        scores = [item["score"] for item in result.envelope.items]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_document_filter_and_unknown_document(self) -> None:
        result = self.tools.dispatch(
            RefStore(),
            "semantic_search",
            {"query": "بازداشت", "applicable_time": self.at, "document": "سند غایب"},
        )
        self.assertEqual(result.envelope.status, ToolStatus.NOT_FOUND)


if __name__ == "__main__":
    unittest.main()
