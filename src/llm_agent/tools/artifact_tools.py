from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from llm_agent.artifact_system import (
    ARTIFACT_KINDS,
    ARTIFACT_STATUSES,
    ArtifactManager,
    format_artifact,
    format_artifact_list,
)
from llm_agent.tool_registry import ToolDefinition, ToolRegistry


@dataclass(frozen=True)
class ArtifactTools:
    manager: ArtifactManager

    @classmethod
    def for_workdir(
        cls,
        workdir: Path | str | None = None,
        *,
        collection_id: str = "default",
    ) -> "ArtifactTools":
        return cls(
            ArtifactManager.for_workdir(workdir, collection_id=collection_id)
        )

    def artifact_create(
        self,
        kind: str,
        title: str,
        content: str,
        status: str = "draft",
        task_id: str | None = None,
        owner: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        artifact = self.manager.create_artifact(
            kind=kind,
            title=title,
            content=content,
            status=status,
            task_id=task_id,
            owner=owner,
            metadata=metadata,
        )
        return f"Created artifact:\n{format_artifact(artifact)}"

    def artifact_get(self, artifact_id: str) -> str:
        return format_artifact(self.manager.get_artifact(artifact_id))

    def artifact_list(
        self,
        kind: str | None = None,
        status: str | None = None,
        task_id: str | None = None,
        owner: str | None = None,
        include_archived: bool = False,
        limit: int = 20,
    ) -> str:
        artifacts = self.manager.list_artifacts(
            kind=kind,
            status=status,
            task_id=task_id,
            owner=owner,
            include_archived=include_archived,
            limit=limit,
        )
        return format_artifact_list(artifacts)

    def artifact_update(
        self,
        artifact_id: str,
        title: str | None = None,
        content: str | None = None,
        status: str | None = None,
        task_id: str | None = None,
        owner: str | None = None,
        metadata: dict[str, Any] | None = None,
        expected_version: int | None = None,
        change_summary: str | None = None,
    ) -> str:
        artifact = self.manager.update_artifact(
            artifact_id,
            title=title,
            content=content,
            status=status,
            task_id=task_id,
            owner=owner,
            metadata=metadata,
            expected_version=expected_version,
            change_summary=change_summary,
        )
        return f"Updated artifact:\n{format_artifact(artifact)}"


def artifact_tool_definitions(
    manager: ArtifactManager | None = None,
    *,
    workdir: Path | str | None = None,
    collection_id: str = "default",
) -> list[ToolDefinition]:
    tools = (
        ArtifactTools(manager)
        if manager is not None
        else ArtifactTools.for_workdir(workdir, collection_id=collection_id)
    )
    return [
        ToolDefinition(
            name="artifact_create",
            description=(
                "Create a durable structured artifact such as a PRD, task spec, "
                "implementation report, test report, acceptance report, or note."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": sorted(ARTIFACT_KINDS),
                        "description": "Artifact type.",
                    },
                    "title": {"type": "string", "description": "Artifact title."},
                    "content": {
                        "type": "string",
                        "description": "Markdown artifact body.",
                    },
                    "status": {
                        "type": "string",
                        "enum": sorted(ARTIFACT_STATUSES),
                        "description": "Artifact lifecycle status.",
                    },
                    "task_id": {
                        "type": "string",
                        "description": "Optional linked task id.",
                    },
                    "owner": {
                        "type": "string",
                        "description": "Agent or role that owns the artifact.",
                    },
                    "metadata": {"type": "object"},
                },
                "required": ["kind", "title", "content"],
            },
            func=tools.artifact_create,
        ),
        ToolDefinition(
            name="artifact_update",
            description=(
                "Update an existing artifact. Read it first with artifact_get and "
                "pass expected_version to avoid stale overwrites."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "artifact_id": {"type": "string"},
                    "title": {"type": "string"},
                    "content": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": sorted(ARTIFACT_STATUSES),
                    },
                    "task_id": {"type": "string"},
                    "owner": {"type": "string"},
                    "metadata": {"type": "object"},
                    "expected_version": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "Version observed from artifact_get.",
                    },
                    "change_summary": {
                        "type": "string",
                        "description": "Short summary of the update.",
                    },
                },
                "required": ["artifact_id"],
            },
            func=tools.artifact_update,
        ),
        ToolDefinition(
            name="artifact_get",
            description="Read one artifact by id, including full content and version.",
            parameters={
                "type": "object",
                "properties": {"artifact_id": {"type": "string"}},
                "required": ["artifact_id"],
            },
            func=tools.artifact_get,
        ),
        ToolDefinition(
            name="artifact_list",
            description="List durable artifacts with optional filters.",
            parameters={
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": sorted(ARTIFACT_KINDS),
                    },
                    "status": {
                        "type": "string",
                        "enum": sorted(ARTIFACT_STATUSES),
                    },
                    "task_id": {"type": "string"},
                    "owner": {"type": "string"},
                    "include_archived": {
                        "type": "boolean",
                        "description": "Whether archived artifacts should be listed.",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 100,
                    },
                },
            },
            func=tools.artifact_list,
        ),
    ]


def register_tools(
    registry: ToolRegistry,
    *,
    manager: ArtifactManager | None = None,
    workdir: Path | str | None = None,
    collection_id: str = "default",
) -> None:
    registry.register_many(
        artifact_tool_definitions(
            manager=manager,
            workdir=workdir,
            collection_id=collection_id,
        )
    )


__all__ = [
    "ArtifactTools",
    "artifact_tool_definitions",
    "register_tools",
]
