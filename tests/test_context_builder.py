from llm_agent.context_builder import (
    AgentContextBuilder,
    DEFAULT_BASE_INSTRUCTIONS,
    PromptSection,
    StaticContextBuilder,
    build_system_prompt,
)


def test_static_context_builder_returns_system_message() -> None:
    builder = StaticContextBuilder("system")

    assert builder.new_messages() == [{"role": "system", "content": "system"}]


def test_agent_context_builder_orders_sections_by_priority() -> None:
    builder = AgentContextBuilder(
        base_instructions="base",
        sections=[
            PromptSection(name="memory", content="remember this", priority=30),
            PromptSection(name="workspace", content="cwd=/tmp/project", priority=10),
        ],
    )

    assert builder.new_messages() == [
        {
            "role": "system",
            "content": (
                "base\n\n"
                "<workspace>\n"
                "cwd=/tmp/project\n"
                "</workspace>\n\n"
                "<memory>\n"
                "remember this\n"
                "</memory>"
            ),
        }
    ]


def test_agent_context_builder_can_add_dynamic_sections() -> None:
    builder = AgentContextBuilder(base_instructions="base")

    builder.add_section("retrieved_context", "doc chunk", priority=40)

    assert "<retrieved_context>\ndoc chunk\n</retrieved_context>" in (
        builder.build_system_prompt()
    )


def test_default_base_instructions_cover_context_tools_and_final_answer() -> None:
    assert "working in the user's workspace" in DEFAULT_BASE_INSTRUCTIONS
    assert "Context:" in DEFAULT_BASE_INSTRUCTIONS
    assert "Tool use:" in DEFAULT_BASE_INSTRUCTIONS
    assert "Final answer:" in DEFAULT_BASE_INSTRUCTIONS
    assert "Do not fabricate tool results." in DEFAULT_BASE_INSTRUCTIONS


def test_default_agent_context_builder_does_not_duplicate_tool_schemas() -> None:
    prompt = AgentContextBuilder().build_system_prompt()

    assert "Tool use:" in prompt
    assert "legacy_available_tools_summary" not in prompt


def test_build_system_prompt_keeps_compatibility_with_tool_summaries() -> None:
    prompt = build_system_prompt(
        [
            {
                "name": "bash",
                "description": "Run a shell command.",
                "parameters": {},
            }
        ]
    )

    assert "coding agent working in the user's workspace" in prompt
    assert "<legacy_available_tools_summary>" in prompt
    assert "- bash: Run a shell command." in prompt
