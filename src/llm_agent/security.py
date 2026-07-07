from __future__ import annotations

import os
from pathlib import Path
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


def validate_shell_command(command: str, *, fragments: Iterable[str] | None = None) -> None:
    if not command.strip():
        raise ValueError("Command is required.")
    command_for_match = command.casefold()
    for fragment in fragments or HARD_DENY_COMMAND_FRAGMENTS:
        if fragment.casefold() in command_for_match:
            raise ValueError(f"Shell command contains blocked fragment: {fragment}")


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
