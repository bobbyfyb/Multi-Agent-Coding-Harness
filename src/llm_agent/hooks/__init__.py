from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Literal


if TYPE_CHECKING:
    from llm_agent.memory_system import MemoryManager


HookEvent = Literal["UserPromptSubmit", "BeforeLLM", "PreToolUse", "PostToolUse", "Stop"]
HookAction = Literal["allow", "deny", "replace"]
HookCallback = Callable[..., "HookResult | None"]


@dataclass
class HookContext:
    messages: list[dict[str, Any]]
    step: int = 0
    workdir: Path | str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        workdir = Path.cwd() if self.workdir is None else Path(self.workdir)
        self.workdir = workdir.resolve()


@dataclass(frozen=True)
class HookResult:
    action: HookAction
    reason: str | None = None
    value: Any = None
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def allow(
        cls,
        reason: str | None = None,
        *,
        data: dict[str, Any] | None = None,
    ) -> "HookResult":
        return cls(action="allow", reason=reason, data=data or {})

    @classmethod
    def deny(
        cls,
        reason: str,
        *,
        value: Any | None = None,
        data: dict[str, Any] | None = None,
    ) -> "HookResult":
        if value is None:
            value = {"ok": False, "error": f"Permission denied: {reason}"}
        return cls(action="deny", reason=reason, value=value, data=data or {})

    @classmethod
    def replace_result(
        cls,
        value: Any,
        *,
        reason: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> "HookResult":
        return cls(action="replace", reason=reason, value=value, data=data or {})

    @property
    def denied(self) -> bool:
        return self.action == "deny"

    @property
    def replaces_result(self) -> bool:
        return self.action == "replace"


def _default_hooks() -> dict[str, list[HookCallback]]:
    return {
        "UserPromptSubmit": [],
        "BeforeLLM": [],
        "PreToolUse": [],
        "PostToolUse": [],
        "Stop": [],
    }


@dataclass
class HookManager:
    hooks: dict[str, list[HookCallback]] = field(default_factory=_default_hooks)

    def register_hook(self, event: str, callback: HookCallback) -> None:
        self.hooks.setdefault(event, []).append(callback)

    def trigger_hooks(self, event: str, *args: Any) -> HookResult | None:
        effective_result: HookResult | None = None
        for callback in self.hooks.get(event, []):
            result = callback(*args)
            if result is None:
                continue
            if not isinstance(result, HookResult):
                raise TypeError(
                    f"Hook {callback!r} returned {type(result).__name__}; "
                    "expected HookResult or None."
                )
            if result.denied:
                return result
            if effective_result is None or result.replaces_result:
                effective_result = result
            elif effective_result.action == "allow" and result.action == "allow":
                effective_result = _merge_allow_results(
                    effective_result,
                    result,
                )
        return effective_result

    def register(self, event: str, callback: HookCallback) -> None:
        self.register_hook(event, callback)

    def trigger(self, event: str, *args: Any) -> HookResult | None:
        return self.trigger_hooks(event, *args)


def build_default_hook_manager(
    *,
    workdir: Path | str | None = None,
    approval_provider: Any | None = None,
    llm: Any | None = None,
    memory_manager: "MemoryManager | None" = None,
) -> HookManager:
    from llm_agent.hooks.memory_hooks import (
        MemoryContextHook,
        MemoryExtractionHook,
    )
    from llm_agent.hooks.permission_hooks import PermissionHook
    from llm_agent.hooks.task_hooks import TaskPlanningHook
    from llm_agent.task_intent import TaskIntentClassifier

    manager = HookManager()
    intent_classifier = TaskIntentClassifier(llm) if llm is not None else None
    manager.register_hook(
        "BeforeLLM",
        TaskPlanningHook(
            workdir=workdir,
            intent_classifier=intent_classifier,
        ),
    )
    if memory_manager is not None:
        manager.register_hook(
            "BeforeLLM",
            MemoryContextHook(memory_manager),
        )
        manager.register_hook(
            "Stop",
            MemoryExtractionHook(memory_manager),
        )
    permission_kwargs = {"workdir": workdir}
    if approval_provider is not None:
        permission_kwargs["approval_provider"] = approval_provider
    manager.register_hook(
        "PreToolUse",
        PermissionHook(**permission_kwargs),
    )
    return manager


def _merge_allow_results(
    first: HookResult,
    second: HookResult,
) -> HookResult:
    data = dict(first.data)
    for key, value in second.data.items():
        if (
            key in data
            and isinstance(data[key], list)
            and isinstance(value, list)
        ):
            data[key] = [*data[key], *value]
        else:
            data[key] = value
    return HookResult.allow(
        reason=second.reason or first.reason,
        data=data,
    )


__all__ = [
    "HookAction",
    "HookCallback",
    "HookContext",
    "HookEvent",
    "HookManager",
    "HookResult",
    "build_default_hook_manager",
]
