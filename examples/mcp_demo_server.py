"""Small stdio MCP server used by the documentation and integration tests."""

import argparse
import os
from pathlib import Path

from mcp.server import MCPServer


server = MCPServer("llm-agent-demo")


@server.tool()
def echo(text: str) -> str:
    """Return the supplied text."""
    return text


@server.tool()
def workspace_info() -> dict[str, object]:
    """Report the server workspace and environment-isolation signals."""
    return {
        "cwd": str(Path.cwd()),
        "home": os.getenv("HOME"),
        "has_anthropic_api_key": "ANTHROPIC_API_KEY" in os.environ,
        "explicit_value": os.getenv("MCP_DEMO_VALUE"),
    }


@server.tool()
def always_fail() -> str:
    """Return a tool-level error for adapter testing."""
    raise RuntimeError("intentional demo failure")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http"),
        default="stdio",
    )
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    if args.transport == "streamable-http":
        server.run(
            transport="streamable-http",
            host="127.0.0.1",
            port=args.port,
        )
    else:
        server.run(transport="stdio")
