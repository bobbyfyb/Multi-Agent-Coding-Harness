from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
from threading import Event, RLock, Thread
from time import perf_counter
from typing import Any, Literal
from uuid import uuid4

from llm_agent.security import safe_subprocess_env, validate_shell_command
from llm_agent.trace_system import (
    TraceContext,
    TraceRecorder,
    current_trace_context,
    current_trace_recorder,
)


BackgroundJobStatus = Literal[
    "created",
    "running",
    "completed",
    "failed",
    "cancelled",
    "timed_out",
    "interrupted",
]

BACKGROUND_JOB_STATUSES: set[str] = {
    "created",
    "running",
    "completed",
    "failed",
    "cancelled",
    "timed_out",
    "interrupted",
}
TERMINAL_JOB_STATUSES: set[str] = {
    "completed",
    "failed",
    "cancelled",
    "timed_out",
    "interrupted",
}


class BackgroundJobError(RuntimeError):
    """Raised when a background job operation cannot be completed."""


class BackgroundJobNotFoundError(BackgroundJobError):
    """Raised when a background job does not exist."""


@dataclass
class BackgroundJob:
    id: str
    command: str
    cwd: str
    status: BackgroundJobStatus = "created"
    kind: str = "local_bash"
    created_at: str = ""
    updated_at: str = ""
    started_at: str | None = None
    finished_at: str | None = None
    pid: int | None = None
    return_code: int | None = None
    owner_run_id: str | None = None
    owner_agent_id: str | None = None
    task_id: str | None = None
    stdout_path: str = ""
    stderr_path: str = ""
    max_runtime_seconds: float | None = None
    error: str | None = None
    notified_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BackgroundJob":
        status = str(data.get("status", "created"))
        if status not in BACKGROUND_JOB_STATUSES:
            raise BackgroundJobError(f"Invalid background job status: {status}")
        return cls(
            id=str(data["id"]),
            command=str(data["command"]),
            cwd=str(data["cwd"]),
            status=status,  # type: ignore[arg-type]
            kind=str(data.get("kind", "local_bash")),
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
            started_at=_optional_str(data.get("started_at")),
            finished_at=_optional_str(data.get("finished_at")),
            pid=_optional_int(data.get("pid")),
            return_code=_optional_int(data.get("return_code")),
            owner_run_id=_optional_str(data.get("owner_run_id")),
            owner_agent_id=_optional_str(data.get("owner_agent_id")),
            task_id=_optional_str(data.get("task_id")),
            stdout_path=str(data.get("stdout_path", "")),
            stderr_path=str(data.get("stderr_path", "")),
            max_runtime_seconds=_optional_float(data.get("max_runtime_seconds")),
            error=_optional_str(data.get("error")),
            notified_at=_optional_str(data.get("notified_at")),
        )

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_JOB_STATUSES


