from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Literal
from uuid import uuid4

from llm_agent.agent import Agent, AgentCallback, AgentEvent
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
from llm_agent.memory_system import (
    MemoryManager,
    build_memory_policy_section,
)
from llm_agent.skill_system import (
    SkillNotFoundError,
    SkillRegistry,
    build_skill_catalog_section,
)
from llm_agent.task_system import Task, TaskManager, TaskSystemError
from llm_agent.tools import build_default_registry
from llm_agent.trace_system import (
    TraceRecorder,
    record_trace,
    summarize_text,
    trace_scope,
)
from llm_agent.workflow_checkpoint import build_attempt_checkpoint
from llm_agent.workflow_store import (
    WorkflowRunRecord,
    WorkflowStore,
)
from llm_agent.worktree import (
    WorktreeError,
    WorktreeManager,
)


if TYPE_CHECKING:
    from llm_agent.mcp_config import MCPToolScope
    from llm_agent.mcp_system import MCPManager


WorkflowStatus = Literal["completed", "failed"]
WorkflowPhaseStatus = Literal["completed", "failed"]
WorkflowIsolation = Literal["shared", "worktree"]
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
- Treat the assigned Task as live execution state. When a decision, blocker, or
  meaningful remaining step changes, update its notes before spending more turns
  on broad discovery. In particular, respond to step-budget warnings by recording
  what is completed, what remains, and the exact next action.
- If an attempt must continue, structure the concise status with headings
  "Completed", "Remaining", "Next actions", "Decisions", and "Blockers" when
  applicable. The Orchestrator persists these fields for the next loop.
- If a loaded skill expects interactive clarification, adapt it to this workflow:
  make reasonable assumptions, record open questions or risks in the artifact,
  and continue unless the original request has no usable core idea.
