"""Law notebook: organization-scoped learned navigation memory.

Docs/qavanin/06_law_notebook.md §6–§8. The notebook is a *shortcut* layer over
the display tree — never a source of legal truth. It records which routes were
taken, which ones led to a verified answer, and (separately, as
``ProgressiveRelation`` candidates) implicit relations discovered in research.
For any final answer the agent re-reads canonical text; a notebook page is
never cited.

This module is pure: :func:`capture_events` derives the append-only event log
from a finished research episode, and the caller persists it. Weighting rule
(§7): merely opening a document earns nothing; only a *verified* answer
strengthens the routes of that episode.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from hashlib import sha1
from typing import Any

from .canonical import require_text, utc_now
from .research import (
    GraphNamespace,
    ProgressiveEdgeType,
    ProgressiveRelation,
    ResearchEpisode,
    TrustStatus,
    TruthClass,
)


class NotebookEventType(StrEnum):
    ROUTE_TAKEN = "route_taken"
    RELATION_DISCOVERED = "relation_discovered"
    ANSWER_CONFIRMED = "answer_confirmed"
    SUMMARY_MERGED = "summary_merged"


@dataclass(frozen=True, slots=True)
class NotebookEvent:
    event_id: str
    organization_id: str
    event_type: NotebookEventType
    episode_id: str | None = None
    topic_id: str | None = None
    node_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        require_text(self.event_id, "event_id")
        require_text(self.organization_id, "organization_id")


@dataclass(frozen=True, slots=True)
class NotebookPage:
    organization_id: str
    node_id: str
    kind: str
    summary: str = ""
    path: str | None = None
    summary_version: int = 0
    based_on: dict[str, Any] = field(default_factory=dict)
    needs_revalidation: bool = False
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        require_text(self.organization_id, "organization_id")
        require_text(self.node_id, "node_id")
        require_text(self.kind, "kind")


@dataclass(frozen=True, slots=True)
class NotebookRoute:
    organization_id: str
    topic_id: str
    to_node_id: str
    weight: float = 0.0
    hits: int = 0
    confirmed_hits: int = 0
    status: str = "candidate"
    source_episode_id: str | None = None


def _event_id(episode_id: str, event_type: NotebookEventType, node_id: str | None) -> str:
    raw = f"{episode_id}|{event_type.value}|{node_id or ''}"
    return "nbkev_" + sha1(raw.encode("utf-8")).hexdigest()[:20]


def derive_candidate_relations(
    episode: ResearchEpisode,
    provision_ids: tuple[str, ...],
    *,
    max_pairs: int = 10,
) -> tuple[ProgressiveRelation, ...]:
    """Co-used provisions -> COMMONLY_CHECK_WITH candidates (docs 06 §7).

    When one verified answer leaned on several provisions, those provisions are
    "commonly checked together" — an implicit relation that is NOT in the
    deterministic graph. The caller must filter out pairs already linked by a
    canonical edge; what remains becomes a reviewable candidate. Deterministic
    ids make the write idempotent.
    """
    unique = list(dict.fromkeys(pid for pid in provision_ids if pid))
    candidates: list[ProgressiveRelation] = []
    for index, source in enumerate(unique):
        for target in unique[index + 1 :]:
            if len(candidates) >= max_pairs:
                return tuple(candidates)
            raw = f"{episode.organization_id}|commonly_check_with|{source}|{target}"
            relation_id = "nbkrel_" + sha1(raw.encode("utf-8")).hexdigest()[:20]
            candidates.append(
                ProgressiveRelation(
                    relation_id=relation_id,
                    organization_id=episode.organization_id,
                    source_node_id=source,
                    target_node_id=target,
                    edge_type=ProgressiveEdgeType.COMMONLY_CHECK_WITH,
                    truth_class=TruthClass.DETERMINISTIC,
                    source_episode_id=episode.episode_id,
                    evidence_version_ids=(),
                    source_namespace=GraphNamespace.RESEARCH,
                    target_namespace=GraphNamespace.CANONICAL,
                    confidence=0.5,
                    status=TrustStatus.CANDIDATE,
                )
            )
    return tuple(candidates)


def capture_events(episode: ResearchEpisode, *, completed: bool) -> tuple[NotebookEvent, ...]:
    """Derive the notebook event log for a finished episode.

    Emits one ``route_taken`` per visited node (cheap, unweighted) and, only
    when the episode produced a verified answer, a single ``answer_confirmed``
    carrying the visited nodes and accepted evidence — the signal the route
    weights are built from.
    """
    events: list[NotebookEvent] = []
    for node_id in episode.visited_nodes:
        events.append(
            NotebookEvent(
                event_id=_event_id(episode.episode_id, NotebookEventType.ROUTE_TAKEN, node_id),
                organization_id=episode.organization_id,
                event_type=NotebookEventType.ROUTE_TAKEN,
                episode_id=episode.episode_id,
                node_id=node_id,
            )
        )
    if completed and episode.visited_nodes:
        events.append(
            NotebookEvent(
                event_id=_event_id(episode.episode_id, NotebookEventType.ANSWER_CONFIRMED, None),
                organization_id=episode.organization_id,
                event_type=NotebookEventType.ANSWER_CONFIRMED,
                episode_id=episode.episode_id,
                payload={
                    "visited_nodes": list(episode.visited_nodes),
                    "evidence_ids": list(episode.evidence_ids),
                    "question": episode.question,
                },
            )
        )
    return tuple(events)
