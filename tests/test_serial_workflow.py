import json
from dataclasses import replace
from pathlib import Path
import subprocess
from typing import Any

from llm_agent.artifact_system import ArtifactManager
from llm_agent.hooks.permission_hooks import AutoApprovalProvider
from llm_agent.llm_client import LLMResponse, LLMToolCall
from llm_agent.memory_system import MemoryManager
from llm_agent.serial_workflow import (
    ROLE_SPECS,
    SerialCodingWorkflow,
    parse_workflow_command,
)
from llm_agent.skill_system import SkillRegistry
from llm_agent.trace_system import TraceRecorder
from llm_agent.worktree import WorktreeManager


class FakeLLM:
    model = "fake"

    def __init__(self, outputs: list[LLMResponse]) -> None:
        self.outputs = outputs
        self.messages: list[list[dict[str, Any]]] = []
        self.tools: list[list[dict[str, Any]]] = []

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]],
        tool_choice: str,
    ) -> LLMResponse:
        self.messages.append([dict(message) for message in messages])
        self.tools.append(tools)
        return self.outputs.pop(0)

    def assistant_message(self, response: LLMResponse) -> dict[str, Any]:
        return {
            "role": "assistant",
            "content": response.content,
            "tool_calls": [tool_call.raw for tool_call in response.tool_calls],
        }

    def tool_result_messages(
        self,
        tool_results: list[tuple[LLMToolCall, Any]],
    ) -> list[dict[str, Any]]:
        return [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_call.id,
                        "content": result,
                    }
                    for tool_call, result in tool_results
                ],
            }
        ]


class FailingLLM(FakeLLM):
    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]],
        tool_choice: str,
    ) -> LLMResponse:
        self.messages.append([dict(message) for message in messages])
        self.tools.append(tools)
        raise RuntimeError("provider failed")


class SideQueryLLM:
    def __init__(self, content: str) -> None:
        self.content = content
        self.messages: list[list[dict[str, Any]]] = []

    def chat(
        self,
        messages: list[dict[str, Any]],
        **_: Any,
    ) -> LLMResponse:
        self.messages.append([dict(message) for message in messages])
        return LLMResponse(content=self.content, tool_calls=[], raw={})


def test_serial_workflow_runs_pm_engineer_qa_acceptance(
    tmp_path: Path,
) -> None:
    llm = FakeLLM(
        [
            _response(
                _create_call(
                    "call-prd",
                    kind="prd",
                    title="PRD",
                    content="Build the requested feature.",
                    status="ready",
                    metadata={"workflow_id": "wf-pass", "role": "pm"},
                ),
                _create_call(
                    "call-task",
                    kind="task_spec",
                    title="Task Spec",
                    content="Implementation scope.",
                    status="ready",
                    metadata={"workflow_id": "wf-pass", "role": "pm"},
                ),
            ),
            LLMResponse(content="planning done", tool_calls=[], raw={}),
            _response(
                _create_call(
                    "call-impl",
                    kind="implementation_report",
                    title="Implementation Report",
                    content="No code changes needed.",
                    metadata={"workflow_id": "wf-pass", "role": "engineer"},
                )
            ),
            LLMResponse(content="implementation done", tool_calls=[], raw={}),
            _response(
                _create_call(
                    "call-test",
                    kind="test_report",
                    title="Test Report",
                    content="Checks pass.",
                    metadata={
                        "workflow_id": "wf-pass",
                        "role": "qa",
                        "verdict": "pass",
                    },
                )
            ),
            LLMResponse(content="qa pass", tool_calls=[], raw={}),
            _response(
                _create_call(
                    "call-accept",
                    kind="acceptance_report",
                    title="Acceptance Report",
                    content="Accepted.",
                    metadata={"workflow_id": "wf-pass", "role": "pm"},
                )
            ),
            LLMResponse(content="accepted", tool_calls=[], raw={}),
        ]
    )
    trace = TraceRecorder.for_run(tmp_path, run_id="wf-pass")
    workflow = _workflow(tmp_path, llm)

    result = workflow.run("Build a small feature.", run_id="wf-pass", trace=trace)

    assert result.status == "completed"
    assert result.qa_verdict == "pass"
    assert result.fix_cycles == 0
    assert [phase.phase for phase in result.phases] == [
        "pm_plan",
        "engineer_implement",
        "qa_verify",
        "pm_acceptance",
    ]
    artifacts = sorted(
        ArtifactManager.for_workdir(tmp_path).list_artifacts(include_archived=True),
        key=lambda artifact: artifact.id,
    )
    assert [artifact.kind for artifact in artifacts] == [
        "prd",
        "task_spec",
        "implementation_report",
        "test_report",
        "acceptance_report",
    ]

    records = [
        json.loads(line)
        for line in trace.jsonl_path.read_text(encoding="utf-8").splitlines()
    ]
    workflow_events = [
        record["name"] for record in records if record["category"] == "workflow"
    ]
    assert workflow_events[0] == "workflow.started"
    assert "workflow.phase.completed" in workflow_events
    assert workflow_events[-1] == "workflow.completed"
    run_data = json.loads(
        (tmp_path / ".llm_agent" / "workflows" / "wf-pass" / "run.json").read_text(
            encoding="utf-8"
        )
    )
    assert run_data["status"] == "completed"
    assert run_data["qa_verdict"] == "pass"
    assert [phase["phase_key"] for phase in run_data["phases"]] == [
        "pm_plan",
        "engineer_implement",
        "qa_verify",
        "pm_acceptance",
    ]


