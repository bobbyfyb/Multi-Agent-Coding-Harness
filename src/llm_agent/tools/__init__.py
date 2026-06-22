"""Built-in tools for the minimal agent."""

from pathlib import Path

from llm_agent.tool_registry import ToolRegistry
from llm_agent.tools.basic_tools import register_tools as register_basic_tools


def register_default_tools(
    registry: ToolRegistry,
    *,
    workdir: Path | str | None = None,
) -> None:
    register_basic_tools(registry, workdir=workdir)


def build_default_registry(workdir: Path | str | None = None) -> ToolRegistry:
    registry = ToolRegistry()
    register_default_tools(registry, workdir=workdir)
    return registry


__all__ = [
    "build_default_registry",
    "register_basic_tools",
    "register_default_tools",
]
