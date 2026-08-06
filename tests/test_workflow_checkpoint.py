import json

import pytest

from llm_agent.task_system import Task
from llm_agent.workflow_checkpoint import (
    AttemptCheckpoint,
    CHECKPOINT_SCHEMA_VERSION,
    MAX_ARTIFACT_IDS,
    MAX_CHANGED_FILES,
    MAX_OBJECTIVE_CHARS,
    MAX_OPEN_TASKS,
    MAX_RECENT_ACTIONS,
    MAX_RECENT_FAILURES,
    MAX_SEMANTIC_ITEM_CHARS,
    MAX_SEMANTIC_ITEMS,
    MAX_SUMMARY_CHARS,
    MAX_TOOL_COUNTS,
    MAX_VERIFICATION_ITEMS,
    build_attempt_checkpoint,
)


def test_build_attempt_checkpoint_captures_bounded_working_state() -> None:
    checkpoint = build_attempt_checkpoint(
        phase_key="engineer_implement",
        attempt=2,
        run_id="run-123-engineer-2",
        agent_status="max_steps",
        continuation_reason="max_steps",
        objective="Finish the local API server.",
        summary="Need to continue from the existing implementation.",
        semantic={
            "completed": [f"completed-{index}-" + "x" * 1_000 for index in range(12)],
            "remaining": ["exercise the API contract"],
            "next_actions": ["run focused tests"],
            "decisions": ["reuse the existing worktree"],
        },
        attempt_state={
            "tool_counts": {f"tool-{index}": index for index in range(40)},
            "recent_actions": [
                {"step": index, "tool": "read_file", "payload": "x" * 2_000}
                for index in range(20)
            ],
            "recent_failures": [
                {"step": index, "error": f"failure-{index}"}
                for index in range(10)
            ],
        },
        verification=[
            {"attempt": 2, "outcome": "failed", "index": index}
            for index in range(20)
        ],
        worktree_snapshot={
            "diff_sha256": "a" * 64,
            "changed_files": [f"src/file_{index}.py" for index in range(50)],
        },
        task_snapshot=[
            Task(
                id=f"task_{index:04d}",
                title=f"Task {index}",
                description="d" * 2_000,
                status="pending" if index != 2 else "completed",
                notes="n" * 2_000,
                metadata={"workflow_id": "wf-123", "values": list(range(50))},
            )
            for index in range(25)
        ],
        artifact_ids=[f"artifact_{index:04d}" for index in range(30)],
    )

    assert checkpoint.phase_key == "engineer_implement"
    assert checkpoint.attempt == 2
    assert checkpoint.objective == "Finish the local API server."
    assert checkpoint.worktree_diff_sha256 == "a" * 64
    assert len(checkpoint.completed) == MAX_SEMANTIC_ITEMS
    assert all(
        len(item) <= MAX_SEMANTIC_ITEM_CHARS for item in checkpoint.completed
    )
    assert checkpoint.blockers == [
        "failure-4",
        "failure-5",
        "failure-6",
        "failure-7",
        "failure-8",
        "failure-9",
    ]
    assert len(checkpoint.changed_files) == MAX_CHANGED_FILES
    assert len(checkpoint.open_tasks) == MAX_OPEN_TASKS
    assert all(task["status"] == "pending" for task in checkpoint.open_tasks)
    assert all(len(task["description"]) == 800 for task in checkpoint.open_tasks)
    assert len(checkpoint.verification) == MAX_VERIFICATION_ITEMS
    assert checkpoint.verification[0]["index"] == 8
    assert len(checkpoint.tool_counts) == MAX_TOOL_COUNTS
    assert len(checkpoint.recent_actions) == MAX_RECENT_ACTIONS
    assert checkpoint.recent_actions[0]["step"] == 8
    assert len(checkpoint.recent_actions[0]["payload"]) == 1_000
    assert len(checkpoint.recent_failures) == MAX_RECENT_FAILURES
    assert len(checkpoint.artifact_ids) == MAX_ARTIFACT_IDS
    json.dumps(checkpoint.to_dict(), allow_nan=False)


