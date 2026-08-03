from pathlib import Path

import pytest

from llm_agent.serial_workflow import ROLE_SPECS
from llm_agent.skill_system import SkillRegistry
from llm_agent.workflow_config import WorkflowConfigError, load_workflow_config


def test_load_workflow_config_returns_defaults_when_file_missing(
    tmp_path: Path,
) -> None:
    config = load_workflow_config(tmp_path)

    assert config.role_specs == ROLE_SPECS
    assert config.isolation == "worktree"
    assert config.max_fix_cycles == 1
    assert config.max_phase_retries == 1
    assert config.max_context_tokens == 100_000
    assert config.warnings == ()


def test_load_workflow_config_merges_role_skills_and_budgets(
    tmp_path: Path,
) -> None:
    _write_skill(tmp_path, "prd-writer")
    _write_skill(tmp_path, "code-review")
    _write_skill(tmp_path, "qa-checklist")
    _write_config(
        tmp_path,
        """
version: 1
workflow:
  isolation: shared
  max_fix_cycles: 2
  max_phase_retries: 3
  max_context_tokens: 120000
roles:
  pm:
    required_skills:
      - prd-writer
    max_steps: 12
  engineer:
    optional_skills:
      - code-review
    max_steps: 26
  qa:
    optional_skills:
      - qa-checklist
""",
    )

    config = load_workflow_config(
        tmp_path,
        skill_registry=SkillRegistry.for_workdir(tmp_path),
    )

    assert config.max_fix_cycles == 2
    assert config.isolation == "shared"
    assert config.max_phase_retries == 3
    assert config.max_context_tokens == 120_000
    assert config.role_specs["pm"].required_skills == ("prd-writer",)
    assert config.role_specs["pm"].max_steps == 12
    assert config.role_specs["pm"].tool_names == ROLE_SPECS["pm"].tool_names
    assert config.role_specs["engineer"].optional_skills == ("code-review",)
    assert config.role_specs["engineer"].max_steps == 26
    assert config.role_specs["qa"].optional_skills == ("qa-checklist",)
    assert config.role_specs["pm_acceptance"] == ROLE_SPECS["pm_acceptance"]


def test_load_workflow_config_rejects_unknown_role_and_unsafe_role_keys(
    tmp_path: Path,
) -> None:
    _write_config(
        tmp_path,
        """
roles:
  designer:
    max_steps: 10
""",
    )

    with pytest.raises(WorkflowConfigError, match="Unknown workflow role"):
        load_workflow_config(tmp_path)

    _write_config(
        tmp_path,
        """
roles:
  qa:
    tool_names:
      - write_file
""",
    )

    with pytest.raises(WorkflowConfigError, match="Unsupported key"):
        load_workflow_config(tmp_path)


def test_load_workflow_config_validates_required_and_optional_skills(
    tmp_path: Path,
) -> None:
    _write_config(
        tmp_path,
        """
roles:
  pm:
    required_skills:
      - missing-required
""",
    )

    with pytest.raises(WorkflowConfigError, match="Required skill"):
        load_workflow_config(
            tmp_path,
            skill_registry=SkillRegistry.for_workdir(tmp_path),
        )

    _write_config(
        tmp_path,
        """
roles:
  engineer:
    optional_skills:
      - missing-optional
""",
    )

    config = load_workflow_config(
        tmp_path,
        skill_registry=SkillRegistry.for_workdir(tmp_path),
    )

    assert config.role_specs["engineer"].optional_skills == ()
    assert "Ignored optional skill 'missing-optional'" in config.warnings[0]


def test_load_workflow_config_rejects_invalid_values(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        """
workflow:
  max_fix_cycles: -1
""",
    )

    with pytest.raises(WorkflowConfigError, match="non-negative integer"):
        load_workflow_config(tmp_path)

    _write_config(
        tmp_path,
        """
roles:
  pm:
    required_skills: prd-writer
""",
    )

    with pytest.raises(WorkflowConfigError, match="list of strings"):
        load_workflow_config(tmp_path)

    _write_config(
        tmp_path,
        """
workflow:
  isolation: container
""",
    )

    with pytest.raises(WorkflowConfigError, match="workflow.isolation"):
        load_workflow_config(tmp_path)


def _write_config(tmp_path: Path, content: str) -> None:
    config_path = tmp_path / ".llm_agent" / "workflow.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(content.strip() + "\n", encoding="utf-8")


def _write_skill(tmp_path: Path, name: str) -> None:
    skill_dir = tmp_path / ".llm_agent" / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"""---
name: {name}
description: Test skill {name}.
---

# {name}
""",
        encoding="utf-8",
    )
