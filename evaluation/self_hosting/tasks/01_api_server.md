# Stage 1: Local Agent API Server

## Workflow Prompt

```text
/workflow Implement a local FastAPI server for this coding harness.

Refactor the existing CLI composition so the CLI and HTTP server share one
runtime construction and shutdown path instead of duplicating initialization.
Keep all existing CLI behavior working.

The server must run with:
  python -m llm_agent.server --host 127.0.0.1 --port <port>

Implement these HTTP contracts:
- GET /api/health
- POST /api/runs with {"mode": "agent" | "workflow", "prompt": string}
- GET /api/runs/{run_id}
- GET /api/runs/{run_id}/events as replayable Server-Sent Events
- POST /api/runs/{run_id}/approval
- GET /api/workflows
- POST /api/workflows/{workflow_id}/resume
- GET /api/artifacts/{artifact_id}

POST /api/runs returns HTTP 202 with a run_id and queued status. Run execution
must not block the request thread. Each SSE data frame must be JSON containing
the existing AgentEvent fields, including type, step, data, agent_id, run_id,
parent_run_id, depth, and duration_ms. Event history must be replayed to late
subscribers, and a completed or failed stream must close after replay.

Support one active run at a time. A second start request while a run is queued,
running, or waiting for approval must return HTTP 409. Store only bounded event
history and completed run metadata in memory; continue using existing Artifact,
Trace, and Workflow stores for durable project data.

Do not auto-approve confirm-required tools. Add a server-side ApprovalProvider
that places the run in waiting_for_approval with a pending approval id, tool
name, arguments, and reason. POST /api/runs/{run_id}/approval accepts
{"approval_id": string, "approved": boolean}, wakes the worker, and rejects
stale or mismatched decisions. Denial must flow through the existing permission
hook as a normal tool result.

Bind to localhost by default, do not accept an arbitrary workspace path from
HTTP clients, do not expose environment variables or API keys, and do not use a
wildcard CORS policy. Shut down background jobs, MCP sessions, blocked approval
waiters, and worker resources cleanly during application shutdown.

Use a background thread for the existing synchronous Agent and Workflow loops,
and bridge callbacks safely into the server event stream. Do not rewrite the
core loop as async in this task. Add focused tests for runtime reuse, run state,
event ordering and replay, approval allow/deny, conflict handling, failures,
and shutdown. Update project dependencies, lockfile, and README instructions.
```

## Scope Boundary

This stage does not implement a browser UI, authentication, multi-process run
coordination, a database, arbitrary concurrent runs, or remote deployment.
