import unittest

from legal_agent_core.adapters.llm_http import JsonHttpResponse, OpenAICompatibleGateway
from legal_agent_core.errors import ConflictError, DomainError, NotFoundError
from legal_agent_core.llm import (
    EndpointStyle,
    GenerationRequest,
    LLMProviderError,
    LLMResponseFormatError,
    LLMResult,
    LLMService,
    LLMTask,
    ModelProfile,
    ModelProfileRegistry,
    PromptRegistry,
    PromptTemplate,
    ProviderConfig,
    ProviderResponse,
    ReasoningEffort,
    RetryPolicy,
    StructuredOutput,
    TokenUsage,
    legal_model_profiles,
    legal_prompt_templates,
)


class FakeTransport:
    def __init__(self, *results) -> None:
        self.results = list(results)
        self.calls = []

    def post(self, url, headers, payload, timeout_seconds):
        self.calls.append((url, headers, payload, timeout_seconds))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class RecordingGateway:
    def __init__(self) -> None:
        self.calls = []

    def generate(self, provider, profile, request):
        self.calls.append((provider, profile, request))
        return ProviderResponse("response-1", profile.model, "پاسخ", TokenUsage(10, 2, 12), "request-1")


def provider(style: EndpointStyle = EndpointStyle.RESPONSES, **overrides) -> ProviderConfig:
    values = {
        "provider_id": "provider-1",
        "base_url": "https://llm.example.test/v1",
        "endpoint_style": style,
        "api_key": "secret-key",
        "timeout_seconds": 7.5,
        "retry_policy": RetryPolicy(3, 0.1, 1.0),
    }
    values.update(overrides)
    return ProviderConfig(**values)


def profile(
    style: EndpointStyle = EndpointStyle.RESPONSES,
    task: LLMTask = LLMTask.ANSWER_COMPOSITION,
) -> ModelProfile:
    return ModelProfile(
        "answer-profile",
        2,
        task,
        "provider-1",
        "model-test",
        "answer-prompt",
        3,
        1200,
        0.2 if style == EndpointStyle.CHAT_COMPLETIONS else None,
        ReasoningEffort.MEDIUM,
    )


def request(*, structured: bool = False) -> GenerationRequest:
    output = (
        StructuredOutput(
            "legal_answer",
            {
                "type": "object",
                "properties": {"answer": {"type": "string"}},
                "required": ["answer"],
                "additionalProperties": False,
            },
        )
        if structured
        else None
    )
    return GenerationRequest(
        "فقط بر اساس شواهد پاسخ بده.",
        "پرسش حقوقی",
        "safe-user-hash",
        {"episode_id": "episode-1"},
        output,
    )


class ModelProfileTests(unittest.TestCase):
    def test_prompt_rendering_is_versioned_and_requires_variables(self) -> None:
        prompt = PromptTemplate(
            "prompt-1",
            2,
            LLMTask.NAVIGATION,
            "سامانه $domain",
            "پرسش: $question",
            ("domain", "question"),
            "agent/4",
        )

        self.assertEqual(prompt.render({"domain": "حقوقی", "question": "حکم چیست؟"}), ("سامانه حقوقی", "پرسش: حکم چیست؟"))
        with self.assertRaisesRegex(DomainError, "question"):
            prompt.render({"domain": "حقوقی"})

    def test_registries_reject_conflicts_and_wrong_task_activation(self) -> None:
        prompts = PromptRegistry(
            PromptTemplate("p", 1, LLMTask.NAVIGATION, "system", "$question", ("question",), "agent/1")
        )
        with self.assertRaises(ConflictError):
            prompts.register(
                PromptTemplate("p", 1, LLMTask.NAVIGATION, "different", "$question", ("question",), "agent/1")
            )

        profiles = ModelProfileRegistry(profile(task=LLMTask.ANSWER_COMPOSITION))
        with self.assertRaises(DomainError):
            profiles.activate(LLMTask.NAVIGATION, "answer-profile", 2)
        with self.assertRaises(NotFoundError):
            profiles.active_for(LLMTask.ANSWER_COMPOSITION)

    def test_default_factory_creates_distinct_profiles_for_all_tasks(self) -> None:
        profiles = legal_model_profiles("provider-1", "configured-model")

        self.assertEqual({item.task for item in profiles}, set(LLMTask))
        self.assertEqual(len({item.profile_id for item in profiles}), 4)
        self.assertEqual({item.model for item in profiles}, {"configured-model"})

    def test_provider_secret_is_not_exposed_by_repr(self) -> None:
        self.assertNotIn("secret-key", repr(provider()))


