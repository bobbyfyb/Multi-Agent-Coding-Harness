# Backend Acceptance Contract

The backend stage passes only when all of the following are true.

## Build And Regression

- `uv sync --locked` succeeds from the candidate Worktree.
- `uv run ruff check .` succeeds.
- `uv run pytest -q` succeeds, including all pre-existing tests.
- `python -m llm_agent.server --help` exits successfully.
- Existing CLI startup and shared runtime construction remain importable.

## Black-box API

- The server starts on an explicitly selected loopback port.
- `GET /api/health` returns HTTP 200 and `{"status": "ok"}`.
- `POST /api/runs` returns HTTP 202 without waiting for the LLM run.
- Run state progresses through queued/running and a terminal state.
- A confirm-required Bash call produces `waiting_for_approval` and a pending
  approval record instead of reading from stdin or auto-approving.
- A second run while approval is pending returns HTTP 409.
- A matching approval resumes the worker; stale approval IDs are rejected.
- A late SSE subscriber receives ordered progress, tool, permission, result,
  and final events, then the stream closes.
- `GET /api/workflows` returns a JSON collection.
- Responses do not enable wildcard CORS.

## Lifecycle And Security

- The HTTP API cannot choose an arbitrary workspace path.
- API keys and the process environment are absent from run responses and events.
- Application shutdown releases the run worker, approval waiter, MCP manager,
  and background job manager.
- Active run state is bounded and explicitly single-process for this MVP.

`checks/backend_contract.py` provides the external black-box portion with a
local deterministic OpenAI-compatible fake. The candidate cannot see this file
inside its baseline Worktree.
