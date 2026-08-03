from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from llm_agent.artifact_system import (
    Artifact,
    ArtifactManager,
    format_artifact_context,
)
from llm_agent.hooks import HookContext, HookResult
from llm_agent.task_system import OPEN_TASK_STATUSES, TaskManager


@dataclass(frozen=True)
class ArtifactContextHook:
    manager: ArtifactManager
    max_artifacts: int = 5
    max_content_chars: int = 3_000
    task_list_id: str = "default"
    task_workdir: Path | str | None = None

    def __call__(self, context: HookContext) -> HookResult | None:
        artifacts = self._select_artifacts(context)
        content = format_artifact_context(
            artifacts,
            max_content_chars=self.max_content_chars,
        )
        if not content:
            return None
        return HookResult.allow(
            data={
                "messages": [content],
                "artifact_ids": [artifact.id for artifact in artifacts],
            }
        )

    def _select_artifacts(self, context: HookContext) -> list[Artifact]:
        open_task_ids = self._open_task_ids(context)
        artifacts = self.manager.list_artifacts(include_archived=False)
        ranked = sorted(
            artifacts,
            key=lambda artifact: (
                self._rank(artifact, open_task_ids),
                artifact.updated_at,
                artifact.created_at,
                artifact.id,
            ),
            reverse=True,
        )
        return ranked[: self.max_artifacts]

    def _open_task_ids(self, context: HookContext) -> set[str]:
        workdir = Path(
            context.workdir if self.task_workdir is None else self.task_workdir
        )
        manager = TaskManager.for_workdir(workdir, task_list_id=self.task_list_id)
        return {
            task.id
            for task in manager.list_tasks(include_completed=False)
            if task.status in OPEN_TASK_STATUSES
        }

    @staticmethod
    def _rank(artifact: Artifact, open_task_ids: set[str]) -> int:
        if artifact.task_id in open_task_ids:
            return 3
        if artifact.kind in {"prd", "task_spec"}:
            return 2
        if artifact.status in {"ready", "accepted"}:
            return 1
        return 0


__all__ = ["ArtifactContextHook"]
