from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from llm_agent.tool_registry import ToolSpec



IDENTITY_INSTRUCTIONS = """
You are a coding agent working in the user's workspace.

Use the provided tools to inspect, modify, and verify the work. Prefer acting on
observed project state over guessing. Keep changes focused on the user's request.
""".strip()

CONTEXT_INSTRUCTIONS = """
Context:
- Treat any structured sections below as authoritative working context.
- Use retrieved context, memory, and workspace notes when relevant, but do not
  invent details that are not present.
- If context conflicts with fresh tool results, prefer the fresh tool results.
""".strip()

TOOL_INSTRUCTIONS = """
Tool use:
- Use tools when you need to inspect files, search, run commands, edit files,
  compute, or verify behavior.
- Do not describe a tool call as text when a real tool call is needed.
- Do not fabricate tool results.
- Keep file operations inside the workspace.
- If a tool fails, use the error to choose the next reasonable step.
""".strip()

FINAL_ANSWER_INSTRUCTIONS = """
Final answer:
- Answer in natural language.
- Be concise, clear, and accurate.
- Summarize what changed or what was found.
- Mention verification results when available.
- If something could not be completed, say why and what remains.
""".strip()

DEFAULT_BASE_INSTRUCTIONS = "\n\n".join(
    [
        IDENTITY_INSTRUCTIONS,
        CONTEXT_INSTRUCTIONS,
        TOOL_INSTRUCTIONS,
        FINAL_ANSWER_INSTRUCTIONS,
    ]
)


class ContextBuilder(Protocol):
    def new_messages(self) -> list[dict[str, Any]]:
        """Build the initial conversation messages for a new session."""


@dataclass(frozen=True)
class PromptSection:
    name: str
    content: str
    priority: int = 100
    token_budget: int | None = None


@dataclass
class StaticContextBuilder:
    system_prompt: str

    def new_messages(self) -> list[dict[str, Any]]:
        return [{"role": "system", "content": self.system_prompt}]


@dataclass
class AgentContextBuilder:
    base_instructions: str = DEFAULT_BASE_INSTRUCTIONS
    sections: list[PromptSection] = field(default_factory=list)

    def add_section(
        self,
        name: str,
        content: str,
        *,
        priority: int = 100,
        token_budget: int | None = None,
    ) -> None:
        self.sections.append(
            PromptSection(
                name=name,
                content=content,
                priority=priority,
                token_budget=token_budget,
            )
        )

    def build_system_prompt(self) -> str:
        parts = [self.base_instructions.strip()]
        for section in sorted(self.sections, key=lambda item: (item.priority, item.name)):
            content = section.content.strip()
            if not content:
                continue
            parts.append(f"<{section.name}>\n{content}\n</{section.name}>")
        return "\n\n".join(parts).strip()

    def new_messages(self) -> list[dict[str, Any]]:
        return [{"role": "system", "content": self.build_system_prompt()}]


def build_tool_summary_section(tool_specs: list[ToolSpec]) -> PromptSection:
    tool_summaries = "\n".join(
        f"- {tool['name']}: {tool.get('description', '')}".strip()
        for tool in tool_specs
    )
    return PromptSection(
        name="legacy_available_tools_summary",
        content=tool_summaries or "- 无",
        priority=50,
    )


def build_system_prompt(tool_specs: list[ToolSpec] | None = None) -> str:
    sections = []
    if tool_specs is not None:
        sections.append(build_tool_summary_section(tool_specs))
    return AgentContextBuilder(sections=sections).build_system_prompt()
