from __future__ import annotations

import json
import unittest
from datetime import date
from pathlib import Path

from agentic_fixture import APPLICABLE

from legal_agent_core.agentic import AgenticConfig, LegalResearchTools, ToolResearchLoop
from legal_agent_core.agentic.planner import PlannerDecision
from legal_agent_core.benchmark import (
    DeterministicGrader,
    DocumentIndex,
    ExtractiveAnswerComposer,
    GoldenCase,
    declared_abstention,
    load_golden_cases,
    seed_corpus,
)
from legal_agent_core.errors import DomainError
from legal_agent_core.verification import EvidenceVerifier

ROOT = Path(__file__).resolve().parents[1] / "tests" / "tamin_longdocs_agentic_benchmark_v4"
SCRATCH = Path("/tmp/opencode/legal-agent-bench-test")


def _write_benchmark(rows: list[dict]) -> Path:
    SCRATCH.mkdir(parents=True, exist_ok=True)
    directory = SCRATCH / "benchmark"
    directory.mkdir(exist_ok=True)
    for name in ("questions.jsonl", "gold_answers.jsonl"):
        (directory / name).write_text("", encoding="utf-8")
    questions = directory / "questions.jsonl"
    gold = directory / "gold_answers.jsonl"
    for row in rows:
        with questions.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row["question"], ensure_ascii=False) + "\n")
        with gold.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row["gold"], ensure_ascii=False) + "\n")
    return directory


def _outcome(repository, script, question: str | None = None):
    planner = _FixedPlanner(script)
    loop = ToolResearchLoop(
        LegalResearchTools(repository),
        planner,
        EvidenceVerifier(repository),
        AgenticConfig(max_steps=4),
    )
    if question is None:
        first = script[0]
        question = (
            first.args.get("query", "پرسش")
            if first.action == "tool"
            else "پرسش"
        )
    return loop.run(
        episode_id="episode-bench",
        organization_id="org-a",
        user_id="user",
        conversation_id="conversation",
        question=question,
        applicable_time=APPLICABLE,
    )


class _FixedPlanner:
    def __init__(self, script) -> None:
        self.script = list(script)

    def decide(self, system_prompt: str, user_prompt: str):
        import re

        from legal_agent_core.agentic.planner import PlannerTurn

        item = self.script.pop(0)
        if item.action == "answer" and not item.claims:
            evidence_ids = re.findall(r'"evidence_id":\s*"([^"]+)"', user_prompt)
            claims = ()
            if evidence_ids:
                claims = (
                    {
                        "claim_id": "c1",
                        "text": "پاسخ مستند",
                        "evidence_ids": sorted(set(evidence_ids)),
                        "importance": "critical",
                    },
                )
            item = PlannerDecision("answer", answer=item.answer, claims=claims)
        return PlannerTurn(
            decision=item,
            contract_error=None,
            raw_text="{}",
            input_tokens=10,
            output_tokens=10,
            provider_id="fake",
            model="fake",
            model_profile_id="profile",
            model_profile_version=1,
            prompt_id="prompt",
            prompt_version=1,
            agent_version="test",
            response_id="resp",
        )


class GoldenCaseLoaderTests(unittest.TestCase):
    def test_load_real_corpus_join(self) -> None:
        cases = load_golden_cases(ROOT / "benchmark", date(2026, 9, 21))
        self.assertEqual(len(cases), 76)
        categories = {case.category for case in cases}
        self.assertIn("temporal_version", categories)
        self.assertIn("limited_evidence", categories)
        insufficient = [case for case in cases if case.answerability == "insufficient"]
        self.assertEqual(len(insufficient), 9)
        self.assertTrue(all(case.expected_abstention for case in insufficient))

    def test_missing_gold_raises(self) -> None:
        rows = [
            {
                "question": {"id": "Q900", "category": "direct", "question": "؟"},
                "gold": {"id": "Q900", "gold_answer": "...", "answerability": "full"},
            }
        ]
        directory = _write_benchmark(rows)
        (directory / "gold_answers.jsonl").write_text("", encoding="utf-8")
        with self.assertRaises(DomainError):
            load_golden_cases(directory, date(2026, 9, 21))

    def test_invalid_answerability_rejected(self) -> None:
        rows = [
            {
                "question": {"id": "Q1"},
                "gold": {"id": "Q1", "answerability": "maybe"},
            }
        ]
        directory = _write_benchmark(rows)
        with self.assertRaises(DomainError):
            load_golden_cases(directory, date(2026, 9, 21))


class DeclaredAbstentionTests(unittest.TestCase):
    def test_issue_flag_wins(self) -> None:
        self.assertTrue(declared_abstention("متن معمولی", ("declared_abstention",)))

    def test_markers_detected(self) -> None:
        self.assertTrue(declared_abstention("با اسناد موجود نمی‌توان نظر قطعی داد."))
        self.assertFalse(declared_abstention("مهلت بیست روز است."))

    def test_markers_ignored_when_claims_are_evidenced(self) -> None:
        # متن نقل‌قولی می‌تواند حاوی همین واژه‌ها باشد؛ ادعای مستند یعنی امتناع نیست.
        self.assertFalse(
            declared_abstention("هیچ کمبودی مجاز نیست.", evidenced_claims=2)
        )


