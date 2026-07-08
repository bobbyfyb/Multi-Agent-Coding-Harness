from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal
from uuid import uuid4

from llm_agent.agent import Agent, AgentCallback
from llm_agent.artifact_system import (
    Artifact,
    ArtifactManager,
    build_artifact_policy_section,
)
from llm_agent.context_manager import (
    ContextManager,
    PromptSection,
    build_tool_summary_section,
)
from llm_agent.hooks import build_default_hook_manager
from llm_agent.hooks.permission_hooks import ApprovalProvider
from llm_agent.llm_client import LLMClient
from llm_agent.skill_system import (
    SkillNotFoundError,
    SkillRegistry,
    build_skill_catalog_section,
)
from llm_agent.tools import build_default_registry
from llm_agent.trace_system import (
    TraceRecorder,
    record_trace,
    summarize_text,
    trace_scope,
)


WorkflowStatus = Literal["completed", "failed"]
WorkflowPhaseStatus = Literal["completed", "failed"]
WorkflowPhase = Literal[
    "pm_plan",
    "engineer_implement",
    "qa_verify",
    "engineer_fix",
    "qa_regression",
    "pm_acceptance",
]


WORKFLOW_WORKER_OUTPUT_INSTRUCTIONS = """
Workflow worker output:
- The required artifacts are the handoff contract; put detailed PRD, task,
  implementation, verification, or acceptance content in artifacts.
- Final chat responses should be concise status summaries, not full artifact
  copies. Prefer 3-5 short bullets.
- If a loaded skill expects interactive clarification, adapt it to this workflow:
  make reasonable assumptions, record open questions or risks in the artifact,
  and continue unless the original request has no usable core idea.
""".strip()

SKILL_TOOLS = {"skill_list", "skill_load", "skill_read_resource"}
PM_TOOLS = {
    "artifact_create",
    "artifact_update",
    "artifact_get",
    "artifact_list",
    "task_create",
    "task_update",
    "task_list",
    "task_get",
    "read_file",
    "glob",
    "search_text",
    "search",
    *SKILL_TOOLS,
}
ENGINEER_TOOLS = {
    "artifact_create",
    "artifact_update",
    "artifact_get",
    "artifact_list",
    "task_get",
    "task_update",
    "task_list",
    "bash",
    "read_file",
    "write_file",
    "edit_file",
    "glob",
    "search_text",
    "search",
    "run_tests",
    "run_lint",
    *SKILL_TOOLS,
}
QA_TOOLS = {
    "artifact_create",
    "artifact_update",
    "artifact_get",
    "artifact_list",
    "task_get",
    "task_update",
    "task_list",
    "read_file",
    "glob",
    "search_text",
    "search",
    "run_tests",
    "run_lint",
    *SKILL_TOOLS,
}


@dataclass(frozen=True)
class RoleSpec:
    name: str
    agent_id: str
    instructions: str
    tool_names: set[str]
    required_skills: tuple[str, ...] = ()
    optional_skills: tuple[str, ...] = ()
    max_steps: int = 16


@dataclass(frozen=True)
class WorkflowPhaseResult:
    phase: WorkflowPhase
    role: str
    status: WorkflowPhaseStatus
    run_ids: list[str]
    artifact_ids: list[str]
    summary: str = ""
    attempts: int = 1
    error: str | None = None
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkflowResult:
    workflow_id: str
    status: WorkflowStatus
    phases: list[WorkflowPhaseResult]
    artifact_ids: list[str]
    qa_verdict: str | None = None
    fix_cycles: int = 0
    error: str | None = None


