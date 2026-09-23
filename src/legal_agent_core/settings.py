"""Environment-driven runtime configuration.

Reads configuration strictly from environment variables so the same code runs
in tests, local development, and production without code changes. Every value
has a safe default; no secret is logged or printed by this module.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _env_bool(name: str, default: bool = False) -> bool:
    value = _env(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class PostgresSettings:
    dsn: str
    migrate: bool = True

    @property
    def enabled(self) -> bool:
        return bool(self.dsn)


@dataclass(frozen=True, slots=True)
class FalkorSettings:
    url: str | None
    graph_prefix: str = "legal_graph"

    @property
    def enabled(self) -> bool:
        return bool(self.url)


@dataclass(frozen=True, slots=True)
class OIDCSettings:
    issuer: str | None
    client_id: str | None
    client_secret: str | None
    redirect_uri: str | None
    authorization_endpoint: str | None
    token_endpoint: str | None
    jwks_uri: str | None
    #: Optional static bearer tokens (digest) mapped to principal JSON for local/test mode.
    static_tokens: dict[str, dict[str, object]] | None = None

    @property
    def enabled(self) -> bool:
        return bool(
            self.issuer
            and self.client_id
            and self.client_secret
            and self.authorization_endpoint
            and self.token_endpoint
            and self.jwks_uri
        )


@dataclass(frozen=True, slots=True)
class Settings:
    host: str
    port: int
    postgres: PostgresSettings
    falkor: FalkorSettings
    oidc: OIDCSettings
    history_path: str | None
    default_organization_id: str
    log_level: str

    @staticmethod
    def from_env() -> Settings:
        static_tokens: dict[str, dict[str, object]] | None = None
        raw_tokens = _env("LEGAL_AGENT_STATIC_TOKENS")
        if raw_tokens:
            import json

            try:
                parsed = json.loads(raw_tokens)
                if isinstance(parsed, dict):
                    static_tokens = parsed
            except json.JSONDecodeError:
                static_tokens = None
        return Settings(
            host=_env("LEGAL_AGENT_HOST", "127.0.0.1") or "127.0.0.1",
            port=int(_env("LEGAL_AGENT_PORT", "8000") or "8000"),
            postgres=PostgresSettings(
                dsn=_env("LEGAL_AGENT_POSTGRES_DSN", "") or "",
                migrate=_env_bool("LEGAL_AGENT_POSTGRES_MIGRATE", True),
            ),
            falkor=FalkorSettings(
                url=_env("FALKORDB_URL"),
                graph_prefix=_env("LEGAL_AGENT_FALKOR_PREFIX", "legal_graph")
                or "legal_graph",
            ),
            oidc=OIDCSettings(
                issuer=_env("LEGAL_AGENT_OIDC_ISSUER"),
                client_id=_env("LEGAL_AGENT_OIDC_CLIENT_ID"),
                client_secret=_env("LEGAL_AGENT_OIDC_CLIENT_SECRET"),
                redirect_uri=_env("LEGAL_AGENT_OIDC_REDIRECT_URI"),
                authorization_endpoint=_env("LEGAL_AGENT_OIDC_AUTHORIZATION_ENDPOINT"),
                token_endpoint=_env("LEGAL_AGENT_OIDC_TOKEN_ENDPOINT"),
                jwks_uri=_env("LEGAL_AGENT_OIDC_JWKS_URI"),
                static_tokens=static_tokens,
            ),
            history_path=_env("LEGAL_AGENT_HISTORY_PATH"),
            default_organization_id=_env("LEGAL_AGENT_ORGANIZATION", "org-a")
            or "org-a",
            log_level=_env("LEGAL_AGENT_LOG_LEVEL", "info") or "info",
        )


def build_token_verifier(settings: Settings) -> Any:
    """Return the appropriate ``TokenVerifier`` for the settings."""
    from .security import StaticTokenVerifier

    if settings.oidc.enabled:
        from .adapters.oidc import OIDCTokenVerifier

        return OIDCTokenVerifier(
            issuer=settings.oidc.issuer,
            client_id=settings.oidc.client_id,
            jwks_uri=settings.oidc.jwks_uri,
        )
    tokens: dict[str, object] = {}
    if settings.oidc.static_tokens:
        from .security import Principal, Role

        for token, principal_data in settings.oidc.static_tokens.items():
            roles = frozenset(
                Role(role) if isinstance(role, str) else role
                for role in principal_data.get("roles", ())
            )
            tokens[token] = Principal(
                str(principal_data.get("actor_id", token)),
                str(principal_data.get("organization_id", "org-a")),
                roles,
            )
    return StaticTokenVerifier(tokens)
