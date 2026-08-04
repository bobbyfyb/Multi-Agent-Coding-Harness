from pathlib import Path
from typing import Any

from llm_agent.agent import Agent, AgentEvent, print_agent_event
from llm_agent.context_manager import ContextManager
from llm_agent.hooks import HookContext, HookManager
from llm_agent.llm_client import (
    BATCHED_TOOL_INPUTS_KEY,
    LLMContextLengthError,
    LLMResponse,
    LLMToolCall,
)
from llm_agent.tool_registry import ToolRegistry


class FakeLLM:
    def __init__(self, outputs: list[LLMResponse | Exception]) -> None:
        self.outputs = outputs
        self.messages: list[list[dict[str, Any]]] = []
        self.tools: list[list[dict[str, Any]] | None] = []
        self.tool_choices: list[str | None] = []

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        self.messages.append([dict(message) for message in messages])
        self.tools.append(tools)
        self.tool_choices.append(tool_choice)
        output = self.outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        return output

    def assistant_message(self, response: LLMResponse) -> dict[str, Any]:
        return {
            "role": "assistant",
            "content": response.content,
            "tool_calls": [tool_call.raw for tool_call in response.tool_calls],
        }

    def tool_result_message(
        self,
        tool_call: LLMToolCall,
        result: Any,
    ) -> dict[str, Any]:
        return {
            "role": "tool",
            "tool_call_id": tool_call.id,
            "content": result,
        }

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


def test_agent_batches_native_tool_results_then_final_answer() -> None:
    registry = ToolRegistry()
    registry.register(
        name="add",
        description="Add two numbers.",
        parameters={},
        func=lambda a, b: a + b,
    )
    first_tool_call = LLMToolCall(
        id="call_1",
        name="add",
        arguments={"a": 1, "b": 2},
        raw={"id": "call_1", "name": "add"},
    )
    second_tool_call = LLMToolCall(
        id="call_2",
        name="add",
        arguments={"a": 3, "b": 4},
        raw={"id": "call_2", "name": "add"},
    )
    llm = FakeLLM(
        [
            LLMResponse(
                content="",
                tool_calls=[first_tool_call, second_tool_call],
                raw={
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    first_tool_call.raw,
                                    second_tool_call.raw,
                                ]
                            }
                        }
                    ]
                },
            ),
            LLMResponse(
                content="results are 3 and 7",
                tool_calls=[],
                raw={"choices": [{"message": {"content": "results are 3 and 7"}}]},
            ),
        ]
    )
    agent = Agent(llm=llm, tools=registry, context_manager="system")
    messages = agent.new_messages()
    messages.append({"role": "user", "content": "calculate"})

    result = agent.run(messages)

    assert result.status == "completed"
    assert result.content == "results are 3 and 7"
    assert result.steps == 2
    assert result.tool_calls == 2
    assert len(llm.messages) == 2
    assert llm.tool_choices == ["auto", "auto"]
    assert llm.tools[0] == registry.tool_specs()

    second_call_messages = llm.messages[1]
    assert second_call_messages[-2] == {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": "call_1", "name": "add"},
            {"id": "call_2", "name": "add"},
        ],
    }
    assert second_call_messages[-1] == {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "call_1",
                "content": {"ok": True, "result": 3},
            },
            {
                "type": "tool_result",
                "tool_use_id": "call_2",
                "content": {"ok": True, "result": 7},
            },
        ],
    }
    assert messages[-1] == {"role": "assistant", "content": "results are 3 and 7"}


def test_agent_executes_batched_artifact_create_arguments() -> None:
    created: list[dict[str, Any]] = []
    registry = ToolRegistry()
    registry.register(
        name="artifact_create",
        description="Create artifact.",
        parameters={},
        func=lambda **kwargs: created.append(kwargs) or f"created {kwargs['kind']}",
    )
    tool_call = LLMToolCall(
        id="call_batch",
        name="artifact_create",
        arguments={
            BATCHED_TOOL_INPUTS_KEY: [
                {"kind": "prd", "title": "PRD", "content": "Plan"},
                {"kind": "task_spec", "title": "Task", "content": "Build"},
            ]
        },
        raw={"id": "call_batch", "name": "artifact_create"},
    )
    llm = FakeLLM(
        [
            LLMResponse(content="", tool_calls=[tool_call], raw={}),
            LLMResponse(content="done", tool_calls=[], raw={}),
        ]
    )
    agent = Agent(llm=llm, tools=registry, context_manager="system")
    messages = agent.new_messages()
    messages.append({"role": "user", "content": "create artifacts"})

    result = agent.run(messages)

    assert result.status == "completed"
    assert [item["kind"] for item in created] == ["prd", "task_spec"]
    tool_result = llm.messages[1][-1]["content"][0]["content"]
    assert tool_result["ok"] is True
    assert tool_result["batched"] is True
    assert tool_result["count"] == 2


