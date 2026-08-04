from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
from typing import Sequence


BASELINE_REF = "demo-api-ui-baseline-v1"
BASELINE_COMMIT = "fe17f79149066034ba68836f1c3fc3e2f30daf38"
PRD_WRITER_COMMIT = "a51d7c850360fe45b1d3a6ccd716c3601d3d08e8"
CODE_REVIEW_SHA256 = "e61b095f887f91c47ad4a820744c1a4840996280168bf0718eeae75313a4d1ec"
HARNESS_RUNTIME_PATHS = (
    "main.py",
    "src",
    "pyproject.toml",
    "uv.lock",
    "evaluation/self_hosting/config",
)


class PreparationError(RuntimeError):
    pass


def _run(
    command: Sequence[str],
    *,
    cwd: Path,
    text: bool = True,
) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        list(command),
        cwd=cwd,
        check=True,
        capture_output=True,
        text=text,
    )


def _git_text(repo: Path, *arguments: str) -> str:
    result = _run(["git", *arguments], cwd=repo)
    return str(result.stdout).strip()


def _safe_remove_target(target: Path, repo_root: Path) -> None:
    protected = {
        Path("/").resolve(),
        Path.home().resolve(),
        repo_root.resolve(),
        repo_root.parent.resolve(),
    }
    if target.resolve() in protected:
        raise PreparationError(f"Refusing to remove protected path: {target}")
    shutil.rmtree(target)


def _export_ref(repo_root: Path, source_ref: str, target: Path) -> str:
    source_commit = _git_text(repo_root, "rev-parse", f"{source_ref}^{{}}")
    archive = _run(
        ["git", "archive", "--format=tar", source_ref],
        cwd=repo_root,
        text=False,
    ).stdout
    if not isinstance(archive, bytes):
        raise PreparationError("git archive did not return binary data.")

    target.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as bundle:
        bundle.extractall(target, filter="data")

    leaked_checks = target / "evaluation" / "self_hosting" / "checks"
    if leaked_checks.exists():
        raise PreparationError(
            "The selected source ref contains external evaluation checks. "
            "Use a ref derived from the frozen baseline without evaluation files."
        )
    return source_commit


def _validate_skill_sources(repo_root: Path) -> dict[str, str]:
    skills_root = repo_root / ".llm_agent" / "skills"
    prd_writer = skills_root / "prd-writer"
    code_review = skills_root / "code-review" / "SKILL.md"
    if not prd_writer.is_dir():
        raise PreparationError(f"Missing required skill: {prd_writer}")
    if not code_review.is_file():
        raise PreparationError(f"Missing required skill: {code_review}")

    prd_commit = _git_text(prd_writer, "rev-parse", "HEAD")
    if prd_commit != PRD_WRITER_COMMIT:
        raise PreparationError(
            "prd-writer revision drifted: "
            f"expected {PRD_WRITER_COMMIT}, found {prd_commit}"
        )

    code_review_hash = sha256(code_review.read_bytes()).hexdigest()
    if code_review_hash != CODE_REVIEW_SHA256:
        raise PreparationError(
            "code-review skill drifted: "
            f"expected {CODE_REVIEW_SHA256}, found {code_review_hash}"
        )
    return {
        "prd-writer": prd_commit,
        "code-review": code_review_hash,
    }


def _copy_runtime_bundle(
    repo_root: Path,
    target: Path,
    *,
    source_ref: str,
    source_commit: str,
    skill_revisions: dict[str, str],
) -> None:
    evaluation_root = repo_root / "evaluation" / "self_hosting"
    runtime_root = target / ".llm_agent"
    runtime_root.mkdir(parents=True, exist_ok=True)
    shutil.copy2(
        evaluation_root / "config" / "workflow.yaml",
        runtime_root / "workflow.yaml",
    )
    shutil.copy2(
        evaluation_root / "config" / "mcp.yaml",
        runtime_root / "mcp.yaml",
    )

    target_skills = runtime_root / "skills"
    source_skills = repo_root / ".llm_agent" / "skills"
    for name in ("prd-writer", "code-review"):
        shutil.copytree(
            source_skills / name,
            target_skills / name,
            ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"),
            dirs_exist_ok=True,
        )

    (runtime_root / "memory").mkdir()
    harness_diff = _run(
        ["git", "diff", "--binary", "HEAD", "--", *HARNESS_RUNTIME_PATHS],
        cwd=repo_root,
        text=False,
    ).stdout
    if not isinstance(harness_diff, bytes):
        raise PreparationError("git diff did not return binary data.")
    metadata = {
        "schema_version": 1,
        "baseline_ref": source_ref,
        "source_commit": source_commit,
        "harness_commit": _git_text(repo_root, "rev-parse", "HEAD"),
        "harness_branch": _git_text(repo_root, "branch", "--show-current"),
        "harness_dirty": bool(harness_diff),
        "harness_diff_sha256": (
            sha256(harness_diff).hexdigest() if harness_diff else None
        ),
        "preparation_script_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        "prepared_at": datetime.now(timezone.utc).isoformat(),
        "memory": "empty",
        "mcp": "disabled",
        "skill_revisions": skill_revisions,
    }
    (runtime_root / "evaluation.json").write_text(
        json.dumps(metadata, ensure_ascii=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _initialize_repository(target: Path, source_ref: str, source_commit: str) -> None:
    _run(["git", "init", "-b", "main"], cwd=target)
    _run(["git", "config", "user.name", "LLM Agent Evaluation"], cwd=target)
    _run(["git", "config", "user.email", "evaluation@local"], cwd=target)
    _run(["git", "add", "-A"], cwd=target)
    _run(
        [
            "git",
            "commit",
            "-m",
            f"Evaluation baseline from {source_ref} ({source_commit[:12]})",
        ],
        cwd=target,
    )


def prepare_workspace(target: Path, *, source_ref: str, force: bool) -> Path:
    repo_root = Path(__file__).resolve().parents[2]
    target = target.expanduser().resolve()
    if target.exists():
        if not force:
            raise PreparationError(
                f"Target already exists: {target}. Pass --force to replace it."
            )
        _safe_remove_target(target, repo_root)

    skill_revisions = _validate_skill_sources(repo_root)
    try:
        source_commit = _export_ref(repo_root, source_ref, target)
        if source_ref == BASELINE_REF and source_commit != BASELINE_COMMIT:
            raise PreparationError(
                f"Baseline tag moved: expected {BASELINE_COMMIT}, found {source_commit}"
            )
        _initialize_repository(target, source_ref, source_commit)
        _copy_runtime_bundle(
            repo_root,
            target,
            source_ref=source_ref,
            source_commit=source_commit,
            skill_revisions=skill_revisions,
        )
    except Exception:
        if target.exists():
            shutil.rmtree(target)
        raise
    return target


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prepare a clean target repository for the self-hosting demo."
    )
    parser.add_argument("target", type=Path, help="Directory to create.")
    parser.add_argument(
        "--ref",
        default=BASELINE_REF,
        help="Source Git ref derived from the frozen baseline.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing non-protected target directory.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        target = prepare_workspace(args.target, source_ref=args.ref, force=args.force)
    except (PreparationError, subprocess.CalledProcessError) as exc:
        print(f"Preparation failed: {exc}", file=sys.stderr)
        return 1

    repo_root = Path(__file__).resolve().parents[2]
    print(f"Prepared: {target}")
    print(f"Baseline: {_git_text(target, 'log', '-1', '--format=%s')}")
    print("Run the harness from the prepared directory with:")
    print(f"  {sys.executable} {repo_root / 'main.py'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
