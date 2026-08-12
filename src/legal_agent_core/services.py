from __future__ import annotations

from datetime import UTC, datetime
from itertools import count

from .errors import AuthorizationError, ConflictError, DomainError
from .repositories import ResearchGraphRepository
from .research import (
    Actor,
    Approval,
    MemoryEvent,
    MemoryEventType,
    ProgressiveRelation,
    TrustStatus,
)


class ProgressiveMemoryService:
    """Governed state transitions for an organization research overlay."""

    def __init__(self, repository: ResearchGraphRepository) -> None:
        self.repository = repository
        self._sequence = count(1)

    def _event(
        self,
        *,
        actor: Actor,
        action: MemoryEventType,
        relation: ProgressiveRelation,
        previous: TrustStatus | None,
        reason: str | None,
    ) -> MemoryEvent:
        now = datetime.now(UTC)
        return MemoryEvent(
            event_id=f"memory-event-{next(self._sequence)}",
            organization_id=relation.organization_id,
            actor_id=actor.actor_id,
            action=action,
            target_id=relation.relation_id,
            previous_state=previous,
            new_state=relation.status,
            reason=reason,
            source_episode_id=relation.source_episode_id,
            timestamp=now,
        )

    @staticmethod
    def _same_tenant(actor: Actor, relation: ProgressiveRelation) -> None:
        if actor.organization_id != relation.organization_id:
            raise AuthorizationError("cross-organization progressive-memory access is forbidden")

    @staticmethod
    def _reviewer(actor: Actor) -> None:
        if not actor.can_review_knowledge:
            raise AuthorizationError("knowledge transition requires a legal expert or knowledge steward")

    def create_candidate(self, actor: Actor, proposal: ProgressiveRelation) -> ProgressiveRelation:
        self._same_tenant(actor, proposal)
        if proposal.status != TrustStatus.CANDIDATE or proposal.approval is not None:
            raise DomainError("agents and users may only create candidate knowledge")
        event = self._event(
            actor=actor,
            action=MemoryEventType.CANDIDATE_CREATED,
            relation=proposal,
            previous=None,
            reason=None,
        )
        self.repository.save_relation(proposal, event)
        return proposal

    def approve(self, actor: Actor, relation_id: str, note: str | None = None) -> ProgressiveRelation:
        self._reviewer(actor)
        current = self.repository.get_relation(actor.organization_id, relation_id)
        self._same_tenant(actor, current)
        if current.status not in {TrustStatus.CANDIDATE, TrustStatus.NEEDS_REVALIDATION}:
            raise ConflictError(f"cannot approve relation in {current.status.value} state")
        approval = Approval(
            approved_by=actor.actor_id,
            approved_at=datetime.now(UTC),
            organization_id=actor.organization_id,
            approval_note=note,
            source_episode_id=current.source_episode_id,
        )
        updated = current.transition(TrustStatus.EXPERT_APPROVED, approval)
        event = self._event(
            actor=actor,
            action=(
                MemoryEventType.KNOWLEDGE_REVALIDATED
                if current.status == TrustStatus.NEEDS_REVALIDATION
                else MemoryEventType.CANDIDATE_APPROVED
            ),
            relation=updated,
            previous=current.status,
            reason=note,
        )
        self.repository.save_relation(updated, event)
        return updated

    def reject(self, actor: Actor, relation_id: str, reason: str) -> ProgressiveRelation:
        self._reviewer(actor)
        current = self.repository.get_relation(actor.organization_id, relation_id)
        if current.status != TrustStatus.CANDIDATE:
            raise ConflictError("only candidate knowledge can be rejected")
        updated = current.transition(TrustStatus.REJECTED)
        self.repository.save_relation(
            updated,
            self._event(
                actor=actor,
                action=MemoryEventType.CANDIDATE_REJECTED,
                relation=updated,
                previous=current.status,
                reason=reason,
            ),
        )
        return updated

    def mark_version_superseded(
        self, actor: Actor, old_provision_version_id: str, reason: str
    ) -> tuple[ProgressiveRelation, ...]:
        self._reviewer(actor)
        impacted = self.repository.relations_using_version(
            actor.organization_id, old_provision_version_id, TrustStatus.EXPERT_APPROVED
        )
        updated_relations: list[ProgressiveRelation] = []
        for current in impacted:
            updated = current.transition(TrustStatus.NEEDS_REVALIDATION)
            self.repository.save_relation(
                updated,
                self._event(
                    actor=actor,
                    action=MemoryEventType.KNOWLEDGE_INVALIDATED,
                    relation=updated,
                    previous=current.status,
                    reason=reason,
                ),
            )
            updated_relations.append(updated)
        return tuple(updated_relations)