@dataclass(frozen=True)
class _GateResult:
    ok: bool
    message: str
    artifact_ids: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class SerialCodingWorkflow:
    llm: LLMClient
    workdir: Path | str
    artifact_manager: ArtifactManager
    approval_provider: ApprovalProvider | None = None
    skill_registry: SkillRegistry | None = None
    role_specs: dict[str, RoleSpec] = field(
        default_factory=lambda: dict(ROLE_SPECS)
    )
    max_fix_cycles: int = 1
    max_phase_retries: int = 1
    max_context_tokens: int = 100_000

    def __post_init__(self) -> None:
        self.workdir = Path(self.workdir).resolve()
        missing_roles = {"pm", "engineer", "qa", "pm_acceptance"} - set(
            self.role_specs
        )
        if missing_roles:
            raise ValueError(
                "Workflow role_specs missing required role(s): "
                + ", ".join(sorted(missing_roles))
            )
        if self.max_fix_cycles < 0:
            raise ValueError("max_fix_cycles cannot be negative.")
        if self.max_phase_retries < 0:
            raise ValueError("max_phase_retries cannot be negative.")

    def run(
        self,
        request: str,
        *,
        on_event: AgentCallback | None = None,
        run_id: str | None = None,
        trace: TraceRecorder | None = None,
    ) -> WorkflowResult:
        if not request.strip():
            raise ValueError("Workflow request is required.")

        workflow_id = run_id or f"workflow-{uuid4().hex[:12]}"
        initial_artifact_ids = self._artifact_ids()
        phases: list[WorkflowPhaseResult] = []
        qa_verdict: str | None = None
        fix_cycles = 0

        with trace_scope(
            trace,
            run_id=workflow_id,
            agent_id="workflow-orchestrator",
        ):
            self._record_workflow_trace(
                "workflow.started",
                phase="started",
                data={"workflow_id": workflow_id, "request": summarize_text(request)},
            )

            pm_result = self._run_phase(
                phase="pm_plan",
                role=self.role_specs["pm"],
                workflow_id=workflow_id,
                request=request,
                prompt=self._pm_prompt(workflow_id, request),
                gate=lambda before: self._gate_required_artifacts(
                    initial_artifact_ids,
                    before,
                    required={"prd": {"ready", "accepted"}, "task_spec": {"ready", "accepted"}},
                ),
                on_event=on_event,
                trace=trace,
            )
            phases.append(pm_result)
            if pm_result.status == "failed":
                return self._finish(
                    workflow_id,
                    status="failed",
                    phases=phases,
                    initial_artifact_ids=initial_artifact_ids,
                    error=pm_result.error,
                )

            engineer_result = self._run_phase(
                phase="engineer_implement",
                role=self.role_specs["engineer"],
                workflow_id=workflow_id,
                request=request,
                prompt=self._engineer_prompt(workflow_id, request, fix=False),
                gate=lambda before: self._gate_required_artifacts(
                    initial_artifact_ids,
                    before,
                    required={"implementation_report": None},
                ),
                on_event=on_event,
                trace=trace,
            )
            phases.append(engineer_result)
            if engineer_result.status == "failed":
                return self._finish(
                    workflow_id,
                    status="failed",
                    phases=phases,
                    initial_artifact_ids=initial_artifact_ids,
                    error=engineer_result.error,
                )

            qa_result = self._run_phase(
                phase="qa_verify",
                role=self.role_specs["qa"],
                workflow_id=workflow_id,
                request=request,
                prompt=self._qa_prompt(workflow_id, request, regression=False),
                gate=lambda before: self._gate_test_report(
                    initial_artifact_ids,
                    before,
                ),
                on_event=on_event,
                trace=trace,
            )
            phases.append(qa_result)
            if qa_result.status == "failed":
                return self._finish(
                    workflow_id,
                    status="failed",
                    phases=phases,
                    initial_artifact_ids=initial_artifact_ids,
                    error=qa_result.error,
                )
            qa_verdict = str(qa_result.data.get("qa_verdict", "unknown"))

            while qa_verdict == "fail" and fix_cycles < self.max_fix_cycles:
                fix_cycles += 1
                fix_result = self._run_phase(
                    phase="engineer_fix",
                    role=self.role_specs["engineer"],
                    workflow_id=workflow_id,
                    request=request,
                    prompt=self._engineer_prompt(
                        workflow_id,
                        request,
                        fix=True,
                        fix_cycle=fix_cycles,
                    ),
                    gate=lambda before: self._gate_required_artifacts(
                        initial_artifact_ids,
                        before,
                        required={"implementation_report": None},
                    ),
                    on_event=on_event,
                    trace=trace,
                )
                phases.append(fix_result)
                if fix_result.status == "failed":
                    return self._finish(
                        workflow_id,
                        status="failed",
                        phases=phases,
                        initial_artifact_ids=initial_artifact_ids,
                        qa_verdict=qa_verdict,
                        fix_cycles=fix_cycles,
                        error=fix_result.error,
                    )

                qa_result = self._run_phase(
                    phase="qa_regression",
                    role=self.role_specs["qa"],
                    workflow_id=workflow_id,
                    request=request,
                    prompt=self._qa_prompt(
                        workflow_id,
                        request,
                        regression=True,
                        fix_cycle=fix_cycles,
                    ),
                    gate=lambda before: self._gate_test_report(
                        initial_artifact_ids,
                        before,
                    ),
                    on_event=on_event,
                    trace=trace,
                )
                phases.append(qa_result)
                if qa_result.status == "failed":
                    return self._finish(
                        workflow_id,
                        status="failed",
                        phases=phases,
                        initial_artifact_ids=initial_artifact_ids,
                        qa_verdict=qa_verdict,
                        fix_cycles=fix_cycles,
                        error=qa_result.error,
                    )
                qa_verdict = str(qa_result.data.get("qa_verdict", "unknown"))

            acceptance_result = self._run_phase(
                phase="pm_acceptance",
                role=self.role_specs["pm_acceptance"],
                workflow_id=workflow_id,
                request=request,
                prompt=self._acceptance_prompt(
                    workflow_id,
                    request,
                    qa_verdict=qa_verdict,
                ),
                gate=lambda before: self._gate_required_artifacts(
                    initial_artifact_ids,
                    before,
                    required={"acceptance_report": None},
                ),
                on_event=on_event,
                trace=trace,
            )
            phases.append(acceptance_result)
            if acceptance_result.status == "failed":
                return self._finish(
                    workflow_id,
                    status="failed",
                    phases=phases,
                    initial_artifact_ids=initial_artifact_ids,
                    qa_verdict=qa_verdict,
                    fix_cycles=fix_cycles,
                    error=acceptance_result.error,
                )

            status: WorkflowStatus = "completed" if qa_verdict == "pass" else "failed"
            error = None if status == "completed" else "QA verdict did not pass."
            return self._finish(
                workflow_id,
                status=status,
                phases=phases,
                initial_artifact_ids=initial_artifact_ids,
                qa_verdict=qa_verdict,
                fix_cycles=fix_cycles,
                error=error,
            )

    def _run_phase(
        self,
        *,
        phase: WorkflowPhase,
        role: RoleSpec,
        workflow_id: str,
        request: str,
        prompt: str,
        gate: Callable[[dict[str, int]], _GateResult],
        on_event: AgentCallback | None,
        trace: TraceRecorder | None,
    ) -> WorkflowPhaseResult:
        before_versions = self._artifact_versions()
        run_ids: list[str] = []
        messages: list[dict[str, Any]] | None = None
        last_summary = ""
        last_gate = _GateResult(ok=False, message="Phase did not run.")

        self._record_workflow_trace(
            "workflow.phase.started",
            phase="started",
            data={"workflow_id": workflow_id, "phase": phase, "role": role.name},
        )

        for attempt in range(1, self.max_phase_retries + 2):
            agent = self._build_role_agent(
                role,
                workflow_id=workflow_id,
                phase=phase,
                request=request,
            )
            if messages is None:
                messages = agent.new_messages()
                messages.append({"role": "user", "content": prompt})
            else:
                messages[0] = agent.new_messages()[0]

            phase_run_id = f"{workflow_id}-{phase}-{attempt}"
            run_ids.append(phase_run_id)
            result = agent.run(
                messages,
                on_event=on_event,
                run_id=phase_run_id,
                trace=trace,
            )
            last_summary = result.content
            if result.status != "completed":
                last_gate = _GateResult(
                    ok=False,
                    message=f"Role agent ended with status={result.status}.",
                )
            else:
                last_gate = gate(before_versions)
            if last_gate.ok:
                self._record_workflow_trace(
                    "workflow.phase.completed",
                    phase="completed",
                    data={
                        "workflow_id": workflow_id,
                        "phase": phase,
                        "role": role.name,
                        "attempts": attempt,
                        "artifact_ids": last_gate.artifact_ids,
                        **last_gate.data,
                    },
                )
                return WorkflowPhaseResult(
                    phase=phase,
                    role=role.name,
                    status="completed",
                    run_ids=run_ids,
                    artifact_ids=last_gate.artifact_ids,
                    summary=last_summary,
                    attempts=attempt,
                    data=last_gate.data,
                )

            if attempt <= self.max_phase_retries:
                messages.append(
                    {
                        "role": "user",
                        "content": self._corrective_prompt(phase, last_gate.message),
                    }
                )

        self._record_workflow_trace(
            "workflow.phase.failed",
            phase="failed",
            status="error",
            data={
                "workflow_id": workflow_id,
                "phase": phase,
                "role": role.name,
                "attempts": self.max_phase_retries + 1,
                "error": last_gate.message,
            },
        )
        return WorkflowPhaseResult(
            phase=phase,
            role=role.name,
            status="failed",
            run_ids=run_ids,
            artifact_ids=last_gate.artifact_ids,
            summary=last_summary,
            attempts=self.max_phase_retries + 1,
            error=last_gate.message,
            data=last_gate.data,
        )

    def _build_role_agent(
        self,
        role: RoleSpec,
        *,
        workflow_id: str,
        phase: WorkflowPhase,
        request: str,
    ) -> Agent:
        registry = build_default_registry(
            workdir=self.workdir,
            artifact_manager=self.artifact_manager,
            skill_registry=self.skill_registry,
        ).subset(role.tool_names)
        sections = [
            PromptSection(
                name="workspace",
                content=f"Working directory: {self.workdir}",
                priority=20,
            ),
            PromptSection(
                name="workflow",
                content=(
                    f"Workflow id: {workflow_id}\n"
                    f"Current phase: {phase}\n"
                    f"Original user request:\n{request}"
                ),
                priority=25,
            ),
            PromptSection(
                name="role",
                content=role.instructions,
                priority=30,
            ),
            PromptSection(
                name="workflow_worker_output",
                content=WORKFLOW_WORKER_OUTPUT_INSTRUCTIONS,
                priority=32,
            ),
            *self._build_role_skill_sections(role),
            build_artifact_policy_section(priority=40),
            build_tool_summary_section(registry.tool_specs()),
        ]
        context = ContextManager(
            llm=self.llm,
            workdir=self.workdir,
            max_context_tokens=self.max_context_tokens,
            sections=sections,
        )
        return Agent(
            llm=self.llm,
            tools=registry,
            context_manager=context,
            hooks=build_default_hook_manager(
                workdir=self.workdir,
                approval_provider=self.approval_provider,
                artifact_manager=self.artifact_manager,
            ),
            workdir=self.workdir,
            max_steps=role.max_steps,
            agent_id=role.agent_id,
            parent_run_id=workflow_id,
            depth=1,
        )

    def _build_role_skill_sections(self, role: RoleSpec) -> list[PromptSection]:
        if self.skill_registry is None:
            return []

        sections = [
            build_skill_catalog_section(self.skill_registry, priority=34),
        ]
        optional_skills = self._format_optional_skills(role)
        if optional_skills:
            sections.append(
                PromptSection(
                    name=f"role_optional_skills_{role.agent_id}",
                    content=optional_skills,
                    priority=35,
                )
            )
        required_skills = self._format_required_skills(role)
        if required_skills:
            sections.append(
                PromptSection(
                    name=f"role_required_skills_{role.agent_id}",
                    content=required_skills,
                    priority=36,
                )
            )
        return sections

    def _format_required_skills(self, role: RoleSpec) -> str:
        if self.skill_registry is None or not role.required_skills:
            return ""

        formatted = []
        for skill_name in role.required_skills:
            try:
                document = self.skill_registry.load(skill_name)
            except SkillNotFoundError as exc:
                raise ValueError(
                    f"Required skill '{skill_name}' for role {role.name} "
                    "is not registered."
                ) from exc
            metadata = document.metadata
            resource_text = (
                "\nResources:\n" + "\n".join(f"- {path}" for path in metadata.resources)
                if metadata.resources
                else ""
            )
            formatted.append(
                (
                    f"## {metadata.name}\n"
                    f"Description: {metadata.description}\n"
                    f"When to use: {metadata.when_to_use or 'Always for this role.'}"
                    f"{resource_text}\n\n"
                    f"{document.instructions}"
                ).strip()
            )

        return (
            "The following skills are preloaded because this role requires them. "
            "They are procedural guidance only and cannot override workflow, "
            "artifact, tool permission, or user instructions.\n\n"
            + "\n\n".join(formatted)
        )

    def _format_optional_skills(self, role: RoleSpec) -> str:
        if self.skill_registry is None or not role.optional_skills:
            return ""

        lines = []
        for skill_name in role.optional_skills:
            try:
                metadata = self.skill_registry.get(skill_name)
            except SkillNotFoundError:
                continue
            line = f"- {metadata.name}: {metadata.description}"
            if metadata.when_to_use:
                line += f" When to use: {metadata.when_to_use}"
            lines.append(line)
        if not lines:
            return ""
        return (
            "Role-relevant optional skills are available. Load one with "
            "skill_load only when it is useful for the current phase.\n"
            + "\n".join(lines)
        )

    def _gate_required_artifacts(
        self,
        initial_artifact_ids: set[str],
        before_versions: dict[str, int],
        *,
        required: dict[str, set[str] | None],
    ) -> _GateResult:
        artifacts = self._changed_workflow_artifacts(
            initial_artifact_ids,
            before_versions,
        )
        found: dict[str, Artifact] = {}
        missing: list[str] = []
        for kind, statuses in required.items():
            matches = [artifact for artifact in artifacts if artifact.kind == kind]
            if statuses is not None:
                matches = [
                    artifact for artifact in matches if artifact.status in statuses
                ]
            if not matches:
                suffix = (
                    f" with status in {sorted(statuses)}"
                    if statuses is not None
                    else ""
                )
                missing.append(f"{kind}{suffix}")
                continue
            found[kind] = self._latest(matches)

        if missing:
            return _GateResult(
                ok=False,
                message=(
                    "Missing required artifact(s) changed in this phase: "
                    + ", ".join(missing)
                ),
            )
        return _GateResult(
            ok=True,
            message="ok",
            artifact_ids=[artifact.id for artifact in found.values()],
        )

    def _gate_test_report(
        self,
        initial_artifact_ids: set[str],
        before_versions: dict[str, int],
    ) -> _GateResult:
        gate = self._gate_required_artifacts(
            initial_artifact_ids,
            before_versions,
            required={"test_report": None},
        )
        if not gate.ok:
            return gate
        artifact = self.artifact_manager.get_artifact(gate.artifact_ids[0])
        verdict = str(artifact.metadata.get("verdict", "")).strip().lower()
        if verdict not in {"pass", "fail"}:
            return _GateResult(
                ok=False,
                message=(
                    "test_report must set metadata.verdict to 'pass' or 'fail'."
                ),
                artifact_ids=[artifact.id],
            )
        return _GateResult(
            ok=True,
            message="ok",
            artifact_ids=[artifact.id],
            data={"qa_verdict": verdict},
        )

    def _changed_workflow_artifacts(
        self,
        initial_artifact_ids: set[str],
        before_versions: dict[str, int],
    ) -> list[Artifact]:
        artifacts = []
        for artifact in self.artifact_manager.list_artifacts(include_archived=True):
            if artifact.id in initial_artifact_ids:
                continue
            previous_version = before_versions.get(artifact.id)
            if previous_version is None or artifact.version != previous_version:
                artifacts.append(artifact)
        return artifacts

    def _workflow_artifacts(self, initial_artifact_ids: set[str]) -> list[Artifact]:
        return [
            artifact
            for artifact in self.artifact_manager.list_artifacts(include_archived=True)
            if artifact.id not in initial_artifact_ids
        ]

    def _artifact_ids(self) -> set[str]:
        return {
            artifact.id
            for artifact in self.artifact_manager.list_artifacts(
                include_archived=True
            )
        }

    def _artifact_versions(self) -> dict[str, int]:
        return {
            artifact.id: artifact.version
            for artifact in self.artifact_manager.list_artifacts(
                include_archived=True
            )
        }

    def _finish(
        self,
        workflow_id: str,
        *,
        status: WorkflowStatus,
        phases: list[WorkflowPhaseResult],
        initial_artifact_ids: set[str],
        qa_verdict: str | None = None,
        fix_cycles: int = 0,
        error: str | None = None,
    ) -> WorkflowResult:
        artifact_ids = [
            artifact.id for artifact in self._workflow_artifacts(initial_artifact_ids)
        ]
        result = WorkflowResult(
            workflow_id=workflow_id,
            status=status,
            phases=phases,
            artifact_ids=artifact_ids,
            qa_verdict=qa_verdict,
            fix_cycles=fix_cycles,
            error=error,
        )
        self._record_workflow_trace(
            "workflow.completed",
            phase="completed",
            status=("ok" if status == "completed" else "error"),
            data={
                "workflow_id": workflow_id,
                "status": status,
                "artifact_ids": artifact_ids,
                "qa_verdict": qa_verdict,
                "fix_cycles": fix_cycles,
                "error": error,
            },
        )
        return result

    def _record_workflow_trace(
        self,
        name: str,
        *,
        phase: str,
        status: str = "ok",
        data: dict[str, Any] | None = None,
    ) -> None:
        record_trace(
            category="workflow",
            name=name,
            phase=phase,
            status=status,
            data=data or {},
        )

    @staticmethod
    def _latest(artifacts: list[Artifact]) -> Artifact:
        return sorted(
            artifacts,
            key=lambda artifact: (
                artifact.updated_at,
                artifact.created_at,
                artifact.id,
            ),
            reverse=True,
        )[0]

    @staticmethod
    def _corrective_prompt(phase: WorkflowPhase, issue: str) -> str:
        return (
            f"The {phase} phase did not satisfy its artifact gate.\n"
            f"Issue: {issue}\n\n"
            "Create or update only the missing required artifact(s) now. Do not "
            "redo unrelated work. Use expected_version when updating an existing "
            "artifact."
        )

    @staticmethod
    def _pm_prompt(workflow_id: str, request: str) -> str:
        return f"""
        Create the planning handoff artifacts for this workflow.

        Workflow id: {workflow_id}
        User request:
        {request}

        Required actions:
        - Create one artifact with kind="prd" and status="ready".
        - Create one artifact with kind="task_spec" and status="ready".
        - Include metadata.workflow_id="{workflow_id}" and metadata.role="pm".
        - You may create project tasks if useful, but the artifact gate requires the PRD
        and TaskSpec artifacts.
        - Do not modify source files.
        """.strip()

    @staticmethod
    def _engineer_prompt(
        workflow_id: str,
        request: str,
        *,
        fix: bool,
        fix_cycle: int = 0,
    ) -> str:
        if fix:
            action = (
                f"This is fix cycle {fix_cycle}. Read the latest test_report, "
                "fix the reported failures, then update or create an "
                "implementation_report artifact."
            )
        else:
            action = (
                "Read the latest PRD and TaskSpec artifacts, implement only the "
                "requested scope, then create an implementation_report artifact."
            )
        return f"""
{action}

Workflow id: {workflow_id}
User request:
{request}

Required actions:
- Inspect relevant artifacts with artifact_list/artifact_get before acting.
- Make focused code changes only when needed.
- Run focused verification when appropriate.
- Create or update one artifact with kind="implementation_report".
- Include metadata.workflow_id="{workflow_id}" and metadata.role="engineer".
- Summarize changed files, verification evidence, and remaining risks.
""".strip()

    @staticmethod
    def _qa_prompt(
        workflow_id: str,
        request: str,
        *,
        regression: bool,
        fix_cycle: int = 0,
    ) -> str:
        label = (
            f"regression verification after fix cycle {fix_cycle}"
            if regression
            else "initial verification"
        )
        return f"""
Perform {label} for this workflow.

Workflow id: {workflow_id}
User request:
{request}

Required actions:
- Read the latest PRD, TaskSpec, and ImplementationReport artifacts.
- Do not modify source files.
- Run focused verification using run_tests/run_lint when appropriate.
- Create one artifact with kind="test_report".
- Set metadata.workflow_id="{workflow_id}".
- Set metadata.role="qa".
- Set metadata.verdict exactly to "pass" or "fail".
- If verdict is "fail", include concrete failures and suggested fixes.
""".strip()

    @staticmethod
    def _acceptance_prompt(
        workflow_id: str,
        request: str,
        *,
        qa_verdict: str | None,
    ) -> str:
        return f"""
Create the final PM acceptance artifact for this workflow.

Workflow id: {workflow_id}
QA verdict: {qa_verdict or "unknown"}
User request:
{request}

Required actions:
- Read the relevant PRD, TaskSpec, ImplementationReport, and TestReport artifacts.
- Create one artifact with kind="acceptance_report".
- Include metadata.workflow_id="{workflow_id}" and metadata.role="pm".
- If QA verdict is pass, state accepted status and evidence.
- If QA verdict is fail or unknown, state rejected/blocked status and blockers.
- Do not modify source files.
""".strip()


