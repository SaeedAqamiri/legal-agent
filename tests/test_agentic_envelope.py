from __future__ import annotations

import unittest

from legal_agent_core.agentic.envelope import (
    ITEMS_IN_CONTEXT,
    RefStore,
    ToolStatus,
    empty_envelope,
    make_envelope,
)
from legal_agent_core.errors import DomainError


def _item(index: int) -> dict:
    return {"provision_version_id": f"v{index:03d}", "text": f"متن {index}"}


class RefStoreTests(unittest.TestCase):
    def test_register_assigns_sequential_refs_and_keeps_full_items(self) -> None:
        store = RefStore()
        items = tuple(_item(index) for index in range(1, 41))
        ref = store.register(items)
        self.assertEqual(ref, "r1")
        self.assertEqual(len(store.get(ref)), 40)

    def test_claim_items_exclude_aux_items(self) -> None:
        store = RefStore()
        store.register((_item(1),))
        store.add((_item(2),))
        claim_ids = [item["provision_version_id"] for item in store.claim_items]
        all_ids = [item["provision_version_id"] for item in store.all_items]
        self.assertEqual(claim_ids, ["v001"])
        self.assertEqual(all_ids, ["v001", "v002"])

    def test_get_unknown_ref_returns_empty(self) -> None:
        store = RefStore()
        self.assertEqual(store.get("missing"), ())


class EnvelopeTests(unittest.TestCase):
    def test_compact_page_and_honest_pagination(self) -> None:
        store = RefStore()
        items = tuple(_item(index) for index in range(1, 41))
        envelope = make_envelope(
            store, items, total_count=40, has_next_page=True, next_cursor="p2"
        )
        self.assertEqual(envelope.returned_count, ITEMS_IN_CONTEXT)
        self.assertEqual(len(envelope.items), ITEMS_IN_CONTEXT)
        self.assertTrue(envelope.has_next_page)
        self.assertFalse(envelope.complete)
        payload = envelope.to_payload()
        self.assertEqual(payload["total_count"], 40)
        self.assertEqual(payload["next_cursor"], "p2")
        self.assertEqual(payload["status"], "ok")

    def test_full_page_is_complete(self) -> None:
        store = RefStore()
        envelope = make_envelope(store, (_item(1),), total_count=1)
        self.assertTrue(envelope.complete)
        self.assertFalse(envelope.has_next_page)

    def test_errors_mark_incomplete(self) -> None:
        store = RefStore()
        envelope = make_envelope(store, (_item(1),), errors=("timeout",))
        self.assertFalse(envelope.complete)

    def test_empty_envelope_requires_non_ok_status(self) -> None:
        store = RefStore()
        with self.assertRaises(DomainError):
            empty_envelope(store, ToolStatus.OK)
        envelope = empty_envelope(store, ToolStatus.EMPTY)
        self.assertEqual(envelope.status, ToolStatus.EMPTY)
        self.assertFalse(envelope.complete)
        self.assertEqual(envelope.items, ())

    def test_partial_status_keeps_items(self) -> None:
        store = RefStore()
        envelope = make_envelope(
            store,
            (_item(1), _item(2)),
            status=ToolStatus.PARTIAL,
            errors=("field failure",),
        )
        self.assertEqual(envelope.returned_count, 2)
        self.assertFalse(envelope.complete)


if __name__ == "__main__":
    unittest.main()
