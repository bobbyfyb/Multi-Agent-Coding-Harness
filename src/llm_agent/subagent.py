from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from llm_agent.agent import (
    Agent,
    AgentEvent,
    AgentRunResult,
    ToolExecutionContext,
)
from llm_agent.context_manager import ContextManager, PromptSection
from llm_agent.hooks import HookManager
from llm_agent.hooks.permission_hooks import (
    ApprovalProvider,
    CliApprovalProvider,
    PermissionHook,
)
from llm_agent.llm_client import LLMClient
from llm_agent.memory_system import MemoryManager, format_memory_context
from llm_agent.skill_system import SkillRegistry, build_skill_catalog_section
from llm_agent.tool_registry import ToolRegistry
from llm_agent.tools.basic_tools import register_tools as register_basic_tools
from llm_agent.tools.search_tools import register_tools as register_search_tools
from llm_agent.tools.skill_tools import register_tools as register_skill_tools
from llm_agent.tools.verification_tools import (
    register_tools as register_verification_tools,
)
from llm_agent.trace_system import record_agent_event
from llm_agent.worktree import WorktreeInfo, WorktreeManager


SubagentMode = Literal["explore", "general"]
SubagentIsolation = Literal["shared", "worktree"]
SubagentStatus = Literal["completed", "incomplete", "max_steps", "failed"]

SUBAGENT_BASE_INSTRUCTIONS = """
You are a worker agent temporarily delegated by a parent agent.

Complete only the self-contained task in the user message. You do not have the
parent conversation, so do not assume information that was not explicitly
provided. Inspect the workspace and use tools to verify conclusions.

Do not delegate work to another agent. Keep changes within the requested scope.
In the final response, report:
- the result or changes made;
- concrete evidence such as files inspected or checks run;
- remaining uncertainty or blockers.
""".strip()

SUBAGENT_PARENT_INSTRUCTIONS = """
Delegation:
- Use subagent_run for self-contained work that needs several inspection or tool
  steps and would otherwise add substantial intermediate context.
- Give the worker all necessary context, an explicit scope, and expected output.
- Use explore for investigation and general only when edits may be required.
- Use isolation=worktree for self-contained edits when the main Git workspace is
  clean. Review and explicitly apply the returned worktree; never assume its
  changes reached the main workspace.
- Treat the returned report as evidence to review, not automatic task acceptance.
- Keep simple work in the current agent instead of delegating it.
""".strip()

SUBAGENT_SKILL_TOOLS = {"skill_load", "skill_read_resource"}

SUBAGENT_TOOL_PROFILES: dict[SubagentMode, set[str]] = {
    "explore": {
        "bash",
        "read_file",
        "glob",
        "search_text",
        "search",
        "run_tests",
        "run_lint",
        *SUBAGENT_SKILL_TOOLS,
    },
    "general": {
        "bash",
        "read_file",
        "write_file",
        "edit_file",
        "glob",
        "search_text",
        "search",
        "run_tests",
        "run_lint",
        *SUBAGENT_SKILL_TOOLS,
    },
}


@dataclass(frozen=True)
class SubagentRequest:
    task: str
    expected_output: str = ""
    mode: SubagentMode = "general"
    isolation: SubagentIsolation = "shared"
    task_id: str | None = None

    def __post_init__(self) -> None:
        if not self.task.strip():
            raise ValueError("Subagent task is required.")
        if self.mode not in SUBAGENT_TOOL_PROFILES:
            raise ValueError(f"Unsupported subagent mode: {self.mode}")
        if self.isolation not in {"shared", "worktree"}:
            raise ValueError(f"Unsupported subagent isolation: {self.isolation}")
        if self.isolation == "worktree" and self.mode != "general":
            raise ValueError("Worktree isolation is only available in general mode.")


