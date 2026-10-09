import unittest
from datetime import date

from legal_agent_core.canonical import (
    CanonicalEdge,
    CanonicalEdgeType,
    Citation,
    CreationMethod,
    DocumentStatus,
    DocumentVersion,
    InstrumentType,
    LegalInstrument,
    Provenance,
    Provision,
    ProvisionType,
    ProvisionVersion,
    SourceDocument,
    SourceSpan,
)
from legal_agent_core.errors import ConflictError, DomainError, NotFoundError
from legal_agent_core.in_memory import InMemoryCanonicalRepository


def seeded_repository() -> InMemoryCanonicalRepository:
    repository = InMemoryCanonicalRepository()
    repository.add_source_document(
        SourceDocument("src-1", None, "law.pdf", "application/pdf", "sha256:a", 100, "u-1", "file://law.pdf")
    )
    repository.add_instrument(
        LegalInstrument("inst-1", "قانون نمونه", "قانون نمونه", InstrumentType.STATUTE, "IR")
    )
    repository.add_document_version(
        DocumentVersion(
            "doc-v1", "inst-1", "src-1", DocumentStatus.SUPERSEDED,
            effective_from=date(2020, 1, 1), effective_to=date(2022, 12, 31),
        )
    )
    repository.add_document_version(
        DocumentVersion("doc-v2", "inst-1", "src-1", DocumentStatus.EFFECTIVE, effective_from=date(2023, 1, 1))
    )
    repository.add_provision(Provision("prov-5", "inst-1", ProvisionType.ARTICLE, "5", "ماده ۵"))
    return repository


