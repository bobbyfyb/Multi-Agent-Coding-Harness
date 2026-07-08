import json
from pathlib import Path
from typing import Any

from llm_agent.artifact_system import ArtifactManager
from llm_agent.hooks.permission_hooks import AutoApprovalProvider
from llm_agent.llm_client import LLMResponse, LLMToolCall
from llm_agent.serial_workflow import SerialCodingWorkflow, parse_workflow_command
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
