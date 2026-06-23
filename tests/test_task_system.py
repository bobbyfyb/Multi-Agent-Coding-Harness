from pathlib import Path

from llm_agent.task_system import TaskManager, TaskSystemError
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