def test_serial_workflow_runs_code_phases_in_one_worktree(
    tmp_path: Path,
) -> None:
    workdir = _repository(tmp_path)
    manager = WorktreeManager.for_workdir(workdir)
    llm = FakeLLM(
        [
            _response(
                _create_call(
                    "call-prd",
                    kind="prd",
                    title="PRD",
                    content="Change the application value.",
                    status="ready",
                ),
                _create_call(
                    "call-task",
                    kind="task_spec",
                    title="Task Spec",
                    content="Update app.py and verify it.",
                    status="ready",
                    metadata={"change_required": True},
                ),
            ),
            LLMResponse(content="planning done", tool_calls=[], raw={}),
            _response(
                _edit_call(
                    "call-edit",
                    path="app.py",
                    old_text="value = 'original'",
                    new_text="value = 'workflow'",
                ),
                _create_call(
                    "call-impl",
                    kind="implementation_report",
                    title="Implementation Report",
                    content="Updated app.py in the isolated workspace.",
                    metadata={
                        "outcome": "changed",
                        "changed_files": ["app.py"],
                    },
                ),
            ),
            LLMResponse(content="implementation done", tool_calls=[], raw={}),
            _response(
                _run_lint_call("call-lint"),
                _create_call(
                    "call-test",
                    kind="test_report",
                    title="Test Report",
                    content="Verified the isolated implementation.",
                    metadata={"verdict": "pass"},
                ),
            ),
            LLMResponse(content="qa pass", tool_calls=[], raw={}),
            _response(
                _create_call(
                    "call-accept",
                    kind="acceptance_report",
                    title="Acceptance Report",
                    content="Accepted and ready to apply.",
                )
            ),
            LLMResponse(content="accepted", tool_calls=[], raw={}),
        ]
    )
    workflow = SerialCodingWorkflow(
        llm=llm,  # type: ignore[arg-type]
        workdir=workdir,
        artifact_manager=ArtifactManager.for_workdir(workdir),
        approval_provider=AutoApprovalProvider(approved=True),
        worktree_manager=manager,
        isolation="worktree",
    )

    result = workflow.run("Change the application value.", run_id="wf-worktree")

    assert result.status == "completed"
    assert result.worktree is not None
    assert result.worktree["changed_files"] == ["app.py"]
    assert (workdir / "app.py").read_text(encoding="utf-8") == ("value = 'original'\n")
    worktree_id = result.worktree["worktree"]["id"]
    info = manager.get(worktree_id)
    isolated = Path(info.path)
    assert (isolated / "app.py").read_text(encoding="utf-8") == ("value = 'workflow'\n")
    assert (isolated / ".llm_agent" / "tool-results").is_dir()
    assert ".llm_agent" not in result.worktree["diff"]
    record = workflow.workflow_store.load_run("wf-worktree")
    assert record.worktree_id == worktree_id
    assert record.worktree_base_commit == info.base_commit
    engineer_evidence = record.checkpoints["engineer_implement"].data[
        "implementation_evidence"
    ]
    assert engineer_evidence["changed_this_phase"] is True
    assert engineer_evidence["actual_changed_files"] == ["app.py"]
    qa_evidence = record.checkpoints["qa_verify"].data["qa_evidence"]
    assert qa_evidence["successful_checks"] == 1
    assert qa_evidence["verification"][0]["outcome"] == "clean"
    assert info.path in llm.messages[2][0]["content"]
    assert info.path in llm.messages[4][0]["content"]
    assert info.path in llm.messages[6][0]["content"]
    assert "run_lint=clean" in llm.messages[6][-1]["content"]
    message_count = len(llm.messages)
    resumed = workflow.resume("wf-worktree")
    assert resumed.worktree is not None
    assert resumed.worktree["worktree"]["id"] == worktree_id
    assert len(llm.messages) == message_count
    assert [item.id for item in manager.list()] == [worktree_id]

    manager.apply(worktree_id)
    assert (workdir / "app.py").read_text(encoding="utf-8") == ("value = 'workflow'\n")
    manager.remove(worktree_id)


def test_serial_workflow_resume_reuses_persisted_worktree(
    tmp_path: Path,
) -> None:
    workdir = _repository(tmp_path)
    manager = WorktreeManager.for_workdir(workdir)
    llm = FakeLLM(_worktree_no_change_outputs("wf-reuse"))
    workflow = SerialCodingWorkflow(
        llm=llm,  # type: ignore[arg-type]
        workdir=workdir,
        artifact_manager=ArtifactManager.for_workdir(workdir),
        approval_provider=AutoApprovalProvider(approved=True),
        worktree_manager=manager,
        isolation="worktree",
    )
    assert workflow.workflow_store is not None
    record = workflow.workflow_store.create_run(
        workflow_id="wf-reuse",
        request="Resume in the existing worktree.",
        initial_artifact_ids=[],
    )
    workflow.workflow_store.mark_phase_started(
        record,
        phase_key="pm_plan",
        phase="pm_plan",
        role="PM",
        before_versions={},
    )
    artifacts = ArtifactManager.for_workdir(workdir)
    artifacts.create_artifact(
        kind="prd",
        title="PRD",
        content="Recovered plan.",
        status="ready",
    )
    artifacts.create_artifact(
        kind="task_spec",
        title="Task Spec",
        content="Recovered task.",
        status="ready",
        metadata={"change_required": False},
    )
    info = manager.create(agent_id="workflow", run_id="wf-reuse")
    record.worktree_id = info.id
    record.worktree_base_commit = info.base_commit
    workflow.workflow_store.save_run(record)

    result = workflow.resume("wf-reuse")

    assert result.status == "completed"
    assert result.worktree is not None
    assert result.worktree["worktree"]["id"] == info.id
    assert result.worktree["changed_files"] == []
    assert result.phases[1].data["implementation_evidence"]["outcome"] == ("no_change")
    assert result.phases[2].data["qa_evidence"]["successful_checks"] == 1
    assert [item.id for item in manager.list()] == [info.id]
    manager.remove(info.id)


def test_serial_workflow_resume_fails_when_worktree_is_missing(
    tmp_path: Path,
) -> None:
    workdir = _repository(tmp_path)
    manager = WorktreeManager.for_workdir(workdir)
    workflow = SerialCodingWorkflow(
        llm=FakeLLM([]),  # type: ignore[arg-type]
        workdir=workdir,
        artifact_manager=ArtifactManager.for_workdir(workdir),
        approval_provider=AutoApprovalProvider(approved=True),
        worktree_manager=manager,
        isolation="worktree",
    )
    assert workflow.workflow_store is not None
    record = workflow.workflow_store.create_run(
        workflow_id="wf-missing",
        request="Resume a missing worktree.",
        initial_artifact_ids=[],
    )
    workflow.workflow_store.mark_phase_started(
        record,
        phase_key="pm_plan",
        phase="pm_plan",
        role="PM",
        before_versions={},
    )
    artifacts = ArtifactManager.for_workdir(workdir)
    artifacts.create_artifact(
        kind="prd",
        title="PRD",
        content="Recovered plan.",
        status="ready",
    )
    artifacts.create_artifact(
        kind="task_spec",
        title="Task Spec",
        content="Recovered task.",
        status="ready",
        metadata={"change_required": True},
    )
    info = manager.create(agent_id="workflow", run_id="wf-missing")
    record.worktree_id = info.id
    record.worktree_base_commit = info.base_commit
    workflow.workflow_store.save_run(record)
    _git(workdir, "worktree", "remove", "--force", info.path)

    result = workflow.resume("wf-missing")

    assert result.status == "failed"
    assert result.error is not None
    assert "worktree setup failed" in result.error.lower()
    assert "missing" in result.error.lower()
    manager.remove(info.id)