@dataclass(frozen=True)
class SubagentResult:
    status: SubagentStatus
    summary: str
    steps: int
    tool_calls: int
    agent_id: str
    run_id: str
    usage: dict[str, Any] = field(default_factory=dict)
    worktree: dict[str, Any] | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SubagentRunner:
    llm: LLMClient
    workdir: Path | str
    max_steps: int = 24
    max_depth: int = 1
    skill_registry: SkillRegistry | None = None
    memory_manager: MemoryManager | None = None
    worktree_manager: WorktreeManager | None = None
    approval_provider: ApprovalProvider | None = field(
        default_factory=CliApprovalProvider
    )

    def __post_init__(self) -> None:
        self.workdir = Path(self.workdir).resolve()
        if self.max_steps <= 0:
            raise ValueError("max_steps must be greater than zero.")
        if self.max_depth <= 0:
            raise ValueError("max_depth must be greater than zero.")

    def run(
        self,
        request: SubagentRequest,
        *,
        parent_context: ToolExecutionContext,
    ) -> SubagentResult:
        agent_id = f"subagent-{uuid4().hex[:8]}"
        run_id = f"run-{uuid4().hex[:12]}"
        child_depth = parent_context.depth + 1

        if child_depth > self.max_depth:
            error = f"Subagent depth limit exceeded: {child_depth} > {self.max_depth}"
            result = SubagentResult(
                status="failed",
                summary="",
                steps=0,
                tool_calls=0,
                agent_id=agent_id,
                run_id=run_id,
                error=error,
            )
            self._emit(parent_context, "subagent_failed", result, request)
            return result

        worktree_info: WorktreeInfo | None = None
        child_workdir = self.workdir
        if request.isolation == "worktree":
            if self.worktree_manager is None:
                result = SubagentResult(
                    status="failed",
                    summary="",
                    steps=0,
                    tool_calls=0,
                    agent_id=agent_id,
                    run_id=run_id,
                    error="Worktree isolation is not configured.",
                )
                self._emit(parent_context, "subagent_failed", result, request)
                return result
            try:
                worktree_info = self.worktree_manager.create(
                    task_id=request.task_id,
                    agent_id=agent_id,
                    run_id=run_id,
                )
            except Exception as exc:
                result = SubagentResult(
                    status="failed",
                    summary="",
                    steps=0,
                    tool_calls=0,
                    agent_id=agent_id,
                    run_id=run_id,
                    error=str(exc),
                )
                self._emit(parent_context, "subagent_failed", result, request)
                return result
            child_workdir = Path(worktree_info.path)

        self._emit_started(parent_context, agent_id, run_id, child_depth, request)
        child_agent = Agent(
            llm=self.llm,
            tools=self._build_tools(request.mode, child_workdir),
            context_manager=self._build_context(
                request.mode,
                request.task,
                child_workdir,
            ),
            hooks=self._build_hooks(child_workdir),
            workdir=child_workdir,
            max_steps=self.max_steps,
            agent_id=agent_id,
            parent_run_id=parent_context.run_id,
            depth=child_depth,
        )
        messages = child_agent.new_messages()
        messages.append(
            {
                "role": "user",
                "content": self._delegation_prompt(request),
            }
        )

        try:
            agent_result = child_agent.run(
                messages,
                on_event=parent_context.on_event,
                run_id=run_id,
            )
        except Exception as exc:
            result = SubagentResult(
                status="failed",
                summary="",
                steps=0,
                tool_calls=0,
                agent_id=agent_id,
                run_id=run_id,
                worktree=self._finish_worktree(worktree_info),
                error=str(exc),
            )
            self._emit(parent_context, "subagent_failed", result, request)
            return result

        result = self._to_subagent_result(
            agent_result,
            worktree=self._finish_worktree(worktree_info),
        )
        self._emit(parent_context, "subagent_completed", result, request)
        return result

    def _build_tools(
        self,
        mode: SubagentMode,
        workdir: Path,
    ) -> ToolRegistry:
        registry = ToolRegistry()
        register_basic_tools(registry, workdir=workdir)
        register_search_tools(registry, workdir=workdir)
        register_verification_tools(registry, workdir=workdir)
        if self.skill_registry is not None:
            register_skill_tools(
                registry,
                skill_registry=self.skill_registry,
            )
        return registry.subset(SUBAGENT_TOOL_PROFILES[mode])

    def _build_hooks(self, workdir: Path) -> HookManager:
        manager = HookManager()
        manager.register_hook(
            "PreToolUse",
            PermissionHook(
                workdir=workdir,
                approval_provider=self.approval_provider,
            ),
        )
        return manager

    def _build_context(
        self,
        mode: SubagentMode,
        task: str,
        workdir: Path,
    ) -> ContextManager:
        sections = [
            PromptSection(
                name="workspace",
                content=f"Working directory: {workdir}",
                priority=20,
            ),
            PromptSection(
                name="subagent_mode",
                content=(
                    f"Mode: {mode}. Available tool profile: "
                    f"{', '.join(sorted(self._available_tool_names(mode)))}."
                ),
                priority=30,
            ),
        ]
        if self.skill_registry is not None:
            sections.append(build_skill_catalog_section(self.skill_registry))
        if self.memory_manager is not None:
            memories = self.memory_manager.retrieve_relevant(task)
            memory_context = format_memory_context(memories)
            if memory_context:
                sections.append(
                    PromptSection(
                        name="delegated_memory",
                        content=(
                            "The following recalled context is read-only and may "
                            "be stale. Prefer the delegated task and fresh workspace "
                            f"evidence.\n\n{memory_context}"
                        ),
                        priority=40,
                    )
                )

        return ContextManager(
            base_instructions=SUBAGENT_BASE_INSTRUCTIONS,
            sections=sections,
            llm=self.llm,
            workdir=workdir,
        )

    def _available_tool_names(self, mode: SubagentMode) -> set[str]:
        names = set(SUBAGENT_TOOL_PROFILES[mode])
        if self.skill_registry is None:
            names -= SUBAGENT_SKILL_TOOLS
        return names

    @staticmethod
    def _delegation_prompt(request: SubagentRequest) -> str:
        expected_output = request.expected_output.strip() or (
            "Return a concise result with evidence and any remaining blockers."
        )
        isolation = ""
        if request.isolation == "worktree":
            isolation = (
                "\n\n<workspace_isolation>\n"
                "You are working in an isolated Git worktree. Make and verify "
                "changes here, but do not merge, apply, or delete the worktree."
                "\n</workspace_isolation>"
            )
        return (
            "<delegated_task>\n"
            f"{request.task.strip()}\n"
            "</delegated_task>\n\n"
            "<expected_output>\n"
            f"{expected_output}\n"
            "</expected_output>"
            f"{isolation}"
        )

    @staticmethod
    def _to_subagent_result(
        result: AgentRunResult,
        *,
        worktree: dict[str, Any] | None = None,
    ) -> SubagentResult:
        return SubagentResult(
            status=result.status,
            summary=result.content,
            steps=result.steps,
            tool_calls=result.tool_calls,
            agent_id=result.agent_id,
            run_id=result.run_id,
            usage=result.usage,
            worktree=worktree,
        )

    def _finish_worktree(
        self,
        info: WorktreeInfo | None,
    ) -> dict[str, Any] | None:
        if info is None or self.worktree_manager is None:
            return None
        try:
            review = self.worktree_manager.finish(info.id)
        except Exception as exc:
            return {
                **info.to_dict(),
                "review_error": str(exc),
            }
        return {
            **review["worktree"],
            "changed_files": review["changed_files"],
            "change_count": review["change_count"],
            "diff": review["diff"],
            "diff_path": review["diff_path"],
            "diff_truncated": review["diff_truncated"],
        }

    @staticmethod
    def _emit_started(
        parent_context: ToolExecutionContext,
        agent_id: str,
        run_id: str,
        depth: int,
        request: SubagentRequest,
    ) -> None:
        event = AgentEvent(
            type="subagent_started",
            step=parent_context.step,
            data={
                "task": request.task,
                "mode": request.mode,
                "isolation": request.isolation,
                "task_id": request.task_id,
            },
            agent_id=agent_id,
            run_id=run_id,
            parent_run_id=parent_context.run_id,
            depth=depth,
        )
        record_agent_event(event)
        if parent_context.on_event is not None:
            parent_context.on_event(event)

    @staticmethod
    def _emit(
        parent_context: ToolExecutionContext,
        event_type: Literal["subagent_completed", "subagent_failed"],
        result: SubagentResult,
        request: SubagentRequest,
    ) -> None:
        data: dict[str, Any] = {
            "task": request.task,
            "mode": request.mode,
            "isolation": request.isolation,
            "status": result.status,
            "steps": result.steps,
            "tool_calls": result.tool_calls,
        }
        if request.task_id:
            data["task_id"] = request.task_id
        if result.worktree:
            data["worktree_id"] = result.worktree.get("id")
        if result.error:
            data["error"] = result.error
        event = AgentEvent(
            type=event_type,
            step=result.steps,
            data=data,
            agent_id=result.agent_id,
            run_id=result.run_id,
            parent_run_id=parent_context.run_id,
            depth=parent_context.depth + 1,
        )
        record_agent_event(event)
        if parent_context.on_event is not None:
            parent_context.on_event(event)


__all__ = [
    "SUBAGENT_PARENT_INSTRUCTIONS",
    "SUBAGENT_SKILL_TOOLS",
    "SUBAGENT_TOOL_PROFILES",
    "SubagentIsolation",
    "SubagentMode",
    "SubagentRequest",
    "SubagentResult",
    "SubagentRunner",
]
