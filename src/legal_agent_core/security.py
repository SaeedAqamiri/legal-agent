from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from .canonical import require_text
from .errors import AuthorizationError


class AuthenticationError(AuthorizationError):
    """Raised when a bearer credential cannot be authenticated."""


class Role(StrEnum):
    RESEARCHER = "researcher"
    LEGAL_EXPERT = "legal_expert"
    KNOWLEDGE_STEWARD = "knowledge_steward"
    EVALUATOR = "evaluator"
    INGESTION_OPERATOR = "ingestion_operator"
    ADMIN = "admin"


class Permission(StrEnum):
    RUN_RESEARCH = "run_research"
    READ_RESEARCH = "read_research"
    READ_LIBRARY = "read_library"
    REVIEW_KNOWLEDGE = "review_knowledge"
    RUN_EVALUATION = "run_evaluation"
    READ_METRICS = "read_metrics"
    MANAGE_INGESTION = "manage_ingestion"


_ROLE_PERMISSIONS: Mapping[Role, frozenset[Permission]] = {
    Role.RESEARCHER: frozenset(
        {Permission.RUN_RESEARCH, Permission.READ_RESEARCH, Permission.READ_LIBRARY}
    ),
    Role.LEGAL_EXPERT: frozenset(
        {
            Permission.RUN_RESEARCH,
            Permission.READ_RESEARCH,
            Permission.READ_LIBRARY,
            Permission.REVIEW_KNOWLEDGE,
        }
    ),
    Role.KNOWLEDGE_STEWARD: frozenset({Permission.REVIEW_KNOWLEDGE}),
    Role.EVALUATOR: frozenset(
        {
            Permission.RUN_RESEARCH,
            Permission.READ_RESEARCH,
            Permission.READ_LIBRARY,
            Permission.RUN_EVALUATION,
            Permission.READ_METRICS,
        }
    ),
    Role.INGESTION_OPERATOR: frozenset({Permission.MANAGE_INGESTION}),
    Role.ADMIN: frozenset(Permission),
}


@dataclass(frozen=True, slots=True)
class Principal:
    actor_id: str
    organization_id: str
    roles: frozenset[Role]

    def __post_init__(self) -> None:
        require_text(self.actor_id, "actor_id")
        require_text(self.organization_id, "organization_id")
        if not self.roles:
            raise AuthenticationError(
                "authenticated principal must have at least one role"
            )


class TokenVerifier(Protocol):
    def verify(self, token: str) -> Principal: ...


class StaticTokenVerifier:
    """Digest-backed token verifier for tests and controlled local deployments."""

    def __init__(self, tokens: Mapping[str, Principal]) -> None:
        self._principals = {
            self._digest(token): principal for token, principal in tokens.items()
        }

    def verify(self, token: str) -> Principal:
        principal = self._principals.get(self._digest(token))
        if principal is None:
            raise AuthenticationError("invalid bearer token")
        return principal

    @staticmethod
    def _digest(token: str) -> bytes:
        return hashlib.sha256(token.encode("utf-8")).digest()


class AuthorizationService:
    def require(
        self,
        principal: Principal,
        permission: Permission,
        organization_id: str,
    ) -> None:
        if principal.organization_id != organization_id:
            raise AuthorizationError("cross-organization access is forbidden")
        permissions = frozenset().union(
            *(_ROLE_PERMISSIONS[role] for role in principal.roles)
        )
        if permission not in permissions:
            raise AuthorizationError(f"permission {permission.value!r} is required")
