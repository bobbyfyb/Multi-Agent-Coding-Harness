from pathlib import Path
import socket
import subprocess
import sys
import time

import pytest

from llm_agent.agent import ToolExecutionContext
from llm_agent.hooks import HookContext
from llm_agent.hooks.mcp_hooks import MCPPermissionHook
from llm_agent.hooks.permission_hooks import AutoApprovalProvider
from llm_agent.llm_client import LLMToolCall
from llm_agent.mcp_config import MCPConfig, MCPServerConfig
from llm_agent.mcp_system import MCPConnectionError, MCPManager
from llm_agent.security import safe_subprocess_env
from llm_agent.tool_registry import ToolRegistry
from llm_agent.tools.mcp_tools import register_tools as register_mcp_tools


DEMO_SERVER = (
    Path(__file__).resolve().parents[1] / "examples" / "mcp_demo_server.py"
)


def test_stdio_mcp_discovery_call_and_error_normalization(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-leak")
    manager = _manager(
        tmp_path,
        env={"MCP_DEMO_VALUE": "configured"},
    )

    with manager:
        assert manager.tool_names_for_scope("main") == {
            "mcp__demo__echo",
            "mcp__demo__workspace_info",
            "mcp__demo__always_fail",
        }, manager.warnings
        registry = ToolRegistry()
        register_mcp_tools(registry, manager=manager, scope="main")
        context = _context(tmp_path)

        echo = registry.call(
            "mcp__demo__echo",
            {"text": "hello MCP"},
            context=context,
        )
        workspace = registry.call(
            "mcp__demo__workspace_info",
            {},
            context=context,
        )
        failure = registry.call(
            "mcp__demo__always_fail",
            {},
            context=context,
        )

        assert echo["ok"] is True
        assert echo["result"]["content"][0]["text"] == "hello MCP"
        assert workspace["ok"] is True
        structured = workspace["result"]["structured_content"]
        assert structured["cwd"] == str(tmp_path)
        assert structured["home"] == str(tmp_path)
        assert structured["has_anthropic_api_key"] is False
        assert structured["explicit_value"] == "configured"
        assert failure["ok"] is False
        assert "intentional demo failure" in failure["error"]

        status = manager.status()[0]
        assert status.state == "connected"
        assert status.tool_count == 3
        assert status.session_count == 1


def test_workspace_scoped_server_uses_execution_workdir(tmp_path: Path) -> None:
    other_workdir = tmp_path / "worktree"
    other_workdir.mkdir()
    manager = _manager(tmp_path, workspace_scoped=True)

    with manager:
        registry = ToolRegistry()
        register_mcp_tools(registry, manager=manager, scope="main")
        result = registry.call(
            "mcp__demo__workspace_info",
            {},
            context=_context(other_workdir),
        )

        assert result["ok"] is True
        assert result["result"]["structured_content"]["cwd"] == str(
            other_workdir
        )
        assert manager.status()[0].session_count == 2


def test_streamable_http_mcp_discovery_and_call(tmp_path: Path) -> None:
    port = _unused_loopback_port()
    process = subprocess.Popen(
        [
            sys.executable,
            str(DEMO_SERVER),
            "--transport",
            "streamable-http",
            "--port",
            str(port),
        ],
        cwd=tmp_path,
        env=safe_subprocess_env(tmp_path),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_for_port(process, port)
        manager = MCPManager(
            MCPConfig(
                servers=(
                    MCPServerConfig(
                        name="http_demo",
                        transport="streamable_http",
                        url=f"http://127.0.0.1:{port}/mcp",
                        permission="allow",
                        timeout_seconds=10,
                    ),
                )
            ),
            workdir=tmp_path,
        )
        with manager:
            registry = ToolRegistry()
            register_mcp_tools(registry, manager=manager, scope="main")
            result = registry.call(
                "mcp__http_demo__echo",
                {"text": "over HTTP"},
                context=_context(tmp_path),
            )

            assert result["ok"] is True
            assert result["result"]["content"][0]["text"] == "over HTTP"
            assert manager.status()[0].transport == "streamable_http"
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def test_mcp_scope_filter_and_permission_policies(tmp_path: Path) -> None:
    manager = _manager(
        tmp_path,
        expose_to=frozenset({"main", "engineer"}),
        permission="deny",
        tool_permissions={"echo": "allow", "workspace_info": "confirm"},
    )

    with manager:
        assert manager.tool_names_for_scope("qa") == set()
        hook = MCPPermissionHook(
            manager,
            approval_provider=AutoApprovalProvider(approved=True),
        )
        context = HookContext(messages=[], workdir=tmp_path)

        allowed = hook(_call("mcp__demo__echo"), context)
        confirmed = hook(_call("mcp__demo__workspace_info"), context)
        denied = hook(_call("mcp__demo__always_fail"), context)

        assert allowed is not None and allowed.action == "allow"
        assert confirmed is not None and confirmed.action == "allow"
        assert confirmed.data["approved"] is True
        assert denied is not None and denied.denied


def test_optional_unavailable_server_becomes_warning(tmp_path: Path) -> None:
    manager = MCPManager(
        MCPConfig(
            servers=(
                MCPServerConfig(
                    name="missing",
                    transport="stdio",
                    command="definitely-not-an-installed-command",
                ),
            )
        ),
        workdir=tmp_path,
    )

    with manager:
        assert manager.tool_names_for_scope("main") == set()
        assert manager.status()[0].state == "failed"
        assert "unavailable" in manager.warnings[0]


def test_required_unavailable_server_stops_startup(tmp_path: Path) -> None:
    manager = MCPManager(
        MCPConfig(
            servers=(
                MCPServerConfig(
                    name="required",
                    transport="stdio",
                    command="definitely-not-an-installed-command",
                    required=True,
                ),
            )
        ),
        workdir=tmp_path,
    )

    with pytest.raises(MCPConnectionError, match="required.*unavailable"):
        manager.start()


def _manager(
    workdir: Path,
    *,
    workspace_scoped: bool = False,
    expose_to=frozenset({"main"}),
    permission="allow",
    tool_permissions=None,
    env=None,
) -> MCPManager:
    return MCPManager(
        MCPConfig(
            servers=(
                MCPServerConfig(
                    name="demo",
                    transport="stdio",
                    command=sys.executable,
                    args=(str(DEMO_SERVER),),
                    env=env or {},
                    workspace_scoped=workspace_scoped,
                    expose_to=expose_to,
                    permission=permission,
                    tool_permissions=tool_permissions or {},
                    timeout_seconds=10,
                ),
            )
        ),
        workdir=workdir,
    )


def _context(workdir: Path) -> ToolExecutionContext:
    return ToolExecutionContext(
        run_id="run-mcp",
        agent_id="main",
        parent_run_id=None,
        depth=0,
        step=1,
        workdir=workdir,
    )


def _call(name: str) -> LLMToolCall:
    return LLMToolCall(id="call-mcp", name=name, arguments={})


def _unused_loopback_port() -> int:
    with socket.socket() as server_socket:
        server_socket.bind(("127.0.0.1", 0))
        return int(server_socket.getsockname()[1])


def _wait_for_port(process: subprocess.Popen, port: int) -> None:
    for _ in range(100):
        if process.poll() is not None:
            raise RuntimeError("HTTP MCP demo server exited before startup.")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                return
        except OSError:
            time.sleep(0.05)
    raise TimeoutError("HTTP MCP demo server did not start in time.")