""".strip()

SKILL_TOOLS = {"skill_list", "skill_load", "skill_read_resource"}
MEMORY_READ_TOOLS = {"memory_search", "memory_get"}
PM_TOOLS = {
    "artifact_create",
    "artifact_update",
    "artifact_get",
    "artifact_list",
    "task_create",
    "task_claim",
    "task_complete",
    "task_update",
    "task_list",
    "task_get",
    "read_file",
    "glob",
    "search_text",
    "search",
    *MEMORY_READ_TOOLS,
    *SKILL_TOOLS,
}
ENGINEER_TOOLS = {
    "artifact_create",
    "artifact_update",
    "artifact_get",
    "artifact_list",
    "task_create",
    "task_get",
    "task_claim",
    "task_complete",
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
    *MEMORY_READ_TOOLS,
    *SKILL_TOOLS,
}
QA_TOOLS = {
    "artifact_create",
    "artifact_update",
    "artifact_get",
    "artifact_list",
    "task_create",
    "task_get",
    "task_claim",
    "task_complete",
    "task_update",
    "task_list",
    "read_file",
    "glob",
    "search_text",
    "search",
    "run_tests",
    "run_lint",
    *MEMORY_READ_TOOLS,
    *SKILL_TOOLS,
}

VERIFICATION_TOOLS = {"run_tests", "run_lint"}
ATTEMPT_HANDOFFS_KEY = "attempt_handoffs"
CONTINUATION_CHECKPOINT_KEY = "continuation_checkpoint"
MAX_ATTEMPT_HANDOFFS = 4
MAX_HANDOFF_ACTIONS = 12
EVIDENCE_PHASES = {
    "engineer_implement",
    "engineer_fix",
    "qa_verify",
    "qa_regression",
}
WORKTREE_MUTATION_TOOLS = {"write_file", "edit_file", "bash"}
WORKFLOW_ROOT_TASK_KEY = "workflow_root"
PLANNING_TASK_SPECS: dict[str, dict[str, Any]] = {
    "engineer_implement": {
        "role": "engineer",
        "owner": "engineer",
    },
    "qa_verify": {
        "role": "qa",
        "owner": "qa",
        "blocked_by": "engineer_implement",
    },
    "pm_acceptance": {
        "role": "pm_acceptance",
        "owner": "pm-acceptance",
        "blocked_by": "qa_verify",
    },
}

WORKFLOW_MCP_SCOPES: dict[str, "MCPToolScope"] = {
    "pm": "pm",
    "engineer": "engineer",
    "qa": "qa",
    "pm-acceptance": "pm_acceptance",
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
    worktree: dict[str, Any] | None = None
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
    workflow_store: WorkflowStore | None = None
    worktree_manager: WorktreeManager | None = None
    mcp_manager: MCPManager | None = None
    memory_manager: MemoryManager | None = None
    isolation: WorkflowIsolation = "worktree"
    role_specs: dict[str, RoleSpec] = field(default_factory=lambda: dict(ROLE_SPECS))
    max_fix_cycles: int = 1
    max_phase_retries: int = 1
    max_context_tokens: int = 100_000

    def __post_init__(self) -> None:
        self.workdir = Path(self.workdir).resolve()
        if self.workflow_store is None:
            self.workflow_store = WorkflowStore.for_workdir(self.workdir)
        if self.isolation not in {"shared", "worktree"}:
            raise ValueError(f"Unsupported workflow isolation: {self.isolation}")
        if self.isolation == "worktree" and self.worktree_manager is None:
            self.worktree_manager = WorktreeManager.for_workdir(self.workdir)
        missing_roles = {"pm", "engineer", "qa", "pm_acceptance"} - set(self.role_specs)
        if missing_roles:
            raise ValueError(
                "Workflow role_specs missing required role(s): "
                + ", ".join(sorted(missing_roles))
            )
        if self.max_fix_cycles < 0:
            raise ValueError("max_fix_cycles cannot be negative.")
        if self.max_phase_retries < 0:
            raise ValueError("max_phase_retries cannot be negative.")

    def _task_manager(self, workflow_id: str) -> TaskManager:
        return TaskManager.for_workdir(
            self.workdir,
            task_list_id=workflow_id,
        )

    def _workflow_task(self, workflow_id: str, task_key: str) -> Task | None:
        matches = [
            task
            for task in self._task_manager(workflow_id).list_tasks(
                include_completed=True
            )
            if str(task.metadata.get("workflow_id") or "") == workflow_id
            and str(task.metadata.get("task_key") or "") == task_key
        ]
        if len(matches) > 1:
            raise TaskSystemError(
                f"Workflow {workflow_id} has duplicate task_key={task_key!r}."
            )
        return matches[0] if matches else None

    def _ensure_workflow_root_task(self, record: WorkflowRunRecord) -> Task:
        manager = self._task_manager(record.workflow_id)
        root = self._workflow_task(record.workflow_id, WORKFLOW_ROOT_TASK_KEY)
        if root is None:
            root = manager.create_task(
                title=f"Complete workflow {record.workflow_id}",
                description=(
                    "Orchestrator-owned root for PM planning, implementation, QA, "
                    "and final acceptance."
                ),
                scope="session",
                owner="workflow-orchestrator",
                priority=0,
                metadata={
                    "workflow_id": record.workflow_id,
                    "task_key": WORKFLOW_ROOT_TASK_KEY,
                    "role": "workflow",
                },
            )
        if root.status == "pending":
            root = manager.claim_task(root.id, owner="workflow-orchestrator")
        elif root.status == "blocked":
            root = manager.update_task(
                root.id,
                status="in_progress",
                owner="workflow-orchestrator",
                notes="Workflow resumed after a blocked or failed run.",
            )
        elif root.status == "cancelled":
            raise TaskSystemError(
                f"Workflow root task is cancelled and cannot resume: {root.id}"
            )
        return root

    def _ensure_fix_cycle_tasks(
        self,
        record: WorkflowRunRecord,
        fix_cycle: int,
    ) -> tuple[Task, Task]:
        workflow_id = record.workflow_id
        manager = self._task_manager(workflow_id)
        root = self._ensure_workflow_root_task(record)
        previous_qa_key = (
            "qa_verify" if fix_cycle == 1 else f"qa_regression_{fix_cycle - 1}"
        )
        previous_qa = self._workflow_task(workflow_id, previous_qa_key)
        if previous_qa is None:
            raise TaskSystemError(
                f"Cannot create fix cycle {fix_cycle}: missing {previous_qa_key} task."
            )

        fix_key = f"engineer_fix_{fix_cycle}"
        fix_task = self._workflow_task(workflow_id, fix_key)
        if fix_task is None:
            fix_task = manager.create_task(
                title=f"Engineer fix cycle {fix_cycle}",
                description=(
                    "Resolve the actionable failures in the latest QA report, "
                    "verify the final diff, and update implementation evidence."
                ),
                scope="session",
                owner="engineer",
                parent_id=root.id,
                blocked_by=[previous_qa.id],
                priority=30 + fix_cycle * 2,
                metadata={
                    "workflow_id": workflow_id,
                    "task_key": fix_key,
                    "role": "engineer",
                },
            )

        regression_key = f"qa_regression_{fix_cycle}"
        regression_task = self._workflow_task(workflow_id, regression_key)
        if regression_task is None:
            regression_task = manager.create_task(
                title=f"QA regression cycle {fix_cycle}",
                description=(
                    "Verify the final fix-cycle diff and record the authoritative "
                    "QA verdict."
                ),
                scope="session",
                owner="qa",
                parent_id=root.id,
                blocked_by=[fix_task.id],
                priority=31 + fix_cycle * 2,
                metadata={
                    "workflow_id": workflow_id,
                    "task_key": regression_key,
                    "role": "qa",
                },
            )
        return fix_task, regression_task

    def _retarget_acceptance_task(self, record: WorkflowRunRecord) -> None:
        acceptance = self._workflow_task(record.workflow_id, "pm_acceptance")
        if acceptance is None or acceptance.status in {"completed", "cancelled"}:
            return
        latest_qa_key = (
            f"qa_regression_{record.fix_cycles}"
            if record.fix_cycles
            else "qa_verify"
        )
        latest_qa = self._workflow_task(record.workflow_id, latest_qa_key)
        if latest_qa is None:
            raise TaskSystemError(
                f"Cannot prepare acceptance: missing {latest_qa_key} task."
            )
        if acceptance.blocked_by != [latest_qa.id]:
            self._task_manager(record.workflow_id).update_task(
                acceptance.id,
                blocked_by=[latest_qa.id],
                notes=(
                    "Acceptance dependency updated to the latest authoritative "
                    f"QA phase: {latest_qa_key}."
                ),
            )

    def _close_workflow_root_task(
        self,
        workflow_id: str,
        *,
        status: WorkflowStatus,
        qa_verdict: str | None,
        error: str | None,
    ) -> None:
        try:
            root = self._workflow_task(workflow_id, WORKFLOW_ROOT_TASK_KEY)
        except TaskSystemError as exc:
            self._record_workflow_trace(
                "workflow.task_root.close_failed",
                phase="failed",
                status="warning",
                data={"workflow_id": workflow_id, "error": exc},
            )
            return
        if root is None or root.status in {"completed", "cancelled"}:
            return
        manager = self._task_manager(workflow_id)
        evidence = (
            f"workflow_status={status}; qa_verdict={qa_verdict or 'unknown'}"
        )
        if status == "completed":
            if root.status == "pending":
                root = manager.claim_task(root.id, owner="workflow-orchestrator")
            if root.status == "blocked":
                root = manager.update_task(root.id, status="in_progress")
            manager.complete_task(root.id, evidence=evidence)
            return
        manager.update_task(
            root.id,
            status="blocked",
            evidence=evidence,
            notes=(error or "Workflow did not complete successfully.")[:2_000],
        )

    def _ensure_execution_workdir(self, record: WorkflowRunRecord) -> Path:
        if self.isolation == "shared":
            return self.workdir
        if self.worktree_manager is None:
            raise WorktreeError("Workflow worktree isolation is not configured.")

        if record.worktree_id is None:
            info = self.worktree_manager.create(
                agent_id="workflow",
                run_id=record.workflow_id,
            )
            record.worktree_id = info.id
            record.worktree_base_commit = info.base_commit
            self._save_record(record)
            event_name = "workflow.worktree.created"
        else:
            self.worktree_manager.reconcile()
            info = self.worktree_manager.get(record.worktree_id)
            if info.status == "missing":
                raise WorktreeError(
                    f"Workflow worktree is missing: {record.worktree_id}"
                )
            if info.status == "applied":
                raise WorktreeError(
                    "Workflow worktree was already applied before the workflow "
                    f"finished: {record.worktree_id}"
                )
            if info.run_id not in {None, record.workflow_id}:
                raise WorktreeError(
                    f"Worktree {info.id} belongs to another run: {info.run_id}"
                )
            if (
                record.worktree_base_commit is not None
                and record.worktree_base_commit != info.base_commit
            ):
                raise WorktreeError(
                    f"Worktree base commit mismatch for {record.worktree_id}."
                )
            if record.worktree_base_commit is None:
                record.worktree_base_commit = info.base_commit
                self._save_record(record)
            event_name = "workflow.worktree.reused"

        self._record_workflow_trace(
            event_name,
            phase="ready",
            data={
                "workflow_id": record.workflow_id,
                "worktree_id": info.id,
                "base_commit": info.base_commit,
                "path": info.path,
            },
        )
        return Path(info.path).resolve()

    def _worktree_review(
        self,
        record: WorkflowRunRecord | None,
    ) -> dict[str, Any] | None:
        if (
            self.isolation != "worktree"
            or record is None
            or record.worktree_id is None
            or self.worktree_manager is None
        ):
            return None
        try:
            return self.worktree_manager.diff(record.worktree_id)
        except WorktreeError as exc:
            return {
                "worktree": {
                    "id": record.worktree_id,
                    "base_commit": record.worktree_base_commit,
                    "status": "missing",
                },
                "changed_files": [],
                "change_count": 0,
                "diff_sha256": None,
                "diff": "",
                "diff_path": None,
                "diff_truncated": False,
                "error": str(exc),
            }

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
        if self.workflow_store is None:
            raise RuntimeError("Workflow store is required.")
        record = self.workflow_store.create_run(
            workflow_id=workflow_id,
            request=request,
            initial_artifact_ids=sorted(initial_artifact_ids),
        )
        self._ensure_workflow_root_task(record)

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
            return self._execute_record(
                record,
                on_event=on_event,
                trace=trace,
            )

    def _execute_record(
        self,
        record: WorkflowRunRecord,
        *,
        on_event: AgentCallback | None,
        trace: TraceRecorder | None,
    ) -> WorkflowResult:
        workflow_id = record.workflow_id
        request = record.request
        initial_artifact_ids = set(record.initial_artifact_ids)

        pm_result = self._ensure_phase(
            record,
            phase_key="pm_plan",
            phase="pm_plan",
            role=self.role_specs["pm"],
            request=request,
            prompt=self._pm_prompt(workflow_id, request),
            gate=lambda before, _: self._gate_planning_artifacts(
                workflow_id,
                initial_artifact_ids,
                before,
            ),
            execution_workdir=self.workdir,
            on_event=on_event,
            trace=trace,
        )
        if pm_result.status == "failed":
            return self._finish(
                workflow_id,
                status="failed",
                phases=self._record_phase_results(record),
                initial_artifact_ids=initial_artifact_ids,
                error=pm_result.error,
                record=record,
            )

        try:
            execution_workdir = self._ensure_execution_workdir(record)
        except WorktreeError as exc:
            return self._finish(
                workflow_id,
                status="failed",
                phases=self._record_phase_results(record),
                initial_artifact_ids=initial_artifact_ids,
                error=f"Workflow worktree setup failed: {exc}",
                record=record,
            )

        engineer_result = self._ensure_phase(
            record,
            phase_key="engineer_implement",
            phase="engineer_implement",
            role=self.role_specs["engineer"],
            request=request,
            prompt=self._engineer_prompt(workflow_id, request, fix=False),
            gate=lambda before, data: self._gate_implementation_report(
                record,
                initial_artifact_ids,
                before,
                data,
                phase_key="engineer_implement",
            ),
            execution_workdir=execution_workdir,
            on_event=on_event,
            trace=trace,
        )
        if engineer_result.status == "failed":
            return self._finish(
                workflow_id,
                status="failed",
                phases=self._record_phase_results(record),
                initial_artifact_ids=initial_artifact_ids,
                error=engineer_result.error,
                record=record,
            )

        qa_result = self._ensure_phase(
            record,
            phase_key="qa_verify",
            phase="qa_verify",
            role=self.role_specs["qa"],
            request=request,
            prompt=self._qa_prompt(workflow_id, request, regression=False),
            gate=lambda before, data: self._gate_test_report(
                record,
                initial_artifact_ids,
                before,
                data,
                phase_key="qa_verify",
            ),
            execution_workdir=execution_workdir,
            on_event=on_event,
            trace=trace,
        )
        if qa_result.status == "failed":
            return self._finish(
                workflow_id,
                status="failed",
                phases=self._record_phase_results(record),
                initial_artifact_ids=initial_artifact_ids,
                error=qa_result.error,
                record=record,
            )
        qa_verdict = str(qa_result.data.get("qa_verdict", "unknown"))
        record.qa_verdict = qa_verdict
        self._save_record(record)

        fix_cycles = 0
        while qa_verdict == "fail" and fix_cycles < self.max_fix_cycles:
            fix_cycles += 1
            record.fix_cycles = fix_cycles
            self._save_record(record)
            self._ensure_fix_cycle_tasks(record, fix_cycles)

            fix_result = self._ensure_phase(
                record,
                phase_key=f"engineer_fix_{fix_cycles}",
                phase="engineer_fix",
                role=self.role_specs["engineer"],
                request=request,
                prompt=self._engineer_prompt(
                    workflow_id,
                    request,
                    fix=True,
                    fix_cycle=fix_cycles,
                    evidence_summary=self._format_evidence_summary(record),
                ),
                gate=lambda before, data: self._gate_implementation_report(
                    record,
                    initial_artifact_ids,
                    before,
                    data,
                    phase_key=f"engineer_fix_{fix_cycles}",
                ),
                execution_workdir=execution_workdir,
                on_event=on_event,
                trace=trace,
            )
            if fix_result.status == "failed":
                return self._finish(
                    workflow_id,
                    status="failed",
                    phases=self._record_phase_results(record),
                    initial_artifact_ids=initial_artifact_ids,
                    qa_verdict=qa_verdict,
                    fix_cycles=fix_cycles,
                    error=fix_result.error,
                    record=record,
                )

            qa_result = self._ensure_phase(
                record,
                phase_key=f"qa_regression_{fix_cycles}",
                phase="qa_regression",
                role=self.role_specs["qa"],
                request=request,
                prompt=self._qa_prompt(
                    workflow_id,
                    request,
                    regression=True,
                    fix_cycle=fix_cycles,
                ),
                gate=lambda before, data: self._gate_test_report(
                    record,
                    initial_artifact_ids,
                    before,
                    data,
                    phase_key=f"qa_regression_{fix_cycles}",
                ),
                execution_workdir=execution_workdir,
                on_event=on_event,
                trace=trace,
            )
            if qa_result.status == "failed":
                return self._finish(
                    workflow_id,
                    status="failed",
                    phases=self._record_phase_results(record),
                    initial_artifact_ids=initial_artifact_ids,
                    qa_verdict=qa_verdict,
                    fix_cycles=fix_cycles,
                    error=qa_result.error,
                    record=record,
                )
            qa_verdict = str(qa_result.data.get("qa_verdict", "unknown"))
            record.qa_verdict = qa_verdict
            record.fix_cycles = fix_cycles
            self._save_record(record)

        self._retarget_acceptance_task(record)
        acceptance_result = self._ensure_phase(
            record,
            phase_key="pm_acceptance",
            phase="pm_acceptance",
            role=self.role_specs["pm_acceptance"],
            request=request,
            prompt=self._acceptance_prompt(
                workflow_id,
                request,
                qa_verdict=qa_verdict,
                evidence_summary=self._format_evidence_summary(record),
            ),
            gate=lambda before, _: self._gate_acceptance_report(
                workflow_id,
                initial_artifact_ids,
                before,
                qa_verdict=qa_verdict,
                phase_key="pm_acceptance",
            ),
            execution_workdir=execution_workdir,
            on_event=on_event,
            trace=trace,
        )
        if acceptance_result.status == "failed":
            return self._finish(
                workflow_id,
                status="failed",
                phases=self._record_phase_results(record),
                initial_artifact_ids=initial_artifact_ids,
                qa_verdict=qa_verdict,
                fix_cycles=fix_cycles,
                error=acceptance_result.error,
                record=record,
            )

        status: WorkflowStatus = "completed" if qa_verdict == "pass" else "failed"
        error = None if status == "completed" else "QA verdict did not pass."
        return self._finish(
            workflow_id,
            status=status,
            phases=self._record_phase_results(record),
            initial_artifact_ids=initial_artifact_ids,
            qa_verdict=qa_verdict,
            fix_cycles=fix_cycles,
            error=error,
            record=record,
        )

    def _ensure_phase(
        self,
        record: WorkflowRunRecord,
        *,
        phase_key: str,
        phase: WorkflowPhase,
        role: RoleSpec,
        request: str,
        prompt: str,
        gate: Callable[[dict[str, int], dict[str, Any]], _GateResult],
        execution_workdir: Path,
        on_event: AgentCallback | None,
        trace: TraceRecorder | None,
    ) -> WorkflowPhaseResult:
        completed_result = self._record_phase_result(record, phase_key)
        if (
            completed_result is not None
            and self._phase_result_is_trusted(
                record,
                phase_key,
                completed_result,
            )
        ):
            return completed_result

        checkpoint = record.checkpoints.get(phase_key)
        before_versions = checkpoint.before_versions if checkpoint else None
        if checkpoint is not None:
            gate_result = gate(
                checkpoint.before_versions,
                dict(checkpoint.data),
            )
            if (
                gate_result.ok
                and checkpoint.status == "completed"
                and checkpoint.data.get("agent_status") == "completed"
            ):
                recovered_data = {
                    **checkpoint.data,
                    **gate_result.data,
                }
                recovered = WorkflowPhaseResult(
                    phase=phase,
                    role=role.name,
                    status="completed",
                    run_ids=list(checkpoint.run_ids),
                    artifact_ids=gate_result.artifact_ids,
                    summary="Recovered completed phase from persisted checkpoint.",
                    attempts=max(1, checkpoint.attempts),
                    data=recovered_data,
                )
                self._record_workflow_trace(
                    "workflow.phase.recovered",
                    phase="completed",
                    data={
                        "workflow_id": record.workflow_id,
                        "phase_key": phase_key,
                        "phase": phase,
                        "role": role.name,
                        "artifact_ids": gate_result.artifact_ids,
                        **self._trace_phase_data(recovered_data),
                    },
                )
                self._persist_phase_completed(record, phase_key, recovered)
                return recovered

        return self._run_phase(
            phase=phase,
            phase_key=phase_key,
            role=role,
            workflow_id=record.workflow_id,
            request=request,
            prompt=prompt,
            gate=gate,
            execution_workdir=execution_workdir,
            on_event=on_event,
            trace=trace,
            record=record,
            before_versions=before_versions,
        )

    def resume(
        self,
        workflow_id: str,
        *,
        on_event: AgentCallback | None = None,
        trace: TraceRecorder | None = None,
    ) -> WorkflowResult:
        if self.workflow_store is None:
            raise RuntimeError("Workflow store is required.")
        record = self.workflow_store.load_run(workflow_id)
        if record.status == "completed" and self._completed_record_is_trusted(record):
            return self._record_to_result(record)
        if record.status == "completed":
            self._invalidate_untrusted_phase_suffix(record)
        self._ensure_workflow_root_task(record)

        with trace_scope(
            trace,
            run_id=workflow_id,
            agent_id="workflow-orchestrator",
        ):
            self._record_workflow_trace(
                "workflow.resumed",
                phase="started",
                data={
                    "workflow_id": workflow_id,
                    "previous_status": record.status,
                    "request": summarize_text(record.request),
                },
            )
            return self._execute_record(
                record,
                on_event=on_event,
                trace=trace,
            )

    def _run_phase(
        self,
        *,
        phase: WorkflowPhase,
        phase_key: str,
        role: RoleSpec,
        workflow_id: str,
        request: str,
        prompt: str,
        gate: Callable[[dict[str, int], dict[str, Any]], _GateResult],
        execution_workdir: Path,
        on_event: AgentCallback | None,
        trace: TraceRecorder | None,
        record: WorkflowRunRecord | None = None,
        before_versions: dict[str, int] | None = None,
    ) -> WorkflowPhaseResult:
        if before_versions is None:
            before_versions = self._artifact_versions()
        run_ids: list[str] = []
        last_summary = ""
        last_gate = _GateResult(ok=False, message="Phase did not run.")
        phase_data = self._phase_checkpoint_data(record, phase_key)
        handoffs = self._attempt_handoffs(phase_data)
        if not handoffs:
            interrupted_handoff = self._interrupted_attempt_handoff(phase_data)
            if interrupted_handoff is not None:
                interrupted_handoff = self._build_attempt_handoff(
                    record=record,
                    phase=phase,
                    phase_key=phase_key,
                    attempt=int(interrupted_handoff["attempt"]),
                    run_id=str(interrupted_handoff.get("run_id") or ""),
                    agent_status="interrupted",
                    summary=str(interrupted_handoff.get("summary") or ""),
                    gate_issue=str(interrupted_handoff.get("gate_issue") or ""),
                    artifact_ids=[],
                    phase_data=phase_data,
                    attempt_state=dict(
                        phase_data.get("current_attempt_state") or {}
                    ),
                )
                phase_data = self._append_attempt_handoff(
                    phase_data,
                    interrupted_handoff,
                )
                handoffs = [interrupted_handoff]
        latest_handoff = handoffs[-1] if handoffs else None
        prior_role_handoff = self._latest_role_handoff(
            record,
            role_name=role.name,
            exclude_phase_key=phase_key,
        )
        checkpoint = record.checkpoints.get(phase_key) if record is not None else None
        previous_attempts = max(
            [
                int(phase_data.get("current_attempt") or 0),
                int(checkpoint.attempts if checkpoint is not None else 0),
                *(int(item.get("attempt") or 0) for item in handoffs),
            ],
        )
        retry_issue = self._handoff_issue(latest_handoff) or (
            checkpoint.error if checkpoint is not None else None
        )
        if (
            self.isolation == "worktree"
            and phase in EVIDENCE_PHASES
            and "worktree_before" not in phase_data
        ):
            phase_data["worktree_before"] = self._worktree_snapshot(record)
        if record is not None and self.workflow_store is not None:
            self.workflow_store.mark_phase_started(
                record,
                phase_key=phase_key,
                phase=phase,
                role=role.name,
                before_versions=before_versions,
                data=phase_data,
            )

        self._record_workflow_trace(
            "workflow.phase.started",
            phase="started",
            data={
                "workflow_id": workflow_id,
                "phase_key": phase_key,
                "phase": phase,
                "role": role.name,
            },
        )

        for local_attempt in range(1, self.max_phase_retries + 2):
            attempt = previous_attempts + local_attempt
            agent = self._build_role_agent(
                role,
                workflow_id=workflow_id,
                phase=phase,
                request=request,
                execution_workdir=execution_workdir,
            )
            messages = agent.new_messages()
            prompt_parts = [prompt]
            task_contract = self._format_phase_task_contract(
                workflow_id,
                phase_key,
            )
            if task_contract:
                prompt_parts.append(task_contract)
            if (
                latest_handoff is None
                and local_attempt == 1
                and prior_role_handoff is not None
            ):
                prompt_parts.append(
                    self._format_prior_role_checkpoint(prior_role_handoff)
                )
            if retry_issue is not None:
                prompt_parts.append(
                    self._corrective_prompt(phase, retry_issue, latest_handoff)
                )
            attempt_prompt = "\n\n".join(prompt_parts)
            messages.append({"role": "user", "content": attempt_prompt})

            phase_run_id = f"{workflow_id}-{phase_key}-{attempt}"
            run_ids.append(phase_run_id)
            self._update_phase_checkpoint_data(
                record,
                phase_key,
                {
                    "current_attempt": attempt,
                    "current_run_id": phase_run_id,
                },
            )
            attempt_state: dict[str, Any] = {
                "tool_counts": {},
                "recent_actions": [],
                "recent_failures": [],
                "progress": [],
                "artifact_snapshots": [],
            }

            def phase_on_event(event: AgentEvent) -> None:
                if self._capture_attempt_event(
                    attempt_state,
                    event,
                    record=record,
                ):
                    self._update_phase_checkpoint_data(
                        record,
                        phase_key,
                        {"current_attempt_state": attempt_state},
                    )
                self._capture_verification_event(
                    record,
                    phase_key,
                    attempt,
                    event,
                )
                if on_event is not None:
                    on_event(event)

            if retry_issue is not None and latest_handoff is not None:
                restored_files = self._handoff_changed_files(latest_handoff)
                self._record_workflow_trace(
                    "workflow.working_context.injected",
                    phase="event",
                    data={
                        "workflow_id": workflow_id,
                        "phase_key": phase_key,
                        "phase": phase,
                        "attempt": attempt,
                        "source_phase_key": phase_key,
                        "source_run_id": latest_handoff.get("run_id"),
                        "source_attempt": latest_handoff.get("attempt"),
                        "changed_files": restored_files,
                        "gate_issue": retry_issue,
                        "context_kind": "attempt_checkpoint",
                    },
                )
                if on_event is not None:
                    on_event(
                        AgentEvent(
                            type="progress",
                            step=0,
                            data={
                                "content": (
                                    "Restored working context from attempt "
                                    f"{latest_handoff.get('attempt', '?')}: "
                                    f"{len(restored_files)} changed file(s). "
                                    "Continuing from the existing Worktree."
                                )
                            },
                            agent_id=role.agent_id,
                            run_id=phase_run_id,
                            parent_run_id=workflow_id,
                            depth=1,
                        )
                    )

            if (
                latest_handoff is None
                and local_attempt == 1
                and prior_role_handoff is not None
            ):
                prior_checkpoint = prior_role_handoff.get("checkpoint")
                changed_files = (
                    list(prior_checkpoint.get("changed_files") or [])
                    if isinstance(prior_checkpoint, dict)
                    else []
                )
                self._record_workflow_trace(
                    "workflow.working_context.injected",
                    phase="event",
                    data={
                        "workflow_id": workflow_id,
                        "phase_key": phase_key,
                        "phase": phase,
                        "attempt": attempt,
                        "source_phase_key": prior_role_handoff.get(
                            "source_phase_key"
                        ),
                        "source_run_id": prior_role_handoff.get("run_id"),
                        "source_attempt": prior_role_handoff.get("attempt"),
                        "changed_files": changed_files,
                        "context_kind": "prior_role_checkpoint",
                    },
                )

            try:
                result = agent.run(
                    messages,
                    on_event=phase_on_event,
                    run_id=phase_run_id,
                    trace=trace,
                )
            except Exception as exc:
                return self._fail_phase_after_agent_error(
                    record=record,
                    phase=phase,
                    phase_key=phase_key,
                    role=role,
                    workflow_id=workflow_id,
                    attempt=attempt,
                    run_ids=run_ids,
                    summary=last_summary,
                    error=exc,
                    run_id=phase_run_id,
                    attempt_state=attempt_state,
                )
            last_summary = result.content
            phase_data = self._phase_checkpoint_data(record, phase_key)
            gate_result = gate(before_versions, phase_data)
            if result.status != "completed":
                artifact_gate_ok = gate_result.ok
                gate_message = f"Role agent ended with status={result.status}; "
                if artifact_gate_ok:
                    gate_message += (
                        "phase continuation is required before completion even "
                        "though the artifact evidence gate passed."
                    )
                else:
                    gate_message += f"evidence gate: {gate_result.message}"
                gate_result = _GateResult(
                    ok=False,
                    message=gate_message,
                    artifact_ids=gate_result.artifact_ids,
                    data={
                        **gate_result.data,
                        "agent_status": result.status,
                        "artifact_gate_ok": artifact_gate_ok,
                        "continuation_reason": result.status,
                    },
                )
            if "gate_warning" not in gate_result.data:
                phase_data.pop("gate_warning", None)
            phase_data = {
                **phase_data,
                **gate_result.data,
                "agent_status": result.status,
            }
            if result.status == "completed" and gate_result.ok:
                phase_data.pop("artifact_gate_ok", None)
                phase_data.pop("continuation_reason", None)
            elif result.status == "completed":
                phase_data["artifact_gate_ok"] = False
                phase_data["continuation_reason"] = "phase_retry"
            handoff = self._build_attempt_handoff(
                record=record,
                phase=phase,
                phase_key=phase_key,
                attempt=attempt,
                run_id=phase_run_id,
                agent_status=result.status,
                summary=last_summary,
                gate_issue=None if gate_result.ok else gate_result.message,
                artifact_ids=gate_result.artifact_ids,
                phase_data=phase_data,
                attempt_state=attempt_state,
            )
            phase_data = self._append_attempt_handoff(phase_data, handoff)
            self._replace_phase_checkpoint_data(record, phase_key, phase_data)
            latest_handoff = handoff
            last_gate = _GateResult(
                ok=gate_result.ok,
                message=gate_result.message,
                artifact_ids=gate_result.artifact_ids,
                data=phase_data,
            )
            if last_gate.ok:
                self._record_workflow_trace(
                    "workflow.phase.completed",
                    phase="completed",
                    status=(
                        "ok"
                        if result.status == "completed"
                        and not last_gate.data.get("gate_warning")
                        else "warning"
                    ),
                    data={
                        "workflow_id": workflow_id,
                        "phase_key": phase_key,
                        "phase": phase,
                        "role": role.name,
                        "attempts": attempt,
                        "artifact_ids": last_gate.artifact_ids,
                        **self._trace_phase_data(last_gate.data),
                    },
                )
                phase_result = WorkflowPhaseResult(
                    phase=phase,
                    role=role.name,
                    status="completed",
                    run_ids=run_ids,
                    artifact_ids=last_gate.artifact_ids,
                    summary=last_summary,
                    attempts=attempt,
                    data=last_gate.data,
                )
                self._persist_phase_completed(record, phase_key, phase_result)
                return phase_result

            if local_attempt <= self.max_phase_retries:
                retry_issue = last_gate.message

        self._record_workflow_trace(
            "workflow.phase.failed",
            phase="failed",
            status="error",
            data={
                "workflow_id": workflow_id,
                "phase_key": phase_key,
                "phase": phase,
                "role": role.name,
                "attempts": previous_attempts + self.max_phase_retries + 1,
                "error": last_gate.message,
            },
        )
        failed_result = WorkflowPhaseResult(
            phase=phase,
            role=role.name,
            status="failed",
            run_ids=run_ids,
            artifact_ids=last_gate.artifact_ids,
            summary=last_summary,
            attempts=previous_attempts + self.max_phase_retries + 1,
            error=last_gate.message,
            data=last_gate.data,
        )
        self._persist_phase_failed(record, phase_key, failed_result)
        return failed_result

    def _fail_phase_after_agent_error(
        self,
        *,
        record: WorkflowRunRecord | None,
        phase: WorkflowPhase,
        phase_key: str,
        role: RoleSpec,
        workflow_id: str,
        attempt: int,
        run_ids: list[str],
        summary: str,
        error: Exception,
        run_id: str,
        attempt_state: dict[str, Any],
    ) -> WorkflowPhaseResult:
        message = f"Role agent failed: {type(error).__name__}: {error}"
        if error.__cause__ is not None:
            message += (
                f"; caused by {type(error.__cause__).__name__}: {error.__cause__}"
            )
        phase_data = self._phase_checkpoint_data(record, phase_key)
        handoff = self._build_attempt_handoff(
            record=record,
            phase=phase,
            phase_key=phase_key,
            attempt=attempt,
            run_id=run_id,
            agent_status="failed",
            summary=summary,
            gate_issue=message,
            artifact_ids=[],
            phase_data=phase_data,
            attempt_state=attempt_state,
        )
        phase_data = self._append_attempt_handoff(phase_data, handoff)
        self._replace_phase_checkpoint_data(record, phase_key, phase_data)
        self._record_workflow_trace(
            "workflow.phase.failed",
            phase="failed",
            status="error",
            data={
                "workflow_id": workflow_id,
                "phase_key": phase_key,
                "phase": phase,
                "role": role.name,
                "attempts": attempt,
                "error": error,
            },
        )
        result = WorkflowPhaseResult(
            phase=phase,
            role=role.name,
            status="failed",
            run_ids=list(run_ids),
            artifact_ids=[],
            summary=summary,
            attempts=attempt,
            error=message,
            data=phase_data,
        )
        self._persist_phase_failed(record, phase_key, result)
        return result

    def _worktree_snapshot(
        self,
        record: WorkflowRunRecord | None,
    ) -> dict[str, Any]:
        if (
            record is None
            or record.worktree_id is None
            or self.worktree_manager is None
        ):
            raise WorktreeError("Workflow worktree evidence is unavailable.")
        review = self.worktree_manager.diff(record.worktree_id)
        worktree = review["worktree"]
        return {
            "worktree_id": worktree["id"],
            "base_commit": worktree["base_commit"],
            "diff_sha256": review["diff_sha256"],
            "changed_files": list(review["changed_files"]),
            "change_count": review["change_count"],
        }

    @staticmethod
    def _phase_checkpoint_data(
        record: WorkflowRunRecord | None,
        phase_key: str,
    ) -> dict[str, Any]:
        if record is None:
            return {}
        checkpoint = record.checkpoints.get(phase_key)
        return dict(checkpoint.data) if checkpoint is not None else {}

    @staticmethod
    def _trace_phase_data(data: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in data.items()
            if key not in {ATTEMPT_HANDOFFS_KEY, "current_attempt_state"}
        }

    def _update_phase_checkpoint_data(
        self,
        record: WorkflowRunRecord | None,
        phase_key: str,
        updates: dict[str, Any],
    ) -> None:
        data = self._phase_checkpoint_data(record, phase_key)
        data.update(updates)
        self._replace_phase_checkpoint_data(record, phase_key, data)

    def _replace_phase_checkpoint_data(
        self,
        record: WorkflowRunRecord | None,
        phase_key: str,
        data: dict[str, Any],
    ) -> None:
        if record is None or self.workflow_store is None:
            return
        checkpoint = record.checkpoints.get(phase_key)
        if checkpoint is None:
            return
        checkpoint.data = dict(data)
        self.workflow_store.save_run(record)

    def _capture_verification_event(
        self,
        record: WorkflowRunRecord | None,
        phase_key: str,
        attempt: int,
        event: AgentEvent,
    ) -> None:
        if event.type != "tool_result":
            return
        tool_name = str(event.data.get("name", ""))
        if tool_name not in VERIFICATION_TOOLS:
            return

        result = event.data.get("result")
        outer = result if isinstance(result, dict) else {}
        inner = outer.get("result") if outer.get("ok") is True else None
        payload = inner if isinstance(inner, dict) else {}
        evidence: dict[str, Any] = {
            "tool": tool_name,
            "attempt": attempt,
            "run_id": event.run_id,
            "step": event.step,
            "outcome": (
                str(payload.get("outcome", "unknown"))
                if outer.get("ok") is True
                else "tool_error"
            ),
        }
        for key in (
            "exit_code",
            "timed_out",
            "duration_ms",
            "report_path",
            "execution_environment",
            "error_kind",
            "diagnostic",
        ):
            if payload.get(key) is not None:
                value = payload[key]
                evidence[key] = value[:2_000] if isinstance(value, str) else value
        missing_modules = payload.get("missing_modules")
        if isinstance(missing_modules, list):
            evidence["missing_modules"] = [
                str(module)[:200] for module in missing_modules[:20]
            ]
        if payload.get("summary") is not None:
            evidence["summary"] = str(payload["summary"])[:1_000]
        failures = payload.get("failures")
        if isinstance(failures, list):
            evidence["failures"] = [
                {
                    key: str(item[key])[:500]
                    for key in ("test", "classname", "kind", "message")
                    if item.get(key) is not None
                }
                for item in failures[:20]
                if isinstance(item, dict)
            ]
        if outer.get("ok") is not True and outer.get("error") is not None:
            evidence["error"] = str(outer["error"])[:1_000]
        if event.duration_ms is not None:
            evidence["event_duration_ms"] = event.duration_ms
        if self.isolation == "worktree":
            try:
                snapshot = self._worktree_snapshot(record)
            except WorktreeError as exc:
                evidence["worktree_error"] = str(exc)[:1_000]
            else:
                evidence["diff_sha256"] = snapshot.get("diff_sha256")
                evidence["changed_files"] = list(
                    snapshot.get("changed_files") or []
                )

        data = self._phase_checkpoint_data(record, phase_key)
        verification = list(data.get("verification") or [])
        verification.append(evidence)
        data["verification"] = verification
        self._replace_phase_checkpoint_data(record, phase_key, data)

    def _capture_attempt_event(
        self,
        state: dict[str, Any],
        event: AgentEvent,
        *,
        record: WorkflowRunRecord | None,
    ) -> bool:
        if event.type == "tool_call":
            tool_name = str(event.data.get("name", "unknown"))
            counts = state.setdefault("tool_counts", {})
            counts[tool_name] = int(counts.get(tool_name, 0)) + 1
            action: dict[str, Any] = {
                "step": event.step,
                "tool": tool_name,
            }
            target = SerialCodingWorkflow._tool_action_target(
                event.data.get("arguments")
            )
            if target:
                action["target"] = target
            actions = state.setdefault("recent_actions", [])
            actions.append(action)
            del actions[:-MAX_HANDOFF_ACTIONS]
            if tool_name in {"artifact_create", "artifact_update"}:
                arguments = event.data.get("arguments")
                artifact_call: dict[str, Any] = {"tool": tool_name}
                if isinstance(arguments, dict):
                    for key in ("artifact_id", "kind"):
                        if arguments.get(key) is not None:
                            artifact_call[key] = str(arguments[key])[:300]
                pending = state.setdefault("pending_artifact_calls", {})
                pending[str(event.data.get("id") or "")] = artifact_call
            return True

        if event.type == "tool_result":
            result = event.data.get("result")
            tool_name = str(event.data.get("name", "unknown"))
            pending_calls = state.setdefault("pending_artifact_calls", {})
            pending = pending_calls.pop(
                str(event.data.get("id") or ""),
                None,
            )
            if not isinstance(result, dict):
                return bool(pending)
            if result.get("ok") is False:
                failures = state.setdefault("recent_failures", [])
                failures.append(
                    {
                        "step": event.step,
                        "tool": tool_name,
                        "error": str(result.get("error", "tool failed"))[:500],
                    }
                )
                del failures[:-6]
                return True
            if result.get("ok") is not True:
                return bool(pending)

            changed = bool(pending)
            if tool_name in WORKTREE_MUTATION_TOOLS and self.isolation == "worktree":
                try:
                    mutation_snapshot = self._worktree_snapshot(record)
                except WorktreeError:
                    mutation_snapshot = None
                if mutation_snapshot is not None:
                    state["last_mutation"] = {
                        "step": event.step,
                        "tool": tool_name,
                        "run_id": event.run_id,
                        "diff_sha256": mutation_snapshot.get("diff_sha256"),
                        "changed_files": list(
                            mutation_snapshot.get("changed_files") or []
                        ),
                    }
                    changed = True

            if isinstance(pending, dict):
                artifact_id = str(pending.get("artifact_id") or "").strip()
                if not artifact_id:
                    artifact_id = self._artifact_id_from_tool_result(result)
                if artifact_id:
                    try:
                        artifact = self.artifact_manager.get_artifact(artifact_id)
                    except Exception:
                        artifact = None
                    if artifact is not None:
                        diff_sha256: str | None = None
                        if self.isolation == "worktree":
                            try:
                                snapshot = self._worktree_snapshot(record)
                            except WorktreeError:
                                snapshot = None
                            if snapshot is not None:
                                diff_sha256 = str(
                                    snapshot.get("diff_sha256") or ""
                                ) or None
                        snapshots = state.setdefault("artifact_snapshots", [])
                        snapshots.append(
                            {
                                "artifact_id": artifact.id,
                                "kind": artifact.kind,
                                "version": artifact.version,
                                "run_id": event.run_id,
                                "step": event.step,
                                "diff_sha256": diff_sha256,
                            }
                        )
                        del snapshots[:-12]
                        changed = True
            return changed

        if event.type != "progress":
            return False
        content = str(event.data.get("content", "")).strip()
        if not content or content.startswith(("Six model turns", "This is the final")):
            return False
        progress = state.setdefault("progress", [])
        progress.append(content[:800])
        del progress[:-4]
        return True

    @staticmethod
    def _artifact_id_from_tool_result(result: dict[str, Any]) -> str:
        payload = result.get("result")
        if not isinstance(payload, str):
            return ""
        for line in payload.splitlines():
            candidate = line.strip().split(" ", 1)[0]
            if candidate.startswith("artifact_"):
                return candidate
        return ""

    @staticmethod
    def _tool_action_target(arguments: Any) -> dict[str, str]:
        if not isinstance(arguments, dict):
            return {}
        target: dict[str, str] = {}
        for key in (
            "path",
            "query",
            "pattern",
            "kind",
            "title",
            "task_id",
            "artifact_id",
            "memory_id",
        ):
            value = arguments.get(key)
            if isinstance(value, (str, int, float)) and str(value).strip():
                target[key] = str(value).strip()[:300]
        return target

    def _build_attempt_handoff(
        self,
        *,
        record: WorkflowRunRecord | None,
        phase: WorkflowPhase,
        phase_key: str,
        attempt: int,
        run_id: str,
        agent_status: str,
        summary: str,
        gate_issue: str | None,
        artifact_ids: list[str],
        phase_data: dict[str, Any],
        attempt_state: dict[str, Any],
    ) -> dict[str, Any]:
        verification = [
            dict(item)
            for item in phase_data.get("verification") or []
            if isinstance(item, dict) and int(item.get("attempt") or 0) == attempt
        ]
        snapshot: dict[str, Any] | None = None
        if self.isolation == "worktree" and phase in EVIDENCE_PHASES:
            try:
                snapshot = self._worktree_snapshot(record)
            except WorktreeError:
                snapshot = None

        task_snapshot: list[Task] = []
        if record is not None:
            try:
                task_snapshot = self._task_manager(record.workflow_id).list_tasks(
                    include_completed=False
                )
            except TaskSystemError:
                task_snapshot = []
        continuation_reason = (
            agent_status
            if agent_status != "completed"
            else ("phase_retry" if gate_issue else "phase_completed")
        )
        checkpoint = build_attempt_checkpoint(
            phase_key=phase_key,
            attempt=attempt,
            run_id=run_id,
            agent_status=agent_status,
            continuation_reason=continuation_reason,
            objective=(
                f"{phase_key}: {record.request}" if record is not None else phase_key
            ),
            summary=summary,
            attempt_state=attempt_state,
            verification=verification,
            worktree_snapshot=snapshot,
            task_snapshot=task_snapshot,
            artifact_ids=artifact_ids,
            semantic=(
                {
                    "remaining": [gate_issue],
                    "next_actions": [gate_issue],
                }
                if gate_issue
                else None
            ),
        ).to_dict()

        handoff: dict[str, Any] = {
            "phase_key": phase_key,
            "attempt": attempt,
            "run_id": run_id,
            "agent_status": agent_status,
            "summary": summary.strip()[:2_000],
            "gate_issue": gate_issue.strip()[:2_000] if gate_issue else None,
            "artifact_ids": list(artifact_ids),
            "tool_counts": dict(attempt_state.get("tool_counts") or {}),
            "recent_actions": list(attempt_state.get("recent_actions") or []),
            "recent_failures": list(attempt_state.get("recent_failures") or []),
            "progress": list(attempt_state.get("progress") or []),
            "verification": verification,
            "artifact_snapshots": list(
                attempt_state.get("artifact_snapshots") or []
            )[-12:],
            "checkpoint": checkpoint,
        }
        if snapshot is not None:
            handoff["worktree"] = snapshot
        self._record_workflow_trace(
            "workflow.attempt.handoff",
            phase="checkpointed",
            status="warning" if gate_issue else "ok",
            data={
                "workflow_id": record.workflow_id if record is not None else None,
                "workflow_phase": phase,
                "attempt": attempt,
                "run_id": run_id,
                "agent_status": agent_status,
                "gate_issue": gate_issue,
                "tool_counts": handoff["tool_counts"],
                "changed_files": (
                    snapshot.get("changed_files", []) if snapshot is not None else []
                ),
                "completed": checkpoint["completed"],
                "remaining": checkpoint["remaining"],
                "next_actions": checkpoint["next_actions"],
                "open_task_ids": [
                    task.get("id") for task in checkpoint["open_tasks"]
                ],
            },
        )
        return handoff

    @staticmethod
    def _append_attempt_handoff(
        phase_data: dict[str, Any],
        handoff: dict[str, Any],
    ) -> dict[str, Any]:
        updated = dict(phase_data)
        handoffs = SerialCodingWorkflow._attempt_handoffs(updated)
        handoffs.append(dict(handoff))
        updated[ATTEMPT_HANDOFFS_KEY] = handoffs[-MAX_ATTEMPT_HANDOFFS:]
        checkpoint = handoff.get("checkpoint")
        if isinstance(checkpoint, dict):
            updated[CONTINUATION_CHECKPOINT_KEY] = dict(checkpoint)
        updated.pop("current_attempt_state", None)
        return updated

    @staticmethod
    def _attempt_handoffs(phase_data: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            dict(item)
            for item in phase_data.get(ATTEMPT_HANDOFFS_KEY) or []
            if isinstance(item, dict)
        ]

    @staticmethod
    def _interrupted_attempt_handoff(
        phase_data: dict[str, Any],
    ) -> dict[str, Any] | None:
        state = phase_data.get("current_attempt_state")
        attempt = int(phase_data.get("current_attempt") or 0)
        if not isinstance(state, dict) or attempt <= 0:
            return None
        if not any(
            state.get(key)
            for key in (
                "tool_counts",
                "recent_actions",
                "recent_failures",
                "progress",
            )
        ):
            return None
        verification = [
            dict(item)
            for item in phase_data.get("verification") or []
            if isinstance(item, dict) and int(item.get("attempt") or 0) == attempt
        ]
        return {
            "attempt": attempt,
            "run_id": str(phase_data.get("current_run_id") or ""),
            "agent_status": "interrupted",
            "summary": "",
            "gate_issue": (
                "The previous attempt was interrupted before it could satisfy the "
                "phase evidence gate."
            ),
            "artifact_ids": [],
            "tool_counts": dict(state.get("tool_counts") or {}),
            "recent_actions": list(state.get("recent_actions") or []),
            "recent_failures": list(state.get("recent_failures") or []),
            "progress": list(state.get("progress") or []),
            "verification": verification,
        }

    @staticmethod
    def _handoff_issue(handoff: dict[str, Any] | None) -> str | None:
        if handoff is None:
            return None
        issue = str(handoff.get("gate_issue") or "").strip()
        return issue or None

    @staticmethod
    def _latest_role_handoff(
        record: WorkflowRunRecord | None,
        *,
        role_name: str,
        exclude_phase_key: str,
    ) -> dict[str, Any] | None:
        if record is None:
            return None
        for phase_result in reversed(record.phases):
            phase_key = str(phase_result.get("phase_key") or "")
            if phase_key == exclude_phase_key:
                continue
            if str(phase_result.get("role") or "") != role_name:
                continue
            data = phase_result.get("data")
            if not isinstance(data, dict):
                continue
            handoffs = SerialCodingWorkflow._attempt_handoffs(data)
            if not handoffs:
                continue
            handoff = dict(handoffs[-1])
            handoff["source_phase_key"] = phase_key
            return handoff
        return None

    def _format_phase_task_contract(
        self,
        workflow_id: str,
        phase_key: str,
    ) -> str:
        if phase_key == "pm_plan":
            return ""
        task = self._workflow_task(workflow_id, phase_key)
        if task is None:
            return (
                "<workflow_task>\n"
                f"No task with metadata.task_key={phase_key!r} exists in this "
                "workflow Task list. Report this as a blocker; never guess a task "
                "id from another run.\n"
                "</workflow_task>"
            )
        return (
            "<workflow_task>\n"
            f"Task list id: {workflow_id}\n"
            f"Required phase task: {task.id}\n"
            f"Task key: {phase_key}\n"
            f"Current status: {task.status}\n"
            "Use task_get to read it. If pending, claim it as this role before "
            "work. If already in_progress, continue it without creating a duplicate. "
            "Before the phase can pass, complete it with concrete artifact and "
            "verification evidence. If it is already completed after a continuation, "
            "do not reopen or duplicate it; finish the remaining artifact/final-response "
            "contract only.\n"
            "</workflow_task>"
        )

    @staticmethod
    def _format_prior_role_checkpoint(handoff: dict[str, Any]) -> str:
        checkpoint = handoff.get("checkpoint")
        if not isinstance(checkpoint, dict):
            checkpoint = {
                "phase_key": handoff.get("source_phase_key"),
                "attempt": handoff.get("attempt"),
                "run_id": handoff.get("run_id"),
                "agent_status": handoff.get("agent_status"),
                "summary": handoff.get("summary"),
                "changed_files": SerialCodingWorkflow._handoff_changed_files(
                    handoff
                ),
            }
        payload = {
            "source_phase_key": handoff.get("source_phase_key"),
            **checkpoint,
        }
        return (
            "<prior_role_checkpoint>\n"
            "This is the latest bounded checkpoint from your previous workflow "
            "phase. Continue from it and the current workspace; do not repeat broad "
            "repository discovery. Fresh files, tasks, artifacts, and tool evidence "
            "remain authoritative.\n"
            f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n"
            "</prior_role_checkpoint>"
        )

    @staticmethod
    def _latest_artifact_snapshot(
        phase_data: dict[str, Any],
        artifact_id: str,
        *,
        normalized_key: str,
    ) -> dict[str, Any] | None:
        candidates: list[dict[str, Any]] = []
        normalized = phase_data.get(normalized_key)
        if isinstance(normalized, dict):
            candidates.append(dict(normalized))
        current_state = phase_data.get("current_attempt_state")
        if isinstance(current_state, dict):
            candidates.extend(
                dict(item)
                for item in current_state.get("artifact_snapshots") or []
                if isinstance(item, dict)
            )
        for handoff in SerialCodingWorkflow._attempt_handoffs(phase_data):
            candidates.extend(
                dict(item)
                for item in handoff.get("artifact_snapshots") or []
                if isinstance(item, dict)
            )
        matches = [
            item
            for item in candidates
            if str(item.get("artifact_id") or "") == artifact_id
        ]
        if not matches:
            return None
        return max(
            enumerate(matches),
            key=lambda pair: (int(pair[1].get("version") or 0), pair[0]),
        )[1]

    def _gate_phase_task(
        self,
        workflow_id: str,
        phase_key: str,
    ) -> _GateResult:
        try:
            task = self._workflow_task(workflow_id, phase_key)
        except TaskSystemError as exc:
            return _GateResult(ok=False, message=str(exc))
        if task is None:
            return _GateResult(
                ok=False,
                message=(
                    f"Missing workflow task with metadata.task_key={phase_key!r}."
                ),
            )
        expected_owner = (
            "pm-acceptance"
            if phase_key == "pm_acceptance"
            else ("qa" if phase_key.startswith("qa_") else "engineer")
        )
        if task.owner != expected_owner:
            return _GateResult(
                ok=False,
                message=(
                    f"Workflow task {task.id} for {phase_key} must be owned by "
                    f"{expected_owner!r}, got {task.owner!r}."
                ),
                data={"phase_task_id": task.id, "phase_task_status": task.status},
            )
        if task.status != "completed":
            return _GateResult(
                ok=False,
                message=(
                    f"Workflow task {task.id} for {phase_key} is {task.status}; "
                    "claim/continue it and complete it with evidence."
                ),
                data={"phase_task_id": task.id, "phase_task_status": task.status},
            )
        if not str(task.evidence or "").strip():
            return _GateResult(
                ok=False,
                message=f"Completed workflow task {task.id} has no evidence.",
                data={"phase_task_id": task.id, "phase_task_status": task.status},
            )
        return _GateResult(
            ok=True,
            message="ok",
            data={"phase_task_id": task.id, "phase_task_status": task.status},
        )

    def _build_role_agent(
        self,
        role: RoleSpec,
        *,
        workflow_id: str,
        phase: WorkflowPhase,
        request: str,
        execution_workdir: Path,
    ) -> Agent:
        mcp_scope = WORKFLOW_MCP_SCOPES.get(role.agent_id)
        mcp_manager = self.mcp_manager if mcp_scope is not None else None
        allowed_tools = set(role.tool_names)
        if mcp_manager is not None and mcp_scope is not None:
            allowed_tools.update(mcp_manager.tool_names_for_scope(mcp_scope))
        registry = build_default_registry(
            workdir=execution_workdir,
            task_workdir=self.workdir,
            task_list_id=workflow_id,
            artifact_manager=self.artifact_manager,
            skill_registry=self.skill_registry,
            memory_manager=self.memory_manager,
            mcp_manager=mcp_manager,
            mcp_scope=mcp_scope or "main",
        ).subset(allowed_tools)
        sections = [
            PromptSection(
                name="workspace",
                content=f"Working directory: {execution_workdir}",
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
            *(
                [build_memory_policy_section(priority=33, read_only=True)]
                if self.memory_manager is not None
                else []
            ),
            *self._build_role_skill_sections(role),
            build_artifact_policy_section(priority=40),
            build_tool_summary_section(registry.tool_specs()),
        ]
        if execution_workdir != self.workdir:
            sections.insert(
                1,
                PromptSection(
                    name="workspace_isolation",
                    content=(
                        "This workflow uses an isolated Git worktree. Read, edit, "
                        "and verify project files only in the working directory "
                        "above. Do not apply, merge, remove, or otherwise manage "
                        "the worktree yourself."
                    ),
                    priority=22,
                ),
            )
        context = ContextManager(
            llm=self.llm,
            workdir=self.workdir,
            max_context_tokens=self.max_context_tokens,
            keep_recent_tool_results=8,
            sections=sections,
        )
        return Agent(
            llm=self.llm,
            tools=registry,
            context_manager=context,
            hooks=build_default_hook_manager(
                workdir=execution_workdir,
                task_workdir=self.workdir,
                task_list_id=workflow_id,
                workflow_id=workflow_id,
                approval_provider=self.approval_provider,
                llm=self.llm,
                memory_manager=self.memory_manager,
                enable_memory_extraction=False,
                artifact_manager=self.artifact_manager,
                mcp_manager=mcp_manager,
            ),
            workdir=execution_workdir,
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

    def _gate_planning_artifacts(
        self,
        workflow_id: str,
        initial_artifact_ids: set[str],
        before_versions: dict[str, int],
    ) -> _GateResult:
        gate = self._gate_required_artifacts(
            workflow_id,
            initial_artifact_ids,
            before_versions,
            required={
                "prd": {"ready", "accepted"},
                "task_spec": {"ready", "accepted"},
            },
        )
        if not gate.ok:
            return gate

        task_spec = self._artifact_from_ids(gate.artifact_ids, "task_spec")
        task_gate = self._gate_planning_tasks(workflow_id, task_spec)
        if not task_gate.ok:
            return _GateResult(
                ok=False,
                message=task_gate.message,
                artifact_ids=gate.artifact_ids,
                data=task_gate.data,
            )
        if self.isolation != "worktree":
            return _GateResult(
                ok=True,
                message="ok",
                artifact_ids=gate.artifact_ids,
                data=task_gate.data,
            )

        change_required = task_spec.metadata.get("change_required")
        if not isinstance(change_required, bool):
            return _GateResult(
                ok=False,
                message=(
                    "task_spec metadata.change_required must be a boolean. Set "
                    "it to true for file-changing work or false for verify-only "
                    "work."
                ),
                artifact_ids=gate.artifact_ids,
            )
        return _GateResult(
            ok=True,
            message="ok",
            artifact_ids=gate.artifact_ids,
            data={**task_gate.data, "change_required": change_required},
        )

    def _gate_planning_tasks(
        self,
        workflow_id: str,
        task_spec: Artifact,
    ) -> _GateResult:
        try:
            root = self._workflow_task(workflow_id, WORKFLOW_ROOT_TASK_KEY)
        except TaskSystemError as exc:
            return _GateResult(ok=False, message=str(exc))
        if root is None:
            return _GateResult(
                ok=False,
                message="The workflow has no orchestrator-owned root task.",
            )
        raw_task_ids = task_spec.metadata.get("task_ids")
        if not isinstance(raw_task_ids, dict):
            return _GateResult(
                ok=False,
                message=(
                    "task_spec metadata.task_ids must map engineer_implement, "
                    "qa_verify, and pm_acceptance to their real workflow Task ids."
                ),
            )
        task_ids = {
            str(key): str(value)
            for key, value in raw_task_ids.items()
            if str(key).strip() and str(value).strip()
        }
        missing = sorted(set(PLANNING_TASK_SPECS) - set(task_ids))
        if missing:
            return _GateResult(
                ok=False,
                message=(
                    "task_spec metadata.task_ids is missing required task key(s): "
                    + ", ".join(missing)
                ),
            )

        manager = self._task_manager(workflow_id)
        all_tasks = manager.list_tasks(include_completed=True)
        resolved: dict[str, Task] = {}
        for task_key, spec in PLANNING_TASK_SPECS.items():
            task_id = task_ids[task_key]
            try:
                task = manager.get_task(task_id)
            except TaskSystemError as exc:
                return _GateResult(
                    ok=False,
                    message=f"TaskSpec references invalid {task_key} task: {exc}",
                )
            key_matches = [
                candidate
                for candidate in all_tasks
                if str(candidate.metadata.get("workflow_id") or "") == workflow_id
                and str(candidate.metadata.get("task_key") or "") == task_key
            ]
            if len(key_matches) != 1 or key_matches[0].id != task.id:
                return _GateResult(
                    ok=False,
                    message=(
                        f"Workflow task_key={task_key!r} must identify exactly one "
                        "Task and TaskSpec must reference that Task."
                    ),
                )
            expected_metadata = {
                "workflow_id": workflow_id,
                "task_key": task_key,
                "role": spec["role"],
            }
            mismatches = [
                key
                for key, expected in expected_metadata.items()
                if str(task.metadata.get(key) or "") != expected
            ]
            if mismatches:
                return _GateResult(
                    ok=False,
                    message=(
                        f"Workflow task {task.id} has invalid metadata fields: "
                        + ", ".join(mismatches)
                    ),
                )
            if task.parent_id != root.id:
                return _GateResult(
                    ok=False,
                    message=(
                        f"Workflow task {task.id} must have parent_id={root.id}."
                    ),
                )
            if task.owner != spec["owner"]:
                return _GateResult(
                    ok=False,
                    message=(
                        f"Workflow task {task.id} owner must be {spec['owner']!r}."
                    ),
                )
            if task.status != "pending":
                return _GateResult(
                    ok=False,
                    message=(
                        f"PM-created workflow task {task.id} must remain pending "
                        "until its role claims it."
                    ),
                )
            resolved[task_key] = task

        for task_key, spec in PLANNING_TASK_SPECS.items():
            dependency_key = spec.get("blocked_by")
            expected_blockers = (
                [resolved[str(dependency_key)].id] if dependency_key else []
            )
            if resolved[task_key].blocked_by != expected_blockers:
                return _GateResult(
                    ok=False,
                    message=(
                        f"Workflow task {resolved[task_key].id} has invalid "
                        f"blocked_by; expected {expected_blockers}."
                    ),
                )

        return _GateResult(
            ok=True,
            message="ok",
            data={
                "root_task_id": root.id,
                "task_ids": {
                    key: resolved[key].id for key in PLANNING_TASK_SPECS
                },
            },
        )

    def _gate_implementation_report(
        self,
        record: WorkflowRunRecord,
        initial_artifact_ids: set[str],
        before_versions: dict[str, int],
        phase_data: dict[str, Any],
        *,
        phase_key: str,
    ) -> _GateResult:
        gate = self._gate_required_artifacts(
            record.workflow_id,
            initial_artifact_ids,
            before_versions,
            required={"implementation_report": None},
        )
        if not gate.ok:
            return gate
        task_gate = self._gate_phase_task(record.workflow_id, phase_key)
        if self.isolation != "worktree":
            return _GateResult(
                ok=task_gate.ok,
                message=task_gate.message,
                artifact_ids=gate.artifact_ids,
                data=task_gate.data,
            )

        report = self.artifact_manager.get_artifact(gate.artifact_ids[0])
        task_spec = self._latest_workflow_artifact(
            record.workflow_id,
            initial_artifact_ids,
            "task_spec",
        )
        change_required = task_spec.metadata.get("change_required")
        if not isinstance(change_required, bool):
            return _GateResult(
                ok=False,
                message="The active task_spec has no boolean change_required value.",
                artifact_ids=gate.artifact_ids,
            )

        before = phase_data.get("worktree_before")
        if not isinstance(before, dict) or not before.get("diff_sha256"):
            return _GateResult(
                ok=False,
                message="The phase has no persisted Worktree baseline evidence.",
                artifact_ids=gate.artifact_ids,
            )
        try:
            after = self._worktree_snapshot(record)
        except WorktreeError as exc:
            return _GateResult(
                ok=False,
                message=f"Unable to collect Worktree evidence: {exc}",
                artifact_ids=gate.artifact_ids,
            )

        outcome = str(report.metadata.get("outcome", "")).strip().lower()
        raw_declared_files = report.metadata.get("changed_files")
        declared_files = self._metadata_file_list(raw_declared_files)
        actual_files = sorted(str(path) for path in after["changed_files"])
        changed_this_phase = before.get("diff_sha256") != after.get("diff_sha256")
        report_snapshot = self._latest_artifact_snapshot(
            phase_data,
            report.id,
            normalized_key="implementation_report_snapshot",
        )
        verification = [
            dict(item)
            for item in phase_data.get("verification") or []
            if isinstance(item, dict)
        ]
        successful_verification = [
            item
            for item in verification
            if item.get("outcome") in {"passed", "clean"}
            and item.get("diff_sha256") == after.get("diff_sha256")
        ]
        evidence = {
            "report_id": report.id,
            "change_required": change_required,
            "outcome": outcome or None,
            "changed_this_phase": changed_this_phase,
            "declared_changed_files": declared_files,
            "effective_changed_files": actual_files,
            "metadata_reconciled": False,
            "actual_changed_files": actual_files,
            "worktree_before": before,
            "worktree_after": after,
            "report_snapshot": report_snapshot,
            "verification": verification,
            "fresh_successful_checks": len(successful_verification),
        }

        def failed(message: str) -> _GateResult:
            return _GateResult(
                ok=False,
                message=message,
                artifact_ids=gate.artifact_ids,
                data={"implementation_evidence": evidence},
            )

        if outcome not in {"changed", "no_change", "blocked"}:
            return failed(
                "implementation_report metadata.outcome must be 'changed', "
                "'no_change', or 'blocked'."
            )
        if outcome == "blocked":
            return failed("Engineer reported outcome=blocked.")
        if outcome == "no_change":
            reason = str(report.metadata.get("no_change_reason", "")).strip()
            if change_required:
                return failed(
                    "TaskSpec requires file changes, but Engineer reported "
                    "outcome=no_change."
                )
            if changed_this_phase:
                return failed(
                    "Engineer reported outcome=no_change, but the Worktree Diff "
                    "changed during this phase."
                )
            if not reason:
                return failed("outcome=no_change requires metadata.no_change_reason.")
        else:
            if not changed_this_phase:
                return failed(
                    "ImplementationReport declares outcome=changed, but the "
                    "Worktree Diff did not change during this phase."
                )
            if not actual_files:
                return failed(
                    "ImplementationReport declares outcome=changed, but the "
                    "Worktree contains no project file changes."
                )
            if not successful_verification:
                return failed(
                    "ImplementationReport describes a changed Worktree, but no "
                    "successful run_tests/run_lint result is bound to the final "
                    "Worktree Diff. Verify after the last mutation."
                )

        if report_snapshot is None:
            return failed(
                "The implementation_report has no Orchestrator-captured write "
                "snapshot. Create or update it after final verification."
            )
        if int(report_snapshot.get("version") or 0) != report.version:
            return failed(
                "The implementation_report version does not match its captured "
                "write snapshot; read and update the current artifact version."
            )
        if report_snapshot.get("diff_sha256") != after.get("diff_sha256"):
            return failed(
                "The implementation_report was written for an older Worktree Diff. "
                "Update it after the final mutation and verification."
            )

        metadata_updates: dict[str, Any] = {}
        warnings: list[str] = []
        if declared_files != actual_files:
            metadata_updates["changed_files"] = actual_files
            warnings.append(
                "ImplementationReport changed_files differed from the Worktree; "
                "the metadata was reconciled to the authoritative changed-file list."
            )
        if report.metadata.get("worktree_diff_sha256") != after.get("diff_sha256"):
            metadata_updates["worktree_diff_sha256"] = after.get("diff_sha256")
        if metadata_updates:
            report = self.artifact_manager.update_artifact(
                report.id,
                metadata=metadata_updates,
                expected_version=report.version,
                change_summary=(
                    "Bound implementation evidence to authoritative Worktree state."
                ),
            )
            evidence["metadata_reconciled"] = True
        normalized_snapshot = {
            "artifact_id": report.id,
            "kind": report.kind,
            "version": report.version,
            "run_id": phase_data.get("current_run_id"),
            "step": report_snapshot.get("step"),
            "diff_sha256": after.get("diff_sha256"),
            "orchestrator_normalized": bool(metadata_updates),
        }
        evidence["report_snapshot"] = normalized_snapshot

        combined_data = {
            "implementation_evidence": evidence,
            "implementation_report_snapshot": normalized_snapshot,
            **task_gate.data,
        }
        if not task_gate.ok:
            return _GateResult(
                ok=False,
                message=task_gate.message,
                artifact_ids=gate.artifact_ids,
                data=combined_data,
            )

        warning = " ".join(warnings) or None

        return _GateResult(
            ok=True,
            message=warning or "ok",
            artifact_ids=gate.artifact_ids,
            data={
                **combined_data,
                **({"gate_warning": warning} if warning else {}),
            },
        )

    def _gate_required_artifacts(
        self,
        workflow_id: str,
        initial_artifact_ids: set[str],
        before_versions: dict[str, int],
        *,
        required: dict[str, set[str] | None],
    ) -> _GateResult:
        artifacts = self._changed_workflow_artifacts(
            workflow_id,
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
        record: WorkflowRunRecord,
        initial_artifact_ids: set[str],
        before_versions: dict[str, int],
        phase_data: dict[str, Any],
        *,
        phase_key: str,
    ) -> _GateResult:
        gate = self._gate_required_artifacts(
            record.workflow_id,
            initial_artifact_ids,
            before_versions,
            required={"test_report": None},
        )
        if not gate.ok:
            return gate
        artifact = self.artifact_manager.get_artifact(gate.artifact_ids[0])
        reported_verdict = str(
            artifact.metadata.get(
                "reported_verdict",
                artifact.metadata.get("verdict", ""),
            )
        ).strip().lower()
        if reported_verdict not in {"pass", "fail"}:
            return _GateResult(
                ok=False,
                message=("test_report must set metadata.verdict to 'pass' or 'fail'."),
                artifact_ids=[artifact.id],
            )

        task_gate = self._gate_phase_task(record.workflow_id, phase_key)
        if self.isolation != "worktree":
            return _GateResult(
                ok=task_gate.ok,
                message=task_gate.message,
                artifact_ids=[artifact.id],
                data={"qa_verdict": reported_verdict, **task_gate.data},
            )

        before = phase_data.get("worktree_before")
        try:
            after = self._worktree_snapshot(record)
        except WorktreeError as exc:
            return _GateResult(
                ok=False,
                message=f"Unable to collect QA Worktree evidence: {exc}",
                artifact_ids=[artifact.id],
            )
        current_run_id = phase_data.get("current_run_id")
        verification = [
            dict(item)
            for item in phase_data.get("verification") or []
            if isinstance(item, dict)
            and (current_run_id is None or item.get("run_id") == current_run_id)
        ]
        successful = [
            item
            for item in verification
            if item.get("outcome") in {"passed", "clean"}
            and item.get("diff_sha256") == after.get("diff_sha256")
        ]
        failed = [
            item
            for item in verification
            if item.get("outcome")
            in {"failed", "issues_found", "error", "timed_out", "tool_error"}
        ]
        blocked_failed = [
            item
            for item in failed
            if item.get("outcome") == "tool_error"
            or item.get("error_kind") == "environment_setup_failed"
        ]
        actionable_failed = [item for item in failed if item not in blocked_failed]
        worktree_changed = isinstance(before, dict) and before.get(
            "diff_sha256"
        ) != after.get("diff_sha256")
        report_snapshot = self._latest_artifact_snapshot(
            phase_data,
            artifact.id,
            normalized_key="test_report_snapshot",
        )
        effective_verdict = (
            "fail"
            if reported_verdict == "pass" and actionable_failed
            else reported_verdict
        )
        verdict_overridden = effective_verdict != reported_verdict
        evidence = {
            "report_id": artifact.id,
            "verdict": effective_verdict,
            "reported_verdict": reported_verdict,
            "effective_verdict": effective_verdict,
            "verdict_overridden": verdict_overridden,
            "verification": verification,
            "successful_checks": len(successful),
            "failed_checks": len(failed),
            "actionable_failed_checks": len(actionable_failed),
            "blocked_checks": len(blocked_failed),
            "worktree_changed_during_qa": worktree_changed,
            "worktree_before": before,
            "worktree_after": after,
            "report_snapshot": report_snapshot,
        }
        if reported_verdict == "pass" and worktree_changed:
            return _GateResult(
                ok=False,
                message=(
                    "QA verification changed tracked project files. A pass verdict "
                    "requires the Worktree to remain unchanged during QA."
                ),
                artifact_ids=[artifact.id],
                data={
                    "qa_verdict": effective_verdict,
                    "qa_evidence": evidence,
                },
            )
        if not verification:
            return _GateResult(
                ok=False,
                message=(
                    "test_report has no structured run_tests/run_lint evidence in "
                    "this QA attempt."
                ),
                artifact_ids=[artifact.id],
                data={
                    "qa_verdict": effective_verdict,
                    "qa_evidence": evidence,
                },
            )
        if failed and not actionable_failed:
            return _GateResult(
                ok=False,
                message=(
                    "QA verification was blocked by a tool execution, permission, "
                    "or project-environment setup error and must be retried before "
                    "routing work to Engineer."
                ),
                artifact_ids=[artifact.id],
                data={
                    "qa_verdict": effective_verdict,
                    "qa_evidence": evidence,
                },
            )
        if reported_verdict == "pass" and not successful and not actionable_failed:
            return _GateResult(
                ok=False,
                message=(
                    "test_report declares verdict=pass, but no successful "
                    "run_tests or run_lint result is bound to the final Worktree Diff."
                ),
                artifact_ids=[artifact.id],
                data={
                    "qa_verdict": effective_verdict,
                    "qa_evidence": evidence,
                },
            )
        if report_snapshot is None:
            return _GateResult(
                ok=False,
                message=(
                    "The test_report has no Orchestrator-captured write snapshot. "
                    "Create or update it after the last verification call."
                ),
                artifact_ids=[artifact.id],
                data={
                    "qa_verdict": effective_verdict,
                    "qa_evidence": evidence,
                },
            )
        if int(report_snapshot.get("version") or 0) != artifact.version:
            return _GateResult(
                ok=False,
                message=(
                    "The test_report version does not match its captured write "
                    "snapshot; update the current artifact version."
                ),
                artifact_ids=[artifact.id],
                data={
                    "qa_verdict": effective_verdict,
                    "qa_evidence": evidence,
                },
            )
        if report_snapshot.get("diff_sha256") != after.get("diff_sha256"):
            return _GateResult(
                ok=False,
                message=(
                    "The test_report was written for an older Worktree Diff. "
                    "Update it after final verification."
                ),
                artifact_ids=[artifact.id],
                data={
                    "qa_verdict": effective_verdict,
                    "qa_evidence": evidence,
                },
            )

        warning: str | None = None
        if reported_verdict == "pass" and actionable_failed:
            warning = (
                "test_report declared verdict=pass, but machine verification "
                "reported a failure, issue, timeout, or execution error; the "
                "effective QA verdict was downgraded to fail."
            )
        metadata_updates = {
            key: value
            for key, value in {
                "reported_verdict": reported_verdict,
                "verdict": effective_verdict,
                "effective_verdict": effective_verdict,
                "verdict_overridden": verdict_overridden,
                "tested_diff_sha256": after.get("diff_sha256"),
            }.items()
            if artifact.metadata.get(key) != value
        }
        if metadata_updates:
            artifact = self.artifact_manager.update_artifact(
                artifact.id,
                metadata=metadata_updates,
                expected_version=artifact.version,
                change_summary=(
                    "Normalized QA verdict and bound it to authoritative verification."
                ),
            )
        normalized_snapshot = {
            "artifact_id": artifact.id,
            "kind": artifact.kind,
            "version": artifact.version,
            "run_id": phase_data.get("current_run_id"),
            "step": report_snapshot.get("step"),
            "diff_sha256": after.get("diff_sha256"),
            "orchestrator_normalized": bool(metadata_updates),
        }
        evidence["report_snapshot"] = normalized_snapshot
        data = {
            "qa_verdict": effective_verdict,
            "qa_evidence": evidence,
            "test_report_snapshot": normalized_snapshot,
            **task_gate.data,
            **({"gate_warning": warning} if warning else {}),
        }
        if not task_gate.ok:
            return _GateResult(
                ok=False,
                message=task_gate.message,
                artifact_ids=[artifact.id],
                data=data,
            )
        return _GateResult(
            ok=True,
            message=warning or "ok",
            artifact_ids=[artifact.id],
            data=data,
        )

    def _gate_acceptance_report(
        self,
        workflow_id: str,
        initial_artifact_ids: set[str],
        before_versions: dict[str, int],
        *,
        qa_verdict: str | None,
        phase_key: str,
    ) -> _GateResult:
        gate = self._gate_required_artifacts(
            workflow_id,
            initial_artifact_ids,
            before_versions,
            required={"acceptance_report": None},
        )
        if not gate.ok:
            return gate

        artifact = self.artifact_manager.get_artifact(gate.artifact_ids[0])
        verdict = str(artifact.metadata.get("verdict", "")).strip().lower()
        if verdict not in {"pass", "fail"}:
            return _GateResult(
                ok=False,
                message=(
                    "acceptance_report must set metadata.verdict to 'pass' or 'fail'."
                ),
                artifact_ids=[artifact.id],
            )

        expected = "pass" if qa_verdict == "pass" else "fail"
        if verdict != expected:
            return _GateResult(
                ok=False,
                message=(
                    "acceptance_report metadata.verdict must match the authoritative "
                    f"QA verdict: expected {expected!r}, got {verdict!r}."
                ),
                artifact_ids=[artifact.id],
            )

        required_status = "accepted" if verdict == "pass" else "ready"
        if artifact.status != required_status:
            return _GateResult(
                ok=False,
                message=(
                    "acceptance_report status conflicts with metadata.verdict; "
                    f"verdict={verdict!r} requires status {required_status}."
                ),
                artifact_ids=[artifact.id],
            )
        task_gate = self._gate_phase_task(workflow_id, phase_key)
        return _GateResult(
            ok=task_gate.ok,
            message=task_gate.message,
            artifact_ids=[artifact.id],
            data={"acceptance_verdict": verdict, **task_gate.data},
        )

    def _artifact_from_ids(
        self,
        artifact_ids: list[str],
        kind: str,
    ) -> Artifact:
        matches = [
            self.artifact_manager.get_artifact(artifact_id)
            for artifact_id in artifact_ids
        ]
        return self._latest([artifact for artifact in matches if artifact.kind == kind])

    def _latest_workflow_artifact(
        self,
        workflow_id: str,
        initial_artifact_ids: set[str],
        kind: str,
    ) -> Artifact:
        matches = [
            artifact
            for artifact in self._workflow_artifacts(
                workflow_id,
                initial_artifact_ids,
            )
            if artifact.kind == kind
        ]
        if not matches:
            raise ValueError(f"Workflow artifact not found: {kind}")
        return self._latest(matches)

    @staticmethod
    def _metadata_file_list(value: Any) -> list[str] | None:
        if not isinstance(value, list):
            return None
        files = []
        for item in value:
            if not isinstance(item, str) or not item.strip():
                return None
            files.append(item.strip())
        return sorted(set(files))

    @staticmethod
    def _format_evidence_summary(record: WorkflowRunRecord) -> str:
        lines: list[str] = []
        for phase_result in record.phases:
            phase = str(phase_result.get("phase", "unknown"))
            data = phase_result.get("data")
            if not isinstance(data, dict):
                continue

            implementation = data.get("implementation_evidence")
            if isinstance(implementation, dict):
                files = implementation.get("actual_changed_files") or []
                rendered_files = ", ".join(str(path) for path in files) or "none"
                lines.append(
                    f"- {phase}: engineer outcome="
                    f"{implementation.get('outcome') or 'unknown'}, "
                    f"diff_changed={bool(implementation.get('changed_this_phase'))}, "
                    f"worktree_files={rendered_files}"
                )

            qa = data.get("qa_evidence")
            if isinstance(qa, dict):
                checks = []
                for item in qa.get("verification") or []:
                    if isinstance(item, dict):
                        check = (
                            f"{item.get('tool', 'unknown')}="
                            f"{item.get('outcome', 'unknown')}"
                        )
                        summary = str(item.get("summary") or "").strip()
                        if summary:
                            check += f" summary={summary[:300]}"
                        error_kind = str(item.get("error_kind") or "").strip()
                        if error_kind:
                            check += f" error_kind={error_kind}"
                        missing_modules = [
                            str(module).strip()
                            for module in item.get("missing_modules") or []
                            if str(module).strip()
                        ]
                        if missing_modules:
                            check += " missing_modules=" + ", ".join(
                                missing_modules[:20]
                            )
                        failed_tests = [
                            str(failure.get("test") or "").strip()
                            for failure in item.get("failures") or []
                            if isinstance(failure, dict)
                            and str(failure.get("test") or "").strip()
                        ]
                        if failed_tests:
                            check += " failed_tests=" + ", ".join(failed_tests[:20])
                        error = str(item.get("error") or "").strip()
                        if error:
                            check += f" error={error[:300]}"
                        diagnostic = str(item.get("diagnostic") or "").strip()
                        if diagnostic:
                            check += f" diagnostic={diagnostic[:500]}"
                        checks.append(check)
                rendered_checks = ", ".join(checks) or "none"
                reported_verdict = (
                    qa.get("reported_verdict") or qa.get("verdict") or "unknown"
                )
                effective_verdict = (
                    qa.get("effective_verdict") or qa.get("verdict") or "unknown"
                )
                verdict_summary = str(effective_verdict)
                if reported_verdict != effective_verdict:
                    verdict_summary += f" (reported={reported_verdict})"
                lines.append(
                    f"- {phase}: QA verdict={verdict_summary}, "
                    f"checks={rendered_checks}, "
                    "worktree_changed_during_qa="
                    f"{bool(qa.get('worktree_changed_during_qa'))}"
                )

        return "\n".join(lines) or "- No structured evidence was recorded."

    @staticmethod
    def _format_reflection_artifacts(artifacts: list[Artifact]) -> str:
        parts: list[str] = []
        for artifact in artifacts:
            metadata = json.dumps(
                artifact.metadata,
                ensure_ascii=False,
                default=str,
            )[:1_500]
            parts.append(
                f"## {artifact.kind}: {artifact.title}\n"
                f"Status: {artifact.status}\n"
                f"Metadata: {metadata}\n"
                f"{artifact.content[:3_000]}"
            )
        return "\n\n".join(parts) or "(none)"

    def _changed_workflow_artifacts(
        self,
        workflow_id: str,
        initial_artifact_ids: set[str],
        before_versions: dict[str, int],
    ) -> list[Artifact]:
        artifacts = []
        for artifact in self.artifact_manager.list_artifacts(include_archived=True):
            if artifact.id in initial_artifact_ids:
                continue
            if not self._artifact_belongs_to_workflow(artifact, workflow_id):
                continue
            previous_version = before_versions.get(artifact.id)
            if previous_version is None or artifact.version != previous_version:
                artifacts.append(artifact)
        return artifacts

    def _workflow_artifacts(
        self,
        workflow_id: str,
        initial_artifact_ids: set[str],
    ) -> list[Artifact]:
        return [
            artifact
            for artifact in self.artifact_manager.list_artifacts(include_archived=True)
            if artifact.id not in initial_artifact_ids
            and self._artifact_belongs_to_workflow(artifact, workflow_id)
        ]

    @staticmethod
    def _artifact_belongs_to_workflow(
        artifact: Artifact,
        workflow_id: str,
    ) -> bool:
        owner = artifact.metadata.get("workflow_id")
        return owner is None or str(owner) == workflow_id

    def _artifact_ids(self) -> set[str]:
        return {
            artifact.id
            for artifact in self.artifact_manager.list_artifacts(include_archived=True)
        }

    def _artifact_versions(self) -> dict[str, int]:
        return {
            artifact.id: artifact.version
            for artifact in self.artifact_manager.list_artifacts(include_archived=True)
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
        record: WorkflowRunRecord | None = None,
    ) -> WorkflowResult:
        workflow_artifacts = self._workflow_artifacts(
            workflow_id,
            initial_artifact_ids,
        )
        artifact_ids = [artifact.id for artifact in workflow_artifacts]
        worktree = self._worktree_review(record)
        if status == "completed" and worktree is not None and worktree.get("error"):
            status = "failed"
            error = f"Workflow worktree review failed: {worktree['error']}"
        result = WorkflowResult(
            workflow_id=workflow_id,
            status=status,
            phases=phases,
            artifact_ids=artifact_ids,
            qa_verdict=qa_verdict,
            fix_cycles=fix_cycles,
            worktree=worktree,
            error=error,
        )
        self._close_workflow_root_task(
            workflow_id,
            status=status,
            qa_verdict=qa_verdict,
            error=error,
        )
        if record is not None and self.workflow_store is not None:
            self.workflow_store.mark_finished(
                record,
                status=status,
                artifact_ids=artifact_ids,
                qa_verdict=qa_verdict,
                fix_cycles=fix_cycles,
                error=error,
            )
        reflected_memory_ids: list[str] = []
        if (
            status == "completed"
            and qa_verdict == "pass"
            and self.memory_manager is not None
            and record is not None
        ):
            try:
                reflected = self.memory_manager.reflect_from_workflow(
                    workflow_id=workflow_id,
                    request=record.request,
                    evidence_summary=self._format_evidence_summary(record),
                    artifact_context=self._format_reflection_artifacts(
                        workflow_artifacts
                    ),
                    changed_files=(
                        [str(path) for path in worktree.get("changed_files", [])]
                        if worktree is not None
                        else []
                    ),
                )
            except Exception as exc:
                reflected = []
                self._record_workflow_trace(
                    "workflow.memory.reflection_failed",
                    phase="failed",
                    status="warning",
                    data={"workflow_id": workflow_id, "error": exc},
                )
            reflected_memory_ids = [memory.id for memory in reflected]
            if reflected_memory_ids:
                self._record_workflow_trace(
                    "workflow.memory.reflected",
                    phase="completed",
                    data={
                        "workflow_id": workflow_id,
                        "memory_ids": reflected_memory_ids,
                    },
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
                "worktree_id": (
                    worktree.get("worktree", {}).get("id")
                    if worktree is not None
                    else None
                ),
                "changed_files": (
                    worktree.get("changed_files", []) if worktree is not None else []
                ),
                "reflected_memory_ids": reflected_memory_ids,
                "error": error,
            },
        )
        return result

    def _persist_phase_completed(
        self,
        record: WorkflowRunRecord | None,
        phase_key: str,
        result: WorkflowPhaseResult,
    ) -> None:
        if record is None or self.workflow_store is None:
            return
        self.workflow_store.mark_phase_completed(
            record,
            phase_key=phase_key,
            phase_result=self._phase_result_to_dict(result),
            attempts=result.attempts,
            run_ids=result.run_ids,
            artifact_ids=result.artifact_ids,
            data=result.data,
        )

    def _persist_phase_failed(
        self,
        record: WorkflowRunRecord | None,
        phase_key: str,
        result: WorkflowPhaseResult,
    ) -> None:
        if record is None or self.workflow_store is None:
            return
        self.workflow_store.mark_phase_failed(
            record,
            phase_key=phase_key,
            phase_result=self._phase_result_to_dict(result),
            attempts=result.attempts,
            run_ids=result.run_ids,
            artifact_ids=result.artifact_ids,
            error=result.error,
            data=result.data,
        )

    def _save_record(self, record: WorkflowRunRecord) -> None:
        if self.workflow_store is not None:
            self.workflow_store.save_run(record)

    def _record_phase_result(
        self,
        record: WorkflowRunRecord,
        phase_key: str,
    ) -> WorkflowPhaseResult | None:
        for item in record.phases:
            if item.get("phase_key") == phase_key:
                return self._phase_result_from_dict(item)
        return None

    @staticmethod
    def _phase_result_is_trusted(
        record: WorkflowRunRecord,
        phase_key: str,
        result: WorkflowPhaseResult,
    ) -> bool:
        checkpoint = record.checkpoints.get(phase_key)
        return bool(
            result.status == "completed"
            and checkpoint is not None
            and checkpoint.status == "completed"
            and result.data.get("agent_status") == "completed"
            and checkpoint.data.get("agent_status") == "completed"
        )

    @staticmethod
    def _expected_phase_keys(record: WorkflowRunRecord) -> list[str]:
        keys = ["pm_plan", "engineer_implement", "qa_verify"]
        for cycle in range(1, record.fix_cycles + 1):
            keys.extend([f"engineer_fix_{cycle}", f"qa_regression_{cycle}"])
        keys.append("pm_acceptance")
        return keys

    def _completed_record_is_trusted(self, record: WorkflowRunRecord) -> bool:
        if record.status != "completed":
            return False
        results = {
            str(item.get("phase_key") or ""): self._phase_result_from_dict(item)
            for item in record.phases
        }
        return all(
            phase_key in results
            and self._phase_result_is_trusted(
                record,
                phase_key,
                results[phase_key],
            )
            for phase_key in self._expected_phase_keys(record)
        )

    def _invalidate_untrusted_phase_suffix(
        self,
        record: WorkflowRunRecord,
    ) -> None:
        phase_items = list(record.phases)
        bad_index: int | None = None
        bad_key: str | None = None
        for index, item in enumerate(phase_items):
            phase_key = str(item.get("phase_key") or "")
            result = self._phase_result_from_dict(item)
            if not self._phase_result_is_trusted(record, phase_key, result):
                bad_index = index
                bad_key = phase_key
                break

        if bad_index is None:
            present = {
                str(item.get("phase_key") or "") for item in phase_items
            }
            for phase_key in self._expected_phase_keys(record):
                if phase_key not in present:
                    bad_index = len(phase_items)
                    bad_key = phase_key
                    break

        if bad_index is None:
            return
        removed_keys = {
            str(item.get("phase_key") or "")
            for item in phase_items[bad_index:]
        }
        record.phases = phase_items[:bad_index]
        for phase_key in removed_keys:
            if phase_key != bad_key:
                record.checkpoints.pop(phase_key, None)
        checkpoint = record.checkpoints.get(bad_key or "")
        if checkpoint is not None:
            checkpoint.status = "running"
            checkpoint.completed_at = None
            checkpoint.error = (
                "Persisted phase completion was invalid because agent_status was "
                "not completed. Continue from its checkpoint."
            )
        record.status = "interrupted"
        record.current_phase_key = bad_key
        record.current_phase = checkpoint.phase if checkpoint is not None else None
        record.error = checkpoint.error if checkpoint is not None else None
        self._save_record(record)

    def _record_phase_results(
        self,
        record: WorkflowRunRecord,
    ) -> list[WorkflowPhaseResult]:
        return [self._phase_result_from_dict(item) for item in record.phases]

    def _record_to_result(self, record: WorkflowRunRecord) -> WorkflowResult:
        status: WorkflowStatus = (
            "completed" if record.status == "completed" else "failed"
        )
        return WorkflowResult(
            workflow_id=record.workflow_id,
            status=status,
            phases=self._record_phase_results(record),
            artifact_ids=list(record.artifact_ids),
            qa_verdict=record.qa_verdict,
            fix_cycles=record.fix_cycles,
            worktree=self._worktree_review(record),
            error=record.error,
        )

    @staticmethod
    def _phase_result_to_dict(result: WorkflowPhaseResult) -> dict[str, Any]:
        return {
            "phase": result.phase,
            "role": result.role,
            "status": result.status,
            "run_ids": list(result.run_ids),
            "artifact_ids": list(result.artifact_ids),
            "summary": result.summary,
            "attempts": result.attempts,
            "error": result.error,
            "data": dict(result.data),
        }

    @staticmethod
    def _phase_result_from_dict(data: dict[str, Any]) -> WorkflowPhaseResult:
        return WorkflowPhaseResult(
            phase=str(data["phase"]),  # type: ignore[arg-type]
            role=str(data["role"]),
            status=str(data["status"]),  # type: ignore[arg-type]
            run_ids=[str(value) for value in data.get("run_ids") or []],
            artifact_ids=[str(value) for value in data.get("artifact_ids") or []],
            summary=str(data.get("summary") or ""),
            attempts=int(data.get("attempts") or 1),
            error=str(data["error"]) if data.get("error") is not None else None,
            data=dict(data.get("data") or {}),
        )

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
    def _corrective_prompt(
        phase: WorkflowPhase,
        issue: str,
        handoff: dict[str, Any] | None = None,
    ) -> str:
        handoff_context = ""
        if handoff is not None:
            working_state = SerialCodingWorkflow._format_working_state(
                phase,
                issue,
                handoff,
            )
            checkpoint = handoff.get("checkpoint")
            checkpoint_context = (
                json.dumps(checkpoint, ensure_ascii=False, indent=2)
                if isinstance(checkpoint, dict)
                else "{}"
            )
            handoff_context = (
                "\n\n<working_state>\n"
                f"{working_state}\n"
                "</working_state>\n\n"
                "<attempt_handoff>\n"
                "This bounded checkpoint describes the previous attempt. The "
                "current workspace and fresh tool evidence remain authoritative.\n"
                f"{json.dumps(handoff, ensure_ascii=False, indent=2)}\n"
                "</attempt_handoff>\n\n"
                "<attempt_checkpoint>\n"
                f"{checkpoint_context}\n"
                "</attempt_checkpoint>"
            )
        return (
            f"The {phase} phase did not satisfy its completion evidence gate.\n"
            f"Issue: {issue}\n\n"
            "Resolve this specific issue with the available tools, then create or "
            "update the required phase artifact so its metadata matches the actual "
            "code and verification evidence. Do not redo unrelated work. Use "
            "expected_version when updating an existing artifact. Continue from "
            "the existing workspace state and verify assumptions when needed."
            f"{handoff_context}"
        )

    @staticmethod
    def _handoff_changed_files(handoff: dict[str, Any]) -> list[str]:
        checkpoint = handoff.get("checkpoint")
        if isinstance(checkpoint, dict):
            changed_files = [
                str(path)
                for path in checkpoint.get("changed_files") or []
                if str(path).strip()
            ]
            if changed_files:
                return changed_files
        worktree = handoff.get("worktree")
        if not isinstance(worktree, dict):
            return []
        return [
            str(path)
            for path in worktree.get("changed_files") or []
            if str(path).strip()
        ]

    @staticmethod
    def _format_working_state(
        phase: WorkflowPhase,
        issue: str,
        handoff: dict[str, Any],
    ) -> str:
        changed_files = SerialCodingWorkflow._handoff_changed_files(handoff)
        lines = [
            "This is persisted short-term working state, not a request to restart.",
            f"Previous attempt: {handoff.get('attempt', 'unknown')} "
            f"({handoff.get('agent_status', 'unknown')}).",
            f"Unresolved completion issue: {issue}",
            "Existing Worktree changes: "
            + (", ".join(changed_files) if changed_files else "none"),
        ]
        checkpoint = handoff.get("checkpoint")
        if isinstance(checkpoint, dict):
            for label, key in (
                ("Completed work", "completed"),
                ("Remaining work", "remaining"),
                ("Next actions", "next_actions"),
                ("Decisions", "decisions"),
                ("Blockers", "blockers"),
            ):
                values = [
                    str(value).strip()
                    for value in checkpoint.get(key) or []
                    if str(value).strip()
                ]
                if values:
                    lines.append(f"{label}: " + "; ".join(values))

        failed_checks = []
        for check in handoff.get("verification") or []:
            if not isinstance(check, dict) or check.get("outcome") in {
                "passed",
                "clean",
            }:
                continue
            rendered = (
                f"{check.get('tool', 'unknown')}={check.get('outcome', 'unknown')}"
            )
            error_kind = str(check.get("error_kind") or "").strip()
            if error_kind:
                rendered += f" [{error_kind}]"
            missing_modules = [
                str(module).strip()
                for module in check.get("missing_modules") or []
                if str(module).strip()
            ]
            if missing_modules:
                rendered += f" missing={', '.join(missing_modules[:10])}"
            tests = [
                str(failure.get("test") or "").strip()
                for failure in check.get("failures") or []
                if isinstance(failure, dict) and str(failure.get("test") or "").strip()
            ]
            if tests:
                rendered += f" ({', '.join(tests[:10])})"
            diagnostic = str(check.get("diagnostic") or "").strip()
            if diagnostic:
                rendered += f": {diagnostic[:500]}"
            failed_checks.append(rendered)
        if failed_checks:
            lines.append("Failed verification: " + "; ".join(failed_checks))

        recent_failures = [
            f"{item.get('tool', 'unknown')}: {item.get('error', 'failed')}"
            for item in handoff.get("recent_failures") or []
            if isinstance(item, dict)
        ]
        if recent_failures:
            lines.append("Recent tool failures: " + "; ".join(recent_failures[-4:]))

        lines.extend(
            [
                "Continue from the existing Worktree. Do not repeat broad repository "
                "discovery or recreate files already listed above.",
                "Inspect the current diff and only the files needed to resolve the "
                "issue, run focused verification, then update the required artifact.",
            ]
        )
        if phase in {"engineer_implement", "engineer_fix"}:
            lines.append(
                "Before finishing, report every current Worktree changed file and "
                "reserve enough turns for the implementation_report."
            )
        return "\n".join(f"- {line}" for line in lines)

    @staticmethod
    def _pm_prompt(workflow_id: str, request: str) -> str:
        return f"""
        Create the planning handoff artifacts for this workflow.

        Workflow id: {workflow_id}
        User request:
        {request}

        Required actions:
        - Call task_list first and find the in_progress orchestrator root task whose
          metadata.task_key is "workflow_root". Never guess a task id from another
          workflow.
        - Under that root, create exactly three pending session tasks for
          engineer_implement, qa_verify, and pm_acceptance. Give them owners
          "engineer", "qa", and "pm-acceptance" respectively. Every child must
          have metadata.workflow_id="{workflow_id}", metadata.task_key equal to its
          phase key, and metadata.role equal to "engineer", "qa", or
          "pm_acceptance".
        - Give every child an actionable description: required deliverables,
          relevant scope or files when known, completion criteria, verification,
          and any dependency or backend-contract constraints. Tasks are the live
          execution state, not title-only placeholders.
        - Set qa_verify blocked_by=[engineer task id] and pm_acceptance
          blocked_by=[qa task id]. Do not claim or complete role tasks for them.
        - Create the required PRD and TaskSpec before optional repository
          exploration. The user request is the primary planning input.
        - Create one artifact with kind="prd" and status="ready".
        - Create one artifact with kind="task_spec" and status="ready".
        - Include metadata.workflow_id="{workflow_id}" and metadata.role="pm".
        - On TaskSpec, set metadata.change_required to true when tracked project
          files must change, or false only for verify-only/no-code work.
        - On TaskSpec, set metadata.task_ids to an object mapping
          engineer_implement, qa_verify, and pm_acceptance to the exact Task ids
          created above.
        - If a retry finds an existing valid child, reuse it and update TaskSpec;
          never create a duplicate metadata.task_key.
        - After both required artifacts are created successfully, stop calling tools
          and return a concise status summary.
        - Do not modify source files.
        """.strip()

    @staticmethod
    def _engineer_prompt(
        workflow_id: str,
        request: str,
        *,
        fix: bool,
        fix_cycle: int = 0,
        evidence_summary: str | None = None,
    ) -> str:
        if fix:
            action = (
                f"This is fix cycle {fix_cycle}. Read the latest test_report, "
                "reproduce and fix its concrete failures, then update or create an "
                "implementation_report artifact."
            )
            evidence = (
                "\n\nAuthoritative workflow evidence from the previous QA phase:\n"
                f"{evidence_summary}"
                if evidence_summary
                else ""
            )
        else:
            action = (
                "Read the latest PRD and TaskSpec artifacts, implement only the "
                "requested scope, then create an implementation_report artifact."
            )
            evidence = ""
        return f"""
{action}{evidence}