def test_worktree_evidence_gate_rejects_report_without_code_change(
    tmp_path: Path,
) -> None:
    workdir = _repository(tmp_path)
    manager = WorktreeManager.for_workdir(workdir)
    llm = FakeLLM(
        [
            _response(
                _create_call(
                    "call-prd",
                    kind="prd",
                    title="PRD",
                    content="Change app.py.",
                    status="ready",
                ),
                _create_call(
                    "call-task",
                    kind="task_spec",
                    title="Task Spec",
                    content="Change app.py.",
                    status="ready",
                    metadata={"change_required": True},
                ),
            ),
            LLMResponse(content="planning done", tool_calls=[], raw={}),
            _response(
                _create_call(
                    "call-impl",
                    kind="implementation_report",
                    title="Implementation Report",
                    content="Claimed app.py was changed.",
                    metadata={
                        "outcome": "changed",
                        "changed_files": ["app.py"],
                    },
                )
            ),
            LLMResponse(content="implementation done", tool_calls=[], raw={}),
        ]
    )
    workflow = SerialCodingWorkflow(
        llm=llm,  # type: ignore[arg-type]
        workdir=workdir,
        artifact_manager=ArtifactManager.for_workdir(workdir),
        approval_provider=AutoApprovalProvider(approved=True),
        worktree_manager=manager,
        isolation="worktree",
        max_phase_retries=0,
    )

    result = workflow.run("Change app.py.", run_id="wf-no-diff")

    assert result.status == "failed"
    assert result.error is not None
    assert "Worktree Diff did not change" in result.error
    assert result.phases[-1].phase == "engineer_implement"
    assert result.worktree is not None
    manager.remove(result.worktree["worktree"]["id"], discard_changes=True)


def test_worktree_evidence_gate_rejects_qa_pass_without_verification(
    tmp_path: Path,
) -> None:
    workdir = _repository(tmp_path)
    manager = WorktreeManager.for_workdir(workdir)
    llm = FakeLLM(
        [
            _response(
                _create_call(
                    "call-prd",
                    kind="prd",
                    title="PRD",
                    content="Change app.py.",
                    status="ready",
                ),
                _create_call(
                    "call-task",
                    kind="task_spec",
                    title="Task Spec",
                    content="Change app.py.",
                    status="ready",
                    metadata={"change_required": True},
                ),
            ),
            LLMResponse(content="planning done", tool_calls=[], raw={}),
            _response(
                _edit_call(
                    "call-edit",
                    path="app.py",
                    old_text="value = 'original'",
                    new_text="value = 'workflow'",
                ),
                _create_call(
                    "call-impl",
                    kind="implementation_report",
                    title="Implementation Report",
                    content="Changed app.py.",
                    metadata={
                        "outcome": "changed",
                        "changed_files": ["app.py"],
                    },
                ),
            ),
            LLMResponse(content="implementation done", tool_calls=[], raw={}),
            _response(
                _create_call(
                    "call-test",
                    kind="test_report",
                    title="Test Report",
                    content="Claimed pass without running checks.",
                    metadata={"verdict": "pass"},
                )
            ),
            LLMResponse(content="qa pass", tool_calls=[], raw={}),
        ]
    )
    workflow = SerialCodingWorkflow(
        llm=llm,  # type: ignore[arg-type]
        workdir=workdir,
        artifact_manager=ArtifactManager.for_workdir(workdir),
        approval_provider=AutoApprovalProvider(approved=True),
        worktree_manager=manager,
        isolation="worktree",
        max_phase_retries=0,
    )

    result = workflow.run("Change app.py.", run_id="wf-no-check")

    assert result.status == "failed"
    assert result.error is not None
    assert "no successful run_tests or run_lint" in result.error
    assert result.phases[-1].phase == "qa_verify"
    assert result.worktree is not None
    manager.remove(result.worktree["worktree"]["id"], discard_changes=True)


def test_worktree_evidence_gate_downgrades_false_pass_and_runs_fix_cycle(
    tmp_path: Path,
) -> None:
    workdir = _repository(tmp_path)
    (workdir / "test_app.py").write_text(
        "from app import value\n\n\ndef test_value():\n    assert value == 'fixed'\n",
        encoding="utf-8",
    )
    _git(workdir, "add", "test_app.py")
    _git(workdir, "commit", "--amend", "--no-edit")
    manager = WorktreeManager.for_workdir(workdir)
    llm = FakeLLM(
        [
            _response(
                _create_call(
                    "call-prd",
                    kind="prd",
                    title="PRD",
                    content="Change app.py.",
                    status="ready",
                ),
                _create_call(
                    "call-task",
                    kind="task_spec",
                    title="Task Spec",
                    content="Change app.py and verify it.",
                    status="ready",
                    metadata={"change_required": True},
                ),
            ),
            LLMResponse(content="planning done", tool_calls=[], raw={}),
            _response(
                _edit_call(
                    "call-edit-initial",
                    path="app.py",
                    old_text="value = 'original'",
                    new_text="value = 'initial'",
                ),
                _create_call(
                    "call-impl-initial",
                    kind="implementation_report",
                    title="Implementation Report",
                    content="Initial implementation.",
                    metadata={
                        "outcome": "changed",
                        "changed_files": ["app.py"],
                    },
                ),
            ),
            LLMResponse(content="implementation done", tool_calls=[], raw={}),
            _response(
                _run_tests_call("call-tests-failed", targets=["test_app.py"]),
                _create_call(
                    "call-false-pass",
                    kind="test_report",
                    title="Test Report",
                    content="Incorrectly claimed that tests passed.",
                    metadata={"verdict": "pass"},
                ),
            ),
            LLMResponse(content="qa pass", tool_calls=[], raw={}),
            _response(
                _edit_call(
                    "call-edit-fix",
                    path="app.py",
                    old_text="value = 'initial'",
                    new_text="value = 'fixed'",
                ),
                _create_call(
                    "call-impl-fix",
                    kind="implementation_report",
                    title="Fix Implementation Report",
                    content="Fixed the machine-reported failure.",
                    metadata={
                        "outcome": "changed",
                        "changed_files": ["app.py"],
                    },
                ),
            ),
            LLMResponse(content="fix done", tool_calls=[], raw={}),
            _response(
                _run_tests_call("call-tests-passed", targets=["test_app.py"]),
                _create_call(
                    "call-regression-pass",
                    kind="test_report",
                    title="Regression Test Report",
                    content="Regression test passed.",
                    metadata={"verdict": "pass"},
                ),
            ),
            LLMResponse(content="regression pass", tool_calls=[], raw={}),
            _response(
                _create_call(
                    "call-accept",
                    kind="acceptance_report",
                    title="Acceptance Report",
                    content="Accepted after the evidence-driven fix.",
                )
            ),
            LLMResponse(content="accepted", tool_calls=[], raw={}),
        ]
    )
    workflow = SerialCodingWorkflow(
        llm=llm,  # type: ignore[arg-type]
        workdir=workdir,
        artifact_manager=ArtifactManager.for_workdir(workdir),
        approval_provider=AutoApprovalProvider(approved=True),
        worktree_manager=manager,
        isolation="worktree",
    )

    result = workflow.run("Change app.py.", run_id="wf-false-pass")

    assert result.status == "completed"
    assert result.fix_cycles == 1
    initial_qa = next(phase for phase in result.phases if phase.phase == "qa_verify")
    assert initial_qa.status == "completed"
    assert initial_qa.data["qa_verdict"] == "fail"
    evidence = initial_qa.data["qa_evidence"]
    assert evidence["reported_verdict"] == "pass"
    assert evidence["effective_verdict"] == "fail"
    assert evidence["verdict_overridden"] is True
    assert evidence["actionable_failed_checks"] == 1
    assert "run_tests=failed" in str(llm.messages[6][-1]["content"])
    assert "test_value" in str(llm.messages[6][-1]["content"])
    assert "new phase-local" in str(llm.messages[6][-1]["content"])
    assert "keyword match" in str(llm.messages[4][-1]["content"])
    assert result.worktree is not None
    manager.remove(result.worktree["worktree"]["id"], discard_changes=True)


