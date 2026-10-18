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
    ToolCall,
)


@dataclass(frozen=True, slots=True)
class JsonHttpResponse:
    status_code: int
    headers: Mapping[str, str]
    body: object


class LLMStreamHTTPError(Exception):
    """Non-2xx status encountered while opening an SSE stream."""

    def __init__(self, response: JsonHttpResponse) -> None:
        super().__init__(f"stream HTTP {response.status_code}")
        self.response = response


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

    def post_stream(
        self,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> Any:
        """Yield SSE ``data:`` payload strings from a streaming chat completion.

        Reads the response line by line, but also enforces an absolute deadline.
        Provider heartbeats must not keep one planner decision alive beyond the
        agent's remaining wall-clock budget.
        """
        request = urllib.request.Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={**dict(headers), "Accept": "text/event-stream"},
            method="POST",
        )
        try:
            response = urllib.request.urlopen(request, timeout=timeout_seconds)
        except urllib.error.HTTPError as exc:
            raw_body = exc.read()
            try:
                body = self._decode(raw_body)
            except LLMResponseFormatError:
                body = {}
            raise LLMStreamHTTPError(
                JsonHttpResponse(
                    exc.code,
                    dict(exc.headers.items()) if exc.headers else {},
                    body,
                )
            ) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise LLMTransportError("LLM provider connection failed") from exc

        deadline = time.monotonic() + timeout_seconds

        def _iter() -> Any:
            try:
                for raw_line in response:
                    if time.monotonic() >= deadline:
                        raise LLMTransportError(
                            "LLM provider stream exceeded its absolute deadline"
                        )
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    if data:
                        yield data
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                raise LLMTransportError("LLM provider stream timed out") from exc
            finally:
                response.close()

        return _iter()


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

        streaming = (
            provider.stream
            and provider.endpoint_style == EndpointStyle.CHAT_COMPLETIONS
            and hasattr(self.transport, "post_stream")
        )
        if streaming:
            payload = {**payload, "stream": True, "stream_options": {"include_usage": True}}
            return self._generate_streaming(provider, profile, url, headers, payload, policy)

        deadline = time.monotonic() + provider.timeout_seconds
        for attempt in range(1, policy.max_attempts + 1):
            remaining = (
                provider.timeout_seconds
                if attempt == 1
                else deadline - time.monotonic()
            )
            if remaining <= 0:
                raise LLMTransportError("LLM request exceeded its absolute deadline")
            try:
                response = self.transport.post(url, headers, payload, remaining)
            except LLMTransportError:
                if attempt == policy.max_attempts or time.monotonic() >= deadline:
                    raise
                delay = self._backoff(
                    policy.initial_backoff_seconds,
                    policy.maximum_backoff_seconds,
                    attempt,
                )
                self.sleeper(min(delay, max(0.0, deadline - time.monotonic())))
                continue

            if 200 <= response.status_code < 300:
                return self._parse_success(response, profile, provider.endpoint_style)

            error = self._provider_error(response, provider.api_key)
            if not error.retryable or attempt == policy.max_attempts:
                raise error
            delay = self._retry_after(response.headers)
            if delay is None:
                delay = self._backoff(policy.initial_backoff_seconds, policy.maximum_backoff_seconds, attempt)
            self.sleeper(
                min(delay, policy.maximum_backoff_seconds, max(0.0, deadline - time.monotonic()))
            )

        raise AssertionError("retry loop must return or raise")

    def _generate_streaming(
        self,
        provider: ProviderConfig,
        profile: ModelProfile,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        policy: Any,
    ) -> ProviderResponse:
        deadline = time.monotonic() + provider.timeout_seconds
        for attempt in range(1, policy.max_attempts + 1):
            remaining = (
                provider.timeout_seconds
                if attempt == 1
                else deadline - time.monotonic()
            )
            if remaining <= 0:
                raise LLMTransportError("LLM stream exceeded its absolute deadline")
            try:
                text, tool_calls, usage, response_id, model = self._stream_chat(
                    provider, url, headers, payload, timeout_seconds=remaining
                )
            except LLMStreamHTTPError as exc:
                error = self._provider_error(exc.response, provider.api_key)
                if (
                    not error.retryable
                    or attempt == policy.max_attempts
                    or time.monotonic() >= deadline
                ):
                    raise error
                delay = self._retry_after(exc.response.headers)
                if delay is None:
                    delay = self._backoff(
                        policy.initial_backoff_seconds,
                        policy.maximum_backoff_seconds,
                        attempt,
                    )
                self.sleeper(
                    min(
                        delay,
                        policy.maximum_backoff_seconds,
                        max(0.0, deadline - time.monotonic()),
                    )
                )
                continue
            except LLMTransportError:
                if attempt == policy.max_attempts or time.monotonic() >= deadline:
                    raise
                self.sleeper(
                    min(
                        self._backoff(
                            policy.initial_backoff_seconds,
                            policy.maximum_backoff_seconds,
                            attempt,
                        ),
                        max(0.0, deadline - time.monotonic()),
                    )
                )
                continue
            if not text.strip() and not tool_calls:
                raise LLMResponseFormatError("chat completion stream produced no text")
            return ProviderResponse(
                response_id=response_id or "stream-response",
                model=model or profile.model,
                text=text,
                usage=usage or TokenUsage(),
                tool_calls=tool_calls,
            )
        raise AssertionError("stream retry loop must return or raise")

    def _stream_chat(
        self,
        provider: ProviderConfig,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> tuple[str, tuple[ToolCall, ...], TokenUsage | None, str | None, str | None]:
        response_id: str | None = None
        model: str | None = None
        usage: TokenUsage | None = None
        text_parts: list[str] = []
        calls: dict[int, dict[str, str]] = {}
        timeout = provider.timeout_seconds if timeout_seconds is None else timeout_seconds
        for data in self.transport.post_stream(url, headers, payload, timeout):
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            if not isinstance(chunk, Mapping):
                continue
            if response_id is None and isinstance(chunk.get("id"), str):
                response_id = chunk["id"]
            if model is None and isinstance(chunk.get("model"), str):
                model = chunk["model"]
            if isinstance(chunk.get("usage"), Mapping):
                usage = self._usage(chunk["usage"], EndpointStyle.CHAT_COMPLETIONS)
            choices = chunk.get("choices")
            if not isinstance(choices, list):
                continue
            for choice in choices:
                if not isinstance(choice, Mapping):
                    continue
                delta = choice.get("delta")
                if not isinstance(delta, Mapping):
                    continue
                content = delta.get("content")
                if isinstance(content, str):
                    text_parts.append(content)
                for tool_delta in delta.get("tool_calls") or []:
                    if not isinstance(tool_delta, Mapping):
                        continue
                    index = tool_delta.get("index")
                    index = index if isinstance(index, int) else 0
                    slot = calls.setdefault(index, {"id": "", "name": "", "args": ""})
                    call_id = tool_delta.get("id")
                    if isinstance(call_id, str) and call_id:
                        slot["id"] = call_id
                    function = tool_delta.get("function")
                    if isinstance(function, Mapping):
                        name = function.get("name")
                        if isinstance(name, str) and name:
                            slot["name"] = name
                        arguments = function.get("arguments")
                        if isinstance(arguments, str):
                            slot["args"] += arguments
        tool_calls = tuple(
            ToolCall(name=slot["name"], arguments=slot["args"] or "{}", id=slot["id"])
            for _, slot in sorted(calls.items())
            if slot["name"]
        )
        return "".join(text_parts), tool_calls, usage, response_id, model

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
            payload.update(profile.extra_body)
            return payload

        payload = {
            "model": profile.model,
            "messages": [
                {"role": "system", "content": request.system_prompt},
                {"role": "user", "content": request.user_prompt},
            ],
            "max_tokens": profile.max_output_tokens,
        }
        if request.tools:
            payload["tools"] = [dict(tool) for tool in request.tools]
            payload["tool_choice"] = request.tool_choice or "auto"
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
        payload.update(profile.extra_body)
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
        tool_calls = cls._chat_tool_calls(body) if style == EndpointStyle.CHAT_COMPLETIONS else ()
        if not text.strip() and not tool_calls:
            raise LLMResponseFormatError("chat completion contains no text")
        model = body.get("model")
        usage = body.get("usage")
        return ProviderResponse(
            response_id=response_id,
            model=model if isinstance(model, str) and model.strip() else profile.model,
            text=text,
            usage=cls._usage(usage, style),
            request_id=cls._header(response.headers, "x-request-id"),
            tool_calls=tool_calls,
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
        return ""

    @staticmethod
    def _chat_tool_calls(body: Mapping[str, Any]) -> tuple[ToolCall, ...]:
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
            return ()
        message = choices[0].get("message")
        if not isinstance(message, Mapping):
            return ()
        raw = message.get("tool_calls")
        if not isinstance(raw, list):
            return ()
        calls: list[ToolCall] = []
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            function = item.get("function")
            if not isinstance(function, Mapping):
                continue
            name = function.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            arguments = function.get("arguments")
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments if arguments is not None else {})
            call_id = item.get("id")
            calls.append(
                ToolCall(
                    name=name.strip(),
                    arguments=arguments,
                    id=call_id if isinstance(call_id, str) else "",
                )
            )
        return tuple(calls)

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
