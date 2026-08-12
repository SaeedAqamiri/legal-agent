from __future__ import annotations

import re
from datetime import datetime
from hashlib import sha256
from typing import Any, Protocol

from ..errors import ConflictError, DomainError, NotFoundError
from ..repositories import ResearchGraphRepository
from ..research import (
    Approval,
    GraphNamespace,
    MemoryEvent,
    MemoryEventType,
    ProgressiveEdgeType,
    ProgressiveRelation,
    TrustStatus,
    TruthClass,
)


class GraphResult(Protocol):
    result_set: list[list[Any]]


class FalkorGraph(Protocol):
    def query(
        self, query: str, params: dict[str, Any] | None = None
    ) -> GraphResult: ...

    def ro_query(
        self, query: str, params: dict[str, Any] | None = None
    ) -> GraphResult: ...


class FalkorClient(Protocol):
    def select_graph(self, graph_id: str) -> FalkorGraph: ...


RELATION_COLUMNS = """
r.relation_id,
r.organization_id,
r.source_node_id,
r.target_node_id,
r.edge_type,
r.truth_class,
r.source_episode_id,
r.evidence_version_ids,
r.source_namespace,
r.target_namespace,
r.confidence,
r.status,
r.approved_by,
r.approved_at,
r.approval_note,
r.approval_organization_id,
r.generated_by_model,
r.model_profile,
r.prompt_version,
r.agent_version
""".strip()


EVENT_COLUMNS = """
e.event_id,
e.organization_id,
e.actor_id,
e.action,
e.target_id,
e.previous_state,
e.new_state,
e.reason,
e.source_episode_id,
e.occurred_at
""".strip()


CREATE_RELATION_QUERY = f"""
MERGE (r:ResearchRelation {{organization_id: $organization_id, relation_id: $relation_id}})
ON CREATE SET r.write_token = $event_id
WITH r
WHERE r.write_token = $event_id AND r.status IS NULL
MERGE (source:GraphNode {{
    organization_id: $organization_id,
    namespace: $source_namespace,
    node_id: $source_node_id
}})
MERGE (target:GraphNode {{
    organization_id: $organization_id,
    namespace: $target_namespace,
    node_id: $target_node_id
}})
MERGE (r)-[:SOURCE]->(source)
MERGE (r)-[:TARGET]->(target)
SET r.source_node_id = $source_node_id,
    r.target_node_id = $target_node_id,
    r.edge_type = $edge_type,
    r.truth_class = $truth_class,
    r.source_episode_id = $source_episode_id,
    r.evidence_version_ids = $evidence_version_ids,
    r.source_namespace = $source_namespace,
    r.target_namespace = $target_namespace,
    r.confidence = $confidence,
    r.status = $status,
    r.approved_by = $approved_by,
    r.approved_at = $approved_at,
    r.approval_note = $approval_note,
    r.approval_organization_id = $approval_organization_id,
    r.generated_by_model = $generated_by_model,
    r.model_profile = $model_profile,
    r.prompt_version = $prompt_version,
    r.agent_version = $agent_version,
    r.created_at = $occurred_at,
    r.updated_at = $occurred_at,
    r.schema_version = 1
CREATE (event:MemoryEvent {{
    event_id: $event_id,
    organization_id: $organization_id,
    actor_id: $actor_id,
    action: $event_action,
    target_id: $relation_id,
    previous_state: $previous_state,
    new_state: $new_state,
    reason: $event_reason,
    source_episode_id: $source_episode_id,
    occurred_at: $occurred_at,
    schema_version: 1
}})
CREATE (event)-[:TARGETS]->(r)
RETURN {RELATION_COLUMNS}
"""


UPDATE_RELATION_QUERY = f"""
MATCH (r:ResearchRelation {{organization_id: $organization_id, relation_id: $relation_id}})
WHERE r.status = $previous_state
  AND r.source_node_id = $source_node_id
  AND r.target_node_id = $target_node_id
  AND r.source_namespace = $source_namespace
  AND r.target_namespace = $target_namespace
  AND r.edge_type = $edge_type
  AND r.truth_class = $truth_class
  AND r.source_episode_id = $source_episode_id
  AND r.evidence_version_ids = $evidence_version_ids
SET r.source_node_id = $source_node_id,
    r.target_node_id = $target_node_id,
    r.edge_type = $edge_type,
    r.truth_class = $truth_class,
    r.source_episode_id = $source_episode_id,
    r.evidence_version_ids = $evidence_version_ids,
    r.source_namespace = $source_namespace,
    r.target_namespace = $target_namespace,
    r.confidence = $confidence,
    r.status = $status,
    r.approved_by = $approved_by,
    r.approved_at = $approved_at,
    r.approval_note = $approval_note,
    r.approval_organization_id = $approval_organization_id,
    r.generated_by_model = $generated_by_model,
    r.model_profile = $model_profile,
    r.prompt_version = $prompt_version,
    r.agent_version = $agent_version,
    r.updated_at = $occurred_at
CREATE (event:MemoryEvent {{
    event_id: $event_id,
    organization_id: $organization_id,
    actor_id: $actor_id,
    action: $event_action,
    target_id: $relation_id,
    previous_state: $previous_state,
    new_state: $new_state,
    reason: $event_reason,
    source_episode_id: $source_episode_id,
    occurred_at: $occurred_at,
    schema_version: 1
}})
CREATE (event)-[:TARGETS]->(r)
RETURN {RELATION_COLUMNS}
"""