def test_worktree_evidence_gate_retries_qa_after_permission_tool_error(
    tmp_path: Path,
) -> None:
    class DenyLintApprovalProvider:
        def approve(self, request: Any) -> bool:
            return request.tool_name != "run_lint"

    workdir = _repository(tmp_path)
    (workdir / "test_app.py").write_text(
        "from app import value\n\n\ndef test_value():\n    assert value == 'workflow'\n",
        encoding="utf-8",
    )
    _git(workdir, "add", "test_app.py")
    _git(workdir, "commit", "--amend", "--no-edit")
    manager = WorktreeManager.for_workdir(workdir)
    llm = FakeLLM(
        [
            _response(
                _create_call(
                    "call-prd",
                    kind="prd",
                    title="PRD",
                    content="Change app.py.",
                    status="ready",
                ),
                _create_call(
                    "call-task",
                    kind="task_spec",
                    title="Task Spec",
                    content="Change app.py and verify it.",
                    status="ready",
                    metadata={"change_required": True},
                ),
            ),
            LLMResponse(content="planning done", tool_calls=[], raw={}),
            _response(
                _edit_call(
                    "call-edit",
                    path="app.py",
                    old_text="value = 'original'",
                    new_text="value = 'workflow'",
                ),
                _create_call(
                    "call-impl",
                    kind="implementation_report",
                    title="Implementation Report",
                    content="Changed app.py.",
                    metadata={
                        "outcome": "changed",
                        "changed_files": ["app.py"],
                    },
                ),
            ),
            LLMResponse(content="implementation done", tool_calls=[], raw={}),
            _response(
                _run_lint_call("call-lint-denied"),
                _create_call(
                    "call-first-report",
                    kind="test_report",
                    title="Test Report",
                    content="Lint was denied but pass was claimed.",
                    metadata={"verdict": "pass"},
                ),
            ),
            LLMResponse(content="qa pass", tool_calls=[], raw={}),
            _response(
                _run_tests_call("call-tests-passed", targets=["test_app.py"]),
                _create_call(
                    "call-second-report",
                    kind="test_report",
                    title="Retry Test Report",
                    content="Focused test passed.",
                    metadata={"verdict": "pass"},
                ),
            ),
            LLMResponse(content="qa retry pass", tool_calls=[], raw={}),
            _response(
                _create_call(
                    "call-accept",
                    kind="acceptance_report",
                    title="Acceptance Report",
                    content="Accepted after QA retry.",
                )
            ),
            LLMResponse(content="accepted", tool_calls=[], raw={}),
        ]
    )
    workflow = SerialCodingWorkflow(
        llm=llm,  # type: ignore[arg-type]
        workdir=workdir,
        artifact_manager=ArtifactManager.for_workdir(workdir),
        approval_provider=DenyLintApprovalProvider(),
        worktree_manager=manager,
        isolation="worktree",
    )

    result = workflow.run("Change app.py.", run_id="wf-qa-tool-error")

    assert result.status == "completed"
    assert result.fix_cycles == 0
    qa = next(phase for phase in result.phases if phase.phase == "qa_verify")
    assert qa.attempts == 2
    assert qa.data["qa_verdict"] == "pass"
    handoffs = qa.data["attempt_handoffs"]
    assert handoffs[0]["verification"][0]["outcome"] == "tool_error"
    assert "must be retried" in handoffs[0]["gate_issue"]
    assert handoffs[1]["verification"][0]["outcome"] == "passed"
    assert not any(phase.phase == "engineer_fix" for phase in result.phases)
    assert result.worktree is not None
    manager.remove(result.worktree["worktree"]["id"], discard_changes=True)


def test_worktree_fix_cycle_requires_a_new_diff_and_regression_evidence(
    tmp_path: Path,
) -> None:
    workdir = _repository(tmp_path)
    manager = WorktreeManager.for_workdir(workdir)
    llm = FakeLLM(
        [
            _response(
                _create_call(
                    "call-prd",
                    kind="prd",
                    title="PRD",
                    content="Change app.py and fix defects.",
                    status="ready",
                ),
                _create_call(
                    "call-task",
                    kind="task_spec",
                    title="Task Spec",
                    content="Change app.py and verify it.",
                    status="ready",
                    metadata={"change_required": True},
                ),
            ),
            LLMResponse(content="planning done", tool_calls=[], raw={}),
            _response(
                _edit_call(
                    "call-edit-initial",
                    path="app.py",
                    old_text="value = 'original'",
                    new_text="value = 'initial'",
                ),
                _create_call(
                    "call-impl-initial",
                    kind="implementation_report",
                    title="Implementation Report",
                    content="Initial implementation.",
                    metadata={
                        "outcome": "changed",
                        "changed_files": ["app.py"],
                    },
                ),
            ),
            LLMResponse(content="implementation done", tool_calls=[], raw={}),
            _response(
                _create_call(
                    "call-test-fail",
                    kind="test_report",
                    title="Test Report",
                    content="A defect remains.",
                    metadata={"verdict": "fail"},
                )
            ),
            LLMResponse(content="qa fail", tool_calls=[], raw={}),
            _response(
                _edit_call(
                    "call-edit-fix",
                    path="app.py",
                    old_text="value = 'initial'",
                    new_text="value = 'fixed'",
                ),
                _create_call(
                    "call-impl-fix",
                    kind="implementation_report",
                    title="Fix Implementation Report",
                    content="Fixed the reported defect.",
                    metadata={
                        "outcome": "changed",
                        "changed_files": ["app.py"],
                    },
                ),
            ),
            LLMResponse(content="fix done", tool_calls=[], raw={}),
            _response(
                _run_lint_call("call-regression-lint"),
                _create_call(
                    "call-regression",
                    kind="test_report",
                    title="Regression Test Report",
                    content="Regression lint passes.",
                    metadata={"verdict": "pass"},
                ),
            ),
            LLMResponse(content="regression pass", tool_calls=[], raw={}),
            _response(
                _create_call(
                    "call-accept",
                    kind="acceptance_report",
                    title="Acceptance Report",
                    content="Accepted after the verified fix.",
                )
            ),
            LLMResponse(content="accepted", tool_calls=[], raw={}),
        ]
    )
    workflow = SerialCodingWorkflow(
        llm=llm,  # type: ignore[arg-type]
        workdir=workdir,
        artifact_manager=ArtifactManager.for_workdir(workdir),
        approval_provider=AutoApprovalProvider(approved=True),
        worktree_manager=manager,
        isolation="worktree",
    )

    result = workflow.run("Change app.py and fix defects.", run_id="wf-fix-evidence")

    assert result.status == "completed"
    assert result.fix_cycles == 1
    fix_phase = next(phase for phase in result.phases if phase.phase == "engineer_fix")
    fix_evidence = fix_phase.data["implementation_evidence"]
    assert fix_evidence["changed_this_phase"] is True
    assert (
        fix_evidence["worktree_before"]["diff_sha256"]
        != fix_evidence["worktree_after"]["diff_sha256"]
    )
    regression = next(
        phase for phase in result.phases if phase.phase == "qa_regression"
    )
    assert regression.data["qa_evidence"]["successful_checks"] == 1
    assert result.worktree is not None
    isolated = Path(result.worktree["worktree"]["path"])
    assert (isolated / "app.py").read_text(encoding="utf-8") == "value = 'fixed'\n"
    manager.remove(result.worktree["worktree"]["id"], discard_changes=True)


