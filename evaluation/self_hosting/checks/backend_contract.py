from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import threading
import time
from time import perf_counter
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from _common import (
    Check,
    ContractError,
    candidate_metadata,
    require,
    run_command,
    utc_now,
    write_result,
)


class _FakeOpenAIHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        tools = payload.get("tools") or []
        messages = payload.get("messages") or []
        has_tool_result = any(message.get("role") == "tool" for message in messages)

        if not tools:
            content = "NO"
            tool_calls: list[dict[str, Any]] = []
            finish_reason = "stop"
        elif has_tool_result:
            content = "Workspace inspection completed."
            tool_calls = []
            finish_reason = "stop"
        else:
            time.sleep(0.3)
            content = "I will inspect the workspace before replying."
            tool_calls = [
                {
                    "id": "call_eval_bash",
                    "type": "function",
                    "function": {
                        "name": "bash",
                        "arguments": json.dumps({"command": "pwd"}),
                    },
                }
            ]
            finish_reason = "tool_calls"

        response = {
            "id": "chatcmpl-evaluation",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "evaluation-model",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": content,
                        "tool_calls": tool_calls,
                    },
                    "finish_reason": finish_reason,
                }
            ],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
            },
        }
        body = json.dumps(response).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _request(
    method: str,
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 5,
) -> tuple[int, dict[str, str], Any]:
    body = json.dumps(payload).encode() if payload is not None else None
    request_headers = {"Accept": "application/json", **(headers or {})}
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    request = Request(url, data=body, method=method, headers=request_headers)
    try:
        response = urlopen(request, timeout=timeout)
    except HTTPError as exc:
        response_body = exc.read().decode()
        parsed = json.loads(response_body) if response_body else None
        return exc.code, dict(exc.headers.items()), parsed
    with response:
        response_body = response.read().decode()
        parsed = json.loads(response_body) if response_body else None
        return response.status, dict(response.headers.items()), parsed


def _poll(
    fetch: Callable[[], tuple[int, dict[str, str], Any]],
    predicate: Callable[[Any], bool],
    *,
    timeout: float,
) -> tuple[int, dict[str, str], Any]:
    deadline = time.monotonic() + timeout
    last: tuple[int, dict[str, str], Any] | None = None
    while time.monotonic() < deadline:
        last = fetch()
        if last[0] == 200 and predicate(last[2]):
            return last
        time.sleep(0.1)
    raise ContractError(f"Timed out waiting for API state; last response={last}")


def _read_sse(url: str) -> tuple[dict[str, str], list[dict[str, Any]]]:
    request = Request(url, headers={"Accept": "text/event-stream"})
    with urlopen(request, timeout=10) as response:
        headers = dict(response.headers.items())
        content = response.read().decode()

    events: list[dict[str, Any]] = []
    for frame in content.replace("\r\n", "\n").split("\n\n"):
        data_lines = [
            line[5:].lstrip() for line in frame.splitlines() if line.startswith("data:")
        ]
        if data_lines:
            events.append(json.loads("\n".join(data_lines)))
    return headers, events


