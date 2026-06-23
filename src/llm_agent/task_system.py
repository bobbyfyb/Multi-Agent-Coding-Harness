from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Literal


TaskStatus = Literal["pending", "in_progress", "completed", "blocked", "cancelled"]
TaskScope = Literal["session", "project"]

TASK_STATUSES: set[str] = {
    "pending",
    "in_progress",
    "completed",
    "blocked",
    "cancelled",
}
TASK_SCOPES: set[str] = {"session", "project"}
OPEN_TASK_STATUSES = {"pending", "in_progress", "blocked"}


class TaskSystemError(RuntimeError):
    """Raised when the task system cannot complete an operation."""


class TaskNotFoundError(TaskSystemError):
    """Raised when a task id does not exist."""


@dataclass
class Task:
    id: str
    title: str
    description: str = ""
    status: TaskStatus = "pending"
    scope: TaskScope = "session"
    owner: str | None = None
    blocked_by: list[str] = field(default_factory=list)
    parent_id: str | None = None
    priority: int = 100
    evidence: str | None = None
    notes: str | None = None
    created_at: str = ""
    updated_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Task":
        return cls(
            id=str(data["id"]),
            title=str(data["title"]),
            description=str(data.get("description", "")),
            status=_validate_status(data.get("status", "pending")),
            scope=_validate_scope(data.get("scope", "session")),
            owner=_optional_str(data.get("owner")),
            blocked_by=_normalize_string_list(data.get("blocked_by", []), "blocked_by"),
            parent_id=_optional_str(data.get("parent_id")),
            priority=int(data.get("priority", 100)),
            evidence=_optional_str(data.get("evidence")),
            notes=_optional_str(data.get("notes")),
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
            metadata=dict(data.get("metadata") or {}),
        )

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_TASK_STATUSES


