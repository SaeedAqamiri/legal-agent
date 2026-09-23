from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from string import Template
from typing import Any, Protocol

from .canonical import require_text
from .errors import ConflictError, DomainError, NotFoundError


class LLMTask(StrEnum):
    NAVIGATION = "navigation"
    EXTRACTION = "extraction"
    VERIFICATION = "verification"
    ANSWER_COMPOSITION = "answer_composition"


class EndpointStyle(StrEnum):
    RESPONSES = "responses"
    CHAT_COMPLETIONS = "chat_completions"


class ReasoningEffort(StrEnum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 3
    initial_backoff_seconds: float = 0.25
    maximum_backoff_seconds: float = 4.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise DomainError("max_attempts must be positive")
        if self.initial_backoff_seconds < 0:
            raise DomainError("initial_backoff_seconds cannot be negative")
        if self.maximum_backoff_seconds < self.initial_backoff_seconds:
            raise DomainError("maximum_backoff_seconds cannot be smaller than initial backoff")


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    provider_id: str
    base_url: str
    endpoint_style: EndpointStyle
    api_key: str | None = field(default=None, repr=False)
    timeout_seconds: float = 30.0
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    extra_headers: Mapping[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        require_text(self.provider_id, "provider_id")
        require_text(self.base_url, "base_url")
        if not self.base_url.startswith(("https://", "http://")):
            raise DomainError("base_url must use http or https")
        if self.timeout_seconds <= 0:
            raise DomainError("timeout_seconds must be positive")
        forbidden = {"authorization", "content-type"} & {key.casefold() for key in self.extra_headers}
        if forbidden:
            raise DomainError("authorization and content-type headers are managed by the gateway")


@dataclass(frozen=True, slots=True)
class PromptTemplate:
    prompt_id: str
    version: int
    task: LLMTask
    system_template: str
    user_template: str
    required_variables: tuple[str, ...]
    agent_version: str

    def __post_init__(self) -> None:
        require_text(self.prompt_id, "prompt_id")
        require_text(self.system_template, "system_template")
        require_text(self.user_template, "user_template")
        require_text(self.agent_version, "agent_version")
        if self.version < 1:
            raise DomainError("prompt version must be positive")
        if len(set(self.required_variables)) != len(self.required_variables):
            raise DomainError("required prompt variables must be unique")

    def render(self, variables: Mapping[str, object]) -> tuple[str, str]:
        missing = tuple(name for name in self.required_variables if name not in variables)
        if missing:
            raise DomainError(f"missing prompt variables: {', '.join(missing)}")
        string_variables = {key: str(value) for key, value in variables.items()}
        try:
            return (
                Template(self.system_template).substitute(string_variables),
                Template(self.user_template).substitute(string_variables),
            )
        except KeyError as exc:
            raise DomainError(f"missing prompt variable: {exc.args[0]}") from exc


@dataclass(frozen=True, slots=True)
class ModelProfile:
    profile_id: str
    version: int
    task: LLMTask
    provider_id: str
    model: str
    prompt_id: str
    prompt_version: int
    max_output_tokens: int = 2048
    temperature: float | None = None
    reasoning_effort: ReasoningEffort | None = None
    store: bool = False
    #: Provider-specific body fields merged verbatim into the request payload
    #: (e.g. {"thinking": {"type": "disabled"}} for z.ai/GLM reasoning models).
    extra_body: Mapping[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        for field_name in ("profile_id", "provider_id", "model", "prompt_id"):
            require_text(getattr(self, field_name), field_name)
        if self.version < 1 or self.prompt_version < 1:
            raise DomainError("profile and prompt versions must be positive")
        if self.max_output_tokens < 1:
            raise DomainError("max_output_tokens must be positive")
        if self.temperature is not None and not 0 <= self.temperature <= 2:
            raise DomainError("temperature must be between 0 and 2")
        if not all(isinstance(key, str) for key in self.extra_body):
            raise DomainError("extra_body keys must be strings")


@dataclass(frozen=True, slots=True)
class StructuredOutput:
    name: str
    schema: Mapping[str, Any]
    strict: bool = True

    def __post_init__(self) -> None:
        require_text(self.name, "structured output name")
        if self.schema.get("type") != "object":
            raise DomainError("structured output schema root must be an object")


@dataclass(frozen=True, slots=True)
class GenerationRequest:
    system_prompt: str
    user_prompt: str
    safety_identifier: str | None = None
    metadata: Mapping[str, str] = field(default_factory=dict)
    structured_output: StructuredOutput | None = None

    def __post_init__(self) -> None:
        require_text(self.system_prompt, "system_prompt")
        require_text(self.user_prompt, "user_prompt")
        if len(self.metadata) > 16:
            raise DomainError("generation metadata cannot contain more than 16 entries")


@dataclass(frozen=True, slots=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0

    def __post_init__(self) -> None:
        if min(self.input_tokens, self.output_tokens, self.total_tokens) < 0:
            raise DomainError("token usage cannot be negative")


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    response_id: str
    model: str
    text: str
    usage: TokenUsage
    request_id: str | None = None

    def __post_init__(self) -> None:
        require_text(self.response_id, "response_id")
        require_text(self.model, "model")
        require_text(self.text, "response text")


@dataclass(frozen=True, slots=True)
class LLMResult:
    response: ProviderResponse
    provider_id: str
    model_profile_id: str
    model_profile_version: int
    prompt_id: str
    prompt_version: int
    agent_version: str
    task: LLMTask


class LLMGateway(Protocol):
    def generate(
        self,
        provider: ProviderConfig,
        profile: ModelProfile,
        request: GenerationRequest,
    ) -> ProviderResponse: ...


class LLMError(DomainError):
    """Base error for provider-safe LLM failures."""


class LLMTransportError(LLMError):
    """Network or timeout failure before a valid provider response."""


class LLMProviderError(LLMError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None,
        provider_code: str | None = None,
        request_id: str | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.provider_code = provider_code
        self.request_id = request_id
        self.retryable = retryable


class LLMResponseFormatError(LLMError):
    """Provider returned a successful but unusable response."""


class PromptRegistry:
    def __init__(self, *templates: PromptTemplate) -> None:
        self._templates: dict[tuple[str, int], PromptTemplate] = {}
        for template in templates:
            self.register(template)

    def register(self, template: PromptTemplate) -> None:
        key = (template.prompt_id, template.version)
        existing = self._templates.get(key)
        if existing is not None and existing != template:
            raise ConflictError(f"prompt {template.prompt_id!r} version {template.version} already exists")
        self._templates[key] = template

    def get(self, prompt_id: str, version: int) -> PromptTemplate:
        try:
            return self._templates[(prompt_id, version)]
        except KeyError as exc:
            raise NotFoundError(f"prompt {prompt_id!r} version {version} not found") from exc


class ModelProfileRegistry:
    def __init__(self, *profiles: ModelProfile) -> None:
        self._profiles: dict[tuple[str, int], ModelProfile] = {}
        self._active: dict[LLMTask, tuple[str, int]] = {}
        for profile in profiles:
            self.register(profile)

    def register(self, profile: ModelProfile, *, activate: bool = False) -> None:
        key = (profile.profile_id, profile.version)
        existing = self._profiles.get(key)
        if existing is not None and existing != profile:
            raise ConflictError(f"model profile {profile.profile_id!r} version {profile.version} already exists")
        self._profiles[key] = profile
        if activate:
            self._active[profile.task] = key

    def activate(self, task: LLMTask, profile_id: str, version: int) -> None:
        profile = self.get(profile_id, version)
        if profile.task != task:
            raise DomainError("cannot activate a model profile for another task")
        self._active[task] = (profile_id, version)

    def get(self, profile_id: str, version: int) -> ModelProfile:
        try:
            return self._profiles[(profile_id, version)]
        except KeyError as exc:
            raise NotFoundError(f"model profile {profile_id!r} version {version} not found") from exc

    def active_for(self, task: LLMTask) -> ModelProfile:
        try:
            key = self._active[task]
        except KeyError as exc:
            raise NotFoundError(f"no active model profile for task {task.value!r}") from exc
        return self._profiles[key]


class LLMService:
    def __init__(
        self,
        gateway: LLMGateway,
        providers: Mapping[str, ProviderConfig],
        profiles: ModelProfileRegistry,
        prompts: PromptRegistry,
    ) -> None:
        self.gateway = gateway
        self.providers = dict(providers)
        self.profiles = profiles
        self.prompts = prompts

    def generate(
        self,
        task: LLMTask,
        variables: Mapping[str, object],
        *,
        safety_identifier: str | None = None,
        metadata: Mapping[str, str] | None = None,
        structured_output: StructuredOutput | None = None,
    ) -> LLMResult:
        profile = self.profiles.active_for(task)
        try:
            provider = self.providers[profile.provider_id]
        except KeyError as exc:
            raise NotFoundError(f"provider {profile.provider_id!r} not configured") from exc
        prompt = self.prompts.get(profile.prompt_id, profile.prompt_version)
        if prompt.task != task:
            raise DomainError("model profile points to a prompt for another task")
        system_prompt, user_prompt = prompt.render(variables)
        response = self.gateway.generate(
            provider,
            profile,
            GenerationRequest(
                system_prompt,
                user_prompt,
                safety_identifier,
                metadata or {},
                structured_output,
            ),
        )
        return LLMResult(
            response=response,
            provider_id=provider.provider_id,
            model_profile_id=profile.profile_id,
            model_profile_version=profile.version,
            prompt_id=prompt.prompt_id,
            prompt_version=prompt.version,
            agent_version=prompt.agent_version,
            task=task,
        )


def legal_prompt_templates(agent_version: str = "legal-agent-core/0.1") -> tuple[PromptTemplate, ...]:
    shared = (
        "شما بخشی از یک دستیار پژوهش حقوقی هستید. فقط بر پایه شواهد ارائه‌شده کار کنید؛ "
        "ابهام، تعارض و نبود منبع را صریح گزارش دهید."
    )
    return (
        PromptTemplate(
            "legal-navigation",
            1,
            LLMTask.NAVIGATION,
            shared,
            "پرسش: $question\nخطاهای راستی‌آزمایی: $issues\nگام پژوهشی بعدی را پیشنهاد کن.",
            ("question", "issues"),
            agent_version,
        ),
        PromptTemplate(
            "legal-extraction",
            1,
            LLMTask.EXTRACTION,
            shared,
            "از متن زیر فقط داده‌های خواسته‌شده را استخراج کن:\n$source_text",
            ("source_text",),
            agent_version,
        ),
        PromptTemplate(
            "legal-verification",
            1,
            LLMTask.VERIFICATION,
            shared,
            "ادعاها را با شواهد مقایسه کن.\nادعاها: $claims\nشواهد: $evidence",
            ("claims", "evidence"),
            agent_version,
        ),
        PromptTemplate(
            "legal-answer",
            1,
            LLMTask.ANSWER_COMPOSITION,
            shared,
            "برای پرسش زیر پاسخ فارسی مستند بساز.\nپرسش: $question\nشواهد تأییدشده: $evidence",
            ("question", "evidence"),
            agent_version,
        ),
    )


def legal_model_profiles(provider_id: str, model: str) -> tuple[ModelProfile, ...]:
    require_text(provider_id, "provider_id")
    require_text(model, "model")
    return tuple(
        ModelProfile(
            profile_id=f"legal-{task.value}",
            version=1,
            task=task,
            provider_id=provider_id,
            model=model,
            prompt_id=prompt_id,
            prompt_version=1,
            max_output_tokens=max_tokens,
            reasoning_effort=effort,
        )
        for task, prompt_id, max_tokens, effort in (
            (LLMTask.NAVIGATION, "legal-navigation", 1024, ReasoningEffort.LOW),
            (LLMTask.EXTRACTION, "legal-extraction", 2048, ReasoningEffort.LOW),
            (LLMTask.VERIFICATION, "legal-verification", 2048, ReasoningEffort.MEDIUM),
            (LLMTask.ANSWER_COMPOSITION, "legal-answer", 4096, ReasoningEffort.MEDIUM),
        )
    )
