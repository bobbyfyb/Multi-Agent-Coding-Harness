from pathlib import Path

import pytest

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
    assert write_result["ok"] is True
    assert write_result["result"]["bytes_written"] == 13
    assert write_result["result"]["before_sha256"] is None
    file_sha256 = write_result["result"]["after_sha256"]

    read_result = registry.call("read_file", {"path": "notes/todo.txt", "limit": 2})
    assert read_result["ok"] is True
    assert read_result["result"] == {
        "path": "notes/todo.txt",
        "sha256": file_sha256,
        "start_line": 1,
        "end_line": 2,
        "total_lines": 3,
        "content": "one\ntwo",
        "truncated": True,
    }

    edit_result = registry.call(
        "edit_file",
        {
            "path": "notes/todo.txt",
            "old_text": "two",
            "new_text": "TWO",
            "expected_sha256": file_sha256,
        },
    )
    assert edit_result["ok"] is True
    assert edit_result["result"]["before_sha256"] == file_sha256
    assert "-two" in edit_result["result"]["diff"]
    assert "+TWO" in edit_result["result"]["diff"]
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

    assert result["ok"] is True
    assert result["result"]["status"] == "completed"
    assert result["result"]["exit_code"] == 0
    assert result["result"]["stdout"] == str(tmp_path)
    assert result["result"]["stderr"] == ""
    assert Path(result["result"]["stdout_path"]).exists()


def test_basic_tools_preserves_failed_bash_exit_code(tmp_path: Path) -> None:
    registry = ToolRegistry()
    register_tools(registry, workdir=tmp_path)

    result = registry.call(
        "bash",
        {"command": "printf 'bad' >&2; exit 7"},
    )

    assert result["ok"] is True
    assert result["result"]["status"] == "failed"
    assert result["result"]["exit_code"] == 7
    assert result["result"]["stdout"] == ""
    assert result["result"]["stderr"] == "bad"


def test_basic_tools_blocks_dangerous_bash(tmp_path: Path) -> None:
    registry = ToolRegistry()
    register_tools(registry, workdir=tmp_path)

    result = registry.call("bash", {"command": "sudo echo no"})

    assert result["ok"] is False
    assert "blocked fragment" in result["error"]


def test_basic_tools_blocks_bash_cd_outside_workspace(tmp_path: Path) -> None:
    registry = ToolRegistry()
    register_tools(registry, workdir=tmp_path)

    result = registry.call("bash", {"command": "cd .. && pwd"})

    assert result["ok"] is False
    assert "outside workspace" in result["error"]


def test_basic_tools_allows_bash_cd_within_workspace(tmp_path: Path) -> None:
    (tmp_path / "child").mkdir()
    registry = ToolRegistry()
    register_tools(registry, workdir=tmp_path)

    result = registry.call("bash", {"command": "cd child && pwd"})

    assert result["ok"] is True
    assert result["result"]["stdout"].strip() == str(tmp_path / "child")


def test_basic_tools_do_not_expose_parent_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_AGENT_TEST_SECRET", "review-marker")
    registry = ToolRegistry()
    register_tools(registry, workdir=tmp_path)

    result = registry.call(
        "bash",
        {"command": "printf '%s' \"$LLM_AGENT_TEST_SECRET\""},
    )

    assert result["ok"] is True
    assert result["result"]["stdout"] == ""


def test_basic_tools_block_sensitive_paths(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / ".env").write_text("SECRET=1\n", encoding="utf-8")
    (tmp_path / "config.env").write_text("SECRET=2\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("SECRET=visible\n", encoding="utf-8")
    registry = ToolRegistry()
    register_tools(registry, workdir=tmp_path)

    read_result = registry.call("read_file", {"path": "src/.env"})
    write_result = registry.call(
        "write_file",
        {"path": "config.env", "content": "SECRET=3\n"},
    )
    exact_search_result = registry.call(
        "search_text",
        {"query": "SECRET", "path": "config.env"},
    )
    broad_search_result = registry.call("search_text", {"query": "SECRET"})
    explicit_env_glob_result = registry.call(
        "search_text",
        {"query": "SECRET", "globs": ["*.env"]},
    )
    glob_result = registry.call("glob", {"pattern": "**/*.env"})

    assert read_result["ok"] is False
    assert "Sensitive path is blocked" in read_result["error"]
    assert write_result["ok"] is False
    assert "Sensitive path is blocked" in write_result["error"]
    assert exact_search_result["ok"] is False
    assert "Sensitive path is blocked" in exact_search_result["error"]
    assert broad_search_result["ok"] is True
    assert broad_search_result["result"]["matches"] == [
        {
            "path": "app.py",
            "line": 1,
            "column": 1,
            "text": "SECRET=visible",
        }
    ]
    assert explicit_env_glob_result["ok"] is True
    assert explicit_env_glob_result["result"]["matches"] == []
    assert glob_result == {"ok": True, "result": "(no matches)"}


def test_edit_file_rejects_ambiguous_or_stale_content(tmp_path: Path) -> None:
    registry = ToolRegistry()
    register_tools(registry, workdir=tmp_path)
    path = tmp_path / "notes.txt"
    path.write_text("same\nsame\n", encoding="utf-8")

    ambiguous = registry.call(
        "edit_file",
        {"path": "notes.txt", "old_text": "same", "new_text": "new"},
    )
    stale = registry.call(
        "edit_file",
        {
            "path": "notes.txt",
            "old_text": "same\nsame",
            "new_text": "new",
            "expected_sha256": "stale",
        },
    )

    assert ambiguous["ok"] is False
    assert "found 2" in ambiguous["error"]
    assert stale["ok"] is False
    assert "File changed since it was read" in stale["error"]
    assert path.read_text(encoding="utf-8") == "same\nsame\n"


def test_read_file_supports_line_ranges(tmp_path: Path) -> None:
    registry = ToolRegistry()
    register_tools(registry, workdir=tmp_path)
    (tmp_path / "lines.txt").write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")

    result = registry.call(
        "read_file",
        {"path": "lines.txt", "start_line": 2, "limit": 2},
    )

    assert result["ok"] is True
    assert result["result"]["content"] == "two\nthree"
    assert result["result"]["start_line"] == 2
    assert result["result"]["end_line"] == 3
    assert result["result"]["truncated"] is True


def test_search_text_returns_structured_matches(tmp_path: Path) -> None:
    registry = ToolRegistry()
    register_tools(registry, workdir=tmp_path)
    (tmp_path / "app.py").write_text(
        "def hello():\n    return 'needle'\n",
        encoding="utf-8",
    )

    result = registry.call(
        "search_text",
        {"query": "needle", "globs": ["*.py"]},
    )

    assert result["ok"] is True
    assert result["result"]["count"] == 1
    assert result["result"]["matches"][0] == {
        "path": "app.py",
        "line": 2,
        "column": 13,
        "text": "    return 'needle'",
    }


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
        "search_text",
        "run_tests",
        "run_lint",
        "task_create",
        "task_update",
        "task_list",
        "task_get",
        "task_claim",
        "task_complete",
        "artifact_create",
        "artifact_update",
        "artifact_get",
        "artifact_list",
        "skill_list",
        "skill_load",
        "skill_read_resource",
        "memory_remember",
        "memory_search",
        "memory_get",
        "memory_forget",
    } <= tool_names
