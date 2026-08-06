from pathlib import Path

import pytest

from llm_agent.task_system import TaskManager, TaskSystemError
from llm_agent.tools import build_default_registry, register_default_tools
from llm_agent.tools.task_tools import register_tools
from llm_agent.tool_registry import ToolRegistry


def test_task_manager_persists_tasks_across_instances(tmp_path: Path) -> None:
    first_manager = TaskManager.for_workdir(tmp_path)
    task = first_manager.create_task(
        "Implement trace recorder",
        description="Record agent events.",
        scope="project",
        owner="pm",
    )

    second_manager = TaskManager.for_workdir(tmp_path)
    restored = second_manager.get_task(task.id)

    assert restored.id == "task_0001"
    assert restored.title == "Implement trace recorder"
    assert restored.scope == "project"
    assert restored.owner == "pm"
    assert (
        tmp_path
        / ".llm_agent"
        / "tasks"
        / "default"
        / "task_0001.json"
    ).exists()


def test_task_manager_enforces_dependencies_and_reports_unblocked(
    tmp_path: Path,
) -> None:
    manager = TaskManager.for_workdir(tmp_path)
    schema = manager.create_task("Design task schema")
    tests = manager.create_task("Write task tests", blocked_by=[schema.id])

    try:
        manager.claim_task(tests.id)
    except TaskSystemError as exc:
        assert "blocked" in str(exc)
    else:  # pragma: no cover - defensive assertion.
        raise AssertionError("Expected blocked task claim to fail.")

    manager.claim_task(schema.id, owner="backend")
    completed, unblocked = manager.complete_task(schema.id, evidence="schema reviewed")

    assert completed.status == "completed"
    assert completed.evidence == "schema reviewed"
    assert [task.id for task in unblocked] == [tests.id]
    assert manager.claim_task(tests.id, owner="qa").owner == "qa"


def test_task_manager_restricts_update_status_transitions(tmp_path: Path) -> None:
    manager = TaskManager.for_workdir(tmp_path)
    task = manager.create_task("Implement workflow task graph")

    with pytest.raises(TaskSystemError, match="pending to completed"):
        manager.update_task(task.id, status="completed", evidence="not enough")

    assert manager.get_task(task.id).status == "pending"

    manager.claim_task(task.id, owner="engineer")
    with pytest.raises(TaskSystemError, match="requires evidence"):
        manager.update_task(task.id, status="completed")

    completed = manager.update_task(
        task.id,
        status="completed",
        evidence="uv run pytest tests/test_task_system.py",
    )
    assert completed.status == "completed"

    with pytest.raises(TaskSystemError, match="completed to in_progress"):
        manager.update_task(task.id, status="in_progress")

    assert manager.get_task(task.id).status == "completed"


def test_task_complete_requires_evidence(tmp_path: Path) -> None:
    manager = TaskManager.for_workdir(tmp_path)
    task = manager.create_task("Small delegated task")
    manager.claim_task(task.id)

    with pytest.raises(TaskSystemError, match="requires evidence"):
        manager.complete_task(task.id)

    completed, _ = manager.complete_task(task.id, evidence="reviewed diff")
    assert completed.status == "completed"
    assert completed.evidence == "reviewed diff"


def test_task_summary_includes_bounded_redacted_working_details(
    tmp_path: Path,
) -> None:
    manager = TaskManager.for_workdir(tmp_path)
    task = manager.create_task(
        "Continue implementation",
        description="Inspect the current diff, then finish the remaining endpoint work.",
        notes="password=do-not-expose Continue from server.py.",
    )
    manager.update_task(
        task.id,
        evidence="Existing smoke check failed at the SSE assertion.",
    )

    summary = manager.summary(max_field_chars=48, max_chars=300)

    assert "description: Inspect the current diff" in summary
    assert "notes: [REDACTED] Continue from server.py." in summary
    assert "do-not-expose" not in summary
    assert "evidence: Existing smoke check failed" in summary
    assert len(summary) <= 300

    truncated = manager.summary(max_chars=80)
    assert len(truncated) <= 80
    assert truncated.endswith("... (task summary truncated)")


def test_task_tools_register_persistent_task_tools(tmp_path: Path) -> None:
    registry = ToolRegistry()
    register_tools(registry, workdir=tmp_path)

    created = registry.call(
        "task_create",
        {
            "title": "Add run_tests tool",
            "scope": "session",
            "owner": "agent",
        },
    )
    listed = registry.call("task_list", {"include_completed": False})

    assert created["ok"] is True
    assert "task_0001" in created["result"]
    assert listed["ok"] is True
    assert "task_0001 [pending/session] owner=agent" in listed["result"]


def test_task_tools_wrap_task_errors(tmp_path: Path) -> None:
    registry = ToolRegistry()
    register_tools(registry, workdir=tmp_path)

    result = registry.call("task_claim", {"task_id": "missing"})

    assert result["ok"] is False
    assert "Task not found" in result["error"]


def test_default_registry_uses_requested_task_list(tmp_path: Path) -> None:
    registry = build_default_registry(tmp_path, task_list_id="run-isolated")

    created = registry.call("task_create", {"title": "Scoped task"})

    assert created["ok"] is True
    assert TaskManager.for_workdir(
        tmp_path,
        task_list_id="run-isolated",
    ).get_task("task_0001").title == "Scoped task"
    assert TaskManager.for_workdir(tmp_path).list_tasks() == []


@pytest.mark.parametrize("task_list_id", ["", ".", "..", "../escape", "a/b"])
def test_task_manager_rejects_unsafe_task_list_id(
    tmp_path: Path,
    task_list_id: str,
) -> None:
    with pytest.raises(TaskSystemError, match="Invalid task list id"):
        TaskManager.for_workdir(tmp_path, task_list_id=task_list_id)


def test_register_default_tools_uses_requested_task_list(tmp_path: Path) -> None:
    registry = ToolRegistry()
    register_default_tools(
        registry,
        workdir=tmp_path,
        task_list_id="run-registered",
    )

    created = registry.call("task_create", {"title": "Registered scoped task"})

    assert created["ok"] is True
    assert TaskManager.for_workdir(
        tmp_path,
        task_list_id="run-registered",
    ).get_task("task_0001").title == "Registered scoped task"
