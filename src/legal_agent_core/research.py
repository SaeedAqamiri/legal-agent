from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from .canonical import require_text, utc_now, validate_confidence
from .errors import DomainError

_KEEP_APPROVAL = object()


class ResearchActionType(StrEnum):
    SEARCHED = "searched"
    OPENED = "opened"
    FOLLOWED_REFERENCE = "followed_reference"
    CHECKED_DEFINITION = "checked_definition"
    CHECKED_VERSION = "checked_version"
    CHECKED_AMENDMENT = "checked_amendment"
    FOUND_EXCEPTION = "found_exception"
    ACCEPTED_EVIDENCE = "accepted_evidence"
    REJECTED_EVIDENCE = "rejected_evidence"


class ProgressiveNodeType(StrEnum):
    ISSUE = "issue"
    CONCEPT = "concept"
    RULE = "rule"
    CONDITION = "condition"
    EXCEPTION = "exception"
    SCOPE = "scope"
    PROCEDURE = "procedure"
    INTERPRETATION = "interpretation"
    RESEARCH_ROUTE = "research_route"
    ORGANIZATIONAL_NOTE = "organizational_note"
    PRIOR_ANALYSIS = "prior_analysis"


class GraphNamespace(StrEnum):
    CANONICAL = "canonical"
    RESEARCH = "research"


class ProgressiveEdgeType(StrEnum):
    GOVERNED_BY = "governed_by"
    LIMITED_BY = "limited_by"
    CONSTRAINED_BY = "constrained_by"
    SUBJECT_TO = "subject_to"
    EXCEPTION_TO = "exception_to"
    DEPENDS_ON = "depends_on"
    APPLIES_WHEN = "applies_when"
    RELEVANT_TO = "relevant_to"
    INTERPRETED_AS = "interpreted_as"
    POTENTIALLY_CONFLICTS_WITH = "potentially_conflicts_with"
    RESEARCH_NEXT = "research_next"
    COMMONLY_CHECK_WITH = "commonly_check_with"
    SUPPORTED_BY = "supported_by"
    DERIVED_FROM = "derived_from"


class TruthClass(StrEnum):
    EXPLICIT = "explicit"
    DETERMINISTIC = "deterministic"
    INFERRED = "inferred"
    EXPERT_ASSERTED = "expert_asserted"


class TrustStatus(StrEnum):
    CANDIDATE = "candidate"
    EXPERT_APPROVED = "expert_approved"
    REJECTED = "rejected"
    NEEDS_REVALIDATION = "needs_revalidation"
    INVALIDATED = "invalidated"
    ARCHIVED = "archived"


class MemoryEventType(StrEnum):
    CANDIDATE_CREATED = "candidate_created"
    CANDIDATE_EDITED = "candidate_edited"
    CANDIDATE_APPROVED = "candidate_approved"
    CANDIDATE_REJECTED = "candidate_rejected"
    KNOWLEDGE_REVALIDATED = "knowledge_revalidated"
    KNOWLEDGE_INVALIDATED = "knowledge_invalidated"
    KNOWLEDGE_ARCHIVED = "knowledge_archived"
    KNOWLEDGE_RESTORED = "knowledge_restored"


class EvidenceStance(StrEnum):
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    CONTEXT = "context"


@dataclass(frozen=True, slots=True)
class Actor:
    actor_id: str
    organization_id: str
    roles: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        require_text(self.actor_id, "actor_id")
        require_text(self.organization_id, "organization_id")

    @property
    def can_review_knowledge(self) -> bool:
        return bool(self.roles & {"knowledge_steward", "legal_expert"})


@dataclass(frozen=True, slots=True)
class ResearchAction:
    action_type: ResearchActionType
    target_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    occurred_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True, slots=True)
class Evidence:
    evidence_id: str
    document_id: str
    document_version_id: str
    provision_id: str
    provision_version_id: str
    source_span_id: str
    page: int
    text: str
    retrieval_method: str
    reason_selected: str
    applicable_time: date
    confidence: float | None = None
    stance: EvidenceStance = EvidenceStance.SUPPORTS

    def __post_init__(self) -> None:
        require_text(self.provision_version_id, "provision_version_id")
        require_text(self.text, "text")
        if self.page < 1:
            raise DomainError("evidence page must be one-based")
        validate_confidence(self.confidence)


@dataclass(slots=True)
class ResearchEpisode:
    episode_id: str
    organization_id: str
    user_id: str
    conversation_id: str
    question: str
    applicable_time: date
    document_scope: tuple[str, ...] = ()
    identified_issues: list[str] = field(default_factory=list)
    actions: list[ResearchAction] = field(default_factory=list)
    visited_nodes: list[str] = field(default_factory=list)
    visited_provisions: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    answer_id: str | None = None
    candidate_knowledge_ids: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=utc_now)

    def record(self, action: ResearchAction) -> None:
        self.actions.append(action)


@dataclass(frozen=True, slots=True)
class Approval:
    approved_by: str
    approved_at: datetime
    organization_id: str
    approval_note: str | None
    source_episode_id: str


@dataclass(frozen=True, slots=True)
class ProgressiveRelation:
    relation_id: str
    organization_id: str
    source_node_id: str
    target_node_id: str
    edge_type: ProgressiveEdgeType
    truth_class: TruthClass
    source_episode_id: str
    evidence_version_ids: tuple[str, ...]
    source_namespace: GraphNamespace = GraphNamespace.RESEARCH
    target_namespace: GraphNamespace = GraphNamespace.CANONICAL
    confidence: float | None = None
    status: TrustStatus = TrustStatus.CANDIDATE
    approval: Approval | None = None
    generated_by_model: str | None = None
    model_profile: str | None = None
    prompt_version: str | None = None
    agent_version: str | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "relation_id",
            "organization_id",
            "source_node_id",
            "target_node_id",
            "source_episode_id",
        ):
            require_text(getattr(self, field_name), field_name)
        validate_confidence(self.confidence)
        if self.status == TrustStatus.EXPERT_APPROVED and self.approval is None:
            raise DomainError("expert-approved knowledge requires approval metadata")
        if self.status in {TrustStatus.CANDIDATE, TrustStatus.REJECTED} and self.approval is not None:
            raise DomainError("unreviewed or rejected candidates cannot have approval metadata")
        if self.truth_class in {TruthClass.INFERRED, TruthClass.EXPERT_ASSERTED} and not self.evidence_version_ids:
            raise DomainError("interpretive knowledge requires source-version evidence")

    @property
    def active_for_navigation(self) -> bool:
        return self.status == TrustStatus.EXPERT_APPROVED

    def transition(
        self,
        status: TrustStatus,
        approval: Approval | None | object = _KEEP_APPROVAL,
    ) -> ProgressiveRelation:
        next_approval = self.approval if approval is _KEEP_APPROVAL else approval
        return replace(self, status=status, approval=next_approval)


@dataclass(frozen=True, slots=True)
class MemoryEvent:
    event_id: str
    organization_id: str
    actor_id: str
    action: MemoryEventType
    target_id: str
    previous_state: TrustStatus | None
    new_state: TrustStatus
    reason: str | None
    source_episode_id: str
    timestamp: datetime = field(default_factory=utc_now)
