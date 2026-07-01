from pathlib import Path
import json
import subprocess
from typing import Any

import pytest

from llm_agent.agent import ToolExecutionContext
from llm_agent.hooks.permission_hooks import AutoApprovalProvider
from llm_agent.llm_client import LLMResponse, LLMToolCall
from llm_agent.subagent import SubagentRequest, SubagentRunner
from llm_agent.tools import build_default_registry
from llm_agent.trace_system import TraceRecorder, trace_scope
from llm_agent.worktree import WorktreeError, WorktreeManager


class FakeLLM:
    def __init__(self, outputs: list[LLMResponse]) -> None:
        self.outputs = outputs

    def chat(self, messages: list[dict[str, Any]], **_: Any) -> LLMResponse:
        return self.outputs.pop(0)

    def assistant_message(self, response: LLMResponse) -> dict[str, Any]:
        return {
            "role": "assistant",
            "content": response.content,
            "tool_calls": [call.raw for call in response.tool_calls],
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
                        "tool_use_id": call.id,
                        "content": result,
                    }
                    for call, result in tool_results
                ],
            }
        ]


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
    (tmp_path / ".gitignore").write_text(".llm_agent/\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("value = 'original'\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "initial")
    return tmp_path


def test_worktree_lifecycle_keeps_main_clean_until_apply(
    tmp_path: Path,
) -> None:
    workdir = _repository(tmp_path)
    manager = WorktreeManager.for_workdir(workdir)

    info = manager.create(
        task_id="task_0001",
        agent_id="worker",
        run_id="run-worker",
    )
    isolated = Path(info.path)
    (isolated / "app.py").write_text("value = 'isolated'\n", encoding="utf-8")
    (isolated / "new.py").write_text("created = True\n", encoding="utf-8")

    review = manager.finish(info.id)

    assert (workdir / "app.py").read_text(encoding="utf-8") == ("value = 'original'\n")
    assert review["worktree"]["status"] == "ready"
    assert review["changed_files"] == ["app.py", "new.py"]
    assert Path(review["diff_path"]).exists()

    applied = manager.apply(info.id)

    assert applied["applied_files"] == ["app.py", "new.py"]
    assert (workdir / "app.py").read_text(encoding="utf-8") == ("value = 'isolated'\n")
    assert (workdir / "new.py").exists()

    removed = manager.remove(info.id)

    assert removed["removed"] is True
    assert not isolated.exists()


def test_worktree_requires_clean_main_workspace(tmp_path: Path) -> None:
    workdir = _repository(tmp_path)
    (workdir / "app.py").write_text("dirty = True\n", encoding="utf-8")
    manager = WorktreeManager.for_workdir(workdir)

    with pytest.raises(WorktreeError, match="must be clean"):
        manager.create()


def test_worktree_refuses_to_remove_unapplied_changes(
    tmp_path: Path,
) -> None:
    workdir = _repository(tmp_path)
    manager = WorktreeManager.for_workdir(workdir)
    info = manager.create()
    isolated = Path(info.path)
    (isolated / "app.py").write_text("value = 'discard me'\n", encoding="utf-8")

    with pytest.raises(WorktreeError, match="unapplied changes"):
        manager.remove(info.id)

    result = manager.remove(info.id, discard_changes=True)

    assert result["discarded"] is True
    assert not isolated.exists()


def test_worktree_without_changes_is_cleaned_automatically(
    tmp_path: Path,
) -> None:
    workdir = _repository(tmp_path)
    manager = WorktreeManager.for_workdir(workdir)
    info = manager.create()

    review = manager.finish(info.id)

    assert review["worktree"]["status"] == "cleaned"
    assert review["change_count"] == 0
    assert not Path(info.path).exists()


def test_worktree_apply_rejects_changed_main_head(tmp_path: Path) -> None:
    workdir = _repository(tmp_path)
    manager = WorktreeManager.for_workdir(workdir)
    info = manager.create()
    (Path(info.path) / "app.py").write_text(
        "value = 'isolated'\n",
        encoding="utf-8",
    )
    manager.finish(info.id)
    (workdir / "main.py").write_text("main = True\n", encoding="utf-8")
    _git(workdir, "add", "main.py")
    _git(workdir, "commit", "-m", "move main")

    with pytest.raises(WorktreeError, match="Main HEAD changed"):
        manager.apply(info.id)

    manager.remove(info.id, discard_changes=True)


