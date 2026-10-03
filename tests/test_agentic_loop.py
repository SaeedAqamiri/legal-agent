from __future__ import annotations

import re
import unittest
from datetime import date

from agentic_fixture import APPLICABLE, add_resolved_reference, build_canonical

from legal_agent_core.agentic.loop import (
    AgenticConfig,
    IncompleteReason,
    ToolResearchLoop,
)
from legal_agent_core.agentic.planner import PlannerDecision, PlannerTurn
from legal_agent_core.agentic.tools import LegalResearchTools
from legal_agent_core.answering import CitationPipeline
from legal_agent_core.application import (
    InMemoryResearchRecordRepository,
    ResearchApplicationService,
    ResearchCommand,
    ResearchStrategy,
)
from legal_agent_core.errors import DomainError
from legal_agent_core.observability import MetricsRegistry
from legal_agent_core.security import AuthorizationService, Principal, Role
from legal_agent_core.verification import EvidenceVerifier


def _turn(
    decision: PlannerDecision | None = None,
    contract_error: str | None = None,
    tokens: int = 100,
) -> PlannerTurn:
    return PlannerTurn(
        decision=decision,
        contract_error=contract_error,
        raw_text="{}",
        input_tokens=tokens,
        output_tokens=tokens,
        provider_id="fake",
        model="fake-model",
        model_profile_id="legal-tool-planning",
        model_profile_version=1,
        prompt_id="legal-agentic-planner",
        prompt_version=1,
        agent_version="test/1",
        response_id="resp-1",
    )


class ScriptedPlanner:
    """Replays decisions; extracts evidence ids from the transcript like a model."""

    def __init__(self, script, repeat_last: bool = False) -> None:
        self.script = list(script)
        self.repeat_last = repeat_last
        self.prompts: list[tuple[str, str]] = []

    def decide(self, system_prompt: str, user_prompt: str) -> PlannerTurn:
        self.prompts.append((system_prompt, user_prompt))
        if not self.script:
            raise AssertionError("script exhausted")
        item = self.script.pop(0)
        if self.repeat_last:
            self.script.insert(0, item)
        if isinstance(item, str):
            return _turn(contract_error=item)
        decision = item
        if decision.action == "answer" and not decision.claims:
            # Evidence ids appear either inside JSON envelopes or in compacted
            # step summaries (ev=...); both are planner-visible.
            evidence_ids = re.findall(r'"evidence_id":\s*"([^"]+)"', user_prompt)
            evidence_ids += re.findall(r"\b(ev-[0-9a-f]+)", user_prompt)
            claims = ()
            if evidence_ids:
                claims = (
                    {
                        "claim_id": "c1",
                        "text": "مهلت تجدیدنظر اشخاص مقیم ایران سی روز است.",
                        "evidence_ids": sorted(set(evidence_ids)),
                        "importance": "critical",
                    },
                )
            decision = PlannerDecision(
                "answer", answer=decision.answer, claims=claims
            )
        return _turn(decision)


def _loop(repository, planner, **config) -> ToolResearchLoop:
    return ToolResearchLoop(
        LegalResearchTools(repository),
        planner,
        EvidenceVerifier(repository),
        AgenticConfig(**config),
    )


def _run(loop: ToolResearchLoop, **overrides):
    return loop.run(
        episode_id="episode-1",
        organization_id="org-a",
        user_id="user-1",
        conversation_id="conversation-1",
        question="مهلت تجدیدنظر اشخاص مقیم ایران در تاریخ ۱۴۰۴ چند روز است؟",
        applicable_time=APPLICABLE,
        **overrides,
    )


class ToolResearchLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = build_canonical()

    def test_completed_answer_publishes_through_citation_pipeline(self) -> None:
        planner = ScriptedPlanner(
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
            ]
        )
        outcome = _run(_loop(self.repository, planner))
        self.assertTrue(outcome.completed)
        self.assertTrue(outcome.verification.accepted)
        self.assertEqual(outcome.iterations, 2)
        self.assertTrue(outcome.draft.claims)
        claim = outcome.draft.claims[0]
        self.assertTrue(claim.evidence_ids)

        published = CitationPipeline(self.repository).publish(
            outcome.draft, outcome.verification, outcome.ledger, APPLICABLE
        )
        self.assertTrue(published.citations)
        self.assertIn("سی روز", published.text)

        action_types = {action.action_type for action in outcome.episode.actions}
        self.assertIn("searched", {item.value for item in action_types})
        self.assertTrue(planner.prompts[0][0].startswith("شما ایجنت پژوهش حقوقی"))

    def test_completed_reference_following_satisfies_verifier(self) -> None:
        repository = build_canonical()
        add_resolved_reference(repository)
        planner = ScriptedPlanner(
            [
                PlannerDecision(
                    "tool",
                    tool="search_provisions",
                    args={
                        "query": "فصل دوم",
                        "applicable_time": APPLICABLE.isoformat(),
                    },
                ),
                PlannerDecision("answer", answer="ماده ۳۴۰ تابع ماده ۳۳۶ است."),
            ]
        )
        outcome = _run(_loop(repository, planner))
        self.assertTrue(outcome.completed)

    def test_contract_error_is_fed_back_and_recovered(self) -> None:
        planner = ScriptedPlanner(
            [
                PlannerDecision(
                    "tool",
                    tool="search_provisions",
                    args={
                        "query": "مهلت تجدیدنظر",
                        "applicable_time": APPLICABLE.isoformat(),
                    },
                ),
                "this is not json",
                PlannerDecision("answer", answer="مهلت سی روز است."),
            ]
        )
        outcome = _run(_loop(self.repository, planner, max_steps=4))
        self.assertTrue(outcome.completed)
        self.assertTrue(
            any("یادآوری قرارداد" in user for _, user in planner.prompts[1:])
        )

    def test_invalid_tool_args_are_reported_not_fatal(self) -> None:
        planner = ScriptedPlanner(
            [
                PlannerDecision(
                    "tool",
                    tool="search_provisions",
                    args={"query": "", "applicable_time": "2025-06-01"},
                ),
                PlannerDecision(
                    "tool",
                    tool="search_provisions",
                    args={
                        "query": "مهلت تجدیدنظر",
                        "applicable_time": APPLICABLE.isoformat(),
                    },
                ),
                PlannerDecision("answer", answer="مهلت سی روز است."),
            ]
        )
        outcome = _run(_loop(self.repository, planner, max_steps=4))
        self.assertTrue(outcome.completed)
        self.assertTrue(outcome.ledger.entries)

    def test_explicit_abstention_is_honest_incomplete(self) -> None:
        planner = ScriptedPlanner(
            [
                PlannerDecision(
                    "abstain",
                    abstention_reason="نسخه‌ی قابل اعمال بر واقعه‌ی ۱۳۸۳ در مجموعه نیست.",
                ),
            ]
        )
        outcome = _run(_loop(self.repository, planner))
        self.assertFalse(outcome.completed)
        self.assertIn(
            IncompleteReason.DECLARED_ABSTENTION.value,
            outcome.episode.identified_issues,
        )
        self.assertIn("امتناع", outcome.draft.text)

    def test_step_budget_exhaustion_is_reported(self) -> None:
        def tool_decision():
            return PlannerDecision(
                "tool",
                tool="search_provisions",
                args={"query": "مهلت", "applicable_time": APPLICABLE.isoformat()},
            )

        planner = ScriptedPlanner([tool_decision()], repeat_last=True)
        outcome = _run(_loop(self.repository, planner, max_steps=2))
        self.assertFalse(outcome.completed)
        self.assertIn(
            IncompleteReason.BUDGET_STEPS.value,
            outcome.episode.identified_issues,
        )
        self.assertIn("INCOMPLETE", outcome.draft.text)

    def test_token_budget_exhaustion_is_reported(self) -> None:
        planner = ScriptedPlanner(
            [
                PlannerDecision("answer", answer="مهلت سی روز است."),
            ]
        )
        outcome = _run(
            _loop(self.repository, planner, max_total_tokens=10),
        )
        self.assertFalse(outcome.completed)
        self.assertIn(
            IncompleteReason.BUDGET_TOKENS.value,
            outcome.episode.identified_issues,
        )

    def test_unevidenced_minor_claim_is_dropped_and_answer_still_publishes(self) -> None:
        # Regression: previously verification accepted an unevidenced MINOR
        # claim (warning only) and then CitationPipeline.publish crashed,
        # failing the whole case. The loop now drops such claims.
        planner = ScriptedPlanner(
            [
                PlannerDecision(
                    "tool",
                    tool="search_provisions",
                    args={
                        "query": "مهلت تجدیدنظر",
                        "applicable_time": APPLICABLE.isoformat(),
                    },
                ),
                PlannerDecision(
                    "answer",
                    answer="مهلت سی روز است. این پاسخ مشاوره حقوقی نیست.",
                    claims=(
                        {
                            "claim_id": "c1",
                            "text": "مهلت سی روز است.",
                            "evidence_ids": [],
                            "importance": "critical",
                        },
                        {
                            "claim_id": "c2",
                            "text": "این پاسخ مشاوره حقوقی نیست.",
                            "evidence_ids": [],
                            "importance": "minor",
                        },
                    ),
                ),
                PlannerDecision("answer", answer="مهلت سی روز است."),
            ]
        )
        outcome = _run(_loop(self.repository, planner, max_steps=4))
        self.assertTrue(outcome.completed)
        self.assertIn(
            "dropped_unevidenced_claims", outcome.episode.identified_issues
        )
        published = CitationPipeline(self.repository).publish(
            outcome.draft, outcome.verification, outcome.ledger, APPLICABLE
        )
        self.assertTrue(published.citations)

    def test_citing_unknown_evidence_fails_verification_then_recovers(self) -> None:
        planner = ScriptedPlanner(
            [
                PlannerDecision(
                    "answer",
                    answer="پاسخ اول با استناد جعلی.",
                    claims=(
                        {
                            "claim_id": "c1",
                            "text": "ادعای بی‌مدرک",
                            "evidence_ids": ["ev-forged"],
                            "importance": "critical",
                        },
                    ),
                ),
                PlannerDecision(
                    "tool",
                    tool="search_provisions",
                    args={
                        "query": "مهلت تجدیدنظر",
                        "applicable_time": APPLICABLE.isoformat(),
                    },
                ),
                PlannerDecision("answer", answer="مهلت سی روز است."),
            ]
        )
        outcome = _run(_loop(self.repository, planner, max_steps=4))
        self.assertTrue(outcome.completed)
        self.assertIn("unknown_evidence", planner.prompts[1][1])

    def test_blank_question_is_rejected(self) -> None:
        loop = _loop(self.repository, ScriptedPlanner([]))
        with self.assertRaises(DomainError):
            loop.run(
                episode_id="e",
                organization_id="o",
                user_id="u",
                conversation_id="c",
                question="   ",
                applicable_time=date(2025, 6, 1),
            )

    def test_llm_failure_is_honest_incomplete(self) -> None:
        class FailingPlanner:
            def decide(self, system_prompt: str, user_prompt: str) -> PlannerTurn:
                raise DomainError("gateway timeout")

        outcome = _run(_loop(self.repository, FailingPlanner(), max_steps=2))
        self.assertFalse(outcome.completed)
        self.assertIn(
            IncompleteReason.LLM_ERROR.value,
            outcome.episode.identified_issues,
        )


