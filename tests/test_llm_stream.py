from __future__ import annotations

import unittest
from unittest.mock import patch

from legal_agent_core.adapters.llm_http import (
    OpenAICompatibleGateway,
    UrllibJsonTransport,
)
from legal_agent_core.llm import (
    EndpointStyle,
    GenerationRequest,
    LLMTask,
    LLMTransportError,
    ModelProfile,
    ProviderConfig,
    RetryPolicy,
)


class FakeStreamTransport:
    """Transport exposing ``post_stream`` (so the gateway takes the SSE path)."""

    def __init__(self, chunks) -> None:
        self.chunks = list(chunks)
        self.payloads = []

    def post(self, url, headers, payload, timeout_seconds):  # pragma: no cover
        raise AssertionError("streaming gateway must not call post()")

    def post_stream(self, url, headers, payload, timeout_seconds):
        self.payloads.append(payload)
        return iter(self.chunks)


def _provider() -> ProviderConfig:
    return ProviderConfig(
        "provider-1",
        "https://llm.example.test/v1",
        EndpointStyle.CHAT_COMPLETIONS,
        api_key="k",
        timeout_seconds=30,
        retry_policy=RetryPolicy(2, 0.01, 0.02),
        stream=True,
    )


def _profile() -> ModelProfile:
    return ModelProfile(
        "p",
        1,
        LLMTask.NAVIGATION,
        "provider-1",
        "model-test",
        prompt_id="p",
        prompt_version=1,
        max_output_tokens=512,
        temperature=0.0,
    )


class StreamingGatewayTests(unittest.TestCase):
    def test_assembles_text_across_delta_chunks(self) -> None:
        import json

        chunks = [
            json.dumps({"id": "c1", "model": "m", "choices": [{"delta": {"content": "سلام "}}]}),
            json.dumps({"id": "c1", "model": "m", "choices": [{"delta": {"content": "دنیا"}}]}),
            json.dumps({"id": "c1", "choices": [{"delta": {}}], "usage": {"prompt_tokens": 5, "completion_tokens": 3}}),
        ]
        transport = FakeStreamTransport(chunks)
        response = OpenAICompatibleGateway(transport).generate(
            _provider(), _profile(), GenerationRequest("sys", "user")
        )
        self.assertEqual(response.text, "سلام دنیا")
        self.assertEqual(response.usage.input_tokens, 5)
        self.assertEqual(response.usage.output_tokens, 3)
        # the request asked the provider to stream with usage
        self.assertTrue(transport.payloads[0]["stream"])
        self.assertEqual(transport.payloads[0]["stream_options"], {"include_usage": True})

    def test_assembles_streamed_tool_calls(self) -> None:
        import json

        chunks = [
            json.dumps({"id": "c2", "choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "call_1", "function": {"name": "search_provisions", "arguments": '{"query":'}}
            ]}}]}),
            json.dumps({"id": "c2", "choices": [{"delta": {"tool_calls": [
                {"index": 0, "function": {"arguments": '"مهریه"}'}}
            ]}}]}),
        ]
        response = OpenAICompatibleGateway(FakeStreamTransport(chunks)).generate(
            _provider(), _profile(), GenerationRequest("sys", "user")
        )
        self.assertEqual(len(response.tool_calls), 1)
        self.assertEqual(response.tool_calls[0].name, "search_provisions")
        self.assertEqual(response.tool_calls[0].arguments, '{"query":"مهریه"}')
        self.assertEqual(response.tool_calls[0].id, "call_1")

    def test_urllib_stream_has_absolute_deadline_despite_heartbeat_lines(self) -> None:
        class Response:
            def __iter__(self):
                return iter((b"\n", b'data: {"choices":[]}\n'))

            def close(self):
                pass

        transport = UrllibJsonTransport()
        with (
            patch(
                "legal_agent_core.adapters.llm_http.urllib.request.urlopen",
                return_value=Response(),
            ),
            patch(
                "legal_agent_core.adapters.llm_http.time.monotonic",
                side_effect=(10.0, 12.0),
            ),
        ):
            stream = transport.post_stream("https://example.test", {}, {}, 1.0)
            with self.assertRaises(LLMTransportError):
                next(stream)

    def test_urllib_stream_converts_socket_read_timeout(self) -> None:
        class Response:
            def __iter__(self):
                raise TimeoutError("socket read timed out")

            def close(self):
                pass

        transport = UrllibJsonTransport()
        with patch(
            "legal_agent_core.adapters.llm_http.urllib.request.urlopen",
            return_value=Response(),
        ):
            stream = transport.post_stream("https://example.test", {}, {}, 1.0)
            with self.assertRaises(LLMTransportError):
                next(stream)


if __name__ == "__main__":
    unittest.main()