Workflow id: {workflow_id}
User request:
{request}

Required actions:
- Inspect relevant artifacts with artifact_list/artifact_get before acting.
- Make focused code changes only when needed.
- In a fix cycle, existing Worktree changes are only the starting point. Produce a
  new phase-local source, test, or configuration diff that addresses the failed
  evidence. Updating an artifact, restaging files, or merely rerunning checks is not
  a code fix and cannot satisfy the evidence gate.
- Re-run exact failed test IDs first, then the relevant broader suite. A keyword-only
  selection is not proof that the requested behavior was tested.
- Do not install packages into a shared interpreter. Record dependencies in project
  manifests and lockfiles, or use a virtual environment inside the Worktree.
- Keep dependency manifests and lockfiles consistent. For a uv project, use uv add
  or uv lock inside the Worktree instead of editing only pyproject.toml.
- After the final source/configuration mutation, run focused verification against
  that exact final diff. Create or update the implementation_report only after the
  final verification so the Orchestrator can bind both to one Worktree hash.
- Create or update one artifact with kind="implementation_report".
- Include metadata.workflow_id="{workflow_id}" and metadata.role="engineer".
- Set metadata.outcome exactly to "changed", "no_change", or "blocked".
- Set metadata.changed_files to every relative project path currently changed in
  the workflow Worktree, including changes from earlier workflow phases.
