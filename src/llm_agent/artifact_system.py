from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Literal

from llm_agent.context_manager import PromptSection
from llm_agent.trace_system import record_trace


ArtifactKind = Literal[
    "prd",
    "task_spec",
    "implementation_report",
    "test_report",
    "acceptance_report",
    "note",
]
ArtifactStatus = Literal["draft", "ready", "accepted", "superseded", "archived"]

ARTIFACT_KINDS: set[str] = {
    "prd",
    "task_spec",
    "implementation_report",
    "test_report",
    "acceptance_report",
    "note",
}
ARTIFACT_STATUSES: set[str] = {
    "draft",
    "ready",
    "accepted",
    "superseded",
    "archived",
}

ARTIFACT_POLICY_INSTRUCTIONS = """
Artifacts are durable handoff documents for multi-agent coding work.
- Use artifact_create for PRDs, task specs, implementation reports, test
  reports, acceptance reports, and important project notes.
- Create one artifact per artifact_create call; do not pass an array of
  artifacts as tool input.
- Link artifacts to task_id when they describe or verify a task.
- Before updating an artifact, read it with artifact_get and pass
  expected_version to artifact_update.
- Do not store secrets, credentials, or raw private environment values in
  artifacts.
- Prefer tasks for work status; prefer artifacts for structured deliverables and
  review evidence.
""".strip()


class ArtifactSystemError(RuntimeError):
    """Raised when the artifact system cannot complete an operation."""


class ArtifactNotFoundError(ArtifactSystemError):
    """Raised when an artifact id does not exist."""


@dataclass
class Artifact:
    id: str
    kind: ArtifactKind
    title: str
    content: str
    status: ArtifactStatus = "draft"
    task_id: str | None = None
    owner: str | None = None
    version: int = 1
    created_at: str = ""
    updated_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Artifact":
        return cls(
            id=str(data["id"]),
            kind=_validate_kind(data.get("kind", "note")),
            title=str(data["title"]),
            content=str(data.get("content", "")),
            status=_validate_status(data.get("status", "draft")),
            task_id=_optional_str(data.get("task_id")),
            owner=_optional_str(data.get("owner")),
            version=int(data.get("version", 1)),
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
            metadata=dict(data.get("metadata") or {}),
            history=_normalize_history(data.get("history", [])),
        )


