from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from llm_agent.hooks import HookContext, HookResult
from llm_agent.llm_client import LLMToolCall
from llm_agent.security import resolve_workspace_path, validate_shell_command

PATH_ARGUMENT_TOOLS = {
    "read_file",
    "write_file",
    "edit_file",
    "search_text",
}
MUTATING_FILE_TOOLS = {"write_file", "edit_file"}
EXECUTION_TOOL_REASONS = {
    "run_tests": "run_tests executes project test code",
    "run_lint": "run_lint executes project lint tooling",
}


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
    confirm_shell_commands: bool = True
    # Backwards-compatible alias for older callers.
    confirm_destructive_bash: bool | None = None

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

        if tool_call.name in EXECUTION_TOOL_REASONS:
            return self._ask_for_approval(
                tool_call,
                EXECUTION_TOOL_REASONS[tool_call.name],
            )

        if tool_call.name in MUTATING_FILE_TOOLS and self.confirm_file_mutations:
            path = str(arguments.get("path", ""))
            reason = f"{tool_call.name} modifies workspace file: {path}"
            return self._ask_for_approval(tool_call, reason)

        if tool_call.name == "memory_forget":
            memory_id = str(arguments.get("memory_id", ""))
            return self._ask_for_approval(
                tool_call,
                f"memory_forget permanently deletes memory: {memory_id}",
            )

        if tool_call.name == "worktree_apply":
            worktree_id = str(arguments.get("worktree_id", ""))
            return self._ask_for_approval(
                tool_call,
                f"worktree_apply modifies the main workspace: {worktree_id}",
            )

        if (
            tool_call.name == "worktree_remove"
            and arguments.get("discard_changes") is True
        ):
            worktree_id = str(arguments.get("worktree_id", ""))
            return self._ask_for_approval(
                tool_call,
                f"worktree_remove will discard isolated changes: {worktree_id}",
            )

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

        try:
            resolve_workspace_path(workdir, path)
        except ValueError as exc:
            return str(exc)
        return None

    def _check_bash(
        self,
        tool_call: LLMToolCall,
        arguments: dict[str, Any],
    ) -> HookResult | None:
        command = str(arguments.get("command", ""))
        try:
            validate_shell_command(command)
        except ValueError as exc:
            return HookResult.deny(str(exc))

        confirm_shell = self.confirm_shell_commands
        if self.confirm_destructive_bash is not None:
            confirm_shell = self.confirm_destructive_bash
        if not confirm_shell:
            return None

        reason = "bash executes a shell command"
        if arguments.get("run_in_background") is True:
            reason = "bash starts a background shell command"
        return self._ask_for_approval(tool_call, reason)

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
