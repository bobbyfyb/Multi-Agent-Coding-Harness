from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Any, Literal, Mapping
from urllib.parse import urlparse

import yaml


DEFAULT_MCP_CONFIG_PATH = ".llm_agent/mcp.yaml"

MCPTransport = Literal["stdio", "streamable_http"]
MCPPermission = Literal["allow", "confirm", "deny"]
MCPToolScope = Literal[
    "main",
    "pm",
    "engineer",
    "qa",
    "pm_acceptance",
    "subagent",
]

MCP_TOOL_SCOPES = frozenset(
    {"main", "pm", "engineer", "qa", "pm_acceptance", "subagent"}
)
MCP_PERMISSIONS = frozenset({"allow", "confirm", "deny"})
_SERVER_NAME_PATTERN = re.compile(r"[A-Za-z0-9_-]+")


class MCPConfigError(RuntimeError):
    """Raised when mcp.yaml is invalid."""


@dataclass(frozen=True)
class MCPServerConfig:
    name: str
    transport: MCPTransport
    enabled: bool = True
    required: bool = False
    command: str | None = None
    args: tuple[str, ...] = ()
    url: str | None = None
    cwd: str = "{workspace}"
    env: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    workspace_scoped: bool = False
    expose_to: frozenset[MCPToolScope] = frozenset({"main"})
    include_tools: frozenset[str] | None = None
    exclude_tools: frozenset[str] = frozenset()
    permission: MCPPermission = "confirm"
    tool_permissions: dict[str, MCPPermission] = field(default_factory=dict)
    timeout_seconds: float = 30.0
    max_tools: int = 32


@dataclass(frozen=True)
class MCPConfig:
    servers: tuple[MCPServerConfig, ...] = ()


def load_mcp_config(
    workdir: Path | str | None = None,
    *,
    config_path: Path | str | None = None,
) -> MCPConfig:
    root = Path.cwd() if workdir is None else Path(workdir)
    path = _resolve_config_path(root, config_path)
    if not path.exists():
        return MCPConfig()
    if not path.is_file():
        raise MCPConfigError(f"MCP config is not a file: {path}")

    raw = path.read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(raw) if raw.strip() else {}
    except yaml.YAMLError as exc:
        raise MCPConfigError(f"Invalid MCP config YAML: {path}") from exc
    if data is None:
        data = {}
    if not isinstance(data, Mapping):
        raise MCPConfigError("MCP config root must be a mapping.")

    unknown = set(data) - {"version", "servers"}
    if unknown:
        raise MCPConfigError(
            "Unsupported top-level MCP config key(s): "
            + ", ".join(sorted(unknown))
        )
    if data.get("version", 1) != 1:
        raise MCPConfigError("Only MCP config version 1 is supported.")

    raw_servers = _mapping(data.get("servers", {}), "servers")
    servers = tuple(
        _parse_server(name, value)
        for name, value in raw_servers.items()
    )
    return MCPConfig(servers=servers)


