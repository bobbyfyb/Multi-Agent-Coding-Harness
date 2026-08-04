# Stage 2: Agent Web Console

## Workflow Prompt

```text
/workflow Implement a React and TypeScript web console for the existing local
Agent API.

Create a Vite frontend under web/ with committed package-lock.json. The primary
screen must be the working console, not a landing page. Provide a compact
developer-tool layout with a run list, multiline prompt composer, Agent versus
Workflow mode selector, live event timeline, workflow phase status, and a
details area for Artifacts, verification evidence, Worktree Diff, errors, and
the final response.

Consume GET /api/runs/{run_id}/events with Server-Sent Events. Render progress,
tool_call, permission, tool_result, recovery, and final events distinctly, with
role prefixes for PM, Engineer, QA, and acceptance. Preserve event order, show
connection and terminal states, reconnect without duplicating replayed events,
and keep long tool payloads collapsed by default.

When a run enters waiting_for_approval, show the tool, arguments, and reason,
and provide explicit Allow and Deny actions through the approval endpoint.
Support starting Agent and Workflow runs, listing persisted workflows, and
resuming an incomplete workflow. Do not add an auto-approve control.

Use accessible semantic controls and lucide icons. Keep cards shallow, avoid a
marketing hero, decorative gradients, and oversized typography. The layout must
remain usable at 1280x800 and 390x844 without overlapping text or controls.

Provide npm scripts named build, typecheck, and test. Add component tests for
event rendering, replay deduplication, terminal states, approval interactions,
and API errors. Configure a development proxy for /api. In production, let the
FastAPI server serve the built frontend while preserving all /api routes and
localhost-only defaults. Update the README with development and production
commands.
```

## Scope Boundary

This stage does not add user accounts, cloud hosting, analytics, collaborative
sessions, or a second persistence system.
