import difflib
import fnmatch
import glob as glob_lib
import hashlib
import json
import os
import re
import stat
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Any, TYPE_CHECKING
from uuid import uuid4

from llm_agent.command_runner import command_artifact_dir, run_command
from llm_agent.security import (
    is_sensitive_workspace_path,
    resolve_workspace_path,
    safe_subprocess_env,
    sensitive_glob_excludes,
    validate_shell_command,
)
from llm_agent.tool_registry import ToolDefinition, ToolRegistry

if TYPE_CHECKING:
    from llm_agent.background_jobs import BackgroundJobManager


@dataclass(frozen=True)
class BasicTools:
    workdir: Path
    bash_timeout: float = 120.0
    output_limit: int = 50_000
    max_read_lines: int = 2_000
    background_manager: "BackgroundJobManager | None" = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "workdir", self.workdir.resolve())

    def safe_path(self, path: str) -> Path:
        return resolve_workspace_path(self.workdir, path)

    def bash(
        self,
        command: str,
        run_in_background: bool = False,
        task_id: str | None = None,
        *,
        context: Any = None,
    ) -> dict[str, Any]:
        validate_shell_command(command, workdir=self.workdir)

        if run_in_background:
            if self.background_manager is None:
                raise RuntimeError(
                    "Background execution is not available for this agent."
                )
            job = self.background_manager.start_shell(
                command,
                owner_run_id=getattr(context, "run_id", None),
                owner_agent_id=getattr(context, "agent_id", None),
                task_id=task_id,
            )
            return {
                "job_id": job.id,
                "status": job.status,
                "pid": job.pid,
                "task_id": job.task_id,
                "stdout_path": job.stdout_path,
                "stderr_path": job.stderr_path,
            }

        return run_command(
            command,
            cwd=self.workdir,
            artifact_dir=command_artifact_dir(
                self.workdir,
                context=context,
                tool_name="bash",
            ),
            timeout_seconds=self.bash_timeout,
            preview_chars=self.output_limit,
            shell=True,
        )

    def read_file(
        self,
        path: str,
        start_line: int = 1,
        limit: int = 400,
    ) -> dict[str, Any]:
        if start_line <= 0:
            raise ValueError("start_line must be greater than zero.")
        if limit <= 0:
            raise ValueError("limit must be greater than zero.")
        file_path = self.safe_path(path)
        raw = file_path.read_bytes()
        lines = raw.decode("utf-8", errors="replace").splitlines()
        if start_line > len(lines) + 1:
            raise ValueError(
                f"start_line {start_line} exceeds file length {len(lines)}."
            )

        resolved_limit = min(limit, self.max_read_lines)
        selected = lines[start_line - 1 : start_line - 1 + resolved_limit]
        end_line = start_line + len(selected) - 1 if selected else start_line - 1
        return {
            "path": path,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "start_line": start_line,
            "end_line": end_line,
            "total_lines": len(lines),
            "content": "\n".join(selected),
            "truncated": end_line < len(lines),
        }

    def write_file(
        self,
        path: str,
        content: str,
        expected_sha256: str | None = None,
    ) -> dict[str, Any]:
        file_path = self.safe_path(path)
        before_sha256 = _file_sha256(file_path) if file_path.exists() else None
        if expected_sha256 is not None and before_sha256 != expected_sha256:
            raise ValueError(
                f"File changed since it was read: {path} "
                f"(expected {expected_sha256}, found {before_sha256})."
            )
        file_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(file_path, content)
        return {
            "path": path,
            "bytes_written": len(content.encode("utf-8")),
            "before_sha256": before_sha256,
            "after_sha256": _file_sha256(file_path),
        }

    def edit_file(
        self,
        path: str,
        old_text: str,
        new_text: str,
        expected_sha256: str | None = None,
    ) -> dict[str, Any]:
        if not old_text:
            raise ValueError("old_text cannot be empty.")
        file_path = self.safe_path(path)
        before_sha256 = _file_sha256(file_path)
        if expected_sha256 is not None and before_sha256 != expected_sha256:
            raise ValueError(
                f"File changed since it was read: {path} "
                f"(expected {expected_sha256}, found {before_sha256})."
            )

        text = file_path.read_text(encoding="utf-8")
        occurrences = text.count(old_text)
        if occurrences != 1:
            raise ValueError(
                f"Expected old_text exactly once in {path}, found {occurrences}."
            )

        updated = text.replace(old_text, new_text, 1)
        if updated == text:
            raise ValueError(f"Edit would not change {path}.")
        _atomic_write_text(file_path, updated)
        return {
            "path": path,
            "before_sha256": before_sha256,
            "after_sha256": _file_sha256(file_path),
            "diff": _diff_preview(path, text, updated),
        }

    def search_text(
        self,
        query: str,
        path: str = ".",
        globs: list[str] | None = None,
        regex: bool = False,
        max_results: int = 100,
    ) -> dict[str, Any]:
        if not query:
            raise ValueError("Search query is required.")
        if not 1 <= max_results <= 200:
            raise ValueError("max_results must be between 1 and 200.")
        search_path = self.safe_path(path)
        if not search_path.exists():
            raise ValueError(f"Search path does not exist: {path}")

        command = ["rg", "--json", "--line-number", "--column", "--color", "never"]
        if not regex:
            command.append("--fixed-strings")
        for pattern in globs or []:
            command.extend(["--glob", pattern])
        for pattern in sensitive_glob_excludes():
            command.extend(["--glob", pattern])
        command.extend(["--", query, str(search_path)])

        try:
            result = subprocess.run(
                command,
                cwd=self.workdir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                env=safe_subprocess_env(self.workdir),
            )
        except FileNotFoundError:
            return self._search_text_fallback(
                query=query,
                path=path,
                search_path=search_path,
                globs=globs or [],
                regex=regex,
                max_results=max_results,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError("search_text timed out after 30 seconds.") from exc
        if result.returncode not in {0, 1}:
            raise RuntimeError(result.stderr.strip() or "rg search failed.")

        matches = []
        for line in result.stdout.splitlines():
            event = json.loads(line)
            if event.get("type") != "match":
                continue
            data = event["data"]
            submatches = data.get("submatches") or []
            column = int(submatches[0]["start"]) + 1 if submatches else 1
            raw_path = Path(data["path"]["text"])
            match_path = (
                raw_path.resolve()
                if raw_path.is_absolute()
                else (self.workdir / raw_path).resolve()
            )
            if is_sensitive_workspace_path(self.workdir, match_path):
                continue
            matches.append(
                {
                    "path": str(match_path.relative_to(self.workdir)),
                    "line": int(data["line_number"]),
                    "column": column,
                    "text": str(data["lines"]["text"]).rstrip("\r\n"),
                }
            )
            if len(matches) >= max_results:
                break
        return {
            "query": query,
            "path": path,
            "matches": matches,
            "count": len(matches),
            "truncated": len(matches) >= max_results,
        }

    def _search_text_fallback(
        self,
        *,
        query: str,
        path: str,
        search_path: Path,
        globs: list[str],
        regex: bool,
        max_results: int,
    ) -> dict[str, Any]:
        try:
            pattern = re.compile(query) if regex else None
        except re.error as exc:
            raise ValueError(f"Invalid search regex: {exc}") from exc

        deadline = perf_counter() + 30
        matches: list[dict[str, Any]] = []
        for file_path in _iter_search_files(self.workdir, search_path):
            if perf_counter() >= deadline:
                raise TimeoutError("search_text timed out after 30 seconds.")
            relative = file_path.relative_to(self.workdir).as_posix()
            if not _matches_search_globs(relative, globs):
                continue
            try:
                lines = file_path.open(
                    encoding="utf-8",
                    errors="replace",
                )
            except OSError:
                continue
            with lines:
                for line_number, line in enumerate(lines, start=1):
                    if "\0" in line:
                        break
                    if pattern is not None:
                        match = pattern.search(line)
                        column = match.start() + 1 if match is not None else 0
                    else:
                        column = line.find(query) + 1
                    if column <= 0:
                        continue
                    matches.append(
                        {
                            "path": relative,
                            "line": line_number,
                            "column": column,
                            "text": line.rstrip("\r\n"),
                        }
                    )
                    if len(matches) >= max_results:
                        break
            if len(matches) >= max_results:
                break
        return {
            "query": query,
            "path": path,
            "matches": matches,
            "count": len(matches),
            "truncated": len(matches) >= max_results,
        }

    def glob(self, pattern: str) -> str:
        matches: list[str] = []
        for match in glob_lib.glob(pattern, root_dir=self.workdir, recursive=True):
            match_path = Path(match)
            candidate = (
                match_path.resolve()
                if match_path.is_absolute()
                else (self.workdir / match_path).resolve()
            )
            if candidate.is_relative_to(self.workdir):
                if is_sensitive_workspace_path(self.workdir, candidate):
                    continue
                matches.append(str(candidate.relative_to(self.workdir)))

        return "\n".join(sorted(matches)) if matches else "(no matches)"


def basic_tool_definitions(
    workdir: Path | str | None = None,
    *,
    background_manager: "BackgroundJobManager | None" = None,
) -> list[ToolDefinition]:
    tools = BasicTools(
        workdir=Path.cwd() if workdir is None else Path(workdir),
        background_manager=background_manager,
    )
    bash_properties: dict[str, Any] = {
        "command": {
            "type": "string",
            "description": "The shell command to run.",
        }
    }
    if background_manager is not None:
        bash_properties.update(
            {
                "run_in_background": {
                    "type": "boolean",
                    "description": (
                        "Run as a managed background job and return a job id "
                        "immediately. Defaults to false."
                    ),
                },
                "task_id": {
                    "type": "string",
                    "description": (
                        "Optional planning task id to associate with a background job."
                    ),
                },
            }
        )

    return [
        ToolDefinition(
            name="bash",
            description="Run a shell command in the workspace.",
            parameters={
                "type": "object",
                "properties": bash_properties,
                "required": ["command"],
            },
            func=tools.bash,
            requires_context=background_manager is not None,
        ),
        ToolDefinition(
            name="read_file",
            description=(
                "Read a bounded line range from a UTF-8 workspace file and "
                "return its SHA256 for safe edits."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Workspace-relative file path.",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": tools.max_read_lines,
                        "description": "Maximum number of lines to return.",
                    },
                    "start_line": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "One-based first line to read.",
                    },
                },
                "required": ["path"],
            },
            func=tools.read_file,
        ),
        ToolDefinition(
            name="write_file",
            description="Write UTF-8 text content to a workspace file.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Workspace-relative file path.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Content to write.",
                    },
                    "expected_sha256": {
                        "type": "string",
                        "description": (
                            "Optional SHA256 from read_file. The write is "
                            "rejected if the file changed."
                        ),
                    },
                },
                "required": ["path", "content"],
            },
            func=tools.write_file,
        ),
        ToolDefinition(
            name="edit_file",
            description="Replace the first exact text match in a workspace file.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Workspace-relative file path.",
                    },
                    "old_text": {
                        "type": "string",
                        "description": "Exact text to replace.",
                    },
                    "new_text": {
                        "type": "string",
                        "description": "Replacement text.",
                    },
                    "expected_sha256": {
                        "type": "string",
                        "description": (
                            "Optional SHA256 from read_file. The edit is "
                            "rejected if the file changed."
                        ),
                    },
                },
                "required": ["path", "old_text", "new_text"],
            },
            func=tools.edit_file,
        ),
        ToolDefinition(
            name="search_text",
            description=(
                "Search workspace text with ripgrep and return bounded, "
                "structured matches."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Text or regular expression to search.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Workspace-relative search root.",
                    },
                    "globs": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional ripgrep glob filters.",
                    },
                    "regex": {
                        "type": "boolean",
                        "description": "Interpret query as a regular expression.",
                    },
                    "max_results": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 200,
                        "description": "Maximum number of matches.",
                    },
                },
                "required": ["query"],
            },
            func=tools.search_text,
        ),
        ToolDefinition(
            name="glob",
            description="Find workspace files matching a glob pattern.",
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Glob pattern, relative to the workspace.",
                    }
                },
                "required": ["pattern"],
            },
            func=tools.glob,
        ),
    ]


