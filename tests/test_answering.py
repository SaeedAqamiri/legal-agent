import json
import unittest
from dataclasses import replace
from datetime import date

from legal_agent_core.answering import (
    AnswerCompositionError,
    AnswerPublicationError,
    CitationPipeline,
    LLMAnswerComposer,
)
from legal_agent_core.llm import LLMResult, LLMTask, ProviderResponse, TokenUsage
from legal_agent_core.navigation import NavigationLoop, RetrievalBatch
from legal_agent_core.verification import (
    AnswerClaim,
    ClaimImportance,
    DraftAnswer,
    EvidenceLedger,
    EvidenceVerifier,
    GenerationMetadata,
    VerificationCode,
)
from tests.test_verification import (
    APPLICABLE_TIME,
    canonical_repository,
    evidence_current,
)


class FakeLLMService:
    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.calls = []

    def generate(self, task, variables, **kwargs):
        self.calls.append((task, variables, kwargs))
        text = self.payload if isinstance(self.payload, str) else json.dumps(self.payload, ensure_ascii=False)
        return LLMResult(
            ProviderResponse("provider-response-1", "resolved-model", text, TokenUsage(100, 20, 120), "request-1"),
            "provider-1",
            "legal-answer-profile",
            4,
            "legal-answer",
            3,
            "agent/9",
            LLMTask.ANSWER_COMPOSITION,
        )


def model_payload(evidence_id: str = "ev-current") -> dict[str, object]:
    return {
        "answer": "نرخ مقرر بر اساس ماده پنج اعمال می‌شود.",
        "claims": [
            {
                "claim_id": "claim-rate",
                "text": "ماده پنج حکم مورد نظر را بیان می‌کند.",
                "evidence_ids": [evidence_id],
                "importance": "critical",
            }
        ],
    }


def generated_draft(*claims: AnswerClaim) -> DraftAnswer:
    return DraftAnswer(
        "answer-verified",
        "پاسخ مستند",
        claims,
        GenerationMetadata(
            "provider-1",
            "model-1",
            "profile-1",
            2,
            "prompt-1",
            3,
            "agent/5",
            "response-1",
            "request-1",
        ),
    )


class SingleBatchRetriever:
    def __init__(self) -> None:
        self.used = False

    def retrieve(self, request):
        if self.used:
            return RetrievalBatch(())
        self.used = True
        return RetrievalBatch((evidence_current(),), visited_provision_ids=("prov-5",))


class LLMAnswerComposerTests(unittest.TestCase):
    def test_composes_structured_claims_and_preserves_generation_audit(self) -> None:
        service = FakeLLMService(model_payload())
        composer = LLMAnswerComposer(service)

        draft = composer.compose("حکم ماده پنج چیست؟", APPLICABLE_TIME, (evidence_current(),))

        self.assertEqual(draft.text, "نرخ مقرر بر اساس ماده پنج اعمال می‌شود.")
        self.assertEqual(draft.claims[0].evidence_ids, ("ev-current",))
        self.assertEqual(draft.claims[0].importance, ClaimImportance.CRITICAL)
        self.assertEqual(draft.generation.model_profile_version, 4)
        self.assertEqual(draft.generation.prompt_version, 3)
        task, variables, options = service.calls[0]
        self.assertEqual(task, LLMTask.ANSWER_COMPOSITION)
        serialized_evidence = json.loads(variables["evidence"])
        self.assertEqual(serialized_evidence[0]["evidence_id"], "ev-current")
        self.assertEqual(options["structured_output"].name, "legal_answer")

    def test_rejects_invalid_json_or_malformed_claims(self) -> None:
        with self.assertRaises(AnswerCompositionError):
            LLMAnswerComposer(FakeLLMService("not-json")).compose("سؤال", APPLICABLE_TIME, ())
        malformed = model_payload()
        malformed["claims"] = [{"claim_id": "c", "text": "t", "evidence_ids": "ev", "importance": "major"}]
        with self.assertRaises(AnswerCompositionError):
            LLMAnswerComposer(FakeLLMService(malformed)).compose("سؤال", APPLICABLE_TIME, ())


class CitationPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = canonical_repository()
        self.ledger = EvidenceLedger()
        self.ledger.add(evidence_current())
        self.draft = generated_draft(
            AnswerClaim("claim-1", "حکم ماده پنج", ("ev-current",), ClaimImportance.CRITICAL)
        )
        self.report = EvidenceVerifier(self.repository).verify(self.draft, self.ledger, APPLICABLE_TIME)

    def test_publishes_exact_canonical_citations_and_claim_map(self) -> None:
        published = CitationPipeline(self.repository).publish(
            self.draft,
            self.report,
            self.ledger,
            APPLICABLE_TIME,
        )

        self.assertEqual(len(published.citations), 1)
        rendered = published.citations[0]
        self.assertEqual(rendered.marker, "[1]")
        self.assertEqual(rendered.evidence_id, "ev-current")
        self.assertEqual(rendered.citation.document_version_id, "doc-current")
        self.assertEqual(rendered.citation.provision_version_id, "prov-5-current")
        self.assertEqual(rendered.citation.source_span_id, "span-current")
        self.assertEqual(rendered.citation.page, 3)
        self.assertEqual(rendered.source_uri, "file:///law.pdf")
        self.assertIn("[1]", published.rendered_text)
        self.assertEqual(published.claim_evidence_map[0].evidence_ids, ("ev-current",))
        self.assertEqual(published.audit.generation.prompt_version, 3)

    def test_rejects_report_for_another_answer_or_failed_verification(self) -> None:
        pipeline = CitationPipeline(self.repository)
        with self.assertRaisesRegex(AnswerPublicationError, "another answer"):
            pipeline.publish(
                self.draft,
                replace(self.report, answer_id="answer-other"),
                self.ledger,
                APPLICABLE_TIME,
            )
        with self.assertRaisesRegex(AnswerPublicationError, "verification"):
            pipeline.publish(
                self.draft,
                replace(self.report, accepted=False, requires_more_research=True),
                self.ledger,
                APPLICABLE_TIME,
            )
        with self.assertRaisesRegex(AnswerPublicationError, "applicable_time"):
            pipeline.publish(
                self.draft,
                self.report,
                self.ledger,
                date(2024, 1, 1),
            )

    def test_rejects_claim_without_evidence_regardless_of_importance(self) -> None:
        # The verifier and the publish gate must agree: an unevidenced claim is
        # an error even when marked MINOR, otherwise publication would crash.
        draft = generated_draft(
            AnswerClaim("claim-sourced", "حکم مستند", ("ev-current",), ClaimImportance.CRITICAL),
            AnswerClaim("claim-minor", "جزئیات بدون منبع", (), ClaimImportance.MINOR),
        )
        report = EvidenceVerifier(self.repository).verify(draft, self.ledger, APPLICABLE_TIME)
        self.assertFalse(report.accepted)
        self.assertIn(
            VerificationCode.MISSING_EVIDENCE,
            {issue.code for issue in report.issues},
        )

        with self.assertRaisesRegex(AnswerPublicationError, "verification"):
            CitationPipeline(self.repository).publish(draft, report, self.ledger, APPLICABLE_TIME)

    def test_navigation_to_verified_publication_end_to_end(self) -> None:
        composer = LLMAnswerComposer(FakeLLMService(model_payload()))
        outcome = NavigationLoop(
            SingleBatchRetriever(),
            composer,
            EvidenceVerifier(self.repository),
        ).run(
            episode_id="episode-answer",
            organization_id="org-1",
            user_id="user-1",
            conversation_id="conversation-1",
            question="حکم ماده پنج چیست؟",
            applicable_time=APPLICABLE_TIME,
        )

        published = CitationPipeline(self.repository).publish(
            outcome.draft,
            outcome.verification,
            outcome.ledger,
            APPLICABLE_TIME,
        )

        self.assertTrue(outcome.completed)
        self.assertEqual(published.answer_id, outcome.draft.answer_id)
        self.assertEqual(published.audit.verified_evidence_ids, ("ev-current",))
        self.assertEqual(published.audit.generation.provider_response_id, "provider-response-1")


if __name__ == "__main__":
    unittest.main()
