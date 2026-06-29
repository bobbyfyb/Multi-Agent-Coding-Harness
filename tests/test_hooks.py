from pathlib import Path
from typing import Any

from llm_agent.agent import Agent, AgentEvent
from llm_agent.hooks import (
    HookContext,
    HookManager,
    HookResult,
    build_default_hook_manager,
)
from llm_agent.hooks.permission_hooks import AutoApprovalProvider, PermissionHook
from llm_agent.hooks.task_hooks import TaskPlanningHook
from llm_agent.llm_client import LLMResponse, LLMToolCall
from llm_agent.task_intent import TaskIntentClassifier
from llm_agent.task_system import TaskManager
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


class StubIntentClassifier:
    def __init__(self, decision: bool) -> None:
        self.decision = decision
        self.prompts: list[str] = []

    def requires_plan(self, text: str) -> bool:
        self.prompts.append(text)
        return self.decision


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
        context_manager="system",
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
        context_manager="system",
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


def test_task_planning_hook_reminds_for_complex_tasks(tmp_path: Path) -> None:
    hook = TaskPlanningHook(workdir=tmp_path)
    context = HookContext(
        messages=[{"role": "user", "content": "Implement a trace recorder and tests."}],
        workdir=tmp_path,
    )

    result = hook(context)

    assert result is not None
    messages = result.data["messages"]
    assert len(messages) == 1
    assert messages[0].startswith("<task_reminder>")
    assert "task_create" in messages[0]


def test_task_planning_hook_uses_classifier_and_skips_internal_messages(
    tmp_path: Path,
) -> None:
    classifier = StubIntentClassifier(decision=True)
    hook = TaskPlanningHook(
        workdir=tmp_path,
        intent_classifier=classifier,
    )
    context = HookContext(
        messages=[
            {"role": "user", "content": "Refactor auth and add tests."},
            {
                "role": "user",
                "content": "<task_reminder>\ninternal reminder\n</task_reminder>",
            },
        ],
        workdir=tmp_path,
    )

    result = hook(context)

    assert result is not None
    assert classifier.prompts == ["Refactor auth and add tests."]
    assert result.data["messages"][0].startswith("<task_reminder>")


def test_task_planning_hook_does_not_remind_when_classifier_returns_false(
    tmp_path: Path,
) -> None:
    classifier = StubIntentClassifier(decision=False)
    hook = TaskPlanningHook(
        workdir=tmp_path,
        intent_classifier=classifier,
    )
    context = HookContext(
        messages=[{"role": "user", "content": "Explain Python dataclasses."}],
        workdir=tmp_path,
    )

    result = hook(context)

    assert result is None
    assert classifier.prompts == ["Explain Python dataclasses."]


def test_task_planning_hook_deduplicates_per_prompt_not_entire_session(
    tmp_path: Path,
) -> None:
    classifier = StubIntentClassifier(decision=True)
    hook = TaskPlanningHook(
        workdir=tmp_path,
        intent_classifier=classifier,
    )
    first_context = HookContext(
        messages=[{"role": "user", "content": "Implement tracing."}],
        workdir=tmp_path,
    )

    first = hook(first_context)
    repeated = hook(first_context)
    second = hook(
        HookContext(
            messages=[
                *first_context.messages,
                {"role": "user", "content": "Implement context compression."},
            ],
            workdir=tmp_path,
        )
    )

    assert first is not None
    assert repeated is None
    assert second is not None


def test_default_hook_manager_builds_llm_intent_classifier(tmp_path: Path) -> None:
    llm = FakeLLM([])

    manager = build_default_hook_manager(workdir=tmp_path, llm=llm)

    planning_hook = manager.hooks["BeforeLLM"][0]
    assert isinstance(planning_hook, TaskPlanningHook)
    assert isinstance(planning_hook.intent_classifier, TaskIntentClassifier)
    assert planning_hook.intent_classifier.llm is llm


def test_task_planning_hook_reinjects_task_state_after_compaction(
    tmp_path: Path,
) -> None:
    TaskManager.for_workdir(tmp_path).create_task("Preserve this task")
    hook = TaskPlanningHook(
        workdir=tmp_path,
        remind_for_complex_tasks=False,
    )
    messages = [{"role": "user", "content": "Continue."}]

    first = hook(HookContext(messages=messages, workdir=tmp_path))
    repeated = hook(HookContext(messages=messages, workdir=tmp_path))
    after_compact = hook(
        HookContext(
            messages=messages,
            workdir=tmp_path,
            metadata={"context_compacted": True},
        )
    )

    assert first is not None
    assert repeated is None
    assert after_compact is not None
    assert after_compact.data["messages"][0].startswith("<current_tasks>")


def test_agent_injects_before_llm_hook_messages(tmp_path: Path) -> None:
    registry = ToolRegistry()
    manager = HookManager()
    manager.register_hook("BeforeLLM", TaskPlanningHook(workdir=tmp_path))
    llm = FakeLLM([LLMResponse(content="done", tool_calls=[], raw={})])
    agent = Agent(
        llm=llm,
        tools=registry,
        context_manager="system",
        hooks=manager,
        workdir=tmp_path,
    )
    messages = agent.new_messages()
    messages.append({"role": "user", "content": "Implement a task system."})
    events: list[AgentEvent] = []

    agent.run(messages, on_event=events.append)

    assert llm.messages[0][-1]["role"] == "user"
    assert llm.messages[0][-1]["content"].startswith("<task_reminder>")
    assert [event.type for event in events[:2]] == ["task_reminder", "step"]
