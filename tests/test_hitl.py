"""Human-in-the-loop confirmation tickets for expert review (opencti v2-B)."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from legal_agent_core.application import (
    ConfirmationTicket,
    KnowledgeReviewApplicationService,
)
from legal_agent_core.errors import ConflictError, DomainError, NotFoundError
from legal_agent_core.in_memory import InMemoryResearchGraphRepository
from legal_agent_core.observability import MetricsRegistry
from legal_agent_core.research import (
    Actor,
    ProgressiveEdgeType,
    ProgressiveRelation,
    TruthClass,
)
from legal_agent_core.security import AuthorizationService, Principal, Role
from legal_agent_core.services import ProgressiveMemoryService

EXPERT = Principal("expert-1", "org-a", frozenset({Role.LEGAL_EXPERT}))
OTHER_EXPERT = Principal("expert-2", "org-a", frozenset({Role.LEGAL_EXPERT}))


def _service(now=None, strict=False):
    graph = InMemoryResearchGraphRepository()
    memory = ProgressiveMemoryService(graph)
    memory.create_candidate(
        Actor("agent", "org-a"),
        ProgressiveRelation(
            "relation-1",
            "org-a",
            "issue-1",
            "provision-5",
            ProgressiveEdgeType.GOVERNED_BY,
            TruthClass.INFERRED,
            "episode-1",
            ("provision-5-v1",),
            confidence=0.9,
            generated_by_model="model",
            model_profile="profile",
            prompt_version="1",
            agent_version="0.1",
        ),
    )
    clock = {"now": now or datetime.now(UTC)}
    service = KnowledgeReviewApplicationService(
        memory,
        AuthorizationService(),
        MetricsRegistry(),
        strict_confirmation=strict,
        now_factory=lambda: clock["now"],
    )
    return service, clock


class ConfirmationFlowTests(unittest.TestCase):
    def test_request_then_confirm_approves_and_consumes_ticket(self) -> None:
        service, _ = _service()
        ticket = service.request_confirmation(
            EXPERT, "org-a", "relation-1", "approve", note="درست است"
        )
        self.assertIsInstance(ticket, ConfirmationTicket)

        relation = service.confirm(
            EXPERT,
            "org-a",
            "relation-1",
            "approve",
            ticket.ticket_id,
            note="درست است",
        )
        self.assertEqual(relation.status.value, "expert_approved")

        with self.assertRaises(NotFoundError):
            service.confirm(
                EXPERT, "org-a", "relation-1", "approve", ticket.ticket_id, note="درست است"
            )

    def test_tampered_payload_invalidates_ticket(self) -> None:
        service, _ = _service()
        ticket = service.request_confirmation(
            EXPERT, "org-a", "relation-1", "reject", reason="مبنای حقوقی ندارد"
        )
        with self.assertRaises(DomainError):
            service.confirm(
                EXPERT,
                "org-a",
                "relation-1",
                "reject",
                ticket.ticket_id,
                reason="دلیل متفاوت",
            )

    def test_expired_ticket_is_rejected(self) -> None:
        service, clock = _service()
        ticket = service.request_confirmation(
            EXPERT, "org-a", "relation-1", "approve"
        )
        clock["now"] = clock["now"] + timedelta(seconds=601)
        with self.assertRaises(DomainError):
            service.confirm(EXPERT, "org-a", "relation-1", "approve", ticket.ticket_id)

    def test_ticket_is_bound_to_actor_action_and_tenant(self) -> None:
        service, _ = _service()
        ticket = service.request_confirmation(
            EXPERT, "org-a", "relation-1", "approve"
        )
        with self.assertRaises(DomainError):
            service.confirm(OTHER_EXPERT, "org-a", "relation-1", "approve", ticket.ticket_id)
        with self.assertRaises(DomainError):
            service.confirm(EXPERT, "org-a", "relation-1", "reject", ticket.ticket_id)

    def test_reject_requires_reason_at_request_time(self) -> None:
        service, _ = _service()
        with self.assertRaises(DomainError):
            service.request_confirmation(EXPERT, "org-a", "relation-1", "reject")

    def test_idempotency_key_replays_cached_result(self) -> None:
        service, _ = _service()
        first_ticket = service.request_confirmation(
            EXPERT, "org-a", "relation-1", "approve"
        )
        first = service.confirm(
            EXPERT, "org-a", "relation-1", "approve", first_ticket.ticket_id,
            idempotency_key="retry-key-1",
        )
        second_ticket = service.request_confirmation(
            EXPERT, "org-a", "relation-1", "approve"
        )
        replay = service.confirm(
            EXPERT, "org-a", "relation-1", "approve", second_ticket.ticket_id,
            idempotency_key="retry-key-1",
        )
        self.assertEqual(replay.status.value, "expert_approved")
        self.assertEqual(first.relation_id, replay.relation_id)

    def test_strict_mode_blocks_direct_approve(self) -> None:
        service, _ = _service(strict=True)
        with self.assertRaises(DomainError):
            service.approve(EXPERT, "org-a", "relation-1")

    def test_post_execution_verification_catches_mismatch(self) -> None:
        service, _ = _service()
        ticket = service.request_confirmation(
            EXPERT, "org-a", "relation-1", "reject", reason="بدون مبنای کافی"
        )
        original = service.memory.reject
        counter = {"n": 0}

        def flaky_reject(actor, relation_id, reason):
            counter["n"] += 1
            if counter["n"] == 1:
                return service.memory.approve(actor, relation_id, None)
            return original(actor, relation_id, reason)

        service.memory.reject = flaky_reject  # type: ignore[method-assign]
        with self.assertRaises(ConflictError):
            service.confirm(
                EXPERT,
                "org-a",
                "relation-1",
                "reject",
                ticket.ticket_id,
                reason="بدون مبنای کافی",
            )


if __name__ == "__main__":
    unittest.main()
