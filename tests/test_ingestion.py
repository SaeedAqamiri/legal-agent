import unittest
from dataclasses import replace
from datetime import UTC, date, datetime

from legal_agent_core.canonical import (
    BoundingBox,
    DocumentStatus,
    InstrumentType,
    ProvisionType,
    ResolutionStatus,
)
from legal_agent_core.in_memory import InMemoryCanonicalRepository
from legal_agent_core.ingestion import (
    CanonicalIngestionPipeline,
    DocumentVersionInput,
    InstrumentInput,
    ParsedDocument,
    ProvisionInput,
    SourceInput,
    normalize_legal_text,
)

INGESTED_AT = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)


def parsed_document(*, checksum: str = "sha256:abc", version_number: int = 1) -> ParsedDocument:
    return ParsedDocument(
        source=SourceInput(
            filename=f"law-v{version_number}.pdf",
            media_type="application/pdf",
            checksum=checksum,
            file_size=2048,
            ingested_at=INGESTED_AT,
            ingested_by="ingestion-test",
            source_uri=f"file:///law-v{version_number}.pdf",
        ),
        instrument=InstrumentInput(
            title="قانون نمونه",
            canonical_title="قانون نمونه",
            instrument_type=InstrumentType.STATUTE,
            jurisdiction="IR",
            external_identifier="IR-EXAMPLE-LAW",
            aliases=("قانون نمونه",),
        ),
        version=DocumentVersionInput(
            status=DocumentStatus.EFFECTIVE,
            version_number=version_number,
            effective_from=date(2025 + version_number, 1, 1),
        ),
        provisions=(
            ProvisionInput(
                provision_type=ProvisionType.CHAPTER,
                label="فصل اول",
                title="احکام عمومی",
                text="احکام عمومی",
                page_number=1,
                children=(
                    ProvisionInput(
                        provision_type=ProvisionType.ARTICLE,
                        number="۴",
                        label="ماده ۴",
                        text="مرجع باید درخواست را بررسی کند.",
                        raw_text="مرجع بايد درخواست را بررسی کند.",
                        page_number=2,
                        bbox=BoundingBox(0.1, 0.2, 0.9, 0.4),
                    ),
                    ProvisionInput(
                        provision_type=ProvisionType.ARTICLE,
                        number="۵",
                        label="ماده ۵",
                        text="طبق ماده ۴ این قانون، تصمیم باید مستند باشد.",
                        page_number=3,
                    ),
                ),
            ),
        ),
    )


class IngestionPipelineTests(unittest.TestCase):
    def test_normalization_keeps_source_text_separate(self) -> None:
        raw = "  ماده\t۴  با حروف عربي ي و ك  "

        self.assertEqual(normalize_legal_text(raw), "ماده 4 با حروف عربی ی و ک")

    def test_builds_hierarchy_versions_spans_and_resolved_reference(self) -> None:
        repository = InMemoryCanonicalRepository()
        result = CanonicalIngestionPipeline(repository).ingest(parsed_document())

        self.assertEqual(len(result.provision_ids), 3)
        self.assertEqual(len(repository.provision_versions), 3)
        self.assertEqual(len(repository.source_spans), 3)
        articles = {item.number: item for item in repository.provisions.values() if item.number}
        self.assertEqual(articles["4"].depth, 1)
        self.assertEqual(articles["4"].parent_provision_id, next(
            item.provision_id for item in repository.provisions.values() if item.provision_type == ProvisionType.CHAPTER
        ))

        self.assertEqual(len(result.explicit_references), 1)
        reference = result.explicit_references[0]
        self.assertEqual(reference.resolution_status, ResolutionStatus.RESOLVED)
        self.assertEqual(reference.resolved_target_provision_id, articles["4"].provision_id)
        self.assertEqual(len(result.canonical_edges), 1)
        self.assertEqual(result.canonical_edges[0].target_node_id, articles["4"].provision_id)
        article_four_version = next(
            version for version in repository.provision_versions.values()
            if version.provision_id == articles["4"].provision_id
        )
        article_four_span = next(
            span for span in repository.source_spans.values()
            if span.provision_version_id == article_four_version.provision_version_id
        )
        self.assertIn("باید", article_four_version.normalized_text)
        self.assertIn("بايد", article_four_span.raw_text)

    def test_ingesting_same_parser_output_twice_is_idempotent(self) -> None:
        repository = InMemoryCanonicalRepository()
        pipeline = CanonicalIngestionPipeline(repository)
        parsed = parsed_document()

        first = pipeline.ingest(parsed)
        second = pipeline.ingest(parsed)

        self.assertEqual(first, second)
        self.assertEqual(len(repository.source_documents), 1)
        self.assertEqual(len(repository.instruments), 1)
        self.assertEqual(len(repository.document_versions), 1)
        self.assertEqual(len(repository.provisions), 3)
        self.assertEqual(len(repository.explicit_references), 1)
        self.assertEqual(len(repository.edges), 1)

    def test_unscoped_article_reference_is_not_guessed(self) -> None:
        repository = InMemoryCanonicalRepository()
        parsed = parsed_document()
        chapter = parsed.provisions[0]
        article_four, article_five = chapter.children
        unresolved_five = replace(article_five, text="مطابق ماده ۴ اقدام شود.")
        parsed = replace(parsed, provisions=(replace(chapter, children=(article_four, unresolved_five)),))

        result = CanonicalIngestionPipeline(repository).ingest(parsed)

        self.assertEqual(result.explicit_references[0].resolution_status, ResolutionStatus.UNRESOLVED)
        self.assertIsNone(result.explicit_references[0].resolved_target_provision_id)
        self.assertEqual(result.canonical_edges, ())

    def test_stable_provision_identity_survives_new_document_version(self) -> None:
        repository = InMemoryCanonicalRepository()
        pipeline = CanonicalIngestionPipeline(repository)

        first = pipeline.ingest(parsed_document())
        second = pipeline.ingest(parsed_document(checksum="sha256:def", version_number=2))

        self.assertEqual(first.instrument_id, second.instrument_id)
        self.assertEqual(first.provision_ids, second.provision_ids)
        self.assertNotEqual(first.document_version_id, second.document_version_id)
        self.assertTrue(set(first.provision_version_ids).isdisjoint(second.provision_version_ids))
        self.assertEqual(len(repository.provisions), 3)
        self.assertEqual(len(repository.provision_versions), 6)


if __name__ == "__main__":
    unittest.main()
