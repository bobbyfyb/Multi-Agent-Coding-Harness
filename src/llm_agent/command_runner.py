from __future__ import annotations

import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
from time import perf_counter
from typing import Any, Sequence
from uuid import uuid4


def command_artifact_dir(
    workdir: Path,
    *,
    context: Any = None,
    tool_name: str,
) -> Path:
    run_id = _safe_component(getattr(context, "run_id", None) or "manual")
    metadata = getattr(context, "metadata", {}) or {}
    call_id = metadata.get("tool_call_id")
    artifact_id = _safe_component(
        str(call_id) if call_id else f"{tool_name}-{uuid4().hex[:10]}"
    )
    path = workdir / ".llm_agent" / "tool-results" / run_id / artifact_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def run_command(
    command: str | Sequence[str],
    *,
    cwd: Path,
    artifact_dir: Path,
    timeout_seconds: float,
    preview_chars: int = 50_000,
    shell: bool = False,
    terminate_grace_seconds: float = 1.0,
) -> dict[str, Any]:
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be greater than zero.")
    if preview_chars <= 0:
        raise ValueError("preview_chars must be greater than zero.")

    artifact_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = artifact_dir / "stdout.log"
    stderr_path = artifact_dir / "stderr.log"
    started_at = perf_counter()

    try:
        with (
            stdout_path.open("wb") as stdout_file,
            stderr_path.open("wb") as stderr_file,
        ):
            process = subprocess.Popen(
                command,
                shell=shell,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                start_new_session=os.name == "posix",
            )
            timed_out = False
            try:
                exit_code = process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                _terminate_process(process, terminate_grace_seconds)
                exit_code = process.wait()
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise RuntimeError(f"Failed to start command: {exc}") from exc

    status = "timed_out" if timed_out else ("completed" if exit_code == 0 else "failed")
    return {
        "status": status,
        "command": (command if isinstance(command, str) else shlex.join(command)),
        "exit_code": exit_code,
        "stdout": _read_preview(stdout_path, preview_chars),
        "stderr": _read_preview(stderr_path, preview_chars),
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "duration_ms": round((perf_counter() - started_at) * 1_000, 3),
        "timed_out": timed_out,
        "truncated": (
            stdout_path.stat().st_size > preview_chars
            or stderr_path.stat().st_size > preview_chars
        ),
    }


def _read_preview(path: Path, limit: int) -> str:
    size = path.stat().st_size
    with path.open("rb") as file:
        if size > limit:
            file.seek(size - limit)
        content = file.read()
    text = content.decode("utf-8", errors="replace")
    if size <= limit:
        return text
    return f"[... {size - limit} earlier bytes omitted ...]\n{text}"


def _terminate_process(
    process: subprocess.Popen[Any],
    grace_seconds: float,
) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=grace_seconds)
        return
    except (ProcessLookupError, subprocess.TimeoutExpired):
        pass

    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except ProcessLookupError:
        pass


def _safe_component(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    return sanitized[:96] or "unknown"


__all__ = ["command_artifact_dir", "run_command"]
