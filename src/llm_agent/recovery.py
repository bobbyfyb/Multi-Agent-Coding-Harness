from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Iterator, Literal, Mapping

from llm_agent.trace_system import record_trace


RecoveryKind = Literal[
    "authentication",
    "conflict",
    "connection",
    "context_length",
    "invalid_request",
    "not_found",
    "overloaded",
    "permission",
    "rate_limit",
    "server",
    "timeout",
    "unknown",
]
RecoveryAction = Literal[
    "retry",
    "fallback",
    "context_compact",
    "output_escalate",
    "continuation",
    "exhausted",
]

CONTINUATION_PROMPT = """
<recovery_continuation>
Output token limit reached. Resume directly without apology or recap. Continue
from the point where the previous response stopped and keep the remainder concise.
</recovery_continuation>
""".strip()


@dataclass(frozen=True)
class RecoveryPolicy:
    max_retries: int = 4
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 8.0
    jitter_ratio: float = 0.25
    max_retry_elapsed_seconds: float = 300.0
    fallback_model: str | None = None
    fallback_after_overloads: int = 3
    escalated_max_tokens: int = 8_192
    max_continuations: int = 2

    def __post_init__(self) -> None:
        if self.max_retries < 0:
            raise ValueError("max_retries cannot be negative.")
        if self.base_delay_seconds < 0:
            raise ValueError("base_delay_seconds cannot be negative.")
        if self.max_delay_seconds < 0:
            raise ValueError("max_delay_seconds cannot be negative.")
        if self.jitter_ratio < 0:
            raise ValueError("jitter_ratio cannot be negative.")
        if self.max_retry_elapsed_seconds < 0:
            raise ValueError("max_retry_elapsed_seconds cannot be negative.")
        if self.fallback_after_overloads <= 0:
            raise ValueError("fallback_after_overloads must be greater than zero.")
        if self.escalated_max_tokens <= 0:
            raise ValueError("escalated_max_tokens must be greater than zero.")
        if self.max_continuations < 0:
            raise ValueError("max_continuations cannot be negative.")


@dataclass
class RecoveryState:
    primary_model: str | None = None
    current_model: str | None = None
    consecutive_overloads: int = 0
    transport_retries: int = 0
    fallback_used: bool = False
    output_escalated: bool = False
    output_continuations: int = 0
    partial_outputs: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.current_model is None:
            self.current_model = self.primary_model

    def reset_output(self) -> None:
        self.output_escalated = False
        self.output_continuations = 0
        self.partial_outputs.clear()


@dataclass(frozen=True)
class ClassifiedError:
    kind: RecoveryKind
    retryable: bool
    status_code: int | None = None
    retry_after_seconds: float | None = None


@dataclass(frozen=True)
class RecoveryNotice:
    action: RecoveryAction
    reason: str
    attempt: int | None = None
    max_attempts: int | None = None
    delay_seconds: float | None = None
    model: str | None = None
    from_model: str | None = None
    to_model: str | None = None
    status_code: int | None = None
    call_id: str | None = None
    detail: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        values: dict[str, Any] = {
            "action": self.action,
            "reason": self.reason,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "delay_seconds": self.delay_seconds,
            "model": self.model,
            "from_model": self.from_model,
            "to_model": self.to_model,
            "status_code": self.status_code,
            "call_id": self.call_id,
            "detail": self.detail,
            **self.data,
        }
        return {key: value for key, value in values.items() if value is not None}


RecoveryCallback = Callable[[RecoveryNotice], None]

_CURRENT_STATE: ContextVar[RecoveryState | None] = ContextVar(
    "llm_agent_recovery_state",
    default=None,
)
_CURRENT_CALLBACK: ContextVar[RecoveryCallback | None] = ContextVar(
    "llm_agent_recovery_callback",
    default=None,
)


@contextmanager
def recovery_scope(
    state: RecoveryState,
    callback: RecoveryCallback | None = None,
) -> Iterator[RecoveryState]:
    state_token = _CURRENT_STATE.set(state)
    callback_token = _CURRENT_CALLBACK.set(callback)
    try:
        yield state
    finally:
        _CURRENT_CALLBACK.reset(callback_token)
        _CURRENT_STATE.reset(state_token)


def current_recovery_state() -> RecoveryState | None:
    return _CURRENT_STATE.get()


def emit_recovery_notice(notice: RecoveryNotice) -> None:
    record_trace(
        category="recovery",
        name=f"recovery.{notice.action}",
        status="error" if notice.action == "exhausted" else "warning",
        correlation_id=notice.call_id,
        data=notice.to_dict(),
    )
    callback = _CURRENT_CALLBACK.get()
    if callback is not None:
        callback(notice)


def retry_delay(
    policy: RecoveryPolicy,
    retry_index: int,
    *,
    retry_after_seconds: float | None = None,
    random_value: float = 0.0,
) -> float:
    if retry_after_seconds is not None:
        return min(max(0.0, retry_after_seconds), policy.max_delay_seconds)
    base = min(
        policy.base_delay_seconds * (2**retry_index),
        policy.max_delay_seconds,
    )
    jitter = base * policy.jitter_ratio * min(max(random_value, 0.0), 1.0)
    return base + jitter