@dataclass(frozen=True)
class BackgroundNotification:
    job_id: str
    status: BackgroundJobStatus
    return_code: int | None
    finished_at: str | None
    stdout_path: str
    stderr_path: str
    error: str | None = None
    task_id: str | None = None

    @classmethod
    def from_job(cls, job: BackgroundJob) -> "BackgroundNotification":
        return cls(
            job_id=job.id,
            status=job.status,
            return_code=job.return_code,
            finished_at=job.finished_at,
            stdout_path=job.stdout_path,
            stderr_path=job.stderr_path,
            error=job.error,
            task_id=job.task_id,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BackgroundJobStore:
    root_dir: Path

    def __post_init__(self) -> None:
        self.root_dir = Path(self.root_dir).resolve()
        self.root_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def for_workdir(
        cls,
        workdir: Path | str | None = None,
    ) -> "BackgroundJobStore":
        root = Path.cwd() if workdir is None else Path(workdir)
        return cls(root / ".llm_agent" / "background")

    @property
    def workdir(self) -> Path:
        return self.root_dir.parent.parent

    def create_job_paths(self, job_id: str) -> tuple[Path, Path]:
        job_dir = self._job_dir(job_id)
        job_dir.mkdir(parents=True, exist_ok=False)
        return job_dir / "stdout.log", job_dir / "stderr.log"

    def save(self, job: BackgroundJob) -> None:
        job_dir = self._job_dir(job.id)
        job_dir.mkdir(parents=True, exist_ok=True)
        path = job_dir / "job.json"
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_text(
            json.dumps(job.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp_path.replace(path)

    def load(self, job_id: str) -> BackgroundJob:
        path = self._job_path(job_id)
        if not path.exists():
            raise BackgroundJobNotFoundError(f"Background job not found: {job_id}")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise BackgroundJobError(
                f"Background job file is not a JSON object: {job_id}"
            )
        return BackgroundJob.from_dict(data)

    def list(self) -> list[BackgroundJob]:
        jobs = []
        for path in sorted(self.root_dir.glob("bg_*/job.json")):
            if path.is_file():
                jobs.append(self.load(path.parent.name))
        return sorted(jobs, key=lambda job: (job.created_at, job.id))

    def read_log(
        self,
        job_id: str,
        stream: Literal["stdout", "stderr"],
        *,
        tail_chars: int,
    ) -> str:
        self.load(job_id)
        path = self._job_dir(job_id) / f"{stream}.log"
        if not path.exists():
            return ""

        max_bytes = max(4_096, tail_chars * 4)
        size = path.stat().st_size
        with path.open("rb") as file:
            if size > max_bytes:
                file.seek(size - max_bytes)
            content = file.read()
        text = content.decode("utf-8", errors="replace")
        return text[-tail_chars:]

    def _job_dir(self, job_id: str) -> Path:
        if (
            not job_id.startswith("bg_")
            or "/" in job_id
            or "\\" in job_id
            or job_id.startswith(".")
        ):
            raise BackgroundJobError(f"Invalid background job id: {job_id}")
        return self.root_dir / job_id

    def _job_path(self, job_id: str) -> Path:
        return self._job_dir(job_id) / "job.json"


@dataclass
class _RunningJob:
    process: subprocess.Popen[Any]
    done: Event
    trace_recorder: TraceRecorder | None
    trace_context: TraceContext | None
    started_at: float


@dataclass
class BackgroundJobManager:
    store: BackgroundJobStore
    max_concurrent: int = 4
    max_runtime_seconds: float = 1_800.0
    terminate_grace_seconds: float = 3.0
    max_output_chars: int = 50_000
    _lock: RLock = field(default_factory=RLock, init=False, repr=False)
    _running: dict[str, _RunningJob] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _requested_status: dict[str, BackgroundJobStatus] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _notifications: deque[str] = field(
        default_factory=deque,
        init=False,
        repr=False,
    )
    _queued_notifications: set[str] = field(
        default_factory=set,
        init=False,
        repr=False,
    )
    _closed: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.max_concurrent <= 0:
            raise ValueError("max_concurrent must be greater than zero.")
        if self.max_runtime_seconds <= 0:
            raise ValueError("max_runtime_seconds must be greater than zero.")
        if self.terminate_grace_seconds < 0:
            raise ValueError("terminate_grace_seconds cannot be negative.")
        if self.max_output_chars <= 0:
            raise ValueError("max_output_chars must be greater than zero.")
        self._restore_persisted_state()

    @classmethod
    def for_workdir(
        cls,
        workdir: Path | str | None = None,
        *,
        max_concurrent: int = 4,
        max_runtime_seconds: float = 1_800.0,
        terminate_grace_seconds: float = 3.0,
        max_output_chars: int = 50_000,
    ) -> "BackgroundJobManager":
        return cls(
            store=BackgroundJobStore.for_workdir(workdir),
            max_concurrent=max_concurrent,
            max_runtime_seconds=max_runtime_seconds,
            terminate_grace_seconds=terminate_grace_seconds,
            max_output_chars=max_output_chars,
        )

    def start_shell(
        self,
        command: str,
        *,
        owner_run_id: str | None = None,
        owner_agent_id: str | None = None,
        task_id: str | None = None,
    ) -> BackgroundJob:
        command = command.strip()
        try:
            validate_shell_command(command, workdir=self.store.workdir)
        except ValueError as exc:
            raise BackgroundJobError(str(exc)) from exc

        with self._lock:
            if self._closed:
                raise BackgroundJobError("Background job manager is closed.")
            if len(self._running) >= self.max_concurrent:
                raise BackgroundJobError(
                    f"Background job concurrency limit reached: {self.max_concurrent}"
                )

            job_id = f"bg_{uuid4().hex[:12]}"
            stdout_path, stderr_path = self.store.create_job_paths(job_id)
            now = _now()
            job = BackgroundJob(
                id=job_id,
                command=command,
                cwd=str(self.store.workdir),
                status="created",
                created_at=now,
                updated_at=now,
                owner_run_id=owner_run_id,
                owner_agent_id=owner_agent_id,
                task_id=task_id,
                stdout_path=str(stdout_path),
                stderr_path=str(stderr_path),
                max_runtime_seconds=self.max_runtime_seconds,
            )
            self.store.save(job)

            trace_recorder = current_trace_recorder()
            trace_context = current_trace_context()
            try:
                with (
                    stdout_path.open("ab", buffering=0) as stdout_file,
                    stderr_path.open("ab", buffering=0) as stderr_file,
                ):
                    process = subprocess.Popen(
                        command,
                        shell=True,
                        cwd=job.cwd,
                        env=safe_subprocess_env(job.cwd),
                        stdin=subprocess.DEVNULL,
                        stdout=stdout_file,
                        stderr=stderr_file,
                        start_new_session=os.name == "posix",
                    )
            except (OSError, ValueError) as exc:
                job.status = "failed"
                job.error = str(exc)
                job.finished_at = _now()
                job.updated_at = job.finished_at
                self.store.save(job)
                self._enqueue_notification(job.id)
                self._record_trace(
                    trace_recorder,
                    trace_context,
                    job,
                    name="background.failed",
                    phase="failed",
                    status="error",
                    data={"error": str(exc)},
                )
                return job

            job.status = "running"
            job.pid = process.pid
            job.started_at = _now()
            job.updated_at = job.started_at
            self.store.save(job)
            running = _RunningJob(
                process=process,
                done=Event(),
                trace_recorder=trace_recorder,
                trace_context=trace_context,
                started_at=perf_counter(),
            )
            self._running[job.id] = running
            monitor = Thread(
                target=self._monitor,
                args=(job.id,),
                name=f"background-{job.id}",
                daemon=True,
            )
            monitor.start()

        self._record_trace(
            trace_recorder,
            trace_context,
            job,
            name="background.started",
            phase="started",
            data={
                "command": command,
                "pid": job.pid,
                "task_id": task_id,
            },
        )
        return job

    def get_job(self, job_id: str) -> BackgroundJob:
        with self._lock:
            return self.store.load(job_id)

    def list_jobs(
        self,
        *,
        status: BackgroundJobStatus | None = None,
        limit: int = 20,
    ) -> list[BackgroundJob]:
        if limit <= 0:
            raise BackgroundJobError("limit must be greater than zero.")
        if status is not None and status not in BACKGROUND_JOB_STATUSES:
            raise BackgroundJobError(f"Invalid background job status: {status}")
        with self._lock:
            jobs = self.store.list()
        if status is not None:
            jobs = [job for job in jobs if job.status == status]
        return list(reversed(jobs))[:limit]

    def read_output(
        self,
        job_id: str,
        *,
        stream: Literal["combined", "stdout", "stderr"] = "combined",
        tail_chars: int = 8_000,
    ) -> dict[str, str]:
        if stream not in {"combined", "stdout", "stderr"}:
            raise BackgroundJobError(f"Invalid output stream: {stream}")
        if tail_chars <= 0:
            raise BackgroundJobError("tail_chars must be greater than zero.")
        resolved_limit = min(tail_chars, self.max_output_chars)
        with self._lock:
            self.store.load(job_id)
            output: dict[str, str] = {}
            if stream in {"combined", "stdout"}:
                output["stdout"] = self.store.read_log(
                    job_id,
                    "stdout",
                    tail_chars=resolved_limit,
                )
            if stream in {"combined", "stderr"}:
                output["stderr"] = self.store.read_log(
                    job_id,
                    "stderr",
                    tail_chars=resolved_limit,
                )
        return output

    def wait(
        self,
        job_id: str,
        *,
        timeout_seconds: float = 30.0,
    ) -> BackgroundJob:
        if timeout_seconds < 0:
            raise BackgroundJobError("timeout_seconds cannot be negative.")
        with self._lock:
            job = self.store.load(job_id)
            running = self._running.get(job_id)
        if job.is_terminal or running is None:
            return job
        running.done.wait(timeout_seconds)
        return self.get_job(job_id)

    def cancel(self, job_id: str) -> BackgroundJob:
        return self._request_stop(job_id, final_status="cancelled")

    def drain_notifications(
        self,
        *,
        owner_agent_id: str | None = None,
        limit: int = 20,
    ) -> list[BackgroundNotification]:
        if limit <= 0:
            raise BackgroundJobError("limit must be greater than zero.")
        notifications: list[BackgroundNotification] = []
        deferred: deque[str] = deque()
        with self._lock:
            pending_count = len(self._notifications)
            for _ in range(pending_count):
                job_id = self._notifications.popleft()
                self._queued_notifications.discard(job_id)
                job = self.store.load(job_id)
                if owner_agent_id is not None and job.owner_agent_id not in {
                    None,
                    owner_agent_id,
                }:
                    deferred.append(job_id)
                    continue
                if len(notifications) >= limit:
                    deferred.append(job_id)
                    continue
                if job.notified_at is None and job.is_terminal:
                    notifications.append(BackgroundNotification.from_job(job))
            while deferred:
                self._enqueue_notification(deferred.popleft())
        return notifications

    def acknowledge_notifications(self, job_ids: list[str]) -> None:
        with self._lock:
            for job_id in job_ids:
                job = self.store.load(job_id)
                if job.notified_at is None:
                    job.notified_at = _now()
                    job.updated_at = job.notified_at
                    self.store.save(job)

    def shutdown(self, *, cancel_running: bool = True) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            running_ids = list(self._running)
        if cancel_running:
            for job_id in running_ids:
                self._request_stop(job_id, final_status="interrupted")

    def _monitor(self, job_id: str) -> None:
        with self._lock:
            running = self._running[job_id]
            job = self.store.load(job_id)

        return_code: int | None = None
        error: str | None = None
        try:
            return_code = running.process.wait(timeout=job.max_runtime_seconds)
        except subprocess.TimeoutExpired:
            with self._lock:
                self._requested_status[job_id] = "timed_out"
            self._terminate_process(running.process)
            return_code = running.process.wait()
            error = f"Background command timed out after {job.max_runtime_seconds:g}s."
        except Exception as exc:
            error = str(exc)
            self._terminate_process(running.process)
            return_code = running.process.poll()

        with self._lock:
            requested_status = self._requested_status.pop(job_id, None)
            if requested_status is not None:
                final_status = requested_status
            elif error is not None:
                final_status = "failed"
            elif return_code == 0:
                final_status = "completed"
            else:
                final_status = "failed"
                error = f"Process exited with code {return_code}."

            job = self.store.load(job_id)
            job.status = final_status
            job.return_code = return_code
            job.error = error
            job.finished_at = _now()
            job.updated_at = job.finished_at
            self.store.save(job)
            self._running.pop(job_id, None)
            self._enqueue_notification(job_id)

        trace_status = "ok" if final_status == "completed" else "error"
        if final_status in {"cancelled", "interrupted"}:
            trace_status = "warning"
        try:
            self._record_trace(
                running.trace_recorder,
                running.trace_context,
                job,
                name=f"background.{final_status}",
                phase="completed",
                status=trace_status,
                duration_ms=(perf_counter() - running.started_at) * 1_000,
                data={
                    "return_code": return_code,
                    "error": error,
                    "stdout_path": job.stdout_path,
                    "stderr_path": job.stderr_path,
                },
            )
        finally:
            running.done.set()

    def _request_stop(
        self,
        job_id: str,
        *,
        final_status: BackgroundJobStatus,
    ) -> BackgroundJob:
        with self._lock:
            job = self.store.load(job_id)
            if job.is_terminal:
                return job
            running = self._running.get(job_id)
            if running is None:
                job.status = "interrupted"
                job.error = "Background process is no longer managed."
                job.finished_at = _now()
                job.updated_at = job.finished_at
                self.store.save(job)
                self._enqueue_notification(job.id)
                return job
            if running.process.poll() is None:
                self._requested_status[job_id] = final_status

        self._terminate_process(running.process)
        running.done.wait(self.terminate_grace_seconds + 1.0)
        return self.get_job(job_id)

    def _terminate_process(self, process: subprocess.Popen[Any]) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
            process.wait(timeout=self.terminate_grace_seconds)
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
            return

    def _restore_persisted_state(self) -> None:
        with self._lock:
            for job in self.store.list():
                if job.status in {"created", "running"}:
                    job.status = "interrupted"
                    job.error = "Agent process exited before the job completed."
                    job.finished_at = _now()
                    job.updated_at = job.finished_at
                    self.store.save(job)
                if job.is_terminal and job.notified_at is None:
                    self._enqueue_notification(job.id)

    def _enqueue_notification(self, job_id: str) -> None:
        if job_id in self._queued_notifications:
            return
        self._notifications.append(job_id)
        self._queued_notifications.add(job_id)

    @staticmethod
    def _record_trace(
        recorder: TraceRecorder | None,
        context: TraceContext | None,
        job: BackgroundJob,
        *,
        name: str,
        phase: str,
        status: str = "ok",
        data: dict[str, Any] | None = None,
        duration_ms: float | None = None,
    ) -> None:
        if recorder is None:
            return
        recorder.record(
            category="background",
            name=name,
            phase=phase,
            status=status,
            correlation_id=job.id,
            duration_ms=duration_ms,
            data={
                "job_id": job.id,
                "status": job.status,
                **(data or {}),
            },
            context=context,
        )
        recorder.render_markdown()


BACKGROUND_JOB_INSTRUCTIONS = """
Background jobs:
- Tool calls are synchronous by default. Use bash with run_in_background=true
  only for independent, long-running commands whose result is not required for
  the next reasoning step.
- Keep file edits and short inspection commands synchronous.
- A background bash call returns a job_id immediately. Use background_get,
  background_output, background_wait, or background_cancel to manage it.
- Completion arrives as a separate background notification; it is not another
  result for the original tool call.
- Do not treat a successful process exit as automatic completion of a planning
  task. Inspect the result and record task evidence explicitly.
""".strip()


def format_background_notifications(
    notifications: list[BackgroundNotification],
) -> str:
    payload = json.dumps(
        [notification.to_dict() for notification in notifications],
        ensure_ascii=False,
    )
    payload = payload.replace("<", "\\u003c").replace(">", "\\u003e")
    return (
        "<background_notifications>\n"
        "The following managed background jobs reached a terminal state. "
        "Inspect their output when relevant.\n"
        f"{payload}\n"
        "</background_notifications>"
    )


def _now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)


__all__ = [
    "BACKGROUND_JOB_INSTRUCTIONS",
    "BACKGROUND_JOB_STATUSES",
    "TERMINAL_JOB_STATUSES",
    "BackgroundJob",
    "BackgroundJobError",
    "BackgroundJobManager",
    "BackgroundJobNotFoundError",
    "BackgroundJobStatus",
    "BackgroundJobStore",
    "BackgroundNotification",
    "format_background_notifications",
]
