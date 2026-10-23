from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from time import perf_counter
from typing import Protocol
from uuid import uuid4

from .answering import CitationPipeline, PublishedAnswer
from .errors import ConflictError, DomainError, NotFoundError
from .navigation import NavigationLoop, ResearchOutcome
from .notebook import capture_events, derive_candidate_relations
from .observability import MetricsRegistry
from .research import Actor, ProgressiveRelation, TrustStatus
from .security import AuthorizationService, Permission, Principal
from .services import ProgressiveMemoryService
from .verification import EvidenceDisposition


class ResearchStrategy(StrEnum):
    NAVIGATION = "navigation"
    AGENTIC = "agentic"


@dataclass(frozen=True, slots=True)
class ResearchCommand:
    organization_id: str
    question: str
    applicable_time: date
    document_scope: tuple[str, ...] = ()
    conversation_id: str | None = None
    strategy: ResearchStrategy = ResearchStrategy.NAVIGATION


@dataclass(frozen=True, slots=True)
class ResearchRecord:
    organization_id: str
    user_id: str
    outcome: ResearchOutcome
    answer: PublishedAnswer | None


class ResearchRecordRepository(Protocol):
    def save(self, record: ResearchRecord) -> None: ...

    def get(self, organization_id: str, episode_id: str) -> ResearchRecord: ...

    def list_history(
        self,
        organization_id: str,
        user_id: str | None = None,
        limit: int = 100,
    ) -> tuple[ResearchRecord, ...]: ...


class InMemoryResearchRecordRepository:
    def __init__(self, seed: tuple[ResearchRecord, ...] = ()) -> None:
        self._records: dict[tuple[str, str], ResearchRecord] = {}
        self._lock = threading.Lock()
        for record in seed:
            self._records[(record.organization_id, record.outcome.episode.episode_id)] = (
                record
            )

    def save(self, record: ResearchRecord) -> None:
        with self._lock:
            self._records[
                (record.organization_id, record.outcome.episode.episode_id)
            ] = record

    def get(self, organization_id: str, episode_id: str) -> ResearchRecord:
        try:
            return self._records[(organization_id, episode_id)]
        except KeyError as exc:
            raise NotFoundError(
                f"research episode {episode_id!r} was not found"
            ) from exc

    def list_history(
        self,
        organization_id: str,
        user_id: str | None = None,
        limit: int = 100,
    ) -> tuple[ResearchRecord, ...]:
        matches = (
            record
            for record in self._records.values()
            if record.organization_id == organization_id
            and (user_id is None or record.user_id == user_id)
        )
        return tuple(sorted(matches, key=_record_sort_key, reverse=True))[:limit]


def _record_sort_key(record: ResearchRecord) -> tuple[str, str]:
    return (
        record.outcome.episode.created_at.isoformat(),
        record.outcome.episode.episode_id,
    )


