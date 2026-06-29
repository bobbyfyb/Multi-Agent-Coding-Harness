from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from llm_agent.memory_system import (
    MemoryManager,
    format_memory,
    format_memory_list,
)
from llm_agent.tool_registry import ToolDefinition, ToolRegistry


@dataclass(frozen=True)
class MemoryTools:
    manager: MemoryManager

    def memory_remember(
        self,
        name: str,
        description: str,
        body: str,
        type: str = "project",
        pinned: bool = False,
        memory_id: str | None = None,
        *,
        context: Any,
    ) -> str:
        memory = self.manager.remember(
            name=name,
            memory_type=type,
            description=description,
            body=body,
            pinned=pinned,
            memory_id=memory_id,
            source="tool",
            source_run_id=str(getattr(context, "run_id", "")).strip() or None,
        )
        return f"Stored memory:\n{format_memory(memory)}"

    def memory_search(self, query: str, limit: int = 10) -> str:
        return format_memory_list(self.manager.search(query, limit=limit))

    def memory_get(self, memory_id: str) -> str:
        return format_memory(self.manager.get(memory_id))

    def memory_forget(self, memory_id: str) -> str:
        memory = self.manager.forget(memory_id)
        return f"Forgot memory:\n{format_memory(memory)}"


def memory_tool_definitions(manager: MemoryManager) -> list[ToolDefinition]:
    tools = MemoryTools(manager)
    return [
        ToolDefinition(
            name="memory_remember",
            description=(
                "Create or update durable cross-session memory. Use only for an "
                "explicit user preference, lasting feedback, stable project fact, "
                "or durable reference. Do not store task progress, logs, guesses, "
                "or secrets."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Short human-readable memory name.",
                    },
                    "description": {
                        "type": "string",
                        "description": "One-line description used for retrieval.",
                    },
                    "body": {
                        "type": "string",
                        "description": "Concrete durable information in Markdown.",
                    },
                    "type": {
                        "type": "string",
                        "enum": ["user", "feedback", "project", "reference"],
                    },
                    "pinned": {
                        "type": "boolean",
                        "description": (
                            "Always recall within a small budget. Reserve this for "
                            "broadly applicable preferences or constraints."
                        ),
                    },
                    "memory_id": {
                        "type": "string",
                        "description": "Existing memory id to update.",
                    },
                },
                "required": ["name", "description", "body"],
            },
            func=tools.memory_remember,
            requires_context=True,
        ),
        ToolDefinition(
            name="memory_search",
            description=(
                "Search persistent memory by topic. Use when the user asks what is "
                "remembered or when automatically recalled memories are insufficient."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 50,
                    },
                },
                "required": ["query"],
            },
            func=tools.memory_search,
        ),
        ToolDefinition(
            name="memory_get",
            description="Read one persistent memory by id.",
            parameters={
                "type": "object",
                "properties": {"memory_id": {"type": "string"}},
                "required": ["memory_id"],
            },
            func=tools.memory_get,
        ),
        ToolDefinition(
            name="memory_forget",
            description=(
                "Permanently delete one memory. Use only when the user explicitly "
                "asks to forget it."
            ),
            parameters={
                "type": "object",
                "properties": {"memory_id": {"type": "string"}},
                "required": ["memory_id"],
            },
            func=tools.memory_forget,
        ),
    ]


def register_tools(
    registry: ToolRegistry,
    *,
    memory_manager: MemoryManager,
) -> None:
    registry.register_many(memory_tool_definitions(memory_manager))


__all__ = [
    "MemoryTools",
    "memory_tool_definitions",
    "register_tools",
]
