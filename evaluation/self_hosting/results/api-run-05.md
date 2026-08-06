# API Run 05 Evaluation

## Verdict

`api-run-05` failed after two Engineer fix cycles. The final QA verdict was
`fail`, and the PM Acceptance phase also failed its evidence gate because the
acceptance report combined `metadata.verdict=fail` with `status=accepted`.

The run is nevertheless a useful regression result for the harness changes
made after `api-run-04`. It confirms that QA machine evidence overrides a
role-authored pass, fix-cycle run IDs remain unique, the PM Acceptance gate
rejects a verdict/status conflict, and project dependency failures are detected
inside the target Worktree environment. The candidate API implementation itself
is not runnable and does not satisfy the external backend contract.

## Reproducibility

| Field | Value |
|---|---|
| Case | `api-server` / serial workflow |
| Target baseline | `demo-api-ui-baseline-v1` |
| Baseline source commit | `fe17f79149066034ba68836f1c3fc3e2f30daf38` |
| Target repository commit | `510b361c791445711ee407227e9cd5daea2d2d6d` |
| Provider adapter | `anthropic` |
| Model | `qwen3-coder` |
| Memory / MCP | empty / disabled |
| Workflow ID | `run-8e00a5513de0` |
| Worktree ID | `wt_d67834ec97c2` |
| Recorded harness commit | `41868afc624056e2bf9769a3ecacd274f7368bbe` |
| Recorded harness dirty diff | `6fcf862ab036aeffda7af35b54806dffd02fae1605aa2bb368f21921f07e5ce9` |
| Equivalent committed harness tree | `a1da51d678e5f390fd4b73c1f8f182568eb69ed4` |

The workspace was prepared while the harness changes were still uncommitted,
so `.llm_agent/evaluation.json` records commit `41868af` plus a dirty runtime
diff. That exact diff hash matches the runtime-path diff from `41868af` to
`a1da51d`; the evaluated harness content is therefore reproducible from
`a1da51d` even though the preparation metadata predates that commit.

The target repository remained clean. All candidate changes stayed in the
workflow Worktree, whose saved patch and staged diff both have SHA-256
`184f8fecd9ec9b0d9c65ad7aecdbf9d21f753ee8ccdf986529891332c63c78ff`.

## Workflow Summary

| Phase | Attempts | Latest agent status | Phase result | Evidence result |
|---|---:|---|---|---|
| PM planning | 1 | `completed` | completed | PRD and TaskSpec created |
| Engineer implementation | 2 | `max_steps` | completed | retry supplied the missing ImplementationReport |
| QA verification | 1 | `completed` | completed | reported pass overridden to effective fail |
| Engineer fix 1 | 1 | `max_steps` | completed | candidate tests added |
| QA regression 1 | 1 | `max_steps` | completed | fail |
| Engineer fix 2 | 1 | `completed` | completed | one constructor call corrected |
| QA regression 2 | 1 | `max_steps` | completed | fail |
| PM acceptance | 2 | `max_steps` | failed | verdict/status conflict remained |

The authoritative final state in both the Workflow record and the last
`workflow.completed` Trace event is:

```text
status=failed
qa_verdict=fail
fix_cycles=2
current_phase=null
current_phase_key=null
```

All ten role-run IDs are unique. In particular, the two fix cycles use distinct
IDs:

```text
run-8e00a5513de0-engineer_fix_1-1
run-8e00a5513de0-qa_regression_1-1
run-8e00a5513de0-engineer_fix_2-1
run-8e00a5513de0-qa_regression_2-1
```

Each phase-local `data.agent_status` matches its latest handoff rather than an
older attempt. The record has no top-level active `agent_status`, and the
current phase is cleared. Phase-local historical statuses remain intentionally
available as evidence; several phases completed with `agent_status=max_steps`
because their Artifact/evidence gate had already been satisfied.

## Gate Behavior

The initial QA artifact (`artifact_0004`) claimed `metadata.verdict=pass`.
Machine evidence from the same attempt contained five successful checks and one
failed `run_tests` invocation. The orchestrator correctly persisted:

```text
reported_verdict=pass
effective_verdict=fail
verdict_overridden=true
```

