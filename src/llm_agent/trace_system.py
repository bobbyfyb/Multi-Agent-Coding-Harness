from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field, is_dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from threading import RLock
from time import perf_counter
from typing import Any, Iterator, Protocol
from uuid import uuid4


TRACE_SCHEMA_VERSION = 1
SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "access_token",
    "auth_token",
    "authorization",
    "cookie",
    "credential",
    "id_token",
    "password",
    "private_key",
    "refresh_token",
    "secret",
)
SECRET_VALUE_PATTERNS = (
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(
        r"(?i)\b(api[_-]?key|access[_-]?token|password|secret)"
        r"\s*[:=]\s*[^\s,;]+"
    ),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*-----"),
)


class TraceSink(Protocol):
    def record(
        self,
        *,
        category: str,
        name: str,
        phase: str = "event",
        status: str = "ok",
        data: dict[str, Any] | None = None,
        correlation_id: str | None = None,
        duration_ms: float | None = None,
        context: "TraceContext | None" = None,
    ) -> "TraceRecord | None":
        """Persist one trace record."""


@dataclass(frozen=True)
class TraceConfig:
    capture_llm_content: bool = False
    capture_tool_content: bool = True
    max_inline_chars: int = 8_000
    strict: bool = False
    write_markdown: bool = True

    def __post_init__(self) -> None:
        if self.max_inline_chars <= 0:
            raise ValueError("max_inline_chars must be greater than zero.")