class ApplicationStrategyTests(unittest.TestCase):
    def test_agentic_strategy_publishes_through_service(self) -> None:
        repository = build_canonical()
        planner = ScriptedPlanner(
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
            ]
        )
        service = ResearchApplicationService(
            None,
            CitationPipeline(repository),
            InMemoryResearchRecordRepository(),
            AuthorizationService(),
            MetricsRegistry(),
            id_factory=lambda: "x",
            agentic=_loop(repository, planner),
        )
        principal = Principal("researcher", "org-a", frozenset({Role.RESEARCHER}))
        record = service.run(
            principal,
            ResearchCommand(
                "org-a",
                "مهلت تجدیدنظر چند روز است؟",
                APPLICABLE,
                strategy=ResearchStrategy.AGENTIC,
            ),
        )
        self.assertTrue(record.outcome.completed)
        self.assertIsNotNone(record.answer)

    def test_agentic_strategy_without_loop_is_rejected(self) -> None:
        repository = build_canonical()
        service = ResearchApplicationService(
            None,
            CitationPipeline(repository),
            InMemoryResearchRecordRepository(),
            AuthorizationService(),
            MetricsRegistry(),
            id_factory=lambda: "x",
        )
        principal = Principal("researcher", "org-a", frozenset({Role.RESEARCHER}))
        with self.assertRaises(DomainError):
            service.run(
                principal,
                ResearchCommand(
                    "org-a",
                    "پرسش",
                    APPLICABLE,
                    strategy=ResearchStrategy.AGENTIC,
                ),
            )


