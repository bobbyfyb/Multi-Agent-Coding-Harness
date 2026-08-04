from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import subprocess
from time import perf_counter
from typing import Any, Sequence


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str


class ContractError(RuntimeError):
    pass


def require(checks: list[Check], name: str, condition: bool, detail: str) -> None:
    checks.append(Check(name=name, passed=condition, detail=detail))
    if not condition:
        raise ContractError(f"{name}: {detail}")


def run_command(
    checks: list[Check],
    name: str,
    command: Sequence[str],
    *,
    cwd: Path,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        list(command),
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    detail = f"exit={result.returncode} command={' '.join(command)}"
    checks.append(Check(name=name, passed=result.returncode == 0, detail=detail))
    if result.returncode != 0:
        output = (result.stdout + "\n" + result.stderr)[-4_000:]
        raise ContractError(f"{name} failed:\n{output}")
    return result


def git_value(workspace: Path, *arguments: str) -> str | None:
    result = subprocess.run(
        ["git", *arguments],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _evaluation_provenance(workspace: Path) -> dict[str, Any]:
    for root in (workspace, *workspace.parents):
        path = root / ".llm_agent" / "evaluation.json"
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}
    return {}


def candidate_metadata(workspace: Path) -> dict[str, Any]:
    patch = subprocess.run(
        ["git", "diff", "--cached", "--binary"],
        cwd=workspace,
        capture_output=True,
        check=False,
    ).stdout
    provenance = _evaluation_provenance(workspace)
    return {
        "source_commit": git_value(workspace, "rev-parse", "HEAD"),
        "baseline_ref": provenance.get("baseline_ref"),
        "baseline_source_commit": provenance.get("source_commit"),
        "skill_revisions": provenance.get("skill_revisions", {}),
        "changed_files": (
            git_value(workspace, "diff", "--cached", "--name-only") or ""
        ).splitlines(),
        "patch_sha256": sha256(patch).hexdigest(),
    }


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_result(
    output: Path,
    *,
    case_id: str,
    workspace: Path,
    started_at: str,
    started_clock: float,
    checks: list[Check],
    observed: dict[str, Any],
    error: str | None,
) -> dict[str, Any]:
    finished_at = utc_now()
    passed = error is None and all(check.passed for check in checks)
    result = {
        "schema_version": 1,
        "case_id": case_id,
        "status": "passed" if passed else "failed",
        "workspace": str(workspace),
        "baseline_ref": observed.get("baseline_ref"),
        "source_commit": observed.get("source_commit"),
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": round(perf_counter() - started_clock, 3),
        "checks": [asdict(check) for check in checks],
        "observed": observed,
        "error": error,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result
