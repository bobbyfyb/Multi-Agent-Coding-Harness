import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from llm_agent.artifact_system import ArtifactManager
from llm_agent.hooks.permission_hooks import AutoApprovalProvider
from llm_agent.llm_client import LLMResponse, LLMToolCall
from llm_agent.serial_workflow import (
    ROLE_SPECS,
    SerialCodingWorkflow,
    parse_workflow_command,
)
from llm_agent.skill_system import SkillRegistry
from llm_agent.trace_system import TraceRecorder


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
        "did not satisfy its artifact gate" in str(message.get("content", ""))
        for call_messages in llm.messages
        for message in call_messages
    )


def test_serial_workflow_runs_fix_cycle_after_qa_fail(
    tmp_path: Path,
) -> None:
    llm = FakeLLM(
        [
            _response(
                _create_call("call-prd", kind="prd", title="PRD", content="Need fix.", status="ready"),
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
    )

    result = workflow.run("Build a feature with role skills.", run_id="wf-skills")

    assert result.status == "completed"
    pm_system_prompt = llm.messages[0][0]["content"]
    engineer_system_prompt = llm.messages[2][0]["content"]
    qa_system_prompt = llm.messages[4][0]["content"]
    assert "prd-writer: Write implementation-ready PRDs." in pm_system_prompt
    assert "PRD Method" in pm_system_prompt
    assert (
        "code-review: Review patches for behavioral defects."
        in engineer_system_prompt
    )
    assert "Code Review Method" not in engineer_system_prompt
    assert "qa-checklist: Verify behavior against requirements." in qa_system_prompt
    assert "QA Method" not in qa_system_prompt
    skill_tools = {"skill_list", "skill_load", "skill_read_resource"}
    assert all(
        skill_tools <= {tool["name"] for tool in tool_specs}
        for tool_specs in llm.tools
    )


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
    )


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
