from dataclasses import dataclass
import os
from typing import Any, cast

from openai import APITimeoutError, OpenAI
from openai.types.chat import ChatCompletionMessageParam

from llm_agent.schemas import ChatMessage


class LLMClientError(RuntimeError):
    """Base error raised by the LLM client wrapper."""


class LLMTimeoutError(LLMClientError):
    """Raised when the LLM request exceeds the configured timeout."""


@dataclass
class LLMClient:
    base_url: str = ''
    model: str = ''
    api_key: str = ''
    temperature: float = 0.0
    max_tokens: int = 512
    timeout: float = 120.0
    client: Any | None = None

    def __post_init__(self) -> None:
        if self.client is None:
            self.base_url = self.base_url or os.getenv("LLM_BASE_URL")
            self.model = self.model or os.getenv("LLM_MODEL")
            self.api_key =self.api_key or os.getenv("LLM_API_KEY")
            self.client = OpenAI(
                base_url=self.base_url,
                api_key=self.api_key,
                timeout=self.timeout,
            )

    def complete(self, messages: list[ChatMessage]) -> str:
        try:
            completion = self.client.chat.completions.create(
                model=self.model,
                messages=cast(list[ChatCompletionMessageParam], messages),
                temperature=self.temperature,
                max_tokens=self.max_tokens,
                timeout=self.timeout,
            )
        except APITimeoutError as exc:
            raise LLMTimeoutError(
                f"LLM request timed out after {self.timeout} seconds."
            ) from exc

        content = completion.choices[0].message.content
        if content is None:
            raise LLMClientError("LLM response did not include message content.")

        return content