def test_worktree_reconciles_missing_directory(tmp_path: Path) -> None:
    workdir = _repository(tmp_path)
    manager = WorktreeManager.for_workdir(workdir)
    info = manager.create()
    _git(workdir, "worktree", "remove", "--force", info.path)

    missing = manager.reconcile()
    listed = manager.list()

    assert missing == [info.id]
    assert listed[0].status == "missing"
    assert manager.reconcile() == []
    manager.remove(info.id)


def test_worktree_records_lifecycle_trace(tmp_path: Path) -> None:
    workdir = _repository(tmp_path)
    manager = WorktreeManager.for_workdir(workdir)
    trace = TraceRecorder.for_run(workdir, run_id="run-worktree")

    with trace_scope(trace, run_id="run-worktree", agent_id="main"):
        info = manager.create()
        (Path(info.path) / "app.py").write_text(
            "value = 'traced'\n",
            encoding="utf-8",
        )
        manager.finish(info.id)
        manager.apply(info.id)
        manager.remove(info.id)

    records = [
        json.loads(line)
        for line in trace.jsonl_path.read_text(encoding="utf-8").splitlines()
    ]

    assert [record["name"] for record in records] == [
        "worktree.created",
        "worktree.ready",
        "worktree.applied",
        "worktree.removed",
    ]
    assert all(record["correlation_id"] == info.id for record in records)


def test_subagent_can_edit_in_isolated_worktree(tmp_path: Path) -> None:
    workdir = _repository(tmp_path)
    manager = WorktreeManager.for_workdir(workdir)
    edit_call = LLMToolCall(
        id="call-edit",
        name="edit_file",
        arguments={
            "path": "app.py",
            "old_text": "original",
            "new_text": "from worker",
        },
        raw={"id": "call-edit", "name": "edit_file"},
    )
    llm = FakeLLM(
        [
            LLMResponse(content="", tool_calls=[edit_call], raw={}),
            LLMResponse(content="Implemented and verified.", tool_calls=[], raw={}),
        ]
    )
    runner = SubagentRunner(
        llm=llm,  # type: ignore[arg-type]
        workdir=workdir,
        worktree_manager=manager,
        approval_provider=AutoApprovalProvider(approved=True),
    )
    parent_context = ToolExecutionContext(
        run_id="run-parent",
        agent_id="main",
        parent_run_id=None,
        depth=0,
        step=1,
        workdir=workdir,
    )

    result = runner.run(
        SubagentRequest(
            task="Edit app.py.",
            mode="general",
            isolation="worktree",
            task_id="task_0001",
        ),
        parent_context=parent_context,
    )

    assert result.status == "completed"
    assert result.worktree is not None
    assert result.worktree["status"] == "ready"
    assert result.worktree["task_id"] == "task_0001"
    assert result.worktree["changed_files"] == ["app.py"]
    assert (workdir / "app.py").read_text(encoding="utf-8") == ("value = 'original'\n")

    manager.apply(result.worktree["id"])
    manager.remove(result.worktree["id"])

    assert (workdir / "app.py").read_text(encoding="utf-8") == (
        "value = 'from worker'\n"
    )


def test_subagent_rejects_worktree_for_explore_mode() -> None:
    with pytest.raises(ValueError, match="only available in general mode"):
        SubagentRequest(
            task="Inspect files.",
            mode="explore",
            isolation="worktree",
        )


def test_default_registry_loads_worktree_review_tools(
    tmp_path: Path,
) -> None:
    manager = WorktreeManager.for_workdir(tmp_path)
    registry = build_default_registry(
        workdir=tmp_path,
        worktree_manager=manager,
    )

    assert {
        "worktree_list",
        "worktree_diff",
        "worktree_apply",
        "worktree_remove",
    } <= set(registry.names())