Both later QA regression reports explicitly returned `fail`. The final
AcceptanceReport (`artifact_0009`) correctly described the implementation as
blocked and set `metadata.verdict=fail`, but then set the artifact status to
`accepted`. PM Acceptance attempt 1 completed at the agent level but was
rejected by the evidence gate. Attempt 2 received the precise conflict in its
working context, tried the unsupported artifact status `rejected`, and reached
its 12-step limit without correcting the report. The workflow therefore did
not turn a QA failure into an accepted result.

## Metrics

| Metric | Result |
|---|---:|
| Wall-clock duration | 3,875.810 s (64m 35.8s) |
| Trace records | 4,133, continuous sequence 1-4,133 |
| Completed LLM calls | 251 |
| Failed LLM calls | 1 context-length call, recovered |
| Provider attempts | 255, including three internal retries |
| Input tokens | 4,455,644 |
| Output tokens | 44,504 |
| Tool calls / results | 236 / 236 |
| Role executions | 10: 4 completed, 6 max-steps |
| Context compactions | 143: 142 preflight, 1 reactive |
| Workflow handoffs / retry injections | 10 / 2 |
| Artifacts | 9 |
| Candidate patch | 7 files, 709 insertions |

The model's actual context limit was 32,768 tokens while the evaluation
workflow configuration declared 100,000. PM planning triggered one reactive
context-length recovery. The other 142 compaction events were preflight tool
result micro-compactions; only one compaction created an LLM summary and none
hard-trimmed the conversation.

Artifact context was injected on 232 of 241 role steps, with 882 artifact
references in total. Together with repeated long role runs, this contributed to
the 4.46 million input-token cost. The run spent most of its budget in three
36-step Engineer executions and two 24-step QA regressions.

The Trace JSONL is internally consistent, but its generated Markdown header is
not: it reports `Status: warning` and `Duration: 30770.552 ms` from the final
child role run instead of the root workflow's failed status and full duration.
The Workflow record and final `workflow.completed` event must be treated as the
authority for this run.

## Short-Term Context and Memory

The workflow persisted one bounded handoff per role attempt. It injected retry
working context twice:

1. Engineer implementation attempt 2 received the six existing changed files
   and the missing-ImplementationReport gate failure. It continued in the
   existing Worktree and produced `artifact_0003`, so this handoff was useful.
2. PM Acceptance attempt 2 received the exact accepted/fail conflict. It
   recognized the issue, but spent most of the retry exploring code and called
   unavailable `run_tests` before trying an invalid artifact status. This was
   only partially useful.

Long-term Memory remained correctly unused: the Memory directory has no files,
there were no `memory_search` or `memory_get` calls, all 241 MemoryContextHook
results injected nothing, and the final `reflected_memory_ids` list is empty.
The failed run therefore did not pollute project Memory; retry continuity came
from Workflow handoffs and normal context management.

## Dependency and Environment Handling

Structured validation invoked the target environment with:

```text
uv run --locked --no-env-file python -m pytest ...
uv run --locked --no-env-file ruff check ...
```

It did not reuse the harness virtual environment. The first broad test run
reported a structured project dependency failure:

```text
error_kind=missing_dependency
missing_modules=["serpapi"]
diagnostic="No module named 'serpapi'"
```

The Engineer first attempted `pip install serpapi`; the permission hook rejected
that shared-environment mutation. It then used `uv add serpapi` followed by
`uv add fastapi uvicorn`, updating both `pyproject.toml` and `uv.lock`. Later
structured QA runs no longer reported the missing module. No `lock_outdated` or
`environment_setup_failed` result occurred.

This validates the new environment isolation and dependency classification.
It also exposes two benchmark-quality issues:

- The frozen baseline itself imports `serpapi` without declaring it, so an
  unrelated dependency repair consumed evaluation steps and changed the patch.
- Some role-authored Bash checks used bare `python -m pytest`, which selected the
  system interpreter and reproduced misleading dependency errors. Only the
  structured validation results should be used as workflow evidence.

The external checker still inherited the harness `VIRTUAL_ENV`; `uv` ignored the
mismatched environment and used the candidate Worktree `.venv`, so this did not
change the result, but clearing `VIRTUAL_ENV` in the checker would make its logs
and isolation contract cleaner.

