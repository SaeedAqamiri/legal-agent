from __future__ import annotations

import unittest
from datetime import date

from legal_agent_core.agentic.envelope import RefStore, ToolStatus
from legal_agent_core.agentic.structure import (
    StructureEntry,
    build_outline,
    render_outline,
)
from legal_agent_core.agentic.tools import (
    LegalResearchTools,
    ToolArgumentError,
)
from legal_agent_core.canonical import (
    DocumentStatus,
    DocumentVersion,
    InstrumentType,
    LegalInstrument,
    Provision,
    ProvisionType,
    ProvisionVersion,
    SourceDocument,
    SourceSpan,
)
from legal_agent_core.in_memory import InMemoryCanonicalRepository

APPLICABLE = date(2025, 6, 1)


class StructureModuleTests(unittest.TestCase):
    def _entry(self, **overrides) -> StructureEntry:
        values = {
            "provision_id": "p1",
            "label": "ماده ۱",
            "title": None,
            "provision_type": "article",
            "ordinal": 0,
            "parent_provision_id": None,
            "page": 1,
            "end_page": 1,
            "summary": None,
        }
        values.update(overrides)
        return StructureEntry(**values)

    def test_build_outline_nests_by_parent_links(self) -> None:
        entries = [
            self._entry(
                provision_id="part",
                label="بخش ۱",
                title="سازماندهی",
                provision_type="part",
                ordinal=0,
                page=1,
                end_page=9,
            ),
            self._entry(
                provision_id="chapter",
                label="فصل اول",
                title="کلیات",
                provision_type="chapter",
                ordinal=1,
                parent_provision_id="part",
                page=1,
                end_page=4,
            ),
            self._entry(
                provision_id="p1",
                ordinal=2,
                parent_provision_id="chapter",
                page=1,
                end_page=2,
            ),
            self._entry(
                provision_id="note",
                label="تبصره ۱",
                provision_type="note",
                ordinal=3,
                parent_provision_id="p1",
                page=2,
                end_page=2,
            ),
            self._entry(
                provision_id="p2",
                label="ماده ۲",
                ordinal=4,
                parent_provision_id="chapter",
                page=3,
                end_page=4,
            ),
        ]
        tree, depth = build_outline(entries)
        self.assertEqual(tree["node_count"], 5)
        self.assertEqual(depth, 3)
        roots = tree["roots"]
        self.assertEqual(len(roots), 1)
        part = roots[0]
        self.assertEqual(part["node_id"], "0001")
        chapter = part["children"][0]
        self.assertEqual([child["label"] for child in chapter["children"]], ["ماده ۱", "ماده ۲"])
        self.assertEqual(chapter["children"][0]["children"][0]["label"], "تبصره ۱")
        # DFS node ids stay document-ordered
        self.assertEqual(chapter["node_id"], "0002")
        self.assertEqual(chapter["children"][1]["node_id"], "0005")

    def test_render_outline_paginates_by_char_budget(self) -> None:
        entries = [
            self._entry(
                provision_id=f"p{index}",
                label=f"ماده {index}",
                ordinal=index,
                page=index + 1,
                end_page=index + 1,
            )
            for index in range(20)
        ]
        tree, _ = build_outline(entries)
        parts = render_outline(tree, char_budget=150)
        self.assertGreater(len(parts), 1)
        self.assertLessEqual(max(len(part) for part in parts), 500)
        joined = "\n".join(parts)
        for index in range(20):
            self.assertIn(f"ماده {index}", joined)
        # page labels render
        self.assertIn("— ص 3", joined)

    def test_summary_rides_on_container_lines(self) -> None:
        entries = [
            self._entry(
                provision_id="chapter",
                label="فصل اول",
                title="کلیات",
                provision_type="chapter",
                page=1,
                end_page=4,
                summary="تعاریف و دامنه شمول قانون",
            ),
            self._entry(ordinal=1, parent_provision_id="chapter"),
        ]
        tree, _ = build_outline(entries)
        text = render_outline(tree)[0]
        self.assertIn("تعاریف و دامنه شمول قانون", text)
        self.assertIn("[فصل اول] کلیات", text)


class GetDocumentStructureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = self._build_repository()
        self.tools = LegalResearchTools(self.repository)
        self.store = RefStore()

    def _build_repository(self) -> InMemoryCanonicalRepository:
        repository = InMemoryCanonicalRepository()
        repository.add_source_document(
            SourceDocument(
                "source-struct",
                None,
                "struct-law.md",
                "text/markdown",
                "sha256:struct",
                128,
                "demo",
                "https://example.invalid/struct-law.md",
            )
        )
        repository.add_instrument(
            LegalInstrument(
                "instrument-struct",
                "قانون ساختار نمونه",
                "قانون ساختار نمونه",
                InstrumentType.STATUTE,
                "IR",
            )
        )
        repository.add_document_version(
            DocumentVersion(
                "document-struct",
                "instrument-struct",
                "source-struct",
                DocumentStatus.EFFECTIVE,
                effective_from=date(2024, 1, 1),
            )
        )
        rows = [
            ("prov-chapter", ProvisionType.CHAPTER, "فصل اول", "کلیات", None, 0),
            ("prov-1", ProvisionType.ARTICLE, "ماده ۱", None, "prov-chapter", 1),
            ("prov-note", ProvisionType.NOTE, "تبصره ۱", None, "prov-1", 2),
            ("prov-2", ProvisionType.ARTICLE, "ماده ۲", None, "prov-chapter", 3),
        ]
        depths: dict[str, int] = {}
        for provision_id, ptype, label, title, parent, ordinal in rows:
            depth = 0 if parent is None else depths[parent] + 1
            depths[provision_id] = depth
            repository.add_provision(
                Provision(
                    provision_id,
                    "instrument-struct",
                    ptype,
                    None,
                    label,
                    title=title,
                    parent_provision_id=parent,
                    ordinal=ordinal,
                    depth=depth,
                )
            )
            repository.add_provision_version(
                ProvisionVersion(
                    f"{provision_id}-v1",
                    provision_id,
                    "document-struct",
                    f"متن {label}",
                    f"متن {label}",
                    DocumentStatus.EFFECTIVE,
                    "test",
                    effective_from=date(2024, 1, 1),
                )
            )
            repository.add_source_span(
                SourceSpan(
                    f"span-{provision_id}",
                    "source-struct",
                    "document-struct",
                    f"{provision_id}-v1",
                    page_number=ordinal + 1,
                    raw_text=f"متن {label}",
                )
            )
        return repository

    def _call(self, document: str, **overrides):
        args = {
            "document": document,
            "applicable_time": APPLICABLE.isoformat(),
            **overrides,
        }
        return self.tools.dispatch(self.store, "get_document_structure", args)

    def test_outline_renders_tree_with_page_ranges(self) -> None:
        result = self._call("قانون ساختار")
        envelope = result.envelope
        self.assertEqual(envelope.status, ToolStatus.OK)
        outline = envelope.meta["outline"]
        self.assertIn("[فصل اول] کلیات", outline)
        self.assertIn("ماده ۱ — ص 2", outline)
        self.assertIn("تبصره ۱ — ص 3", outline)
        self.assertEqual(envelope.meta["total_parts"], 1)
        self.assertEqual(envelope.meta["node_count"], 4)
        self.assertFalse(envelope.has_next_page)
        # no claim items: the map is orientation, not evidence
        self.assertEqual(envelope.items, ())

    def test_summaries_are_injected(self) -> None:
        tools = LegalResearchTools(
            self.repository,
            structure_summaries={"prov-chapter": "تعاریف و شمول"},
        )
        result = tools.dispatch(
            self.store,
            "get_document_structure",
            {
                "document": "instrument-struct",
                "applicable_time": APPLICABLE.isoformat(),
            },
        )
        self.assertIn("تعاریف و شمول", result.envelope.meta["outline"])

    def test_unknown_document_is_not_found(self) -> None:
        result = self._call("قانون ناموجود")
        self.assertEqual(result.envelope.status, ToolStatus.NOT_FOUND)

    def test_requires_document_argument(self) -> None:
        with self.assertRaises(ToolArgumentError):
            self.tools.dispatch(
                self.store,
                "get_document_structure",
                {"applicable_time": APPLICABLE.isoformat()},
            )

    def test_hidden_tools_are_gated_from_catalog(self) -> None:
        from legal_agent_core.agentic.tools import tool_catalog

        tools = LegalResearchTools(
            self.repository, hidden_tools=frozenset({"get_document_structure"})
        )
        self.assertFalse(tools.spec_available("get_document_structure"))
        self.assertNotIn("get_document_structure", tool_catalog(tools))
        # dispatch still works for scripted planners / direct callers
        result = tools.dispatch(
            self.store,
            "get_document_structure",
            {
                "document": "instrument-struct",
                "applicable_time": APPLICABLE.isoformat(),
            },
        )
        self.assertEqual(result.envelope.status, ToolStatus.OK)


class GetStatuteFamilyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = self._build_repository()
        self.tools = LegalResearchTools(self.repository)
        self.store = RefStore()

    def _build_repository(self) -> InMemoryCanonicalRepository:
        from datetime import UTC, datetime

        from legal_agent_core.canonical import (
            CanonicalEdge,
            CanonicalEdgeType,
            CreationMethod,
            Provenance,
        )

        repository = InMemoryCanonicalRepository()
        repository.add_source_document(
            SourceDocument(
                "source-family",
                None,
                "family.md",
                "text/markdown",
                "sha256:family",
                64,
                "demo",
                "https://example.invalid/family.md",
            )
        )
        for instrument_id, title in (
            ("inst-base", "قانون اساسی"),
            ("inst-child", "آیین‌نامه اجرایی نمونه"),
        ):
            repository.add_instrument(
                LegalInstrument(
                    instrument_id,
                    title,
                    title,
                    InstrumentType.STATUTE,
                    "IR",
                )
            )
        repository.add_document_version(
            DocumentVersion(
                "document-family",
                "inst-child",
                "source-family",
                DocumentStatus.EFFECTIVE,
                effective_from=date(2024, 1, 1),
            )
        )
        repository.add_provision(
            Provision("prov-family", "inst-child", ProvisionType.ARTICLE, None, "ماده ۱")
        )
        repository.add_provision_version(
            ProvisionVersion(
                "prov-family-v1",
                "prov-family",
                "document-family",
                "متن",
                "متن",
                DocumentStatus.EFFECTIVE,
                "test",
                effective_from=date(2024, 1, 1),
            )
        )
        repository.add_edge(
            CanonicalEdge(
                "edge-implements-1",
                "inst-child",
                "inst-base",
                CanonicalEdgeType.IMPLEMENTS,
                Provenance(
                    created_by="test",
                    creation_method=CreationMethod.DETERMINISTIC_EXTRACTOR,
                    source_id="span-family",
                    created_at=datetime.now(UTC),
                ),
                confidence=0.9,
            )
        )
        return repository

    def test_family_reports_bases_and_derivatives(self) -> None:
        result = self.tools.dispatch(
            self.store,
            "get_statute_family",
            {
                "instrument": "آیین‌نامه اجرایی نمونه",
                "applicable_time": APPLICABLE.isoformat(),
            },
        )
        envelope = result.envelope
        self.assertEqual(envelope.status, ToolStatus.OK)
        meta = envelope.meta
        self.assertEqual(meta["statutory_bases"][0]["instrument_id"], "inst-base")
        self.assertEqual(meta["statutory_bases"][0]["title"], "قانون اساسی")
        # reverse view from the base law
        base_view = self.tools.dispatch(
            self.store,
            "get_statute_family",
            {"instrument": "inst-base", "applicable_time": APPLICABLE.isoformat()},
        )
        self.assertEqual(
            base_view.envelope.meta["instruments_implementing_it"][0]["instrument_id"],
            "inst-child",
        )

    def test_unknown_instrument_is_not_found(self) -> None:
        result = self.tools.dispatch(
            self.store,
            "get_statute_family",
            {"instrument": "قانون ناموجود", "applicable_time": APPLICABLE.isoformat()},
        )
        self.assertEqual(result.envelope.status, ToolStatus.NOT_FOUND)


if __name__ == "__main__":
    unittest.main()
