from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping

import yaml

from llm_agent.serial_workflow import ROLE_SPECS, RoleSpec
from llm_agent.skill_system import SkillNotFoundError, SkillRegistry


DEFAULT_WORKFLOW_CONFIG_PATH = ".llm_agent/workflow.yaml"


class WorkflowConfigError(RuntimeError):
    """Raised when workflow.yaml cannot be parsed or applied."""


@dataclass(frozen=True)
class WorkflowConfig:
    role_specs: dict[str, RoleSpec] = field(default_factory=lambda: dict(ROLE_SPECS))
    max_fix_cycles: int = 1
    max_phase_retries: int = 1
    max_context_tokens: int = 100_000
    warnings: tuple[str, ...] = ()


def load_workflow_config(
    workdir: Path | str | None = None,
    *,
    base_role_specs: Mapping[str, RoleSpec] | None = None,
    skill_registry: SkillRegistry | None = None,
    config_path: Path | str | None = None,
) -> WorkflowConfig:
    root = Path.cwd() if workdir is None else Path(workdir)
    path = _resolve_config_path(root, config_path)
    base_roles = dict(base_role_specs or ROLE_SPECS)
    if not path.exists():
        return WorkflowConfig(role_specs=base_roles)
    if not path.is_file():
        raise WorkflowConfigError(f"Workflow config is not a file: {path}")

    raw = path.read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(raw) if raw.strip() else {}
    except yaml.YAMLError as exc:
        raise WorkflowConfigError(f"Invalid workflow config YAML: {path}") from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise WorkflowConfigError("Workflow config root must be a mapping.")

    _validate_top_level_keys(data)
    version = data.get("version", 1)
    if version != 1:
        raise WorkflowConfigError("Only workflow config version 1 is supported.")

    workflow = _mapping(data.get("workflow", {}), "workflow")
    roles = _mapping(data.get("roles", {}), "roles")

    max_fix_cycles = _non_negative_int(
        workflow.get("max_fix_cycles", 1),
        "workflow.max_fix_cycles",
    )
    max_phase_retries = _non_negative_int(
        workflow.get("max_phase_retries", 1),
        "workflow.max_phase_retries",
    )
    max_context_tokens = _positive_int(
        workflow.get("max_context_tokens", 100_000),
        "workflow.max_context_tokens",
    )

    role_specs, warnings = _apply_role_config(
        base_roles,
        roles,
        skill_registry=skill_registry,
    )
    return WorkflowConfig(
        role_specs=role_specs,
        max_fix_cycles=max_fix_cycles,
        max_phase_retries=max_phase_retries,
        max_context_tokens=max_context_tokens,
        warnings=tuple(warnings),
    )


def _apply_role_config(
    base_roles: dict[str, RoleSpec],
    roles: Mapping[str, Any],
    *,
    skill_registry: SkillRegistry | None,
) -> tuple[dict[str, RoleSpec], list[str]]:
    role_specs = dict(base_roles)
    warnings: list[str] = []
    allowed_role_keys = {"required_skills", "optional_skills", "max_steps"}
    for role_name, raw_role_config in roles.items():
        if role_name not in base_roles:
            raise WorkflowConfigError(f"Unknown workflow role: {role_name}")
        role_config = _mapping(raw_role_config, f"roles.{role_name}")
        unknown_keys = set(role_config) - allowed_role_keys
        if unknown_keys:
            raise WorkflowConfigError(
                f"Unsupported key(s) for roles.{role_name}: "
                + ", ".join(sorted(unknown_keys))
            )

        required_skills = _string_list(
            role_config.get("required_skills"),
            f"roles.{role_name}.required_skills",
            default=base_roles[role_name].required_skills,
        )
        optional_skills = _string_list(
            role_config.get("optional_skills"),
            f"roles.{role_name}.optional_skills",
            default=base_roles[role_name].optional_skills,
        )
        required_skills = _validate_required_skills(
            required_skills,
            role_name,
            skill_registry,
        )
        optional_skills, optional_warnings = _filter_optional_skills(
            optional_skills,
            role_name,
            skill_registry,
        )
        warnings.extend(optional_warnings)

        max_steps = _positive_int(
            role_config.get("max_steps", base_roles[role_name].max_steps),
            f"roles.{role_name}.max_steps",
        )
        role_specs[role_name] = replace(
            base_roles[role_name],
            required_skills=required_skills,
            optional_skills=optional_skills,
            max_steps=max_steps,
        )

    return role_specs, warnings


def _validate_required_skills(
    skill_names: tuple[str, ...],
    role_name: str,
    skill_registry: SkillRegistry | None,
) -> tuple[str, ...]:
    if skill_registry is None:
        return skill_names
    for skill_name in skill_names:
        try:
            skill_registry.get(skill_name)
        except SkillNotFoundError as exc:
            raise WorkflowConfigError(
                f"Required skill '{skill_name}' for role '{role_name}' "
                "is not registered."
            ) from exc
    return skill_names


def _filter_optional_skills(
    skill_names: tuple[str, ...],
    role_name: str,
    skill_registry: SkillRegistry | None,
) -> tuple[tuple[str, ...], list[str]]:
    if skill_registry is None:
        return skill_names, []

    kept: list[str] = []
    warnings: list[str] = []
    for skill_name in skill_names:
        try:
            skill_registry.get(skill_name)
        except SkillNotFoundError:
            warnings.append(
                f"Ignored optional skill '{skill_name}' for role '{role_name}': "
                "skill is not registered."
            )
            continue
        kept.append(skill_name)
    return tuple(kept), warnings


def _resolve_config_path(
    workdir: Path,
    config_path: Path | str | None,
) -> Path:
    if config_path is None:
        return (workdir / DEFAULT_WORKFLOW_CONFIG_PATH).resolve()
    path = Path(config_path)
    if path.is_absolute():
        return path.resolve()
    return (workdir / path).resolve()


def _validate_top_level_keys(data: Mapping[str, Any]) -> None:
    unknown_keys = set(data) - {"version", "workflow", "roles"}
    if unknown_keys:
        raise WorkflowConfigError(
            "Unsupported top-level workflow config key(s): "
            + ", ".join(sorted(unknown_keys))
        )


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise WorkflowConfigError(f"{label} must be a mapping.")
    return value


def _string_list(
    value: Any,
    label: str,
    *,
    default: tuple[str, ...],
) -> tuple[str, ...]:
    if value is None:
        return tuple(default)
    if not isinstance(value, list):
        raise WorkflowConfigError(f"{label} must be a list of strings.")
    result = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise WorkflowConfigError(
                f"{label}[{index}] must be a non-empty string."
            )
        result.append(item.strip())
    return tuple(result)


def _positive_int(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise WorkflowConfigError(f"{label} must be a positive integer.")
    return value


def _non_negative_int(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise WorkflowConfigError(f"{label} must be a non-negative integer.")
    return value


__all__ = [
    "DEFAULT_WORKFLOW_CONFIG_PATH",
    "WorkflowConfig",
    "WorkflowConfigError",
    "load_workflow_config",
]
