# Self-hosting Demo Evaluation

This evaluation asks the coding harness to build its own local API server and
web console. The target repository is exported from the fixed
`demo-api-ui-baseline-v1` tag, while task definitions and external checks stay
outside the target workspace.

## Goals

- Produce a repeatable self-hosting demo from a known CLI-only baseline.
- Keep the running orchestrator isolated from the code being modified.
- Evaluate behavior with external contracts instead of role-authored reports.
- Capture traces, artifacts, diffs, verification evidence, cost, and duration.

## Stages

1. `tasks/01_api_server.md`: shared runtime composition, FastAPI, SSE events,
   web approvals, workflow resume, and backend tests.
2. Apply the accepted backend patch to a branch created from the baseline and
   commit it.
3. `tasks/02_web_console.md`: React and TypeScript console consuming the API.
4. Run the external frontend contract and browser smoke test.

The second stage must start from the accepted backend commit, not directly from
the CLI-only baseline.

## Prepare A Clean Target

From the harness repository:

```bash
uv run python evaluation/self_hosting/prepare_workspace.py \
  evaluation/self_hosting/workspaces/api-run-01
```

The preparation script:

- exports only files reachable from the baseline tag;
- initializes a new one-commit Git repository;
- installs fixed workflow and MCP-disabled runtime configuration;
- copies the pinned local skills without nested Git metadata;
- starts with empty memory;
- records the harness commit, branch, runtime dirty diff, and preparation script hash;
- writes provenance to `.llm_agent/evaluation.json`.

Run the current harness against that target while keeping the target as the
process working directory:

```bash
cd evaluation/self_hosting/workspaces/api-run-01
HARNESS_ROOT=/path/to/LLM-agent
"$HARNESS_ROOT/.venv/bin/python" "$HARNESS_ROOT/main.py"
```

Paste the contents under `Workflow Prompt` from the relevant task file into the
CLI. Do not copy the acceptance or external-check files into the target.

## External Verification

After the backend workflow completes, find its `ready` Worktree path in:

```text
<target>/.llm_agent/worktree-state/<worktree_id>.json
```

Then run:

```bash
uv run python evaluation/self_hosting/checks/backend_contract.py \
  <ready-worktree-path>
```

For the frontend stage:

```bash
uv run python evaluation/self_hosting/checks/frontend_contract.py \
  <ready-worktree-path>
```

Raw result JSON belongs under `evaluation/self_hosting/results/raw/` and is
ignored by Git. Curated aggregate tables and conclusions should be committed
later as a separate evaluation report.

## Repeatability Rules

- Record provider, exact model ID, baseline ref, source commit, and skill pins.
- Use the same task text and workflow configuration for every compared run.
- Keep Memory empty and external MCP disabled for core comparisons.
- Use the disposable target only when auto-approving evaluation setup actions.
- Run Single Agent and Serial Workflow from separate fresh targets.
- Repeat each mode at least three times before reporting pass rates.
- Treat external contract results as ground truth; artifacts are supporting
  evidence, not the final verdict.

The baseline tag is local until it is explicitly pushed:

```bash
git push origin demo-api-ui-baseline-v1
```