def test_agent_emits_key_step_events() -> None:
    registry = ToolRegistry()
    registry.register(
        name="add",
        description="Add two numbers.",
        parameters={},
        func=lambda a, b: a + b,
    )
    tool_call = LLMToolCall(
        id="call_1",
        name="add",
        arguments={"a": 1, "b": 2},
        raw={"id": "call_1", "name": "add"},
    )
    llm = FakeLLM(
        [
            LLMResponse(content="", tool_calls=[tool_call], raw={}),
            LLMResponse(content="done", tool_calls=[], raw={}),
        ]
    )
    agent = Agent(llm=llm, tools=registry, context_manager="system")
    messages = agent.new_messages()
    messages.append({"role": "user", "content": "calculate"})
    events: list[AgentEvent] = []

    agent.run(messages, on_event=events.append)

    assert [event.type for event in events] == [
        "step",
        "tool_call",
        "tool_result",
        "step",
        "final",
    ]
    assert events[0].step == 1
    assert events[1].data == {
        "id": "call_1",
        "name": "add",
        "arguments": {"a": 1, "b": 2},
    }
    assert events[2].data == {
        "id": "call_1",
        "name": "add",
        "result": {"ok": True, "result": 3},
    }
    assert events[-1].step == 2
    assert events[-1].data == {"content": "done"}


def test_agent_emits_progress_before_tool_calls() -> None:
    registry = ToolRegistry()
    registry.register(
        name="add",
        description="Add two numbers.",
        parameters={},
        func=lambda a, b: a + b,
    )
    tool_call = LLMToolCall(
        id="call_1",
        name="add",
        arguments={"a": 1, "b": 2},
        raw={"id": "call_1", "name": "add"},
    )
    llm = FakeLLM(
        [
            LLMResponse(
                content="  I will calculate the result first.  ",
                tool_calls=[tool_call],
                raw={},
            ),
            LLMResponse(content="done", tool_calls=[], raw={}),
        ]
    )
    agent = Agent(llm=llm, tools=registry, context_manager="system")
    messages = agent.new_messages()
    messages.append({"role": "user", "content": "calculate"})
    events: list[AgentEvent] = []

    agent.run(messages, on_event=events.append)

    assert [event.type for event in events] == [
        "step",
        "progress",
        "tool_call",
        "tool_result",
        "step",
        "final",
    ]
    assert events[1].data == {"content": "I will calculate the result first."}


def test_print_agent_event_outputs_human_readable_trace(capsys: Any) -> None:
    print_agent_event(AgentEvent("step", 1, {"message": "calling llm"}))
    print_agent_event(
        AgentEvent("progress", 1, {"content": "I will inspect the inputs first."})
    )
    print_agent_event(
        AgentEvent("tool_call", 1, {"name": "add", "arguments": {"a": 1, "b": 2}})
    )
    print_agent_event(
        AgentEvent(
            "tool_result",
            1,
            {"name": "add", "result": {"ok": True, "result": 3}},
        )
    )
    print_agent_event(
        AgentEvent(
            "tool_result",
            1,
            {
                "name": "run_tests",
                "result": {
                    "ok": True,
                    "result": {"outcome": "failed", "exit_code": 1},
                },
            },
        )
    )
    print_agent_event(AgentEvent("final", 2, {"content": "done"}))

    output = capsys.readouterr().out

    assert "[step 1] calling llm" in output
    assert "\033[35m[progress]" in output
    assert "I will inspect the inputs first." in output
    assert "[tool call] add" in output
    assert '{"a": 1, "b": 2}' in output
    assert "[tool result] add ->" in output
    assert '{"ok": true, "result": 3}' in output
    assert "\033[31m[tool result] run_tests ->" in output
    assert "[final]" in output
    assert "done" in output


