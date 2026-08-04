from __future__ import annotations

import argparse
import json
from pathlib import Path
from time import perf_counter

from _common import (
    Check,
    candidate_metadata,
    require,
    run_command,
    utc_now,
    write_result,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run deterministic checks for the self-hosting web console."
    )
    parser.add_argument("workspace", type=Path, help="Candidate Worktree path.")
    parser.add_argument("--output", type=Path, help="Result JSON path.")
    parser.add_argument(
        "--skip-install",
        action="store_true",
        help="Skip npm ci when node_modules is already prepared.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    workspace = args.workspace.expanduser().resolve()
    webdir = workspace / "web"
    output = args.output or workspace / ".llm_agent/evaluation/frontend-result.json"
    checks: list[Check] = []
    observed = candidate_metadata(workspace) if workspace.is_dir() else {}
    started_at = utc_now()
    started_clock = perf_counter()
    error: str | None = None

    try:
        require(checks, "workspace_exists", workspace.is_dir(), str(workspace))
        package_path = webdir / "package.json"
        lock_path = webdir / "package-lock.json"
        require(checks, "package_json", package_path.is_file(), str(package_path))
        require(checks, "package_lock", lock_path.is_file(), str(lock_path))

        package = json.loads(package_path.read_text(encoding="utf-8"))
        scripts = package.get("scripts") or {}
        required_scripts = {"build", "typecheck", "test"}
        require(
            checks,
            "required_scripts",
            required_scripts.issubset(scripts),
            f"scripts={sorted(scripts)}",
        )

        if not args.skip_install:
            run_command(
                checks,
                "npm_ci",
                ["npm", "ci"],
                cwd=webdir,
                timeout=300,
            )
        run_command(
            checks,
            "typecheck",
            ["npm", "run", "typecheck"],
            cwd=webdir,
            timeout=120,
        )
        run_command(
            checks,
            "unit_tests",
            ["npm", "test", "--", "--run"],
            cwd=webdir,
            timeout=180,
        )
        run_command(
            checks,
            "production_build",
            ["npm", "run", "build"],
            cwd=webdir,
            timeout=180,
        )
        dist_index = webdir / "dist" / "index.html"
        require(checks, "dist_index", dist_index.is_file(), str(dist_index))
        observed["frontend"] = {
            "scripts": sorted(scripts),
            "dist_index_bytes": dist_index.stat().st_size,
        }
    except Exception as exc:
        error = str(exc)

    result = write_result(
        output,
        case_id="self-hosting-web-console",
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
