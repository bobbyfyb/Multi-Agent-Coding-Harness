from __future__ import annotations

from dataclasses import dataclass

from llm_agent.hooks import HookContext, HookResult
from llm_agent.hooks.permission_hooks import ApprovalProvider, ApprovalRequest
from llm_agent.llm_client import LLMToolCall
from llm_agent.mcp_system import MCPManager


@dataclass(frozen=True)
class MCPPermissionHook:
    manager: MCPManager
    approval_provider: ApprovalProvider | None = None

    def __call__(
        self,
        tool_call: LLMToolCall,
        context: HookContext,
    ) -> HookResult | None:
        del context
        if not self.manager.is_tool(tool_call.name):
            return None

        binding = self.manager.binding(tool_call.name)
        data = {
            "mcp_server": binding.server_name,
            "mcp_tool": binding.remote_name,
            "policy": binding.permission,
            "annotations": binding.annotations,
        }
        label = f"{binding.server_name}/{binding.remote_name}"
        if binding.permission == "deny":
            return HookResult.deny(
                f"MCP policy denies tool: {label}",
                data=data,
            )
        if binding.permission == "allow":
            return HookResult.allow(
                reason=f"MCP policy allows tool: {label}",
                data=data,
            )

        reason = f"MCP tool requires confirmation: {label}"
        if self.approval_provider is None:
            return HookResult.deny(
                f"{reason}; no approval provider configured",
                data=data,
            )
        request = ApprovalRequest(
            tool_name=tool_call.name,
            arguments=tool_call.arguments,
            reason=reason,
        )
        if self.approval_provider.approve(request):
            return HookResult.allow(
                reason=reason,
                data={**data, "approved": True},
            )
        return HookResult.deny(
            f"{reason}; denied by user",
            data={**data, "approved": False},
        )


__all__ = ["MCPPermissionHook"]
