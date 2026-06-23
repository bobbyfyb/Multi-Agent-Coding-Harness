from pathlib import Path
from typing import Any

from llm_agent.agent import Agent, AgentEvent
from llm_agent.hooks import HookContext, HookManager, HookResult
from llm_agent.hooks.permission_hooks import AutoApprovalProvider, PermissionHook
from llm_agent.llm_client import LLMResponse, LLMToolCall
from llm_agent.tool_registry import ToolRegistry


class FakeLLM:
    def __init__(self, outputs: list[LLMResponse]) -> None:
        self.outputs = outputs
        self.messages: list[list[dict[str, Any]]] = []

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]],
        tool_choice: str,
    ) -> LLMResponse:
        self.messages.append([dict(message) for message in messages])
        return self.outputs.pop(0)

    def assistant_message(self, response: LLMResponse) -> dict[str, Any]:
        return {
            "role": "assistant",
            "content": response.content,
            "tool_calls": [tool_call.raw for tool_call in response.tool_calls],
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


def test_permission_hook_denies_paths_outside_workspace(tmp_path: Path) -> None:
    hook = PermissionHook(
        workdir=tmp_path,
        approval_provider=AutoApprovalProvider(approved=True),
    )
    tool_call = LLMToolCall(
        id="call_1",
        name="write_file",
        arguments={"path": "../outside.txt", "content": "no"},
    )

    result = hook(tool_call, HookContext(messages=[], workdir=tmp_path))

    assert result is not None
    assert result.denied
    assert result.reason == "Path escapes workspace: ../outside.txt"


def test_permission_hook_uses_approval_provider_for_file_mutations(
    tmp_path: Path,
) -> None:
    tool_call = LLMToolCall(
        id="call_1",
        name="write_file",
        arguments={"path": "notes.txt", "content": "ok"},
    )
    context = HookContext(messages=[], workdir=tmp_path)

    allowed = PermissionHook(
        workdir=tmp_path,
        approval_provider=AutoApprovalProvider(approved=True),
    )(tool_call, context)
    denied = PermissionHook(
        workdir=tmp_path,
        approval_provider=AutoApprovalProvider(approved=False),
    )(tool_call, context)

    assert allowed is not None
    assert allowed.action == "allow"
    assert denied is not None
    assert denied.denied
    assert "denied by user" in str(denied.reason)


def test_agent_uses_pre_tool_hook_to_deny_tool_execution(tmp_path: Path) -> None:
    executed = False

    def write_file(path: str, content: str) -> str:
        nonlocal executed
        executed = True
        return f"{path}: {content}"

    registry = ToolRegistry()
    registry.register(
        name="write_file",
        description="Write a file.",
        parameters={},
        func=write_file,
    )
    manager = HookManager()
    manager.register_hook(
        "PreToolUse",
        PermissionHook(
            workdir=tmp_path,
            approval_provider=AutoApprovalProvider(approved=False),
        ),
    )
    tool_call = LLMToolCall(
        id="call_1",
        name="write_file",
        arguments={"path": "notes.txt", "content": "no"},
        raw={"id": "call_1", "name": "write_file"},
    )
    llm = FakeLLM(
        [
            LLMResponse(content="", tool_calls=[tool_call], raw={}),
            LLMResponse(content="done", tool_calls=[], raw={}),
        ]
    )
    agent = Agent(
        llm=llm,
        tools=registry,
        context_builder="system",
        hooks=manager,
        workdir=tmp_path,
    )
    messages = agent.new_messages()
    messages.append({"role": "user", "content": "write"})
    events: list[AgentEvent] = []

    agent.run(messages, on_event=events.append)

    assert executed is False
    assert [event.type for event in events] == [
        "step",
        "tool_call",
        "permission_denied",
        "tool_result",
        "step",
        "final",
    ]
    tool_result_message = llm.messages[1][-1]
    assert tool_result_message["content"][0]["content"]["ok"] is False
    assert "Permission denied" in tool_result_message["content"][0]["content"]["error"]


def test_agent_uses_post_tool_hook_to_replace_tool_result(tmp_path: Path) -> None:
    registry = ToolRegistry()
    registry.register(
        name="read_file",
        description="Read a file.",
        parameters={},
        func=lambda path: "full output",
    )
    manager = HookManager()

    def replace_output(
        tool_call: LLMToolCall,
        tool_result: dict[str, Any],
        context: HookContext,
    ) -> HookResult:
        return HookResult.replace_result({"ok": True, "result": "trimmed"})

    manager.register_hook("PostToolUse", replace_output)
    tool_call = LLMToolCall(
        id="call_1",
        name="read_file",
        arguments={"path": "notes.txt"},
        raw={"id": "call_1", "name": "read_file"},
    )
    llm = FakeLLM(
        [
            LLMResponse(content="", tool_calls=[tool_call], raw={}),
            LLMResponse(content="done", tool_calls=[], raw={}),
        ]
    )
    agent = Agent(
        llm=llm,
        tools=registry,
        context_builder="system",
        hooks=manager,
        workdir=tmp_path,
    )
    messages = agent.new_messages()
    messages.append({"role": "user", "content": "read"})

    agent.run(messages)

    tool_result_message = llm.messages[1][-1]
    assert tool_result_message["content"][0]["content"] == {
        "ok": True,
        "result": "trimmed",
    }