def test_build_attempt_checkpoint_parses_labelled_summary_without_llm() -> None:
    checkpoint = build_attempt_checkpoint(
        phase_key="engineer_fix_1",
        attempt=1,
        run_id="run-fix-1",
        agent_status="max_steps",
        continuation_reason="max_steps",
        summary="""
        ## Completed
        - Added the API router
        - Added health handling

        Remaining:
        1. Exercise the approval flow

        Next steps: Run the contract test

        Decisions
        * Keep one runtime per run

        Blockers:
        - Missing a valid manager constructor
        """,
    )

    assert checkpoint.completed == ["Added the API router", "Added health handling"]
    assert checkpoint.remaining == ["Exercise the approval flow"]
    assert checkpoint.next_actions == ["Run the contract test"]
    assert checkpoint.decisions == ["Keep one runtime per run"]
    assert checkpoint.blockers == ["Missing a valid manager constructor"]


def test_build_attempt_checkpoint_uses_unstructured_summary_as_fallback() -> None:
    incomplete = build_attempt_checkpoint(
        phase_key="engineer_implement",
        attempt=1,
        run_id="run-1",
        agent_status="max_steps",
        continuation_reason="max_steps",
        summary="Finish wiring the server and rerun its tests.",
    )
    completed = build_attempt_checkpoint(
        phase_key="qa_verify",
        attempt=1,
        run_id="run-2",
        agent_status="completed",
        continuation_reason="phase_completed",
        summary="All required contract checks passed.",
    )

    assert incomplete.remaining == ["Finish wiring the server and rerun its tests."]
    assert incomplete.next_actions == [
        "Finish wiring the server and rerun its tests."
    ]
    assert incomplete.completed == []
    assert completed.completed == ["All required contract checks passed."]
    assert completed.remaining == []


def test_build_attempt_checkpoint_uses_objective_when_working_state_is_empty() -> None:
    checkpoint = build_attempt_checkpoint(
        phase_key="engineer_implement",
        attempt=1,
        run_id="run-1",
        agent_status="interrupted",
        continuation_reason="interrupted",
        objective="Implement the API contract. " + "x" * MAX_OBJECTIVE_CHARS,
    )

    assert len(checkpoint.objective) == MAX_OBJECTIVE_CHARS
    assert checkpoint.remaining == [checkpoint.objective[:MAX_SEMANTIC_ITEM_CHARS]]
    assert checkpoint.next_actions == checkpoint.remaining


def test_completed_checkpoint_does_not_treat_objective_as_remaining_work() -> None:
    checkpoint = build_attempt_checkpoint(
        phase_key="qa_verify",
        attempt=1,
        run_id="run-qa-1",
        agent_status="completed",
        continuation_reason="phase_completed",
        objective="Verify the API contract.",
    )

    assert checkpoint.remaining == []
    assert checkpoint.next_actions == []


def test_attempt_checkpoint_round_trips_and_rebounds_loaded_data() -> None:
    original = build_attempt_checkpoint(
        phase_key="qa_regression_1",
        attempt=3,
        run_id="run-qa-3",
        agent_status="interrupted",
        continuation_reason="interrupted",
        summary="Remaining:\n- rerun the regression suite",
        attempt_state={"tool_counts": {"run_tests": 2}},
        task_snapshot={
            "tasks": [
                {
                    "id": "task_0001",
                    "title": "Verify API",
                    "status": "in_progress",
                },
                {
                    "id": "task_0002",
                    "title": "Old task",
                    "status": "completed",
                },
            ]
        },
    )

    loaded = AttemptCheckpoint.from_dict(original.to_dict())

    assert loaded == original
    assert loaded.open_tasks == [
        {
            "id": "task_0001",
            "title": "Verify API",
            "description": "",
            "status": "in_progress",
            "owner": None,
            "blocked_by": [],
            "parent_id": None,
            "evidence": None,
            "notes": None,
            "metadata": {},
        }
    ]

    oversized = original.to_dict()
    oversized["summary"] = "s" * (MAX_SUMMARY_CHARS + 500)
    oversized["remaining"] = ["r" * 2_000] * (MAX_SEMANTIC_ITEMS + 10)
    oversized["attempt"] = 10**20
    rebound = AttemptCheckpoint.from_dict(oversized)

    assert len(rebound.summary) == MAX_SUMMARY_CHARS
    assert len(rebound.remaining) == 1
    assert len(rebound.remaining[0]) == MAX_SEMANTIC_ITEM_CHARS
    assert rebound.attempt == 1_000_000


def test_attempt_checkpoint_rejects_unknown_schema() -> None:
    with pytest.raises(ValueError, match="Unsupported attempt checkpoint schema"):
        AttemptCheckpoint.from_dict({"schema_version": CHECKPOINT_SCHEMA_VERSION + 1})
