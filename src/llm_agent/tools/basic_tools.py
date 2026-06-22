import glob as glob_lib
import subprocess
from dataclasses import dataclass
from pathlib import Path

from llm_agent.tool_registry import ToolDefinition, ToolRegistry


@dataclass(frozen=True)
class BasicTools:
    workdir: Path
    bash_timeout: float = 120.0
    output_limit: int = 50_000

    def __post_init__(self) -> None:
        object.__setattr__(self, "workdir", self.workdir.resolve())

    def safe_path(self, path: str) -> Path:
        candidate = (self.workdir / path).resolve()
        if not candidate.is_relative_to(self.workdir):
            raise ValueError(f"Path escapes workspace: {path}")
        return candidate

    def bash(self, command: str) -> str:
        dangerous_fragments = [
            "rm -rf /",
            "sudo",
            "shutdown",
            "reboot",
            "> /dev/",
            "mkfs",
        ]
        if any(fragment in command for fragment in dangerous_fragments):
            raise ValueError("Dangerous command blocked")

        try:
            result = subprocess.run(
                command,
                shell=True,
                cwd=self.workdir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.bash_timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(f"Command timed out after {self.bash_timeout:g}s") from exc
        except (FileNotFoundError, OSError) as exc:
            raise RuntimeError(str(exc)) from exc

        output = (result.stdout + result.stderr).strip()
        if not output:
            return "(no output)"
        return output[: self.output_limit]

    def read_file(self, path: str, limit: int | None = None) -> str:
        file_path = self.safe_path(path)
        lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
        if limit is not None and limit > 0 and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)

    def write_file(self, path: str, content: str) -> str:
        file_path = self.safe_path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} bytes to {path}"

    def edit_file(self, path: str, old_text: str, new_text: str) -> str:
        file_path = self.safe_path(path)
        text = file_path.read_text(encoding="utf-8", errors="replace")
        if old_text not in text:
            raise ValueError(f"Text not found in {path}")

        file_path.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
        return f"Edited {path}"

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
                matches.append(str(candidate.relative_to(self.workdir)))

        return "\n".join(sorted(matches)) if matches else "(no matches)"


def basic_tool_definitions(workdir: Path | str | None = None) -> list[ToolDefinition]:
    tools = BasicTools(workdir=Path.cwd() if workdir is None else Path(workdir))

    return [
        ToolDefinition(
            name="bash",
            description="Run a shell command in the workspace.",
            parameters={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to run.",
                    }
                },
                "required": ["command"],
            },
            func=tools.bash,
        ),
        ToolDefinition(
            name="read_file",
            description="Read a UTF-8 text file from the workspace.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Workspace-relative file path.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Optional maximum number of lines to return.",
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
                },
                "required": ["path", "old_text", "new_text"],
            },
            func=tools.edit_file,
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
) -> None:
    registry.register_many(basic_tool_definitions(workdir=workdir))
