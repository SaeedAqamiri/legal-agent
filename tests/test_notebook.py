from __future__ import annotations

import unittest
from datetime import date

from legal_agent_core.notebook import NotebookEventType, capture_events
from legal_agent_core.research import ResearchEpisode


def _episode(*, visited, evidence=()):
    episode = ResearchEpisode(
        episode_id="ep-1",
        organization_id="org-1",
        user_id="user-1",
        conversation_id="conv-1",
        question="مهریه و حق حبس",
        applicable_time=date(2024, 1, 1),
    )
    episode.visited_nodes = list(visited)
    episode.evidence_ids = list(evidence)
    return episode


class NotebookCaptureTests(unittest.TestCase):
    def test_verified_answer_emits_route_and_confirmation(self) -> None:
        events = capture_events(_episode(visited=["a", "b"], evidence=["ev1"]), completed=True)
        kinds = [e.event_type for e in events]
        self.assertEqual(kinds.count(NotebookEventType.ROUTE_TAKEN), 2)
        confirmed = [e for e in events if e.event_type == NotebookEventType.ANSWER_CONFIRMED]
        self.assertEqual(len(confirmed), 1)
        self.assertEqual(confirmed[0].payload["visited_nodes"], ["a", "b"])
        self.assertEqual(confirmed[0].payload["evidence_ids"], ["ev1"])

    def test_unverified_episode_only_marks_routes(self) -> None:
        events = capture_events(_episode(visited=["a"]), completed=False)
        self.assertEqual(
            [e.event_type for e in events], [NotebookEventType.ROUTE_TAKEN]
        )

    def test_event_ids_are_deterministic_and_unique_per_node(self) -> None:
        first = capture_events(_episode(visited=["a", "b"]), completed=False)
        second = capture_events(_episode(visited=["a", "b"]), completed=False)
        self.assertEqual([e.event_id for e in first], [e.event_id for e in second])
        self.assertEqual(len({e.event_id for e in first}), 2)


if __name__ == "__main__":
    unittest.main()