@dataclass
class ArtifactStore:
    artifacts_dir: Path
    collection_id: str = "default"

    def __post_init__(self) -> None:
        self.artifacts_dir = Path(self.artifacts_dir).resolve()
        self.collection_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def for_workdir(
        cls,
        workdir: Path | str | None = None,
        *,
        collection_id: str = "default",
    ) -> "ArtifactStore":
        root = Path.cwd() if workdir is None else Path(workdir)
        return cls(
            root / ".llm_agent" / "artifacts",
            collection_id=collection_id,
        )

    @property
    def collection_dir(self) -> Path:
        return self.artifacts_dir / self.collection_id

    def next_id(self) -> str:
        marker = self.collection_dir / ".highwatermark"
        last_id = (
            int(marker.read_text(encoding="utf-8").strip() or "0")
            if marker.exists()
            else 0
        )
        next_id = last_id + 1
        marker.write_text(str(next_id), encoding="utf-8")
        return f"artifact_{next_id:04d}"

    def save(self, artifact: Artifact) -> None:
        self.collection_dir.mkdir(parents=True, exist_ok=True)
        tmp_path = self._artifact_path(artifact.id).with_suffix(".tmp")
        tmp_path.write_text(
            json.dumps(artifact.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp_path.replace(self._artifact_path(artifact.id))

    def load(self, artifact_id: str) -> Artifact:
        path = self._artifact_path(artifact_id)
        if not path.exists():
            raise ArtifactNotFoundError(f"Artifact not found: {artifact_id}")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ArtifactSystemError(
                f"Artifact file is not a JSON object: {artifact_id}"
            )
        return Artifact.from_dict(data)

    def list(self) -> list[Artifact]:
        return [
            self.load(path.stem)
            for path in sorted(self.collection_dir.glob("artifact_*.json"))
            if path.is_file()
        ]

    def _artifact_path(self, artifact_id: str) -> Path:
        if (
            "/" in artifact_id
            or "\\" in artifact_id
            or artifact_id.startswith(".")
            or not artifact_id.startswith("artifact_")
        ):
            raise ArtifactSystemError(f"Invalid artifact id: {artifact_id}")
        return self.collection_dir / f"{artifact_id}.json"


@dataclass
class ArtifactManager:
    store: ArtifactStore

    @classmethod
    def for_workdir(
        cls,
        workdir: Path | str | None = None,
        *,
        collection_id: str = "default",
    ) -> "ArtifactManager":
        return cls(
            ArtifactStore.for_workdir(workdir, collection_id=collection_id)
        )

    def create_artifact(
        self,
        *,
        kind: ArtifactKind | str,
        title: str,
        content: str,
        status: ArtifactStatus | str = "draft",
        task_id: str | None = None,
        owner: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Artifact:
        now = _now()
        artifact = Artifact(
            id=self.store.next_id(),
            kind=_validate_kind(kind),
            title=_validate_title(title),
            content=str(content),
            status=_validate_status(status),
            task_id=_optional_str(task_id),
            owner=_optional_str(owner),
            version=1,
            created_at=now,
            updated_at=now,
            metadata=dict(metadata or {}),
            history=[
                {
                    "action": "created",
                    "version": 1,
                    "timestamp": now,
                    "summary": "Initial artifact created.",
                }
            ],
        )
        self.store.save(artifact)
        _record_artifact_trace("artifact.created", artifact)
        return artifact

    def get_artifact(self, artifact_id: str) -> Artifact:
        return self.store.load(artifact_id)

    def list_artifacts(
        self,
        *,
        kind: ArtifactKind | str | None = None,
        status: ArtifactStatus | str | None = None,
        task_id: str | None = None,
        owner: str | None = None,
        include_archived: bool = False,
        limit: int | None = None,
    ) -> list[Artifact]:
        artifacts = self.store.list()
        if kind is not None:
            resolved_kind = _validate_kind(kind)
            artifacts = [artifact for artifact in artifacts if artifact.kind == resolved_kind]
        if status is not None:
            resolved_status = _validate_status(status)
            artifacts = [
                artifact for artifact in artifacts if artifact.status == resolved_status
            ]
        elif not include_archived:
            artifacts = [
                artifact for artifact in artifacts if artifact.status != "archived"
            ]
        if task_id is not None:
            artifacts = [artifact for artifact in artifacts if artifact.task_id == task_id]
        if owner is not None:
            artifacts = [artifact for artifact in artifacts if artifact.owner == owner]
        artifacts = sorted(
            artifacts,
            key=lambda artifact: (
                artifact.updated_at,
                artifact.created_at,
                artifact.id,
            ),
            reverse=True,
        )
        if limit is not None:
            if limit <= 0:
                raise ArtifactSystemError("limit must be greater than zero.")
            artifacts = artifacts[:limit]
        return artifacts

    def update_artifact(
        self,
        artifact_id: str,
        *,
        title: str | None = None,
        content: str | None = None,
        status: ArtifactStatus | str | None = None,
        task_id: str | None = None,
        owner: str | None = None,
        metadata: dict[str, Any] | None = None,
        expected_version: int | None = None,
        change_summary: str | None = None,
    ) -> Artifact:
        artifact = self.store.load(artifact_id)
        if expected_version is not None and artifact.version != expected_version:
            raise ArtifactSystemError(
                f"Artifact version conflict: {artifact_id} is v{artifact.version}, "
                f"expected v{expected_version}."
            )

        changed_fields: list[str] = []
        if title is not None:
            artifact.title = _validate_title(title)
            changed_fields.append("title")
        if content is not None:
            artifact.content = str(content)
            changed_fields.append("content")
        if status is not None:
            artifact.status = _validate_status(status)
            changed_fields.append("status")
        if task_id is not None:
            artifact.task_id = _optional_str(task_id)
            changed_fields.append("task_id")
        if owner is not None:
            artifact.owner = _optional_str(owner)
            changed_fields.append("owner")
        if metadata is not None:
            artifact.metadata.update(metadata)
            changed_fields.append("metadata")

        if not changed_fields:
            raise ArtifactSystemError("No artifact fields were provided to update.")

        artifact.version += 1
        artifact.updated_at = _now()
        artifact.history.append(
            {
                "action": "updated",
                "version": artifact.version,
                "timestamp": artifact.updated_at,
                "fields": changed_fields,
                "summary": (change_summary or "").strip(),
            }
        )
        self.store.save(artifact)
        _record_artifact_trace(
            "artifact.updated",
            artifact,
            {
                "changed_fields": changed_fields,
                "change_summary": change_summary or "",
            },
        )
        return artifact


def build_artifact_policy_section(*, priority: int = 34) -> PromptSection:
    return PromptSection(
        name="artifact_policy",
        content=ARTIFACT_POLICY_INSTRUCTIONS,
        priority=priority,
    )


def format_artifact(artifact: Artifact, *, include_content: bool = True) -> str:
    lines = [
        (
            f"{artifact.id} [{artifact.kind}] v{artifact.version} "
            f"status={artifact.status}"
        ),
        f"Title: {artifact.title}",
    ]
    if artifact.task_id:
        lines.append(f"Task: {artifact.task_id}")
    if artifact.owner:
        lines.append(f"Owner: {artifact.owner}")
    lines.append(f"Updated: {artifact.updated_at}")
    if artifact.metadata:
        lines.append(f"Metadata: {json.dumps(artifact.metadata, ensure_ascii=False)}")
    if include_content:
        lines.extend(["", artifact.content])
    return "\n".join(lines)


def format_artifact_list(artifacts: list[Artifact]) -> str:
    if not artifacts:
        return "No artifacts."
    return "\n".join(
        (
            f"- {artifact.id} [{artifact.kind}] v{artifact.version} "
            f"{artifact.status}: {artifact.title}"
            + (f" (task: {artifact.task_id})" if artifact.task_id else "")
        )
        for artifact in artifacts
    )


def format_artifact_context(
    artifacts: list[Artifact],
    *,
    max_content_chars: int = 3_000,
) -> str:
    if not artifacts:
        return ""
    parts = ["<relevant_artifacts>"]
    for artifact in artifacts:
        content = _preview(artifact.content, max_content_chars)
        parts.append(
            (
                f'<artifact id="{artifact.id}" kind="{artifact.kind}" '
                f'version="{artifact.version}" status="{artifact.status}">\n'
                f"Title: {artifact.title}\n"
                f"Task: {artifact.task_id or ''}\n"
                f"Owner: {artifact.owner or ''}\n"
                f"Updated: {artifact.updated_at}\n\n"
                f"{content}\n"
                "</artifact>"
            )
        )
    parts.append("</relevant_artifacts>")
    return "\n\n".join(parts)


def _record_artifact_trace(
    name: str,
    artifact: Artifact,
    extra: dict[str, Any] | None = None,
) -> None:
    data = {
        "artifact_id": artifact.id,
        "kind": artifact.kind,
        "title": artifact.title,
        "task_id": artifact.task_id,
        "version": artifact.version,
        "status": artifact.status,
    }
    if extra:
        data.update(extra)
    record_trace(
        category="artifact",
        name=name,
        phase="completed",
        status="ok",
        correlation_id=artifact.id,
        data=data,
    )


def _validate_kind(value: Any) -> ArtifactKind:
    kind = str(value)
    if kind not in ARTIFACT_KINDS:
        raise ArtifactSystemError(f"Invalid artifact kind: {kind}")
    return kind  # type: ignore[return-value]


def _validate_status(value: Any) -> ArtifactStatus:
    status = str(value)
    if status not in ARTIFACT_STATUSES:
        raise ArtifactSystemError(f"Invalid artifact status: {status}")
    return status  # type: ignore[return-value]


def _validate_title(value: str) -> str:
    title = value.strip()
    if not title:
        raise ArtifactSystemError("Artifact title is required.")
    return title


def _normalize_history(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _preview(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    return f"{text[:limit]}\n... ({omitted} chars omitted; use artifact_get)"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


__all__ = [
    "ARTIFACT_KINDS",
    "ARTIFACT_POLICY_INSTRUCTIONS",
    "ARTIFACT_STATUSES",
    "Artifact",
    "ArtifactKind",
    "ArtifactManager",
    "ArtifactNotFoundError",
    "ArtifactStatus",
    "ArtifactStore",
    "ArtifactSystemError",
    "build_artifact_policy_section",
    "format_artifact",
    "format_artifact_context",
    "format_artifact_list",
]
