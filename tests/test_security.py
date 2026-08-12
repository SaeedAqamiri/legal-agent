import unittest

from legal_agent_core.errors import AuthorizationError
from legal_agent_core.security import (
    AuthenticationError,
    AuthorizationService,
    Permission,
    Principal,
    Role,
    StaticTokenVerifier,
)


class SecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.principal = Principal("user-1", "org-a", frozenset({Role.RESEARCHER}))
        self.authorization = AuthorizationService()

    def test_static_verifier_authenticates_without_retaining_plain_token(self) -> None:
        verifier = StaticTokenVerifier({"secret-token": self.principal})

        self.assertEqual(verifier.verify("secret-token"), self.principal)
        self.assertNotIn("secret-token", repr(verifier.__dict__))

    def test_invalid_token_is_unauthenticated(self) -> None:
        verifier = StaticTokenVerifier({"secret-token": self.principal})

        with self.assertRaises(AuthenticationError):
            verifier.verify("wrong-token")

    def test_role_permission_and_tenant_are_both_enforced(self) -> None:
        self.authorization.require(self.principal, Permission.RUN_RESEARCH, "org-a")

        with self.assertRaises(AuthorizationError):
            self.authorization.require(
                self.principal, Permission.REVIEW_KNOWLEDGE, "org-a"
            )
        with self.assertRaises(AuthorizationError):
            self.authorization.require(self.principal, Permission.RUN_RESEARCH, "org-b")

    def test_admin_cannot_cross_tenant_boundary(self) -> None:
        admin = Principal("admin-1", "org-a", frozenset({Role.ADMIN}))

        with self.assertRaises(AuthorizationError):
            self.authorization.require(admin, Permission.READ_METRICS, "org-b")


if __name__ == "__main__":
    unittest.main()
