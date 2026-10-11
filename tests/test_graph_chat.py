"""Tests for the graph-guide chat: tools, loop, and the SSE endpoint."""

from __future__ import annotations

import json
import unittest
from itertools import count

from agentic_fixture import build_canonical
from fastapi.testclient import TestClient

from legal_agent_core.agentic.envelope import RefStore, ToolStatus
from legal_agent_core.agentic.graph_chat import (
    GraphChatConfig,
    GraphChatLoop,
    GraphChatService,
)
from legal_agent_core.agentic.graph_tools import GraphTools
from legal_agent_core.agentic.planner import PlannerDecision, PlannerTurn
from legal_agent_core.answering import CitationPipeline
from legal_agent_core.api import APIContainer, create_app
from legal_agent_core.application import (
    InMemoryResearchRecordRepository,
    KnowledgeReviewApplicationService,
    ResearchApplicationService,
)
from legal_agent_core.errors import DomainError
from legal_agent_core.evaluation import EvaluationRunner
from legal_agent_core.in_memory import (
    InMemoryResearchGraphRepository,
)
from legal_agent_core.navigation import NavigationLoop
from legal_agent_core.observability import MetricsRegistry
from legal_agent_core.retrieval import MultiSignalEvidenceRetriever
from legal_agent_core.security import AuthorizationService, Principal, Role
from legal_agent_core.services import ProgressiveMemoryService
from legal_agent_core.verification import (
    DraftAnswer,
    EvidenceVerifier,
)


class FakeResult:
    def __init__(self, header: list[tuple], rows: list[list]) -> None:
        self.header = header
        self.result_set = rows


class FakeGraph:
    """Routes canned rows by a substring of the query, like the real projection."""

    def __init__(self, routes: dict[str, list[list]] | None = None) -> None:
        self.routes = routes or {}
        self.calls: list[tuple[str, dict | None]] = []

    def ro_query(self, query: str, params: dict | None = None) -> FakeResult:
        self.calls.append((query, params))
        for key, rows in self.routes.items():
            if key in query:
                header = [(f"c{i}", c) for i, c in enumerate(rows[0])] if rows else []
                return FakeResult(header, rows)
        return FakeResult([], [])

    def query(self, query: str, params: dict | None = None) -> FakeResult:  # pragma: no cover
        raise AssertionError("graph chat must only run ro_query")


def scripted_planner(decisions: list[dict]):
    """Planner double replaying a decision list, then stalling on the last one."""

    class _Scripted:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.index = 0

        def decide(self, system_prompt: str, user_prompt: str) -> PlannerTurn:
            del system_prompt
            self.calls.append(user_prompt)
            payload = decisions[min(self.index, len(decisions) - 1)]
            self.index += 1
            return PlannerTurn(
                decision=PlannerDecision(**payload),
                contract_error=None,
                raw_text=json.dumps(payload),
                input_tokens=10,
                output_tokens=10,
                provider_id="test",
                model="scripted",
                model_profile_id="test",
                model_profile_version=1,
                prompt_id="test",
                prompt_version=1,
                agent_version="test/1",
                response_id=f"resp-{self.index}",
            )

    return _Scripted()


class GraphToolsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.graph = FakeGraph(
            {
                "size(i.title)": [["instrument-1", "قانون کار", 3, "statute"]],
                "labels(n)[0]": [["LegalInstrument", 608], ["Provision", 48110]],
                "count(i) AS cnt": [[1, 1], [3, 531]],
                "type(r)": [["CONTAINS", 48110], ["EXPLICITLY_REFERENCES", 6880]],
                "BELONGS_TO_DOMAIN": [["کیفری", 68]],
            }
        )
        self.tools = GraphTools(self.graph)
        self.store = RefStore()

    def test_graph_stats_returns_section_counts(self) -> None:
        envelope = self.tools.dispatch(self.store, "graph_stats", {})
        self.assertIs(envelope.status, ToolStatus.OK)
        metrics = {item["metric"] for item in envelope.items}
        self.assertIn("nodes", metrics)
        self.assertIn("instruments_by_tier", metrics)
        tier_items = [i for i in envelope.items if i["metric"] == "instruments_by_tier"]
        self.assertEqual(tier_items[0]["tier_label"], "اساسی")

    def test_find_nodes_normalizes_persian_label_alias(self) -> None:
        envelope = self.tools.dispatch(
            self.store, "find_nodes", {"query": "کار", "label": "قانون"}
        )
        self.assertIs(envelope.status, ToolStatus.OK)
        self.assertEqual(envelope.items[0]["label"], "instrument")
        self.assertEqual(envelope.items[0]["tier_label"], "عادی (قانون)")

    def test_find_nodes_rejects_unknown_label(self) -> None:
        with self.assertRaises(DomainError):
            self.tools.dispatch(
                self.store, "find_nodes", {"query": "کار", "label": "planet"}
            )

    def test_node_neighbors_reports_directions(self) -> None:
        graph = FakeGraph(
            {
                "MATCH (n)-[r]->(m)": [
                    ["CONTAINS", "LegalInstrument", "instrument-1", "قانون کار"]
                ],
                "MATCH (n)<-[r]-(m)": [
                    ["EXPLICITLY_REFERENCES", "Provision", "p-9", "ماده ۹"]
                ],
            }
        )
        tools = GraphTools(graph)
        envelope = tools.dispatch(RefStore(), "node_neighbors", {"node_id": "p-1"})
        self.assertIs(envelope.status, ToolStatus.OK)
        directions = {item["direction"] for item in envelope.items}
        self.assertEqual(directions, {"out", "in"})
        self.assertIn("EXPLICITLY_REFERENCES", [item["rel"] for item in envelope.items])

    def test_node_neighbors_filters_rel_types(self) -> None:
        graph = FakeGraph({"MATCH (n)-[r]->(m)": [["CONTAINS", "X", "i-1", "t"]]})
        tools = GraphTools(graph)
        envelope = tools.dispatch(
            RefStore(),
            "node_neighbors",
            {"node_id": "p-1", "rel_types": ["explicitly_references"]},
        )
        self.assertIsNotNone(envelope)
        self.assertEqual(graph.calls[0][1]["rels"], ["EXPLICITLY_REFERENCES"])

    def test_find_paths_inlines_validated_depth(self) -> None:
        graph = FakeGraph(
            {
                "shortestPath": [
                    [
                        ["j-1", "instrument-1"],
                        ["رأی", "قانون کار"],
                        ["judgment", "instrument"],
                        ["CITES"],
                    ]
                ]
            }
        )
        tools = GraphTools(graph)
        with self.assertRaises(DomainError):
            tools.dispatch(
                RefStore(),
                "find_paths",
                {"source_id": "j-1", "target_id": "instrument-1", "max_depth": 5},
            )
        envelope = tools.dispatch(
            RefStore(), "find_paths", {"source_id": "j-1", "target_id": "instrument-1"}
        )
        self.assertIs(envelope.status, ToolStatus.OK)
        self.assertEqual(envelope.items[0]["rels"], ["CITES"])
        self.assertIn("1..3", graph.calls[-1][0])

    def test_run_cypher_blocks_writes_and_multi_statements(self) -> None:
        for query in ("CREATE (n)", "MERGE (n)", "MATCH (n) DETACH DELETE n", "MATCH (n) RETURN n; MATCH (m) RETURN m"):
            with self.assertRaises(DomainError):
                self.tools.dispatch(self.store, "run_cypher", {"query": query})

    def test_run_cypher_returns_error_envelope_on_query_failure(self) -> None:
        class Boom(FakeGraph):
            def ro_query(self, query, params=None):
                raise RuntimeError("parse error near RETURN")

        tools = GraphTools(Boom())
        envelope = tools.dispatch(
            RefStore(), "run_cypher", {"query": "MATCH (n) RETURN nonsense"}
        )
        self.assertIs(envelope.status, ToolStatus.ERROR)
        self.assertIn("cypher failed", envelope.errors[0])

    def test_run_cypher_truncates_rows(self) -> None:
        graph = FakeGraph({"RETURN": [[i] for i in range(10)]})
        tools = GraphTools(graph, max_cypher_rows=5)
        envelope = tools.dispatch(
            RefStore(), "run_cypher", {"query": "MATCH (n) RETURN n"}
        )
        self.assertIs(envelope.status, ToolStatus.PARTIAL)
        self.assertEqual(envelope.returned_count, 5)
        self.assertFalse(envelope.complete)

    def test_search_laws_ranks_title_over_summary_and_returns_summaries(self) -> None:
        graph = FakeGraph(
            {
                "toLower(i.title) CONTAINS": [
                    ["inst-2", "قانون دیگر", "حفاظت از محیط زیست", "کیفری", 3, "statute"],
                    ["inst-1", "قانون محیط زیست", "خلاصه الف", "محیط زیست", 3, "statute"],
                ]
            }
        )
        tools = GraphTools(graph)
        envelope = tools.dispatch(RefStore(), "search_laws", {"topic": "محیط زیست"})
        self.assertIs(envelope.status, ToolStatus.OK)
        self.assertEqual([item["id"] for item in envelope.items], ["inst-1", "inst-2"])
        self.assertEqual(envelope.meta["matched_domains"], ["محیط زیست"])
        self.assertIn("summary", envelope.items[0])

    def test_search_laws_empty_when_no_token_matches(self) -> None:
        tools = GraphTools(FakeGraph({"toLower(i.title) CONTAINS": []}))
        envelope = tools.dispatch(RefStore(), "search_laws", {"topic": "ناموجود"})
        self.assertIs(envelope.status, ToolStatus.EMPTY)

    def test_list_provisions_filters_and_reports_filtered_total(self) -> None:
        graph = FakeGraph(
            {
                "RETURN count(p)": [[3]],
                "RETURN p.id, p.label": [["p-1", "ماده ۱", "1", "خلاصه"]],
            }
        )
        tools = GraphTools(graph)
        envelope = tools.dispatch(
            RefStore(), "list_provisions", {"instrument_id": "i-1", "query": "آلودگی"}
        )
        self.assertIs(envelope.status, ToolStatus.OK)
        self.assertEqual(envelope.total_count, 3)
        self.assertEqual(envelope.items[0]["label"], "ماده ۱")

    def test_get_provision_text_without_provider_is_honest_error(self) -> None:
        tools = GraphTools(FakeGraph())
        envelope = tools.dispatch(
            RefStore(), "get_provision_text", {"provision_id": "p-1"}
        )
        self.assertIs(envelope.status, ToolStatus.ERROR)
        self.assertIn("canonical", envelope.errors[0])

    def test_get_provision_text_returns_full_text(self) -> None:
        tools = GraphTools(
            FakeGraph(),
            provision_text=lambda pid: {
                "text": "متن کامل ماده",
                "label": "ماده ۱",
                "instrument_id": "i-1",
                "instrument_title": "قانون هوای پاک",
                "truncated": False,
            },
        )
        envelope = tools.dispatch(
            RefStore(), "get_provision_text", {"provision_id": "p-1"}
        )
        self.assertIs(envelope.status, ToolStatus.OK)
        self.assertEqual(envelope.items[0]["text"], "متن کامل ماده")
        self.assertEqual(envelope.items[0]["instrument_title"], "قانون هوای پاک")


class GraphChatLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tools = GraphTools(
            FakeGraph({"labels(n)[0]": [["LegalInstrument", 608]]})
        )

    def test_tool_then_answer_flow_emits_progress(self) -> None:
        planner = scripted_planner(
            [
                {"action": "tool", "tool": "graph_stats", "args": {}, "thought": "شمارش"},
                {
                    "action": "answer",
                    "answer": "۶۰۸ قانون داریم.",
                    "nodes": [{"id": "instrument-1", "label": "instrument", "title": "قانون کار"}],
                },
            ]
        )
        service = GraphChatService(GraphChatLoop(self.tools, planner))
        frames = list(service.stream("چند قانون داریم؟"))
        result = frames[-1]["result"]
        types = [frame["type"] for frame in frames]
        self.assertEqual(types[-1], "completed")
        self.assertIn("tool_call", types)
        self.assertIn("answering", types)
        self.assertTrue(result["completed"])
        self.assertEqual(result["answer"], "۶۰۸ قانون داریم.")
        self.assertEqual(result["nodes"][0]["id"], "instrument-1")

    def test_history_lands_in_transcript(self) -> None:
        planner = scripted_planner([{"action": "answer", "answer": "خب"}])
        loop = GraphChatLoop(self.tools, planner)
        loop.run(
            message="ادامه بده",
            history=[
                {"role": "user", "content": "قانون کار چند ماده دارد؟"},
                {"role": "assistant", "content": "۲۰۰ ماده."},
            ],
        )
        transcript = planner.calls[0]
        self.assertIn("گفتگوی پیشین", transcript)
        self.assertIn("۲۰۰ ماده.", transcript)
        self.assertIn("ادامه بده", transcript)

    def test_planner_failure_reports_honest_non_answer(self) -> None:
        class FailingPlanner:
            def decide(self, system_prompt: str, user_prompt: str) -> PlannerTurn:
                raise DomainError("llm down")

        result = GraphChatLoop(self.tools, FailingPlanner()).run(message="سلام")
        self.assertFalse(result.completed)
        self.assertIn("نمی‌توانم", result.answer)

    def test_budget_exhaustion_reports_honest_fallback(self) -> None:
        planner = scripted_planner(
            [{"action": "tool", "tool": "graph_stats", "args": {}}]
        )
        loop = GraphChatLoop(
            self.tools, planner, GraphChatConfig(max_steps=2, wall_seconds=10)
        )
        result = loop.run(message="چند قانون؟")
        self.assertFalse(result.completed)
        self.assertEqual(len(result.trace), 2)

    def test_malformed_nodes_are_dropped_not_fatal(self) -> None:
        planner = scripted_planner(
            [
                {
                    "action": "answer",
                    "answer": "پاسخ",
                    "nodes": [{"id": ""}, "bad", {"id": "n-1", "label": "provision"}],
                }
            ]
        )
        result = GraphChatLoop(self.tools, planner).run(message="پرسش")
        self.assertEqual([node["id"] for node in result.nodes], ["n-1"])

    def test_nodes_fall_back_to_tool_results_when_model_omits_them(self) -> None:
        tools = GraphTools(
            FakeGraph({"i.title": [["inst-1", "قانون کار", 3, "statute"]]})
        )
        planner = scripted_planner(
            [
                {"action": "tool", "tool": "find_nodes", "args": {"query": "کار", "label": "قانون"}},
                {"action": "answer", "answer": "قانون کار"},
            ]
        )
        result = GraphChatLoop(tools, planner).run(message="قانون کار")
        self.assertEqual([node["id"] for node in result.nodes], ["inst-1"])
        self.assertEqual(result.nodes[0]["label"], "instrument")


