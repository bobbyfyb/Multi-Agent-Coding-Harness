from pathlib import Path

from llm_agent.workflow_store import WorkflowStore


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
            "data": {},
        },
        attempts=1,
        run_ids=["wf-store-pm_plan-1"],
        artifact_ids=["artifact_0001"],
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
    assert loaded.phases[0]["phase_key"] == "pm_plan"
    assert store.list_runs()[0].workflow_id == "wf-store"
