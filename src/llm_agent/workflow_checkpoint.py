from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
import math
import re
from typing import Any, Mapping, Sequence


CHECKPOINT_SCHEMA_VERSION = 1

MAX_ID_CHARS = 300
MAX_STATUS_CHARS = 100
MAX_CONTINUATION_REASON_CHARS = 1_000
MAX_OBJECTIVE_CHARS = 2_000
MAX_SUMMARY_CHARS = 2_000
MAX_SEMANTIC_ITEMS = 8
MAX_SEMANTIC_ITEM_CHARS = 600
MAX_CHANGED_FILES = 40
MAX_PATH_CHARS = 500
MAX_OPEN_TASKS = 20
MAX_VERIFICATION_ITEMS = 12
MAX_TOOL_COUNTS = 32
MAX_RECENT_ACTIONS = 12
MAX_RECENT_FAILURES = 6
MAX_ARTIFACT_IDS = 24

MAX_OBJECT_FIELDS = 24
MAX_NESTED_ITEMS = 20
MAX_NESTING_DEPTH = 3
MAX_VALUE_CHARS = 1_000
MAX_TASK_DESCRIPTION_CHARS = 800

_OPEN_TASK_STATUSES = {"pending", "in_progress", "blocked"}
_NON_BLOCKING_CONTINUATION_REASONS = {
    "incomplete",
    "interrupted",
    "max_steps",
    "phase_retry",
}
_SEMANTIC_FIELDS = (
    "completed",
    "remaining",
    "next_actions",
    "decisions",
    "blockers",
)
_SEMANTIC_ALIASES = {
    "completed": "completed",
    "completed work": "completed",
    "done": "completed",
    "remaining": "remaining",
    "remaining work": "remaining",
    "open work": "remaining",
    "next": "next_actions",
    "next action": "next_actions",
    "next actions": "next_actions",
    "next step": "next_actions",
    "next steps": "next_actions",
    "decisions": "decisions",
    "decision": "decisions",
    "blocker": "blockers",
    "blockers": "blockers",
}
_SEMANTIC_INPUT_ALIASES = {
    "completed": ("completed", "completed_work", "done"),
    "remaining": ("remaining", "remaining_work", "open_work"),
    "next_actions": ("next_actions", "next_steps"),
    "decisions": ("decisions",),
    "blockers": ("blockers",),
}
_LIST_PREFIX = re.compile(r"^(?:[-*+•]|\d+[.)]|\[[ xX]\])\s+")


@dataclass(slots=True)
class AttemptCheckpoint:
    """Bounded, deterministic working state for continuing a workflow attempt."""

    phase_key: str
    attempt: int
    run_id: str
    agent_status: str
    continuation_reason: str
    objective: str
    summary: str
    completed: list[str]
    remaining: list[str]
    next_actions: list[str]
    decisions: list[str]
    blockers: list[str]
    changed_files: list[str]
    open_tasks: list[dict[str, Any]]
    verification: list[dict[str, Any]]
    tool_counts: dict[str, int]
    recent_actions: list[dict[str, Any]]
    recent_failures: list[dict[str, Any]]
    artifact_ids: list[str]
    worktree_diff_sha256: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "phase_key": self.phase_key,
            "attempt": self.attempt,
            "run_id": self.run_id,
            "agent_status": self.agent_status,
            "continuation_reason": self.continuation_reason,
            "objective": self.objective,
            "summary": self.summary,
            "completed": list(self.completed),
            "remaining": list(self.remaining),
            "next_actions": list(self.next_actions),
            "decisions": list(self.decisions),
            "blockers": list(self.blockers),
            "changed_files": list(self.changed_files),
            "open_tasks": [dict(task) for task in self.open_tasks],
            "verification": [dict(item) for item in self.verification],
            "tool_counts": dict(self.tool_counts),
            "recent_actions": [dict(item) for item in self.recent_actions],
            "recent_failures": [dict(item) for item in self.recent_failures],
            "artifact_ids": list(self.artifact_ids),
            "worktree_diff_sha256": self.worktree_diff_sha256,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> AttemptCheckpoint:
        try:
            version = int(data.get("schema_version", CHECKPOINT_SCHEMA_VERSION))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("Invalid attempt checkpoint schema version") from exc
        if version != CHECKPOINT_SCHEMA_VERSION:
            raise ValueError(f"Unsupported attempt checkpoint schema: {version}")

        return build_attempt_checkpoint(
            phase_key=data.get("phase_key", ""),
            attempt=data.get("attempt", 0),
            run_id=data.get("run_id", ""),
            agent_status=data.get("agent_status", ""),
            continuation_reason=data.get("continuation_reason", ""),
            objective=data.get("objective", ""),
            summary=data.get("summary", ""),
            semantic={field: data.get(field, []) for field in _SEMANTIC_FIELDS},
            attempt_state={
                "tool_counts": data.get("tool_counts", {}),
                "recent_actions": data.get("recent_actions", []),
                "recent_failures": data.get("recent_failures", []),
            },
            verification=data.get("verification", []),
            worktree_snapshot={
                "changed_files": data.get("changed_files", []),
                "diff_sha256": data.get("worktree_diff_sha256"),
            },
            task_snapshot=data.get("open_tasks", []),
            artifact_ids=data.get("artifact_ids", []),
        )


