import json
from pathlib import Path

import pytest

from llm_agent.artifact_system import (
    ArtifactManager,
    ArtifactSystemError,
    format_artifact_context,
)
from llm_agent.hooks import HookContext
from llm_agent.hooks.artifact_hooks import ArtifactContextHook
from llm_agent.task_system import TaskManager
from llm_agent.tool_registry import ToolRegistry
from llm_agent.tools.artifact_tools import register_tools
from llm_agent.trace_system import TraceRecorder, trace_scope


def test_artifact_manager_creates_updates_and_filters(
    tmp_path: Path,
) -> None:
    manager = ArtifactManager.for_workdir(tmp_path)

    artifact = manager.create_artifact(
        kind="prd",
        title="Smoke PRD",
        content="# Goal\nBuild the thing.",
        status="ready",
        task_id="task_0001",
        owner="pm",
        metadata={"source": "test"},
    )
    updated = manager.update_artifact(
        artifact.id,
        content="# Goal\nBuild the thing safely.",
        status="accepted",
        expected_version=1,
        change_summary="Accepted with safety wording.",
    )
    reloaded = ArtifactManager.for_workdir(tmp_path).get_artifact(artifact.id)

    assert artifact.id == "artifact_0001"
    assert updated.version == 2
    assert updated.history[-1]["fields"] == ["content", "status"]
    assert reloaded.content.endswith("safely.")
    assert manager.list_artifacts(kind="prd", status="accepted") == [reloaded]

    with pytest.raises(ArtifactSystemError, match="version conflict"):
        manager.update_artifact(
            artifact.id,
            content="stale overwrite",
            expected_version=1,
        )


def test_artifact_tools_register_create_list_get_and_update(
    tmp_path: Path,
) -> None:
    registry = ToolRegistry()
    register_tools(registry, workdir=tmp_path)

    created = registry.call(
        "artifact_create",
        {
            "kind": "task_spec",
            "title": "Implement security patch",
            "content": "## Scope\nTighten tool safety.",
            "status": "draft",
            "task_id": "task_0002",
        },
    )
    listed = registry.call("artifact_list", {"kind": "task_spec"})
    fetched = registry.call("artifact_get", {"artifact_id": "artifact_0001"})
    updated = registry.call(
        "artifact_update",
        {
            "artifact_id": "artifact_0001",
            "status": "ready",
            "expected_version": 1,
            "change_summary": "Ready for implementation.",
        },
    )

    assert created["ok"] is True
    assert "Created artifact" in created["result"]
    assert listed["ok"] is True
    assert "artifact_0001 [task_spec] v1 draft" in listed["result"]
    assert fetched["ok"] is True
    assert "## Scope" in fetched["result"]
    assert updated["ok"] is True
    assert "artifact_0001 [task_spec] v2 status=ready" in updated["result"]


def test_artifact_context_hook_prioritizes_open_task_artifacts(
    tmp_path: Path,
) -> None:
    task_manager = TaskManager.for_workdir(tmp_path)
    task = task_manager.create_task(
        "Implement artifact MVP",
        scope="project",
    )
    task_manager.update_task(task.id, status="in_progress")
    artifact_manager = ArtifactManager.for_workdir(tmp_path)
    artifact_manager.create_artifact(
        kind="prd",
        title="General PRD",
        content="General background",
        status="ready",
    )
    linked = artifact_manager.create_artifact(
        kind="implementation_report",
        title="Current task report",
        content="Linked work evidence",
        status="draft",
        task_id=task.id,
    )
    hook = ArtifactContextHook(
        artifact_manager,
        max_artifacts=1,
        max_content_chars=200,
    )

    result = hook(HookContext(messages=[], workdir=tmp_path))

    assert result is not None
    assert result.data["artifact_ids"] == [linked.id]
    message = result.data["messages"][0]
    assert message.startswith("<relevant_artifacts>")
    assert f'id="{linked.id}"' in message
    assert "Linked work evidence" in message


def test_artifact_context_truncates_large_content(tmp_path: Path) -> None:
    manager = ArtifactManager.for_workdir(tmp_path)
    artifact = manager.create_artifact(
        kind="note",
        title="Large note",
        content="x" * 50,
    )

    content = format_artifact_context([artifact], max_content_chars=10)

    assert "x" * 10 in content
    assert "chars omitted; use artifact_get" in content


def test_artifact_manager_records_trace(tmp_path: Path) -> None:
    manager = ArtifactManager.for_workdir(tmp_path)
    trace = TraceRecorder.for_run(tmp_path, run_id="run-artifact")

    with trace_scope(trace, run_id="run-artifact", agent_id="main"):
        artifact = manager.create_artifact(
            kind="test_report",
            title="Test report",
            content="All checks passed.",
            status="ready",
        )
        manager.update_artifact(
            artifact.id,
            status="accepted",
            expected_version=1,
            change_summary="Accepted test evidence.",
        )

    records = [
        json.loads(line)
        for line in trace.jsonl_path.read_text(encoding="utf-8").splitlines()
    ]

    assert [record["name"] for record in records] == [
        "artifact.created",
        "artifact.updated",
    ]
    assert records[0]["data"]["artifact_id"] == artifact.id
    assert records[1]["data"]["version"] == 2
