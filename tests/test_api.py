import json
import unittest
from datetime import date
from itertools import count

from fastapi.testclient import TestClient

from legal_agent_core.answering import CitationPipeline
from legal_agent_core.api import APIContainer, create_app
from legal_agent_core.application import (
    InMemoryResearchRecordRepository,
    KnowledgeReviewApplicationService,
    ResearchApplicationService,
)
from legal_agent_core.canonical import (
    DocumentStatus,
    DocumentVersion,
    InstrumentType,
    LegalInstrument,
    Provision,
    ProvisionType,
    ProvisionVersion,
    SourceDocument,
    SourceSpan,
)
from legal_agent_core.evaluation import EvaluationRunner
from legal_agent_core.in_memory import (
    InMemoryCanonicalRepository,
    InMemoryResearchGraphRepository,
)
from legal_agent_core.navigation import NavigationLoop
from legal_agent_core.observability import MetricsRegistry
from legal_agent_core.research import (
    Actor,
    GraphNamespace,
    ProgressiveEdgeType,
    ProgressiveRelation,
    TruthClass,
)
from legal_agent_core.retrieval import MultiSignalEvidenceRetriever
from legal_agent_core.security import (
    AuthorizationService,
    Principal,
    Role,
    StaticTokenVerifier,
)
from legal_agent_core.services import ProgressiveMemoryService
from legal_agent_core.verification import (
    AnswerClaim,
    ClaimImportance,
    DraftAnswer,
    EvidenceVerifier,
)

APPLICABLE_TIME = date(2025, 1, 1)


class DeterministicComposer:
    def compose(
        self, question: str, applicable_time: date, evidence: tuple
    ) -> DraftAnswer:
        del question, applicable_time
        return DraftAnswer(
            "answer-1",
            "The payment deadline is ten days.",
            (
                AnswerClaim(
                    "claim-1",
                    "Payment is due within ten days.",
                    tuple(item.evidence_id for item in evidence),
                    ClaimImportance.CRITICAL,
                ),
            ),
        )


def canonical_repository() -> InMemoryCanonicalRepository:
    repository = InMemoryCanonicalRepository()
    repository.add_source_document(
        SourceDocument(
            "source-1",
            None,
            "law.pdf",
            "application/pdf",
            "sha256:test",
            64,
            "test",
            "file:///law.pdf",
        )
    )
    repository.add_instrument(
        LegalInstrument(
            "instrument-1", "Payment Act", "Payment Act", InstrumentType.STATUTE, "IR"
        )
    )
    repository.add_document_version(
        DocumentVersion(
            "document-1",
            "instrument-1",
            "source-1",
            DocumentStatus.EFFECTIVE,
            effective_from=date(2024, 1, 1),
        )
    )
    repository.add_provision(
        Provision(
            "provision-5", "instrument-1", ProvisionType.ARTICLE, "5", "Article 5"
        )
    )
    repository.add_provision_version(
        ProvisionVersion(
            "provision-5-v1",
            "provision-5",
            "document-1",
            "The payment deadline is ten days.",
            "The payment deadline is ten days.",
            DocumentStatus.EFFECTIVE,
            "test",
            date(2024, 1, 1),
        )
    )
    repository.add_source_span(
        SourceSpan(
            "span-1",
            "source-1",
            "document-1",
            "provision-5-v1",
            3,
            "The payment deadline is ten days.",
        )
    )
    return repository


