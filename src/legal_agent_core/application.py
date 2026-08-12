from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from time import perf_counter
from typing import Protocol
from uuid import uuid4

from .answering import CitationPipeline, PublishedAnswer
from .errors import NotFoundError
from .navigation import NavigationLoop, ResearchOutcome
from .observability import MetricsRegistry
from .research import Actor, ProgressiveRelation, TrustStatus
from .security import AuthorizationService, Permission, Principal
from .services import ProgressiveMemoryService


@dataclass(frozen=True, slots=True)
class ResearchCommand:
    organization_id: str
    question: str
    applicable_time: date
    document_scope: tuple[str, ...] = ()
    conversation_id: str | None = None


@dataclass(frozen=True, slots=True)
class ResearchRecord:
    organization_id: str
    user_id: str
    outcome: ResearchOutcome
    answer: PublishedAnswer | None


class ResearchRecordRepository(Protocol):
    def save(self, record: ResearchRecord) -> None: ...

    def get(self, organization_id: str, episode_id: str) -> ResearchRecord: ...


class InMemoryResearchRecordRepository:
    def __init__(self) -> None:
        self._records: dict[tuple[str, str], ResearchRecord] = {}

    def save(self, record: ResearchRecord) -> None:
        self._records[(record.organization_id, record.outcome.episode.episode_id)] = (
            record
        )

    def get(self, organization_id: str, episode_id: str) -> ResearchRecord:
        try:
            return self._records[(organization_id, episode_id)]
        except KeyError as exc:
            raise NotFoundError(
                f"research episode {episode_id!r} was not found"
            ) from exc


class ResearchApplicationService:
    def __init__(
        self,
        navigation: NavigationLoop,
        citations: CitationPipeline,
        records: ResearchRecordRepository,
        authorization: AuthorizationService,
        metrics: MetricsRegistry,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.navigation = navigation
        self.citations = citations
        self.records = records
        self.authorization = authorization
        self.metrics = metrics
        self.id_factory = id_factory or (lambda: uuid4().hex)

    def run(self, principal: Principal, command: ResearchCommand) -> ResearchRecord:
        self.authorization.require(
            principal, Permission.RUN_RESEARCH, command.organization_id
        )
        episode_id = f"episode-{self.id_factory()}"
        conversation_id = command.conversation_id or f"conversation-{self.id_factory()}"
        started = perf_counter()
        self.metrics.increment(
            "research_started_total", organization_id=command.organization_id
        )
        try:
            outcome = self.navigation.run(
                episode_id=episode_id,
                organization_id=command.organization_id,
                user_id=principal.actor_id,
                conversation_id=conversation_id,
                question=command.question,
                applicable_time=command.applicable_time,
                document_scope=command.document_scope,
            )
            answer = (
                self.citations.publish(
                    outcome.draft,
                    outcome.verification,
                    outcome.ledger,
                    command.applicable_time,
                )
                if outcome.completed
                else None
            )
            record = ResearchRecord(
                command.organization_id, principal.actor_id, outcome, answer
            )
            self.records.save(record)
            metric = (
                "research_completed_total"
                if outcome.completed
                else "research_incomplete_total"
            )
            self.metrics.increment(metric, organization_id=command.organization_id)
            return record
        except Exception:
            self.metrics.increment(
                "research_failed_total", organization_id=command.organization_id
            )
            raise
        finally:
            self.metrics.observe(
                "research_duration_ms",
                (perf_counter() - started) * 1000,
                organization_id=command.organization_id,
            )

    def get(
        self, principal: Principal, organization_id: str, episode_id: str
    ) -> ResearchRecord:
        self.authorization.require(principal, Permission.READ_RESEARCH, organization_id)
        return self.records.get(organization_id, episode_id)


class KnowledgeReviewApplicationService:
    def __init__(
        self,
        memory: ProgressiveMemoryService,
        authorization: AuthorizationService,
        metrics: MetricsRegistry,
    ) -> None:
        self.memory = memory
        self.authorization = authorization
        self.metrics = metrics

    def list_queue(
        self,
        principal: Principal,
        organization_id: str,
        statuses: frozenset[TrustStatus],
    ) -> tuple[ProgressiveRelation, ...]:
        self.authorization.require(
            principal, Permission.REVIEW_KNOWLEDGE, organization_id
        )
        return self.memory.repository.list_relations(organization_id, statuses)

    def approve(
        self,
        principal: Principal,
        organization_id: str,
        relation_id: str,
        note: str | None = None,
    ) -> ProgressiveRelation:
        self.authorization.require(
            principal, Permission.REVIEW_KNOWLEDGE, organization_id
        )
        result = self.memory.approve(self._actor(principal), relation_id, note)
        self.metrics.increment(
            "knowledge_approved_total", organization_id=organization_id
        )
        return result

    def reject(
        self,
        principal: Principal,
        organization_id: str,
        relation_id: str,
        reason: str,
    ) -> ProgressiveRelation:
        self.authorization.require(
            principal, Permission.REVIEW_KNOWLEDGE, organization_id
        )
        result = self.memory.reject(self._actor(principal), relation_id, reason)
        self.metrics.increment(
            "knowledge_rejected_total", organization_id=organization_id
        )
        return result

    @staticmethod
    def _actor(principal: Principal) -> Actor:
        return Actor(
            principal.actor_id,
            principal.organization_id,
            frozenset(role.value for role in principal.roles),
        )