def test_serial_workflow_resume_completed_run_does_not_call_llm(
    tmp_path: Path,
) -> None:
    llm = FakeLLM(_successful_outputs("wf-done"))
    workflow = _workflow(tmp_path, llm)

    result = workflow.run("Build a completed workflow.", run_id="wf-done")
    message_count = len(llm.messages)
    resumed = workflow.resume("wf-done")

    assert result.status == "completed"
    assert resumed.status == "completed"
    assert resumed.workflow_id == "wf-done"
    assert len(llm.messages) == message_count


def test_serial_workflow_persists_role_agent_failure(tmp_path: Path) -> None:
    workflow = _workflow(tmp_path, FailingLLM([]))

    result = workflow.run("Build a feature.", run_id="wf-provider-failure")

    assert result.status == "failed"
    assert result.error is not None
    assert "provider failed" in result.error
    assert workflow.workflow_store is not None
    record = workflow.workflow_store.load_run("wf-provider-failure")
    assert record.status == "failed"
    assert record.current_phase is None
    assert record.current_phase_key is None
    assert record.checkpoints["pm_plan"].status == "failed"
    assert record.checkpoints["pm_plan"].attempts == 1
    assert record.checkpoints["pm_plan"].run_ids == ["wf-provider-failure-pm_plan-1"]
    handoff = record.checkpoints["pm_plan"].data["attempt_handoffs"][0]
    assert handoff["attempt"] == 1
    assert "provider failed" in handoff["gate_issue"]

    recovering_llm = FakeLLM(_successful_outputs("wf-provider-failure"))
    resumed = _workflow(tmp_path, recovering_llm).resume("wf-provider-failure")

    assert resumed.status == "completed"
    assert resumed.phases[0].attempts == 2
    assert "<attempt_handoff>" in str(recovering_llm.messages[0])
    assert "provider failed" in str(recovering_llm.messages[0])
    assert resumed.phases[0].run_ids == ["wf-provider-failure-pm_plan-2"]


def test_serial_workflow_resume_recovers_running_phase_when_gate_is_satisfied(
    tmp_path: Path,
) -> None:
    llm = FakeLLM(_successful_outputs("wf-recover", include_pm=False))
    workflow = _workflow(tmp_path, llm)
    assert workflow.workflow_store is not None
    record = workflow.workflow_store.create_run(
        workflow_id="wf-recover",
        request="Build from recovered planning.",
        initial_artifact_ids=[],
    )
    workflow.workflow_store.mark_phase_started(
        record,
        phase_key="pm_plan",
        phase="pm_plan",
        role="PM",
        before_versions={},
    )
    manager = ArtifactManager.for_workdir(tmp_path)
    manager.create_artifact(
        kind="prd",
        title="PRD",
        content="Recovered plan.",
        status="ready",
        metadata={"workflow_id": "wf-recover", "role": "pm"},
    )
    manager.create_artifact(
        kind="task_spec",
        title="Task Spec",
        content="Recovered task.",
        status="ready",
        metadata={"workflow_id": "wf-recover", "role": "pm"},
    )

    result = workflow.resume("wf-recover")

    assert result.status == "completed"
    assert result.phases[0].phase == "pm_plan"
    assert result.phases[0].summary.startswith("Recovered completed phase")
    assert len(llm.messages) == 6


def test_serial_workflow_resume_reruns_running_phase_when_gate_is_missing(
    tmp_path: Path,
) -> None:
    llm = FakeLLM(_successful_outputs("wf-rerun"))
    workflow = _workflow(tmp_path, llm)
    assert workflow.workflow_store is not None
    record = workflow.workflow_store.create_run(
        workflow_id="wf-rerun",
        request="Build from rerun planning.",
        initial_artifact_ids=[],
    )
    workflow.workflow_store.mark_phase_started(
        record,
        phase_key="pm_plan",
        phase="pm_plan",
        role="PM",
        before_versions={},
    )

    result = workflow.resume("wf-rerun")

    assert result.status == "completed"
    assert result.phases[0].phase == "pm_plan"
    assert result.phases[0].summary == "planning done"
    assert len(llm.messages) == 8


def test_serial_workflow_resume_uses_rolling_interrupted_attempt_state(
    tmp_path: Path,
) -> None:
    llm = FakeLLM(_successful_outputs("wf-interrupted"))
    workflow = _workflow(tmp_path, llm)
    assert workflow.workflow_store is not None
    record = workflow.workflow_store.create_run(
        workflow_id="wf-interrupted",
        request="Build from an interrupted planning attempt.",
        initial_artifact_ids=[],
    )
    workflow.workflow_store.mark_phase_started(
        record,
        phase_key="pm_plan",
        phase="pm_plan",
        role="PM",
        before_versions={},
        data={
            "current_attempt": 1,
            "current_run_id": "wf-interrupted-pm_plan-1",
            "current_attempt_state": {
                "tool_counts": {"read_file": 1},
                "recent_actions": [
                    {
                        "step": 1,
                        "tool": "read_file",
                        "target": {"path": "README.md"},
                    }
                ],
                "recent_failures": [],
                "progress": ["Inspected the existing project structure."],
            },
        },
    )

    result = workflow.resume("wf-interrupted")

    assert result.status == "completed"
    assert result.phases[0].attempts == 2
    assert result.phases[0].run_ids == ["wf-interrupted-pm_plan-2"]
    first_request = str(llm.messages[0])
    assert "<attempt_handoff>" in first_request
    assert '"agent_status": "interrupted"' in first_request
    assert "README.md" in first_request


