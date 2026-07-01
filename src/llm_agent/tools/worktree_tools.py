from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from llm_agent.tool_registry import ToolDefinition, ToolRegistry
from llm_agent.worktree import WorktreeManager


@dataclass(frozen=True)
class WorktreeTools:
    manager: WorktreeManager

    def list_worktrees(self) -> list[dict[str, Any]]:
        return [info.to_dict() for info in self.manager.list()]

    def diff(self, worktree_id: str) -> dict[str, Any]:
        return self.manager.diff(worktree_id)

    def apply(self, worktree_id: str) -> dict[str, Any]:
        return self.manager.apply(worktree_id)

    def remove(
        self,
        worktree_id: str,
        discard_changes: bool = False,
    ) -> dict[str, Any]:
        return self.manager.remove(
            worktree_id,
            discard_changes=discard_changes,
        )


def worktree_tool_definitions(
    manager: WorktreeManager,
) -> list[ToolDefinition]:
    tools = WorktreeTools(manager)
    worktree_id = {
        "type": "string",
        "description": "Managed worktree id returned by subagent_run.",
    }
    return [
        ToolDefinition(
            name="worktree_list",
            description="List managed isolated Subagent worktrees.",
            parameters={"type": "object", "properties": {}},
            func=tools.list_worktrees,
        ),
        ToolDefinition(
            name="worktree_diff",
            description=(
                "Inspect changed files and a bounded diff for an isolated "
                "Subagent worktree."
            ),
            parameters={
                "type": "object",
                "properties": {"worktree_id": worktree_id},
                "required": ["worktree_id"],
            },
            func=tools.diff,
        ),
        ToolDefinition(
            name="worktree_apply",
            description=(
                "Apply a reviewed isolated worktree patch to the main workspace. "
                "Refuses stale or conflicting patches."
            ),
            parameters={
                "type": "object",
                "properties": {"worktree_id": worktree_id},
                "required": ["worktree_id"],
            },
            func=tools.apply,
        ),
        ToolDefinition(
            name="worktree_remove",
            description=(
                "Remove a managed worktree. Unapplied changes are preserved "
                "unless discard_changes=true."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "worktree_id": worktree_id,
                    "discard_changes": {
                        "type": "boolean",
                        "description": (
                            "Permanently discard unapplied isolated changes."
                        ),
                    },
                },
                "required": ["worktree_id"],
            },
            func=tools.remove,
        ),
    ]


def register_tools(
    registry: ToolRegistry,
    *,
    manager: WorktreeManager,
) -> None:
    registry.register_many(worktree_tool_definitions(manager))


__all__ = [
    "WorktreeTools",
    "register_tools",
    "worktree_tool_definitions",
]