class APITests(unittest.TestCase):
    def setUp(self) -> None:
        canonical = canonical_repository()
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
        memory.create_candidate(
            Actor("seed", "org-a"),
            ProgressiveRelation(
                "relation-1",
                "org-a",
                "issue-1",
                "provision-5",
                ProgressiveEdgeType.GOVERNED_BY,
                TruthClass.EXPLICIT,
                "episode-seed",
                ("provision-5-v1",),
                GraphNamespace.RESEARCH,
                GraphNamespace.CANONICAL,
            ),
        )
        reviews = KnowledgeReviewApplicationService(memory, authorization, metrics)
        tokens = StaticTokenVerifier(
            {
                "researcher-token": Principal(
                    "researcher-1", "org-a", frozenset({Role.RESEARCHER})
                ),
                "expert-token": Principal(
                    "expert-1", "org-a", frozenset({Role.LEGAL_EXPERT})
                ),
                "evaluator-token": Principal(
                    "evaluator-1", "org-a", frozenset({Role.EVALUATOR})
                ),
                "other-token": Principal(
                    "researcher-2", "org-b", frozenset({Role.RESEARCHER})
                ),
            }
        )
        container = APIContainer(
            research,
            reviews,
            EvaluationRunner(research, authorization),
            tokens,
            authorization,
            metrics,
        )
        self.client = TestClient(create_app(container))

    @staticmethod
    def headers(token: str, request_id: str | None = None) -> dict[str, str]:
        result = {"Authorization": f"Bearer {token}"}
        if request_id:
            result["X-Request-ID"] = request_id
        return result

    @staticmethod
    def research_payload() -> dict[str, object]:
        return {
            "organization_id": "org-a",
            "question": "What is the payment deadline?",
            "applicable_time": APPLICABLE_TIME.isoformat(),
        }

    def test_health_is_public_and_request_id_is_propagated(self) -> None:
        response = self.client.get("/healthz", headers={"X-Request-ID": "request-123"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["X-Request-ID"], "request-123")
        self.assertEqual(response.json()["status"], "ok")

    def test_research_stream_emits_sse_and_final_result(self) -> None:
        with self.client.stream(
            "POST",
            "/v1/research/stream",
            json={**self.research_payload(), "strategy": "navigation"},
            headers=self.headers("researcher-token"),
        ) as response:
            self.assertEqual(response.status_code, 200)
            self.assertIn("text/event-stream", response.headers["content-type"])
            names = []
            frames = []
            for line in response.iter_lines():
                if line.startswith("event: "):
                    names.append(line[len("event: "):])
                elif line.startswith("data: ") and names and names[-1] == "result":
                    frames.append(json.loads(line[len("data: "):]))
        self.assertEqual(names[0], "started")
        self.assertIn("result", names)
        self.assertEqual(names[-1], "done")
        self.assertTrue(frames[0]["data"]["completed"])

    def test_search_parse_returns_deterministic_filters(self) -> None:
        response = self.client.post(
            "/v1/organizations/org-a/search/parse",
            json={"question": "مهلت پرداخت طبق ماده ۵ در تاریخ ۱۴۰۳/۰۲/۱۰ چیست؟"},
            headers=self.headers("researcher-token"),
        )

        self.assertEqual(response.status_code, 200, response.text)
        filters = response.json()["filters"]
        self.assertEqual(filters["provision_type"], "article")
        self.assertEqual(filters["provision_number"], "5")
        self.assertIsNotNone(filters["applicable_time"])
        self.assertIn("مهلت", filters["keywords"])

    def test_unknown_strategy_is_rejected_as_domain_error(self) -> None:
        response = self.client.post(
            "/v1/research",
            json={**self.research_payload(), "strategy": "agentic"},
            headers=self.headers("researcher-token"),
        )
        self.assertEqual(response.status_code, 422)

    def test_workspace_and_assets_are_served_with_security_headers(self) -> None:
        root = self.client.get("/", follow_redirects=False)
        workspace = self.client.get("/workspace")
        script = self.client.get("/ui/app.js")

        self.assertEqual(root.status_code, 307)
        self.assertEqual(root.headers["location"], "/workspace")
        self.assertEqual(workspace.status_code, 200)
        self.assertIn('lang="fa"', workspace.text)
        self.assertIn(
            "default-src 'self'", workspace.headers["Content-Security-Policy"]
        )
        self.assertEqual(workspace.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(script.status_code, 200)
        self.assertNotIn("innerHTML", script.text)

    def test_research_runs_end_to_end_and_can_be_read(self) -> None:
        response = self.client.post(
            "/v1/research",
            json=self.research_payload(),
            headers=self.headers("researcher-token"),
        )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertTrue(payload["completed"])
        self.assertEqual(
            payload["answer"]["citations"][0]["provision_version_id"], "provision-5-v1"
        )
        self.assertGreater(len(payload["trace"]["actions"]), 0)
        self.assertEqual(payload["trace"]["evidence"][0]["disposition"], "accepted")
        episode_id = payload["episode_id"]

        read = self.client.get(
            f"/v1/organizations/org-a/research/{episode_id}",
            headers=self.headers("researcher-token"),
        )
        self.assertEqual(read.status_code, 200)
        self.assertEqual(read.json()["answer"]["answer_id"], "answer-1")

    def test_research_history_lists_and_reopens_episodes(self) -> None:
        first = self.client.post(
            "/v1/research",
            json=self.research_payload(),
            headers=self.headers("researcher-token"),
        )
        episode_id = first.json()["episode_id"]

        history = self.client.get(
            "/v1/organizations/org-a/research",
            headers=self.headers("researcher-token"),
        )
        self.assertEqual(history.status_code, 200, history.text)
        self.assertEqual(history.json()["count"], 1)
        self.assertEqual(history.json()["episodes"][0]["episode_id"], episode_id)
        self.assertTrue(history.json()["episodes"][0]["has_answer"])

        reopen = self.client.get(
            f"/v1/organizations/org-a/research/{episode_id}",
            headers=self.headers("researcher-token"),
        )
        self.assertEqual(reopen.status_code, 200)
        self.assertEqual(reopen.json()["answer"]["answer_id"], "answer-1")

        # History is tenant- and permission-scoped.
        denied = self.client.get(
            "/v1/organizations/org-b/research",
            headers=self.headers("researcher-token"),
        )
        self.assertEqual(denied.status_code, 403)

    def test_research_graph_returns_nodes_edges_and_events(self) -> None:
        graph = self.client.get(
            "/v1/organizations/org-a/research-graph",
            headers=self.headers("researcher-token"),
        )
        self.assertEqual(graph.status_code, 200, graph.text)
        payload = graph.json()
        self.assertEqual(payload["organization_id"], "org-a")
        self.assertGreaterEqual(payload["node_count"], 2)
        edge = payload["edges"][0]
        self.assertEqual(edge["source"], "issue-1")
        self.assertEqual(edge["target"], "provision-5")
        self.assertEqual(edge["status"], "candidate")
        by_id = {node["id"]: node for node in payload["nodes"]}
        self.assertIn("provision-5", by_id)
        self.assertEqual(by_id["provision-5"]["label"], "Article 5 · instrument-1")
        self.assertEqual(payload["events"][0]["action"], "candidate_created")

    def test_library_search_and_exact_source_span_document_view(self) -> None:
        response = self.client.get(
            "/v1/organizations/org-a/library",
            params={"query": "payment deadline", "applicable_time": "2025-01-01"},
            headers=self.headers("researcher-token"),
        )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["documents"][0]["document_version_id"], "document-1")
        self.assertEqual(payload["documents"][0]["provision_count"], 1)

        detail = self.client.get(
            "/v1/organizations/org-a/library/documents/document-1",
            params={"source_span_id": "span-1"},
            headers=self.headers("researcher-token"),
        )
        self.assertEqual(detail.status_code, 200, detail.text)
        document = detail.json()
        self.assertEqual(document["selected_source_span_id"], "span-1")
        self.assertEqual(
            document["provisions"][0]["spans"][0]["raw_text"],
            "The payment deadline is ten days.",
        )

        unknown_span = self.client.get(
            "/v1/organizations/org-a/library/documents/document-1",
            params={"source_span_id": "span-other"},
            headers=self.headers("researcher-token"),
        )
        cross_tenant = self.client.get(
            "/v1/organizations/org-a/library",
            headers=self.headers("other-token"),
        )
        self.assertEqual(unknown_span.status_code, 404)
        self.assertEqual(cross_tenant.status_code, 403)

    def test_authentication_role_and_tenant_boundaries(self) -> None:
        missing = self.client.post("/v1/research", json=self.research_payload())
        invalid = self.client.post(
            "/v1/research",
            json=self.research_payload(),
            headers=self.headers("invalid"),
        )
        cross_tenant = self.client.post(
            "/v1/research",
            json=self.research_payload(),
            headers=self.headers("other-token"),
        )
        forbidden_review = self.client.post(
            "/v1/organizations/org-a/relations/relation-1/approve",
            json={"note": "checked"},
            headers=self.headers("researcher-token"),
        )

        self.assertEqual(missing.status_code, 401)
        self.assertEqual(missing.headers["WWW-Authenticate"], "Bearer")
        self.assertEqual(invalid.status_code, 401)
        self.assertEqual(cross_tenant.status_code, 403)
        self.assertEqual(forbidden_review.status_code, 403)

    def test_expert_can_approve_candidate(self) -> None:
        response = self.client.post(
            "/v1/organizations/org-a/relations/relation-1/approve",
            json={"note": "verified against source"},
            headers=self.headers("expert-token"),
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "expert_approved")
        self.assertEqual(response.json()["approval"]["approved_by"], "expert-1")

    def test_two_step_review_flow_with_confirmation_ticket(self) -> None:
        request_ticket = self.client.post(
            "/v1/organizations/org-a/relations/relation-1/review",
            json={"action": "approve", "note": "تأیید با کنترل منبع"},
            headers=self.headers("expert-token"),
        )
        self.assertEqual(request_ticket.status_code, 200, request_ticket.text)
        ticket = request_ticket.json()
        self.assertEqual(ticket["action"], "approve")
        self.assertTrue(ticket["ticket_id"])

        confirm = self.client.post(
            "/v1/organizations/org-a/relations/relation-1/review/confirm",
            json={
                "ticket_id": ticket["ticket_id"],
                "action": "approve",
                "note": "تأیید با کنترل منبع",
                "idempotency_key": "idem-0001",
            },
            headers=self.headers("expert-token"),
        )
        self.assertEqual(confirm.status_code, 200, confirm.text)
        self.assertEqual(confirm.json()["status"], "expert_approved")

        replay = self.client.post(
            "/v1/organizations/org-a/relations/relation-1/review/confirm",
            json={
                "ticket_id": ticket["ticket_id"],
                "action": "approve",
                "note": "تأیید با کنترل منبع",
                "idempotency_key": "idem-0001",
            },
            headers=self.headers("expert-token"),
        )
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json()["status"], "expert_approved")

        # بدون کلید idempotency، تیکت مصرف‌شده دیگر قابل استفاده نیست.
        exhausted = self.client.post(
            "/v1/organizations/org-a/relations/relation-1/review/confirm",
            json={
                "ticket_id": ticket["ticket_id"],
                "action": "approve",
                "note": "تأیید با کنترل منبع",
            },
            headers=self.headers("expert-token"),
        )
        self.assertEqual(exhausted.status_code, 404)

    def test_review_confirm_rejects_tampered_note(self) -> None:
        ticket = self.client.post(
            "/v1/organizations/org-a/relations/relation-1/review",
            json={"action": "approve", "note": "متن اصلی"},
            headers=self.headers("expert-token"),
        ).json()
        tampered = self.client.post(
            "/v1/organizations/org-a/relations/relation-1/review/confirm",
            json={"ticket_id": ticket["ticket_id"], "action": "approve", "note": "متن دستکاری‌شده"},
            headers=self.headers("expert-token"),
        )
        self.assertEqual(tampered.status_code, 422)

    def test_expert_review_queue_is_tenant_scoped_and_role_protected(self) -> None:
        forbidden = self.client.get(
            "/v1/organizations/org-a/relations",
            headers=self.headers("researcher-token"),
        )
        cross_tenant = self.client.get(
            "/v1/organizations/org-a/relations",
            headers=self.headers("other-token"),
        )
        response = self.client.get(
            "/v1/organizations/org-a/relations",
            headers=self.headers("expert-token"),
        )

        self.assertEqual(forbidden.status_code, 403)
        self.assertEqual(cross_tenant.status_code, 403)
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["relations"][0]["relation_id"], "relation-1")
        self.assertEqual(payload["relations"][0]["edge_type"], "governed_by")
        self.assertEqual(
            payload["relations"][0]["evidence_version_ids"], ["provision-5-v1"]
        )

    def test_evaluation_and_tenant_metrics(self) -> None:
        response = self.client.post(
            "/v1/organizations/org-a/evaluations",
            json={
                "cases": [
                    {
                        "case_id": "deadline-current",
                        "question": "What is the payment deadline?",
                        "applicable_time": APPLICABLE_TIME.isoformat(),
                        "expected_provision_ids": ["provision-5"],
                        "expected_provision_version_ids": ["provision-5-v1"],
                    }
                ]
            },
            headers=self.headers("evaluator-token"),
        )

        self.assertEqual(response.status_code, 200, response.text)
        report = response.json()
        self.assertEqual(report["completion_rate"], 1.0)
        self.assertEqual(report["citation_precision"], 1.0)
        self.assertEqual(report["citation_recall"], 1.0)
        self.assertEqual(report["temporal_accuracy"], 1.0)
        self.assertEqual(report["grounded_claim_rate"], 1.0)

        metrics = self.client.get(
            "/v1/organizations/org-a/metrics",
            headers=self.headers("evaluator-token"),
        )
        self.assertEqual(metrics.status_code, 200)
        self.assertEqual(metrics.json()["counters"]["research_completed_total"], 1)
        self.assertNotIn("http_requests_total", metrics.json()["counters"])

    def test_unknown_fields_are_rejected(self) -> None:
        payload = self.research_payload() | {"unsafe_override": True}

        response = self.client.post(
            "/v1/research",
            json=payload,
            headers=self.headers("researcher-token"),
        )

        self.assertEqual(response.status_code, 422)

    def test_openapi_declares_bearer_security(self) -> None:
        document = self.client.get("/openapi.json").json()

        self.assertIn("HTTPBearer", document["components"]["securitySchemes"])


if __name__ == "__main__":
    unittest.main()
