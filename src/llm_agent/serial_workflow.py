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
from llm_agent.tools import build_default_registry
from llm_agent.trace_system import (
    TraceRecorder,
    record_trace,
    summarize_text,
    trace_scope,
)
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
MAX_ATTEMPT_HANDOFFS = 4
MAX_HANDOFF_ACTIONS = 12
EVIDENCE_PHASES = {
    "engineer_implement",
    "engineer_fix",
    "qa_verify",
    "qa_regression",
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
            gate=lambda before, _: self._gate_required_artifacts(
                workflow_id,
                initial_artifact_ids,
                before,
                required={"acceptance_report": None},
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
        if completed_result is not None and completed_result.status == "completed":
            return completed_result

        checkpoint = record.checkpoints.get(phase_key)
        before_versions = checkpoint.before_versions if checkpoint else None
        if checkpoint is not None:
            gate_result = gate(
                checkpoint.before_versions,
                dict(checkpoint.data),
            )
            if gate_result.ok:
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
        if record.status == "completed":
            return self._record_to_result(record)

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
                phase_data = self._append_attempt_handoff(
                    phase_data,
                    interrupted_handoff,
                )
                handoffs = [interrupted_handoff]
        latest_handoff = handoffs[-1] if handoffs else None
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
            attempt_prompt = prompt
            if retry_issue is not None:
                attempt_prompt = (
                    f"{prompt}\n\n"
                    f"{self._corrective_prompt(phase, retry_issue, latest_handoff)}"
                )
            messages.append({"role": "user", "content": attempt_prompt})

            phase_run_id = f"{workflow_id}-{phase}-{attempt}"
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
            }

            def phase_on_event(event: AgentEvent) -> None:
                if self._capture_attempt_event(attempt_state, event):
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
                gate_message = gate_result.message
                if not gate_result.ok:
                    gate_message = (
                        f"Role agent ended with status={result.status}; "
                        f"evidence gate: {gate_result.message}"
                    )
                gate_result = _GateResult(
                    ok=gate_result.ok,
                    message=gate_message,
                    artifact_ids=gate_result.artifact_ids,
                    data={**gate_result.data, "agent_status": result.status},
                )
            phase_data = {**phase_data, **gate_result.data}
            handoff = self._build_attempt_handoff(
                record=record,
                phase=phase,
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
        ):
            if payload.get(key) is not None:
                evidence[key] = payload[key]
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

        data = self._phase_checkpoint_data(record, phase_key)
        verification = list(data.get("verification") or [])
        verification.append(evidence)
        data["verification"] = verification
        self._replace_phase_checkpoint_data(record, phase_key, data)

    @staticmethod
    def _capture_attempt_event(
        state: dict[str, Any],
        event: AgentEvent,
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
            return True

        if event.type == "tool_result":
            result = event.data.get("result")
            if not isinstance(result, dict) or result.get("ok") is not False:
                return False
            failures = state.setdefault("recent_failures", [])
            failures.append(
                {
                    "step": event.step,
                    "tool": str(event.data.get("name", "unknown")),
                    "error": str(result.get("error", "tool failed"))[:500],
                }
            )
            del failures[:-6]
            return True

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

        handoff: dict[str, Any] = {
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
        if not gate.ok or self.isolation != "worktree":
            return gate

        task_spec = self._artifact_from_ids(gate.artifact_ids, "task_spec")
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
            data={"change_required": change_required},
        )

    def _gate_implementation_report(
        self,
        record: WorkflowRunRecord,
        initial_artifact_ids: set[str],
        before_versions: dict[str, int],
        phase_data: dict[str, Any],
    ) -> _GateResult:
        gate = self._gate_required_artifacts(
            record.workflow_id,
            initial_artifact_ids,
            before_versions,
            required={"implementation_report": None},
        )
        if not gate.ok or self.isolation != "worktree":
            return gate

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
        evidence = {
            "report_id": report.id,
            "change_required": change_required,
            "outcome": outcome or None,
            "changed_this_phase": changed_this_phase,
            "declared_changed_files": declared_files,
            "actual_changed_files": actual_files,
            "worktree_before": before,
            "worktree_after": after,
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
        if declared_files is None:
            return failed(
                "implementation_report metadata.changed_files must be a list "
                "of relative file paths."
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
            if declared_files != actual_files:
                return failed(
                    "ImplementationReport changed_files does not match the "
                    "current Worktree Diff."
                )
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
            if declared_files != actual_files:
                return failed(
                    "ImplementationReport changed_files does not match the "
                    "actual Worktree changed files."
                )

        return _GateResult(
            ok=True,
            message="ok",
            artifact_ids=gate.artifact_ids,
            data={"implementation_evidence": evidence},
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
        verdict = str(artifact.metadata.get("verdict", "")).strip().lower()
        if verdict not in {"pass", "fail"}:
            return _GateResult(
                ok=False,
                message=("test_report must set metadata.verdict to 'pass' or 'fail'."),
                artifact_ids=[artifact.id],
            )

        if self.isolation != "worktree":
            return _GateResult(
                ok=True,
                message="ok",
                artifact_ids=[artifact.id],
                data={"qa_verdict": verdict},
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
            item for item in verification if item.get("outcome") in {"passed", "clean"}
        ]
        failed = [
            item
            for item in verification
            if item.get("outcome")
            in {"failed", "issues_found", "error", "timed_out", "tool_error"}
        ]
        actionable_failed = [
            item for item in failed if item.get("outcome") != "tool_error"
        ]
        worktree_changed = isinstance(before, dict) and before.get(
            "diff_sha256"
        ) != after.get("diff_sha256")
        evidence = {
            "report_id": artifact.id,
            "verdict": verdict,
            "reported_verdict": verdict,
            "effective_verdict": verdict,
            "verdict_overridden": False,
            "verification": verification,
            "successful_checks": len(successful),
            "failed_checks": len(failed),
            "actionable_failed_checks": len(actionable_failed),
            "worktree_changed_during_qa": worktree_changed,
            "worktree_before": before,
            "worktree_after": after,
        }
        if verdict == "pass" and worktree_changed:
            return _GateResult(
                ok=False,
                message=(
                    "QA verification changed tracked project files. A pass verdict "
                    "requires the Worktree to remain unchanged during QA."
                ),
                artifact_ids=[artifact.id],
                data={"qa_verdict": verdict, "qa_evidence": evidence},
            )
        if failed and not actionable_failed:
            return _GateResult(
                ok=False,
                message=(
                    "QA verification was blocked by a tool execution or permission "
                    "error and must be retried before routing work to Engineer."
                ),
                artifact_ids=[artifact.id],
                data={"qa_verdict": verdict, "qa_evidence": evidence},
            )
        if verdict == "pass" and actionable_failed:
            warning = (
                "test_report declared verdict=pass, but machine verification "
                "reported a failure, issue, timeout, or execution error; the "
                "effective QA verdict was downgraded to fail."
            )
            evidence = {
                **evidence,
                "verdict": "fail",
                "effective_verdict": "fail",
                "verdict_overridden": True,
            }
            return _GateResult(
                ok=True,
                message=warning,
                artifact_ids=[artifact.id],
                data={
                    "qa_verdict": "fail",
                    "qa_evidence": evidence,
                    "gate_warning": warning,
                },
            )
        if verdict == "pass" and not successful:
            return _GateResult(
                ok=False,
                message=(
                    "test_report declares verdict=pass, but no successful "
                    "run_tests or run_lint result was recorded in this attempt."
                ),
                artifact_ids=[artifact.id],
                data={"qa_verdict": verdict, "qa_evidence": evidence},
            )
        return _GateResult(
            ok=True,
            message="ok",
            artifact_ids=[artifact.id],
            data={"qa_verdict": verdict, "qa_evidence": evidence},
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
            handoff_context = (
                "\n\n<attempt_handoff>\n"
                "This bounded checkpoint describes the previous attempt. The "
                "current workspace and fresh tool evidence remain authoritative.\n"
                f"{json.dumps(handoff, ensure_ascii=False, indent=2)}\n"
                "</attempt_handoff>"
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
    def _pm_prompt(workflow_id: str, request: str) -> str:
        return f"""
        Create the planning handoff artifacts for this workflow.

        Workflow id: {workflow_id}
        User request:
        {request}

        Required actions:
        - Create the required PRD and TaskSpec before optional repository
          exploration. The user request is the primary planning input.
        - Create one artifact with kind="prd" and status="ready".
        - Create one artifact with kind="task_spec" and status="ready".
        - Include metadata.workflow_id="{workflow_id}" and metadata.role="pm".
        - On TaskSpec, set metadata.change_required to true when tracked project
          files must change, or false only for verify-only/no-code work.
        - You may create project tasks if useful, but the artifact gate requires the PRD
        and TaskSpec artifacts.
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
- Set verdict="pass" only when this attempt has at least one passed/clean check,
  every verification call completed successfully, and QA did not change source files.
- Set verdict="fail" for failed tests, lint issues, timeouts, or execution errors.
  If a tool cannot run because permission was denied, report that blocker and never
  claim the check passed.
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
