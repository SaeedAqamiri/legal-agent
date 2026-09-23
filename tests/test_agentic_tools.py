from __future__ import annotations

import unittest

from agentic_fixture import APPLICABLE, add_resolved_reference, build_canonical

from legal_agent_core.agentic.envelope import RefStore, ToolStatus
from legal_agent_core.agentic.tools import (
    LegalResearchTools,
    ToolArgumentError,
    is_known_tool,
    tool_catalog,
)
from legal_agent_core.errors import DomainError
from legal_agent_core.retrieval import search_tokens


class ToolCatalogTests(unittest.TestCase):
    def test_catalog_lists_all_tools(self) -> None:
        catalog = tool_catalog()
        for name in (
            "search_provisions",
            "get_provision_version",
            "get_version_history",
            "get_references",
            "temporal_check",
            "aggregate_count",
        ):
            self.assertIn(name, catalog)
            self.assertTrue(is_known_tool(name))
        self.assertFalse(is_known_tool("delete_everything"))


class SearchProvisionsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = build_canonical()
        add_resolved_reference(self.repository)
        self.tools = LegalResearchTools(self.repository, items_per_page=2)
        self.store = RefStore()

    def _search(self, query: str, **overrides):
        args = {
            "query": query,
            "applicable_time": APPLICABLE.isoformat(),
            **overrides,
        }
        return self.tools.dispatch(self.store, "search_provisions", args)

    def test_search_is_temporal_and_deterministic(self) -> None:
        result = self._search("مهلت تجدیدنظر بیست روز")
        envelope = result.envelope
        self.assertEqual(envelope.status, ToolStatus.OK)
        # v1 (2024) نباید در تاریخ 2025-06-01 قابل اعمال باشد.
        ids = [item["provision_version_id"] for item in envelope.items]
        self.assertNotIn("provision-demo-336-v1", ids)
        self.assertIn("provision-demo-336-v2", ids)

        replay = self._search("مهلت تجدیدنظر بیست روز")
        self.assertEqual(
            [item["provision_version_id"] for item in replay.envelope.items],
            ids,
        )

    def test_search_paginates_with_cursor(self) -> None:
        tools = LegalResearchTools(self.repository, items_per_page=1)
        args = {"query": "ماده", "applicable_time": APPLICABLE.isoformat()}
        first = tools.dispatch(self.store, "search_provisions", args)
        self.assertTrue(first.envelope.has_next_page)
        self.assertEqual(first.envelope.total_count, 2)
        self.assertEqual(first.envelope.next_cursor, "p2")
        second = tools.dispatch(
            self.store, "search_provisions", {**args, "cursor": "p2"}
        )
        self.assertFalse(second.envelope.has_next_page)
        self.assertEqual(len(second.envelope.items), 1)
        first_ids = {item["provision_version_id"] for item in first.envelope.items}
        second_ids = {item["provision_version_id"] for item in second.envelope.items}
        self.assertEqual(len(first_ids | second_ids), 2)

    def test_search_beyond_last_page_is_not_found(self) -> None:
        result = self._search("مهلت تجدیدنظر", cursor="p9")
        self.assertEqual(result.envelope.status, ToolStatus.NOT_FOUND)

    def test_search_no_match_is_empty(self) -> None:
        result = self._search("فهرست کشتی‌های بادبانی")
        self.assertEqual(result.envelope.status, ToolStatus.EMPTY)

    def test_items_carry_citable_evidence_ids(self) -> None:
        result = self._search("مهلت تجدیدنظر")
        for item in result.envelope.items:
            self.assertIsNotNone(item["evidence_id"])
            self.assertIsNotNone(item["page"])
        self.assertTrue(result.evidence)
        evidence = result.evidence[0]
        self.assertEqual(evidence.applicable_time, APPLICABLE)
        self.assertIn("مهلت", search_tokens(evidence.text))

    def test_search_follows_resolved_references_into_ledger_evidence(self) -> None:
        result = self._search("فصل دوم")
        referenced = result.envelope.meta.get("referenced", [])
        self.assertTrue(referenced)
        self.assertTrue(any(item["evidence_id"] for item in referenced))

    def test_invalid_args_raise_tool_argument_error(self) -> None:
        with self.assertRaises(ToolArgumentError):
            self._search("   ")
        with self.assertRaises(ToolArgumentError):
            self._search("مهلت", applicable_time="2025-13-99")
        with self.assertRaises(ToolArgumentError):
            self._search("مهلت", cursor="x3")
        with self.assertRaises(ToolArgumentError):
            self.tools.dispatch(
                self.store,
                "search_provisions",
                {"query": "مهلت", "applicable_time": APPLICABLE.isoformat(), "bogus": 1},
            )

    def test_unknown_tool_is_rejected(self) -> None:
        with self.assertRaises(ToolArgumentError):
            self.tools.dispatch(self.store, "write_law", {})

    def test_scope_filters_results(self) -> None:
        result = self._search("ماده", document_scope=["provision-demo-340"])
        ids = [item["provision_version_id"] for item in result.envelope.items]
        self.assertTrue(ids)
        self.assertTrue(all("provision-demo-340" in item for item in ids))

    def test_scope_accepts_human_readable_title(self) -> None:
        # planners pass document titles, not opaque ids (qwen/GLM behavior)
        result = self._search("مهلت", document_scope=["قانون نمونه آیین دادرسی"])
        self.assertEqual(result.envelope.status, ToolStatus.OK)
        self.assertNotIn("scope_unresolved", result.envelope.meta)

    def test_scope_accepts_single_string(self) -> None:
        result = self._search("مهلت", document_scope="قانون نمونه آیین دادرسی")
        self.assertEqual(result.envelope.status, ToolStatus.OK)

    def test_unresolved_scope_is_reported(self) -> None:
        result = self._search("مهلت", document_scope=["سند ناموجود"])
        self.assertEqual(result.envelope.status, ToolStatus.EMPTY)
        self.assertIn("scope_unresolved", result.envelope.meta)
        self.assertIn("scope_hint", result.envelope.meta)

    def test_loose_and_jalali_dates_are_accepted(self) -> None:
        loose = self.tools.dispatch(
            self.store,
            "temporal_check",
            {"provision_id": "provision-demo-336", "applicable_time": "2025-6-1"},
        )
        self.assertEqual(loose.envelope.status, ToolStatus.OK)
        jalali = self.tools.dispatch(
            self.store,
            "temporal_check",
            {"provision_id": "provision-demo-336", "applicable_time": "۱۴۰۴/۰۳/۱۱"},
        )
        self.assertEqual(jalali.envelope.status, ToolStatus.OK)


class TemporalAndHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = build_canonical()
        self.tools = LegalResearchTools(self.repository)
        self.store = RefStore()

    def _dispatch(self, name: str, **args):
        return self.tools.dispatch(self.store, name, args)

    def test_temporal_check_returns_only_applicable_version(self) -> None:
        result = self._dispatch(
            "temporal_check",
            provision_id="provision-demo-336",
            applicable_time=APPLICABLE.isoformat(),
        )
        self.assertEqual(result.envelope.status, ToolStatus.OK)
        self.assertEqual(result.envelope.total_count, 1)
        self.assertEqual(
            result.envelope.items[0]["provision_version_id"],
            "provision-demo-336-v2",
        )

    def test_temporal_check_between_versions_is_empty_with_meaning(self) -> None:
        result = self._dispatch(
            "temporal_check",
            provision_id="provision-demo-336",
            applicable_time="2024-12-31",
        )
        # v1 تا 2024-12-31 اعتبار دارد؛ پس EMPTY نمی‌شود.
        self.assertEqual(result.envelope.status, ToolStatus.OK)
        gap = self._dispatch(
            "temporal_check",
            provision_id="provision-demo-336",
            applicable_time="2023-06-01",
        )
        self.assertEqual(gap.envelope.status, ToolStatus.EMPTY)
        self.assertIn("meaning", gap.envelope.meta)

    def test_temporal_check_unknown_provision_is_not_found(self) -> None:
        result = self._dispatch(
            "temporal_check",
            provision_id="nope",
            applicable_time=APPLICABLE.isoformat(),
        )
        self.assertEqual(result.envelope.status, ToolStatus.NOT_FOUND)

    def test_version_history_lists_versions_in_order(self) -> None:
        result = self._dispatch(
            "get_version_history",
            provision_id="provision-demo-336",
            applicable_time=APPLICABLE.isoformat(),
        )
        ids = [item["provision_version_id"] for item in result.envelope.items]
        self.assertEqual(
            ids,
            ["provision-demo-336-v1", "provision-demo-336-v2"],
        )
        self.assertTrue(result.envelope.items[-1]["applies_at_time"])
        self.assertFalse(result.envelope.items[0]["applies_at_time"])

    def test_get_provision_version_full_text_and_unknown(self) -> None:
        result = self._dispatch(
            "get_provision_version",
            provision_version_id="provision-demo-336-v2",
            applicable_time=APPLICABLE.isoformat(),
        )
        item = result.envelope.items[0]
        self.assertEqual(item["text"], "مهلت درخواست تجدیدنظر اشخاص مقیم ایران سی روز است.")
        self.assertTrue(result.evidence)

        missing = self._dispatch(
            "get_provision_version",
            provision_version_id="ghost",
            applicable_time=APPLICABLE.isoformat(),
        )
        self.assertEqual(missing.envelope.status, ToolStatus.NOT_FOUND)

    def test_references_include_resolved_targets(self) -> None:
        add_resolved_reference(self.repository)
        result = self._dispatch(
            "get_references",
            provision_version_id="provision-demo-340-v2",
            applicable_time=APPLICABLE.isoformat(),
        )
        self.assertEqual(result.envelope.items[0]["resolution_status"], "resolved")
        referenced = result.envelope.meta["referenced"]
        self.assertEqual(referenced[0]["provision_id"], "provision-demo-336")

    def test_aggregate_count_is_exact(self) -> None:
        result = self._dispatch(
            "aggregate_count",
            query="مهلت تجدیدنظر",
            applicable_time=APPLICABLE.isoformat(),
        )
        self.assertEqual(result.envelope.items[0]["matched_provision_versions"], 1)

    def test_dispatch_rejects_non_object_args(self) -> None:
        with self.assertRaises(DomainError):
            self.tools.dispatch(self.store, "search_provisions", ["not", "a", "dict"])


if __name__ == "__main__":
    unittest.main()