class OpenAICompatibleGatewayTests(unittest.TestCase):
    def test_builds_responses_payload_and_parses_nested_output(self) -> None:
        transport = FakeTransport(
            JsonHttpResponse(
                200,
                {"X-Request-ID": "request-123"},
                {
                    "id": "resp-123",
                    "model": "model-resolved",
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": '{"answer":"بله"}'}],
                        }
                    ],
                    "usage": {"input_tokens": 12, "output_tokens": 4, "total_tokens": 16},
                },
            )
        )
        gateway = OpenAICompatibleGateway(transport, sleeper=lambda _: None)

        result = gateway.generate(provider(), profile(), request(structured=True))

        url, headers, payload, timeout = transport.calls[0]
        self.assertEqual(url, "https://llm.example.test/v1/responses")
        self.assertEqual(headers["Authorization"], "Bearer secret-key")
        self.assertEqual(timeout, 7.5)
        self.assertEqual(payload["instructions"], "فقط بر اساس شواهد پاسخ بده.")
        self.assertEqual(payload["input"], "پرسش حقوقی")
        self.assertEqual(payload["reasoning"], {"effort": "medium"})
        self.assertFalse(payload["store"])
        self.assertEqual(payload["safety_identifier"], "safe-user-hash")
        self.assertEqual(payload["text"]["format"]["type"], "json_schema")
        self.assertEqual(result.text, '{"answer":"بله"}')
        self.assertEqual(result.request_id, "request-123")
        self.assertEqual(result.usage, TokenUsage(12, 4, 16))

    def test_supports_local_chat_completions_without_api_key(self) -> None:
        transport = FakeTransport(
            JsonHttpResponse(
                200,
                {},
                {
                    "id": "chat-1",
                    "model": "local-model",
                    "choices": [{"message": {"content": "پاسخ محلی"}}],
                    "usage": {"prompt_tokens": 8, "completion_tokens": 3, "total_tokens": 11},
                },
            )
        )
        gateway = OpenAICompatibleGateway(transport)
        local_provider = provider(
            EndpointStyle.CHAT_COMPLETIONS,
            base_url="http://127.0.0.1:11434/v1",
            api_key=None,
        )

        result = gateway.generate(
            local_provider,
            profile(EndpointStyle.CHAT_COMPLETIONS),
            request(structured=True),
        )

        url, headers, payload, _ = transport.calls[0]
        self.assertEqual(url, "http://127.0.0.1:11434/v1/chat/completions")
        self.assertNotIn("Authorization", headers)
        self.assertEqual(payload["messages"][0]["role"], "system")
        self.assertEqual(payload["max_tokens"], 1200)
        self.assertEqual(payload["response_format"]["type"], "json_schema")
        self.assertEqual(result.text, "پاسخ محلی")

    def test_retries_rate_limit_using_retry_after(self) -> None:
        transport = FakeTransport(
            JsonHttpResponse(
                429,
                {"Retry-After": "0.4", "x-request-id": "request-rate"},
                {"error": {"message": "rate limited", "code": "rate_limit"}},
            ),
            JsonHttpResponse(
                200,
                {},
                {"id": "resp-ok", "model": "model-test", "output_text": "ok", "usage": {}},
            ),
        )
        sleeps = []

        result = OpenAICompatibleGateway(transport, sleeper=sleeps.append).generate(
            provider(),
            profile(),
            request(),
        )

        self.assertEqual(result.text, "ok")
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(sleeps, [0.4])

    def test_does_not_retry_bad_request_and_preserves_safe_error_metadata(self) -> None:
        transport = FakeTransport(
            JsonHttpResponse(
                400,
                {"x-request-id": "request-bad"},
                {"error": {"message": "invalid schema secret-key", "code": "invalid_request"}},
            )
        )

        with self.assertRaises(LLMProviderError) as caught:
            OpenAICompatibleGateway(transport).generate(provider(), profile(), request())

        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(caught.exception.provider_code, "invalid_request")
        self.assertEqual(caught.exception.request_id, "request-bad")
        self.assertFalse(caught.exception.retryable)
        self.assertNotIn("secret-key", str(caught.exception))
        self.assertIn("[REDACTED]", str(caught.exception))

    def test_rejects_success_response_without_text(self) -> None:
        transport = FakeTransport(JsonHttpResponse(200, {}, {"id": "resp-empty", "output": []}))

        with self.assertRaises(LLMResponseFormatError):
            OpenAICompatibleGateway(transport).generate(provider(), profile(), request())


class LLMServiceTests(unittest.TestCase):
    def test_routes_active_profile_and_returns_full_audit_identity(self) -> None:
        prompt = PromptTemplate(
            "answer-prompt",
            3,
            LLMTask.ANSWER_COMPOSITION,
            "سامانه پاسخ حقوقی",
            "پرسش: $question\nشواهد: $evidence",
            ("question", "evidence"),
            "agent/7",
        )
        profiles = ModelProfileRegistry(profile())
        profiles.activate(LLMTask.ANSWER_COMPOSITION, "answer-profile", 2)
        gateway = RecordingGateway()
        service = LLMService(gateway, {"provider-1": provider()}, profiles, PromptRegistry(prompt))

        result = service.generate(
            LLMTask.ANSWER_COMPOSITION,
            {"question": "حکم چیست؟", "evidence": "ماده ۵"},
            metadata={"episode_id": "episode-1"},
        )

        self.assertIsInstance(result, LLMResult)
        self.assertEqual(result.model_profile_id, "answer-profile")
        self.assertEqual(result.model_profile_version, 2)
        self.assertEqual(result.prompt_id, "answer-prompt")
        self.assertEqual(result.prompt_version, 3)
        self.assertEqual(result.agent_version, "agent/7")
        self.assertEqual(gateway.calls[0][2].user_prompt, "پرسش: حکم چیست؟\nشواهد: ماده ۵")

    def test_default_prompts_cover_every_task(self) -> None:
        self.assertEqual({item.task for item in legal_prompt_templates()}, set(LLMTask))


if __name__ == "__main__":
    unittest.main()