def build_attempt_checkpoint(
    *,
    phase_key: Any,
    attempt: Any,
    run_id: Any,
    agent_status: Any,
    continuation_reason: Any,
    objective: Any = "",
    summary: Any = "",
    attempt_state: Mapping[str, Any] | None = None,
    verification: Any = None,
    worktree_snapshot: Mapping[str, Any] | None = None,
    task_snapshot: Any = None,
    artifact_ids: Any = None,
    semantic: Mapping[str, Any] | None = None,
) -> AttemptCheckpoint:
    """Build a bounded checkpoint from deterministic workflow state.

    ``semantic`` may provide the five semantic list fields directly. Missing
    fields are recovered from labelled sections in ``summary``. When the summary
    is unstructured, a completed run treats it as completed work; other statuses
    retain it as remaining work and a next action.
    """

    normalized_objective = _text(objective, MAX_OBJECTIVE_CHARS)
    normalized_summary = _text(summary, MAX_SUMMARY_CHARS)
    normalized_state = attempt_state if isinstance(attempt_state, Mapping) else {}
    recent_failures = _bounded_object_list(
        normalized_state.get("recent_failures"),
        limit=MAX_RECENT_FAILURES,
        take_latest=True,
    )
    semantic_lists = _build_semantic_lists(
        semantic=semantic,
        objective=normalized_objective,
        summary=normalized_summary,
        agent_status=_text(agent_status, MAX_STATUS_CHARS),
        continuation_reason=_text(
            continuation_reason,
            MAX_CONTINUATION_REASON_CHARS,
        ),
        recent_failures=recent_failures,
    )
    normalized_worktree = (
        worktree_snapshot if isinstance(worktree_snapshot, Mapping) else {}
    )

    return AttemptCheckpoint(
        phase_key=_text(phase_key, MAX_ID_CHARS),
        attempt=_bounded_int(attempt, default=0, minimum=0, maximum=1_000_000),
        run_id=_text(run_id, MAX_ID_CHARS),
        agent_status=_text(agent_status, MAX_STATUS_CHARS),
        continuation_reason=_text(
            continuation_reason,
            MAX_CONTINUATION_REASON_CHARS,
        ),
        objective=normalized_objective,
        summary=normalized_summary,
        completed=semantic_lists["completed"],
        remaining=semantic_lists["remaining"],
        next_actions=semantic_lists["next_actions"],
        decisions=semantic_lists["decisions"],
        blockers=semantic_lists["blockers"],
        changed_files=_bounded_strings(
            normalized_worktree.get("changed_files"),
            limit=MAX_CHANGED_FILES,
            char_limit=MAX_PATH_CHARS,
        ),
        open_tasks=_normalize_open_tasks(task_snapshot),
        verification=_bounded_object_list(
            _unwrap_items(verification, "verification"),
            limit=MAX_VERIFICATION_ITEMS,
            take_latest=True,
        ),
        tool_counts=_normalize_tool_counts(normalized_state.get("tool_counts")),
        recent_actions=_bounded_object_list(
            normalized_state.get("recent_actions"),
            limit=MAX_RECENT_ACTIONS,
            take_latest=True,
        ),
        recent_failures=recent_failures,
        artifact_ids=_bounded_strings(
            artifact_ids,
            limit=MAX_ARTIFACT_IDS,
            char_limit=MAX_ID_CHARS,
        ),
        worktree_diff_sha256=_optional_text(
            normalized_worktree.get(
                "diff_sha256",
                normalized_worktree.get("worktree_diff_sha256"),
            ),
            MAX_ID_CHARS,
        ),
    )


