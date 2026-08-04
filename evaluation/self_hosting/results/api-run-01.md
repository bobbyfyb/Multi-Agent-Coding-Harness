# API Run 01 Evaluation

## Verdict

`api-run-01` failed during `engineer_implement`. The PM phase completed and
produced usable planning artifacts, but the Engineer never produced an
`implementation_report`; QA and the external backend contract therefore did
not run.

This run was still useful as a harness diagnostic. It verified worktree
isolation, workflow resume, context-length recovery, and durable failure state,
while exposing weak retry context, poor step-budget discipline, and insufficient
workspace guidance for role agents.

## Reproducibility

| Field | Value |
|---|---|
| Case | `api-server` / serial workflow |
| Target baseline | `demo-api-ui-baseline-v1` |
| Source commit | `fe17f79149066034ba68836f1c3fc3e2f30daf38` |
| Provider adapter | `anthropic` |
| Model | `qwen3-coder` |
| Memory / MCP | empty / disabled |
| Workflow ID | `run-a70e95d906c3` |
| Worktree ID | `wt_c80df9beca0f` |
| Initial harness commit | `59a85e2032ed353d3b54394934b076f9ab72e2f1` |
| Post-run harness fix | `73d03948ecd2243825d822a157e5a0799e2ce74b` |

The initial request ran from `59a85e2`. Recovery and persistence fixes were
applied between the initial request and its resume, before all fixes were
committed as `73d0394`. Consequently, this is a diagnostic run rather than a
strictly commit-pinned benchmark sample. `api-run-02` is the first clean
regression run on `73d0394`.

## Execution Summary

1. PM attempt 1 reached its 14-step limit.
2. PM attempt 2 completed in 6 steps and created a PRD, task specification, and
   implementation-plan note.
3. The initial Engineer execution reached step 16, then an upstream HTTP 500
   (`Unterminated string`) exhausted recovery and interrupted the workflow.
4. Workflow resume reused the same isolated worktree and restarted the
   Engineer phase.
5. Resumed Engineer attempt 1 used all 36 steps and ended with `max_steps`.
6. Resumed Engineer attempt 2 hit the model's 32,768-token context limit at
   step 21. Context recovery compacted the history successfully, but the agent
   still consumed all 36 steps and ended with `max_steps`.
7. The workflow persisted `status=failed`, cleared `current_phase`, and did not
   start QA.

An additional trace, `run-074b7233ddad.jsonl`, contains only one CLI input
record and no execution. It is excluded from the metrics below.

## Metrics

| Metric | Result |
|---|---:|
| Active trace duration | 813.013 s (13m 33s) |
| LLM calls | 110 attempted, 108 completed, 2 failed |
| Input tokens | 1,675,599 |
| Output tokens | 25,684 |
| Tool calls / results | 106 / 106 |
| PM attempts | 2 |
| Engineer executions | 3 (1 interrupted, 2 resumed) |
| QA attempts | 0 |
| Fix cycles | 0 |
| Artifacts | 3 planning artifacts, 0 implementation reports |

Token totals include successful recovery/compaction calls. Active duration is
the sum of the initial and resumed trace spans and excludes operator time
between the two CLI sessions.

## Implementation Inspection

The target repository remained clean. All generated code stayed in the linked
workflow worktree, confirming that isolation worked.

The worktree contains three staged files with 533 inserted lines:

- `src/llm_agent/runtime_manager.py`
- `src/llm_agent/server.py`
- `test_server_simple.py`

The implementation is not runnable or acceptable:

- Importing `llm_agent.server` fails because `fastapi` was not added to project
  dependencies.
- It imports modules and classes that do not exist in the baseline, including
  `artifact_store`, `trace_store`, and `SerialWorkflowRunner`.
- `get_shared_runtime()` recursively calls itself after shadowing the imported
  function.
- Core Agent and Workflow execution paths contain placeholders.
- The CLI composition, `pyproject.toml`, `uv.lock`, README, and focused test
  suite were not updated.
- Ruff reports 37 errors.

Because the server cannot import and the workflow never reached QA, the
external backend contract was correctly left unexecuted rather than reported
as a pass.

## Harness Findings

The run motivated the following changes now present in `73d0394`:

- persist workflow failure when a role agent raises an exception;
- allow a larger elapsed retry budget for long model requests;
- load `.env` before runtime configuration is read;
- give Engineer and QA the full task-management tools;
- start phase retries with fresh context plus the corrective failure reason;
- warn role agents when six steps remain and again on the final step;
- strengthen Engineer instructions around API inspection, task planning, and
  verification evidence;
- prevent shell `cd` and `pushd` from escaping the configured workspace.

The post-run fix commit previously passed all 188 tests and Ruff. During
`api-run-02` preparation, the 71 directly affected Agent, tool, recovery, hook,
and serial-workflow tests passed again, and Ruff remained clean. A redundant
full-suite rerun was stopped after it stalled in the current sandbox, with no
failure reported before the stall.

## Run 02 Gates

`api-run-02` should use the same target baseline, task text, workflow config,
skills, provider, and model. It passes only when all of the following hold:

- the workflow reaches `completed` with an approved QA verdict;
- Engineer creates an `implementation_report` backed by changed-file and test
  evidence;
- the server imports and starts from the ready worktree;
- project lint and focused tests pass;
- `checks/backend_contract.py` passes externally;
- all target changes remain isolated in the workflow worktree.