@dataclass
class TaskStore:
    tasks_dir: Path
    task_list_id: str = "default"

    def __post_init__(self) -> None:
        self.tasks_dir = Path(self.tasks_dir).resolve()
        self.list_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def for_workdir(
        cls,
        workdir: Path | str | None = None,
        *,
        task_list_id: str = "default",
    ) -> "TaskStore":
        root = Path.cwd() if workdir is None else Path(workdir)
        return cls(root / ".llm_agent" / "tasks", task_list_id=task_list_id)

    @property
    def list_dir(self) -> Path:
        return self.tasks_dir / self.task_list_id

    def next_id(self) -> str:
        marker = self.list_dir / ".highwatermark"
        last_id = int(marker.read_text(encoding="utf-8").strip() or "0") if marker.exists() else 0
        next_id = last_id + 1
        marker.write_text(str(next_id), encoding="utf-8")
        return f"task_{next_id:04d}"

    def save(self, task: Task) -> None:
        self.list_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = self._task_path(task.id).with_suffix(".tmp")
        tmp_path.write_text(
            json.dumps(task.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp_path.replace(self._task_path(task.id))

    def load(self, task_id: str) -> Task:
        path = self._task_path(task_id)
        if not path.exists():
            raise TaskNotFoundError(f"Task not found: {task_id}")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise TaskSystemError(f"Task file is not a JSON object: {task_id}")
        return Task.from_dict(data)

    def list(self) -> list[Task]:
        return [
            self.load(path.stem)
            for path in sorted(self.list_dir.glob("task_*.json"))
            if path.is_file()
        ]

    def _task_path(self, task_id: str) -> Path:
        if "/" in task_id or "\\" in task_id or task_id.startswith("."):
            raise TaskSystemError(f"Invalid task id: {task_id}")
        return self.list_dir / f"{task_id}.json"


@dataclass
class TaskManager:
    store: TaskStore

    @classmethod
    def for_workdir(
        cls,
        workdir: Path | str | None = None,
        *,
        task_list_id: str = "default",
    ) -> "TaskManager":
        return cls(TaskStore.for_workdir(workdir, task_list_id=task_list_id))

    def create_task(
        self,
        title: str,
        description: str = "",
        *,
        scope: TaskScope = "session",
        owner: str | None = None,
        blocked_by: list[str] | None = None,
        parent_id: str | None = None,
        priority: int = 100,
        notes: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Task:
        now = _now()
        task = Task(
            id=self.store.next_id(),
            title=title.strip(),
            description=description.strip(),
            status="pending",
            scope=_validate_scope(scope),
            owner=owner,
            blocked_by=_normalize_string_list(blocked_by or [], "blocked_by"),
            parent_id=parent_id,
            priority=priority,
            notes=notes,
            created_at=now,
            updated_at=now,
            metadata=dict(metadata or {}),
        )
        if not task.title:
            raise TaskSystemError("Task title is required.")
        self.store.save(task)
        return task

    def get_task(self, task_id: str) -> Task:
        return self.store.load(task_id)

    def list_tasks(
        self,
        *,
        scope: TaskScope | None = None,
        status: TaskStatus | None = None,
        owner: str | None = None,
        include_completed: bool = True,
    ) -> list[Task]:
        tasks = self.store.list()
        if scope is not None:
            resolved_scope = _validate_scope(scope)
            tasks = [task for task in tasks if task.scope == resolved_scope]
        if status is not None:
            resolved_status = _validate_status(status)
            tasks = [task for task in tasks if task.status == resolved_status]
        if owner is not None:
            tasks = [task for task in tasks if task.owner == owner]
        if not include_completed:
            tasks = [task for task in tasks if task.status not in {"completed", "cancelled"}]
        return sorted(tasks, key=lambda task: (task.priority, task.created_at, task.id))

    def update_task(
        self,
        task_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
        status: TaskStatus | None = None,
        scope: TaskScope | None = None,
        owner: str | None = None,
        blocked_by: list[str] | None = None,
        parent_id: str | None = None,
        priority: int | None = None,
        evidence: str | None = None,
        notes: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Task:
        task = self.store.load(task_id)
        if title is not None:
            if not title.strip():
                raise TaskSystemError("Task title cannot be empty.")
            task.title = title.strip()
        if description is not None:
            task.description = description.strip()
        if status is not None:
            resolved_status = _validate_status(status)
            if resolved_status == "in_progress" and not self.can_start(task.id):
                raise TaskSystemError(
                    f"Task {task.id} is blocked by unfinished dependencies."
                )
            task.status = resolved_status
        if scope is not None:
            task.scope = _validate_scope(scope)
        if owner is not None:
            task.owner = owner
        if blocked_by is not None:
            task.blocked_by = _normalize_string_list(blocked_by, "blocked_by")
        if parent_id is not None:
            task.parent_id = parent_id
        if priority is not None:
            task.priority = priority
        if evidence is not None:
            task.evidence = evidence
        if notes is not None:
            task.notes = notes
        if metadata is not None:
            task.metadata.update(metadata)

        task.updated_at = _now()
        self.store.save(task)
        return task

    def claim_task(self, task_id: str, *, owner: str = "agent") -> Task:
        task = self.store.load(task_id)
        if task.status != "pending":
            raise TaskSystemError(
                f"Task {task_id} is {task.status}, only pending tasks can be claimed."
            )
        if not self.can_start(task_id):
            raise TaskSystemError(f"Task {task_id} is blocked by unfinished dependencies.")

        task.owner = owner
        task.status = "in_progress"
        task.updated_at = _now()
        self.store.save(task)
        return task

    def complete_task(
        self,
        task_id: str,
        *,
        evidence: str | None = None,
        notes: str | None = None,
    ) -> tuple[Task, list[Task]]:
        task = self.store.load(task_id)
        if task.status != "in_progress":
            raise TaskSystemError(
                f"Task {task_id} is {task.status}, only in_progress tasks can be completed."
            )

        task.status = "completed"
        if evidence is not None:
            task.evidence = evidence
        if notes is not None:
            task.notes = notes
        task.updated_at = _now()
        self.store.save(task)

        unblocked = [
            candidate
            for candidate in self.list_tasks(include_completed=False)
            if task.id in candidate.blocked_by and self.can_start(candidate.id)
        ]
        return task, unblocked

    def can_start(self, task_id: str) -> bool:
        task = self.store.load(task_id)
        for dependency_id in task.blocked_by:
            try:
                dependency = self.store.load(dependency_id)
            except TaskNotFoundError:
                return False
            if dependency.status != "completed":
                return False
        return True

    def summary(
        self,
        *,
        scope: TaskScope | None = None,
        include_completed: bool = False,
        limit: int = 12,
    ) -> str:
        tasks = self.list_tasks(scope=scope, include_completed=include_completed)
        if not tasks:
            return "No tasks."

        lines = []
        for task in tasks[:limit]:
            owner = f" owner={task.owner}" if task.owner else ""
            deps = f" blocked_by={','.join(task.blocked_by)}" if task.blocked_by else ""
            lines.append(
                f"- {task.id} [{task.status}/{task.scope}]{owner}{deps}: {task.title}"
            )
        if len(tasks) > limit:
            lines.append(f"... ({len(tasks) - limit} more tasks)")
        return "\n".join(lines)


def format_task(task: Task) -> str:
    return json.dumps(task.to_dict(), ensure_ascii=False, indent=2)


def format_task_list(tasks: list[Task]) -> str:
    if not tasks:
        return "No tasks."
    lines = []
    for task in tasks:
        owner = f" owner={task.owner}" if task.owner else ""
        deps = f" blocked_by={','.join(task.blocked_by)}" if task.blocked_by else ""
        evidence = " evidence=yes" if task.evidence else ""
        lines.append(
            f"{task.id} [{task.status}/{task.scope}]{owner}{deps}{evidence}: "
            f"{task.title}"
        )
    return "\n".join(lines)


def _now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _validate_status(status: Any) -> TaskStatus:
    if status not in TASK_STATUSES:
        raise TaskSystemError(f"Invalid task status: {status}")
    return status


def _validate_scope(scope: Any) -> TaskScope:
    if scope not in TASK_SCOPES:
        raise TaskSystemError(f"Invalid task scope: {scope}")
    return scope


def _normalize_string_list(value: Any, field_name: str) -> list[str]:
    if not isinstance(value, list):
        raise TaskSystemError(f"{field_name} must be a list.")
    normalized = []
    for item in value:
        if not isinstance(item, str):
            raise TaskSystemError(f"{field_name} items must be strings.")
        if item:
            normalized.append(item)
    return normalized


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


__all__ = [
    "OPEN_TASK_STATUSES",
    "Task",
    "TaskManager",
    "TaskNotFoundError",
    "TaskScope",
    "TaskStatus",
    "TaskStore",
    "TaskSystemError",
    "format_task",
    "format_task_list",
]
