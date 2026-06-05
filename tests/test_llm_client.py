from types import SimpleNamespace

import httpx
import pytest
from openai import APITimeoutError

from llm_agent.llm_client import LLMClient, LLMTimeoutError
from llm_agent.schemas import ChatMessage


class FakeCompletions:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        if self.error is not None:
            raise self.error

        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="hello"),
                )
            ]
        )


class FakeOpenAIClient:
    def __init__(self, error: Exception | None = None) -> None:
        self.completions = FakeCompletions(error=error)
        self.chat = SimpleNamespace(completions=self.completions)


def test_llm_client_uses_openai_chat_completions() -> None:
    client = FakeOpenAIClient()
    llm = LLMClient(
        client=client,
        model="qwen-coder",
        temperature=0.3,
        max_tokens=128,
    )
    messages: list[ChatMessage] = [{"role": "user", "content": "hi"}]

    assert llm.complete(messages) == "hello"
    assert client.completions.kwargs == {
        "model": "qwen-coder",
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": 128,
        "timeout": 120.0,
    }


def test_llm_client_raises_timeout_error() -> None:
    request = httpx.Request("POST", "http://localhost:8000/v1/chat/completions")
    client = FakeOpenAIClient(error=APITimeoutError(request=request))
    llm = LLMClient(client=client, timeout=0.1)
    messages: list[ChatMessage] = [{"role": "user", "content": "hi"}]

    with pytest.raises(LLMTimeoutError, match="timed out after 0.1 seconds") as exc:
        llm.complete(messages)

    assert isinstance(exc.value.__cause__, APITimeoutError)
