from __future__ import annotations

import re
import time
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

    def test_search_preserves_compound_legal_phrase_from_question(self) -> None:
        planner = ScriptedPlanner(
            [
                PlannerDecision(
                    "tool",
                    tool="search_provisions",
                    args={
                        "query": "بدهی شرکت مدیر",
                        "applicable_time": APPLICABLE.isoformat(),
                    },
                ),
                PlannerDecision("abstain", abstention_reason="پایان آزمون"),
            ]
        )
        loop = _loop(self.repository, planner, max_steps=2)
        loop.run(
            episode_id="episode-anchor",
            organization_id="org-a",
            user_id="user-1",
            conversation_id="conversation-1",
            question=(
                "آیا خانه مدیرعامل شرکت با مسئولیت محدود برای بدهی حق بیمه "
                "قابل توقیف است؟"
            ),
            applicable_time=APPLICABLE,
        )
        self.assertIn("مسئولیت محدود", loop.last_trace[0].args["query"])

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

    def test_unevidenced_claim_forces_consistent_answer_rewrite(self) -> None:
        # An unevidenced claim cannot be dropped while its prose remains in the
        # answer. The whole answer is returned to the planner for a rewrite.
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
        self.assertIn("claimهای بدون شاهد", planner.prompts[2][1])
        self.assertNotIn("مشاوره حقوقی نیست", outcome.draft.text)
        published = CitationPipeline(self.repository).publish(
            outcome.draft, outcome.verification, outcome.ledger, APPLICABLE
        )
        self.assertTrue(published.citations)

    def test_citing_unknown_evidence_is_caught_before_verification_then_recovers(self) -> None:
        # Citing ids that never entered the ledger (e.g. a graph node id) is now
        # caught as a contract error with actionable feedback, instead of
        # bouncing through a verification rejection.
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
        self.assertIn("شناسه‌های ناشناخته", planner.prompts[1][1])

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

    def test_wall_deadline_is_checked_after_slow_planner_call(self) -> None:
        class SlowPlanner:
            def decide(self, system_prompt: str, user_prompt: str) -> PlannerTurn:
                time.sleep(0.02)
                return _turn(PlannerDecision("answer", answer="دیر رسید."))

        outcome = _run(
            _loop(self.repository, SlowPlanner(), wall_seconds=0.005, max_steps=2)
        )
        self.assertFalse(outcome.completed)
        self.assertIn(
            IncompleteReason.BUDGET_WALL.value,
            outcome.episode.identified_issues,
        )

    def test_remaining_wall_time_is_passed_to_bounded_planner(self) -> None:
        class BoundedPlanner:
            timeout: float | None = None

            def decide_with_timeout(
                self,
                system_prompt: str,
                user_prompt: str,
                *,
                timeout_seconds: float,
            ) -> PlannerTurn:
                self.timeout = timeout_seconds
                return _turn(PlannerDecision("abstain", abstention_reason="آزمون"))

            def decide(self, system_prompt: str, user_prompt: str) -> PlannerTurn:
                raise AssertionError("unbounded planner path used")

        planner = BoundedPlanner()
        _run(_loop(self.repository, planner, wall_seconds=0.2, max_steps=1))
        self.assertIsNotNone(planner.timeout)
        self.assertGreater(planner.timeout, 0.0)  # type: ignore[operator]
        self.assertLessEqual(planner.timeout, 0.2)  # type: ignore[operator]

    def test_planner_call_emits_latency_tokens_and_action(self) -> None:
        events: list[dict] = []
        loop = _loop(
            self.repository,
            ScriptedPlanner(
                [PlannerDecision("abstain", abstention_reason="آزمون ثبت تصمیم")]
            ),
            max_steps=1,
        )
        _run(loop, progress=events.append)

        planner_events = [event for event in events if event["type"] == "planner_call"]
        self.assertEqual(len(planner_events), 1)
        event = planner_events[0]
        self.assertTrue(event["ok"])
        self.assertEqual(event["step"], 1)
        self.assertEqual(event["attempt"], 1)
        self.assertEqual(event["action"], "abstain")
        self.assertEqual(event["input_tokens"], 100)
        self.assertEqual(event["output_tokens"], 100)
        self.assertGreaterEqual(event["latency_ms"], 0)
        self.assertEqual(len(loop.last_planner_trace), 1)

    def test_last_step_disables_native_tool_choice(self) -> None:
        class ForceAwarePlanner:
            forced = False

            def decide(
                self,
                system_prompt: str,
                user_prompt: str,
                *,
                force_answer: bool = False,
            ) -> PlannerTurn:
                self.forced = force_answer
                return _turn(
                    PlannerDecision("abstain", abstention_reason="بودجه پایان یافت")
                )

        planner = ForceAwarePlanner()
        _run(_loop(self.repository, planner, max_steps=1))
        self.assertTrue(planner.forced)

    def test_search_budget_forces_next_turn_to_finalize(self) -> None:
        class ForceAwarePlanner:
            calls = 0
            final_forced = False

            def decide(
                self,
                system_prompt: str,
                user_prompt: str,
                *,
                force_answer: bool = False,
            ) -> PlannerTurn:
                self.calls += 1
                if self.calls == 1:
                    if force_answer:
                        raise AssertionError(
                            "first research turn must keep tools available"
                        )
                    return _turn(
                        PlannerDecision(
                            "tool",
                            tool="search_provisions",
                            args={
                                "query": "مهلت تجدیدنظر",
                                "applicable_time": APPLICABLE.isoformat(),
                            },
                        )
                    )
                self.final_forced = force_answer
                return _turn(
                    PlannerDecision("abstain", abstention_reason="بودجهٔ جست‌وجو تمام شد")
                )

        planner = ForceAwarePlanner()
        _run(
            _loop(
                self.repository,
                planner,
                max_steps=3,
                max_search_steps=1,
            )
        )
        self.assertEqual(planner.calls, 2)
        self.assertTrue(planner.final_forced)

    def test_failed_search_attempt_consumes_search_budget(self) -> None:
        class FailingSearchTools:
            def spec_available(self, name: str) -> bool:
                return False

            def dispatch(self, store, name: str, args: dict):
                raise DomainError("database unavailable")

            def tool_catalog(self) -> str:
                return "search_provisions(query, applicable_time)"

        class ForceAwarePlanner:
            calls = 0
            final_forced = False

            def decide(
                self,
                system_prompt: str,
                user_prompt: str,
                *,
                force_answer: bool = False,
            ) -> PlannerTurn:
                self.calls += 1
                if self.calls == 1:
                    return _turn(
                        PlannerDecision(
                            "tool",
                            tool="search_provisions",
                            args={
                                "query": "مهلت تجدیدنظر",
                                "applicable_time": APPLICABLE.isoformat(),
                            },
                        )
                    )
                self.final_forced = force_answer
                return _turn(
                    PlannerDecision("abstain", abstention_reason="جست‌وجو شکست خورد")
                )

        planner = ForceAwarePlanner()
        loop = ToolResearchLoop(
            FailingSearchTools(),
            planner,
            EvidenceVerifier(self.repository),
            AgenticConfig(max_steps=3, max_search_steps=1),
        )
        _run(loop)
        self.assertEqual(planner.calls, 2)
        self.assertTrue(planner.final_forced)

    def test_total_tool_budget_forces_next_turn_to_finalize(self) -> None:
        class ForceAwarePlanner:
            calls = 0
            final_forced = False

            def decide(
                self,
                system_prompt: str,
                user_prompt: str,
                *,
                force_answer: bool = False,
            ) -> PlannerTurn:
                self.calls += 1
                if self.calls == 1:
                    return _turn(
                        PlannerDecision(
                            "tool",
                            tool="find_in_document",
                            args={
                                "document": "سند ناموجود",
                                "query": "مهلت تجدیدنظر",
                                "applicable_time": APPLICABLE.isoformat(),
                            },
                        )
                    )
                self.final_forced = force_answer
                return _turn(
                    PlannerDecision("abstain", abstention_reason="بودجهٔ ابزار تمام شد")
                )

        planner = ForceAwarePlanner()
        _run(
            _loop(
                self.repository,
                planner,
                max_steps=3,
                max_tool_calls=1,
            )
        )
        self.assertEqual(planner.calls, 2)
        self.assertTrue(planner.final_forced)

    def test_default_policy_does_not_end_after_three_distinct_searches(self) -> None:
        planner = ScriptedPlanner(
            [
                PlannerDecision(
                    "tool",
                    tool="search_provisions",
                    args={"query": query, "applicable_time": APPLICABLE.isoformat()},
                )
                for query in (
                    "مهلت تجدیدنظر",
                    "نحوه اعتراض",
                    "قطعیت مطالبات",
                    "ابلاغ تصمیم",
                )
            ]
            + [PlannerDecision("abstain", abstention_reason="پایان بررسی")]
        )
        loop = _loop(self.repository, planner)
        _run(loop)
        self.assertEqual(
            len([item for item in loop.last_trace if item.tool == "search_provisions" and item.ok]),
            4,
        )
        self.assertNotIn("۳ جست‌وجوی سراسری", planner.prompts[0][0])

    def test_similar_search_is_redirected_without_ending_research(self) -> None:
        class PivotAwarePlanner(ScriptedPlanner):
            exclusions: list[frozenset[str]]

            def __init__(self, script) -> None:
                super().__init__(script)
                self.exclusions = []

            def decide(
                self,
                system_prompt: str,
                user_prompt: str,
                *,
                excluded_tools: frozenset[str] = frozenset(),
            ) -> PlannerTurn:
                self.exclusions.append(excluded_tools)
                return super().decide(system_prompt, user_prompt)

        planner = PivotAwarePlanner(
            [
                PlannerDecision(
                    "tool",
                    tool="search_provisions",
                    args={
                        "query": "مهلت تجدیدنظر اشخاص مقیم",
                        "applicable_time": APPLICABLE.isoformat(),
                    },
                ),
                PlannerDecision(
                    "tool",
                    tool="search_provisions",
                    args={
                        "query": "مهلت تجدیدنظر اشخاص مقیم ایران",
                        "applicable_time": APPLICABLE.isoformat(),
                    },
                ),
                PlannerDecision(
                    "tool",
                    tool="find_in_document",
                    args={
                        "document": "سند ناموجود",
                        "query": "مهلت تجدیدنظر",
                        "applicable_time": APPLICABLE.isoformat(),
                    },
                ),
                PlannerDecision("abstain", abstention_reason="پایان بررسی"),
            ]
        )
        loop = _loop(self.repository, planner, max_steps=4)
        _run(loop)
        self.assertEqual(loop.last_trace[1].error, "near-duplicate broad search")
        self.assertIn("NEAR-DUPLICATE-SEARCH", planner.prompts[2][1])
        self.assertEqual(planner.exclusions[2], frozenset({"search_provisions"}))
        self.assertEqual(loop.last_trace[2].tool, "find_in_document")

    def test_different_queries_with_same_evidence_trigger_route_change(self) -> None:
        repository = self.repository

        class SameResultsTools:
            def __init__(self) -> None:
                self.canonical = LegalResearchTools(repository)

            def spec_available(self, name: str) -> bool:
                return False

            def dispatch(self, store, name: str, args: dict):
                return self.canonical.dispatch(
                    store,
                    name,
                    {
                        "query": "مهلت تجدیدنظر",
                        "applicable_time": APPLICABLE.isoformat(),
                    },
                )

            def tool_catalog(self) -> str:
                return "search_provisions(query, applicable_time)"

        class ObservingPlanner:
            exclusions: list[frozenset[str]]

            def __init__(self) -> None:
                self.exclusions = []

            def decide(
                self,
                system_prompt: str,
                user_prompt: str,
                *,
                excluded_tools: frozenset[str] = frozenset(),
            ) -> PlannerTurn:
                self.exclusions.append(excluded_tools)
                if len(self.exclusions) <= 2:
                    query = (
                        "مهلت تجدیدنظر"
                        if len(self.exclusions) == 1
                        else "آیین دادرسی اعتراض به رای"
                    )
                    return _turn(
                        PlannerDecision(
                            "tool",
                            tool="search_provisions",
                            args={
                                "query": query,
                                "applicable_time": APPLICABLE.isoformat(),
                            },
                        )
                    )
                return _turn(
                    PlannerDecision("abstain", abstention_reason="پایان بررسی")
                )

        planner = ObservingPlanner()
        loop = ToolResearchLoop(
            SameResultsTools(),
            planner,
            EvidenceVerifier(repository),
            AgenticConfig(max_steps=4),
        )
        _run(loop)
        self.assertEqual(planner.exclusions[2], frozenset({"search_provisions"}))
        self.assertEqual(len([item for item in loop.last_trace if item.ok]), 2)

    def test_slow_orientation_tool_cannot_consume_past_run_deadline(self) -> None:
        class SlowOrientationTools:
            def spec_available(self, name: str) -> bool:
                return name == "walk_tree"

            def dispatch(self, store, name: str, args: dict):
                time.sleep(0.2)
                raise AssertionError("late orientation result must be ignored")

            def tool_catalog(self) -> str:
                return "walk_tree(topic, limit?)"

        loop = ToolResearchLoop(
            SlowOrientationTools(),
            ScriptedPlanner([]),
            EvidenceVerifier(self.repository),
            AgenticConfig(wall_seconds=0.01, max_steps=1),
        )
        started = time.monotonic()
        outcome = _run(loop)
        elapsed = time.monotonic() - started
        self.assertFalse(outcome.completed)
        self.assertLess(elapsed, 0.1)
        self.assertIn(
            IncompleteReason.BUDGET_WALL.value,
            outcome.episode.identified_issues,
        )

    def test_slow_research_tool_cannot_consume_past_run_deadline(self) -> None:
        class SlowResearchTools:
            def spec_available(self, name: str) -> bool:
                return False

            def dispatch(self, store, name: str, args: dict):
                time.sleep(0.2)
                raise AssertionError("late tool result must be ignored")

            def tool_catalog(self) -> str:
                return "search_provisions(query, applicable_time)"

        planner = ScriptedPlanner(
            [
                PlannerDecision(
                    "tool",
                    tool="search_provisions",
                    args={"query": "مهلت", "applicable_time": APPLICABLE.isoformat()},
                )
            ]
        )
        loop = ToolResearchLoop(
            SlowResearchTools(),
            planner,
            EvidenceVerifier(self.repository),
            AgenticConfig(wall_seconds=0.01, max_steps=1),
        )
        started = time.monotonic()
        outcome = _run(loop)
        elapsed = time.monotonic() - started
        self.assertFalse(outcome.completed)
        self.assertLess(elapsed, 0.1)
        self.assertIn(
            IncompleteReason.BUDGET_WALL.value,
            outcome.episode.identified_issues,
        )
        self.assertEqual(loop.last_trace[-1].error, "research wall-clock deadline exceeded during tool call")


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

    def _search(self, query: str = "مهلت تجدیدنظر"):
        return PlannerDecision(
            "tool",
            tool="search_provisions",
            args={
                "query": query,
                "applicable_time": APPLICABLE.isoformat(),
            },
        )

    def test_old_steps_compact_recent_stay_full(self) -> None:
        # Identical (tool, args) calls are replayed from the repeat cache, so
        # the burn run uses distinct queries; the 5th fills the step budget.
        planner = ScriptedPlanner(
            [
                self._search("مهلت تجدیدنظر"),
                self._search("مهلت اعتراض"),
                self._search("مهلت واخواهی"),
                self._search("مهلت فرجام"),
                self._search("مهلت مطالبه"),
                PlannerDecision("abstain", abstention_reason="بودجه گام تمام شد"),
            ],
        )
        outcome = _run(_loop(self.repository, planner, max_steps=5))
        self.assertFalse(outcome.completed)  # never answered; budget burn run
        # The LAST planner call is the forced-final grace turn (step 6); the
        # grace record consumes the recent-full window, so envelope assertions
        # target the step-5 deciding call (prompts[-2]): steps 1-2 compacted,
        # steps 3-4 full there.
        final_prompt = planner.prompts[-1][1]
        self.assertIn("گام پایانی", final_prompt)
        step5_prompt = planner.prompts[-2][1]
        self.assertIn("گام‌های قدیمی خلاصه شده‌اند", step5_prompt)
        self.assertIn("TOOL search_provisions", step5_prompt)
        self.assertIn("ev=ev-", step5_prompt)
        self.assertNotIn('"result_ref": "r1"', step5_prompt)  # old envelope gone
        self.assertIn('"result_ref": "r4"', step5_prompt)  # recent envelope kept
        self.assertIn("بستهٔ شواهد پایدار", final_prompt)
        self.assertIn("مهلت درخواست تجدیدنظر", final_prompt)

    def test_cached_repeat_replays_payload_after_original_step_compacts(self) -> None:
        first = self._search("مهلت تجدیدنظر")
        planner = ScriptedPlanner(
            [
                first,
                self._search("مهلت اعتراض"),
                self._search("مهلت واخواهی"),
                first,
                PlannerDecision("answer", answer="مهلت سی روز است."),
            ]
        )
        outcome = _run(
            _loop(self.repository, planner, max_steps=5, recent_full_steps=1)
        )
        self.assertTrue(outcome.completed)
        prompt_after_replay = planner.prompts[4][1]
        self.assertIn("REPLAY-CACHED search_provisions", prompt_after_replay)
        self.assertIn("مهلت درخواست تجدیدنظر", prompt_after_replay)

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

    def test_max_tool_seconds_validation(self) -> None:
        with self.assertRaises(DomainError):
            AgenticConfig(max_tool_seconds=0)

    def test_max_tool_calls_validation(self) -> None:
        with self.assertRaises(DomainError):
            AgenticConfig(max_tool_calls=0)


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

    def test_broad_search_budget_is_enforced_by_controller(self) -> None:
        repository = build_canonical()
        planner = ScriptedPlanner(
            [
                PlannerDecision(
                    "tool",
                    tool="search_provisions",
                    args={"query": query, "applicable_time": APPLICABLE.isoformat()},
                )
                for query in ("مهلت", "اعتراض", "تجدیدنظر", "واخواهی")
            ]
            + [PlannerDecision("abstain", abstention_reason="بودجه تمام شد")]
        )
        loop = _loop(repository, planner, max_steps=4, max_search_steps=2)
        outcome = _run(loop)
        self.assertFalse(outcome.completed)
        self.assertIn("SEARCH-BUDGET-EXHAUSTED", planner.prompts[3][1])
        blocked = [item for item in loop.last_trace if item.error == "broad search budget exhausted"]
        self.assertEqual(len(blocked), 2)


if __name__ == "__main__":
    unittest.main()
