from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from llm_agent.task_system import (
    TaskManager,
    format_task,
    format_task_list,
)
from llm_agent.tool_registry import ToolDefinition, ToolRegistry


@dataclass(frozen=True)
class TaskTools:
    manager: TaskManager

    @classmethod
    def for_workdir(
        cls,
        workdir: Path | str | None = None,
        *,
        task_list_id: str = "default",
    ) -> "TaskTools":
        return cls(TaskManager.for_workdir(workdir, task_list_id=task_list_id))

    def task_create(
        self,
        title: str,
        description: str = "",
        scope: str = "session",
        owner: str | None = None,
        blocked_by: list[str] | None = None,
        parent_id: str | None = None,
        priority: int = 100,
        notes: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        task = self.manager.create_task(
            title=title,
            description=description,
            scope=scope,
            owner=owner,
            blocked_by=blocked_by,
            parent_id=parent_id,
            priority=priority,
            notes=notes,
            metadata=metadata,
        )
        return f"Created task:\n{format_task(task)}"

    def task_get(self, task_id: str) -> str:
        return format_task(self.manager.get_task(task_id))

    def task_list(
        self,
        scope: str | None = None,
        status: str | None = None,
        owner: str | None = None,
        include_completed: bool = True,
    ) -> str:
        tasks = self.manager.list_tasks(
            scope=scope,
            status=status,
            owner=owner,
            include_completed=include_completed,
        )
        return format_task_list(tasks)

    def task_update(
        self,
        task_id: str,
        title: str | None = None,
        description: str | None = None,
        status: str | None = None,
        scope: str | None = None,
        owner: str | None = None,
        blocked_by: list[str] | None = None,
        parent_id: str | None = None,
        priority: int | None = None,
        evidence: str | None = None,
        notes: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        task = self.manager.update_task(
            task_id,
            title=title,
            description=description,
            status=status,
            scope=scope,
            owner=owner,
            blocked_by=blocked_by,
            parent_id=parent_id,
            priority=priority,
            evidence=evidence,
            notes=notes,
            metadata=metadata,
        )
        return f"Updated task:\n{format_task(task)}"

    def task_claim(self, task_id: str, owner: str = "agent") -> str:
        task = self.manager.claim_task(task_id, owner=owner)
        return f"Claimed task:\n{format_task(task)}"

    def task_complete(
        self,
        task_id: str,
        evidence: str | None = None,
        notes: str | None = None,
    ) -> str:
        task, unblocked = self.manager.complete_task(
            task_id,
            evidence=evidence,
            notes=notes,
        )
        output = [f"Completed task:\n{format_task(task)}"]
        if unblocked:
            output.append("Unblocked tasks:")
            output.append(format_task_list(unblocked))
        return "\n\n".join(output)


def task_tool_definitions(
    workdir: Path | str | None = None,
    *,
    task_list_id: str = "default",
) -> list[ToolDefinition]:
    tools = TaskTools.for_workdir(workdir, task_list_id=task_list_id)

    return [
        ToolDefinition(
            name="task_create",
            description=(
                "Create a persistent task. Use scope='session' for the current "
                "agent plan and scope='project' for cross-session or multi-agent work."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Short task title."},
                    "description": {
                        "type": "string",
                        "description": "Detailed task description.",
                    },
                    "scope": {
                        "type": "string",
                        "enum": ["session", "project"],
                        "description": "Task lifetime and collaboration scope.",
                    },
                    "owner": {"type": "string", "description": "Assigned agent."},
                    "blocked_by": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Task ids that must complete first.",
                    },
                    "parent_id": {
                        "type": "string",
                        "description": "Optional parent task id.",
                    },
                    "priority": {
                        "type": "integer",
                        "description": "Lower numbers are higher priority.",
                    },
                    "notes": {"type": "string"},
                    "metadata": {"type": "object"},
                },
                "required": ["title"],
            },
            func=tools.task_create,
        ),
        ToolDefinition(
            name="task_update",
            description="Update a persistent task by id.",
            parameters={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": [
                            "pending",
                            "in_progress",
                            "completed",
                            "blocked",
                            "cancelled",
                        ],
                    },
                    "scope": {"type": "string", "enum": ["session", "project"]},
                    "owner": {"type": "string"},
                    "blocked_by": {"type": "array", "items": {"type": "string"}},
                    "parent_id": {"type": "string"},
                    "priority": {"type": "integer"},
                    "evidence": {
                        "type": "string",
                        "description": "Verification evidence or completed work proof.",
                    },
                    "notes": {"type": "string"},
                    "metadata": {"type": "object"},
                },
                "required": ["task_id"],
            },
            func=tools.task_update,
        ),
        ToolDefinition(
            name="task_list",
            description="List persistent tasks with optional filters.",
            parameters={
                "type": "object",
                "properties": {
                    "scope": {"type": "string", "enum": ["session", "project"]},
                    "status": {
                        "type": "string",
                        "enum": [
                            "pending",
                            "in_progress",
                            "completed",
                            "blocked",
                            "cancelled",
                        ],
                    },
                    "owner": {"type": "string"},
                    "include_completed": {"type": "boolean"},
                },
            },
            func=tools.task_list,
        ),
        ToolDefinition(
            name="task_get",
            description="Get full JSON details for one persistent task.",
            parameters={
                "type": "object",
                "properties": {"task_id": {"type": "string"}},
                "required": ["task_id"],
            },
            func=tools.task_get,
        ),
        ToolDefinition(
            name="task_claim",
            description=(
                "Claim a pending task and mark it in_progress. Dependencies must "
                "already be completed."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "owner": {"type": "string"},
                },
                "required": ["task_id"],
            },
            func=tools.task_claim,
        ),
        ToolDefinition(
            name="task_complete",
            description=(
                "Complete an in-progress task. Include evidence such as tests run, "
                "files changed, or review notes."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "evidence": {"type": "string"},
                    "notes": {"type": "string"},
                },
                "required": ["task_id", "evidence"],
            },
            func=tools.task_complete,
        ),
    ]


def register_tools(
    registry: ToolRegistry,
    *,
    workdir: Path | str | None = None,
    task_list_id: str = "default",
) -> None:
    registry.register_many(task_tool_definitions(workdir=workdir, task_list_id=task_list_id))


__all__ = [
    "TaskTools",
    "register_tools",
    "task_tool_definitions",
]