if __name__ == "__main__":
    unittest.main()


class TranscriptCompactionTests(unittest.TestCase):
    """Old tool envelopes collapse to summaries; evidence ids stay citable."""

    def setUp(self) -> None:
        self.repository = build_canonical()

    def _search(self):
        return PlannerDecision(
            "tool",
            tool="search_provisions",
            args={
                "query": "مهلت تجدیدنظر",
                "applicable_time": APPLICABLE.isoformat(),
            },
        )

    def test_old_steps_compact_recent_stay_full(self) -> None:
        planner = ScriptedPlanner(
            [self._search(), self._search(), self._search(), self._search()],
            repeat_last=True,
        )
        outcome = _run(_loop(self.repository, planner, max_steps=5))
        self.assertFalse(outcome.completed)  # never answered; budget burn run
        # The LAST planner call (deciding step 5) saw 4 recorded steps:
        # steps 1-2 compacted, steps 3-4 full.
        final_prompt = planner.prompts[-1][1]
        self.assertIn("گام‌های قدیمی خلاصه شده‌اند", final_prompt)
        self.assertIn("TOOL search_provisions", final_prompt)
        self.assertIn("ev=ev-", final_prompt)
        self.assertNotIn('"result_ref": "r1"', final_prompt)  # old envelope gone
        self.assertIn('"result_ref": "r4"', final_prompt)  # recent envelope kept

    def test_budget_warning_visible_before_last_steps(self) -> None:
        planner = ScriptedPlanner([self._search()], repeat_last=True)
        outcome = _run(_loop(self.repository, planner, max_steps=4))
        self.assertFalse(outcome.completed)
        # The planner deciding step 4 must see the warning appended after step 2.
        third_prompt = planner.prompts[2][1]
        self.assertIn("هشدار بودجه", third_prompt)

    def test_recent_full_steps_validation(self) -> None:
        with self.assertRaises(DomainError):
            AgenticConfig(recent_full_steps=-1)


class PlannerParsingTests(unittest.TestCase):
    def test_trailing_prose_with_braces_no_longer_breaks_parsing(self) -> None:
        from legal_agent_core.agentic.planner import _parse_decision

        raw = (
            '{"action":"tool","tool":"search_provisions",'
            '"args":{"query":"مهلت"}}\n\n'
            "توضیح: چون پرسش درباره مهلت است {پیشنهاد می‌شود} ادامه دهیم."
        )
        from legal_agent_core.llm import ProviderResponse, TokenUsage

        response = ProviderResponse("r", "m", raw, TokenUsage())
        decision, error = _parse_decision(response)
        self.assertIsNone(error)
        self.assertIsNotNone(decision)
        self.assertEqual(decision.tool, "search_provisions")  # type: ignore[union-attr]


class DeadEndGuardTests(unittest.TestCase):
    def test_three_empty_searches_trigger_dead_end_hint(self) -> None:
        repository = build_canonical()
        # کوئری‌ای که هیچ واژه‌ای از آن در کورپوس نیست → EMPTY پیاپی
        decision = PlannerDecision(
            "tool",
            tool="search_provisions",
            args={
                "query": "فهرست کشتی‌های بادبانی اقیانوس هند",
                "applicable_time": APPLICABLE.isoformat(),
            },
        )
        planner = ScriptedPlanner([decision], repeat_last=True)
        outcome = _run(_loop(repository, planner, max_steps=5))
        self.assertFalse(outcome.completed)
        # از گام ۴ به بعد (بعد از ۳ خالی) هشدار بن‌بست باید دیده شود
        fourth_prompt = planner.prompts[3][1]
        self.assertIn("هشدار بن‌بست", fourth_prompt)
        # در گام ۳ (فقط ۲ خالی) هنوز نباید باشد
        third_prompt = planner.prompts[2][1]
        self.assertNotIn("هشدار بن‌بست", third_prompt)


if __name__ == "__main__":
    unittest.main()
