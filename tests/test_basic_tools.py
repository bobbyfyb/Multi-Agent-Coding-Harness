from pathlib import Path

from llm_agent.memory_system import MemoryManager
from llm_agent.skill_system import SkillRegistry
from llm_agent.tool_registry import ToolRegistry
from llm_agent.tools import build_default_registry
from llm_agent.tools.basic_tools import register_tools


def test_basic_tools_file_workflow(tmp_path: Path) -> None:
    registry = ToolRegistry()
    register_tools(registry, workdir=tmp_path)

    write_result = registry.call(
        "write_file",
        {"path": "notes/todo.txt", "content": "one\ntwo\nthree"},
    )
    assert write_result == {
        "ok": True,
        "result": "Wrote 13 bytes to notes/todo.txt",
    }

    read_result = registry.call("read_file", {"path": "notes/todo.txt", "limit": 2})
    assert read_result == {
        "ok": True,
        "result": "one\ntwo\n... (1 more lines)",
    }

    edit_result = registry.call(
        "edit_file",
        {"path": "notes/todo.txt", "old_text": "two", "new_text": "TWO"},
    )
    assert edit_result == {"ok": True, "result": "Edited notes/todo.txt"}
    assert (tmp_path / "notes/todo.txt").read_text(encoding="utf-8") == (
        "one\nTWO\nthree"
    )

    glob_result = registry.call("glob", {"pattern": "**/*.txt"})
    assert glob_result == {"ok": True, "result": "notes/todo.txt"}


def test_basic_tools_reject_paths_outside_workspace(tmp_path: Path) -> None:
    registry = ToolRegistry()
    register_tools(registry, workdir=tmp_path)

    result = registry.call("read_file", {"path": "../outside.txt"})

    assert result["ok"] is False
    assert "Path escapes workspace" in result["error"]


def test_basic_tools_runs_bash_in_workspace(tmp_path: Path) -> None:
    registry = ToolRegistry()
    register_tools(registry, workdir=tmp_path)

    result = registry.call("bash", {"command": "printf '%s' \"$PWD\""})

    assert result == {"ok": True, "result": str(tmp_path)}


def test_basic_tools_blocks_dangerous_bash(tmp_path: Path) -> None:
    registry = ToolRegistry()
    register_tools(registry, workdir=tmp_path)

    result = registry.call("bash", {"command": "sudo echo no"})

    assert result["ok"] is False
    assert "Dangerous command blocked" in result["error"]


def test_default_registry_loads_basic_tools(tmp_path: Path) -> None:
    skill_registry = SkillRegistry.for_workdir(tmp_path)
    memory_manager = MemoryManager.for_workdir(tmp_path)
    registry = build_default_registry(
        workdir=tmp_path,
        skill_registry=skill_registry,
        memory_manager=memory_manager,
    )
    tool_names = {tool["name"] for tool in registry.tool_specs()}

    assert {
        "bash",
        "read_file",
        "write_file",
        "edit_file",
        "glob",
        "task_create",
        "task_update",
        "task_list",
        "task_get",
        "task_claim",
        "task_complete",
        "skill_load",
        "skill_read_resource",
        "memory_remember",
        "memory_search",
        "memory_get",
        "memory_forget",
    } <= tool_names