def _build_semantic_lists(
    *,
    semantic: Mapping[str, Any] | None,
    objective: str,
    summary: str,
    agent_status: str,
    continuation_reason: str,
    recent_failures: list[dict[str, Any]],
) -> dict[str, list[str]]:
    explicit = semantic if isinstance(semantic, Mapping) else {}
    result: dict[str, list[str]] = {}
    for field, aliases in _SEMANTIC_INPUT_ALIASES.items():
        value: Any = None
        for alias in aliases:
            if alias in explicit:
                value = explicit[alias]
                break
        result[field] = _bounded_strings(
            value,
            limit=MAX_SEMANTIC_ITEMS,
            char_limit=MAX_SEMANTIC_ITEM_CHARS,
            clean_items=True,
        )

    parsed, found_heading = _parse_semantic_summary(summary)
    for field in _SEMANTIC_FIELDS:
        if not result[field]:
            result[field] = parsed[field]

    if summary and not found_heading:
        fallback = _clean_semantic_item(summary)
        if fallback:
            if agent_status == "completed" and not result["completed"]:
                result["completed"] = [fallback]
            elif agent_status != "completed" and not result["remaining"]:
                result["remaining"] = [fallback]

    if (
        agent_status != "completed"
        and not summary
        and not result["remaining"]
        and not result["next_actions"]
    ):
        fallback = _clean_semantic_item(objective)
        if fallback:
            result["remaining"] = [fallback]
            result["next_actions"] = [fallback]

    if not result["blockers"]:
        result["blockers"] = _failure_blockers(recent_failures)
    if (
        not result["blockers"]
        and continuation_reason
        and continuation_reason.casefold() not in _NON_BLOCKING_CONTINUATION_REASONS
    ):
        result["blockers"] = [_clean_semantic_item(continuation_reason)]

    if agent_status != "completed" and not result["next_actions"]:
        result["next_actions"] = result["remaining"][:MAX_SEMANTIC_ITEMS]
    return result


def _parse_semantic_summary(summary: str) -> tuple[dict[str, list[str]], bool]:
    result = {field: [] for field in _SEMANTIC_FIELDS}
    current: str | None = None
    found_heading = False

    for raw_line in summary.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        heading_text = line.lstrip("#").strip()
        before, separator, after = heading_text.partition(":")
        candidate = _canonical_heading(before if separator else heading_text)
        if candidate is not None:
            current = candidate
            found_heading = True
            if separator and after.strip():
                result[current].extend(
                    _bounded_strings(
                        after,
                        limit=MAX_SEMANTIC_ITEMS - len(result[current]),
                        char_limit=MAX_SEMANTIC_ITEM_CHARS,
                        clean_items=True,
                    )
                )
            continue
        if current is None or len(result[current]) >= MAX_SEMANTIC_ITEMS:
            continue
        item = _clean_semantic_item(line)
        if item and item not in result[current]:
            result[current].append(item)

    return result, found_heading


def _canonical_heading(value: str) -> str | None:
    normalized = " ".join(
        value.casefold().replace("_", " ").replace("-", " ").split()
    )
    return _SEMANTIC_ALIASES.get(normalized)


def _clean_semantic_item(value: Any) -> str:
    text = _text(value, MAX_SEMANTIC_ITEM_CHARS * 2)
    text = _LIST_PREFIX.sub("", text).strip()
    text = " ".join(text.split())
    return text[:MAX_SEMANTIC_ITEM_CHARS]


def _failure_blockers(failures: Sequence[Mapping[str, Any]]) -> list[str]:
    blockers: list[str] = []
    for failure in failures:
        value = next(
            (
                failure.get(key)
                for key in ("error", "diagnostic", "message", "summary")
                if failure.get(key)
            ),
            None,
        )
        item = _clean_semantic_item(value)
        if item and item not in blockers:
            blockers.append(item)
        if len(blockers) >= MAX_SEMANTIC_ITEMS:
            break
    return blockers


def _normalize_open_tasks(snapshot: Any) -> list[dict[str, Any]]:
    items = _unwrap_items(snapshot, "tasks")
    normalized: list[dict[str, Any]] = []
    for raw_task in items:
        task = _as_mapping(raw_task)
        if task is None:
            continue
        status = _text(task.get("status", "pending"), MAX_STATUS_CHARS)
        if status not in _OPEN_TASK_STATUSES:
            continue
        item: dict[str, Any] = {
            "id": _text(task.get("id", ""), MAX_ID_CHARS),
            "title": _text(task.get("title", ""), MAX_VALUE_CHARS),
            "description": _text(
                task.get("description", ""),
                MAX_TASK_DESCRIPTION_CHARS,
            ),
            "status": status,
            "owner": _optional_text(task.get("owner"), MAX_ID_CHARS),
            "blocked_by": _bounded_strings(
                task.get("blocked_by"),
                limit=MAX_NESTED_ITEMS,
                char_limit=MAX_ID_CHARS,
            ),
            "parent_id": _optional_text(task.get("parent_id"), MAX_ID_CHARS),
            "evidence": _optional_text(task.get("evidence"), MAX_VALUE_CHARS),
            "notes": _optional_text(task.get("notes"), MAX_VALUE_CHARS),
            "metadata": _bounded_json(task.get("metadata", {}), depth=0),
        }
        priority = task.get("priority")
        if priority is not None:
            item["priority"] = _bounded_int(
                priority,
                default=100,
                minimum=-1_000_000,
                maximum=1_000_000,
            )
        normalized.append(item)
        if len(normalized) >= MAX_OPEN_TASKS:
            break
    return normalized