def test_agent_returns_direct_final_answer_without_tools() -> None:
    registry = ToolRegistry()
    llm = FakeLLM(
        [
            LLMResponse(
                content="done",
                tool_calls=[],
                raw={"choices": [{"message": {"content": "done"}}]},
            )
        ]
    )
    agent = Agent(llm=llm, tools=registry, context_manager="system")
    messages = agent.new_messages()
    messages.append({"role": "user", "content": "answer directly"})

    result = agent.run(messages)

    assert result.status == "completed"
    assert result.content == "done"
    assert len(llm.messages) == 1
    assert messages == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "answer directly"},
        {"role": "assistant", "content": "done"},
    ]


def test_agent_reuses_caller_managed_history() -> None:
    registry = ToolRegistry()
    llm = FakeLLM(
        [
            LLMResponse(
                content="你好，小明",
                tool_calls=[],
                raw={"choices": [{"message": {"content": "你好，小明"}}]},
            ),
            LLMResponse(
                content="你叫小明",
                tool_calls=[],
                raw={"choices": [{"message": {"content": "你叫小明"}}]},
            ),
        ]
    )
    agent = Agent(llm=llm, tools=registry, context_manager="system")
    messages = agent.new_messages()

    messages.append({"role": "user", "content": "我叫小明"})
    first_result = agent.run(messages)
    assert first_result.status == "completed"

    messages.append({"role": "user", "content": "我叫什么？"})
    second_result = agent.run(messages)
    assert second_result.status == "completed"

    assert llm.messages[1] == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "我叫小明"},
        {"role": "assistant", "content": "你好，小明"},
        {"role": "user", "content": "我叫什么？"},
    ]


def test_agent_returns_structured_result_when_max_steps_are_exhausted() -> None:
    registry = ToolRegistry()
    registry.register(
        name="missing",
        description="Always unavailable.",
        parameters={},
        func=lambda: None,
    )
    tool_call = LLMToolCall(id="call_1", name="missing", arguments={})
    llm = FakeLLM(
        [
            LLMResponse(content="", tool_calls=[tool_call], raw={}),
            LLMResponse(content="", tool_calls=[tool_call], raw={}),
        ]
    )
    agent = Agent(llm=llm, tools=registry, context_manager="system", max_steps=2)
    messages = agent.new_messages()
    messages.append({"role": "user", "content": "loop forever"})

    result = agent.run(messages)

    assert result.status == "max_steps"
    assert result.steps == 2
    assert result.tool_calls == 2


def test_agent_reactively_compacts_and_retries_context_length_error(
    tmp_path: Path,
) -> None:
    llm = FakeLLM(
        [
            LLMContextLengthError("prompt_too_long"),
            LLMResponse(content="Preserved current goal.", tool_calls=[], raw={}),
            LLMResponse(content="done after compact", tool_calls=[], raw={}),
        ]
    )
    manager = ContextManager(
        llm=llm,
        workdir=tmp_path,
        max_context_tokens=10_000,
    )
    hooks = HookManager()
    compacted_flags: list[bool] = []

    def record_context_state(context: HookContext) -> None:
        compacted_flags.append(bool(context.metadata.get("context_compacted")))
        return None

    hooks.register_hook("BeforeLLM", record_context_state)
    agent = Agent(
        llm=llm,
        tools=ToolRegistry(),
        context_manager=manager,
        hooks=hooks,
        workdir=tmp_path,
    )
    messages = agent.new_messages()
    messages.append({"role": "user", "content": "Continue the coding task."})
    events: list[AgentEvent] = []

    result = agent.run(messages, on_event=events.append, run_id="run-reactive")

    assert result.status == "completed"
    assert result.content == "done after compact"
    assert any(event.type == "context_compacted" for event in events)
    assert any(
        message.get("role") == "user"
        and str(message.get("content", "")).startswith("<conversation_summary")
        for message in messages
    )
    assert len(llm.messages) == 3
    assert compacted_flags == [False, True]
