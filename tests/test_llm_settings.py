"""Tests for runtime LLM gateway settings (store, provider, API)."""

from __future__ import annotations

import json
import tempfile
import unittest
from itertools import count
from pathlib import Path

from agentic_fixture import build_canonical
from fastapi.testclient import TestClient

from legal_agent_core.answering import CitationPipeline
from legal_agent_core.api import APIContainer, create_app
from legal_agent_core.application import (
    InMemoryResearchRecordRepository,
    KnowledgeReviewApplicationService,
    ResearchApplicationService,
)
from legal_agent_core.errors import DomainError
from legal_agent_core.evaluation import EvaluationRunner
from legal_agent_core.graph_chat_provider import GraphChatProvider
from legal_agent_core.in_memory import InMemoryResearchGraphRepository
from legal_agent_core.navigation import NavigationLoop
from legal_agent_core.observability import MetricsRegistry
from legal_agent_core.retrieval import MultiSignalEvidenceRetriever
from legal_agent_core.runtime_settings import LLMConfig, LLMSettingsStore
from legal_agent_core.security import AuthorizationService, Principal, Role
from legal_agent_core.services import ProgressiveMemoryService
from legal_agent_core.verification import DraftAnswer, EvidenceVerifier


class _FakeGraph:
    def ro_query(self, query, params=None):  # pragma: no cover - never called in these tests
        raise AssertionError("graph should not be queried")


class DeterministicComposer:
    def compose(self, question, applicable_time, evidence):
        del question, applicable_time, evidence
        return DraftAnswer("answer-1", "پاسخ آزمایشی", ())


class LLMSettingsStoreTests(unittest.TestCase):
    def test_env_fallback_defaults_to_avalai(self) -> None:
        store = LLMSettingsStore(None)
        config = store.load()
        self.assertEqual(config.base_url, "https://api.avalai.ir/v1")
        self.assertEqual(config.model, "deepseek-v4.1-flash")
        self.assertFalse(config.configured)

    def test_save_load_roundtrip_and_masking(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "llm_settings.json"
            store = LLMSettingsStore(path)
            store.save(
                LLMConfig(
                    base_url="https://api.avalai.ir/v1",
                    model="glm-5.3-flash",
                    api_key="sk-verysecretkey1234",
                )
            )
            reloaded = LLMSettingsStore(path).load()
            self.assertEqual(reloaded.model, "glm-5.3-flash")
            self.assertEqual(reloaded.api_key, "sk-verysecretkey1234")
            public = reloaded.public()
            self.assertTrue(public["configured"])
            self.assertTrue(public["api_key_set"])
            self.assertNotIn("verysecret", json.dumps(public))
            self.assertEqual(public["models"], ["deepseek-v4.1-flash", "glm-5.3-flash"])

    def test_corrupt_file_falls_back_to_env(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "llm_settings.json"
            path.write_text("{not json", encoding="utf-8")
            config = LLMSettingsStore(path).load()
            self.assertEqual(config.base_url, "https://api.avalai.ir/v1")


class GraphChatProviderTests(unittest.TestCase):
    def test_unconfigured_then_configured(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = LLMSettingsStore(Path(directory) / "llm_settings.json")
            provider = GraphChatProvider(_FakeGraph(), None, store)
            self.assertFalse(provider.available)
            with self.assertRaises(DomainError):
                provider.stream("سلام")
            store.save(
                LLMConfig(
                    base_url="https://api.avalai.ir/v1",
                    model="deepseek-v4.1-flash",
                    api_key="sk-test",
                )
            )
            provider.reload()
            self.assertTrue(provider.available)


class _FakeSettingsProvider:
    def __init__(self) -> None:
        self._config = LLMConfig(
            base_url="https://api.avalai.ir/v1",
            model="deepseek-v4.1-flash",
            api_key=None,
        )
        self.store = self

    @property
    def config(self) -> LLMConfig:
        return self._config

    @property
    def available(self) -> bool:
        return self._config.configured

    def save(self, config: LLMConfig) -> None:
        self._config = config

    def reload(self) -> None:
        pass

    def probe(self) -> dict:
        if not self._config.configured:
            raise DomainError("ابتدا کلید API را ذخیره کنید")
        return {"ok": True, "model": self._config.model, "latency_ms": 12, "sample": "ok"}


class LLMSettingsEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        canonical = build_canonical()
        graph = InMemoryResearchGraphRepository()
        authorization = AuthorizationService()
        metrics = MetricsRegistry()
        identifiers = count(1)
        research = ResearchApplicationService(
            NavigationLoop(
                MultiSignalEvidenceRetriever(canonical, graph),
                DeterministicComposer(),
                EvidenceVerifier(canonical),
            ),
            CitationPipeline(canonical),
            InMemoryResearchRecordRepository(),
            authorization,
            metrics,
            id_factory=lambda: str(next(identifiers)),
        )
        memory = ProgressiveMemoryService(graph)
        reviews = KnowledgeReviewApplicationService(memory, authorization, metrics)
        principal = Principal("local-user", "org-a", frozenset({Role.ADMIN}))
        self.provider = _FakeSettingsProvider()
        self.client = TestClient(
            create_app(
                APIContainer(
                    research,
                    reviews,
                    EvaluationRunner(research, authorization),
                    authorization,
                    authorization,
                    metrics,
                    default_principal=principal,
                    llm_settings=self.provider,
                )
            )
        )

    def test_get_returns_masked_defaults(self) -> None:
        response = self.client.get("/v1/settings/llm")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["base_url"], "https://api.avalai.ir/v1")
        self.assertFalse(body["configured"])
        self.assertIn("deepseek-v4.1-flash", body["models"])

    def test_put_saves_key_and_never_returns_it(self) -> None:
        response = self.client.put(
            "/v1/settings/llm",
            json={
                "base_url": "https://api.avalai.ir/v1",
                "model": "glm-5.3-flash",
                "api_key": "sk-supersecret9999",
            },
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["configured"])
        self.assertTrue(body["api_key_set"])
        self.assertNotIn("supersecret", response.text)
        self.assertEqual(self.provider.config.api_key, "sk-supersecret9999")

    def test_put_blank_key_keeps_existing(self) -> None:
        self.provider.save(
            LLMConfig("https://api.avalai.ir/v1", "deepseek-v4.1-flash", "sk-existing")
        )
        response = self.client.put(
            "/v1/settings/llm",
            json={"base_url": "https://api.avalai.ir/v1", "model": "glm-5.3-flash"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.provider.config.api_key, "sk-existing")
        self.assertEqual(self.provider.config.model, "glm-5.3-flash")

    def test_put_rejects_non_http_base_url(self) -> None:
        response = self.client.put(
            "/v1/settings/llm",
            json={"base_url": "ftp://x", "model": "glm-5.3-flash", "api_key": "k"},
        )
        self.assertEqual(response.status_code, 422)

    def test_test_endpoint_probes(self) -> None:
        self.provider.save(
            LLMConfig("https://api.avalai.ir/v1", "deepseek-v4.1-flash", "sk-x")
        )
        response = self.client.post("/v1/settings/llm/test")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
