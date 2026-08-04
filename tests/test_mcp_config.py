from pathlib import Path

import pytest

from llm_agent.mcp_config import MCPConfigError, load_mcp_config


def test_load_mcp_config_returns_empty_when_file_is_missing(
    tmp_path: Path,
) -> None:
    assert load_mcp_config(tmp_path).servers == ()


def test_load_mcp_config_parses_stdio_and_http_servers(
    tmp_path: Path,
) -> None:
    _write_config(
        tmp_path,
        """
version: 1
servers:
  local:
    transport: stdio
    command: python
    args: [server.py]
    workspace_scoped: true
    expose_to: [main, engineer, subagent]
    include_tools: [echo]
    permission: confirm
    tool_permissions:
      echo: allow
  remote:
    transport: streamable_http
    url: https://mcp.example.com/mcp
    headers:
      Authorization: Bearer ${MCP_TOKEN}
    expose_to: [qa]
    permission: deny
""",
    )

    config = load_mcp_config(tmp_path)

    assert [server.name for server in config.servers] == ["local", "remote"]
    local, remote = config.servers
    assert local.workspace_scoped is True
    assert local.expose_to == {"main", "engineer", "subagent"}
    assert local.include_tools == {"echo"}
    assert local.tool_permissions == {"echo": "allow"}
    assert remote.url == "https://mcp.example.com/mcp"
    assert remote.headers == {"Authorization": "Bearer ${MCP_TOKEN}"}
    assert remote.permission == "deny"


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (
            """
servers:
  demo:
    transport: stdio
""",
            "command is required",
        ),
        (
            """
servers:
  demo:
    transport: streamable_http
    url: http://mcp.example.com/mcp
""",
            "must use https",
        ),
        (
            """
servers:
  demo:
    transport: stdio
    command: python
    expose_to: [explore]
""",
            "Unknown MCP scope",
        ),
        (
            """
servers:
  demo:
    transport: stdio
    command: python
    permission: maybe
""",
            "allow.*confirm.*deny",
        ),
    ],
)
def test_load_mcp_config_rejects_invalid_values(
    tmp_path: Path,
    content: str,
    message: str,
) -> None:
    _write_config(tmp_path, content)

    with pytest.raises(MCPConfigError, match=message):
        load_mcp_config(tmp_path)


def _write_config(tmp_path: Path, content: str) -> None:
    path = tmp_path / ".llm_agent" / "mcp.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.strip() + "\n", encoding="utf-8")
