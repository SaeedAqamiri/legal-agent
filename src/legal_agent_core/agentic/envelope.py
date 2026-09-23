"""Tool result envelope (honest pagination + result_ref registry).

The model only ever sees a compact page of items; the full item list stays
behind a ``result_ref`` so later steps and the artifact/ledger builder can use
complete data. ``complete=False`` means "the needed set was not fully
examined" — it is never silently interpreted as "not found".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ..errors import DomainError

ITEMS_IN_CONTEXT = 30


class ToolStatus(StrEnum):
    OK = "ok"
    PARTIAL = "partial"
    EMPTY = "empty"
    NOT_FOUND = "not_found"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class Envelope:
    result_ref: str
    status: ToolStatus = ToolStatus.OK
    items: tuple[dict[str, Any], ...] = ()
    returned_count: int = 0
    total_count: int | None = None
    next_cursor: str | None = None
    has_next_page: bool = False
    complete: bool = True
    errors: tuple[str, ...] = ()
    meta: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        """Compact, JSON-normalized view handed to the planner or a transport."""
        return {
            "result_ref": self.result_ref,
            "status": self.status.value,
            "items": [_jsonify(item) for item in self.items],
            "returned_count": self.returned_count,
            "total_count": self.total_count,
            "next_cursor": self.next_cursor,
            "has_next_page": self.has_next_page,
            "complete": self.complete,
            "errors": list(self.errors),
            "meta": _jsonify(self.meta),
        }


class RefStore:
    """Keeps the full item list behind every ``result_ref``.

    Items added via :meth:`add` (aux) count for evidence backing but never
    become claims — claims must come from what a tool actually returned to the
    planner.
    """

    def __init__(self) -> None:
        self._store: dict[str, tuple[dict[str, Any], ...]] = {}
        self._aux: tuple[dict[str, Any], ...] = ()
        self._counter = 0

    def register(self, items: tuple[dict[str, Any], ...]) -> str:
        self._counter += 1
        ref = f"r{self._counter}"
        self._store[ref] = items
        return ref

    def add(self, items: tuple[dict[str, Any], ...]) -> None:
        self._aux = self._aux + tuple(items)

    def get(self, ref: str) -> tuple[dict[str, Any], ...]:
        return self._store.get(ref, ())

    @property
    def claim_items(self) -> tuple[dict[str, Any], ...]:
        return tuple(item for items in self._store.values() for item in items)

    @property
    def all_items(self) -> tuple[dict[str, Any], ...]:
        return self.claim_items + self._aux


def make_envelope(
    store: RefStore,
    items: tuple[dict[str, Any], ...],
    *,
    status: ToolStatus = ToolStatus.OK,
    has_next_page: bool = False,
    next_cursor: str | None = None,
    total_count: int | None = None,
    errors: tuple[str, ...] = (),
    complete: bool | None = None,
    meta: dict[str, Any] | None = None,
) -> Envelope:
    if complete is None:
        complete = status is ToolStatus.OK and not has_next_page and not errors
    return Envelope(
        result_ref=store.register(items),
        status=status,
        items=tuple(items[:ITEMS_IN_CONTEXT]),
        returned_count=min(len(items), ITEMS_IN_CONTEXT),
        total_count=total_count,
        next_cursor=next_cursor,
        has_next_page=has_next_page,
        complete=complete,
        errors=tuple(errors),
        meta=meta or {},
    )


def empty_envelope(
    store: RefStore,
    status: ToolStatus,
    errors: tuple[str, ...] = (),
    meta: dict[str, Any] | None = None,
) -> Envelope:
    if status in (ToolStatus.OK,):
        raise DomainError("empty envelope requires a non-OK status")
    return make_envelope(store, (), status=status, errors=errors, complete=False, meta=meta)


def _jsonify(value: Any) -> Any:
    """Normalize tuples to lists so payloads are stable across transports."""
    if isinstance(value, tuple):
        return [_jsonify(item) for item in value]
    if isinstance(value, list):
        return [_jsonify(item) for item in value]
    if isinstance(value, dict):
        return {key: _jsonify(item) for key, item in value.items()}
    return value
