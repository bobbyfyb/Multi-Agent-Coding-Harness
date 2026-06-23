from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from llm_agent.hooks import HookContext, HookResult
from llm_agent.llm_client import LLMToolCall


HARD_DENY_COMMAND_FRAGMENTS = (
    "rm -rf /",
    "sudo",
    "shutdown",
    "reboot",
    "mkfs",
    "dd if=",
    "> /dev/sda",
)

CONFIRM_COMMAND_FRAGMENTS = (
    "rm ",
    "rm\t",
    "chmod ",
    "chown ",
    "> /etc/",
    ">> /etc/",
    "git push",
    "git reset",
    "pip install",
    "uv add",
    "uv remove",
)

PATH_ARGUMENT_TOOLS = {"read_file", "write_file", "edit_file"}
MUTATING_FILE_TOOLS = {"write_file", "edit_file"}


@dataclass(frozen=True)
class ApprovalRequest:
    tool_name: str
    arguments: dict[str, Any]
    reason: str


class ApprovalProvider(Protocol):
    def approve(self, request: ApprovalRequest) -> bool:
        """Return True when the tool call is approved."""


@dataclass(frozen=True)
class CliApprovalProvider:
    input_func: Callable[[str], str] = input
    output_func: Callable[[str], None] = print

    def approve(self, request: ApprovalRequest) -> bool:
        self.output_func("")
        self.output_func(f"Permission required: {request.reason}")
        self.output_func(f"Tool: {request.tool_name}({request.arguments})")
        choice = self.input_func("Allow? [y/N] ").strip().lower()
        return choice in {"y", "yes"}


@dataclass(frozen=True)
class AutoApprovalProvider:
    approved: bool = True

    def approve(self, request: ApprovalRequest) -> bool:
        return self.approved


@dataclass(frozen=True)
class PermissionHook:
    workdir: Path | str | None = None
    approval_provider: ApprovalProvider | None = field(
        default_factory=CliApprovalProvider
    )
    confirm_file_mutations: bool = True
    confirm_destructive_bash: bool = True

    def __call__(
        self,
        tool_call: LLMToolCall,
        context: HookContext,
    ) -> HookResult | None:
        arguments = tool_call.arguments
        workdir = self._workdir(context)

        path_denial = self._check_workspace_path(tool_call, arguments, workdir)
        if path_denial is not None:
            return HookResult.deny(path_denial)

        if tool_call.name == "bash":
            return self._check_bash(tool_call, arguments)

        if tool_call.name in MUTATING_FILE_TOOLS and self.confirm_file_mutations:
            path = str(arguments.get("path", ""))
            reason = f"{tool_call.name} modifies workspace file: {path}"
            return self._ask_for_approval(tool_call, reason)

        return None

    def _workdir(self, context: HookContext) -> Path:
        workdir = context.workdir if self.workdir is None else self.workdir
        return Path(workdir).resolve()

    def _check_workspace_path(
        self,
        tool_call: LLMToolCall,
        arguments: dict[str, Any],
        workdir: Path,
    ) -> str | None:
        if tool_call.name not in PATH_ARGUMENT_TOOLS:
            return None

        path = arguments.get("path")
        if not isinstance(path, str):
            return None

        candidate = (workdir / path).resolve()
        if not candidate.is_relative_to(workdir):
            return f"Path escapes workspace: {path}"
        return None

    def _check_bash(
        self,
        tool_call: LLMToolCall,
        arguments: dict[str, Any],
    ) -> HookResult | None:
        command = str(arguments.get("command", ""))
        for fragment in HARD_DENY_COMMAND_FRAGMENTS:
            if fragment in command:
                return HookResult.deny(
                    f"bash command contains blocked fragment: {fragment}"
                )

        if not self.confirm_destructive_bash:
            return None

        for fragment in CONFIRM_COMMAND_FRAGMENTS:
            if fragment in command:
                return self._ask_for_approval(
                    tool_call,
                    f"bash command may be destructive: {fragment}",
                )
        return None

    def _ask_for_approval(
        self,
        tool_call: LLMToolCall,
        reason: str,
    ) -> HookResult:
        if self.approval_provider is None:
            return HookResult.deny(f"{reason}; no approval provider configured")

        request = ApprovalRequest(
            tool_name=tool_call.name,
            arguments=tool_call.arguments,
            reason=reason,
        )
        if self.approval_provider.approve(request):
            return HookResult.allow(reason=reason, data={"approved": True})
        return HookResult.deny(f"{reason}; denied by user", data={"approved": False})


__all__ = [
    "ApprovalProvider",
    "ApprovalRequest",
    "AutoApprovalProvider",
    "CliApprovalProvider",
    "PermissionHook",
]