ROLE_SPECS: dict[str, RoleSpec] = {
    "pm": RoleSpec(
        name="PM",
        agent_id="pm",
        instructions=(
            "You are the PM worker. Produce clear planning artifacts and task "
            "handoffs. Do not modify source files."
        ),
        tool_names=PM_TOOLS,
        max_steps=10,
    ),
    "engineer": RoleSpec(
        name="Engineer",
        agent_id="engineer",
        instructions=(
            "You are the Engineer worker. Implement the requested scope, keep "
            "changes focused, verify when possible, and report evidence."
        ),
        tool_names=ENGINEER_TOOLS,
        max_steps=24,
    ),
    "qa": RoleSpec(
        name="QA",
        agent_id="qa",
        instructions=(
            "You are the QA worker. Verify behavior, avoid source edits, and "
            "produce a test_report with metadata.verdict."
        ),
        tool_names=QA_TOOLS,
        max_steps=16,
    ),
    "pm_acceptance": RoleSpec(
        name="PM Acceptance",
        agent_id="pm-acceptance",
        instructions=(
            "You are the PM acceptance worker. Review artifacts and decide "
            "whether the workflow is accepted or blocked."
        ),
        tool_names=PM_TOOLS,
        max_steps=8,
    ),
}


def parse_workflow_command(query: str) -> str | None:
    stripped = query.strip()
    if stripped == "/workflow":
        return ""
    if not stripped.startswith("/workflow"):
        return None
    rest = stripped.removeprefix("/workflow")
    if not rest or not rest[0].isspace():
        return None
    return rest.strip()


__all__ = [
    "ROLE_SPECS",
    "RoleSpec",
    "SerialCodingWorkflow",
    "WorkflowPhase",
    "WorkflowPhaseResult",
    "WorkflowResult",
    "WorkflowStatus",
    "parse_workflow_command",
]