def test_serial_workflow_accepts_max_steps_when_evidence_gate_passes(
    tmp_path: Path,
) -> None:
    workflow_id = "wf-max-steps-evidence"
    llm = FakeLLM(
        [
            _response(
                _create_call(
                    "call-prd",
                    kind="prd",
                    title="PRD",
                    content="Plan.",
                    status="ready",
                    metadata={"workflow_id": workflow_id, "role": "pm"},
                ),
                _create_call(
                    "call-task",
                    kind="task_spec",
                    title="Task Spec",
                    content="Task.",
                    status="ready",
                    metadata={"workflow_id": workflow_id, "role": "pm"},
                ),
            ),
            *_successful_outputs(workflow_id, include_pm=False),
        ]
    )
    role_specs = dict(ROLE_SPECS)
    role_specs["pm"] = replace(ROLE_SPECS["pm"], max_steps=1)
    workflow = SerialCodingWorkflow(
        llm=llm,  # type: ignore[arg-type]
        workdir=tmp_path,
        artifact_manager=ArtifactManager.for_workdir(tmp_path),
        approval_provider=AutoApprovalProvider(approved=True),
        role_specs=role_specs,
        isolation="shared",
    )

    result = workflow.run("Build a feature.", run_id=workflow_id)

    assert result.status == "completed"
    assert result.phases[0].status == "completed"
    assert result.phases[0].attempts == 1
    assert result.phases[0].data["agent_status"] == "max_steps"


def test_serial_workflow_ignores_artifacts_owned_by_another_workflow(
    tmp_path: Path,
) -> None:
    workflow_id = "wf-owned"
    llm = FakeLLM(_successful_outputs(workflow_id))
    workflow = _workflow(tmp_path, llm)
    assert workflow.workflow_store is not None
    record = workflow.workflow_store.create_run(
        workflow_id=workflow_id,
        request="Build with owned artifacts.",
        initial_artifact_ids=[],
    )
    workflow.workflow_store.mark_phase_started(
        record,
        phase_key="pm_plan",
        phase="pm_plan",
        role="PM",
        before_versions={},
    )
    artifacts = ArtifactManager.for_workdir(tmp_path)
    artifacts.create_artifact(
        kind="prd",
        title="Foreign PRD",
        content="Wrong workflow.",
        status="ready",
        metadata={"workflow_id": "wf-foreign", "role": "pm"},
    )
    artifacts.create_artifact(
        kind="task_spec",
        title="Foreign Task Spec",
        content="Wrong workflow.",
        status="ready",
        metadata={"workflow_id": "wf-foreign", "role": "pm"},
    )

    result = workflow.resume(workflow_id)

    assert result.status == "completed"
    assert result.phases[0].summary == "planning done"
    assert len(llm.messages) == 8
    assert all(
        artifacts.get_artifact(artifact_id).metadata.get("workflow_id") == workflow_id
        for artifact_id in result.artifact_ids
    )


def test_serial_workflow_retries_missing_artifact_gate(
    tmp_path: Path,
) -> None:
    llm = FakeLLM(
        [
            _response(
                _create_call(
                    "call-prd",
                    kind="prd",
                    title="PRD",
                    content="Planning only partly done.",
                    status="ready",
                    metadata={"workflow_id": "wf-retry", "role": "pm"},
                )
            ),
            LLMResponse(content="forgot task spec", tool_calls=[], raw={}),
            _response(
                _create_call(
                    "call-task",
                    kind="task_spec",
                    title="Task Spec",
                    content="Corrective task spec.",
                    status="ready",
                    metadata={"workflow_id": "wf-retry", "role": "pm"},
                )
            ),
            LLMResponse(content="planning fixed", tool_calls=[], raw={}),
            _response(
                _create_call(
                    "call-impl",
                    kind="implementation_report",
                    title="Implementation Report",
                    content="Implemented.",
                )
            ),
            LLMResponse(content="implementation done", tool_calls=[], raw={}),
            _response(
                _create_call(
                    "call-test",
                    kind="test_report",
                    title="Test Report",
                    content="Pass.",
                    metadata={"verdict": "pass"},
                )
            ),
            LLMResponse(content="qa pass", tool_calls=[], raw={}),
            _response(
                _create_call(
                    "call-accept",
                    kind="acceptance_report",
                    title="Acceptance Report",
                    content="Accepted.",
                )
            ),
            LLMResponse(content="accepted", tool_calls=[], raw={}),
        ]
    )
    workflow = _workflow(tmp_path, llm)

    result = workflow.run("Build a feature.", run_id="wf-retry")

    assert result.status == "completed"
    assert result.phases[0].phase == "pm_plan"
    assert result.phases[0].attempts == 2
    assert any(
        "did not satisfy its completion evidence gate"
        in str(message.get("content", ""))
        for call_messages in llm.messages
        for message in call_messages
    )
    retry_messages = llm.messages[2]
    assert "forgot task spec" in str(retry_messages)
    assert "<attempt_handoff>" in str(retry_messages)
    assert "Create the planning handoff artifacts" in str(retry_messages)
    run_data = json.loads(
        (tmp_path / ".llm_agent/workflows/wf-retry/run.json").read_text(
            encoding="utf-8"
        )
    )
    handoffs = run_data["checkpoints"]["pm_plan"]["data"]["attempt_handoffs"]
    assert [handoff["attempt"] for handoff in handoffs] == [1, 2]
    assert handoffs[0]["tool_counts"] == {"artifact_create": 1}
    assert "task_spec" in handoffs[0]["gate_issue"]


def test_serial_workflow_runs_fix_cycle_after_qa_fail(
    tmp_path: Path,
) -> None:
    llm = FakeLLM(
        [
            _response(
                _create_call(
                    "call-prd",
                    kind="prd",
                    title="PRD",
                    content="Need fix.",
                    status="ready",
                ),
                _create_call(
                    "call-task",
                    kind="task_spec",
                    title="Task Spec",
                    content="Implement and verify.",
                    status="ready",
                ),
            ),
            LLMResponse(content="planning done", tool_calls=[], raw={}),
            _response(
                _create_call(
                    "call-impl",
                    kind="implementation_report",
                    title="Implementation Report",
                    content="Initial implementation.",
                )
            ),
            LLMResponse(content="implementation done", tool_calls=[], raw={}),
            _response(
                _create_call(
                    "call-test-fail",
                    kind="test_report",
                    title="Test Report",
                    content="Failure found.",
                    metadata={"verdict": "fail"},
                )
            ),
            LLMResponse(content="qa fail", tool_calls=[], raw={}),
            _response(
                _update_call(
                    "call-fix",
                    artifact_id="artifact_0003",
                    content="Fixed implementation.",
                    expected_version=1,
                    change_summary="Addressed QA failure.",
                )
            ),
            LLMResponse(content="fix done", tool_calls=[], raw={}),
            _response(
                _create_call(
                    "call-test-pass",
                    kind="test_report",
                    title="Regression Test Report",
                    content="Regression passes.",
                    metadata={"verdict": "pass"},
                )
            ),
            LLMResponse(content="qa pass", tool_calls=[], raw={}),
            _response(
                _create_call(
                    "call-accept",
                    kind="acceptance_report",
                    title="Acceptance Report",
                    content="Accepted after fix.",
                )
            ),
            LLMResponse(content="accepted", tool_calls=[], raw={}),
        ]
    )
    workflow = _workflow(tmp_path, llm)

    result = workflow.run("Build and fix if needed.", run_id="wf-fix")

    assert result.status == "completed"
    assert result.qa_verdict == "pass"
    assert result.fix_cycles == 1
    assert [phase.phase for phase in result.phases] == [
        "pm_plan",
        "engineer_implement",
        "qa_verify",
        "engineer_fix",
        "qa_regression",
        "pm_acceptance",
    ]
    implementation_report = ArtifactManager.for_workdir(tmp_path).get_artifact(
        "artifact_0003"
    )
    assert implementation_report.version == 2
    assert implementation_report.content == "Fixed implementation."


