"""Built-in tools for the minimal agent."""

from pathlib import Path
from typing import TYPE_CHECKING

from llm_agent.tool_registry import ToolRegistry
from llm_agent.tools.artifact_tools import register_tools as register_artifact_tools
from llm_agent.tools.basic_tools import register_tools as register_basic_tools
from llm_agent.tools.memory_tools import register_tools as register_memory_tools
from llm_agent.tools.search_tools import register_tools as register_search_tools
from llm_agent.tools.skill_tools import register_tools as register_skill_tools
from llm_agent.tools.task_tools import register_tools as register_task_tools
from llm_agent.tools.verification_tools import (
    register_tools as register_verification_tools,
)

if TYPE_CHECKING:
    from llm_agent.artifact_system import ArtifactManager
    from llm_agent.background_jobs import BackgroundJobManager
    from llm_agent.mcp_config import MCPToolScope
    from llm_agent.mcp_system import MCPManager
    from llm_agent.memory_system import MemoryManager
    from llm_agent.skill_system import SkillRegistry
    from llm_agent.subagent import SubagentRunner
    from llm_agent.worktree import WorktreeManager


def register_subagent_tools(
    registry: ToolRegistry,
    *,
    runner: "SubagentRunner",
) -> None:
    from llm_agent.tools.subagent_tools import register_tools

    register_tools(registry, runner=runner)


def register_background_tools(
    registry: ToolRegistry,
    *,
    manager: "BackgroundJobManager",
) -> None:
    from llm_agent.tools.background_tools import register_tools

    register_tools(registry, manager=manager)


def register_worktree_tools(
    registry: ToolRegistry,
    *,
    manager: "WorktreeManager",
) -> None:
    from llm_agent.tools.worktree_tools import register_tools

    register_tools(registry, manager=manager)


def register_mcp_tools(
    registry: ToolRegistry,
    *,
    manager: "MCPManager",
    scope: "MCPToolScope",
) -> None:
    from llm_agent.tools.mcp_tools import register_tools

    register_tools(registry, manager=manager, scope=scope)


def register_default_tools(
    registry: ToolRegistry,
    *,
    workdir: Path | str | None = None,
    task_workdir: Path | str | None = None,
    subagent_runner: "SubagentRunner | None" = None,
    skill_registry: "SkillRegistry | None" = None,
    memory_manager: "MemoryManager | None" = None,
    artifact_manager: "ArtifactManager | None" = None,
    background_manager: "BackgroundJobManager | None" = None,
    worktree_manager: "WorktreeManager | None" = None,
    mcp_manager: "MCPManager | None" = None,
    mcp_scope: "MCPToolScope" = "main",
) -> None:
    register_basic_tools(
        registry,
        workdir=workdir,
        background_manager=background_manager,
    )
    if background_manager is not None:
        register_background_tools(registry, manager=background_manager)
    if worktree_manager is not None:
        register_worktree_tools(registry, manager=worktree_manager)
    register_search_tools(registry, workdir=workdir)
    register_verification_tools(registry, workdir=workdir)
    register_task_tools(
        registry,
        workdir=workdir if task_workdir is None else task_workdir,
    )
    register_artifact_tools(
        registry,
        manager=artifact_manager,
        workdir=workdir,
    )
    if skill_registry is not None:
        register_skill_tools(registry, skill_registry=skill_registry)
    if memory_manager is not None:
        register_memory_tools(
            registry,
            memory_manager=memory_manager,
        )
    if subagent_runner is not None:
        register_subagent_tools(registry, runner=subagent_runner)
    if mcp_manager is not None:
        register_mcp_tools(registry, manager=mcp_manager, scope=mcp_scope)


def build_default_registry(
    workdir: Path | str | None = None,
    *,
    task_workdir: Path | str | None = None,
    subagent_runner: "SubagentRunner | None" = None,
    skill_registry: "SkillRegistry | None" = None,
    memory_manager: "MemoryManager | None" = None,
    artifact_manager: "ArtifactManager | None" = None,
    background_manager: "BackgroundJobManager | None" = None,
    worktree_manager: "WorktreeManager | None" = None,
    mcp_manager: "MCPManager | None" = None,
    mcp_scope: "MCPToolScope" = "main",
) -> ToolRegistry:
    registry = ToolRegistry()
    register_default_tools(
        registry,
        workdir=workdir,
        task_workdir=task_workdir,
        subagent_runner=subagent_runner,
        skill_registry=skill_registry,
        memory_manager=memory_manager,
        artifact_manager=artifact_manager,
        background_manager=background_manager,
        worktree_manager=worktree_manager,
        mcp_manager=mcp_manager,
        mcp_scope=mcp_scope,
    )
    return registry


__all__ = [
    "build_default_registry",
    "register_artifact_tools",
    "register_basic_tools",
    "register_background_tools",
    "register_memory_tools",
    "register_mcp_tools",
    "register_search_tools",
    "register_skill_tools",
    "register_subagent_tools",
    "register_task_tools",
    "register_verification_tools",
    "register_worktree_tools",
    "register_default_tools",
]
