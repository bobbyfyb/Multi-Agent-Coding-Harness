from pathlib import Path
from typing import Any

from llm_agent.agent import Agent, AgentEvent, ToolExecutionContext
from llm_agent.hooks.permission_hooks import AutoApprovalProvider
from llm_agent.llm_client import LLMResponse, LLMToolCall
from llm_agent.skill_system import SkillRegistry
from llm_agent.subagent import SubagentRequest, SubagentRunner
from llm_agent.tool_registry import ToolRegistry
from llm_agent.tools.subagent_tools import register_tools as register_subagent_tools


class FakeLLM:
    def __init__(self, outputs: list[LLMResponse]) -> None:
        self.outputs = outputs
        self.messages: list[list[dict[str, Any]]] = []
        self.tools: list[list[dict[str, Any]]] = []

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]],
        tool_choice: str,
    ) -> LLMResponse:
        self.messages.append([dict(message) for message in messages])
        self.tools.append(tools)
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


def test_parent_agent_runs_synchronous_subagent_with_fresh_context(
    tmp_path: Path,
) -> None:
    (tmp_path / "notes.txt").write_text("important detail", encoding="utf-8")
    subagent_call = LLMToolCall(
        id="call_subagent",
        name="subagent_run",
        arguments={
            "task": "Read notes.txt and report its content.",
            "expected_output": "Return the exact important detail.",
            "mode": "explore",
        },
        raw={"id": "call_subagent", "name": "subagent_run"},
    )
    read_call = LLMToolCall(
        id="call_read",
        name="read_file",
        arguments={"path": "notes.txt"},
        raw={"id": "call_read", "name": "read_file"},
    )
    llm = FakeLLM(
        [
            LLMResponse(content="", tool_calls=[subagent_call], raw={}),
            LLMResponse(content="", tool_calls=[read_call], raw={}),
            LLMResponse(
                content="notes.txt contains: important detail",
                tool_calls=[],
                raw={},
            ),
            LLMResponse(content="The worker confirmed the detail.", tool_calls=[], raw={}),
        ]
    )
    runner = SubagentRunner(
        llm=llm,
        workdir=tmp_path,
        approval_provider=AutoApprovalProvider(approved=True),
    )
    registry = ToolRegistry()
    register_subagent_tools(registry, runner=runner)
    parent = Agent(
        llm=llm,
        tools=registry,
        context_manager="parent system",
        workdir=tmp_path,
        agent_id="main",
    )
    messages = parent.new_messages()
    messages.append({"role": "user", "content": "Delegate reading notes.txt."})
    events: list[AgentEvent] = []

    result = parent.run(messages, on_event=events.append, run_id="parent-run")

    assert result.status == "completed"
    assert result.content == "The worker confirmed the detail."
    assert len(llm.messages) == 4
    child_first_messages = llm.messages[1]
    assert child_first_messages[0]["role"] == "system"
    assert "worker agent temporarily delegated" in child_first_messages[0]["content"]
    assert child_first_messages[1]["role"] == "user"
    assert "Read notes.txt" in child_first_messages[1]["content"]
    assert "parent system" not in child_first_messages[0]["content"]

    child_tool_names = {tool["name"] for tool in llm.tools[1]}
    assert child_tool_names == {"bash", "read_file", "glob", "search"}
    assert "write_file" not in child_tool_names
    assert "subagent_run" not in child_tool_names
    assert "task_create" not in child_tool_names

    event_types = [event.type for event in events]
    assert "subagent_started" in event_types
    assert "subagent_completed" in event_types
    child_events = [event for event in events if event.depth == 1]
    assert child_events
    assert all(event.parent_run_id == "parent-run" for event in child_events)

    parent_tool_result = llm.messages[3][-1]["content"][0]["content"]
    assert parent_tool_result["ok"] is True
    assert parent_tool_result["result"]["status"] == "completed"
    assert (
        parent_tool_result["result"]["summary"]
        == "notes.txt contains: important detail"
    )


