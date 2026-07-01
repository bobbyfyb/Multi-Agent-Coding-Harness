from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
from threading import RLock
from typing import Any, Literal
from uuid import uuid4

from llm_agent.trace_system import record_trace


WorktreeStatus = Literal["active", "ready", "applied", "missing"]
WORKTREE_ID_PATTERN = re.compile(r"^wt_[0-9a-f]{12}$")
RUNTIME_PATHSPEC = ":(exclude).llm_agent/**"


class WorktreeError(RuntimeError):
    """Raised when a managed Git worktree operation fails."""


class WorktreeNotFoundError(WorktreeError):
    """Raised when managed worktree metadata does not exist."""


@dataclass
class WorktreeInfo:
    id: str
    path: str
    branch: str
    base_commit: str
    status: WorktreeStatus = "active"
    task_id: str | None = None
    agent_id: str | None = None
    run_id: str | None = None
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WorktreeInfo":
        status = str(data.get("status", "active"))
        if status not in {"active", "ready", "applied", "missing"}:
            raise WorktreeError(f"Invalid worktree status: {status}")
        return cls(
            id=str(data["id"]),
            path=str(data["path"]),
            branch=str(data["branch"]),
            base_commit=str(data["base_commit"]),
            status=status,  # type: ignore[arg-type]
            task_id=_optional_str(data.get("task_id")),
            agent_id=_optional_str(data.get("agent_id")),
            run_id=_optional_str(data.get("run_id")),
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
        )


