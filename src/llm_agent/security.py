from __future__ import annotations

import os
from pathlib import Path
import shlex
from typing import Iterable


DEFAULT_PATH = "/usr/local/bin:/usr/bin:/bin"

SENSITIVE_DIR_NAMES = {
    ".aws",
    ".git",
    ".gcloud",
    ".kube",
    ".llm_agent",
    ".ssh",
}
SENSITIVE_FILE_NAMES = {
    ".env",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "credentials",
    "credentials.json",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
}
SENSITIVE_SUFFIXES = (
    ".env",
    ".key",
    ".pem",
    ".p12",
    ".pfx",
)
SENSITIVE_GLOB_EXCLUDES = (
    "!**/.aws/**",
    "!**/.git/**",
    "!**/.gcloud/**",
    "!**/.kube/**",
    "!**/.llm_agent/**",
    "!**/.ssh/**",
    "!**/.env",
    "!**/*.env",
    "!**/*.key",
    "!**/*.pem",
    "!**/*.p12",
    "!**/*.pfx",
    "!**/.netrc",
    "!**/.npmrc",
    "!**/.pypirc",
)

HARD_DENY_COMMAND_FRAGMENTS = (
    "rm -rf /",
    "rm -rf /*",
    "sudo",
    "shutdown",
    "reboot",
    "mkfs",
    "dd if=",
    "> /dev/sda",
)

PYTHON_ENV_MUTATIONS = {"install", "uninstall"}
CONDA_ENV_MUTATIONS = {"install", "remove", "uninstall", "update"}

SECRET_ENV_MARKERS = (
    "API_KEY",
    "AUTH",
    "CREDENTIAL",
    "PASSWORD",
    "SECRET",
    "TOKEN",
)
PASSTHROUGH_ENV_KEYS = {
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "PATH",
    "TERM",
    "TMPDIR",
    "TZ",
    "VIRTUAL_ENV",
}


def resolve_workspace_path(
    workdir: Path | str,
    path: str,
    *,
    allow_sensitive: bool = False,
) -> Path:
    workspace = Path(workdir).resolve()
    candidate = (workspace / path).resolve()
    if not candidate.is_relative_to(workspace):
        raise ValueError(f"Path escapes workspace: {path}")
    if not allow_sensitive and is_sensitive_workspace_path(workspace, candidate):
        raise ValueError(f"Sensitive path is blocked: {path}")
    return candidate


def is_sensitive_workspace_path(workdir: Path | str, path: Path | str) -> bool:
    workspace = Path(workdir).resolve()
    candidate = Path(path).resolve()
    if not candidate.is_relative_to(workspace):
        return True
    relative = candidate.relative_to(workspace)
    parts = [part.casefold() for part in relative.parts]
    if any(part in SENSITIVE_DIR_NAMES for part in parts):
        return True
    name = candidate.name.casefold()
    return name in SENSITIVE_FILE_NAMES or name.endswith(SENSITIVE_SUFFIXES)


def sensitive_glob_excludes() -> list[str]:
    return list(SENSITIVE_GLOB_EXCLUDES)


