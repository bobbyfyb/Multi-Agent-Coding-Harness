from __future__ import annotations

from typing import Any

from llm_agent.mcp_config import MCPToolScope
from llm_agent.mcp_system import MCPManager, MCPToolBinding
from llm_agent.tool_registry import ToolDefinition, ToolRegistry


def register_tools(
    registry: ToolRegistry,
    *,
    manager: MCPManager,
    scope: MCPToolScope,
) -> None:
    registry.register_many(
        [_to_definition(manager, binding) for binding in manager.tool_bindings(scope)]
    )


def _to_definition(
    manager: MCPManager,
    binding: MCPToolBinding,
) -> ToolDefinition:
    def call(*, context: Any, **arguments: Any) -> dict[str, Any]:
        return manager.call(
            binding.public_name,
            arguments,
            workdir=context.workdir,
        )

    return ToolDefinition(
        name=binding.public_name,
        description=binding.description,
        parameters=binding.parameters,
        func=call,
        requires_context=True,
    )


__all__ = ["register_tools"]
