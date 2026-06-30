import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from llm_agent.agent import Agent, AgentEvent, print_agent_event
from llm_agent.llm_client import (
    LLMClient,
    LLMClientError,
    LLMResponse,
    LLMToolCall,
)
from llm_agent.recovery import RecoveryPolicy
from llm_agent.tool_registry import ToolRegistry
from llm_agent.trace_system import TraceRecorder, trace_scope


class StatusError(RuntimeError):
    def __init__(
        self,
        message: str,
        status_code: int,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response = SimpleNamespace(
            status_code=status_code,
            headers=headers or {},
        )


class SequenceCreate:
    def __init__(self, responses: list[dict[str, Any] | Exception]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeOpenAIClient:
    def __init__(self, responses: list[dict[str, Any] | Exception]) -> None:
        self.create = SequenceCreate(responses)
        self.chat = SimpleNamespace(completions=self.create)


class RetryAwareOpenAIClient(FakeOpenAIClient):
    def __init__(self, responses: list[dict[str, Any] | Exception]) -> None:
        super().__init__(responses)
        self.max_retries: list[int] = []

    def with_options(self, *, max_retries: int) -> "RetryAwareOpenAIClient":
        self.max_retries.append(max_retries)
        return self


class FakeAnthropicClient:
    def __init__(self, responses: list[dict[str, Any] | Exception]) -> None:
        self.create = SequenceCreate(responses)
        self.messages = SimpleNamespace(create=self.create.create)


class AgentFakeLLM:
    def __init__(
        self,
        outputs: list[LLMResponse],
        *,
        recovery_policy: RecoveryPolicy,
    ) -> None:
        self.outputs = outputs
        self.recovery_policy = recovery_policy
        self.max_tokens = 1_024
        self.model = "test-model"
        self.max_token_requests: list[int | None] = []
        self.messages: list[list[dict[str, Any]]] = []

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]],
        tool_choice: str,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        self.messages.append([dict(message) for message in messages])
        self.max_token_requests.append(max_tokens)
        return self.outputs.pop(0)

    def assistant_message(self, response: LLMResponse) -> dict[str, Any]:
        return {"role": "assistant", "content": response.content}

    def tool_result_messages(
        self,
        tool_results: list[tuple[LLMToolCall, Any]],
    ) -> list[dict[str, Any]]:
        return [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_call.id,
                        "content": result,
                    }
                    for tool_call, result in tool_results
                ],
            }
        ]


def _final_response(content: str) -> dict[str, Any]:
    return {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content},
            }
        ],
        "usage": {"total_tokens": 3},
    }


def test_llm_client_retries_rate_limit_and_records_recovery(
    tmp_path: Path,
) -> None:
    sdk = FakeOpenAIClient(
        [
            StatusError(
                "rate limited",
                429,
                headers={"retry-after": "1.5"},
            ),
            _final_response("done"),
        ]
    )
    delays: list[float] = []
    client = LLMClient(
        provider="openai",
        model="primary",
        openai_client=sdk,
        recovery_policy=RecoveryPolicy(max_retries=2),
        retry_sleep=delays.append,
        retry_random=lambda: 0.0,
    )
    trace = TraceRecorder.for_run(tmp_path, run_id="retry-run")

    with trace_scope(trace, run_id="retry-run", agent_id="main"):
        response = client.chat([{"role": "user", "content": "hello"}])

    records = [
        json.loads(line)
        for line in trace.jsonl_path.read_text(encoding="utf-8").splitlines()
    ]

    assert response.content == "done"
    assert len(sdk.create.calls) == 2
    assert delays == [1.5]
    assert any(record["name"] == "recovery.retry" for record in records)
    completed = records[-1]
    assert completed["name"] == "llm.call"
    assert completed["phase"] == "completed"
    assert completed["data"]["attempts"] == 2


def test_llm_client_does_not_retry_authentication_error() -> None:
    sdk = FakeOpenAIClient([StatusError("invalid key", 401)])
    delays: list[float] = []
    client = LLMClient(
        provider="openai",
        model="primary",
        openai_client=sdk,
        recovery_policy=RecoveryPolicy(max_retries=4),
        retry_sleep=delays.append,
    )

    with pytest.raises(LLMClientError) as exc_info:
        client.chat([{"role": "user", "content": "hello"}])

    assert exc_info.value.kind == "authentication"
    assert exc_info.value.retryable is False
    assert len(sdk.create.calls) == 1
    assert delays == []


