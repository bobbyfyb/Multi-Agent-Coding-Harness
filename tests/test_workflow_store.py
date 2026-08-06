from pathlib import Path

import pytest

from llm_agent.workflow_store import WorkflowStore, WorkflowStoreError


def test_workflow_store_persists_run_and_phase_checkpoints(
    tmp_path: Path,
) -> None:
    store = WorkflowStore.for_workdir(tmp_path)
    record = store.create_run(
        workflow_id="wf-store",
        request="Build a workflow.",
        initial_artifact_ids=["artifact_old"],
    )
    record.worktree_id = "wt_123456789abc"
    record.worktree_base_commit = "abc123"
    store.save_run(record)

    store.mark_phase_started(
        record,
        phase_key="pm_plan",
        phase="pm_plan",
        role="PM",
        before_versions={"artifact_old": 1},
        data={"evidence": {"diff_sha256": "abc123"}},
    )
    store.mark_phase_completed(
        record,
        phase_key="pm_plan",
        phase_result={
            "phase": "pm_plan",
            "role": "PM",
            "status": "completed",
            "run_ids": ["wf-store-pm_plan-1"],
            "artifact_ids": ["artifact_0001"],
            "summary": "done",
            "attempts": 1,
            "error": None,
            "data": {
                "agent_status": "completed",
                "evidence": {"diff_sha256": "abc123"},
            },
        },
        attempts=1,
        run_ids=["wf-store-pm_plan-1"],
        artifact_ids=["artifact_0001"],
        data={
            "agent_status": "completed",
            "evidence": {"diff_sha256": "abc123"},
        },
    )
    store.mark_finished(
        record,
        status="completed",
        artifact_ids=["artifact_0001"],
        qa_verdict="pass",
        fix_cycles=0,
        error=None,
    )

    loaded = store.load_run("wf-store")
    assert loaded.status == "completed"
    assert loaded.initial_artifact_ids == ["artifact_old"]
    assert loaded.artifact_ids == ["artifact_0001"]
    assert loaded.worktree_id == "wt_123456789abc"
    assert loaded.worktree_base_commit == "abc123"
    assert loaded.checkpoints["pm_plan"].before_versions == {"artifact_old": 1}
    assert loaded.checkpoints["pm_plan"].data == {
        "agent_status": "completed",
        "evidence": {"diff_sha256": "abc123"}
    }
    assert loaded.phases[0]["phase_key"] == "pm_plan"
    assert store.list_runs()[0].workflow_id == "wf-store"


@pytest.mark.parametrize("workflow_id", ["", ".", "..", "../escape", "a/b"])
def test_workflow_store_rejects_unsafe_run_ids(
    tmp_path: Path,
    workflow_id: str,
) -> None:
    store = WorkflowStore.for_workdir(tmp_path)

    with pytest.raises(WorkflowStoreError, match="Invalid workflow id"):
        store.create_run(
            workflow_id=workflow_id,
            request="unsafe",
            initial_artifact_ids=[],
        )


def test_workflow_store_rejects_noncompleted_agent_phase_completion(
    tmp_path: Path,
) -> None:
    store = WorkflowStore.for_workdir(tmp_path)
    record = store.create_run(
        workflow_id="wf-invalid-completion",
        request="continue safely",
        initial_artifact_ids=[],
    )
    store.mark_phase_started(
        record,
        phase_key="pm_plan",
        phase="pm_plan",
        role="PM",
        before_versions={},
    )

    with pytest.raises(WorkflowStoreError, match="agent_status"):
        store.mark_phase_completed(
            record,
            phase_key="pm_plan",
            phase_result={
                "phase": "pm_plan",
                "role": "PM",
                "status": "completed",
                "data": {"agent_status": "max_steps"},
            },
            attempts=1,
            run_ids=["wf-invalid-completion-pm_plan-1"],
            artifact_ids=[],
            data={"agent_status": "max_steps"},
        )