def test_serial_workflow_roles_can_discover_and_load_skills(
    tmp_path: Path,
) -> None:
    _write_skill(
        tmp_path,
        "prd-writer",
        """---
name: prd-writer
description: Write implementation-ready PRDs.
when_to_use: Use during product planning.
---

# PRD Method

Clarify users, requirements, acceptance criteria, and delivery risks.
""",
    )
    _write_skill(
        tmp_path,
        "code-review",
        """---
name: code-review
description: Review patches for behavioral defects.
---

# Code Review Method

Inspect changed files and tests before approval.
""",
    )
    _write_skill(
        tmp_path,
        "qa-checklist",
        """---
name: qa-checklist
description: Verify behavior against requirements.
---

# QA Method

Map each requirement to evidence.
""",
    )
    role_specs = dict(ROLE_SPECS)
    role_specs["pm"] = replace(
        ROLE_SPECS["pm"],
        required_skills=("prd-writer",),
    )
    role_specs["engineer"] = replace(
        ROLE_SPECS["engineer"],
        optional_skills=("code-review",),
    )
    role_specs["qa"] = replace(
        ROLE_SPECS["qa"],
        optional_skills=("qa-checklist",),
    )
    llm = FakeLLM(
        [
            _response(
                _create_call(
                    "call-prd",
                    kind="prd",
                    title="PRD",
                    content="Build the requested feature.",
                    status="ready",
                ),
                _create_call(
                    "call-task",
                    kind="task_spec",
                    title="Task Spec",
                    content="Implementation scope.",
                    status="ready",
                ),
            ),
            LLMResponse(content="planning done", tool_calls=[], raw={}),
            _response(
                _create_call(
                    "call-impl",
                    kind="implementation_report",
                    title="Implementation Report",
                    content="Implemented.",
                )
            ),
            LLMResponse(content="implementation done", tool_calls=[], raw={}),
            _response(
                _create_call(
                    "call-test",
                    kind="test_report",
                    title="Test Report",
                    content="Pass.",
                    metadata={"verdict": "pass"},
                )
            ),
            LLMResponse(content="qa pass", tool_calls=[], raw={}),
            _response(
                _create_call(
                    "call-accept",
                    kind="acceptance_report",
                    title="Acceptance Report",
                    content="Accepted.",
                )
            ),
            LLMResponse(content="accepted", tool_calls=[], raw={}),
        ]
    )
    workflow = SerialCodingWorkflow(
        llm=llm,  # type: ignore[arg-type]
        workdir=tmp_path,
        artifact_manager=ArtifactManager.for_workdir(tmp_path),
        approval_provider=AutoApprovalProvider(approved=True),
        skill_registry=SkillRegistry.for_workdir(tmp_path),
        role_specs=role_specs,
        isolation="shared",
    )

    result = workflow.run("Build a feature with role skills.", run_id="wf-skills")

    assert result.status == "completed"
    pm_system_prompt = llm.messages[0][0]["content"]
    engineer_system_prompt = llm.messages[2][0]["content"]
    qa_system_prompt = llm.messages[4][0]["content"]
    assert "prd-writer: Write implementation-ready PRDs." in pm_system_prompt
    assert "PRD Method" in pm_system_prompt
    assert (
        "code-review: Review patches for behavioral defects." in engineer_system_prompt
    )
    assert "Code Review Method" not in engineer_system_prompt
    assert "qa-checklist: Verify behavior against requirements." in qa_system_prompt
    assert "QA Method" not in qa_system_prompt
    skill_tools = {"skill_list", "skill_load", "skill_read_resource"}
    assert all(
        skill_tools <= {tool["name"] for tool in tool_specs} for tool_specs in llm.tools
    )
    engineer_tools = {tool["name"] for tool in llm.tools[2]}
    assert {"task_create", "task_claim", "task_complete"} <= engineer_tools


def test_workflow_roles_recall_shared_project_memory(tmp_path: Path) -> None:
    memory_manager = MemoryManager.for_workdir(tmp_path)
    memory = memory_manager.remember(
        name="Project test command",
        memory_type="reference",
        description="How to run the project tests.",
        body="Run `uv run pytest` from the project root.",
        pinned=True,
    )
    llm = FakeLLM(_successful_outputs("wf-memory"))
    workflow = SerialCodingWorkflow(
        llm=llm,  # type: ignore[arg-type]
        workdir=tmp_path,
        artifact_manager=ArtifactManager.for_workdir(tmp_path),
        approval_provider=AutoApprovalProvider(approved=True),
        memory_manager=memory_manager,
        isolation="shared",
    )

    result = workflow.run("Build with project conventions.", run_id="wf-memory")

    assert result.status == "completed"
    assert any(memory.id in str(messages) for messages in llm.messages)
    assert {"memory_search", "memory_get"} <= {tool["name"] for tool in llm.tools[0]}
    assert "memory_remember" not in {tool["name"] for tool in llm.tools[0]}
    assert memory_manager.get(memory.id).use_count == 4


def test_completed_workflow_reflects_verified_long_term_memory(
    tmp_path: Path,
) -> None:
    reflection_llm = SideQueryLLM(
        """[{
          "name": "Project test command",
          "type": "reference",
          "description": "Verified project test command.",
          "body": "Run `uv run pytest` from the repository root.",
          "confidence": 0.9
        }]"""
    )
    memory_manager = MemoryManager.for_workdir(
        tmp_path,
        llm=reflection_llm,  # type: ignore[arg-type]
    )
    workflow = SerialCodingWorkflow(
        llm=FakeLLM(_successful_outputs("wf-reflection")),  # type: ignore[arg-type]
        workdir=tmp_path,
        artifact_manager=ArtifactManager.for_workdir(tmp_path),
        approval_provider=AutoApprovalProvider(approved=True),
        memory_manager=memory_manager,
        isolation="shared",
    )

    result = workflow.run(
        "Remember this convention while building and verifying a feature.",
        run_id="wf-reflection",
    )

    assert result.status == "completed"
    assert len(reflection_llm.messages) == 1
    memories = memory_manager.list()
    assert len(memories) == 1
    assert memories[0].source == "workflow_reflection"
    assert memories[0].source_run_id == "wf-reflection"


