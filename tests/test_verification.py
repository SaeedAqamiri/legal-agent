import unittest
from datetime import date

from legal_agent_core.canonical import (
    CreationMethod,
    DocumentStatus,
    DocumentVersion,
    ExplicitReference,
    InstrumentType,
    LegalInstrument,
    Provision,
    ProvisionType,
    ProvisionVersion,
    ResolutionStatus,
    SourceDocument,
    SourceSpan,
)
from legal_agent_core.in_memory import InMemoryCanonicalRepository
from legal_agent_core.research import Evidence, EvidenceStance
from legal_agent_core.verification import (
    AnswerClaim,
    ClaimImportance,
    DraftAnswer,
    EvidenceDisposition,
    EvidenceLedger,
    EvidenceVerifier,
    VerificationCode,
)

APPLICABLE_TIME = date(2025, 1, 1)


def canonical_repository(*, with_reference: bool = False) -> InMemoryCanonicalRepository:
    repository = InMemoryCanonicalRepository()
    repository.add_source_document(
        SourceDocument("src-1", None, "law.pdf", "application/pdf", "sha256:test", 100, "test", "file:///law.pdf")
    )
    repository.add_instrument(
        LegalInstrument("inst-1", "قانون نمونه", "قانون نمونه", InstrumentType.STATUTE, "IR")
    )
    repository.add_document_version(
        DocumentVersion(
            "doc-old",
            "inst-1",
            "src-1",
            DocumentStatus.SUPERSEDED,
            effective_from=date(2020, 1, 1),
            effective_to=date(2022, 12, 31),
        )
    )
    repository.add_document_version(
        DocumentVersion(
            "doc-current",
            "inst-1",
            "src-1",
            DocumentStatus.EFFECTIVE,
            effective_from=date(2023, 1, 1),
        )
    )
    repository.add_provision(Provision("prov-5", "inst-1", ProvisionType.ARTICLE, "5", "ماده ۵"))
    current_text = "طبق ماده ۴ این قانون، متن جاری ماده پنج است." if with_reference else "متن جاری ماده پنج است."
    repository.add_provision_version(
        ProvisionVersion(
            "prov-5-old",
            "prov-5",
            "doc-old",
            "متن قدیمی ماده پنج است.",
            "متن قدیمی ماده پنج است.",
            DocumentStatus.SUPERSEDED,
            "test",
            date(2020, 1, 1),
            date(2022, 12, 31),
        )
    )
    repository.add_provision_version(
        ProvisionVersion(
            "prov-5-current",
            "prov-5",
            "doc-current",
            current_text,
            current_text,
            DocumentStatus.EFFECTIVE,
            "test",
            date(2023, 1, 1),
            None,
            "prov-5-old",
        )
    )
    repository.add_source_span(SourceSpan("span-old", "src-1", "doc-old", "prov-5-old", 2, "متن قدیمی ماده پنج است."))
    repository.add_source_span(SourceSpan("span-current", "src-1", "doc-current", "prov-5-current", 3, current_text))

    if with_reference:
        repository.add_provision(Provision("prov-4", "inst-1", ProvisionType.ARTICLE, "4", "ماده ۴"))
        repository.add_provision_version(
            ProvisionVersion(
                "prov-4-current",
                "prov-4",
                "doc-current",
                "متن ماده چهار است.",
                "متن ماده چهار است.",
                DocumentStatus.EFFECTIVE,
                "test",
                date(2023, 1, 1),
            )
        )
        repository.add_source_span(SourceSpan("span-4", "src-1", "doc-current", "prov-4-current", 2, "متن ماده چهار است."))
        repository.add_explicit_reference(
            ExplicitReference(
                "ref-5-to-4",
                "prov-5-current",
                "ماده 4 این قانون",
                "span-current",
                CreationMethod.DETERMINISTIC_EXTRACTOR,
                ResolutionStatus.RESOLVED,
                "prov-4",
                1.0,
            )
        )
    return repository


def evidence_current(*, stance: EvidenceStance = EvidenceStance.SUPPORTS) -> Evidence:
    return Evidence(
        "ev-current",
        "src-1",
        "doc-current",
        "prov-5",
        "prov-5-current",
        "span-current",
        3,
        "متن جاری ماده پنج",
        "keyword",
        "supports the main claim",
        APPLICABLE_TIME,
        0.95,
        stance,
    )


