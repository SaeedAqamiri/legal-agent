from __future__ import annotations

import unittest

from legal_agent_core.agentic.envelope import RefStore, ToolStatus
from legal_agent_core.agentic.tree_tools import TreeTools


def _tree() -> dict:
    return {
        "id": "root",
        "kind": "root",
        "label": "قوانین",
        "children": [
            {
                "id": "branch:social",
                "kind": "branch",
                "label": "تأمین اجتماعی",
                "basis": "topic",
                "children": [
                    {
                        "id": "inst:appeal",
                        "kind": "instrument",
                        "label": "بخشنامه اعتراض کارفرما به حق بیمه",
                        "summary": "مهلت اعتراض کارفرما به بدهی بیمه را تعیین می‌کند.",
                        "basis": "topic",
                        "children": [],
                    },
                    {
                        "id": "inst:drivers",
                        "kind": "instrument",
                        "label": "بیمه اجتماعی رانندگان حمل و نقل بین شهری",
                        "summary": "شرایط رانندگان دارای دفترچه کار را بیان می‌کند.",
                        "basis": "topic",
                        "children": [],
                    },
                ],
            },
            {
                "id": "branch:tax",
                "kind": "branch",
                "label": "مالیات",
                "basis": "topic",
                "children": [
                    {
                        "id": "inst:tax",
                        "kind": "instrument",
                        "label": "بخشودگی جرایم مالیاتی",
                        "summary": "پس از ابلاغ، بدهی را در مهلت مقرر پرداخت کند.",
                        "basis": "topic",
                        "children": [],
                    }
                ],
            },
        ],
    }


class TreeToolRankingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tools = TreeTools(_tree())

    def test_function_words_do_not_make_unrelated_document_win(self) -> None:
        result = self.tools.dispatch(
            RefStore(),
            "walk_tree",
            {
                "topic": (
                    "کارفرما پس از ابلاغ بدهی اگر به مبلغ حق بیمه اعتراض داشته باشد "
                    "تا چه زمانی فرصت اعتراض دارد؟"
                )
            },
        )
        self.assertEqual(result.status, ToolStatus.OK)
        self.assertEqual(result.items[0]["target_id"], "inst:appeal")
        self.assertNotIn("به", result.items[0]["matched_tokens"])
        self.assertNotIn("از", result.items[0]["matched_tokens"])

    def test_route_reports_evidence_for_its_ranking(self) -> None:
        result = self.tools.dispatch(
            RefStore(),
            "walk_tree",
            {
                "topic": (
                    "راننده با دفترچه کار معتبر بین شهری حق بیمه پرداخته است؛ "
                    "آیا سابقه او پذیرفته می‌شود؟"
                )
            },
        )
        top = result.items[0]
        self.assertEqual(top["target_id"], "inst:drivers")
        self.assertIn("راننده", top["matched_tokens"])
        self.assertIn(top["route_confidence"], {"strong", "weak"})
        self.assertGreater(top["coverage"], 0)
        self.assertGreater(top["rank_score"], 0)


if __name__ == "__main__":
    unittest.main()