def test_parse_workflow_command() -> None:
    assert parse_workflow_command("/workflow build feature") == "build feature"
    assert parse_workflow_command("/workflow\nbuild feature") == "build feature"
    assert parse_workflow_command("/workflow") == ""
    assert parse_workflow_command("/workflowish build") is None
    assert parse_workflow_command("normal prompt") is None


def _workflow(tmp_path: Path, llm: FakeLLM) -> SerialCodingWorkflow:
    return SerialCodingWorkflow(
        llm=llm,  # type: ignore[arg-type]
        workdir=tmp_path,
        artifact_manager=ArtifactManager.for_workdir(tmp_path),
        approval_provider=AutoApprovalProvider(approved=True),
        isolation="shared",
    )


def _git(workdir: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=workdir,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _repository(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "agent@example.com")
    _git(tmp_path, "config", "user.name", "Agent Test")
    (tmp_path / ".gitignore").write_text(
        ".llm_agent/\n__pycache__/\n.pytest_cache/\n",
        encoding="utf-8",
    )
    (tmp_path / "app.py").write_text("value = 'original'\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "initial")
    return tmp_path


def _successful_outputs(
    workflow_id: str,
    *,
    include_pm: bool = True,
) -> list[LLMResponse]:
    outputs: list[LLMResponse] = []
    if include_pm:
        outputs.extend(
            [
                _response(
                    _create_call(
                        f"{workflow_id}-prd",
                        kind="prd",
                        title="PRD",
                        content="Build the requested feature.",
                        status="ready",
                        metadata={"workflow_id": workflow_id, "role": "pm"},
                    ),
                    _create_call(
                        f"{workflow_id}-task",
                        kind="task_spec",
                        title="Task Spec",
                        content="Implementation scope.",
                        status="ready",
                        metadata={"workflow_id": workflow_id, "role": "pm"},
                    ),
                ),
                LLMResponse(content="planning done", tool_calls=[], raw={}),
            ]
        )
    outputs.extend(
        [
            _response(
                _create_call(
                    f"{workflow_id}-impl",
                    kind="implementation_report",
                    title="Implementation Report",
                    content="No code changes needed.",
                    metadata={"workflow_id": workflow_id, "role": "engineer"},
                )
            ),
            LLMResponse(content="implementation done", tool_calls=[], raw={}),
            _response(
                _create_call(
                    f"{workflow_id}-test",
                    kind="test_report",
                    title="Test Report",
                    content="Checks pass.",
                    metadata={
                        "workflow_id": workflow_id,
                        "role": "qa",
                        "verdict": "pass",
                    },
                )
            ),
            LLMResponse(content="qa pass", tool_calls=[], raw={}),
            _response(
                _create_call(
                    f"{workflow_id}-accept",
                    kind="acceptance_report",
                    title="Acceptance Report",
                    content="Accepted.",
                    metadata={"workflow_id": workflow_id, "role": "pm"},
                )
            ),
            LLMResponse(content="accepted", tool_calls=[], raw={}),
        ]
    )
    return outputs


def _worktree_no_change_outputs(workflow_id: str) -> list[LLMResponse]:
    return [
        _response(
            _create_call(
                f"{workflow_id}-impl",
                kind="implementation_report",
                title="Implementation Report",
                content="No project file changes were required.",
                metadata={
                    "workflow_id": workflow_id,
                    "role": "engineer",
                    "outcome": "no_change",
                    "changed_files": [],
                    "no_change_reason": "The recovered task is verify-only.",
                },
            )
        ),
        LLMResponse(content="implementation done", tool_calls=[], raw={}),
        _response(
            _run_lint_call(f"{workflow_id}-lint"),
            _create_call(
                f"{workflow_id}-test",
                kind="test_report",
                title="Test Report",
                content="Lint passes.",
                metadata={
                    "workflow_id": workflow_id,
                    "role": "qa",
                    "verdict": "pass",
                },
            ),
        ),
        LLMResponse(content="qa pass", tool_calls=[], raw={}),
        _response(
            _create_call(
                f"{workflow_id}-accept",
                kind="acceptance_report",
                title="Acceptance Report",
                content="Accepted.",
                metadata={"workflow_id": workflow_id, "role": "pm"},
            )
        ),
        LLMResponse(content="accepted", tool_calls=[], raw={}),
    ]


def _write_skill(tmp_path: Path, directory: str, manifest: str) -> None:
    skill_dir = tmp_path / ".llm_agent" / "skills" / directory
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(manifest, encoding="utf-8")


def _response(*tool_calls: LLMToolCall) -> LLMResponse:
    return LLMResponse(content="", tool_calls=list(tool_calls), raw={})


def _create_call(
    call_id: str,
    *,
    kind: str,
    title: str,
    content: str,
    status: str = "draft",
    metadata: dict[str, Any] | None = None,
) -> LLMToolCall:
    arguments: dict[str, Any] = {
        "kind": kind,
        "title": title,
        "content": content,
        "status": status,
    }
    if metadata is not None:
        arguments["metadata"] = metadata
    return LLMToolCall(
        id=call_id,
        name="artifact_create",
        arguments=arguments,
        raw={"id": call_id, "name": "artifact_create"},
    )


def _edit_call(
    call_id: str,
    *,
    path: str,
    old_text: str,
    new_text: str,
) -> LLMToolCall:
    return LLMToolCall(
        id=call_id,
        name="edit_file",
        arguments={
            "path": path,
            "old_text": old_text,
            "new_text": new_text,
        },
        raw={"id": call_id, "name": "edit_file"},
    )


def _run_lint_call(call_id: str) -> LLMToolCall:
    return LLMToolCall(
        id=call_id,
        name="run_lint",
        arguments={},
        raw={"id": call_id, "name": "run_lint"},
    )


def _run_tests_call(
    call_id: str,
    *,
    targets: list[str] | None = None,
) -> LLMToolCall:
    arguments: dict[str, Any] = {}
    if targets is not None:
        arguments["targets"] = targets
    return LLMToolCall(
        id=call_id,
        name="run_tests",
        arguments=arguments,
        raw={"id": call_id, "name": "run_tests"},
    )


def _update_call(
    call_id: str,
    *,
    artifact_id: str,
    content: str,
    expected_version: int,
    change_summary: str,
) -> LLMToolCall:
    return LLMToolCall(
        id=call_id,
        name="artifact_update",
        arguments={
            "artifact_id": artifact_id,
            "content": content,
            "expected_version": expected_version,
            "change_summary": change_summary,
        },
        raw={"id": call_id, "name": "artifact_update"},
    )
