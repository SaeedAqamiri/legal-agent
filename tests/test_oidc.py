import time
import unittest

from legal_agent_core.adapters.oidc import (
    OIDCError,
    OIDCTokenVerifier,
    SessionTokenVerifier,
)
from legal_agent_core.security import AuthenticationError, Principal, Role


class _FakeJWK:
    def __init__(self, key, algorithm_name):
        self.key = key
        self.algorithm_name = algorithm_name


class _FakeJWKClient:
    def __init__(self, public_key):
        self._public = public_key

    def get_signing_key_from_jwt(self, token):
        return _FakeJWK(self._public, "RS256")


def _sign_rs256(private_key, payload):
    import jwt

    return jwt.encode(payload, private_key, algorithm="RS256")


class SessionTokenVerifierTests(unittest.TestCase):
    def test_issue_and_verify_round_trip(self) -> None:
        verifier = SessionTokenVerifier("very-secret")
        principal = Principal(
            "user-1", "org-a", frozenset({Role.RESEARCHER, Role.LEGAL_EXPERT})
        )
        token = verifier.issue(principal)
        decoded = verifier.verify(token)
        self.assertEqual(decoded.actor_id, principal.actor_id)
        self.assertEqual(decoded.organization_id, principal.organization_id)
        self.assertEqual(decoded.roles, principal.roles)

    def test_tampered_token_is_rejected(self) -> None:
        verifier = SessionTokenVerifier("very-secret")
        token = verifier.issue(Principal("user-1", "org-a", frozenset({Role.RESEARCHER})))
        forged = token[:-2] + ("ab" if token[-2:] != "ab" else "cd")
        with self.assertRaises(AuthenticationError):
            verifier.verify(forged)

    def test_expired_token_is_rejected(self) -> None:
        import jwt

        verifier = SessionTokenVerifier("very-secret", ttl_seconds=-1)
        token = jwt.encode(
            {
                "sub": "user-1",
                "organization_id": "org-a",
                "roles": ["researcher"],
                "iss": verifier.issuer,
                "aud": verifier.audience,
                "iat": int(time.time()),
                "exp": 0,
            },
            verifier.secret,
            algorithm="HS256",
        )
        with self.assertRaises(AuthenticationError):
            verifier.verify(token)


class OIDCTokenVerifierTests(unittest.TestCase):
    def test_verifies_signed_id_token_and_maps_claims(self) -> None:
        from cryptography.hazmat.primitives.asymmetric import rsa

        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public_key = private_key.public_key()

        now = int(time.time())
        id_token = _sign_rs256(
            private_key,
            {
                "iss": "https://idp.example",
                "sub": "user-42",
                "aud": "legal-app",
                "exp": now + 300,
                "iat": now,
                "organization_id": "org-b",
                "roles": ["legal_expert", "evaluator"],
            },
        )
        verifier = OIDCTokenVerifier(
            issuer="https://idp.example",
            client_id="legal-app",
            jwks_uri="https://idp.example/jwks",
        )
        verifier.jwks_client = _FakeJWKClient(public_key)
        principal = verifier.verify_id_token(id_token)
        self.assertEqual(principal.actor_id, "user-42")
        self.assertEqual(principal.organization_id, "org-b")
        self.assertEqual(
            principal.roles, frozenset({Role.LEGAL_EXPERT, Role.EVALUATOR})
        )

    def test_rejects_wrong_issuer(self) -> None:
        from cryptography.hazmat.primitives.asymmetric import rsa

        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = int(time.time())
        verifier = OIDCTokenVerifier(
            issuer="https://idp.example",
            client_id="legal-app",
            jwks_uri="https://idp.example/jwks",
        )
        verifier.jwks_client = _FakeJWKClient(private_key.public_key())
        bad = _sign_rs256(
            private_key,
            {
                "iss": "https://evil.example",
                "sub": "user-42",
                "aud": "legal-app",
                "exp": now + 300,
                "iat": now,
            },
        )
        with self.assertRaises(OIDCError):
            verifier.verify_id_token(bad)


if __name__ == "__main__":
    unittest.main()