## Candidate Inspection

The candidate staged these files:

- `pyproject.toml`
- `uv.lock`
- `src/llm_agent/server/__init__.py`
- `src/llm_agent/server/_minimal_server.py`
- `src/llm_agent/server/cli.py`
- `src/llm_agent/server/server.py`
- `tests/test_server_integration.py`

It did not modify `main.py`, the existing CLI composition, or `README.md`.
Consequently, the claimed shared runtime refactor and documentation update did
not happen. Static inspection found additional blocking defects:

- no `llm_agent/server/__main__.py`, so the required module command has no
  entry point;
- runtime managers, `Agent`, and `SerialCodingWorkflow` are constructed with
  nonexistent or missing arguments;
- health returns `{"status":"healthy"}` instead of `{"status":"ok"}`;
- `POST /api/runs` uses FastAPI's default HTTP 200 instead of 202, and queued
  conflict protection has a race before `active_run_id` is set;
- the events route builds a generator but returns an ordinary Pydantic object,
  not a streaming `text/event-stream` response;
- even that unused generator calls nonexistent `AgentEvent.json()`, never emits
  newly appended events, and produces an incomplete terminal frame;
- event and completed-run storage is unbounded;
- the ApprovalProvider always returns false without registering, blocking, or
  waking an approval waiter; the endpoint ignores the allow/deny value, does
  not bind an approval to its run, and returns the wrong stale-ID status;
- workflow listing/resume and artifact retrieval are placeholders, and the
  workflow-list response shape does not match the external checker;
- worker results are unconditionally marked completed even when the Agent or
  Workflow returns a failed, incomplete, or max-step result;
- shutdown calls a nonexistent MCP `shutdown()` method and does not release all
  requested resources;
- raw AgentEvents are retained and returned without a response-level secret
  sanitizer;
- the added tests largely assert the placeholder behavior and do not cover SSE,
  real approval allow/deny, runtime reuse, failure propagation, or shutdown.

The patch also has 33 trailing-whitespace errors.

## External Backend Contract

The formal result is stored in ignored raw output as
`evaluation/self_hosting/results/raw/api-run-05.json`:

1. `workspace_exists` passed.
2. `uv sync --locked` passed.
3. Ruff failed on five unused imports in `tests/test_server_integration.py`.
4. The checker stopped before pytest and HTTP black-box checks, as designed.

A second diagnostic run with `--skip-regression`, stored as
`api-run-05-contract-only.json`, reached `server_help` and failed because
`python -m llm_agent.server` could not find `llm_agent.server.__main__`.
Therefore the server never started and none of the health, run state, SSE,
approval, conflict, or workflow-list HTTP checks executed.

An independent run of the checker's pytest command, `uv run pytest -q`, also
failed during collection because the candidate test imports
`src.llm_agent.server` instead of the installed `llm_agent.server` package.

The external backend verdict is unambiguously **failed**.

## Harness Findings and Next Actions

Validated in this run:

- QA machine evidence overrides an unsupported pass claim.
- PM Acceptance verdict/status consistency is enforced.
- Fix-cycle role run IDs are unique.
- Workflow terminal state clears the active phase.
- Worktree isolation preserves a clean target repository.
- Structured validation uses the locked target environment and classifies a
  missing project dependency.
- Failed workflows do not trigger long-term Memory reflection.

Recommended follow-up order:

1. Fix Trace Markdown root-status and root-duration selection.
2. Make PM Acceptance corrective context state the valid artifact statuses (or
   deterministically normalize a fail report away from `accepted`).
3. Pin the evaluation context budget to the actual 32,768-token model window
   and reduce repeated full Artifact context injection.
4. Repair or replace the frozen baseline so its locked dependency set passes
   before the evaluated task begins.
5. Clear `VIRTUAL_ENV` in the external checker and align its pytest invocation
   with the structured validation command.
6. Avoid substring-based Bash denials: this run rejected a harmless diagnostic
   command merely because an echoed sentence contained the word `shutdown`.
7. Strengthen the Engineer/QA backend workflow guidance or required skill so
   placeholder implementations cannot consume two full fix cycles without a
   module-entrypoint smoke test.