class CanonicalModelTests(unittest.TestCase):
    def test_rejects_inverted_validity_period(self) -> None:
        with self.assertRaisesRegex(DomainError, "effective_to"):
            ProvisionVersion(
                "prov-5-v1", "prov-5", "doc-v1", "raw", "normalized", DocumentStatus.EFFECTIVE,
                "ocr", date(2023, 1, 1), date(2022, 1, 1),
            )

    def test_temporal_candidate_rule_preserves_historical_repealed_version(self) -> None:
        version = ProvisionVersion(
            "prov-5-v1",
            "prov-5",
            "doc-v1",
            "متن تاریخی",
            "متن تاریخی",
            DocumentStatus.REPEALED,
            "parser",
            date(2020, 1, 1),
            date(2022, 12, 31),
        )

        self.assertTrue(version.applies_at(date(2021, 1, 1)))
        self.assertFalse(version.applies_at(date(2023, 1, 1)))

    def test_preserves_stable_identity_and_filters_temporal_versions(self) -> None:
        repository = seeded_repository()
        repository.add_provision_version(
            ProvisionVersion(
                "prov-5-v1", "prov-5", "doc-v1", "متن قدیم", "متن قدیم", DocumentStatus.SUPERSEDED,
                "ocr", date(2020, 1, 1), date(2022, 12, 31),
            )
        )
        repository.add_provision_version(
            ProvisionVersion(
                "prov-5-v2", "prov-5", "doc-v2", "متن جدید", "متن جدید", DocumentStatus.EFFECTIVE,
                "ocr", date(2023, 1, 1), None, "prov-5-v1",
            )
        )

        self.assertEqual(repository.applicable_provision_versions("prov-5", date(2021, 6, 1))[0].text, "متن قدیم")
        self.assertEqual(repository.applicable_provision_versions("prov-5", date(2025, 6, 1))[0].text, "متن جدید")
        self.assertEqual(set(repository.provision_versions), {"prov-5-v1", "prov-5-v2"})

    def test_enforces_referential_identity(self) -> None:
        repository = seeded_repository()
        with self.assertRaises(NotFoundError):
            repository.add_provision_version(
                ProvisionVersion("bad", "missing", "doc-v1", "x", "x", DocumentStatus.EFFECTIVE, "ocr")
            )

    def test_citation_must_point_to_exact_source_version_and_span(self) -> None:
        repository = seeded_repository()
        repository.add_provision_version(
            ProvisionVersion("prov-5-v2", "prov-5", "doc-v2", "متن", "متن", DocumentStatus.EFFECTIVE, "ocr")
        )
        repository.add_source_span(SourceSpan("span-1", "src-1", "doc-v2", "prov-5-v2", 2, "متن"))
        repository.add_citation(
            Citation("cite-1", "inst-1", "doc-v2", "prov-5", "prov-5-v2", "span-1", 2, "متن")
        )

        with self.assertRaisesRegex(ConflictError, "citation identity"):
            repository.add_citation(
                Citation("cite-bad", "inst-1", "doc-v2", "prov-5", "prov-5-v2", "span-1", 3, "متن")
            )

    def test_explicit_graph_edge_requires_auditable_source_span(self) -> None:
        repository = seeded_repository()
        repository.add_provision(Provision("prov-4", "inst-1", ProvisionType.ARTICLE, "4", "ماده ۴"))
        repository.add_provision_version(
            ProvisionVersion("prov-5-v2", "prov-5", "doc-v2", "طبق ماده ۴", "طبق ماده ۴", DocumentStatus.EFFECTIVE, "ocr")
        )
        repository.add_source_span(SourceSpan("span-ref", "src-1", "doc-v2", "prov-5-v2", 2, "ماده ۴"))
        provenance = Provenance("reference-extractor", CreationMethod.DETERMINISTIC_EXTRACTOR, "span-ref")

        repository.add_edge(
            CanonicalEdge(
                "edge-1", "prov-5-v2", "prov-4", CanonicalEdgeType.EXPLICITLY_REFERENCES,
                provenance, "span-ref", 0.99,
            )
        )

        self.assertIn("edge-1", repository.edges)
        with self.assertRaisesRegex(DomainError, "source span"):
            CanonicalEdge(
                "edge-bad", "prov-5-v2", "prov-4", CanonicalEdgeType.EXPLICITLY_REFERENCES,
                provenance,
            )

    def test_judicial_and_conflict_edge_types_are_accepted(self) -> None:
        repository = seeded_repository()
        repository.add_instrument(
            LegalInstrument("inst-2", "رأی وحدت رویه نمونه", "رأی وحدت رویه نمونه", InstrumentType.JUDGMENT, "IR")
        )
        repository.add_instrument(
            LegalInstrument("inst-3", "نظریه مشورتی نمونه", "نظریه مشورتی نمونه", InstrumentType.ADVISORY_OPINION, "IR")
        )
        provenance = Provenance("cats-extractor", CreationMethod.LLM_EXTRACTOR, "src-1")

        repository.add_edge(
            CanonicalEdge("edge-interp", "inst-2", "prov-5", CanonicalEdgeType.INTERPRETS, provenance)
        )
        repository.add_edge(
            CanonicalEdge("edge-annul", "inst-2", "prov-5", CanonicalEdgeType.ANNULS, provenance)
        )
        repository.add_edge(
            CanonicalEdge("edge-conflict", "inst-3", "inst-1", CanonicalEdgeType.CONFLICTS_WITH, provenance)
        )

        self.assertIn("edge-interp", repository.edges)
        self.assertIn("edge-annul", repository.edges)
        self.assertIn("edge-conflict", repository.edges)
        self.assertEqual(repository.edges["edge-interp"].edge_type, CanonicalEdgeType.INTERPRETS)
        self.assertEqual(InstrumentType.ADVISORY_OPINION.value, "advisory_opinion")

    def test_new_edge_types_still_reject_self_loops(self) -> None:
        provenance = Provenance("cats-extractor", CreationMethod.LLM_EXTRACTOR, "src-1")
        for edge_type in (CanonicalEdgeType.INTERPRETS, CanonicalEdgeType.ANNULS, CanonicalEdgeType.CONFLICTS_WITH):
            with self.assertRaisesRegex(DomainError, "itself"):
                CanonicalEdge(f"edge-{edge_type.value}", "inst-1", "inst-1", edge_type, provenance)


if __name__ == "__main__":
    unittest.main()