@dataclass
class WorktreeManager:
    workdir: Path | str
    diff_preview_chars: int = 20_000

    def __post_init__(self) -> None:
        self.workdir = Path(self.workdir).resolve()
        self.state_dir = self.workdir / ".llm_agent" / "worktree-state"
        self.worktrees_dir = self.workdir / ".llm_agent" / "worktrees"
        self._lock = RLock()
        if self.diff_preview_chars <= 0:
            raise ValueError("diff_preview_chars must be greater than zero.")

    @classmethod
    def for_workdir(
        cls,
        workdir: Path | str | None = None,
    ) -> "WorktreeManager":
        return cls(Path.cwd() if workdir is None else Path(workdir))

    def create(
        self,
        *,
        task_id: str | None = None,
        agent_id: str | None = None,
        run_id: str | None = None,
    ) -> WorktreeInfo:
        with self._lock:
            self._ensure_repository()
            dirty = self._git(
                [
                    "status",
                    "--porcelain",
                    "--untracked-files=all",
                    "--",
                    ".",
                    RUNTIME_PATHSPEC,
                ]
            ).strip()
            if dirty:
                raise WorktreeError(
                    "Main workspace must be clean before creating an isolated worktree."
                )

            worktree_id = f"wt_{uuid4().hex[:12]}"
            path = (self.worktrees_dir / worktree_id).resolve()
            branch = f"llm-agent/{worktree_id}"
            base_commit = self._git(["rev-parse", "HEAD"]).strip()
            self.worktrees_dir.mkdir(parents=True, exist_ok=True)
            self._git(
                [
                    "worktree",
                    "add",
                    "-b",
                    branch,
                    str(path),
                    base_commit,
                ]
            )

            now = _now()
            info = WorktreeInfo(
                id=worktree_id,
                path=str(path),
                branch=branch,
                base_commit=base_commit,
                task_id=task_id,
                agent_id=agent_id,
                run_id=run_id,
                created_at=now,
                updated_at=now,
            )
            self._save(info)
            record_trace(
                category="worktree",
                name="worktree.created",
                phase="created",
                correlation_id=info.id,
                data=info.to_dict(),
            )
            return info

    def get(self, worktree_id: str) -> WorktreeInfo:
        with self._lock:
            return self._load(worktree_id)

    def list(self) -> list[WorktreeInfo]:
        with self._lock:
            self.reconcile()
            if not self.state_dir.exists():
                return []
            return [
                self._load(path.stem)
                for path in sorted(self.state_dir.glob("wt_*.json"))
                if path.is_file()
            ]

    def diff(self, worktree_id: str) -> dict[str, Any]:
        with self._lock:
            info = self._load(worktree_id)
            path = self._active_path(info)
            self._stage_worktree(path)
            patch = self._git(
                [
                    "diff",
                    "--cached",
                    "--binary",
                    info.base_commit,
                    "--",
                    ".",
                    RUNTIME_PATHSPEC,
                ],
                cwd=path,
            )
            changed_files = [
                line
                for line in self._git(
                    [
                        "diff",
                        "--cached",
                        "--name-only",
                        info.base_commit,
                        "--",
                        ".",
                        RUNTIME_PATHSPEC,
                    ],
                    cwd=path,
                ).splitlines()
                if line
            ]
            patch_path = self._patch_path(info.id)
            if patch:
                self.state_dir.mkdir(parents=True, exist_ok=True)
                patch_path.write_text(patch, encoding="utf-8")
                if info.status == "active":
                    info.status = "ready"
                    info.updated_at = _now()
                    self._save(info)
            else:
                patch_path.unlink(missing_ok=True)

            return {
                "worktree": info.to_dict(),
                "changed_files": changed_files,
                "change_count": len(changed_files),
                "diff": _preview(patch, self.diff_preview_chars),
                "diff_path": str(patch_path) if patch else None,
                "diff_truncated": len(patch) > self.diff_preview_chars,
            }

    def finish(self, worktree_id: str) -> dict[str, Any]:
        with self._lock:
            review = self.diff(worktree_id)
            if review["change_count"]:
                record_trace(
                    category="worktree",
                    name="worktree.ready",
                    phase="completed",
                    correlation_id=worktree_id,
                    data={
                        "changed_files": review["changed_files"],
                        "diff_path": review["diff_path"],
                    },
                )
                return review

            info = review["worktree"]
            self.remove(worktree_id)
            return {
                "worktree": {
                    **info,
                    "status": "cleaned",
                },
                "changed_files": [],
                "change_count": 0,
                "diff": "",
                "diff_path": None,
                "diff_truncated": False,
            }

    def apply(self, worktree_id: str) -> dict[str, Any]:
        with self._lock:
            info = self._load(worktree_id)
            current_head = self._git(["rev-parse", "HEAD"]).strip()
            if current_head != info.base_commit:
                raise WorktreeError(
                    "Main HEAD changed after the worktree was created; refusing "
                    "to apply a stale patch."
                )

            review = self.diff(worktree_id)
            patch_path = review["diff_path"]
            if not patch_path:
                raise WorktreeError(f"Worktree has no changes to apply: {worktree_id}")
            patch = Path(patch_path).read_text(encoding="utf-8")
            try:
                self._git(["apply", "--check", "--binary", "-"], input_text=patch)
                self._git(["apply", "--binary", "-"], input_text=patch)
            except WorktreeError as exc:
                raise WorktreeError(
                    f"Worktree patch conflicts with the main workspace: {exc}"
                ) from exc

            info.status = "applied"
            info.updated_at = _now()
            self._save(info)
            record_trace(
                category="worktree",
                name="worktree.applied",
                phase="completed",
                correlation_id=info.id,
                data={
                    "changed_files": review["changed_files"],
                    "base_commit": info.base_commit,
                },
            )
            return {
                "worktree": info.to_dict(),
                "applied_files": review["changed_files"],
                "change_count": review["change_count"],
            }

    def remove(
        self,
        worktree_id: str,
        *,
        discard_changes: bool = False,
    ) -> dict[str, Any]:
        with self._lock:
            info = self._load(worktree_id)
            path = Path(info.path).resolve()
            has_changes = self._has_changes(info) if path.exists() else False
            if has_changes and info.status != "applied" and not discard_changes:
                raise WorktreeError(
                    "Worktree has unapplied changes; apply them first or set "
                    "discard_changes=true."
                )

            if path.exists():
                self._git(
                    ["worktree", "remove", "--force", str(path)],
                )
            else:
                self._git(["worktree", "prune"])
            self._git(["branch", "-D", info.branch], check=False)
            self._metadata_path(info.id).unlink(missing_ok=True)
            self._patch_path(info.id).unlink(missing_ok=True)
            record_trace(
                category="worktree",
                name="worktree.removed",
                phase="completed",
                correlation_id=info.id,
                data={
                    "discarded": bool(has_changes and info.status != "applied"),
                },
            )
            return {
                "worktree_id": info.id,
                "removed": True,
                "discarded": bool(has_changes and info.status != "applied"),
            }

    def reconcile(self) -> list[str]:
        with self._lock:
            if not self.state_dir.exists():
                return []
            missing = []
            registered = self._registered_worktree_paths()
            for path in sorted(self.state_dir.glob("wt_*.json")):
                info = self._load(path.stem)
                worktree_path = Path(info.path).resolve()
                if worktree_path not in registered or not worktree_path.exists():
                    if info.status != "missing":
                        info.status = "missing"
                        info.updated_at = _now()
                        self._save(info)
                        missing.append(info.id)
            return missing

    def _has_changes(self, info: WorktreeInfo) -> bool:
        path = self._active_path(info)
        status = self._git(
            [
                "status",
                "--porcelain",
                "--untracked-files=all",
                "--",
                ".",
                RUNTIME_PATHSPEC,
            ],
            cwd=path,
        ).strip()
        commits = int(
            self._git(
                ["rev-list", "--count", f"{info.base_commit}..HEAD"],
                cwd=path,
            ).strip()
            or "0"
        )
        return bool(status) or commits > 0

    def _stage_worktree(self, path: Path) -> None:
        self._git(
            ["add", "-A", "--", ".", RUNTIME_PATHSPEC],
            cwd=path,
        )

    def _active_path(self, info: WorktreeInfo) -> Path:
        path = Path(info.path).resolve()
        if not path.is_relative_to(self.worktrees_dir.resolve()):
            raise WorktreeError(f"Worktree path escapes managed root: {path}")
        if info.status == "missing" or not path.exists():
            raise WorktreeError(f"Worktree directory is missing: {info.id}")
        return path

    def _ensure_repository(self) -> None:
        root = Path(self._git(["rev-parse", "--show-toplevel"]).strip()).resolve()
        if root != self.workdir:
            raise WorktreeError(
                f"WorktreeManager requires the Git repository root: {root}"
            )
        self._git(["rev-parse", "--verify", "HEAD"])

    def _registered_worktree_paths(self) -> set[Path]:
        try:
            output = self._git(["worktree", "list", "--porcelain"])
        except WorktreeError:
            return set()
        paths = set()
        for line in output.splitlines():
            if line.startswith("worktree "):
                paths.add(Path(line.removeprefix("worktree ")).resolve())
        return paths

    def _save(self, info: WorktreeInfo) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        path = self._metadata_path(info.id)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(info.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(path)

    def _load(self, worktree_id: str) -> WorktreeInfo:
        path = self._metadata_path(worktree_id)
        if not path.exists():
            raise WorktreeNotFoundError(f"Managed worktree not found: {worktree_id}")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise WorktreeError(
                f"Worktree metadata is not a JSON object: {worktree_id}"
            )
        info = WorktreeInfo.from_dict(data)
        if info.id != worktree_id:
            raise WorktreeError(f"Worktree metadata id mismatch: {worktree_id}")
        return info

    def _metadata_path(self, worktree_id: str) -> Path:
        _validate_worktree_id(worktree_id)
        return self.state_dir / f"{worktree_id}.json"

    def _patch_path(self, worktree_id: str) -> Path:
        _validate_worktree_id(worktree_id)
        return self.state_dir / f"{worktree_id}.patch"

    def _git(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        check: bool = True,
    ) -> str:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=cwd or self.workdir,
                input=input_text,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            raise WorktreeError(f"Git command failed to start: {exc}") from exc
        if check and result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise WorktreeError(detail or f"Git exited with {result.returncode}")
        return result.stdout


def _validate_worktree_id(worktree_id: str) -> None:
    if not WORKTREE_ID_PATTERN.fullmatch(worktree_id):
        raise WorktreeError(f"Invalid managed worktree id: {worktree_id}")


def _preview(content: str, limit: int) -> str:
    if len(content) <= limit:
        return content
    return f"{content[:limit]}\n... (diff truncated)"


def _now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)


__all__ = [
    "WorktreeError",
    "WorktreeInfo",
    "WorktreeManager",
    "WorktreeNotFoundError",
    "WorktreeStatus",
]
