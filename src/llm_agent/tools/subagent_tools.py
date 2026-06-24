from __future__ import annotations

from dataclasses import dataclass

from llm_agent.agent import ToolExecutionContext
from llm_agent.subagent import SubagentRequest, SubagentRunner
from llm_agent.tool_registry import ToolDefinition, ToolRegistry


@dataclass(frozen=True)
class SubagentTools:
    runner: SubagentRunner

    def subagent_run(
        self,
        task: str,
        expected_output: str = "",
        mode: str = "general",
        *,
        context: ToolExecutionContext,
    ) -> dict:
        request = SubagentRequest(
            task=task,
            expected_output=expected_output,
            mode=mode,
        )
        return self.runner.run(request, parent_context=context).to_dict()


def subagent_tool_definitions(
    runner: SubagentRunner,
) -> list[ToolDefinition]:
    tools = SubagentTools(runner)
    return [
        ToolDefinition(
            name="subagent_run",
            description=(
                "Delegate a self-contained, multi-step subtask to a synchronous "
                "worker agent with fresh context. Use mode='explore' for read-only "
                "investigation and mode='general' when workspace edits may be needed. "
                "The worker cannot see the parent conversation, manage parent tasks, "
                "or delegate further. Returns a structured final report."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": (
                            "Self-contained task with all context the worker needs."
                        ),
                    },
                    "expected_output": {
                        "type": "string",
                        "description": (
                            "Required report shape, evidence, or acceptance criteria."
                        ),
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["explore", "general"],
                        "description": (
                            "explore omits file mutation tools; general permits them "
                            "subject to normal permission hooks."
                        ),
                    },
                },
                "required": ["task"],
            },
            func=tools.subagent_run,
            requires_context=True,
        )
    ]


def register_tools(
    registry: ToolRegistry,
    *,
    runner: SubagentRunner,
) -> None:
    registry.register_many(subagent_tool_definitions(runner))


__all__ = [
    "SubagentTools",
    "register_tools",
    "subagent_tool_definitions",
]
