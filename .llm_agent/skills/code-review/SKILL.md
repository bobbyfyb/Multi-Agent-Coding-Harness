---
name: code-review
description: Review code changes for bugs, regressions, security risks, and missing tests.
when_to_use: Use when reviewing an implementation, patch, pull request, or proposed code change.
---

# Code Review

Review the actual code and surrounding behavior before drawing conclusions.

## Workflow

1. Inspect the complete change and the nearby code it depends on.
2. Identify behavioral bugs, regressions, unsafe assumptions, and missing validation.
3. Check whether tests cover the changed behavior and important failure paths.
4. Run focused verification when the available tools and task scope allow it.
5. Report findings first, ordered by severity.

## Output

For each finding, include:

- severity;
- file and line reference;
- the concrete failure mode;
- why it matters;
- the smallest reasonable fix.

If no issues are found, say so clearly and mention remaining test gaps or residual risk.