@pytest.mark.parametrize(
    ("error", "expected_kind"),
    [
        (TimeoutError("timed out"), "timeout"),
        (ConnectionError("connection reset"), "connection"),
        (StatusError("conflict", 409), "conflict"),
        (StatusError("service unavailable", 503), "server"),
    ],
)
def test_llm_client_retries_other_transient_errors(
    error: Exception,
    expected_kind: str,
    tmp_path: Path,
) -> None:
    sdk = FakeOpenAIClient([error, _final_response("done")])
    client = LLMClient(
        provider="openai",
        model="primary",
        openai_client=sdk,
        recovery_policy=RecoveryPolicy(max_retries=1),
        retry_sleep=lambda delay: None,
        retry_random=lambda: 0.0,
    )
    trace = TraceRecorder.for_run(tmp_path, run_id=f"retry-{expected_kind}")

    with trace_scope(
        trace,
        run_id=f"retry-{expected_kind}",
        agent_id="main",
    ):
        response = client.chat([{"role": "user", "content": "hello"}])

    records = [
        json.loads(line)
        for line in trace.jsonl_path.read_text(encoding="utf-8").splitlines()
    ]
    retry_record = next(
        record for record in records if record["name"] == "recovery.retry"
    )

    assert response.content == "done"
    assert retry_record["data"]["reason"] == expected_kind


def test_llm_client_stops_after_retry_budget_is_exhausted() -> None:
    sdk = FakeOpenAIClient(
        [
            StatusError("rate limited", 429),
            StatusError("rate limited", 429),
        ]
    )
    client = LLMClient(
        provider="openai",
        model="primary",
        openai_client=sdk,
        recovery_policy=RecoveryPolicy(max_retries=1),
        retry_sleep=lambda delay: None,
        retry_random=lambda: 0.0,
    )

    with pytest.raises(LLMClientError) as exc_info:
        client.chat([{"role": "user", "content": "hello"}])

    assert exc_info.value.kind == "rate_limit"
    assert len(sdk.create.calls) == 2


def test_llm_client_switches_to_fallback_after_repeated_overload() -> None:
    sdk = FakeOpenAIClient(
        [
            StatusError("overloaded", 529),
            StatusError("overloaded", 529),
            StatusError("overloaded", 529),
            _final_response("fallback worked"),
        ]
    )
    delays: list[float] = []
    client = LLMClient(
        provider="openai",
        model="primary",
        openai_client=sdk,
        recovery_policy=RecoveryPolicy(
            max_retries=3,
            fallback_model="fallback",
            fallback_after_overloads=3,
        ),
        retry_sleep=delays.append,
        retry_random=lambda: 0.0,
    )

    response = client.chat([{"role": "user", "content": "hello"}])

    assert response.content == "fallback worked"
    assert [call["model"] for call in sdk.create.calls] == [
        "primary",
        "primary",
        "primary",
        "fallback",
    ]
    assert delays == [0.5, 1.0, 2.0]


def test_llm_client_disables_official_sdk_retries() -> None:
    sdk = RetryAwareOpenAIClient([_final_response("done")])
    client = LLMClient(
        provider="openai",
        model="primary",
        openai_client=sdk,
    )

    client.chat([{"role": "user", "content": "hello"}])

    assert sdk.max_retries == [0]


def test_anthropic_uses_the_same_transport_recovery() -> None:
    sdk = FakeAnthropicClient(
        [
            StatusError("service unavailable", 503),
            {
                "content": [{"type": "text", "text": "done"}],
                "stop_reason": "end_turn",
            },
        ]
    )
    client = LLMClient(
        provider="anthropic",
        model="primary",
        anthropic_client=sdk,
        recovery_policy=RecoveryPolicy(max_retries=1),
        retry_sleep=lambda delay: None,
        retry_random=lambda: 0.0,
    )

    response = client.chat([{"role": "user", "content": "hello"}])

    assert response.content == "done"
    assert len(sdk.create.calls) == 2


def test_agent_escalates_output_limit_without_saving_first_draft() -> None:
    llm = AgentFakeLLM(
        [
            LLMResponse(
                content="discarded draft",
                tool_calls=[],
                raw={},
                stop_reason="length",
            ),
            LLMResponse(
                content="complete answer",
                tool_calls=[],
                raw={},
                stop_reason="stop",
            ),
        ],
        recovery_policy=RecoveryPolicy(escalated_max_tokens=8_192),
    )
    agent = Agent(llm=llm, tools=ToolRegistry(), context_manager="system")
    messages = agent.new_messages()
    messages.append({"role": "user", "content": "answer"})
    events: list[AgentEvent] = []

    result = agent.run(messages, on_event=events.append)

    assert result.status == "completed"
    assert result.content == "complete answer"
    assert llm.max_token_requests == [None, 8_192]
    assert all("discarded draft" not in str(message) for message in messages)
    assert any(
        event.type == "recovery" and event.data["action"] == "output_escalate"
        for event in events
    )