def test_subagent_general_mode_exposes_mutating_tools(tmp_path: Path) -> None:
    llm = FakeLLM([LLMResponse(content="done", tool_calls=[], raw={})])
    runner = SubagentRunner(
        llm=llm,
        workdir=tmp_path,
        approval_provider=AutoApprovalProvider(approved=True),
    )
    registry = ToolRegistry()
    register_subagent_tools(registry, runner=runner)
    parent = Agent(
        llm=FakeLLM(
            [
                LLMResponse(
                    content="",
                    tool_calls=[
                        LLMToolCall(
                            id="call_subagent",
                            name="subagent_run",
                            arguments={"task": "Inspect the project.", "mode": "general"},
                            raw={"id": "call_subagent", "name": "subagent_run"},
                        )
                    ],
                    raw={},
                ),
                LLMResponse(content="done", tool_calls=[], raw={}),
            ]
        ),
        tools=registry,
        context_manager="parent",
        workdir=tmp_path,
    )
    messages = parent.new_messages()
    messages.append({"role": "user", "content": "delegate"})

    result = parent.run(messages)

    assert result.status == "completed"
    child_tool_names = {tool["name"] for tool in llm.tools[0]}
    assert {"write_file", "edit_file"}.issubset(child_tool_names)


def test_subagent_runner_rejects_nested_delegation_at_depth_limit(
    tmp_path: Path,
) -> None:
    llm = FakeLLM([])
    runner = SubagentRunner(
        llm=llm,
        workdir=tmp_path,
        max_depth=1,
        approval_provider=AutoApprovalProvider(approved=True),
    )
    parent_context = ToolExecutionContext(
        run_id="child-run",
        agent_id="existing-child",
        parent_run_id="parent-run",
        depth=1,
        step=2,
        workdir=tmp_path,
    )

    result = runner.run(
        SubagentRequest(task="Try to delegate again."),
        parent_context=parent_context,
    )

    assert result.status == "failed"
    assert "depth limit" in str(result.error)
    assert llm.messages == []


def test_subagent_can_load_skills_in_its_own_context(tmp_path: Path) -> None:
    skill_dir = tmp_path / ".llm_agent" / "skills" / "debugging"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
description: Diagnose failures with controlled experiments.
---
# Debugging

Reproduce the failure before changing code.
""",
        encoding="utf-8",
    )
    skill_registry = SkillRegistry.for_workdir(tmp_path)
    skill_call = LLMToolCall(
        id="call_skill",
        name="skill_load",
        arguments={"name": "debugging"},
        raw={"id": "call_skill", "name": "skill_load"},
    )
    llm = FakeLLM(
        [
            LLMResponse(content="", tool_calls=[skill_call], raw={}),
            LLMResponse(content="debug plan ready", tool_calls=[], raw={}),
        ]
    )
    runner = SubagentRunner(
        llm=llm,
        workdir=tmp_path,
        skill_registry=skill_registry,
        approval_provider=AutoApprovalProvider(approved=True),
    )
    parent_context = ToolExecutionContext(
        run_id="parent-run",
        agent_id="main",
        parent_run_id=None,
        depth=0,
        step=1,
        workdir=tmp_path,
    )

    result = runner.run(
        SubagentRequest(
            task="Load the debugging skill and prepare a diagnosis workflow.",
            mode="explore",
        ),
        parent_context=parent_context,
    )

    assert result.status == "completed"
    child_system_prompt = llm.messages[0][0]["content"]
    assert "debugging: Diagnose failures" in child_system_prompt
    assert "Reproduce the failure before changing code." not in child_system_prompt
    child_tool_names = {tool["name"] for tool in llm.tools[0]}
    assert {"skill_load", "skill_read_resource"} <= child_tool_names
    assert "subagent_run" not in child_tool_names
    loaded_result = llm.messages[1][-1]["content"][0]["content"]
    assert "Reproduce the failure" in loaded_result["result"]["instructions"]