def classify_llm_error(exc: BaseException) -> ClassifiedError:
    chain = list(_exception_chain(exc))
    text = " ".join(str(item).lower() for item in chain)
    names = " ".join(type(item).__name__.lower() for item in chain)
    status_code = _status_code(chain)
    retry_after = _retry_after_seconds(chain)
    code = _error_code(chain)

    context_markers = (
        "context_length_exceeded",
        "context length exceeded",
        "maximum context length",
        "max_context_window",
        "prompt_too_long",
        "prompt is too long",
        "too many tokens",
        "request too large",
    )
    if (
        status_code == 413
        or code in {"context_length_exceeded", "prompt_too_long"}
        or any(marker in text for marker in context_markers)
    ):
        return ClassifiedError(
            kind="context_length",
            retryable=False,
            status_code=status_code,
            retry_after_seconds=retry_after,
        )

    if status_code == 429 or "ratelimit" in names or "rate limit" in text:
        return ClassifiedError(
            kind="rate_limit",
            retryable=True,
            status_code=status_code,
            retry_after_seconds=retry_after,
        )
    if (
        status_code == 529
        or "overloaded" in names
        or "overloaded" in text
        or "overload" in text
    ):
        return ClassifiedError(
            kind="overloaded",
            retryable=True,
            status_code=status_code,
            retry_after_seconds=retry_after,
        )
    if status_code == 409:
        return ClassifiedError(
            kind="conflict",
            retryable=True,
            status_code=status_code,
            retry_after_seconds=retry_after,
        )
    if (
        status_code == 408
        or "apitimeouterror" in names
        or any(isinstance(item, TimeoutError) for item in chain)
        or "timed out" in text
    ):
        return ClassifiedError(
            kind="timeout",
            retryable=True,
            status_code=status_code,
            retry_after_seconds=retry_after,
        )
    if (
        "apiconnectionerror" in names
        or any(isinstance(item, OSError) for item in chain)
        or "connection error" in text
        or "connection reset" in text
    ):
        return ClassifiedError(
            kind="connection",
            retryable=True,
            status_code=status_code,
            retry_after_seconds=retry_after,
        )
    if (
        status_code is not None and status_code >= 500
    ) or "internalservererror" in names:
        return ClassifiedError(
            kind="server",
            retryable=True,
            status_code=status_code,
            retry_after_seconds=retry_after,
        )
    if status_code == 401 or "authenticationerror" in names:
        kind: RecoveryKind = "authentication"
    elif status_code == 403 or "permissiondeniederror" in names:
        kind = "permission"
    elif status_code == 404 or "notfounderror" in names:
        kind = "not_found"
    elif (
        status_code is not None and 400 <= status_code < 500
    ) or "badrequesterror" in names:
        kind = "invalid_request"
    else:
        kind = "unknown"
    return ClassifiedError(
        kind=kind,
        retryable=False,
        status_code=status_code,
        retry_after_seconds=retry_after,
    )


def _exception_chain(exc: BaseException) -> Iterator[BaseException]:
    current: BaseException | None = exc
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _status_code(chain: list[BaseException]) -> int | None:
    for exc in chain:
        value = getattr(exc, "status_code", None)
        if isinstance(value, int):
            return value
        response = getattr(exc, "response", None)
        value = getattr(response, "status_code", None)
        if isinstance(value, int):
            return value
    return None


def _error_code(chain: list[BaseException]) -> str:
    for exc in chain:
        code = getattr(exc, "code", None)
        if code is not None:
            return str(code).lower()
        body = getattr(exc, "body", None)
        if isinstance(body, Mapping):
            value = body.get("code")
            if value is None and isinstance(body.get("error"), Mapping):
                value = body["error"].get("code")
            if value is not None:
                return str(value).lower()
    return ""


def _retry_after_seconds(chain: list[BaseException]) -> float | None:
    for exc in chain:
        response = getattr(exc, "response", None)
        headers = getattr(response, "headers", None)
        if not isinstance(headers, Mapping):
            continue
        retry_after_ms = headers.get("retry-after-ms")
        if retry_after_ms is not None:
            try:
                return max(0.0, float(retry_after_ms) / 1_000)
            except (TypeError, ValueError):
                pass
        retry_after = headers.get("retry-after")
        if retry_after is None:
            continue
        try:
            return max(0.0, float(retry_after))
        except (TypeError, ValueError):
            try:
                retry_at = parsedate_to_datetime(str(retry_after))
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                return max(
                    0.0,
                    (retry_at - datetime.now(timezone.utc)).total_seconds(),
                )
            except (TypeError, ValueError, OverflowError):
                continue
    return None


__all__ = [
    "CONTINUATION_PROMPT",
    "ClassifiedError",
    "RecoveryAction",
    "RecoveryCallback",
    "RecoveryKind",
    "RecoveryNotice",
    "RecoveryPolicy",
    "RecoveryState",
    "classify_llm_error",
    "current_recovery_state",
    "emit_recovery_notice",
    "recovery_scope",
    "retry_delay",
]