class ResearchHistoryStore:
    """Durable, self-contained history of the API display payload.

    Persists the exact ``dict`` the workspace renders (see ``api._research_response``)
    so past research can be reopened across sessions and restarts without
    reconstructing the full domain object graph.
    """

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self._payloads: dict[tuple[str, str], dict] = {}
        self._lock = threading.Lock()
        if path is not None:
            self._path = os.fspath(path)
            self._directory = os.path.dirname(os.path.abspath(self._path))
            self._load()
        else:
            self._path = None
            self._directory = ""

    def save(self, organization_id: str, episode_id: str, payload: dict) -> None:
        with self._lock:
            self._payloads[(organization_id, episode_id)] = payload
            if self._path is not None:
                self._persist()

    def get(self, organization_id: str, episode_id: str) -> dict:
        with self._lock:
            try:
                return self._payloads[(organization_id, episode_id)]
            except KeyError as exc:
                raise NotFoundError(
                    f"research episode {episode_id!r} was not found"
                ) from exc

    def list(self, organization_id: str, limit: int = 100) -> tuple[dict, ...]:
        with self._lock:
            matches = [
                payload
                for (tenant, _), payload in self._payloads.items()
                if tenant == organization_id
            ]
        return tuple(
            sorted(
                matches, key=lambda item: item.get("trace", {}).get("created_at", "")
            )
        )[:limit]

    def _load(self) -> None:
        if not os.path.exists(self._path):
            return
        try:
            with open(self._path, encoding="utf-8") as handle:
                document = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return
        for organization_id, episodes in document.get("episodes", {}).items():
            for episode_id, payload in episodes.items():
                self._payloads[(organization_id, episode_id)] = payload

    def _persist(self) -> None:
        os.makedirs(self._directory, exist_ok=True)
        grouped: dict[str, dict] = {}
        for (organization_id, episode_id), payload in self._payloads.items():
            grouped.setdefault(organization_id, {})[episode_id] = payload
        document = {"episodes": grouped}
        descriptor, temporary = tempfile.mkstemp(
            prefix=".history-", dir=self._directory
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(document, handle, ensure_ascii=False, indent=2)
            os.replace(temporary, self._path)
        finally:
            if os.path.exists(temporary):
                os.remove(temporary)


class ResearchApplicationService:
    def __init__(
        self,
        navigation: NavigationLoop,
        citations: CitationPipeline,
        records: ResearchRecordRepository,
        authorization: AuthorizationService,
        metrics: MetricsRegistry,
        id_factory: Callable[[], str] | None = None,
        agentic: object | None = None,
        notebook: object | None = None,
        notebook_topic_resolver: Callable[[str], str | None] | None = None,
        memory: ProgressiveMemoryService | None = None,
    ) -> None:
        self.navigation = navigation
        self.agentic = agentic
        self.citations = citations
        self.records = records
        self.authorization = authorization
        self.metrics = metrics
        self.id_factory = id_factory or (lambda: uuid4().hex)
        self.notebook = notebook
        self.notebook_topic_resolver = notebook_topic_resolver
        self.memory = memory

    def run(self, principal: Principal, command: ResearchCommand) -> ResearchRecord:
        self.authorization.require(
            principal, Permission.RUN_RESEARCH, command.organization_id
        )
        loop = self._loop(command.strategy)
        episode_id = f"episode-{self.id_factory()}"
        conversation_id = command.conversation_id or f"conversation-{self.id_factory()}"
        started = perf_counter()
        self.metrics.increment(
            "research_started_total", organization_id=command.organization_id
        )
        try:
            outcome = loop.run(
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

    def _capture_notebook(self, outcome: ResearchOutcome, organization_id: str) -> None:
        """Record learned navigation from a finished episode (docs 06 §7).

        Append-only events are always written; route weights are strengthened
        ONLY for the nodes the accepted answer actually used (not every visited
        node), and only when the episode produced a verified answer. Never
        raises: notebook capture is best-effort.
        """
        if self.notebook is None:
            return
        try:
            events = capture_events(outcome.episode, completed=outcome.completed)
            self.notebook.save_events(events)
            if not outcome.completed:
                return
            for node_id in self._effective_nodes(outcome):
                topic_id = self._topic_for(node_id)
                if not topic_id:
                    continue
                self.notebook.upsert_route(
                    organization_id,
                    topic_id,
                    node_id,
                    hit=True,
                    confirmed=True,
                    source_episode_id=outcome.episode.episode_id,
                )
                # Also record at the instrument level so a later visit to the
                # same law can jump straight to the article instead of walking
                # the domain notes again.
                if node_id.startswith("prov_"):
                    instrument_id = self._instrument_of(node_id)
                    if instrument_id and instrument_id != topic_id:
                        self.notebook.upsert_route(
                            organization_id,
                            instrument_id,
                            node_id,
                            hit=True,
                            confirmed=True,
                            source_episode_id=outcome.episode.episode_id,
                        )
            self._capture_relations(outcome, organization_id)
        except Exception:  # capture is best-effort
            logging.getLogger(__name__).warning("notebook capture failed", exc_info=True)

    def _effective_nodes(self, outcome: ResearchOutcome) -> list[str]:
        """Provisions the accepted answer relied on, plus their instruments."""
        accepted = [
            entry.evidence.provision_id
            for entry in outcome.ledger.entries
            if entry.disposition == EvidenceDisposition.ACCEPTED
        ]
        nodes: list[str] = []
        for provision_id in dict.fromkeys(accepted):
            nodes.append(provision_id)
            instrument_id = self._instrument_of(provision_id)
            if instrument_id:
                nodes.append(instrument_id)
        return list(dict.fromkeys(nodes))

    def _instrument_of(self, provision_id: str) -> str | None:
        canonical = getattr(self.citations, "canonical_repository", None)
        if canonical is None:
            return None
        try:
            return canonical.get_provision(provision_id).instrument_id
        except Exception:  # noqa: BLE001 - unknown id is not fatal
            return None

    def _topic_for(self, node_id: str) -> str | None:
        """Topic for a node; provisions are mapped through their instrument."""
        if self.notebook_topic_resolver is None:
            return "root"
        topic = self.notebook_topic_resolver(node_id)
        if topic and topic != "root":
            return topic
        instrument_id = self._instrument_of(node_id)
        if instrument_id:
            mapped = self.notebook_topic_resolver(instrument_id)
            if mapped:
                return mapped
        return topic or "root"

    def _capture_relations(self, outcome: ResearchOutcome, organization_id: str) -> None:
        """Discover implicit relations between co-used provisions (docs 06 §7).

        Provisions that one verified answer relied on together are
        "commonly checked with" each other. Pairs already linked by a canonical
        edge are skipped; the rest become reviewable ProgressiveRelation
        candidates. Best-effort and bounded.
        """
        if self.memory is None:
            return
        accepted = [
            entry.evidence.provision_id
            for entry in outcome.ledger.entries
            if entry.disposition == EvidenceDisposition.ACCEPTED
        ]
        if len(accepted) < 2:
            return
        candidates = derive_candidate_relations(outcome.episode, tuple(accepted))
        if not candidates:
            return
        linked = self._linked_provision_pairs(accepted)
        actor = Actor("notebook-capture", organization_id)
        for candidate in candidates:
            pair = frozenset((candidate.source_node_id, candidate.target_node_id))
            if pair in linked:
                continue
            try:
                self.memory.create_candidate(actor, candidate)
            except Exception:  # noqa: BLE001 - one bad candidate must not abort
                logging.getLogger(__name__).debug(
                    "candidate relation skipped: %s", candidate.relation_id
                )

    def _linked_provision_pairs(self, provision_ids: list[str]) -> set[frozenset[str]]:
        """Canonical-edge-linked pairs among the given provisions."""
        canonical = getattr(self.citations, "canonical_repository", None)
        if canonical is None:
            return set()
        wanted = set(provision_ids)
        linked: set[frozenset[str]] = set()
        try:
            for provision_id in provision_ids:
                for edge in canonical.edges_touching(provision_id):
                    other = (
                        edge.target_node_id
                        if edge.source_node_id == provision_id
                        else edge.source_node_id
                    )
                    if other in wanted:
                        linked.add(frozenset((provision_id, other)))
        except Exception:  # noqa: BLE001 - filtering is best-effort
            return set()
        return linked

    def get(
        self, principal: Principal, organization_id: str, episode_id: str
    ) -> ResearchRecord:
        self.authorization.require(principal, Permission.READ_RESEARCH, organization_id)
        return self.records.get(organization_id, episode_id)

    def stream_events(
        self, principal: Principal, command: ResearchCommand
    ) -> Iterator[dict]:
        """Yield live progress events, then a final ``completed`` event.

        The final event carries the :class:`ResearchRecord`; transport layers
        convert it to their own payload shape. Progress events flow only for
        strategies that support step-level hooks (the agentic loop); the
        navigation strategy still streams start/final events.
        """
        import queue
        import threading

        self.authorization.require(
            principal, Permission.RUN_RESEARCH, command.organization_id
        )
        loop = self._loop(command.strategy)
        events: queue.Queue = queue.Queue()
        supports_progress = command.strategy is ResearchStrategy.AGENTIC

        episode_id = f"episode-{self.id_factory()}"
        conversation_id = command.conversation_id or f"conversation-{self.id_factory()}"
        started = perf_counter()
        self.metrics.increment(
            "research_started_total", organization_id=command.organization_id
        )
        yield {
            "type": "started",
            "episode_id": episode_id,
            "conversation_id": conversation_id,
            "strategy": command.strategy.value,
        }
        run_kwargs: dict = {}
        if supports_progress:
            run_kwargs["progress"] = lambda event: events.put(("progress", event))

        def worker() -> None:
            try:
                outcome = loop.run(
                    episode_id=episode_id,
                    organization_id=command.organization_id,
                    user_id=principal.actor_id,
                    conversation_id=conversation_id,
                    question=command.question,
                    applicable_time=command.applicable_time,
                    document_scope=command.document_scope,
                    **run_kwargs,
                )
            except Exception as exc:  # noqa: BLE001 — streamed to the caller
                events.put(("failed", str(exc)))
                return
            self._capture_notebook(outcome, command.organization_id)
            events.put(("outcome", outcome))

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        while True:
            kind, payload = events.get()
            if kind == "progress":
                yield payload
                continue
            break
        thread.join()

        if kind == "failed":
            self.metrics.increment(
                "research_failed_total", organization_id=command.organization_id
            )
            yield {"type": "failed", "message": str(payload)}
            return
        outcome = payload
        try:
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
        finally:
            self.metrics.observe(
                "research_duration_ms",
                (perf_counter() - started) * 1000,
                organization_id=command.organization_id,
            )
        yield {"type": "completed", "record": record}

    def _loop(self, strategy: ResearchStrategy):
        if strategy is ResearchStrategy.NAVIGATION:
            return self.navigation
        if self.agentic is None:
            raise DomainError("agentic research strategy is not configured")
        # A provider exposes ``.loop`` and rebuilds it from the live settings;
        # a plain loop object is returned unchanged (tests/demo).
        return getattr(self.agentic, "loop", self.agentic)


@dataclass(frozen=True, slots=True)
class ConfirmationTicket:
    """One-shot HITL approval ticket bound to op + argument hash."""

    ticket_id: str
    organization_id: str
    relation_id: str
    action: str
    payload_hash: str
    actor_id: str
    created_at: datetime
    expires_at: datetime


class KnowledgeReviewApplicationService:
    """Expert review with optional human-in-the-loop confirmation tickets.

    ``request_confirmation`` binds a one-shot ticket to
    ``(action, relation_id, organization_id, payload)`` — the exact opencti
    v2-B pattern: approval bound to the operation plus a hash of its
    arguments, re-authorization before execution, result verification after,
    and idempotency keys so transport retries never double-apply.
    """

    TICKET_TTL_SECONDS = 600

    def __init__(
        self,
        memory: ProgressiveMemoryService,
        authorization: AuthorizationService,
        metrics: MetricsRegistry,
        strict_confirmation: bool = False,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self.memory = memory
        self.authorization = authorization
        self.metrics = metrics
        self.strict_confirmation = strict_confirmation
        self._now = now_factory or (lambda: datetime.now(UTC))
        self._pending: dict[str, ConfirmationTicket] = {}
        self._completed: dict[tuple[str, str], ProgressiveRelation] = {}

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

    def request_confirmation(
        self,
        principal: Principal,
        organization_id: str,
        relation_id: str,
        action: str,
        note: str | None = None,
        reason: str | None = None,
    ) -> ConfirmationTicket:
        if action not in {"approve", "reject"}:
            raise DomainError("review action must be 'approve' or 'reject'")
        if action == "reject" and not (reason and reason.strip()):
            raise DomainError("rejecting a relation requires a reason")
        self.authorization.require(
            principal, Permission.REVIEW_KNOWLEDGE, organization_id
        )
        self.memory.repository.get_relation(organization_id, relation_id)
        now = self._now()
        ticket = ConfirmationTicket(
            ticket_id=uuid4().hex,
            organization_id=organization_id,
            relation_id=relation_id,
            action=action,
            payload_hash=self._payload_hash(
                action, relation_id, organization_id, note, reason
            ),
            actor_id=principal.actor_id,
            created_at=now,
            expires_at=now + timedelta(seconds=self.TICKET_TTL_SECONDS),
        )
        self._pending[ticket.ticket_id] = ticket
        return ticket

    def confirm(
        self,
        principal: Principal,
        organization_id: str,
        relation_id: str,
        action: str,
        ticket_id: str,
        note: str | None = None,
        reason: str | None = None,
        idempotency_key: str | None = None,
    ) -> ProgressiveRelation:
        if idempotency_key:
            cached = self._completed.get((principal.actor_id, idempotency_key))
            if cached is not None:
                return cached
        ticket = self._pending.get(ticket_id)
        if ticket is None:
            raise NotFoundError(f"confirmation ticket {ticket_id!r} is unknown or consumed")
        del self._pending[ticket_id]
        if ticket.action != action or ticket.relation_id != relation_id:
            raise DomainError("confirmation ticket does not match the review action")
        if ticket.organization_id != organization_id:
            raise DomainError("confirmation ticket belongs to another organization")
        if ticket.actor_id != principal.actor_id:
            raise DomainError("confirmation ticket belongs to another reviewer")
        if self._now() > ticket.expires_at:
            raise DomainError("confirmation ticket has expired")
        if ticket.payload_hash != self._payload_hash(
            action, relation_id, organization_id, note, reason
        ):
            raise DomainError("confirmation payload changed after the ticket was issued")

        self.authorization.require(
            principal, Permission.REVIEW_KNOWLEDGE, organization_id
        )
        result = self._apply(principal, organization_id, action, relation_id, note, reason)
        if action == "approve" and result.status != TrustStatus.EXPERT_APPROVED:
            raise ConflictError("post-execution verification failed: relation is not approved")
        if action == "reject" and result.status != TrustStatus.REJECTED:
            raise ConflictError("post-execution verification failed: relation is not rejected")
        if idempotency_key:
            self._completed[(principal.actor_id, idempotency_key)] = result
        return result

    def approve(
        self,
        principal: Principal,
        organization_id: str,
        relation_id: str,
        note: str | None = None,
    ) -> ProgressiveRelation:
        self._require_direct_path(principal, organization_id)
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
        self._require_direct_path(principal, organization_id)
        result = self.memory.reject(self._actor(principal), relation_id, reason)
        self.metrics.increment(
            "knowledge_rejected_total", organization_id=organization_id
        )
        return result

    def _require_direct_path(
        self, principal: Principal, organization_id: str
    ) -> None:
        if self.strict_confirmation:
            raise DomainError(
                "strict mode: obtain a confirmation ticket via request_confirmation"
            )
        self.authorization.require(
            principal, Permission.REVIEW_KNOWLEDGE, organization_id
        )

    def _apply(
        self,
        principal: Principal,
        organization_id: str,
        action: str,
        relation_id: str,
        note: str | None,
        reason: str | None,
    ) -> ProgressiveRelation:
        actor = self._actor(principal)
        if action == "approve":
            result = self.memory.approve(actor, relation_id, note)
            self.metrics.increment(
                "knowledge_approved_total", organization_id=organization_id
            )
        else:
            result = self.memory.reject(actor, relation_id, reason or "")
            self.metrics.increment(
                "knowledge_rejected_total", organization_id=organization_id
            )
        return result

    @staticmethod
    def _payload_hash(
        action: str,
        relation_id: str,
        organization_id: str,
        note: str | None,
        reason: str | None,
    ) -> str:
        identity = f"{action}|{relation_id}|{organization_id}|{note or ''}|{reason or ''}"
        return hashlib.sha256(identity.encode()).hexdigest()

    @staticmethod
    def _actor(principal: Principal) -> Actor:
        return Actor(
            principal.actor_id,
            principal.organization_id,
            frozenset(role.value for role in principal.roles),
        )
