import os
import unittest

from legal_agent_core.adapters import OpenAICompatibleGateway
from legal_agent_core.llm import (
    EndpointStyle,
    GenerationRequest,
    LLMTask,
    ModelProfile,
    ProviderConfig,
    ReasoningEffort,
    RetryPolicy,
)


@unittest.skipUnless(
    os.environ.get("LEGAL_AGENT_RUN_LIVE_LLM") == "1",
    "set LEGAL_AGENT_RUN_LIVE_LLM=1 and explicit provider settings to run a billable live LLM test",
)
class LiveLLMGatewayTests(unittest.TestCase):
    def test_text_round_trip(self) -> None:
        base_url = os.environ.get("LEGAL_AGENT_LLM_BASE_URL")
        model = os.environ.get("LEGAL_AGENT_LLM_MODEL")
        if not base_url or not model:
            self.fail("LEGAL_AGENT_LLM_BASE_URL and LEGAL_AGENT_LLM_MODEL are required")
        style = EndpointStyle(os.environ.get("LEGAL_AGENT_LLM_ENDPOINT_STYLE", "responses"))
        provider = ProviderConfig(
            "live-provider",
            base_url,
            style,
            os.environ.get("LEGAL_AGENT_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY"),
            60,
            RetryPolicy(2, 1, 4),
        )
        profile = ModelProfile(
            "live-smoke",
            1,
            LLMTask.VERIFICATION,
            "live-provider",
            model,
            "live-prompt",
            1,
            32,
            reasoning_effort=ReasoningEffort.NONE if style == EndpointStyle.RESPONSES else None,
        )

        result = OpenAICompatibleGateway().generate(
            provider,
            profile,
            GenerationRequest("Return only the requested token.", "Return exactly: OK"),
        )

        self.assertTrue(result.response_id)
        self.assertTrue(result.text.strip())


if __name__ == "__main__":
    unittest.main()
