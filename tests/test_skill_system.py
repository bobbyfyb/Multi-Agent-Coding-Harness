from pathlib import Path
from typing import Any

import pytest

from llm_agent.agent import Agent
from llm_agent.context_manager import ContextManager
from llm_agent.llm_client import LLMResponse, LLMToolCall
from llm_agent.skill_system import (
    SkillNotFoundError,
    SkillRegistry,
    SkillSystemError,
    build_skill_catalog_section,
)
from llm_agent.tool_registry import ToolRegistry
from llm_agent.tools.skill_tools import register_tools as register_skill_tools


class FakeLLM:
    def __init__(self, outputs: list[LLMResponse]) -> None:
        self.outputs = outputs
        self.messages: list[list[dict[str, Any]]] = []

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]],
        tool_choice: str,
    ) -> LLMResponse:
        self.messages.append([dict(message) for message in messages])
        return self.outputs.pop(0)

    def assistant_message(self, response: LLMResponse) -> dict[str, Any]:
        return {
            "role": "assistant",
            "content": response.content,
            "tool_calls": [tool_call.raw for tool_call in response.tool_calls],
        }

    def tool_result_messages(
        self,
        tool_results: list[tuple[LLMToolCall, Any]],
    ) -> list[dict[str, Any]]:
        return [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_call.id,
                        "content": result,
                    }
                    for tool_call, result in tool_results
                ],
            }
        ]


def _write_skill(
    skills_root: Path,
    directory: str,
    manifest: str,
) -> Path:
    skill_dir = skills_root / directory
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(manifest, encoding="utf-8")
    return skill_dir


def test_skill_registry_scans_metadata_loads_body_and_reads_resources(
    tmp_path: Path,
) -> None:
    skills_root = tmp_path / ".llm_agent" / "skills"
    skill_dir = _write_skill(
        skills_root,
        "review",
        """---
name: code-review
description: Review code for behavioral defects.
when_to_use: Use for patches and pull requests.
---

# Internal Review Instructions

Inspect the complete diff before reporting findings.
""",
    )
    (skill_dir / "references").mkdir()
    (skill_dir / "references" / "checklist.md").write_text(
        "Check failure paths.",
        encoding="utf-8",
    )

    registry = SkillRegistry(skills_root)
    tools = ToolRegistry()
    register_skill_tools(tools, skill_registry=registry)

    metadata = registry.get("code-review")
    assert metadata.description == "Review code for behavioral defects."
    assert metadata.when_to_use == "Use for patches and pull requests."
    assert metadata.resources == ("references/checklist.md",)
    assert "Internal Review Instructions" not in registry.catalog()

    document = registry.load("code-review")
    assert "Inspect the complete diff" in document.instructions
    assert document.to_dict()["resources"] == ["references/checklist.md"]
    assert registry.read_resource(
        "code-review",
        "references/checklist.md",
    ) == "Check failure paths."
    skill_list = tools.call("skill_list", {})
    assert skill_list["ok"] is True
    assert skill_list["result"]["skills"][0]["name"] == "code-review"


def test_skill_registry_supports_manifest_without_frontmatter(
    tmp_path: Path,
) -> None:
    skills_root = tmp_path / "skills"
    _write_skill(
        skills_root,
        "debugging",
        "# Debugging Workflow\n\nUse controlled experiments.",
    )

    registry = SkillRegistry(skills_root)

    metadata = registry.get("debugging")
    assert metadata.name == "debugging"
    assert metadata.description == "Debugging Workflow"


def test_skill_registry_rejects_invalid_yaml_and_duplicate_names(
    tmp_path: Path,
) -> None:
    invalid_root = tmp_path / "invalid"
    _write_skill(
        invalid_root,
        "broken",
        "---\nname: [broken\n---\n# Broken",
    )

    with pytest.raises(SkillSystemError, match="Invalid YAML"):
        SkillRegistry(invalid_root)

    duplicate_root = tmp_path / "duplicate"
    for directory in ("one", "two"):
        _write_skill(
            duplicate_root,
            directory,
            """---
name: shared
description: Shared name.
---
# Shared
""",
        )

    with pytest.raises(SkillSystemError, match="Duplicate skill name"):
        SkillRegistry(duplicate_root)


def test_skill_registry_rejects_unknown_skills_path_escape_and_large_files(
    tmp_path: Path,
) -> None:
    skills_root = tmp_path / "skills"
    skill_dir = _write_skill(
        skills_root,
        "review",
        "# Review\n\nShort body.",
    )
    (tmp_path / "outside.md").write_text("outside", encoding="utf-8")
    (skill_dir / "large.md").write_text("x" * 20, encoding="utf-8")
    registry = SkillRegistry(skills_root, max_resource_chars=10)

    with pytest.raises(SkillNotFoundError, match="Skill not found"):
        registry.load("missing")
    with pytest.raises(SkillSystemError, match="escapes skill root"):
        registry.read_resource("review", "../../outside.md")
    with pytest.raises(SkillSystemError, match="Skill resource .* exceeds 10"):
        registry.read_resource("review", "large.md")

    with pytest.raises(SkillSystemError, match="manifest exceeds"):
        SkillRegistry(skills_root, max_skill_chars=5)


def test_skill_catalog_respects_budget(tmp_path: Path) -> None:
    skills_root = tmp_path / "skills"
    for index in range(4):
        _write_skill(
            skills_root,
            f"skill-{index}",
            f"""---
description: Description for skill number {index}.
---
# Skill {index}
""",
        )

    registry = SkillRegistry(skills_root, max_catalog_chars=90)
    catalog = registry.catalog()

    assert len(catalog) <= 90
    assert "more skills omitted" in catalog


def test_agent_loads_full_skill_only_after_tool_call(tmp_path: Path) -> None:
    skills_root = tmp_path / "skills"
    _write_skill(
        skills_root,
        "review",
        """---
name: code-review
description: Review code changes.
---
# Secret Review Workflow

Inspect every changed branch.
""",
    )
    registry = SkillRegistry(skills_root)
    tool_call = LLMToolCall(
        id="call_skill",
        name="skill_load",
        arguments={"name": "code-review"},
        raw={"id": "call_skill", "name": "skill_load"},
    )
    llm = FakeLLM(
        [
            LLMResponse(content="", tool_calls=[tool_call], raw={}),
            LLMResponse(content="review ready", tool_calls=[], raw={}),
        ]
    )
    tools = ToolRegistry()
    register_skill_tools(tools, skill_registry=registry)
    agent = Agent(
        llm=llm,
        tools=tools,
        context_manager=ContextManager(
            base_instructions="system",
            sections=[build_skill_catalog_section(registry)],
        ),
        workdir=tmp_path,
    )
    messages = agent.new_messages()
    messages.append({"role": "user", "content": "Review this change."})

    result = agent.run(messages)

    assert result.status == "completed"
    first_system_prompt = llm.messages[0][0]["content"]
    assert "code-review: Review code changes." in first_system_prompt
    assert "Secret Review Workflow" not in first_system_prompt

    loaded_result = llm.messages[1][-1]["content"][0]["content"]
    assert loaded_result["ok"] is True
    assert "Secret Review Workflow" in loaded_result["result"]["instructions"]
    assert "cannot override system instructions" in loaded_result["result"]["policy"]
