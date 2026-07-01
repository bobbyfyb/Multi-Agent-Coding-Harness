from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sys
from typing import Any
import xml.etree.ElementTree as ET

from llm_agent.command_runner import command_artifact_dir, run_command
from llm_agent.tool_registry import ToolDefinition, ToolRegistry


@dataclass(frozen=True)
class VerificationTools:
    workdir: Path
    output_limit: int = 20_000

    def __post_init__(self) -> None:
        object.__setattr__(self, "workdir", self.workdir.resolve())

    def run_tests(
        self,
        targets: list[str] | None = None,
        keyword: str | None = None,
        timeout_seconds: float = 120.0,
        *,
        context: Any,
    ) -> dict[str, Any]:
        _validate_timeout(timeout_seconds)
        resolved_targets = targets or []
        _validate_targets(self.workdir, resolved_targets)

        artifact_dir = command_artifact_dir(
            self.workdir,
            context=context,
            tool_name="run_tests",
        )
        report_path = artifact_dir / "junit.xml"
        command = [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            *resolved_targets,
        ]
        if keyword:
            command.extend(["-k", keyword])
        command.append(f"--junitxml={report_path}")

        process_result = run_command(
            command,
            cwd=self.workdir,
            artifact_dir=artifact_dir,
            timeout_seconds=timeout_seconds,
            preview_chars=self.output_limit,
        )
        summary, failures = _parse_junit(report_path)
        exit_code = process_result["exit_code"]
        if process_result["timed_out"]:
            outcome = "timed_out"
        elif exit_code == 0:
            outcome = "passed"
        elif exit_code == 1:
            outcome = "failed"
        elif exit_code == 5:
            outcome = "no_tests"
        else:
            outcome = "error"

        return {
            "outcome": outcome,
            "runner": "pytest",
            **process_result,
            "summary": summary,
            "failures": failures,
            "report_path": str(report_path) if report_path.exists() else None,
        }

    def run_lint(
        self,
        paths: list[str] | None = None,
        timeout_seconds: float = 120.0,
        *,
        context: Any,
    ) -> dict[str, Any]:
        _validate_timeout(timeout_seconds)
        resolved_paths = paths or ["."]
        _validate_targets(self.workdir, resolved_paths)

        artifact_dir = command_artifact_dir(
            self.workdir,
            context=context,
            tool_name="run_lint",
        )
        command = [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--output-format",
            "json",
            *resolved_paths,
        ]
        process_result = run_command(
            command,
            cwd=self.workdir,
            artifact_dir=artifact_dir,
            timeout_seconds=timeout_seconds,
            preview_chars=self.output_limit,
        )
        exit_code = process_result["exit_code"]
        if process_result["timed_out"]:
            outcome = "timed_out"
        elif exit_code == 0:
            outcome = "clean"
        elif exit_code == 1:
            outcome = "issues_found"
        else:
            outcome = "error"
        diagnostics = (
            _parse_ruff(
                Path(process_result["stdout_path"]),
                workdir=self.workdir,
            )
            if outcome in {"clean", "issues_found"}
            else []
        )

        return {
            "outcome": outcome,
            "runner": "ruff",
            **process_result,
            "stdout": "",
            "issue_count": len(diagnostics),
            "diagnostics": diagnostics[:200],
            "diagnostics_truncated": len(diagnostics) > 200,
        }


def verification_tool_definitions(
    workdir: Path | str | None = None,
) -> list[ToolDefinition]:
    tools = VerificationTools(workdir=Path.cwd() if workdir is None else Path(workdir))
    timeout_schema = {
        "type": "number",
        "minimum": 1,
        "maximum": 600,
        "description": "Execution timeout in seconds.",
    }
    return [
        ToolDefinition(
            name="run_tests",
            description=(
                "Run pytest with bounded output and return a structured test "
                "summary. A failed test run is a valid tool result."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "targets": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional workspace-relative pytest paths or node ids."
                        ),
                    },
                    "keyword": {
                        "type": "string",
                        "description": "Optional pytest -k expression.",
                    },
                    "timeout_seconds": timeout_schema,
                },
            },
            func=tools.run_tests,
            requires_context=True,
        ),
        ToolDefinition(
            name="run_lint",
            description=(
                "Run Ruff without modifying files and return structured diagnostics."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional workspace-relative paths.",
                    },
                    "timeout_seconds": timeout_schema,
                },
            },
            func=tools.run_lint,
            requires_context=True,
        ),
    ]


def register_tools(
    registry: ToolRegistry,
    *,
    workdir: Path | str | None = None,
) -> None:
    registry.register_many(verification_tool_definitions(workdir))


def _validate_timeout(timeout_seconds: float) -> None:
    if not 1 <= timeout_seconds <= 600:
        raise ValueError("timeout_seconds must be between 1 and 600.")


def _validate_targets(workdir: Path, targets: list[str]) -> None:
    for target in targets:
        if not target or target.startswith("-"):
            raise ValueError(f"Invalid target: {target!r}")
        path_text = target.split("::", 1)[0]
        candidate = (workdir / path_text).resolve()
        if not candidate.is_relative_to(workdir):
            raise ValueError(f"Path escapes workspace: {target}")
        if not candidate.exists():
            raise ValueError(f"Target does not exist: {target}")


def _parse_junit(path: Path) -> tuple[dict[str, int], list[dict[str, Any]]]:
    empty = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0, "total": 0}
    if not path.exists():
        return empty, []

    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return empty, []
    suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
    total = sum(int(suite.attrib.get("tests", 0)) for suite in suites)
    failed = sum(int(suite.attrib.get("failures", 0)) for suite in suites)
    errors = sum(int(suite.attrib.get("errors", 0)) for suite in suites)
    skipped = sum(int(suite.attrib.get("skipped", 0)) for suite in suites)
    summary = {
        "passed": max(0, total - failed - errors - skipped),
        "failed": failed,
        "errors": errors,
        "skipped": skipped,
        "total": total,
    }
    failures = []
    for case in root.iter("testcase"):
        detail = case.find("failure")
        kind = "failure"
        if detail is None:
            detail = case.find("error")
            kind = "error"
        if detail is None:
            continue
        message = detail.attrib.get("message") or (detail.text or "").strip()
        failures.append(
            {
                "test": case.attrib.get("name"),
                "classname": case.attrib.get("classname"),
                "file": case.attrib.get("file"),
                "line": case.attrib.get("line"),
                "kind": kind,
                "message": message[:4_000],
            }
        )
    return summary, failures[:100]


def _parse_ruff(path: Path, *, workdir: Path) -> list[dict[str, Any]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise RuntimeError("Ruff output is not a JSON list.")

    diagnostics = []
    for item in data:
        raw_filename = Path(str(item.get("filename", "")))
        filename = (
            raw_filename.resolve()
            if raw_filename.is_absolute()
            else (workdir / raw_filename).resolve()
        )
        display_path = (
            str(filename.relative_to(workdir))
            if filename.is_relative_to(workdir)
            else str(filename)
        )
        location = item.get("location") or {}
        diagnostics.append(
            {
                "path": display_path,
                "line": location.get("row"),
                "column": location.get("column"),
                "code": item.get("code"),
                "message": item.get("message"),
                "fixable": item.get("fix") is not None,
            }
        )
    return diagnostics


__all__ = [
    "VerificationTools",
    "register_tools",
    "verification_tool_definitions",
]
