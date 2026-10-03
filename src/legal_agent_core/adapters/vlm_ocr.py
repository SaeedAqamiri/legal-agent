"""VLM-based OCR adapter for scanned Persian legal documents.

Implements the ``OCRAdapter`` port (``extract(artifact) -> OCRResult``) by
sending a page image to an OpenAI-compatible vision model (e.g.
``qwen3.8-flash`` on avalai). The prompt demands verbatim transcription with
``[ناخوانا]`` markers instead of guesses — fluent hallucination is worse than
visible gaps in a legal corpus.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Protocol

from ..errors import DomainError
from ..ingestion.operations import OCRResult, SourceArtifact

PROMPT = (
    "رونویسیِ دقیق (verbatim) متن این صفحه‌ی سند حقوقی فارسی. قاعده‌ها: "
    "۱) هر خط را همان‌طور که هست بنویس؛ واژه‌های ناخوانا را [ناخوانا] علامت بزن، حدس نزن. "
    "۲) شماره‌ها و تاریخ‌ها را دقیق رونویسی کن. "
    "۳) هیچ توضیح، مقدمه یا markdown اضافه ننویس."
)


class JsonTransport(Protocol):
    def post(self, url: str, headers: dict[str, str], payload: dict[str, Any], timeout: float) -> tuple[int, Any]: ...


class UrllibTransport:
    def post(self, url: str, headers: dict[str, str], payload: dict[str, Any], timeout: float) -> tuple[int, Any]:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                body = json.loads(exc.read().decode("utf-8"))
            except Exception:  # noqa: BLE001
                body = {}
            return exc.code, body
        except (urllib.error.URLError, TimeoutError) as exc:
            raise DomainError(f"VLM OCR transport failed: {exc}") from exc


class VLMOCRAdapter:
    """Vision-LLM OCR implementing the ``OCRAdapter`` port."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float = 180.0,
        transport: JsonTransport | None = None,
    ) -> None:
        if not base_url.startswith(("http://", "https://")):
            raise DomainError("VLM base_url must be an http(s) endpoint")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.transport = transport or UrllibTransport()

    @classmethod
    def from_env(cls) -> VLMOCRAdapter:
        base_url = os.environ.get("LEGAL_AGENT_OCR_BASE_URL") or os.environ.get("LEGAL_AGENT_LLM_BASE_URL")
        api_key = os.environ.get("LEGAL_AGENT_OCR_API_KEY") or os.environ.get("LEGAL_AGENT_LLM_API_KEY")
        model = os.environ.get("LEGAL_AGENT_OCR_MODEL", "qwen3.8-flash")
        if not base_url or not api_key:
            raise DomainError(
                "VLM OCR needs LEGAL_AGENT_OCR_BASE_URL/LEGAL_AGENT_OCR_API_KEY "
                "(falls back to LEGAL_AGENT_LLM_*)"
            )
        return cls(base_url, api_key, model)

    @property
    def engine(self) -> str:
        return "vlm-ocr"

    def extract(self, artifact: SourceArtifact) -> OCRResult:
        """Transcribe a page image (PNG/JPEG bytes) into text."""
        if artifact.media_type not in ("image/png", "image/jpeg"):
            raise DomainError(f"VLM OCR expects an image artifact, got {artifact.media_type!r}")
        encoded = __import__("base64").b64encode(artifact.content).decode("ascii")
        payload = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": 4096,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:{artifact.media_type};base64,{encoded}"}},
                        {"type": "text", "text": PROMPT},
                    ],
                }
            ],
        }
        status, body = self.transport.post(
            f"{self.base_url}/chat/completions",
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            payload,
            self.timeout_seconds,
        )
        if not 200 <= status < 300:
            raise DomainError(f"VLM OCR request failed with status {status}")
        if not isinstance(body, dict):
            raise DomainError("VLM OCR response must be a JSON object")
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise DomainError("VLM OCR response contains no choices")
        message = choices[0].get("message")
        text = message.get("content") if isinstance(message, dict) else None
        if not isinstance(text, str) or not text.strip():
            raise DomainError("VLM OCR returned empty content (reasoning budget or provider issue)")
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`").lstrip("json\n").strip()
        return OCRResult(
            text=cleaned,
            engine=self.engine,
            engine_version=self.model,
        )


__all__ = ["PROMPT", "VLMOCRAdapter"]