class DeterministicGraderTests(unittest.TestCase):
    def setUp(self) -> None:
        from agentic_fixture import add_resolved_reference, build_canonical

        self.repository = build_canonical()
        add_resolved_reference(self.repository)
        self.document_map = {
            "DX": DocumentIndex(
                doc_code="DX",
                instrument_id="instrument-demo",
                source_document_id="source-demo",
                document_version_id="document-demo-v2",
                effective_from=date(2025, 1, 1),
            ),
        }
        self.grader = DeterministicGrader(self.document_map)

    def _case(self, **overrides) -> GoldenCase:
        values = {
            "case_id": "Q100",
            "question": "مهلت تجدیدنظر؟",
            "category": "direct",
            "difficulty": "easy",
            "answerability": "full",
            "applicable_time": APPLICABLE,
            "required_documents": ("DX",),
            "evidence_pages": (("DX", frozenset({17})),),
            "expected_abstention": None,
        }
        values.update(overrides)
        return GoldenCase(**values)

    def test_full_answer_with_page_hit_scores_high(self) -> None:
        from agentic_fixture import build_canonical

        outcome = _outcome(
            build_canonical(),
            [
                PlannerDecision(
                    "tool",
                    tool="search_provisions",
                    args={
                        "query": "مهلت تجدیدنظر",
                        "applicable_time": APPLICABLE.isoformat(),
                    },
                ),
                PlannerDecision("answer", answer="مهلت سی روز است."),
            ],
        )
        grade = self.grader.grade(self._case(), outcome, None)
        self.assertTrue(grade.completed)
        self.assertEqual(grade.document_recall, 1.0)
        self.assertEqual(grade.page_hit_rate, 1.0)
        self.assertTrue(grade.abstention_correct)
        self.assertGreater(grade.score, 0.8)

    def test_over_refusal_on_answerable_question_is_penalized(self) -> None:
        outcome = _outcome(
            self.repository,
            [PlannerDecision("abstain", abstention_reason="اطلاعات کافی نیست.")],
        )
        grade = self.grader.grade(self._case(), outcome, None)
        self.assertTrue(grade.abstained)
        self.assertFalse(grade.abstention_correct)
        self.assertLess(grade.score, 0.5)

    def test_correct_abstention_on_insufficient_question_scores_full(self) -> None:
        outcome = _outcome(
            self.repository,
            [PlannerDecision("abstain", abstention_reason="منبع همان دوره موجود نیست.")],
        )
        grade = self.grader.grade(
            self._case(answerability="insufficient", expected_abstention="نیاز به منبع"),
            outcome,
            None,
        )
        self.assertTrue(grade.abstention_correct)
        self.assertEqual(grade.score, 1.0)

    def test_temporal_proxy_is_reported_for_temporal_category(self) -> None:
        from agentic_fixture import build_canonical

        outcome = _outcome(
            build_canonical(),
            [
                PlannerDecision(
                    "tool",
                    tool="temporal_check",
                    args={
                        "provision_id": "provision-demo-336",
                        "applicable_time": APPLICABLE.isoformat(),
                    },
                ),
                PlannerDecision("answer", answer="نسخه‌ی معتبر سی روز است."),
            ],
            question="نسخه‌ی قابل اعمال کدام است؟",
        )
        grade = self.grader.grade(
            self._case(category="temporal_version"), outcome, None
        )
        self.assertIsNotNone(grade.temporal_ok)

    def test_non_abstaining_insufficient_answer_is_halved(self) -> None:
        from agentic_fixture import build_canonical

        outcome = _outcome(
            build_canonical(),
            [
                PlannerDecision(
                    "tool",
                    tool="search_provisions",
                    args={
                        "query": "مهلت تجدیدنظر",
                        "applicable_time": APPLICABLE.isoformat(),
                    },
                ),
                PlannerDecision("answer", answer="پاسخ قطعی بدون هیچ اعلام محدودیتی."),
            ],
        )
        grade = self.grader.grade(
            self._case(answerability="insufficient"), outcome, None
        )
        self.assertFalse(grade.abstained)
        self.assertLess(grade.score, 0.5)


class SeedCorpusTests(unittest.TestCase):
    def test_seed_real_corpus_with_manifest_dates(self) -> None:
        from legal_agent_core.in_memory import InMemoryCanonicalRepository

        repository = InMemoryCanonicalRepository()
        manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))[
            "documents"
        ]
        document_map = seed_corpus(repository, ROOT / "documents_md", manifest)
        self.assertEqual(len(document_map), 9)
        self.assertEqual(document_map["D01"].effective_from, date(2017, 11, 26))
        self.assertEqual(document_map["D05"].effective_from, date(2021, 4, 3))
        self.assertIsNone(document_map["D03"].effective_from)
        self.assertTrue(len(repository.list_provisions()) > 1000)


class ExtractiveComposerTests(unittest.TestCase):
    def test_one_claim_per_evidence(self) -> None:
        from agentic_fixture import build_canonical

        repository = build_canonical()
        tools = LegalResearchTools(repository)
        from legal_agent_core.agentic.envelope import RefStore

        result = tools.dispatch(
            RefStore(),
            "search_provisions",
            {"query": "مهلت", "applicable_time": APPLICABLE.isoformat()},
        )
        composer = ExtractiveAnswerComposer()
        draft = composer.compose("پرسش", APPLICABLE, result.evidence)
        self.assertEqual(len(draft.claims), len(result.evidence))
        self.assertTrue(all(claim.evidence_ids for claim in draft.claims))

    def test_empty_evidence_raises(self) -> None:
        with self.assertRaises(DomainError):
            ExtractiveAnswerComposer().compose("پرسش", APPLICABLE, ())


if __name__ == "__main__":
    unittest.main()