- Use outcome="changed" only after making a real Worktree change. Use
  outcome="no_change" only when TaskSpec change_required=false and include a
  non-empty metadata.no_change_reason. Describe blockers for outcome="blocked".
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
- Run focused verification using run_tests and/or run_lint. Treat their structured
  outcomes as authoritative over prose reports or assumptions.
- Create or update the test_report only after the last verification call; the
  Orchestrator binds the report and checks to the exact tested Worktree hash.
- Set verdict="pass" only when this attempt has at least one passed/clean check,
  every verification call completed successfully, and QA did not change source files.
- Set verdict="fail" for failed tests, lint issues, timeouts, or execution errors.
  If a tool cannot run because permission was denied, report that blocker and never
  claim the check passed.
- Preserve structured error_kind, missing_modules, and diagnostic fields in failure
  reports so Engineer can distinguish project defects from environment blockers.
- Judge coverage from the selected test paths and collected test IDs. A keyword match
  count alone is not evidence that the requested feature was tested.
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
        evidence_summary: str,
    ) -> str:
        return f"""
Create the final PM acceptance artifact for this workflow.

Workflow id: {workflow_id}
QA verdict: {qa_verdict or "unknown"}
User request:
{request}

Orchestrator-collected evidence (source of truth when artifact claims conflict):
{evidence_summary}

Required actions:
- Read the relevant PRD, TaskSpec, ImplementationReport, and TestReport artifacts.
- Create one artifact with kind="acceptance_report".
- Include metadata.workflow_id="{workflow_id}" and metadata.role="pm".
- Set metadata.verdict="pass" only when the QA verdict is pass; otherwise set it
  to "fail".
- If metadata.verdict is "pass", create the report with status="accepted" and
  state the supporting evidence.
- If metadata.verdict is "fail", do not use status="accepted"; state that the
  workflow is rejected or blocked, create it with status="ready", and list the
  concrete blockers.
- Do not modify source files.
""".strip()


ROLE_SPECS: dict[str, RoleSpec] = {
    "pm": RoleSpec(
        name="PM",
        agent_id="pm",
        instructions=(
            "You are the PM worker. Produce clear planning artifacts and task "
            "handoffs. Create the required artifacts before optional repository "
            "exploration, then stop using tools. Do not modify source files."
        ),
        tool_names=PM_TOOLS,
        max_steps=10,
    ),
    "engineer": RoleSpec(
        name="Engineer",
        agent_id="engineer",
        instructions=(
            "You are the Engineer worker. Plan multi-step work with the task tools, "
            "inspect exact local APIs before editing, and never invent modules or "
            "constructors. Work directly in the configured workspace without "
            "changing to another directory. Keep changes focused, reserve time for "
            "verification, and create the required implementation evidence."
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
    "WorkflowIsolation",
    "WorkflowResult",
    "WorkflowStatus",
    "parse_workflow_command",
]