def _parse_server(name: Any, value: Any) -> MCPServerConfig:
    if not isinstance(name, str) or not name.strip():
        raise MCPConfigError("MCP server names must be non-empty strings.")
    name = name.strip()
    if _SERVER_NAME_PATTERN.fullmatch(name) is None:
        raise MCPConfigError(
            f"MCP server name '{name}' may contain only letters, digits, _ and -."
        )

    label = f"servers.{name}"
    data = _mapping(value, label)
    allowed_keys = {
        "transport",
        "enabled",
        "required",
        "command",
        "args",
        "url",
        "cwd",
        "env",
        "headers",
        "workspace_scoped",
        "expose_to",
        "include_tools",
        "exclude_tools",
        "permission",
        "tool_permissions",
        "timeout_seconds",
        "max_tools",
    }
    unknown = set(data) - allowed_keys
    if unknown:
        raise MCPConfigError(
            f"Unsupported key(s) for {label}: " + ", ".join(sorted(unknown))
        )

    transport = data.get("transport", "stdio")
    if transport not in {"stdio", "streamable_http"}:
        raise MCPConfigError(
            f"{label}.transport must be 'stdio' or 'streamable_http'."
        )

    enabled = _boolean(data.get("enabled", True), f"{label}.enabled")
    required = _boolean(data.get("required", False), f"{label}.required")
    if required and not enabled:
        raise MCPConfigError(f"{label} cannot be both required and disabled.")
    workspace_scoped = _boolean(
        data.get("workspace_scoped", False),
        f"{label}.workspace_scoped",
    )
    expose_to = frozenset(
        _string_list(
            data.get("expose_to", ["main"]),
            f"{label}.expose_to",
        )
    )
    invalid_scopes = expose_to - MCP_TOOL_SCOPES
    if invalid_scopes:
        raise MCPConfigError(
            f"Unknown MCP scope(s) in {label}.expose_to: "
            + ", ".join(sorted(invalid_scopes))
        )

    permission = _permission(
        data.get("permission", "confirm"),
        f"{label}.permission",
    )
    raw_tool_permissions = _mapping(
        data.get("tool_permissions", {}),
        f"{label}.tool_permissions",
    )
    tool_permissions: dict[str, MCPPermission] = {}
    for tool_name, policy in raw_tool_permissions.items():
        _validate_tool_name(tool_name, f"{label}.tool_permissions")
        tool_permissions[tool_name] = _permission(
            policy,
            f"{label}.tool_permissions.{tool_name}",
        )

    include_value = data.get("include_tools")
    include_tools = (
        None
        if include_value is None
        else frozenset(_string_list(include_value, f"{label}.include_tools"))
    )
    exclude_tools = frozenset(
        _string_list(
            data.get("exclude_tools", []),
            f"{label}.exclude_tools",
        )
    )
    overlap = (include_tools or frozenset()) & exclude_tools
    if overlap:
        raise MCPConfigError(
            f"Tools cannot be both included and excluded for {label}: "
            + ", ".join(sorted(overlap))
        )

    timeout_seconds = _positive_number(
        data.get("timeout_seconds", 30),
        f"{label}.timeout_seconds",
    )
    max_tools = _positive_int(data.get("max_tools", 32), f"{label}.max_tools")
    if max_tools > 128:
        raise MCPConfigError(f"{label}.max_tools cannot exceed 128.")

    command = _optional_string(data.get("command"), f"{label}.command")
    url = _optional_string(data.get("url"), f"{label}.url")
    args = tuple(_string_list(data.get("args", []), f"{label}.args"))
    cwd = _optional_string(data.get("cwd"), f"{label}.cwd") or "{workspace}"
    env = _string_mapping(data.get("env", {}), f"{label}.env")
    headers = _string_mapping(data.get("headers", {}), f"{label}.headers")

    if transport == "stdio":
        if command is None:
            raise MCPConfigError(f"{label}.command is required for stdio.")
        if url is not None or headers:
            raise MCPConfigError(
                f"{label} cannot use url or headers with stdio transport."
            )
    else:
        if url is None:
            raise MCPConfigError(
                f"{label}.url is required for streamable_http."
            )
        _validate_http_url(url, f"{label}.url")
        if command is not None or args or env or "cwd" in data:
            raise MCPConfigError(
                f"{label} cannot use command, args, cwd, or env with "
                "streamable_http transport."
            )
        if workspace_scoped:
            raise MCPConfigError(
                f"{label}.workspace_scoped is only supported for stdio."
            )

    return MCPServerConfig(
        name=name,
        transport=transport,
        enabled=enabled,
        required=required,
        command=command,
        args=args,
        url=url,
        cwd=cwd,
        env=env,
        headers=headers,
        workspace_scoped=workspace_scoped,
        expose_to=expose_to,
        include_tools=include_tools,
        exclude_tools=exclude_tools,
        permission=permission,
        tool_permissions=tool_permissions,
        timeout_seconds=timeout_seconds,
        max_tools=max_tools,
    )


def _resolve_config_path(workdir: Path, config_path: Path | str | None) -> Path:
    if config_path is None:
        return (workdir / DEFAULT_MCP_CONFIG_PATH).resolve()
    path = Path(config_path)
    return path.resolve() if path.is_absolute() else (workdir / path).resolve()


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise MCPConfigError(f"{label} must be a mapping.")
    return value


def _string_mapping(value: Any, label: str) -> dict[str, str]:
    mapping = _mapping(value, label)
    result: dict[str, str] = {}
    for key, item in mapping.items():
        if not isinstance(key, str) or not key.strip():
            raise MCPConfigError(f"{label} keys must be non-empty strings.")
        if not isinstance(item, str):
            raise MCPConfigError(f"{label}.{key} must be a string.")
        result[key] = item
    return result


def _string_list(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise MCPConfigError(f"{label} must be a list of strings.")
    result: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise MCPConfigError(
                f"{label}[{index}] must be a non-empty string."
            )
        result.append(item.strip())
    return tuple(result)


def _validate_tool_name(value: Any, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise MCPConfigError(f"{label} keys must be non-empty tool names.")


def _optional_string(value: Any, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise MCPConfigError(f"{label} must be a non-empty string.")
    return value.strip()


def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise MCPConfigError(f"{label} must be a boolean.")
    return value


def _positive_int(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise MCPConfigError(f"{label} must be a positive integer.")
    return value


def _positive_number(value: Any, label: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        raise MCPConfigError(f"{label} must be a positive number.")
    return float(value)


def _permission(value: Any, label: str) -> MCPPermission:
    if value not in MCP_PERMISSIONS:
        raise MCPConfigError(f"{label} must be 'allow', 'confirm', or 'deny'.")
    return value


def _validate_http_url(value: str, label: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme == "https" and parsed.netloc:
        return
    if (
        parsed.scheme == "http"
        and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        and parsed.netloc
    ):
        return
    raise MCPConfigError(
        f"{label} must use https, except loopback hosts may use http."
    )


__all__ = [
    "DEFAULT_MCP_CONFIG_PATH",
    "MCPConfig",
    "MCPConfigError",
    "MCPPermission",
    "MCPServerConfig",
    "MCPToolScope",
    "MCPTransport",
    "load_mcp_config",
]
