# Frontend Acceptance Contract

The frontend stage passes only when all of the following are true.

## Deterministic Commands

- `web/package-lock.json` is committed.
- `npm ci` succeeds.
- `npm run typecheck` succeeds.
- `npm test -- --run` succeeds.
- `npm run build` succeeds and produces `web/dist/index.html`.

## User Workflow

- A user can start Agent and Workflow runs from a multiline prompt.
- The timeline renders ordered progress, tool calls, permission decisions, tool
  results, recovery events, and final output without duplicate replay events.
- PM, Engineer, QA, acceptance, and nested Agent events remain distinguishable.
- Pending approvals show tool name, arguments, reason, and explicit Allow/Deny.
- Artifacts, verification evidence, Diff, failures, and final output are
  inspectable without opening raw runtime files.
- Persisted incomplete workflows can be listed and resumed.

## Presentation And Integration

- The console is usable at 1280x800 and 390x844 with no incoherent overlap.
- Long payloads are collapsed and do not shift the primary layout.
- Keyboard focus and form labels are usable.
- The development server proxies `/api`; the production FastAPI process serves
  the built app without intercepting `/api` routes.
- No auto-approval control, wildcard CORS, arbitrary workspace selector, or
  secret-bearing response is introduced.

The first external checker validates deterministic build commands. A Playwright
browser contract will be added after the backend stage fixes the final response
shapes, before the frontend Workflow is started.