def evidence_old() -> Evidence:
    return Evidence(
        "ev-old",
        "src-1",
        "doc-old",
        "prov-5",
        "prov-5-old",
        "span-old",
        2,
        "متن قدیمی ماده پنج",
        "keyword",
        "historical match",
        APPLICABLE_TIME,
        0.9,
    )


def evidence_target() -> Evidence:
    return Evidence(
        "ev-target",
        "src-1",
        "doc-current",
        "prov-4",
        "prov-4-current",
        "span-4",
        2,
        "متن ماده چهار",
        "graph_reference",
        "explicitly referenced source",
        APPLICABLE_TIME,
        1.0,
    )


def draft(*evidence_ids: str) -> DraftAnswer:
    return DraftAnswer(
        "answer-1",
        "پاسخ آزمایشی",
        (AnswerClaim("claim-1", "حکم ماده پنج", tuple(evidence_ids), ClaimImportance.CRITICAL),),
    )


class EvidenceVerifierTests(unittest.TestCase):
    def test_accepts_exact_canonical_temporal_evidence(self) -> None:
        ledger = EvidenceLedger()
        ledger.add(evidence_current())

        report = EvidenceVerifier(canonical_repository()).verify(draft("ev-current"), ledger, APPLICABLE_TIME)

        self.assertTrue(report.accepted)
        self.assertEqual(report.verified_evidence_ids, ("ev-current",))
        self.assertEqual(ledger.get("ev-current").disposition, EvidenceDisposition.ACCEPTED)

    def test_rejects_historical_version_for_current_question(self) -> None:
        ledger = EvidenceLedger()
        ledger.add(evidence_old())

        report = EvidenceVerifier(canonical_repository()).verify(draft("ev-old"), ledger, APPLICABLE_TIME)

        self.assertFalse(report.accepted)
        self.assertIn(VerificationCode.TEMPORAL_MISMATCH, {issue.code for issue in report.issues})
        self.assertEqual(ledger.get("ev-old").disposition, EvidenceDisposition.REJECTED)

    def test_requires_evidence_for_important_claim(self) -> None:
        report = EvidenceVerifier(canonical_repository()).verify(draft(), EvidenceLedger(), APPLICABLE_TIME)

        self.assertFalse(report.accepted)
        self.assertEqual(report.issues[0].code, VerificationCode.MISSING_EVIDENCE)

    def test_requires_following_resolved_explicit_reference(self) -> None:
        repository = canonical_repository(with_reference=True)
        verifier = EvidenceVerifier(repository)
        ledger = EvidenceLedger()
        ledger.add(evidence_current())

        incomplete = verifier.verify(draft("ev-current"), ledger, APPLICABLE_TIME)
        self.assertIn(
            VerificationCode.UNFOLLOWED_EXPLICIT_REFERENCE,
            {issue.code for issue in incomplete.issues},
        )

        ledger.add(evidence_target())
        complete = verifier.verify(draft("ev-current", "ev-target"), ledger, APPLICABLE_TIME)
        self.assertTrue(complete.accepted)

    def test_surfaces_unresolved_contradictory_evidence(self) -> None:
        ledger = EvidenceLedger()
        ledger.add(evidence_current(stance=EvidenceStance.CONTRADICTS))

        report = EvidenceVerifier(canonical_repository()).verify(draft("ev-current"), ledger, APPLICABLE_TIME)

        self.assertFalse(report.accepted)
        self.assertIn(VerificationCode.CONTRADICTORY_EVIDENCE, {issue.code for issue in report.issues})

    def test_rejects_excerpt_that_is_not_in_canonical_source(self) -> None:
        ledger = EvidenceLedger()
        fabricated = Evidence(
            "ev-fabricated",
            "src-1",
            "doc-current",
            "prov-5",
            "prov-5-current",
            "span-current",
            3,
            "این عبارت در منبع وجود ندارد",
            "model_memory",
            "unsupported model statement",
            APPLICABLE_TIME,
            0.99,
        )
        ledger.add(fabricated)

        report = EvidenceVerifier(canonical_repository()).verify(
            draft("ev-fabricated"), ledger, APPLICABLE_TIME
        )

        self.assertFalse(report.accepted)
        self.assertIn(VerificationCode.TEXT_NOT_IN_SOURCE, {issue.code for issue in report.issues})


if __name__ == "__main__":
    unittest.main()
