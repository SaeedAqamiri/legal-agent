from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from ..llm import (
    EndpointStyle,
    GenerationRequest,
    LLMProviderError,
    LLMResponseFormatError,
    LLMTransportError,
    ModelProfile,
    ProviderConfig,
    ProviderResponse,
    TokenUsage,
)


@dataclass(frozen=True, slots=True)
class JsonHttpResponse:
    status_code: int
    headers: Mapping[str, str]
    body: object


class JsonHttpTransport(Protocol):
    def post(
        self,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> JsonHttpResponse: ...


class UrllibJsonTransport:
    """Small dependency-free HTTP transport suitable for OpenAI-compatible endpoints."""

    def post(
        self,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> JsonHttpResponse:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=dict(headers),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                return JsonHttpResponse(
                    response.status,
                    dict(response.headers.items()),
                    self._decode(response.read()),
                )
        except urllib.error.HTTPError as exc:
            raw_body = exc.read()
            try:
                body = self._decode(raw_body)
            except LLMResponseFormatError:
                body = {}
            return JsonHttpResponse(
                exc.code,
                dict(exc.headers.items()) if exc.headers else {},
                body,
            )
        except (urllib.error.URLError, TimeoutError) as exc:
            raise LLMTransportError("LLM provider connection failed") from exc

    @staticmethod
    def _decode(raw: bytes) -> object:
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LLMResponseFormatError("LLM provider returned invalid JSON") from exc


class OpenAICompatibleGateway:
    RETRYABLE_STATUS_CODES = frozenset({408, 409, 429, 500, 502, 503, 504})

    def __init__(
        self,
        transport: JsonHttpTransport | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.transport = transport or UrllibJsonTransport()
        self.sleeper = sleeper

    def generate(
        self,
        provider: ProviderConfig,
        profile: ModelProfile,
        request: GenerationRequest,
    ) -> ProviderResponse:
        if provider.provider_id != profile.provider_id:
            raise LLMProviderError(
                "model profile provider does not match gateway configuration",
                status_code=None,
            )
        url = self._endpoint_url(provider)
        headers = self._headers(provider)
        payload = self._payload(provider.endpoint_style, profile, request)
        policy = provider.retry_policy

        for attempt in range(1, policy.max_attempts + 1):
            try:
                response = self.transport.post(url, headers, payload, provider.timeout_seconds)
            except LLMTransportError:
                if attempt == policy.max_attempts:
                    raise
                self.sleeper(self._backoff(policy.initial_backoff_seconds, policy.maximum_backoff_seconds, attempt))
                continue

            if 200 <= response.status_code < 300:
                return self._parse_success(response, profile, provider.endpoint_style)

            error = self._provider_error(response, provider.api_key)
            if not error.retryable or attempt == policy.max_attempts:
                raise error
            delay = self._retry_after(response.headers)
            if delay is None:
                delay = self._backoff(policy.initial_backoff_seconds, policy.maximum_backoff_seconds, attempt)
            self.sleeper(min(delay, policy.maximum_backoff_seconds))

        raise AssertionError("retry loop must return or raise")

    @staticmethod
    def _endpoint_url(provider: ProviderConfig) -> str:
        suffix = "/responses" if provider.endpoint_style == EndpointStyle.RESPONSES else "/chat/completions"
        return f"{provider.base_url.rstrip('/')}{suffix}"

    @staticmethod
    def _headers(provider: ProviderConfig) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            **provider.extra_headers,
        }
        if provider.api_key:
            headers["Authorization"] = f"Bearer {provider.api_key}"
        return headers

    @classmethod
    def _payload(
        cls,
        style: EndpointStyle,
        profile: ModelProfile,
        request: GenerationRequest,
    ) -> dict[str, Any]:
        if style == EndpointStyle.RESPONSES:
            payload: dict[str, Any] = {
                "model": profile.model,
                "instructions": request.system_prompt,
                "input": request.user_prompt,
                "max_output_tokens": profile.max_output_tokens,
                "store": profile.store,
            }
            if profile.reasoning_effort is not None:
                payload["reasoning"] = {"effort": profile.reasoning_effort.value}
            if profile.temperature is not None:
                payload["temperature"] = profile.temperature
            if request.safety_identifier:
                payload["safety_identifier"] = request.safety_identifier
            if request.metadata:
                payload["metadata"] = dict(request.metadata)
            if request.structured_output:
                payload["text"] = {
                    "format": {
                        "type": "json_schema",
                        "name": request.structured_output.name,
                        "schema": dict(request.structured_output.schema),
                        "strict": request.structured_output.strict,
                    }
                }
            return payload

        payload = {
            "model": profile.model,
            "messages": [
                {"role": "system", "content": request.system_prompt},
                {"role": "user", "content": request.user_prompt},
            ],
            "max_tokens": profile.max_output_tokens,
        }
        if profile.temperature is not None:
            payload["temperature"] = profile.temperature
        if request.structured_output:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": request.structured_output.name,
                    "schema": dict(request.structured_output.schema),
                    "strict": request.structured_output.strict,
                },
            }
        return payload

    @classmethod
    def _parse_success(
        cls,
        response: JsonHttpResponse,
        profile: ModelProfile,
        style: EndpointStyle,
    ) -> ProviderResponse:
        if not isinstance(response.body, Mapping):
            raise LLMResponseFormatError("LLM provider response must be a JSON object")
        body = response.body
        response_id = body.get("id")
        if not isinstance(response_id, str) or not response_id.strip():
            raise LLMResponseFormatError("LLM provider response is missing id")
        text = cls._responses_text(body) if style == EndpointStyle.RESPONSES else cls._chat_text(body)
        model = body.get("model")
        usage = body.get("usage")
        return ProviderResponse(
            response_id=response_id,
            model=model if isinstance(model, str) and model.strip() else profile.model,
            text=text,
            usage=cls._usage(usage, style),
            request_id=cls._header(response.headers, "x-request-id"),
        )

    @staticmethod
    def _responses_text(body: Mapping[str, Any]) -> str:
        direct = body.get("output_text")
        if isinstance(direct, str) and direct.strip():
            return direct
        text_parts: list[str] = []
        output = body.get("output")
        if isinstance(output, list):
            for item in output:
                if not isinstance(item, Mapping) or item.get("type") != "message":
                    continue
                content = item.get("content")
                if not isinstance(content, list):
                    continue
                for part in content:
                    if isinstance(part, Mapping) and part.get("type") == "output_text":
                        text = part.get("text")
                        if isinstance(text, str):
                            text_parts.append(text)
        text = "".join(text_parts)
        if not text.strip():
            raise LLMResponseFormatError("Responses API result contains no output text")
        return text

    @staticmethod
    def _chat_text(body: Mapping[str, Any]) -> str:
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
            raise LLMResponseFormatError("chat completion contains no choices")
        message = choices[0].get("message")
        if not isinstance(message, Mapping):
            raise LLMResponseFormatError("chat completion contains no message")
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content
        if isinstance(content, list):
            parts = [
                part.get("text")
                for part in content
                if isinstance(part, Mapping) and isinstance(part.get("text"), str)
            ]
            text = "".join(parts)
            if text.strip():
                return text
        raise LLMResponseFormatError("chat completion contains no text")

    @staticmethod
    def _usage(value: object, style: EndpointStyle) -> TokenUsage:
        if not isinstance(value, Mapping):
            return TokenUsage()
        if style == EndpointStyle.RESPONSES:
            input_tokens = OpenAICompatibleGateway._non_negative_int(value.get("input_tokens"))
            output_tokens = OpenAICompatibleGateway._non_negative_int(value.get("output_tokens"))
        else:
            input_tokens = OpenAICompatibleGateway._non_negative_int(value.get("prompt_tokens"))
            output_tokens = OpenAICompatibleGateway._non_negative_int(value.get("completion_tokens"))
        total = OpenAICompatibleGateway._non_negative_int(value.get("total_tokens"))
        return TokenUsage(input_tokens, output_tokens, total or input_tokens + output_tokens)

    @staticmethod
    def _non_negative_int(value: object) -> int:
        return value if isinstance(value, int) and value >= 0 else 0

    @classmethod
    def _provider_error(cls, response: JsonHttpResponse, api_key: str | None) -> LLMProviderError:
        message = "LLM provider rejected the request"
        provider_code = None
        if isinstance(response.body, Mapping):
            error = response.body.get("error")
            if isinstance(error, Mapping):
                raw_message = error.get("message")
                raw_code = error.get("code") or error.get("type")
                if isinstance(raw_message, str) and raw_message.strip():
                    message = raw_message[:500]
                if isinstance(raw_code, str):
                    provider_code = raw_code
        if api_key:
            message = message.replace(api_key, "[REDACTED]")
        return LLMProviderError(
            message,
            status_code=response.status_code,
            provider_code=provider_code,
            request_id=cls._header(response.headers, "x-request-id"),
            retryable=response.status_code in cls.RETRYABLE_STATUS_CODES,
        )

    @staticmethod
    def _header(headers: Mapping[str, str], name: str) -> str | None:
        expected = name.casefold()
        return next((value for key, value in headers.items() if key.casefold() == expected), None)

    @classmethod
    def _retry_after(cls, headers: Mapping[str, str]) -> float | None:
        value = cls._header(headers, "retry-after")
        if value is None:
            return None
        try:
            result = float(value)
        except ValueError:
            return None
        return max(0.0, result)

    @staticmethod
    def _backoff(initial: float, maximum: float, attempt: int) -> float:
        return min(maximum, initial * (2 ** (attempt - 1)))
