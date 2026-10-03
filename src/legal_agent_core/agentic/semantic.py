"""Embedding client + in-memory span index for semantic search.

The client speaks the OpenAI-compatible ``POST /embeddings`` contract, so any
provider works (local TEI/Ollama/vLLM on a GPU box, or a cloud API). The index
embeds page-level spans once (lazily) and answers cosine queries; results stay
canonical spans, so the verifier and publish gate are unaffected.
"""

from __future__ import annotations

import json
import math
import os
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..errors import DomainError


class EmbeddingClient(Protocol):
    model: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class OpenAIEmbeddingClient:
    """Dependency-free client for OpenAI-compatible embedding endpoints."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float = 120.0,
        batch_size: int = 64,
    ) -> None:
        if not base_url.startswith(("http://", "https://")):
            raise DomainError("embeddings base_url must be an http(s) endpoint")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.batch_size = batch_size

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            batch = texts[start : start + self.batch_size]
            request = urllib.request.Request(
                f"{self.base_url}/embeddings",
                data=json.dumps(
                    {"model": self.model, "input": batch}, ensure_ascii=False
                ).encode("utf-8"),
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.api_key}",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                    body = json.loads(response.read().decode("utf-8"))
            except (urllib.error.URLError, TimeoutError) as exc:
                raise DomainError(f"embeddings endpoint failed: {exc}") from exc
            data = body.get("data")
            if not isinstance(data, list) or len(data) != len(batch):
                raise DomainError("embeddings response shape mismatch")
            ordered = sorted(data, key=lambda item: item.get("index", 0))
            for item in ordered:
                vector = item.get("embedding")
                if not isinstance(vector, list):
                    raise DomainError("embeddings response missing vector")
                vectors.append(vector)
        return vectors

    @classmethod
    def from_env(cls) -> OpenAIEmbeddingClient:
        base_url = os.environ.get("LEGAL_AGENT_EMBEDDINGS_BASE_URL")
        api_key = os.environ.get("LEGAL_AGENT_EMBEDDINGS_API_KEY", "")
        model = os.environ.get("LEGAL_AGENT_EMBEDDINGS_MODEL", "bge-m3")
        if not base_url:
            raise DomainError(
                "semantic search needs LEGAL_AGENT_EMBEDDINGS_BASE_URL "
                "(+ optional LEGAL_AGENT_EMBEDDINGS_API_KEY/MODEL)"
            )
        return cls(base_url, api_key, model)


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


@dataclass(slots=True)
class IndexedSpan:
    source_span_id: str
    provision_version_id: str
    page: int
    text: str
    vector: list[float]


@dataclass(slots=True)
class SemanticHit:
    span: IndexedSpan
    similarity: float


@dataclass(slots=True)
class SemanticSpanIndex:
    """Lazy in-memory vector index over the corpus's page-level spans."""

    canonical: Any
    client: EmbeddingClient
    max_chars: int = 1_500
    entries: list[IndexedSpan] = field(default_factory=list)
    built: bool = False

    def ensure_built(self) -> int:
        if self.built:
            return len(self.entries)
        jobs: list[tuple[Any, Any, str]] = []
        for version in self.canonical.list_provision_versions():
            for span in self.canonical.source_spans_for_version(version.provision_version_id):
                text = span.raw_text[: self.max_chars]
                if text.strip():
                    jobs.append((span, version, text))
        vectors = self.client.embed([text for _, _, text in jobs])
        if len(vectors) != len(jobs):
            raise DomainError("embedding index build: vector count mismatch")
        self.entries = [
            IndexedSpan(
                source_span_id=span.source_span_id,
                provision_version_id=version.provision_version_id,
                page=span.page_number,
                text=text,
                vector=vector,
            )
            for (span, version, text), vector in zip(jobs, vectors)
        ]
        self.built = True
        return len(self.entries)

    def query(self, query: str, top_k: int = 10) -> list[SemanticHit]:
        if not query.strip():
            raise DomainError("semantic query must not be blank")
        self.ensure_built()
        [query_vector] = self.client.embed([query[: self.max_chars]])
        scored = [
            SemanticHit(entry, cosine(query_vector, entry.vector))
            for entry in self.entries
        ]
        scored.sort(key=lambda hit: -hit.similarity)
        return scored[:top_k]


__all__ = [
    "EmbeddingClient",
    "IndexedSpan",
    "OpenAIEmbeddingClient",
    "SemanticHit",
    "SemanticSpanIndex",
    "cosine",
]