def safe_subprocess_env(cwd: Path | str | None = None) -> dict[str, str]:
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        if _is_safe_env_key(key):
            env[key] = value
    env.setdefault("PATH", DEFAULT_PATH)
    if cwd is not None:
        env["HOME"] = str(Path(cwd).resolve())
    env["NO_COLOR"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    return env


def validate_shell_command(
    command: str,
    *,
    fragments: Iterable[str] | None = None,
    workdir: Path | str | None = None,
) -> None:
    if not command.strip():
        raise ValueError("Command is required.")
    command_for_match = command.casefold()
    for fragment in fragments or HARD_DENY_COMMAND_FRAGMENTS:
        if fragment.casefold() in command_for_match:
            raise ValueError(f"Shell command contains blocked fragment: {fragment}")
    _validate_shared_environment_mutation(
        command,
        Path(workdir).resolve() if workdir is not None else None,
    )
    if workdir is not None:
        _validate_shell_cd_targets(command, Path(workdir).resolve())


def _validate_shared_environment_mutation(
    command: str,
    workspace: Path | None,
) -> None:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()")
        tokens = list(lexer)
    except ValueError as exc:
        raise ValueError(f"Invalid shell command quoting: {exc}") from exc

    controls = {";", ";;", "&", "&&", "|", "||", "(", ")"}
    segment: list[str] = []
    for token in [*tokens, ";"]:
        if token not in controls:
            segment.append(token)
            continue
        if segment:
            _validate_command_segment_environment(segment, workspace)
            segment = []


def _validate_command_segment_environment(
    tokens: list[str],
    workspace: Path | None,
) -> None:
    lowered = [token.casefold() for token in tokens]
    for index, token in enumerate(tokens):
        executable = Path(token).name.casefold()
        remaining = lowered[index + 1 :]

        if executable in {"pip", "pip3", "pipx"} and any(
            action in remaining for action in PYTHON_ENV_MUTATIONS
        ):
            if executable != "pipx" and _is_workspace_executable(token, workspace):
                continue
            _raise_shared_environment_mutation(token)

        if (executable == "python" or executable.startswith("python3")) and any(
            remaining[offset : offset + 2] == ["-m", "pip"]
            and any(
                action in remaining[offset + 2 :]
                for action in PYTHON_ENV_MUTATIONS
            )
            for offset in range(max(0, len(remaining) - 1))
        ):
            if _is_workspace_executable(token, workspace):
                continue
            _raise_shared_environment_mutation(token)

        if executable in {"conda", "mamba", "micromamba"} and any(
            action in remaining for action in CONDA_ENV_MUTATIONS
        ):
            _raise_shared_environment_mutation(token)

        if executable == "uv" and any(
            remaining[offset : offset + 2] == ["pip", action]
            for action in PYTHON_ENV_MUTATIONS
            for offset in range(max(0, len(remaining) - 1))
        ):
            _raise_shared_environment_mutation(token)


def _is_workspace_executable(token: str, workspace: Path | None) -> bool:
    if workspace is None or "/" not in token or "$" in token or "`" in token:
        return False
    path = Path(token)
    candidate = path.resolve() if path.is_absolute() else (workspace / path).resolve()
    return candidate.is_relative_to(workspace)


def _raise_shared_environment_mutation(command: str) -> None:
    raise ValueError(
        "Shared Python environment mutation is blocked for "
        f"{command!r}. Edit dependency manifests or use a virtual environment "
        "located inside the workspace."
    )


def _validate_shell_cd_targets(command: str, workspace: Path) -> None:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()")
        tokens = list(lexer)
    except ValueError as exc:
        raise ValueError(f"Invalid shell command quoting: {exc}") from exc

    controls = {";", ";;", "&", "&&", "|", "||", "(", ")"}
    current = workspace
    for index, token in enumerate(tokens):
        if token not in {"cd", "pushd"}:
            continue
        target_index = index + 1
        if target_index < len(tokens) and tokens[target_index] == "--":
            target_index += 1
        if target_index >= len(tokens) or tokens[target_index] in controls:
            current = workspace
            continue

        target = tokens[target_index]
        if target == "-":
            raise ValueError("Shell directory switching with '-' is blocked.")
        if target in {"$HOME", "${HOME}", "~"}:
            candidate = workspace
        elif target.startswith("~/"):
            candidate = (workspace / target[2:]).resolve()
        else:
            if "$" in target or "`" in target:
                raise ValueError("Dynamic shell working directories are blocked.")
            path = Path(target)
            candidate = (
                path.resolve() if path.is_absolute() else (current / path).resolve()
            )

        if not candidate.is_relative_to(workspace):
            raise ValueError(
                f"Shell command changes directory outside workspace: {target}"
            )
        if is_sensitive_workspace_path(workspace, candidate):
            raise ValueError(
                f"Shell command changes directory into sensitive path: {target}"
            )
        current = candidate


def _is_safe_env_key(key: str) -> bool:
    upper = key.upper()
    if any(marker in upper for marker in SECRET_ENV_MARKERS):
        return False
    return key in PASSTHROUGH_ENV_KEYS or key.startswith("LC_")


__all__ = [
    "HARD_DENY_COMMAND_FRAGMENTS",
    "is_sensitive_workspace_path",
    "resolve_workspace_path",
    "safe_subprocess_env",
    "sensitive_glob_excludes",
    "validate_shell_command",
]
