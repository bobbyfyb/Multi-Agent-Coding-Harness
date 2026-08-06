from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import sys
from typing import Any
import xml.etree.ElementTree as ET

from llm_agent.command_runner import command_artifact_dir, run_command
from llm_agent.security import resolve_workspace_path
from llm_agent.tool_registry import ToolDefinition, ToolRegistry


_MISSING_MODULE_RE = re.compile(
    r"(?:ModuleNotFoundError|ImportError): No module named ['\"]([^'\"]+)['\"]"
)
_LOCK_FAILURE_MARKERS = (
    "needs to be updated",
    "is out of date",
    "does not satisfy",
)
_ENVIRONMENT_FAILURE_MARKERS = (
    "failed to create virtual environment",
    "failed to download",
    "failed to fetch",
    "failed to install",
    "failed to prepare distributions",
    "failed to build",
    "failed to inspect python interpreter",
    "no solution found when resolving dependencies",
    "unable to find a compatible python",
)
_DIAGNOSTIC_MARKERS = (
    "error",
    "failed",
    "lockfile",
    "lock file",
    "download",
    "fetch",
    "virtual environment",
)


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
        command, execution_environment = _module_command(
            self.workdir,
            "pytest",
        )
        command.extend(
            [
                "-q",
                *resolved_targets,
            ]
        )
        if keyword:
            command.extend(["-k", keyword])
        command.append(f"--junitxml={report_path}")

        process_result = run_command(
            command,
            cwd=self.workdir,
            artifact_dir=artifact_dir,
            timeout_seconds=timeout_seconds,
            preview_chars=self.output_limit,
            unset_env=_unset_env(execution_environment),
        )
        summary, failures = _parse_junit(report_path)
        exit_code = process_result["exit_code"]
        error_details = _classify_process_error(
            process_result,
            execution_environment=execution_environment,
        )
        if process_result["timed_out"]:
            outcome = "timed_out"
        elif error_details.get("error_kind") in {
            "environment_setup_failed",
            "lock_outdated",
        }:
            outcome = "error"
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
            "execution_environment": execution_environment,
            **process_result,
            **error_details,
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
        command, execution_environment = _module_command(
            self.workdir,
            "ruff",
        )
        command.extend(
            [
                "check",
                "--output-format",
                "json",
                *resolved_paths,
            ]
        )
        process_result = run_command(
            command,
            cwd=self.workdir,
            artifact_dir=artifact_dir,
            timeout_seconds=timeout_seconds,
            preview_chars=self.output_limit,
            unset_env=_unset_env(execution_environment),
        )
        exit_code = process_result["exit_code"]
        error_details = _classify_process_error(
            process_result,
            execution_environment=execution_environment,
        )
        if process_result["timed_out"]:
            outcome = "timed_out"
        elif error_details.get("error_kind") in {
            "environment_setup_failed",
            "lock_outdated",
        }:
            outcome = "error"
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
            "execution_environment": execution_environment,
            **process_result,
            **error_details,
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
                "summary. For uv projects, synchronize and use the locked project "
                "environment. A failed test run is a valid tool result."
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
                "Run Ruff without modifying files and return structured diagnostics. "
                "For uv projects, use the locked project environment."
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


def _module_command(workdir: Path, module: str) -> tuple[list[str], str]:
    if (workdir / "pyproject.toml").is_file() and (workdir / "uv.lock").is_file():
        return (
            [
                "uv",
                "run",
                "--locked",
                "--no-env-file",
                "python",
                "-m",
                module,
            ],
            "uv_project",
        )
    return [sys.executable, "-m", module], "harness"


def _unset_env(execution_environment: str) -> tuple[str, ...]:
    return ("VIRTUAL_ENV",) if execution_environment == "uv_project" else ()


def _classify_process_error(
    process_result: dict[str, Any],
    *,
    execution_environment: str,
) -> dict[str, Any]:
    output = "\n".join(
        str(process_result.get(key) or "") for key in ("stdout", "stderr")
    )
    missing_modules = sorted(set(_MISSING_MODULE_RE.findall(output)))
    if missing_modules:
        return {
            "error_kind": "missing_dependency",
            "missing_modules": missing_modules,
            "diagnostic": ", ".join(
                f"No module named {module!r}" for module in missing_modules
            ),
        }

    normalized = output.casefold()
    if _lock_is_outdated(normalized):
        return {
            "error_kind": "lock_outdated",
            "diagnostic": _diagnostic_excerpt(output),
        }
    if execution_environment == "uv_project" and any(
        marker in normalized for marker in _ENVIRONMENT_FAILURE_MARKERS
    ):
        return {
            "error_kind": "environment_setup_failed",
            "diagnostic": _diagnostic_excerpt(output),
        }
    return {}


def _lock_is_outdated(output: str) -> bool:
    return ("lockfile" in output or "lock file" in output) and any(
        marker in output for marker in _LOCK_FAILURE_MARKERS
    )


def _diagnostic_excerpt(output: str, limit: int = 2_000) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    relevant = [
        line
        for line in lines
        if any(marker in line.casefold() for marker in _DIAGNOSTIC_MARKERS)
    ]
    selected = relevant[-8:] if relevant else lines[-8:]
    return "\n".join(selected)[:limit]


def _validate_timeout(timeout_seconds: float) -> None:
    if not 1 <= timeout_seconds <= 600:
        raise ValueError("timeout_seconds must be between 1 and 600.")


def _validate_targets(workdir: Path, targets: list[str]) -> None:
    for target in targets:
        if not target or target.startswith("-"):
            raise ValueError(f"Invalid target: {target!r}")
        path_text = target.split("::", 1)[0]
        candidate = resolve_workspace_path(workdir, path_text)
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