def register_tools(
    registry: ToolRegistry,
    *,
    workdir: Path | str | None = None,
    background_manager: "BackgroundJobManager | None" = None,
) -> None:
    registry.register_many(
        basic_tool_definitions(
            workdir=workdir,
            background_manager=background_manager,
        )
    )


def _iter_search_files(workdir: Path, search_path: Path) -> Iterator[Path]:
    if search_path.is_file():
        if not is_sensitive_workspace_path(workdir, search_path):
            yield search_path
        return

    for root, directories, filenames in os.walk(search_path):
        root_path = Path(root)
        directories[:] = [
            name
            for name in directories
            if not name.startswith(".")
            and not is_sensitive_workspace_path(workdir, root_path / name)
        ]
        for filename in filenames:
            if filename.startswith("."):
                continue
            candidate = (root_path / filename).resolve()
            if is_sensitive_workspace_path(workdir, candidate):
                continue
            if candidate.is_file():
                yield candidate


def _matches_search_globs(relative_path: str, globs: list[str]) -> bool:
    includes = [pattern for pattern in globs if not pattern.startswith("!")]
    excludes = [pattern[1:] for pattern in globs if pattern.startswith("!")]

    def matches(pattern: str) -> bool:
        normalized = pattern.removeprefix("./")
        candidates = [relative_path, Path(relative_path).name]
        if normalized.startswith("**/"):
            normalized = normalized[3:]
        return any(fnmatch.fnmatchcase(candidate, normalized) for candidate in candidates)

    return (not includes or any(matches(pattern) for pattern in includes)) and not any(
        matches(pattern) for pattern in excludes
    )


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _atomic_write_text(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex[:8]}.tmp")
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else None
    try:
        temporary.write_text(content, encoding="utf-8")
        if mode is not None:
            os.chmod(temporary, mode)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _diff_preview(
    path: str,
    before: str,
    after: str,
    *,
    limit: int = 12_000,
) -> str:
    diff = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )
    if len(diff) <= limit:
        return diff
    return f"{diff[:limit]}\n... (diff truncated)"
