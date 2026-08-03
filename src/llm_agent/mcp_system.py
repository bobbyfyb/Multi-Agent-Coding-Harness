from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from threading import RLock
from time import perf_counter
from typing import Any, AsyncIterator, Literal
from uuid import uuid4

from anyio.from_thread import BlockingPortal, start_blocking_portal
import httpx2
from mcp import Client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client

from llm_agent.mcp_config import (
    MCPConfig,
    MCPPermission,
    MCPServerConfig,
    MCPToolScope,
)
from llm_agent.security import safe_subprocess_env
from llm_agent.trace_system import elapsed_ms, record_trace


MAX_DISCOVERY_PAGES = 20
MAX_RESULT_BLOCKS = 50
MAX_RESULT_CHARS = 50_000
MAX_TOOL_DESCRIPTION_CHARS = 2_000
MAX_TOOL_NAME_LENGTH = 64
MAX_TOOL_SCHEMA_CHARS = 50_000

MCPServerState = Literal["disabled", "connected", "failed"]


class MCPError(RuntimeError):
    """Base error for MCP integration failures."""


class MCPConnectionError(MCPError):
    """Raised when an MCP server cannot be connected or discovered."""


class MCPToolError(MCPError):
    """Raised when an MCP tool invocation fails."""


@dataclass(frozen=True)
class MCPToolBinding:
    public_name: str
    server_name: str
    remote_name: str
    description: str
    parameters: dict[str, Any]
    scopes: frozenset[MCPToolScope]
    permission: MCPPermission
    annotations: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MCPServerStatus:
    name: str
    transport: str
    state: MCPServerState
    tool_count: int = 0
    session_count: int = 0
    error: str | None = None


@dataclass
class _ClientSession:
    client: Client
    context: Any