def _normalize_tool_counts(value: Any) -> dict[str, int]:
    if not isinstance(value, Mapping):
        return {}
    counts: dict[str, int] = {}
    for raw_name, raw_count in value.items():
        name = _text(raw_name, MAX_STATUS_CHARS)
        if not name or name in counts:
            continue
        counts[name] = _bounded_int(
            raw_count,
            default=0,
            minimum=0,
            maximum=1_000_000,
        )
        if len(counts) >= MAX_TOOL_COUNTS:
            break
    return counts


def _bounded_object_list(
    value: Any,
    *,
    limit: int,
    take_latest: bool,
) -> list[dict[str, Any]]:
    items = list(_sequence(value))
    if take_latest:
        items = items[-limit:]
    else:
        items = items[:limit]
    normalized: list[dict[str, Any]] = []
    for item in items:
        mapping = _as_mapping(item)
        if mapping is None:
            continue
        bounded = _bounded_json(mapping, depth=0)
        if isinstance(bounded, dict):
            normalized.append(bounded)
    return normalized[-limit:] if take_latest else normalized[:limit]


def _bounded_json(value: Any, *, depth: int) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return max(-1_000_000_000, min(value, 1_000_000_000))
    if isinstance(value, float):
        return value if math.isfinite(value) else _text(value, MAX_VALUE_CHARS)
    if isinstance(value, str):
        return value[:MAX_VALUE_CHARS]
    if depth >= MAX_NESTING_DEPTH:
        return "<bounded>"
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, raw_item in value.items():
            key = _text(raw_key, MAX_STATUS_CHARS)
            if not key or key in result:
                continue
            result[key] = _bounded_json(raw_item, depth=depth + 1)
            if len(result) >= MAX_OBJECT_FIELDS:
                break
        return result
    if isinstance(value, (list, tuple)):
        return [
            _bounded_json(item, depth=depth + 1)
            for item in value[:MAX_NESTED_ITEMS]
        ]
    if isinstance(value, (set, frozenset)):
        ordered = sorted(value, key=lambda item: str(item))[:MAX_NESTED_ITEMS]
        return [_bounded_json(item, depth=depth + 1) for item in ordered]
    return _text(value, MAX_VALUE_CHARS)


def _bounded_strings(
    value: Any,
    *,
    limit: int,
    char_limit: int,
    clean_items: bool = False,
) -> list[str]:
    if limit <= 0:
        return []
    values = [value] if isinstance(value, str) else list(_sequence(value))
    result: list[str] = []
    for item in values:
        normalized = (
            _clean_semantic_item(item)
            if clean_items
            else _text(item, char_limit).strip()
        )
        normalized = normalized[:char_limit]
        if not normalized or normalized in result:
            continue
        result.append(normalized)
        if len(result) >= limit:
            break
    return result


def _unwrap_items(value: Any, key: str) -> list[Any]:
    if isinstance(value, Mapping) and key in value:
        return list(_sequence(value.get(key)))
    return list(_sequence(value))


def _sequence(value: Any) -> Sequence[Any]:
    if isinstance(value, (list, tuple)):
        return value
    if isinstance(value, (set, frozenset)):
        return tuple(sorted(value, key=lambda item: str(item)))
    return ()


def _as_mapping(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    if is_dataclass(value) and not isinstance(value, type):
        data = asdict(value)
        return data if isinstance(data, dict) else None
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        data = to_dict()
        return data if isinstance(data, Mapping) else None
    return None


def _optional_text(value: Any, limit: int) -> str | None:
    normalized = _text(value, limit).strip()
    return normalized or None


def _text(value: Any, limit: int) -> str:
    if value is None:
        return ""
    return str(value)[:limit]


def _bounded_int(
    value: Any,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(minimum, min(parsed, maximum))


__all__ = [
    "AttemptCheckpoint",
    "CHECKPOINT_SCHEMA_VERSION",
    "MAX_ARTIFACT_IDS",
    "MAX_CHANGED_FILES",
    "MAX_OBJECTIVE_CHARS",
    "MAX_OPEN_TASKS",
    "MAX_RECENT_ACTIONS",
    "MAX_RECENT_FAILURES",
    "MAX_SEMANTIC_ITEM_CHARS",
    "MAX_SEMANTIC_ITEMS",
    "MAX_SUMMARY_CHARS",
    "MAX_TOOL_COUNTS",
    "MAX_VERIFICATION_ITEMS",
    "build_attempt_checkpoint",
]