def _wait_for_health(base_url: str, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise ContractError(f"Server exited early with code {process.returncode}")
        try:
            status, _, payload = _request("GET", f"{base_url}/api/health", timeout=1)
            if status == 200 and payload == {"status": "ok"}:
                return
        except (URLError, TimeoutError, json.JSONDecodeError):
            pass
        time.sleep(0.1)
    raise ContractError("Server did not become healthy within 30 seconds.")


def _stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the external self-hosting backend contract."
    )
    parser.add_argument("workspace", type=Path, help="Candidate Worktree path.")
    parser.add_argument("--output", type=Path, help="Result JSON path.")
    parser.add_argument(
        "--skip-regression",
        action="store_true",
        help="Skip uv sync, Ruff, and pytest while iterating on the API contract.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    workspace = args.workspace.expanduser().resolve()
    output = args.output or workspace / ".llm_agent/evaluation/backend-result.json"
    checks: list[Check] = []
    observed = candidate_metadata(workspace) if workspace.is_dir() else {}
    started_at = utc_now()
    started_clock = perf_counter()
    error: str | None = None
    fake_server: ThreadingHTTPServer | None = None
    server_process: subprocess.Popen[str] | None = None
    server_log = None

    try:
        require(checks, "workspace_exists", workspace.is_dir(), str(workspace))
        if not args.skip_regression:
            run_command(
                checks,
                "uv_sync_locked",
                ["uv", "sync", "--locked"],
                cwd=workspace,
                timeout=180,
            )
            run_command(
                checks,
                "ruff",
                ["uv", "run", "ruff", "check", "."],
                cwd=workspace,
                timeout=120,
            )
            run_command(
                checks,
                "pytest",
                ["uv", "run", "pytest", "-q"],
                cwd=workspace,
                timeout=180,
            )
        run_command(
            checks,
            "server_help",
            ["uv", "run", "python", "-m", "llm_agent.server", "--help"],
            cwd=workspace,
            timeout=30,
        )

        fake_server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            _FakeOpenAIHandler,
        )
        threading.Thread(target=fake_server.serve_forever, daemon=True).start()
        fake_port = int(fake_server.server_address[1])
        api_port = _free_port()
        base_url = f"http://127.0.0.1:{api_port}"
        environment = {
            **os.environ,
            "LLM_PROVIDER": "openai",
            "LLM_MODEL": "evaluation-model",
            "OPENAI_API_KEY": "evaluation-key",
            "LLM_BASE_URL": f"http://127.0.0.1:{fake_port}/v1",
            "LLM_TIMEOUT": "10",
            "LLM_MAX_RETRIES": "0",
        }
        log_path = workspace / ".llm_agent/evaluation/server.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        server_log = log_path.open("w", encoding="utf-8")
        server_process = subprocess.Popen(
            [
                "uv",
                "run",
                "python",
                "-m",
                "llm_agent.server",
                "--host",
                "127.0.0.1",
                "--port",
                str(api_port),
            ],
            cwd=workspace,
            env=environment,
            stdout=server_log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        _wait_for_health(base_url, server_process)
        checks.append(Check("health", True, "GET /api/health returned status=ok"))

        status, headers, payload = _request(
            "GET",
            f"{base_url}/api/health",
            headers={"Origin": "https://outside.example"},
        )
        require(checks, "health_status", status == 200, f"status={status}")
        allow_origin = next(
            (
                value
                for key, value in headers.items()
                if key.lower() == "access-control-allow-origin"
            ),
            None,
        )
        require(
            checks,
            "no_wildcard_cors",
            allow_origin != "*",
            f"access-control-allow-origin={allow_origin}",
        )
        require(
            checks,
            "health_payload",
            payload == {"status": "ok"},
            f"payload={payload}",
        )

        status, _, payload = _request(
            "POST",
            f"{base_url}/api/runs",
            payload={
                "mode": "agent",
                "prompt": "Inspect the workspace with a shell command, then reply.",
            },
        )
        require(checks, "run_accepted", status == 202, f"status={status}")
        require(
            checks,
            "run_identifier",
            isinstance(payload, dict) and isinstance(payload.get("run_id"), str),
            f"payload={payload}",
        )
        run_id = str(payload["run_id"])

        _, _, run_state = _poll(
            lambda: _request("GET", f"{base_url}/api/runs/{run_id}"),
            lambda item: (
                isinstance(item, dict) and item.get("status") == "waiting_for_approval"
            ),
            timeout=20,
        )
        pending = run_state.get("pending_approval")
        require(
            checks,
            "approval_pending",
            isinstance(pending, dict)
            and pending.get("tool_name") == "bash"
            and isinstance(pending.get("approval_id"), str),
            f"pending={pending}",
        )

        conflict_status, _, _ = _request(
            "POST",
            f"{base_url}/api/runs",
            payload={"mode": "agent", "prompt": "Second run"},
        )
        require(
            checks,
            "single_active_run",
            conflict_status == 409,
            f"status={conflict_status}",
        )

        stale_status, _, _ = _request(
            "POST",
            f"{base_url}/api/runs/{run_id}/approval",
            payload={"approval_id": "stale", "approved": True},
        )
        require(
            checks,
            "stale_approval_rejected",
            stale_status in {404, 409},
            f"status={stale_status}",
        )

        approval_status, _, _ = _request(
            "POST",
            f"{base_url}/api/runs/{run_id}/approval",
            payload={
                "approval_id": pending["approval_id"],
                "approved": True,
            },
        )
        require(
            checks,
            "approval_accepted",
            approval_status in {200, 202},
            f"status={approval_status}",
        )

        _, _, completed = _poll(
            lambda: _request("GET", f"{base_url}/api/runs/{run_id}"),
            lambda item: (
                isinstance(item, dict) and item.get("status") in {"completed", "failed"}
            ),
            timeout=20,
        )
        require(
            checks,
            "run_completed",
            completed.get("status") == "completed",
            f"state={completed}",
        )

        sse_headers, events = _read_sse(f"{base_url}/api/runs/{run_id}/events")
        content_type = next(
            (
                value
                for key, value in sse_headers.items()
                if key.lower() == "content-type"
            ),
            "",
        )
        require(
            checks,
            "sse_content_type",
            content_type.startswith("text/event-stream"),
            f"content-type={content_type}",
        )
        event_types = [event.get("type") for event in events]
        required_types = {
            "progress",
            "tool_call",
            "permission_granted",
            "tool_result",
            "final",
        }
        require(
            checks,
            "sse_event_replay",
            required_types.issubset(set(event_types)),
            f"event_types={event_types}",
        )
        require(
            checks,
            "sse_event_shape",
            all(
                isinstance(event.get("step"), int)
                and isinstance(event.get("data"), dict)
                and event.get("run_id") == run_id
                for event in events
                if event.get("type") in required_types
            ),
            "AgentEvent fields are present on replayed events",
        )

        workflow_status, _, workflow_payload = _request(
            "GET", f"{base_url}/api/workflows"
        )
        is_collection = isinstance(workflow_payload, list) or (
            isinstance(workflow_payload, dict)
            and isinstance(workflow_payload.get("items"), list)
        )
        require(
            checks,
            "workflow_collection",
            workflow_status == 200 and is_collection,
            f"status={workflow_status} payload={workflow_payload}",
        )
        observed.update(
            {
                "run_id": run_id,
                "event_types": event_types,
                "terminal_status": completed.get("status"),
            }
        )
    except Exception as exc:
        error = str(exc)
    finally:
        if server_process is not None:
            _stop_process(server_process)
        if server_log is not None:
            server_log.close()
        if fake_server is not None:
            fake_server.shutdown()
            fake_server.server_close()

    result = write_result(
        output,
        case_id="self-hosting-api-server",
        workspace=workspace,
        started_at=started_at,
        started_clock=started_clock,
        checks=checks,
        observed=observed,
        error=error,
    )
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
