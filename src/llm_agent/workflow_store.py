from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Literal


WorkflowRunStatus = Literal["running", "completed", "failed", "interrupted"]
WorkflowCheckpointStatus = Literal["running", "completed", "failed"]


class WorkflowStoreError(RuntimeError):
    """Raised when workflow run state cannot be persisted or loaded."""


class WorkflowRunNotFoundError(WorkflowStoreError):
    """Raised when a workflow run record does not exist."""


@dataclass
class WorkflowPhaseCheckpoint:
    phase_key: str
    phase: str
    role: str
    status: WorkflowCheckpointStatus
    before_versions: dict[str, int]
    started_at: str
    completed_at: str | None = None
    attempts: int = 0
    run_ids: list[str] = field(default_factory=list)
    artifact_ids: list[str] = field(default_factory=list)
    error: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorkflowPhaseCheckpoint":
        return cls(
            phase_key=str(data["phase_key"]),
            phase=str(data["phase"]),
            role=str(data["role"]),
            status=str(data["status"]),  # type: ignore[arg-type]
            before_versions={
                str(key): int(value)
                for key, value in dict(data.get("before_versions") or {}).items()
            },
            started_at=str(data["started_at"]),
            completed_at=(
                str(data["completed_at"])
                if data.get("completed_at") is not None
                else None
            ),
            attempts=int(data.get("attempts") or 0),
            run_ids=[str(value) for value in data.get("run_ids") or []],
            artifact_ids=[
                str(value) for value in data.get("artifact_ids") or []
            ],
            error=str(data["error"]) if data.get("error") is not None else None,
            data=dict(data.get("data") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class WorkflowRunRecord:
    workflow_id: str
    request: str
    status: WorkflowRunStatus
    current_phase: str | None
    current_phase_key: str | None
    phases: list[dict[str, Any]]
    checkpoints: dict[str, WorkflowPhaseCheckpoint]
    artifact_ids: list[str]
    initial_artifact_ids: list[str]
    worktree_id: str | None
    worktree_base_commit: str | None
    qa_verdict: str | None
    fix_cycles: int
    created_at: str
    updated_at: str
    error: str | None = None

    @classmethod
    def create(
        cls,
        *,
        workflow_id: str,
        request: str,
        initial_artifact_ids: list[str],
    ) -> "WorkflowRunRecord":
        now = _utc_now()
        return cls(
            workflow_id=workflow_id,
            request=request,
            status="running",
            current_phase=None,
            current_phase_key=None,
            phases=[],
            checkpoints={},
            artifact_ids=[],
            initial_artifact_ids=initial_artifact_ids,
            worktree_id=None,
            worktree_base_commit=None,
            qa_verdict=None,
            fix_cycles=0,
            created_at=now,
            updated_at=now,
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorkflowRunRecord":
        return cls(
            workflow_id=str(data["workflow_id"]),
            request=str(data["request"]),
            status=str(data["status"]),  # type: ignore[arg-type]
            current_phase=(
                str(data["current_phase"])
                if data.get("current_phase") is not None
                else None
            ),
            current_phase_key=(
                str(data["current_phase_key"])
                if data.get("current_phase_key") is not None
                else None
            ),
            phases=[dict(item) for item in data.get("phases") or []],
            checkpoints={
                str(key): WorkflowPhaseCheckpoint.from_dict(dict(value))
                for key, value in dict(data.get("checkpoints") or {}).items()
            },
            artifact_ids=[
                str(value) for value in data.get("artifact_ids") or []
            ],
            initial_artifact_ids=[
                str(value) for value in data.get("initial_artifact_ids") or []
            ],
            worktree_id=(
                str(data["worktree_id"])
                if data.get("worktree_id") is not None
                else None
            ),
            worktree_base_commit=(
                str(data["worktree_base_commit"])
                if data.get("worktree_base_commit") is not None
                else None
            ),
            qa_verdict=(
                str(data["qa_verdict"])
                if data.get("qa_verdict") is not None
                else None
            ),
            fix_cycles=int(data.get("fix_cycles") or 0),
            created_at=str(data["created_at"]),
            updated_at=str(data["updated_at"]),
            error=str(data["error"]) if data.get("error") is not None else None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow_id": self.workflow_id,
            "request": self.request,
            "status": self.status,
            "current_phase": self.current_phase,
            "current_phase_key": self.current_phase_key,
            "phases": self.phases,
            "checkpoints": {
                key: checkpoint.to_dict()
                for key, checkpoint in self.checkpoints.items()
            },
            "artifact_ids": self.artifact_ids,
            "initial_artifact_ids": self.initial_artifact_ids,
            "worktree_id": self.worktree_id,
            "worktree_base_commit": self.worktree_base_commit,
            "qa_verdict": self.qa_verdict,
            "fix_cycles": self.fix_cycles,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "error": self.error,
        }


@dataclass
class WorkflowStore:
    root: Path | str

    def __post_init__(self) -> None:
        self.root = Path(self.root).resolve()

    @classmethod
    def for_workdir(cls, workdir: Path | str | None = None) -> "WorkflowStore":
        root = Path.cwd() if workdir is None else Path(workdir)
        return cls(root / ".llm_agent" / "workflows")

    def create_run(
        self,
        *,
        workflow_id: str,
        request: str,
        initial_artifact_ids: list[str],
    ) -> WorkflowRunRecord:
        path = self.run_path(workflow_id)
        if path.exists():
            raise WorkflowStoreError(f"Workflow run already exists: {workflow_id}")
        record = WorkflowRunRecord.create(
            workflow_id=workflow_id,
            request=request,
            initial_artifact_ids=initial_artifact_ids,
        )
        self.save_run(record)
        return record

    def load_run(self, workflow_id: str) -> WorkflowRunRecord:
        path = self.run_path(workflow_id)
        if not path.is_file():
            raise WorkflowRunNotFoundError(f"Workflow run not found: {workflow_id}")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise WorkflowStoreError(f"Invalid workflow run JSON: {path}") from exc
        if not isinstance(data, dict):
            raise WorkflowStoreError(f"Workflow run JSON root must be object: {path}")
        return WorkflowRunRecord.from_dict(data)

    def save_run(self, record: WorkflowRunRecord) -> None:
        record.updated_at = _utc_now()
        path = self.run_path(record.workflow_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(path.name + ".tmp")
        tmp_path.write_text(
            json.dumps(record.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        tmp_path.replace(path)

    def list_runs(self) -> list[WorkflowRunRecord]:
        if not self.root.exists():
            return []
        runs: list[WorkflowRunRecord] = []
        for path in sorted(self.root.glob("*/run.json")):
            try:
                runs.append(self.load_run(path.parent.name))
            except WorkflowStoreError:
                continue
        return sorted(runs, key=lambda record: record.updated_at, reverse=True)

    def run_path(self, workflow_id: str) -> Path:
        _validate_workflow_id(workflow_id)
        return self.root / workflow_id / "run.json"

    def mark_phase_started(
        self,
        record: WorkflowRunRecord,
        *,
        phase_key: str,
        phase: str,
        role: str,
        before_versions: dict[str, int],
        data: dict[str, Any] | None = None,
    ) -> WorkflowPhaseCheckpoint:
        checkpoint = record.checkpoints.get(phase_key)
        if checkpoint is None:
            checkpoint = WorkflowPhaseCheckpoint(
                phase_key=phase_key,
                phase=phase,
                role=role,
                status="running",
                before_versions=dict(before_versions),
                started_at=_utc_now(),
                data=dict(data or {}),
            )
        else:
            checkpoint.status = "running"
            checkpoint.completed_at = None
            checkpoint.attempts = 0
            checkpoint.run_ids = []
            checkpoint.artifact_ids = []
            checkpoint.error = None
            checkpoint.data = dict(checkpoint.data if data is None else data)
        record.status = "running"
        record.current_phase = phase
        record.current_phase_key = phase_key
        record.checkpoints[phase_key] = checkpoint
        self.save_run(record)
        return checkpoint

    def mark_phase_completed(
        self,
        record: WorkflowRunRecord,
        *,
        phase_key: str,
        phase_result: dict[str, Any],
        attempts: int,
        run_ids: list[str],
        artifact_ids: list[str],
        data: dict[str, Any] | None = None,
    ) -> None:
        checkpoint = record.checkpoints.get(phase_key)
        if checkpoint is not None:
            checkpoint.status = "completed"
            checkpoint.completed_at = _utc_now()
            checkpoint.attempts = attempts
            checkpoint.run_ids = list(run_ids)
            checkpoint.artifact_ids = list(artifact_ids)
            checkpoint.error = None
            checkpoint.data = dict(checkpoint.data if data is None else data)
        _upsert_phase_result(record, phase_key, phase_result)
        record.current_phase = None
        record.current_phase_key = None
        record.error = None
        self.save_run(record)

    def mark_phase_failed(
        self,
        record: WorkflowRunRecord,
        *,
        phase_key: str,
        phase_result: dict[str, Any] | None = None,
        attempts: int,
        run_ids: list[str],
        artifact_ids: list[str],
        error: str | None,
        data: dict[str, Any] | None = None,
    ) -> None:
        checkpoint = record.checkpoints.get(phase_key)
        if checkpoint is not None:
            checkpoint.status = "failed"
            checkpoint.completed_at = _utc_now()
            checkpoint.attempts = attempts
            checkpoint.run_ids = list(run_ids)
            checkpoint.artifact_ids = list(artifact_ids)
            checkpoint.error = error
            checkpoint.data = dict(checkpoint.data if data is None else data)
        if phase_result is not None:
            _upsert_phase_result(record, phase_key, phase_result)
        record.current_phase = None
        record.current_phase_key = None
        record.error = error
        self.save_run(record)

    def mark_finished(
        self,
        record: WorkflowRunRecord,
        *,
        status: WorkflowRunStatus,
        artifact_ids: list[str],
        qa_verdict: str | None,
        fix_cycles: int,
        error: str | None,
    ) -> None:
        record.status = status
        record.current_phase = None
        record.current_phase_key = None
        record.artifact_ids = list(artifact_ids)
        record.qa_verdict = qa_verdict
        record.fix_cycles = fix_cycles
        record.error = error
        self.save_run(record)


def _validate_workflow_id(workflow_id: str) -> None:
    if not workflow_id or "/" in workflow_id or "\\" in workflow_id:
        raise WorkflowStoreError(f"Invalid workflow id: {workflow_id!r}")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _upsert_phase_result(
    record: WorkflowRunRecord,
    phase_key: str,
    phase_result: dict[str, Any],
) -> None:
    payload = {"phase_key": phase_key, **phase_result}
    for index, item in enumerate(record.phases):
        if item.get("phase_key") == phase_key:
            record.phases[index] = payload
            return
    record.phases.append(payload)


__all__ = [
    "WorkflowCheckpointStatus",
    "WorkflowPhaseCheckpoint",
    "WorkflowRunNotFoundError",
    "WorkflowRunRecord",
    "WorkflowRunStatus",
    "WorkflowStore",
    "WorkflowStoreError",
]
