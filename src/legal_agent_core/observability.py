from __future__ import annotations

from dataclasses import dataclass
from threading import Lock


@dataclass(frozen=True, slots=True)
class DurationSummary:
    count: int
    total_ms: float
    maximum_ms: float


@dataclass(frozen=True, slots=True)
class MetricsSnapshot:
    counters: dict[str, int]
    durations: dict[str, DurationSummary]


@dataclass(slots=True)
class _DurationAccumulator:
    count: int = 0
    total_ms: float = 0.0
    maximum_ms: float = 0.0

    def add(self, value: float) -> None:
        self.count += 1
        self.total_ms += value
        self.maximum_ms = max(self.maximum_ms, value)


class MetricsRegistry:
    """Small in-process metric sink; production exporters can mirror this interface."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._counters: dict[tuple[str, str | None], int] = {}
        self._durations: dict[tuple[str, str | None], _DurationAccumulator] = {}

    def increment(
        self, name: str, *, organization_id: str | None = None, value: int = 1
    ) -> None:
        key = (name, organization_id)
        with self._lock:
            self._counters[key] = self._counters.get(key, 0) + value

    def observe(
        self, name: str, duration_ms: float, *, organization_id: str | None = None
    ) -> None:
        key = (name, organization_id)
        with self._lock:
            self._durations.setdefault(key, _DurationAccumulator()).add(duration_ms)

    def snapshot(self, organization_id: str) -> MetricsSnapshot:
        with self._lock:
            counters = {
                name: value
                for (name, tenant), value in self._counters.items()
                if tenant == organization_id
            }
            durations = {
                name: DurationSummary(
                    accumulator.count,
                    round(accumulator.total_ms, 3),
                    round(accumulator.maximum_ms, 3),
                )
                for (name, tenant), accumulator in self._durations.items()
                if tenant == organization_id and accumulator.count
            }
        return MetricsSnapshot(counters, durations)
