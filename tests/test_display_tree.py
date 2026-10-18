from __future__ import annotations

import unittest

from legal_agent_core.display_tree import (
    UNCLASSIFIED_BRANCH,
    DisplayInstrument,
    build_display_tree,
)


def _inst(iid, title, domain=None, tier=3, kind="statute"):
    return DisplayInstrument(iid, title, kind=kind, tier=tier, domain=domain)


class DisplayTreeTests(unittest.TestCase):
    def test_derivation_parent_beats_topic_folder(self) -> None:
        instruments = [
            _inst("const", "قانون اساسی", domain="سایر", tier=1),
            _inst("law", "قانون حمایت از خانواده و جوانی جمعیت", domain="خانواده", tier=2),
            _inst("reg", "آیین‌نامه اجرایی ماده ۴", domain="خانواده", tier=4),
        ]
        tree = build_display_tree(instruments, [("reg", "law", 0.9)], "const")

        # the regulation nests under the law it implements, not as a sibling
        branches = {c["label"]: c for c in tree["children"] if c["kind"] == "branch"}
        family = branches["حقوق مدنی و خانواده"]["children"][0]
        law_node = family["children"][0]
        self.assertEqual(law_node["id"], "law")
        self.assertEqual(law_node["children"][0]["id"], "reg")
        self.assertEqual(law_node["children"][0]["basis"], "derivation")
        self.assertEqual(tree["stats"]["placed_by_derivation"], 1)

    def test_constitution_edges_are_not_structural_parents(self) -> None:
        instruments = [
            _inst("const", "قانون اساسی", domain="سایر", tier=1),
            _inst("reg", "آیین‌نامه‌ای", domain="بهداشت و درمان", tier=4),
        ]
        tree = build_display_tree(instruments, [("reg", "const", 0.9)], "const")

        # reg must fall back to its topic folder, not become a child of the constitution
        self.assertEqual(tree["stats"]["placed_by_derivation"], 0)
        self.assertEqual(tree["stats"]["placed_by_topic"], 1)

    def test_unknown_domain_goes_to_unclassified_branch(self) -> None:
        instruments = [
            _inst("const", "قانون اساسی", domain="سایر", tier=1),
            _inst("x", "سندی بدون دامنه", domain=None, tier=3),
        ]
        tree = build_display_tree(instruments, [], "const")

        labels = [c["label"] for c in tree["children"] if c["kind"] == "branch"]
        self.assertIn("طبقه‌بندی‌نشده", labels)
        unclassified = next(
            c for c in tree["children"] if c["id"] == f"branch:{UNCLASSIFIED_BRANCH}"
        )
        self.assertEqual(unclassified["children"][0]["children"][0]["id"], "x")

    def test_cycles_do_not_hang(self) -> None:
        instruments = [
            _inst("const", "قانون اساسی", domain="سایر", tier=1),
            _inst("a", "الف", domain="مدنی"),
            _inst("b", "ب", domain="مدنی"),
        ]
        tree = build_display_tree(instruments, [("a", "b", 0.9), ("b", "a", 0.9)], "const")
        self.assertIsInstance(tree, dict)


if __name__ == "__main__":
    unittest.main()
