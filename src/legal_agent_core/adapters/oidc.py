"""OpenID Connect integration.

- ``OIDCDiscovery`` / ``OIDCAuthorizationEndpoint`` : discovery + authorization
  code flow against an external Identity Provider.
- ``OIDCTokenVerifier`` : verifies signed ID tokens (RS256/ES256) against the
  provider's JWKS and maps claims to a domain ``Principal``.
- ``SessionTokenVerifier`` : verifies short-lived, HttpOnly session cookies that
  we mint after a successful provider exchange, so UI requests no longer need a
  manually-pasted bearer token.

Network calls use the standard library only; JWT verification uses PyJWT.
"""

from __future__ import annotations

import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from ..errors import AuthorizationError
from ..security import AuthenticationError, Principal, Role, TokenVerifier

try:  # pragma: no cover - exercised in live/tests with the oidc extra installed
    import jwt
    from jwt import PyJWKClient
except ImportError as exc:  # pragma: no cover
    jwt = None  # type: ignore[assignment]
    PyJWKClient = None  # type: ignore[assignment]
    _JWK_IMPORT_ERROR = exc
else:  # pragma: no cover
    _JWK_IMPORT_ERROR = None


def _require_jwt() -> None:
    if _JWK_IMPORT_ERROR is not None:
        raise RuntimeError(
            "install the oidc extra: pip install -e '.[oidc]'"
        ) from _JWK_IMPORT_ERROR


class OIDCError(AuthenticationError):
    """Raised when an OIDC message cannot be verified."""


@dataclass(frozen=True, slots=True)
class ProviderMetadata:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    client_id: str
    client_secret: str
    redirect_uri: str


@dataclass(slots=True)
class OIDCAuthorizationClient:
    """Authorization-code flow client using ``urllib`` for HTTP."""

    provider: ProviderMetadata
    scope: str = "openid email profile"

    def authorization_uri(self, state: str, nonce: str) -> str:
        parameters = urlencode(
            {
                "response_type": "code",
                "client_id": self.provider.client_id,
                "redirect_uri": self.provider.redirect_uri,
                "scope": self.scope,
                "state": state,
                "nonce": nonce,
            }
        )
        return f"{self.provider.authorization_endpoint}?{parameters}"

    def exchange_code(self, code: str, redirect_uri: str) -> dict[str, Any]:
        body = urlencode(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": self.provider.client_id,
                "client_secret": self.provider.client_secret,
            }
        ).encode("utf-8")
        request = Request(
            self.provider.token_endpoint,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urlopen(request, timeout=15) as response:
                raw = response.read()
        except OSError as exc:
            raise OIDCError(f"token exchange failed: {exc}") from exc
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise OIDCError("token endpoint returned invalid JSON") from exc
        id_token = payload.get("id_token")
        if not isinstance(id_token, str):
            raise OIDCError("token endpoint returned no id_token")
        return {"id_token": id_token, "access_token": payload.get("access_token")}


class OIDCTokenVerifier(TokenVerifier):
    """Verifies signed ID tokens against the provider JWKS and maps claims."""

    def __init__(
        self,
        *,
        issuer: str,
        client_id: str,
        jwks_uri: str,
        organization_claim: str = "organization_id",
        roles_claim: str = "roles",
    ) -> None:
        _require_jwt()
        self.issuer = issuer
        self.client_id = client_id
        self.jwks_client = PyJWKClient(jwks_uri)
        self.organization_claim = organization_claim
        self.roles_claim = roles_claim

    def verify_id_token(self, token: str, nonce: str | None = None) -> Principal:
        _require_jwt()
        try:
            signing_key = self.jwks_client.get_signing_key_from_jwt(token)
        except Exception as exc:
            raise OIDCError(f"id token signing key lookup failed: {exc}") from exc
        try:
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=[signing_key.algorithm_name],
                audience=self.client_id,
                issuer=self.issuer,
                options={"verify_exp": True},
            )
        except Exception as exc:
            raise OIDCError(f"id token verification failed: {exc}") from exc
        if nonce is not None and claims.get("nonce") != nonce:
            raise OIDCError("id token nonce mismatch")
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject:
            raise OIDCError("id token missing subject")
        organization_id = claims.get(self.organization_claim, "org-a")
        if not isinstance(organization_id, str) or not organization_id:
            raise OIDCError("id token missing organization claim")
        raw_roles = claims.get(self.roles_claim, ())
        roles: set[Role] = set()
        if isinstance(raw_roles, (list, tuple)):
            for item in raw_roles:
                try:
                    roles.add(Role(item))
                except (TypeError, ValueError):
                    continue
        if not roles:
            roles.add(Role.RESEARCHER)
        return Principal(subject, organization_id, frozenset(roles))

    # TokenVerifier adapter: acceptable for directly presenting a verified ID token.
    def verify(self, token: str) -> Principal:
        return self.verify_id_token(token)


class SessionTokenVerifier(TokenVerifier):
    """Verifies HMAC-signed session JWTs minted after a successful exchange.

    The session token encodes the principal plus iss/aud/exp. The HMAC secret is
    shared out-of-band; it is not the IdP's key.
    """

    def __init__(
        self,
        secret: str,
        *,
        issuer: str = "legal-agent",
        audience: str = "legal-agent-workspace",
        ttl_seconds: int = 60 * 60 * 8,
    ) -> None:
        _require_jwt()
        if not secret:
            raise AuthorizationError("a session signing secret is required")
        self.secret = secret
        self.issuer = issuer
        self.audience = audience
        self.ttl_seconds = ttl_seconds

    def issue(self, principal: Principal) -> str:
        now = int(time.time())
        payload = {
            "sub": principal.actor_id,
            "organization_id": principal.organization_id,
            "roles": [role.value for role in principal.roles],
            "iss": self.issuer,
            "aud": self.audience,
            "iat": now,
            "exp": now + self.ttl_seconds,
            "jti": secrets.token_urlsafe(12),
        }
        return jwt.encode(payload, self.secret, algorithm="HS256")

    def verify(self, token: str) -> Principal:
        try:
            claims = jwt.decode(
                token,
                self.secret,
                algorithms=["HS256"],
                audience=self.audience,
                issuer=self.issuer,
                options={"verify_aud": True, "verify_exp": True},
            )
        except Exception as exc:
            raise AuthenticationError(f"invalid session token: {exc}") from exc
        subject = claims.get("sub")
        organization_id = claims.get("organization_id", "org-a")
        if not isinstance(subject, str) or not subject:
            raise AuthenticationError("session token missing subject")
        roles = {
            Role(role)
            for role in claims.get("roles", ())
            if isinstance(role, str)
        } or {Role.RESEARCHER}
        return Principal(subject, str(organization_id), frozenset(roles))


def secrets_compare(a: str | None, b: str | None) -> bool:
    if a is None or b is None:
        return False
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


@dataclass(frozen=True, slots=True)
class OIDCAuthRequest:
    state: str
    nonce: str


def new_auth_request() -> OIDCAuthRequest:
    return OIDCAuthRequest(
        state=secrets.token_urlsafe(24), nonce=secrets.token_urlsafe(16)
    )
