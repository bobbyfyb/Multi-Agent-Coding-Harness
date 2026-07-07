from pathlib import Path
import sys

from llm_agent.agent import ToolExecutionContext
from llm_agent.command_runner import run_command
from llm_agent.tool_registry import ToolRegistry
from llm_agent.tools.verification_tools import register_tools


def _context(tmp_path: Path, tool_call_id: str) -> ToolExecutionContext:
    return ToolExecutionContext(
        run_id="run-verification",
        agent_id="main",
        parent_run_id=None,
        depth=0,
        step=1,
        workdir=tmp_path,
        metadata={"tool_call_id": tool_call_id},
    )


def test_run_tests_returns_structured_pytest_failures(
    tmp_path: Path,
) -> None:
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_sample.py").write_text(
        """
def test_passes():
    assert 1 + 1 == 2


def test_fails():
    assert 1 + 1 == 3
""".strip(),
        encoding="utf-8",
    )
    registry = ToolRegistry()
    register_tools(registry, workdir=tmp_path)

    result = registry.call(
        "run_tests",
        {"targets": ["tests/test_sample.py"]},
        context=_context(tmp_path, "call-tests"),
    )

    assert result["ok"] is True
    verification = result["result"]
    assert verification["outcome"] == "failed"
    assert verification["exit_code"] == 1
    assert verification["summary"] == {
        "passed": 1,
        "failed": 1,
        "errors": 0,
        "skipped": 0,
        "total": 2,
    }
    assert verification["failures"][0]["test"] == "test_fails"
    assert Path(verification["report_path"]).exists()
    assert Path(verification["stdout_path"]).exists()


def test_run_lint_returns_structured_ruff_diagnostics(
    tmp_path: Path,
) -> None:
    (tmp_path / "bad.py").write_text("import os\n", encoding="utf-8")
    registry = ToolRegistry()
    register_tools(registry, workdir=tmp_path)

    result = registry.call(
        "run_lint",
        {"paths": ["bad.py"]},
        context=_context(tmp_path, "call-lint"),
    )

    assert result["ok"] is True
    verification = result["result"]
    assert verification["outcome"] == "issues_found"
    assert verification["exit_code"] == 1
    assert verification["issue_count"] == 1
    assert verification["diagnostics"][0] == {
        "path": "bad.py",
        "line": 1,
        "column": 8,
        "code": "F401",
        "message": "`os` imported but unused",
        "fixable": True,
    }
    assert verification["stdout"] == ""
    assert Path(verification["stdout_path"]).exists()


def test_command_runner_returns_timeout_as_structured_result(
    tmp_path: Path,
) -> None:
    result = run_command(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=tmp_path,
        artifact_dir=tmp_path / "artifacts",
        timeout_seconds=0.05,
    )

    assert result["status"] == "timed_out"
    assert result["timed_out"] is True
    assert result["exit_code"] != 0


def test_verification_tools_reject_sensitive_targets(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("SECRET=1\n", encoding="utf-8")
    registry = ToolRegistry()
    register_tools(registry, workdir=tmp_path)

    result = registry.call(
        "run_lint",
        {"paths": [".env"]},
        context=_context(tmp_path, "call-lint-sensitive"),
    )

    assert result["ok"] is False
    assert "Sensitive path is blocked" in result["error"]