class DeterministicComposer:
    def compose(self, question: str, applicable_time, evidence: tuple):
        del question, applicable_time, evidence
        return DraftAnswer("answer-1", "پاسخ آزمایشی", ())


class GraphChatEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        canonical = build_canonical()
        graph = InMemoryResearchGraphRepository()
        authorization = AuthorizationService()
        metrics = MetricsRegistry()
        identifiers = count(1)
        research = ResearchApplicationService(
            NavigationLoop(
                MultiSignalEvidenceRetriever(canonical, graph),
                DeterministicComposer(),
                EvidenceVerifier(canonical),
            ),
            CitationPipeline(canonical),
            InMemoryResearchRecordRepository(),
            authorization,
            metrics,
            id_factory=lambda: str(next(identifiers)),
        )
        memory = ProgressiveMemoryService(graph)
        reviews = KnowledgeReviewApplicationService(memory, authorization, metrics)
        principal = Principal(
            "local-user",
            "org-a",
            frozenset({Role.RESEARCHER, Role.LEGAL_EXPERT, Role.KNOWLEDGE_STEWARD}),
        )

        class StubService:
            def stream(self, message, history=()):
                yield {
                    "type": "tool_call",
                    "step": 1,
                    "tool": "graph_stats",
                    "args": {},
                    "status": "ok",
                    "returned_count": 1,
                    "total_count": None,
                    "complete": True,
                    "latency_ms": 1,
                }
                yield {
                    "type": "completed",
                    "result": {
                        "answer": "۶۰۸ قانون داریم.",
                        "nodes": [{"id": "instrument-1", "label": "instrument", "title": "قانون"}],
                        "steps": [],
                        "completed": True,
                    },
                }

        self.client = TestClient(
            create_app(
                APIContainer(
                    research,
                    reviews,
                    EvaluationRunner(research, authorization),
                    authorization,
                    authorization,
                    metrics,
                    default_principal=principal,
                    graph_chat=StubService(),
                )
            )
        )
        self.unconfigured_client = TestClient(
            create_app(
                APIContainer(
                    research,
                    reviews,
                    EvaluationRunner(research, authorization),
                    authorization,
                    authorization,
                    metrics,
                    default_principal=principal,
                )
            )
        )

    def test_chat_stream_emits_progress_then_answer_and_done(self) -> None:
        with self.client.stream(
            "POST",
            "/v1/organizations/org-a/graph/chat/stream",
            json={
                "message": "چند قانون داریم؟",
                "history": [{"role": "user", "content": "سلام"}],
            },
        ) as response:
            self.assertEqual(response.status_code, 200)
            self.assertIn("text/event-stream", response.headers["content-type"])
            names: list[str] = []
            answer = None
            for line in response.iter_lines():
                if line.startswith("event: "):
                    names.append(line[len("event: "):])
                elif line.startswith("data: ") and names and names[-1] == "answer":
                    answer = json.loads(line[len("data: "):])
        self.assertEqual(names[0], "tool_call")
        self.assertEqual(names[-1], "done")
        self.assertIn("answer", names)
        self.assertEqual(answer["data"]["answer"], "۶۰۸ قانون داریم.")
        self.assertEqual(answer["data"]["nodes"][0]["id"], "instrument-1")

    def test_chat_endpoint_requires_configuration(self) -> None:
        response = self.unconfigured_client.post(
            "/v1/organizations/org-a/graph/chat/stream",
            json={"message": "سلام"},
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("not configured", response.json()["detail"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
