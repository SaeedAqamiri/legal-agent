import unittest

from fastapi.testclient import TestClient

from legal_agent_core.demo import create_demo_app


class DemoApplicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(create_demo_app())
        self.headers = {"Authorization": "Bearer expert-demo"}

    def test_demo_workspace_runs_research_and_loads_review_queue(self) -> None:
        workspace = self.client.get("/workspace")
        research = self.client.post(
            "/v1/research",
            headers=self.headers,
            json={
                "organization_id": "org-a",
                "question": "مهلت تجدیدنظر برای اشخاص مقیم ایران چند روز است؟",
                "applicable_time": "2026-08-11",
            },
        )
        queue = self.client.get(
            "/v1/organizations/org-a/relations", headers=self.headers
        )
        library = self.client.get(
            "/v1/organizations/org-a/library",
            params={"query": "مهلت تجدیدنظر", "applicable_time": "2026-08-11"},
            headers=self.headers,
        )

        self.assertEqual(workspace.status_code, 200)
        self.assertEqual(research.status_code, 200, research.text)
        self.assertTrue(research.json()["completed"])
        self.assertEqual(
            research.json()["answer"]["citations"][0]["provision_id"],
            "provision-demo-336",
        )
        self.assertEqual(queue.status_code, 200)
        self.assertEqual(queue.json()["count"], 1)
        self.assertEqual(library.status_code, 200)
        self.assertEqual(library.json()["count"], 1)


if __name__ == "__main__":
    unittest.main()