class FalkorResearchGraphRepository(ResearchGraphRepository):
    """FalkorDB adapter with per-organization graph keys and optimistic state transitions."""

    def __init__(
        self, client: FalkorClient, graph_prefix: str = "legal_research_v1"
    ) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", graph_prefix):
            raise DomainError(
                "graph_prefix may only contain letters, numbers, '_' and '-'"
            )
        self.client = client
        self.graph_prefix = graph_prefix

    @classmethod
    def connect(
        cls, graph_prefix: str = "legal_research_v1", **client_options: Any
    ) -> FalkorResearchGraphRepository:
        """Construct the adapter with the optional official ``FalkorDB`` package."""
        try:
            from falkordb import FalkorDB
        except ImportError as exc:
            raise RuntimeError(
                "install the FalkorDB extra: pip install 'legal-agent-core[falkordb]'"
            ) from exc
        return cls(FalkorDB(**client_options), graph_prefix)

    def graph_name_for(self, organization_id: str) -> str:
        if not organization_id or not organization_id.strip():
            raise DomainError("organization_id must not be blank")
        tenant_digest = sha256(organization_id.encode("utf-8")).hexdigest()[:24]
        return f"{self.graph_prefix}_{tenant_digest}"

    def _graph(self, organization_id: str) -> FalkorGraph:
        return self.client.select_graph(self.graph_name_for(organization_id))

    @staticmethod
    def _rows(result: GraphResult) -> list[list[Any]]:
        return result.result_set

    @staticmethod
    def _iso(value: datetime | None) -> str | None:
        return None if value is None else value.isoformat()

    @staticmethod
    def _datetime(value: datetime | str) -> datetime:
        return value if isinstance(value, datetime) else datetime.fromisoformat(value)

    @staticmethod
    def _relation_params(
        relation: ProgressiveRelation, event: MemoryEvent
    ) -> dict[str, Any]:
        approval = relation.approval
        return {
            "relation_id": relation.relation_id,
            "organization_id": relation.organization_id,
            "source_node_id": relation.source_node_id,
            "target_node_id": relation.target_node_id,
            "edge_type": relation.edge_type.value,
            "truth_class": relation.truth_class.value,
            "source_episode_id": relation.source_episode_id,
            "evidence_version_ids": list(relation.evidence_version_ids),
            "source_namespace": relation.source_namespace.value,
            "target_namespace": relation.target_namespace.value,
            "confidence": relation.confidence,
            "status": relation.status.value,
            "approved_by": None if approval is None else approval.approved_by,
            "approved_at": None
            if approval is None
            else approval.approved_at.isoformat(),
            "approval_note": None if approval is None else approval.approval_note,
            "approval_organization_id": None
            if approval is None
            else approval.organization_id,
            "generated_by_model": relation.generated_by_model,
            "model_profile": relation.model_profile,
            "prompt_version": relation.prompt_version,
            "agent_version": relation.agent_version,
            "event_id": event.event_id,
            "actor_id": event.actor_id,
            "event_action": event.action.value,
            "previous_state": None
            if event.previous_state is None
            else event.previous_state.value,
            "new_state": event.new_state.value,
            "event_reason": event.reason,
            "occurred_at": event.timestamp.isoformat(),
        }

    @classmethod
    def _decode_relation(cls, row: list[Any]) -> ProgressiveRelation:
        approval = None
        if row[12] is not None:
            approval = Approval(
                approved_by=row[12],
                approved_at=cls._datetime(row[13]),
                approval_note=row[14],
                organization_id=row[15],
                source_episode_id=row[6],
            )
        return ProgressiveRelation(
            relation_id=row[0],
            organization_id=row[1],
            source_node_id=row[2],
            target_node_id=row[3],
            edge_type=ProgressiveEdgeType(row[4]),
            truth_class=TruthClass(row[5]),
            source_episode_id=row[6],
            evidence_version_ids=tuple(row[7] or ()),
            source_namespace=GraphNamespace(row[8]),
            target_namespace=GraphNamespace(row[9]),
            confidence=row[10],
            status=TrustStatus(row[11]),
            approval=approval,
            generated_by_model=row[16],
            model_profile=row[17],
            prompt_version=row[18],
            agent_version=row[19],
        )

    @classmethod
    def _decode_event(cls, row: list[Any]) -> MemoryEvent:
        return MemoryEvent(
            event_id=row[0],
            organization_id=row[1],
            actor_id=row[2],
            action=MemoryEventType(row[3]),
            target_id=row[4],
            previous_state=None if row[5] is None else TrustStatus(row[5]),
            new_state=TrustStatus(row[6]),
            reason=row[7],
            source_episode_id=row[8],
            timestamp=cls._datetime(row[9]),
        )

    @staticmethod
    def _validate_event(relation: ProgressiveRelation, event: MemoryEvent) -> None:
        if (
            relation.organization_id != event.organization_id
            or relation.relation_id != event.target_id
            or relation.source_episode_id != event.source_episode_id
            or relation.status != event.new_state
        ):
            raise ConflictError(
                "event and relation identity, tenancy, provenance, and state must match"
            )

    def get_relation(
        self, organization_id: str, relation_id: str
    ) -> ProgressiveRelation:
        result = self._graph(organization_id).ro_query(
            f"""
            MATCH (r:ResearchRelation {{organization_id: $organization_id, relation_id: $relation_id}})
            RETURN {RELATION_COLUMNS}
            """,
            {"organization_id": organization_id, "relation_id": relation_id},
        )
        rows = self._rows(result)
        if not rows:
            raise NotFoundError(f"relation {relation_id!r} not found in organization")
        return self._decode_relation(rows[0])

    def save_relation(self, relation: ProgressiveRelation, event: MemoryEvent) -> None:
        self._validate_event(relation, event)
        query = (
            CREATE_RELATION_QUERY
            if event.previous_state is None
            else UPDATE_RELATION_QUERY
        )
        result = self._graph(relation.organization_id).query(
            query, self._relation_params(relation, event)
        )
        if not self._rows(result):
            if event.previous_state is None:
                raise ConflictError(f"relation {relation.relation_id!r} already exists")
            raise ConflictError(
                f"relation {relation.relation_id!r} is not in expected {event.previous_state.value} state"
            )

    def active_relations(self, organization_id: str) -> tuple[ProgressiveRelation, ...]:
        result = self._graph(organization_id).ro_query(
            f"""
            MATCH (r:ResearchRelation {{organization_id: $organization_id, status: $status}})
            RETURN {RELATION_COLUMNS}
            ORDER BY r.relation_id
            """,
            {
                "organization_id": organization_id,
                "status": TrustStatus.EXPERT_APPROVED.value,
            },
        )
        return tuple(self._decode_relation(row) for row in self._rows(result))

    def list_relations(
        self,
        organization_id: str,
        statuses: frozenset[TrustStatus] | None = None,
    ) -> tuple[ProgressiveRelation, ...]:
        result = self._graph(organization_id).ro_query(
            f"""
            MATCH (r:ResearchRelation {{organization_id: $organization_id}})
            WHERE $statuses IS NULL OR r.status IN $statuses
            RETURN {RELATION_COLUMNS}
            ORDER BY r.relation_id
            """,
            {
                "organization_id": organization_id,
                "statuses": (
                    None
                    if statuses is None
                    else sorted(status.value for status in statuses)
                ),
            },
        )
        return tuple(self._decode_relation(row) for row in self._rows(result))

    def relations_using_version(
        self,
        organization_id: str,
        provision_version_id: str,
        status: TrustStatus | None = None,
    ) -> tuple[ProgressiveRelation, ...]:
        result = self._graph(organization_id).ro_query(
            f"""
            MATCH (r:ResearchRelation {{organization_id: $organization_id}})
            WHERE $provision_version_id IN r.evidence_version_ids
              AND ($status IS NULL OR r.status = $status)
            RETURN {RELATION_COLUMNS}
            ORDER BY r.relation_id
            """,
            {
                "organization_id": organization_id,
                "provision_version_id": provision_version_id,
                "status": None if status is None else status.value,
            },
        )
        return tuple(self._decode_relation(row) for row in self._rows(result))

    def events(self, organization_id: str) -> tuple[MemoryEvent, ...]:
        result = self._graph(organization_id).ro_query(
            f"""
            MATCH (e:MemoryEvent {{organization_id: $organization_id}})
            RETURN {EVENT_COLUMNS}
            ORDER BY e.occurred_at, e.event_id
            """,
            {"organization_id": organization_id},
        )
        return tuple(self._decode_event(row) for row in self._rows(result))

    def ensure_indexes(self, organization_id: str) -> tuple[str, ...]:
        """Create recommended range indexes missing from an organization's graph."""
        graph = self._graph(organization_id)
        graph.query(
            "MERGE (schema:GraphSchema {name: $name, version: $version}) RETURN schema.version",
            {"name": "progressive_research", "version": 1},
        )
        existing_result = graph.ro_query(
            "CALL db.indexes() YIELD label, properties RETURN label, properties"
        )
        existing = {
            (str(row[0]), tuple(str(property_name) for property_name in row[1]))
            for row in self._rows(existing_result)
        }
        desired = (
            ("ResearchRelation", "relation_id"),
            ("ResearchRelation", "status"),
            ("ResearchRelation", "evidence_version_ids"),
            ("GraphNode", "node_id"),
            ("MemoryEvent", "event_id"),
            ("MemoryEvent", "occurred_at"),
        )
        created: list[str] = []
        for label, property_name in desired:
            if (label, (property_name,)) in existing:
                continue
            statement = f"CREATE INDEX FOR (node:{label}) ON (node.{property_name})"
            graph.query(statement)
            created.append(statement)
        return tuple(created)