@dataclass(frozen=True)
class TraceContext:
    trace_id: str
    run_id: str
    root_run_id: str
    parent_run_id: str | None
    agent_id: str
    depth: int = 0
    step: int = 0
    operation: str = ""
    operation_data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TraceRecord:
    schema_version: int
    trace_id: str
    event_id: str
    sequence: int
    timestamp: str
    run_id: str
    root_run_id: str
    parent_run_id: str | None
    agent_id: str
    depth: int
    step: int
    category: str
    name: str
    phase: str
    status: str
    correlation_id: str | None
    duration_ms: float | None
    data: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TraceRecorder:
    workdir: Path | str
    root_run_id: str
    config: TraceConfig = field(default_factory=TraceConfig)
    trace_id: str = field(default_factory=lambda: f"trace-{uuid4().hex[:16]}")
    _sequence: int = field(default=0, init=False)
    _lock: RLock = field(default_factory=RLock, init=False, repr=False)
    last_error: str | None = field(default=None, init=False)
    trace_dir: Path = field(init=False)
    jsonl_path: Path = field(init=False)
    markdown_path: Path = field(init=False)

    def __post_init__(self) -> None:
        self.workdir = Path(self.workdir).resolve()
        safe_run_id = _safe_component(self.root_run_id)
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.trace_dir = self.workdir / ".llm_agent" / "traces" / date
        self.jsonl_path = self.trace_dir / f"{safe_run_id}.jsonl"
        self.markdown_path = self.trace_dir / f"{safe_run_id}.md"

    @classmethod
    def for_run(
        cls,
        workdir: Path | str | None,
        *,
        run_id: str,
        config: TraceConfig | None = None,
    ) -> "TraceRecorder":
        root = Path.cwd() if workdir is None else Path(workdir)
        return cls(
            workdir=root,
            root_run_id=run_id,
            config=config or TraceConfig(),
        )

    def record(
        self,
        *,
        category: str,
        name: str,
        phase: str = "event",
        status: str = "ok",
        data: dict[str, Any] | None = None,
        correlation_id: str | None = None,
        duration_ms: float | None = None,
        context: TraceContext | None = None,
    ) -> TraceRecord | None:
        resolved_context = context or current_trace_context()
        if resolved_context is None:
            resolved_context = TraceContext(
                trace_id=self.trace_id,
                run_id=self.root_run_id,
                root_run_id=self.root_run_id,
                parent_run_id=None,
                agent_id="agent",
            )

        with self._lock:
            self._sequence += 1
            record = TraceRecord(
                schema_version=TRACE_SCHEMA_VERSION,
                trace_id=self.trace_id,
                event_id=f"evt-{uuid4().hex[:16]}",
                sequence=self._sequence,
                timestamp=_timestamp(),
                run_id=resolved_context.run_id,
                root_run_id=resolved_context.root_run_id,
                parent_run_id=resolved_context.parent_run_id,
                agent_id=resolved_context.agent_id,
                depth=resolved_context.depth,
                step=resolved_context.step,
                category=category,
                name=name,
                phase=phase,
                status=status,
                correlation_id=correlation_id,
                duration_ms=(
                    round(duration_ms, 3) if duration_ms is not None else None
                ),
                data=self._sanitize_data(category, data or {}),
            )
            try:
                self.trace_dir.mkdir(parents=True, exist_ok=True)
                with self.jsonl_path.open("a", encoding="utf-8") as file:
                    file.write(
                        json.dumps(
                            record.to_dict(),
                            ensure_ascii=False,
                            default=str,
                        )
                    )
                    file.write("\n")
                    file.flush()
            except Exception as exc:
                self.last_error = str(exc)
                if self.config.strict:
                    raise
                return None
            return record

    def render_markdown(self) -> Path | None:
        with self._lock:
            if not self.config.write_markdown or not self.jsonl_path.exists():
                return None
            try:
                from llm_agent.trace_render import render_trace_markdown

                return render_trace_markdown(
                    self.jsonl_path,
                    self.markdown_path,
                )
            except Exception as exc:
                self.last_error = str(exc)
                if self.config.strict:
                    raise
                return None

    def _sanitize_data(
        self,
        category: str,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        if category == "tool" and not self.config.capture_tool_content:
            data = {
                key: (
                    summarize_value(value)
                    if key.lower() in {"arguments", "content", "output", "result"}
                    else value
                )
                for key, value in data.items()
            }
        sanitized = _sanitize_value(
            data,
            max_chars=self.config.max_inline_chars,
        )
        return sanitized if isinstance(sanitized, dict) else {"value": sanitized}


_CURRENT_RECORDER: ContextVar[TraceRecorder | None] = ContextVar(
    "llm_agent_trace_recorder",
    default=None,
)
_CURRENT_CONTEXT: ContextVar[TraceContext | None] = ContextVar(
    "llm_agent_trace_context",
    default=None,
)


@contextmanager
def trace_scope(
    recorder: TraceRecorder | None = None,
    *,
    run_id: str,
    agent_id: str,
    parent_run_id: str | None = None,
    depth: int = 0,
    root_run_id: str | None = None,
) -> Iterator[TraceContext | None]:
    active_recorder = recorder or current_trace_recorder()
    parent_context = current_trace_context()
    if active_recorder is None:
        yield None
        return

    resolved_root_run_id = (
        root_run_id
        or (parent_context.root_run_id if parent_context else None)
        or active_recorder.root_run_id
        or run_id
    )
    context = TraceContext(
        trace_id=active_recorder.trace_id,
        run_id=run_id,
        root_run_id=resolved_root_run_id,
        parent_run_id=parent_run_id,
        agent_id=agent_id,
        depth=depth,
    )
    recorder_token = _CURRENT_RECORDER.set(active_recorder)
    context_token = _CURRENT_CONTEXT.set(context)
    try:
        yield context
    finally:
        _CURRENT_CONTEXT.reset(context_token)
        _CURRENT_RECORDER.reset(recorder_token)


@contextmanager
def trace_operation(
    operation: str,
    **operation_data: Any,
) -> Iterator[None]:
    context = current_trace_context()
    if context is None:
        yield
        return
    token = _CURRENT_CONTEXT.set(
        replace(
            context,
            operation=operation,
            operation_data=dict(operation_data),
        )
    )
    try:
        yield
    finally:
        _CURRENT_CONTEXT.reset(token)


def update_trace_context(**changes: Any) -> TraceContext | None:
    context = current_trace_context()
    if context is None:
        return None
    updated = replace(context, **changes)
    _CURRENT_CONTEXT.set(updated)
    return updated


def current_trace_recorder() -> TraceRecorder | None:
    return _CURRENT_RECORDER.get()


def current_trace_context() -> TraceContext | None:
    return _CURRENT_CONTEXT.get()


def record_trace(
    *,
    category: str,
    name: str,
    phase: str = "event",
    status: str = "ok",
    data: dict[str, Any] | None = None,
    correlation_id: str | None = None,
    duration_ms: float | None = None,
) -> TraceRecord | None:
    recorder = current_trace_recorder()
    if recorder is None:
        return None
    return recorder.record(
        category=category,
        name=name,
        phase=phase,
        status=status,
        data=data,
        correlation_id=correlation_id,
        duration_ms=duration_ms,
    )


def record_agent_event(event: Any) -> TraceRecord | None:
    event_type = str(getattr(event, "type", "unknown"))
    category = _agent_event_category(event_type)
    status = "ok"
    if event_type.endswith("_failed") or event_type == "context_compact_failed":
        status = "error"
    elif event_type == "permission_denied":
        status = "denied"
    elif event_type in {"max_steps", "background_cancelled"}:
        status = "warning"
    event_data = dict(getattr(event, "data", {}) or {})
    if event_type == "final" and event_data.get("status") == "incomplete":
        status = "warning"
    result = event_data.get("result")
    if event_type == "tool_result":
        status = _tool_result_status(result)
    duration_ms = getattr(event, "duration_ms", None)
    correlation_id = event_data.get("id")
    context = TraceContext(
        trace_id=(
            current_trace_recorder().trace_id
            if current_trace_recorder() is not None
            else ""
        ),
        run_id=str(getattr(event, "run_id", "")),
        root_run_id=(
            current_trace_context().root_run_id
            if current_trace_context() is not None
            else str(getattr(event, "run_id", ""))
        ),
        parent_run_id=getattr(event, "parent_run_id", None),
        agent_id=str(getattr(event, "agent_id", "agent")),
        depth=int(getattr(event, "depth", 0)),
        step=int(getattr(event, "step", 0)),
    )
    recorder = current_trace_recorder()
    if recorder is None:
        return None
    return recorder.record(
        category=category,
        name=event_type,
        status=status,
        data=event_data,
        correlation_id=str(correlation_id) if correlation_id is not None else None,
        duration_ms=duration_ms if isinstance(duration_ms, (int, float)) else None,
        context=context,
    )


def trace_llm_content_enabled() -> bool:
    recorder = current_trace_recorder()
    return bool(recorder and recorder.config.capture_llm_content)


def summarize_text(value: str) -> dict[str, Any]:
    encoded = value.encode("utf-8")
    return {
        "chars": len(value),
        "bytes": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


def summarize_value(value: Any) -> dict[str, Any]:
    try:
        serialized = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        serialized = str(value)
    summary = summarize_text(serialized)
    summary["type"] = type(value).__name__
    return summary


def elapsed_ms(started_at: float) -> float:
    return (perf_counter() - started_at) * 1_000


def _agent_event_category(event_type: str) -> str:
    if event_type.startswith("tool_"):
        return "tool"
    if event_type.startswith("permission_"):
        return "permission"
    if event_type.startswith("context_"):
        return "context"
    if event_type.startswith("memory_"):
        return "memory"
    if event_type.startswith("task_"):
        return "task"
    if event_type.startswith("subagent_"):
        return "subagent"
    if event_type.startswith("background_"):
        return "background"
    if event_type == "recovery":
        return "recovery"
    return "agent"


def _tool_result_status(result: Any) -> str:
    if not isinstance(result, dict):
        return "ok"
    if result.get("ok") is False:
        return "error"
    payload = result.get("result")
    if not isinstance(payload, dict):
        return "ok"
    if payload.get("status") in {"failed", "timed_out"}:
        return "error"
    if payload.get("outcome") in {"failed", "error", "timed_out"}:
        return "error"
    if payload.get("outcome") in {"issues_found", "no_tests"}:
        return "warning"
    return "ok"


def _sanitize_value(value: Any, *, max_chars: int, depth: int = 0) -> Any:
    if depth > 10:
        return "[MAX_DEPTH]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Exception):
        return {
            "type": type(value).__name__,
            "message": _sanitize_string(str(value), max_chars=max_chars),
        }
    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if _is_sensitive_key(key_text):
                result[key_text] = "[REDACTED]"
            else:
                result[key_text] = _sanitize_value(
                    item,
                    max_chars=max_chars,
                    depth=depth + 1,
                )
        return result
    if isinstance(value, (list, tuple, set)):
        return [
            _sanitize_value(item, max_chars=max_chars, depth=depth + 1)
            for item in value
        ]
    return _sanitize_string(str(value), max_chars=max_chars)


def _sanitize_string(value: str, *, max_chars: int) -> Any:
    sanitized = value
    for pattern in SECRET_VALUE_PATTERNS:
        sanitized = pattern.sub("[REDACTED]", sanitized)
    if len(sanitized) <= max_chars:
        return sanitized
    summary = summarize_text(sanitized)
    return {
        "truncated": True,
        "preview": sanitized[:max_chars],
        **summary,
    }


def _is_sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return any(part in normalized for part in SENSITIVE_KEY_PARTS)


def _safe_component(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value)).strip("._")
    return sanitized[:96] or "unknown"


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "TRACE_SCHEMA_VERSION",
    "TraceConfig",
    "TraceContext",
    "TraceRecord",
    "TraceRecorder",
    "TraceSink",
    "current_trace_context",
    "current_trace_recorder",
    "elapsed_ms",
    "record_agent_event",
    "record_trace",
    "summarize_text",
    "summarize_value",
    "trace_llm_content_enabled",
    "trace_operation",
    "trace_scope",
    "update_trace_context",
]
