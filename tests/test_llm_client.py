from types import SimpleNamespace
from typing import Any

import pytest

from llm_agent.llm_client import LLMClient, LLMClientError, LLMToolCall
from llm_agent.tool_registry import ToolSpec


class FakeCreate:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.kwargs: dict[str, Any] = {}

    def create(self, **kwargs: Any) -> dict[str, Any]:
        self.kwargs = kwargs
        return self.response


class FakeOpenAIClient:
    def __init__(self, response: dict[str, Any]) -> None:
        self.create = FakeCreate(response)
        self.chat = SimpleNamespace(completions=self.create)


class FakeAnthropicClient:
    def __init__(self, response: dict[str, Any]) -> None:
        self.create = FakeCreate(response)
        self.messages = SimpleNamespace(create=self.create.create)


def add_tool() -> ToolSpec:
    return {
        "name": "add",
        "description": "Add two integers.",
        "parameters": {
            "type": "object",
            "properties": {
                "a": {"type": "integer"},
                "b": {"type": "integer"},
            },
            "required": ["a", "b"],
        },
    }


def test_openai_sdk_tool_call_params_and_parse() -> None:
    sdk = FakeOpenAIClient(
        {
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "add",
                                    "arguments": '{"a": 1, "b": 2}',
                                },
                            }
                        ],
                    },
                }
            ],
            "usage": {"total_tokens": 12},
        }
    )
    client = LLMClient(provider="OpenAI", model="qwen-coder", openai_client=sdk)

    response = client.chat(
        [{"role": "user", "content": "1 + 2?"}],
        tools=[add_tool()],
        tool_choice="auto",
        extra_body={"top_k": 20},
    )

    assert sdk.create.kwargs["model"] == "qwen-coder"
    assert sdk.create.kwargs["messages"] == [{"role": "user", "content": "1 + 2?"}]
    assert sdk.create.kwargs["tool_choice"] == "auto"
    assert sdk.create.kwargs["extra_body"] == {"top_k": 20}
    assert sdk.create.kwargs["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "add",
                "description": "Add two integers.",
                "parameters": add_tool()["parameters"],
            },
        }
    ]
    assert response.tool_calls == [
        LLMToolCall(
            id="call_1",
            name="add",
            arguments={"a": 1, "b": 2},
            raw={
                "id": "call_1",
                "type": "function",
                "function": {"name": "add", "arguments": '{"a": 1, "b": 2}'},
            },
        )
    ]
    assert response.stop_reason == "tool_calls"
    assert response.usage == {"total_tokens": 12}


def test_anthropic_sdk_tool_call_params_and_parse() -> None:
    sdk = FakeAnthropicClient(
        {
            "id": "msg_1",
            "content": [
                {"type": "text", "text": "I should use a tool."},
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "add",
                    "input": {"a": 1, "b": 2},
                },
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 20, "output_tokens": 8},
        }
    )
    client = LLMClient(provider="Anthropic", model="claude-test", anthropic_client=sdk)

    response = client.chat(
        [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "1 + 2?"},
        ],
        tools=[add_tool()],
        tool_choice="required",
    )

    assert sdk.create.kwargs["model"] == "claude-test"
    assert sdk.create.kwargs["system"] == "You are helpful."
    assert sdk.create.kwargs["messages"] == [
        {"role": "user", "content": "1 + 2?"}
    ]
    assert sdk.create.kwargs["tool_choice"] == {"type": "any"}
    assert sdk.create.kwargs["tools"] == [
        {
            "name": "add",
            "description": "Add two integers.",
            "input_schema": add_tool()["parameters"],
        }
    ]
    assert response.content == "I should use a tool."
    assert response.tool_calls == [
        LLMToolCall(
            id="toolu_1",
            name="add",
            arguments={"a": 1, "b": 2},
            raw={
                "type": "tool_use",
                "id": "toolu_1",
                "name": "add",
                "input": {"a": 1, "b": 2},
            },
        )
    ]


def test_provider_specific_tool_result_messages() -> None:
    tool_call = LLMToolCall(id="call_1", name="add", arguments={"a": 1, "b": 2})

    openai_client = LLMClient(provider="openai", model="model")
    anthropic_client = LLMClient(provider="anthropic", model="model")

    assert openai_client.tool_result_message(tool_call, {"ok": True, "result": 3}) == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": '{"ok": true, "result": 3}',
    }
    assert anthropic_client.tool_result_message(tool_call, "3") == {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "call_1",
                "content": "3",
            }
        ],
    }


def test_batched_tool_result_messages_follow_provider_conventions() -> None:
    first_tool_call = LLMToolCall(id="call_1", name="add", arguments={"a": 1, "b": 2})
    second_tool_call = LLMToolCall(id="call_2", name="add", arguments={"a": 3, "b": 4})

    openai_client = LLMClient(provider="openai", model="model")
    anthropic_client = LLMClient(provider="anthropic", model="model")

    tool_results = [
        (first_tool_call, {"ok": True, "result": 3}),
        (second_tool_call, {"ok": True, "result": 7}),
    ]

    assert openai_client.tool_result_messages(tool_results) == [
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": '{"ok": true, "result": 3}',
        },
        {
            "role": "tool",
            "tool_call_id": "call_2",
            "content": '{"ok": true, "result": 7}',
        },
    ]
    assert anthropic_client.tool_result_messages(tool_results) == [
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "call_1",
                    "content": '{"ok": true, "result": 3}',
                },
                {
                    "type": "tool_result",
                    "tool_use_id": "call_2",
                    "content": '{"ok": true, "result": 7}',
                },
            ],
        }
    ]


def test_requires_model_before_sdk_request() -> None:
    client = LLMClient(
        provider="openai",
        model="",
        openai_client=FakeOpenAIClient({"choices": []}),
    )

    with pytest.raises(LLMClientError, match="model is required"):
        client.chat([{"role": "user", "content": "hello"}])


def test_llm_client_can_load_explicit_env_file(tmp_path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "OPENAI_MODEL=from-env-file\nOPENAI_API_KEY=from-env-file\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "already-set")

    client = LLMClient(provider="openai", env_file=env_file)

    assert client.model == "from-env-file"
    assert client.api_key == "already-set"


def test_llm_client_accepts_model_id_env_alias(monkeypatch) -> None:
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    monkeypatch.setenv("MODEL_ID", "claude-compatible-model")

    client = LLMClient(provider="anthropic")

    assert client.model == "claude-compatible-model"