def test_agent_continues_and_combines_truncated_output() -> None:
    llm = AgentFakeLLM(
        [
            LLMResponse(
                content="discarded",
                tool_calls=[],
                raw={},
                stop_reason="max_tokens",
            ),
            LLMResponse(
                content="part one",
                tool_calls=[],
                raw={},
                stop_reason="max_tokens",
            ),
            LLMResponse(
                content="part two",
                tool_calls=[],
                raw={},
                stop_reason="stop",
            ),
        ],
        recovery_policy=RecoveryPolicy(
            escalated_max_tokens=8_192,
            max_continuations=2,
        ),
    )
    agent = Agent(llm=llm, tools=ToolRegistry(), context_manager="system")
    messages = agent.new_messages()
    messages.append({"role": "user", "content": "write a long answer"})

    result = agent.run(messages)

    assert result.status == "completed"
    assert result.content == "part one\npart two"
    assert llm.max_token_requests == [None, 8_192, 8_192]
    assert any(
        str(message.get("content", "")).startswith("<recovery_continuation>")
        for message in messages
    )


def test_agent_returns_incomplete_after_continuation_budget() -> None:
    llm = AgentFakeLLM(
        [
            LLMResponse("discarded", [], {}, stop_reason="length"),
            LLMResponse("part one", [], {}, stop_reason="length"),
            LLMResponse("part two", [], {}, stop_reason="length"),
        ],
        recovery_policy=RecoveryPolicy(max_continuations=1),
    )
    agent = Agent(llm=llm, tools=ToolRegistry(), context_manager="system")
    messages = agent.new_messages()
    messages.append({"role": "user", "content": "write a long answer"})

    result = agent.run(messages)

    assert result.status == "incomplete"
    assert result.content == "part one\npart two"


def test_agent_never_executes_truncated_tool_call() -> None:
    executions = 0

    def mutate() -> None:
        nonlocal executions
        executions += 1

    tool_call = LLMToolCall("call_1", "mutate", {})
    llm = AgentFakeLLM(
        [
            LLMResponse("", [tool_call], {}, stop_reason="length"),
            LLMResponse("", [tool_call], {}, stop_reason="length"),
        ],
        recovery_policy=RecoveryPolicy(max_continuations=0),
    )
    tools = ToolRegistry()
    tools.register("mutate", "Mutate state.", {}, mutate)
    agent = Agent(llm=llm, tools=tools, context_manager="system")
    messages = agent.new_messages()
    messages.append({"role": "user", "content": "mutate"})

    with pytest.raises(RuntimeError, match="truncated while producing tool calls"):
        agent.run(messages)

    assert executions == 0


def test_transport_retry_does_not_repeat_completed_tool_side_effect() -> None:
    tool_call_response = {
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
                                "name": "increment",
                                "arguments": "{}",
                            },
                        }
                    ],
                },
            }
        ]
    }
    sdk = FakeOpenAIClient(
        [
            tool_call_response,
            StatusError("rate limited", 429),
            _final_response("done"),
        ]
    )
    client = LLMClient(
        provider="openai",
        model="primary",
        openai_client=sdk,
        recovery_policy=RecoveryPolicy(max_retries=1),
        retry_sleep=lambda delay: None,
        retry_random=lambda: 0.0,
    )
    executions = 0

    def increment() -> int:
        nonlocal executions
        executions += 1
        return executions

    tools = ToolRegistry()
    tools.register("increment", "Increment once.", {}, increment)
    agent = Agent(llm=client, tools=tools, context_manager="system")
    messages = agent.new_messages()
    messages.append({"role": "user", "content": "increment"})

    result = agent.run(messages)

    assert result.status == "completed"
    assert executions == 1
    assert len(sdk.create.calls) == 3


def test_recovery_event_has_concise_terminal_output(capsys: Any) -> None:
    print_agent_event(
        AgentEvent(
            "recovery",
            1,
            {
                "action": "retry",
                "reason": "rate_limit",
                "attempt": 2,
                "max_attempts": 5,
                "delay_seconds": 0.5,
            },
        )
    )

    output = capsys.readouterr().out

    assert "[recovery retry]" in output
    assert "rate_limit 2/5, wait 0.5s" in output