class MCPManager:
    """Own MCP connections and expose their tools through a synchronous API."""

    def __init__(
        self,
        config: MCPConfig,
        *,
        workdir: Path | str | None = None,
    ) -> None:
        self.config = config
        self.workdir = (
            Path.cwd() if workdir is None else Path(workdir)
        ).resolve()
        self._configs = {server.name: server for server in config.servers}
        self._bindings: dict[str, MCPToolBinding] = {}
        self._statuses: dict[str, MCPServerStatus] = {}
        self._warnings: list[str] = []
        self._sessions: dict[tuple[str, str], _ClientSession] = {}
        self._portal_context: Any | None = None
        self._portal: BlockingPortal | None = None
        self._lock = RLock()
        self._started = False
        self._closed = False

    @property
    def started(self) -> bool:
        return self._started

    @property
    def warnings(self) -> tuple[str, ...]:
        return tuple(self._warnings)

    def start(self) -> None:
        if self._started:
            return
        if self._closed:
            raise MCPConnectionError("MCP manager is already closed.")

        enabled = [server for server in self.config.servers if server.enabled]
        for server in self.config.servers:
            if not server.enabled:
                self._statuses[server.name] = MCPServerStatus(
                    name=server.name,
                    transport=server.transport,
                    state="disabled",
                )
        if not enabled:
            self._started = True
            return

        self._portal_context = start_blocking_portal(backend="asyncio")
        self._portal = self._portal_context.__enter__()
        try:
            for server in enabled:
                self._start_server(server)
        except Exception:
            self._shutdown_portal()
            self._closed = True
            raise
        self._started = True

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._shutdown_portal()
            self._closed = True
            self._started = False

    def __enter__(self) -> "MCPManager":
        self.start()
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def tool_bindings(self, scope: MCPToolScope) -> tuple[MCPToolBinding, ...]:
        return tuple(
            binding
            for binding in self._bindings.values()
            if scope in binding.scopes
        )

    def tool_names_for_scope(self, scope: MCPToolScope) -> set[str]:
        return {binding.public_name for binding in self.tool_bindings(scope)}

    def is_tool(self, public_name: str) -> bool:
        return public_name in self._bindings

    def binding(self, public_name: str) -> MCPToolBinding:
        try:
            return self._bindings[public_name]
        except KeyError as exc:
            raise MCPToolError(f"Unknown MCP tool: {public_name}") from exc

    def status(self) -> tuple[MCPServerStatus, ...]:
        with self._lock:
            session_counts = self._session_counts()
            return tuple(
                MCPServerStatus(
                    name=status.name,
                    transport=status.transport,
                    state=status.state,
                    tool_count=status.tool_count,
                    session_count=session_counts.get(status.name, 0),
                    error=status.error,
                )
                for status in self._statuses.values()
            )

    def call(
        self,
        public_name: str,
        arguments: dict[str, Any],
        *,
        workdir: Path | str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            if not self._started or self._portal is None:
                raise MCPToolError("MCP manager is not running.")
            if not isinstance(arguments, dict):
                raise MCPToolError("MCP tool arguments must be a JSON object.")

            binding = self.binding(public_name)
            server = self._configs[binding.server_name]
            execution_workdir = (
                self.workdir if workdir is None else Path(workdir).resolve()
            )
            workspace = (
                execution_workdir if server.workspace_scoped else self.workdir
            )
            session = self._get_session(server, workspace)
            key = self._session_key(server, workspace)
        call_id = f"mcp-{uuid4().hex[:16]}"
        started_at = perf_counter()
        record_trace(
            category="mcp",
            name="mcp.tool.call",
            phase="started",
            correlation_id=call_id,
            data={
                "server": server.name,
                "tool": binding.remote_name,
                "public_name": public_name,
            },
        )
        try:
            with self._lock:
                if self._portal is None:
                    raise MCPToolError("MCP manager is not running.")
                result = self._portal.call(
                    _call_client,
                    session.client,
                    binding.remote_name,
                    dict(arguments),
                    server.timeout_seconds,
                )
        except Exception as exc:
            with self._lock:
                self._close_session(key)
            error = _safe_error(exc)
            record_trace(
                category="mcp",
                name="mcp.tool.call",
                phase="failed",
                status="error",
                correlation_id=call_id,
                duration_ms=elapsed_ms(started_at),
                data={
                    "server": server.name,
                    "tool": binding.remote_name,
                    "error": error,
                },
            )
            if isinstance(exc, MCPToolError):
                raise
            raise MCPToolError(
                f"MCP server '{server.name}' tool "
                f"'{binding.remote_name}' failed: {error}"
            ) from exc

        if result.is_error:
            detail = _tool_error_detail(result)
            error = MCPToolError(
                f"MCP server '{server.name}' tool "
                f"'{binding.remote_name}' returned an error: {detail}"
            )
            record_trace(
                category="mcp",
                name="mcp.tool.call",
                phase="failed",
                status="error",
                correlation_id=call_id,
                duration_ms=elapsed_ms(started_at),
                data={
                    "server": server.name,
                    "tool": binding.remote_name,
                    "error": str(error),
                },
            )
            raise error

        record_trace(
            category="mcp",
            name="mcp.tool.call",
            phase="completed",
            correlation_id=call_id,
            duration_ms=elapsed_ms(started_at),
            data={"server": server.name, "tool": binding.remote_name},
        )
        return _normalize_result(
            result,
            server_name=server.name,
            tool_name=binding.remote_name,
        )

    def _start_server(self, server: MCPServerConfig) -> None:
        if self._portal is None:
            raise MCPConnectionError("MCP portal is not running.")
        key = self._session_key(server, self.workdir)
        try:
            session = self._get_session(server, self.workdir)
            tools = self._discover(session.client, server)
            bindings = self._build_bindings(server, tools)
        except Exception as exc:
            self._close_session(key)
            error = _safe_error(exc)
            self._statuses[server.name] = MCPServerStatus(
                name=server.name,
                transport=server.transport,
                state="failed",
                error=error,
            )
            message = f"MCP server '{server.name}' unavailable: {error}"
            if server.required:
                raise MCPConnectionError(message) from exc
            self._warnings.append(message)
            return

        for binding in bindings:
            if binding.public_name in self._bindings:
                raise MCPConnectionError(
                    f"MCP public tool name collision: {binding.public_name}"
                )
            self._bindings[binding.public_name] = binding
        self._statuses[server.name] = MCPServerStatus(
            name=server.name,
            transport=server.transport,
            state="connected",
            tool_count=len(bindings),
        )

    def _discover(
        self,
        client: Client,
        server: MCPServerConfig,
    ) -> list[Any]:
        if self._portal is None:
            raise MCPConnectionError("MCP portal is not running.")
        tools: list[Any] = []
        cursor: str | None = None
        for _ in range(MAX_DISCOVERY_PAGES):
            page = self._portal.call(_list_tools, client, cursor)
            tools.extend(page.tools)
            cursor = page.next_cursor
            if cursor is None:
                return tools
        raise MCPConnectionError(
            f"MCP server '{server.name}' exceeded {MAX_DISCOVERY_PAGES} "
            "tool discovery pages."
        )

    def _get_session(
        self,
        server: MCPServerConfig,
        workspace: Path,
    ) -> _ClientSession:
        if self._portal is None:
            raise MCPConnectionError("MCP portal is not running.")
        key = self._session_key(server, workspace)
        existing = self._sessions.get(key)
        if existing is not None:
            return existing

        transport = self._transport(server, workspace)
        client = Client(
            transport,
            read_timeout_seconds=server.timeout_seconds,
            cache=None,
        )
        context = self._portal.wrap_async_context_manager(client)
        entered_client = context.__enter__()

        session = _ClientSession(client=entered_client, context=context)
        self._sessions[key] = session
        return session

    def _transport(
        self,
        server: MCPServerConfig,
        workspace: Path,
    ) -> Any:
        if server.transport == "stdio":
            if server.command is None:
                raise MCPConnectionError(
                    f"MCP server '{server.name}' has no command."
                )
            env = safe_subprocess_env(workspace)
            env.update(
                {
                    key: _expand(value, workspace, f"{server.name}.env.{key}")
                    for key, value in server.env.items()
                }
            )
            params = StdioServerParameters(
                command=_expand(
                    server.command,
                    workspace,
                    f"{server.name}.command",
                ),
                args=[
                    _expand(value, workspace, f"{server.name}.args")
                    for value in server.args
                ],
                env=env,
                cwd=_resolve_cwd(server.cwd, workspace, server.name),
            )
            return stdio_client(params)

        if server.url is None:
            raise MCPConnectionError(f"MCP server '{server.name}' has no URL.")
        headers = {
            key: _expand(value, workspace, f"{server.name}.headers.{key}")
            for key, value in server.headers.items()
        }
        return _http_transport(
            server.url,
            headers=headers,
            timeout_seconds=server.timeout_seconds,
        )

    def _build_bindings(
        self,
        server: MCPServerConfig,
        tools: list[Any],
    ) -> list[MCPToolBinding]:
        available_names = {str(tool.name) for tool in tools}
        if server.include_tools is not None:
            missing = server.include_tools - available_names
            if missing:
                self._warnings.append(
                    f"MCP server '{server.name}' did not expose configured "
                    f"tool(s): {', '.join(sorted(missing))}"
                )
        unknown_permissions = set(server.tool_permissions) - available_names
        if unknown_permissions:
            self._warnings.append(
                f"MCP server '{server.name}' has permission rules for unknown "
                f"tool(s): {', '.join(sorted(unknown_permissions))}"
            )

        selected = [
            tool
            for tool in tools
            if (
                server.include_tools is None
                or str(tool.name) in server.include_tools
            )
            and str(tool.name) not in server.exclude_tools
        ]
        if len(selected) > server.max_tools:
            self._warnings.append(
                f"MCP server '{server.name}' exposed {len(selected)} selected "
                f"tools; only the first {server.max_tools} were registered."
            )
            selected = selected[: server.max_tools]

        bindings: list[MCPToolBinding] = []
        used_names = set(self._bindings)
        for tool in selected:
            remote_name = str(tool.name)
            schema = dict(tool.input_schema)
            if schema.get("type") != "object":
                self._warnings.append(
                    f"Ignored MCP tool '{server.name}/{remote_name}': "
                    "input schema root must have type=object."
                )
                continue
            schema_chars = len(
                json.dumps(schema, ensure_ascii=True, default=str)
            )
            if schema_chars > MAX_TOOL_SCHEMA_CHARS:
                self._warnings.append(
                    f"Ignored MCP tool '{server.name}/{remote_name}': input "
                    f"schema exceeds {MAX_TOOL_SCHEMA_CHARS} characters."
                )
                continue
            public_name = _public_tool_name(
                server.name,
                remote_name,
                used_names,
            )
            used_names.add(public_name)
            annotations = (
                tool.annotations.model_dump(
                    mode="json",
                    by_alias=True,
                    exclude_none=True,
                )
                if tool.annotations is not None
                else {}
            )
            description = (tool.description or remote_name).strip()
            if len(description) > MAX_TOOL_DESCRIPTION_CHARS:
                description = description[:MAX_TOOL_DESCRIPTION_CHARS]
                self._warnings.append(
                    f"Truncated MCP tool description for "
                    f"'{server.name}/{remote_name}'."
                )
            bindings.append(
                MCPToolBinding(
                    public_name=public_name,
                    server_name=server.name,
                    remote_name=remote_name,
                    description=f"[MCP: {server.name}] {description}",
                    parameters=schema,
                    scopes=server.expose_to,
                    permission=server.tool_permissions.get(
                        remote_name,
                        server.permission,
                    ),
                    annotations=annotations,
                )
            )
        return bindings

    def _session_key(
        self,
        server: MCPServerConfig,
        workspace: Path,
    ) -> tuple[str, str]:
        workspace_key = str(workspace.resolve()) if server.workspace_scoped else ""
        return server.name, workspace_key

    def _close_session(self, key: tuple[str, str]) -> None:
        session = self._sessions.pop(key, None)
        if session is not None:
            session.context.__exit__(None, None, None)

    def _close_sessions(self) -> None:
        for key in reversed(tuple(self._sessions)):
            try:
                self._close_session(key)
            except Exception as exc:
                self._warnings.append(
                    f"Failed to close MCP session '{key[0]}': {_safe_error(exc)}"
                )

    def _session_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for server_name, _ in self._sessions:
            counts[server_name] = counts.get(server_name, 0) + 1
        return counts

    def _shutdown_portal(self) -> None:
        portal = self._portal
        portal_context = self._portal_context
        if portal is not None:
            self._close_sessions()
        self._portal = None
        self._portal_context = None
        if portal_context is not None:
            portal_context.__exit__(None, None, None)


@asynccontextmanager
async def _http_transport(
    url: str,
    *,
    headers: dict[str, str],
    timeout_seconds: float,
) -> AsyncIterator[tuple[Any, Any]]:
    async with httpx2.AsyncClient(
        headers=headers,
        timeout=timeout_seconds,
        follow_redirects=False,
        trust_env=False,
    ) as http_client:
        async with streamable_http_client(
            url,
            http_client=http_client,
        ) as streams:
            yield streams


async def _list_tools(client: Client, cursor: str | None) -> Any:
    return await client.list_tools(cursor=cursor)


async def _call_client(
    client: Client,
    tool_name: str,
    arguments: dict[str, Any],
    timeout_seconds: float,
) -> Any:
    return await client.call_tool(
        tool_name,
        arguments,
        read_timeout_seconds=timeout_seconds,
    )


def _resolve_cwd(value: str, workspace: Path, server_name: str) -> Path:
    expanded = _expand(value, workspace, f"{server_name}.cwd")
    path = Path(expanded)
    if not path.is_absolute():
        path = workspace / path
    return path.resolve()


_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_SAFE_NAME_PATTERN = re.compile(r"[^A-Za-z0-9_-]+")


def _expand(value: str, workspace: Path, label: str) -> str:
    def replace_env(match: re.Match[str]) -> str:
        name = match.group(1)
        resolved = os.getenv(name)
        if resolved is None:
            raise MCPConnectionError(
                f"Missing environment variable {name} for {label}."
            )
        return resolved

    return _ENV_PATTERN.sub(replace_env, value).replace(
        "{workspace}",
        str(workspace.resolve()),
    )


def _public_tool_name(
    server_name: str,
    remote_name: str,
    used_names: set[str],
) -> str:
    safe_server = _safe_segment(server_name)
    safe_tool = _safe_segment(remote_name)
    base = f"mcp__{safe_server}__{safe_tool}"
    if len(base) <= MAX_TOOL_NAME_LENGTH and base not in used_names:
        return base

    digest = sha256(f"{server_name}\0{remote_name}".encode()).hexdigest()[:8]
    suffix = f"__{digest}"
    return base[: MAX_TOOL_NAME_LENGTH - len(suffix)].rstrip("_-") + suffix


def _safe_segment(value: str) -> str:
    segment = _SAFE_NAME_PATTERN.sub("_", value).strip("_-")
    return segment or "tool"


def _normalize_result(
    result: Any,
    *,
    server_name: str,
    tool_name: str,
) -> dict[str, Any]:
    remaining_chars = MAX_RESULT_CHARS
    content: list[dict[str, Any]] = []
    for block in result.content[:MAX_RESULT_BLOCKS]:
        normalized, used = _normalize_content_block(block, remaining_chars)
        content.append(normalized)
        remaining_chars = max(0, remaining_chars - used)
    if len(result.content) > MAX_RESULT_BLOCKS:
        content.append(
            {
                "type": "notice",
                "text": (
                    f"{len(result.content) - MAX_RESULT_BLOCKS} additional "
                    "content blocks omitted."
                ),
            }
        )

    normalized_result: dict[str, Any] = {
        "server": server_name,
        "tool": tool_name,
        "content": content,
    }
    if result.structured_content is not None:
        normalized_result["structured_content"] = _bounded_json_value(
            result.structured_content,
            MAX_RESULT_CHARS,
        )
    return normalized_result


def _normalize_content_block(
    block: Any,
    remaining_chars: int,
) -> tuple[dict[str, Any], int]:
    data = block.model_dump(mode="json", by_alias=True, exclude_none=True)
    block_type = data.get("type")
    if block_type == "text":
        text = str(data.get("text", ""))
        kept = text[:remaining_chars]
        result: dict[str, Any] = {"type": "text", "text": kept}
        if len(kept) < len(text):
            result["truncated"] = True
        return result, len(kept)

    if block_type in {"image", "audio"}:
        payload = str(data.get("data", ""))
        return (
            {
                "type": block_type,
                "mime_type": data.get("mimeType"),
                "base64_chars": len(payload),
                "omitted": True,
            },
            0,
        )

    if block_type == "resource_link":
        return (
            {
                key: data[key]
                for key in (
                    "type",
                    "uri",
                    "name",
                    "title",
                    "description",
                    "mimeType",
                )
                if key in data
            },
            0,
        )

    if block_type == "resource":
        resource = dict(data.get("resource") or {})
        text = resource.pop("text", None)
        blob = resource.pop("blob", None)
        used = 0
        if text is not None:
            raw_text = str(text)
            kept = raw_text[:remaining_chars]
            resource["text"] = kept
            used = len(kept)
            if len(kept) < len(raw_text):
                resource["truncated"] = True
        if blob is not None:
            resource["blob_base64_chars"] = len(str(blob))
            resource["blob_omitted"] = True
        return {"type": "resource", "resource": resource}, used

    return _bounded_json_value(data, remaining_chars), 0


def _bounded_json_value(value: Any, max_chars: int) -> Any:
    try:
        encoded = json.dumps(value, ensure_ascii=True, default=str)
    except (TypeError, ValueError):
        encoded = str(value)
    if len(encoded) <= max_chars:
        return value
    return {
        "truncated": True,
        "preview": encoded[:max_chars],
        "original_chars": len(encoded),
    }


def _tool_error_detail(result: Any) -> str:
    text_parts = [
        str(getattr(block, "text"))
        for block in result.content
        if getattr(block, "type", None) == "text"
        and getattr(block, "text", None)
    ]
    if text_parts:
        return "\n".join(text_parts)[:2_000]
    if result.structured_content is not None:
        return json.dumps(
            result.structured_content,
            ensure_ascii=True,
            default=str,
        )[:2_000]
    return "the server did not provide error details"


def _safe_error(exc: BaseException) -> str:
    text = str(exc).strip() or type(exc).__name__
    return text[:2_000]


__all__ = [
    "MCPConnectionError",
    "MCPError",
    "MCPManager",
    "MCPServerStatus",
    "MCPToolBinding",
    "MCPToolError",
]
