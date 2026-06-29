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


SubagentMode = Literal["explore", "general"]
SubagentStatus = Literal["completed", "max_steps", "failed"]

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
- Treat the returned report as evidence to review, not automatic task acceptance.
- Keep simple work in the current agent instead of delegating it.
""".strip()

SUBAGENT_SKILL_TOOLS = {"skill_load", "skill_read_resource"}

SUBAGENT_TOOL_PROFILES: dict[SubagentMode, set[str]] = {
    "explore": {
        "bash",
        "read_file",
        "glob",
        "search",
        *SUBAGENT_SKILL_TOOLS,
    },
    "general": {
        "bash",
        "read_file",
        "write_file",
        "edit_file",
        "glob",
        "search",
        *SUBAGENT_SKILL_TOOLS,
    },
}


@dataclass(frozen=True)
class SubagentRequest:
    task: str
    expected_output: str = ""
    mode: SubagentMode = "general"

    def __post_init__(self) -> None:
        if not self.task.strip():
            raise ValueError("Subagent task is required.")
        if self.mode not in SUBAGENT_TOOL_PROFILES:
            raise ValueError(f"Unsupported subagent mode: {self.mode}")


@dataclass(frozen=True)
class SubagentResult:
    status: SubagentStatus
    summary: str
    steps: int
    tool_calls: int
    agent_id: str
    run_id: str
    usage: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SubagentRunner:
    llm: LLMClient
    workdir: Path | str
    max_steps: int = 12
    max_depth: int = 1
    skill_registry: SkillRegistry | None = None
    memory_manager: MemoryManager | None = None
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
            error = (
                f"Subagent depth limit exceeded: "
                f"{child_depth} > {self.max_depth}"
            )
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

        self._emit_started(parent_context, agent_id, run_id, child_depth, request)
        child_agent = Agent(
            llm=self.llm,
            tools=self._build_tools(request.mode),
            context_manager=self._build_context(request.mode, request.task),
            hooks=self._build_hooks(),
            workdir=self.workdir,
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
                error=str(exc),
            )
            self._emit(parent_context, "subagent_failed", result, request)
            return result

        result = self._to_subagent_result(agent_result)
        self._emit(parent_context, "subagent_completed", result, request)
        return result

    def _build_tools(self, mode: SubagentMode) -> ToolRegistry:
        registry = ToolRegistry()
        register_basic_tools(registry, workdir=self.workdir)
        register_search_tools(registry, workdir=self.workdir)
        if self.skill_registry is not None:
            register_skill_tools(
                registry,
                skill_registry=self.skill_registry,
            )
        return registry.subset(SUBAGENT_TOOL_PROFILES[mode])

    def _build_hooks(self) -> HookManager:
        manager = HookManager()
        manager.register_hook(
            "PreToolUse",
            PermissionHook(
                workdir=self.workdir,
                approval_provider=self.approval_provider,
            ),
        )
        return manager

    def _build_context(
        self,
        mode: SubagentMode,
        task: str,
    ) -> ContextManager:
        sections = [
            PromptSection(
                name="workspace",
                content=f"Working directory: {self.workdir}",
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
            workdir=self.workdir,
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
        return (
            "<delegated_task>\n"
            f"{request.task.strip()}\n"
            "</delegated_task>\n\n"
            "<expected_output>\n"
            f"{expected_output}\n"
            "</expected_output>"
        )

    @staticmethod
    def _to_subagent_result(result: AgentRunResult) -> SubagentResult:
        return SubagentResult(
            status=result.status,
            summary=result.content,
            steps=result.steps,
            tool_calls=result.tool_calls,
            agent_id=result.agent_id,
            run_id=result.run_id,
            usage=result.usage,
        )

    @staticmethod
    def _emit_started(
        parent_context: ToolExecutionContext,
        agent_id: str,
        run_id: str,
        depth: int,
        request: SubagentRequest,
    ) -> None:
        if parent_context.on_event is None:
            return
        parent_context.on_event(
            AgentEvent(
                type="subagent_started",
                step=parent_context.step,
                data={"task": request.task, "mode": request.mode},
                agent_id=agent_id,
                run_id=run_id,
                parent_run_id=parent_context.run_id,
                depth=depth,
            )
        )

    @staticmethod
    def _emit(
        parent_context: ToolExecutionContext,
        event_type: Literal["subagent_completed", "subagent_failed"],
        result: SubagentResult,
        request: SubagentRequest,
    ) -> None:
        if parent_context.on_event is None:
            return
        data: dict[str, Any] = {
            "task": request.task,
            "mode": request.mode,
            "status": result.status,
            "steps": result.steps,
            "tool_calls": result.tool_calls,
        }
        if result.error:
            data["error"] = result.error
        parent_context.on_event(
            AgentEvent(
                type=event_type,
                step=result.steps,
                data=data,
                agent_id=result.agent_id,
                run_id=result.run_id,
                parent_run_id=parent_context.run_id,
                depth=parent_context.depth + 1,
            )
        )


__all__ = [
    "SUBAGENT_PARENT_INSTRUCTIONS",
    "SUBAGENT_SKILL_TOOLS",
    "SUBAGENT_TOOL_PROFILES",
    "SubagentMode",
    "SubagentRequest",
    "SubagentResult",
    "SubagentRunner",
]
