from pathlib import Path
from typing import Any

from llm_agent.context_manager import (
    COMPACTED_TOOL_RESULT,
    ContextManager,
    DEFAULT_BASE_INSTRUCTIONS,
    PromptSection,
    build_system_prompt,
)
from llm_agent.llm_client import LLMResponse


class FakeSummaryLLM:
    def __init__(
        self,
        responses: list[LLMResponse] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.responses = responses or []
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: Any = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        self.calls.append(
            {
                "messages": messages,
                "tools": tools,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
        )
        if self.error is not None:
            raise self.error
        return self.responses.pop(0)


def _response(content: str) -> LLMResponse:
    return LLMResponse(content=content, tool_calls=[], raw={})


def test_context_manager_returns_system_message() -> None:
    manager = ContextManager(base_instructions="system")

    assert manager.new_messages() == [{"role": "system", "content": "system"}]


def test_default_instructions_request_concise_progress_updates() -> None:
    assert "Progress updates:" in DEFAULT_BASE_INSTRUCTIONS
    assert "one or two concise sentences" in DEFAULT_BASE_INSTRUCTIONS
    assert "Do not expose private chain-of-thought" in DEFAULT_BASE_INSTRUCTIONS


def test_context_manager_orders_sections_by_priority() -> None:
    manager = ContextManager(
        base_instructions="base",
        sections=[
            PromptSection(name="memory", content="remember this", priority=30),
            PromptSection(name="workspace", content="cwd=/tmp/project", priority=10),
        ],
    )

    assert manager.new_messages() == [
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


def test_context_manager_can_add_dynamic_sections() -> None:
    manager = ContextManager(base_instructions="base")

    manager.add_section("retrieved_context", "doc chunk", priority=40)

    assert "<retrieved_context>\ndoc chunk\n</retrieved_context>" in (
        manager.build_system_prompt()
    )


def test_runtime_context_is_request_scoped_and_counted_with_tools() -> None:
    manager = ContextManager(base_instructions="system")
    messages = manager.new_messages()
    messages.append({"role": "user", "content": "current request"})
    runtime = ["<relevant_memories>\npreference\n</relevant_memories>"]
    tools = [
        {
            "name": "memory_search",
            "description": "Search memory.",
            "parameters": {"type": "object"},
        }
    ]

    request_messages = manager.build_request_messages(
        messages,
        runtime_messages=runtime,
    )

    assert request_messages[-2]["content"].startswith("<relevant_memories>")
    assert request_messages[-1] == {
        "role": "user",
        "content": "current request",
    }
    assert messages[-1] == {"role": "user", "content": "current request"}
    assert manager.estimate_request_tokens(
        messages,
        runtime_messages=runtime,
        tools=tools,
    ) > manager.estimate_tokens(messages)


def test_runtime_context_does_not_split_tool_call_result_pairs() -> None:
    manager = ContextManager(base_instructions="system")
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "Inspect the file."},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call-1", "type": "function"}],
        },
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": "file contents",
        },
    ]

    request_messages = manager.build_request_messages(
        messages,
        runtime_messages=["<relevant_memories>\ncontext\n</relevant_memories>"],
    )

    assistant_index = next(
        index
        for index, message in enumerate(request_messages)
        if message.get("tool_calls")
    )
    assert request_messages[assistant_index + 1]["role"] == "tool"
    assert request_messages[-1]["role"] == "tool"


def test_prepare_reserves_budget_for_runtime_context(tmp_path: Path) -> None:
    llm = FakeSummaryLLM([_response("Earlier request summarized.")])
    manager = ContextManager(
        llm=llm,
        workdir=tmp_path,
        max_context_tokens=1_000,
        auto_compact_ratio=0.5,
        keep_recent_groups=1,
    )
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "older request"},
        {"role": "assistant", "content": "older response"},
        {"role": "user", "content": "latest request"},
    ]

    update = manager.prepare(
        messages,
        run_id="runtime-budget",
        reserved_tokens=500,
    )

    assert update is not None
    assert update.summary_created is True
    assert update.messages[-1] == {"role": "user", "content": "latest request"}


def test_default_instructions_and_tool_summary_compatibility() -> None:
    assert "working in the user's workspace" in DEFAULT_BASE_INSTRUCTIONS
    assert "Do not fabricate tool results." in DEFAULT_BASE_INSTRUCTIONS
    assert "legacy_available_tools_summary" not in (
        ContextManager().build_system_prompt()
    )

    prompt = build_system_prompt(
        [
            {
                "name": "bash",
                "description": "Run a shell command.",
                "parameters": {},
            }
        ]
    )
    assert "<legacy_available_tools_summary>" in prompt
    assert "- bash: Run a shell command." in prompt


def test_prepare_micro_compacts_old_anthropic_and_openai_results(
    tmp_path: Path,
) -> None:
    manager = ContextManager(
        workdir=tmp_path,
        keep_recent_tool_results=1,
        max_tool_result_chars=10_000,
    )
    messages = [
        {"role": "system", "content": "system"},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "a1", "name": "read_file", "input": {}}
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "a1",
                    "content": "a" * 500,
                }
            ],
        },
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "o1", "type": "function", "function": {}}],
        },
        {
            "role": "tool",
            "tool_call_id": "o1",
            "content": "latest result",
        },
    ]

    update = manager.prepare(messages, run_id="run-1")

    assert update is not None
    assert update.compacted_results == 1
    assert update.messages[2]["content"][0]["content"] == COMPACTED_TOOL_RESULT
    assert update.messages[4]["content"] == "latest result"
    assert update.transcript_path is not None
    assert update.transcript_path.exists()


def test_micro_compaction_keeps_structured_tool_result_evidence(
    tmp_path: Path,
) -> None:
    manager = ContextManager(
        workdir=tmp_path,
        keep_recent_tool_results=0,
        max_tool_result_chars=10_000,
    )
    messages = [
        {"role": "system", "content": "system"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "check-1", "type": "function"}],
        },
        {
            "role": "tool",
            "tool_call_id": "check-1",
            "content": (
                '{"ok": true, "result": {"outcome": "passed", '
                '"exit_code": 0, "summary": "42 tests passed", '
                '"report_path": ".llm_agent/reports/tests.json"}}'
            ),
        },
    ]

    update = manager.prepare(messages, run_id="structured-result")

    assert update is not None
    compacted = update.messages[-1]["content"]
    assert compacted.startswith("<compacted_tool_result>")
    assert '"outcome": "passed"' in compacted
    assert '"summary": "42 tests passed"' in compacted


def test_prepare_persists_large_tool_results_with_preview(tmp_path: Path) -> None:
    manager = ContextManager(
        workdir=tmp_path,
        keep_recent_tool_results=10,
        max_tool_result_chars=20,
        tool_result_preview_chars=8,
    )
    messages = [
        {"role": "system", "content": "system"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call/1", "type": "function", "function": {}}],
        },
        {
            "role": "tool",
            "tool_call_id": "call/1",
            "content": "0123456789" * 10,
        },
    ]

    update = manager.prepare(messages, run_id="run/1")

    assert update is not None
    assert update.persisted_results == 1
    marker = update.messages[-1]["content"]
    assert "<persisted_tool_result>" in marker
    assert "<preview>\n01234567" in marker
    relative_path = marker.split("<path>", 1)[1].split("</path>", 1)[0]
    persisted_path = tmp_path / relative_path
    assert persisted_path.read_text(encoding="utf-8") == "0123456789" * 10


def test_prepare_summarizes_old_history_and_keeps_system_and_recent_group(
    tmp_path: Path,
) -> None:
    llm = FakeSummaryLLM([_response("Goal and decisions preserved.")])
    manager = ContextManager(
        llm=llm,
        workdir=tmp_path,
        max_context_tokens=120,
        auto_compact_ratio=0.5,
        keep_recent_groups=1,
    )
    messages = [
        {"role": "system", "content": "important system"},
        {"role": "user", "content": "old request " + "x" * 300},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "latest request"},
    ]

    update = manager.prepare(messages, run_id="run-summary")

    assert update is not None
    assert update.summary_created is True
    assert update.messages[0] == {"role": "system", "content": "important system"}
    assert update.messages[1]["content"].startswith("<conversation_summary")
    assert "Goal and decisions preserved." in update.messages[1]["content"]
    assert update.messages[-1] == {"role": "user", "content": "latest request"}
    assert llm.calls[0]["tools"] is None
    assert llm.calls[0]["temperature"] == 0
    assert update.transcript_path is not None
    assert update.transcript_path.exists()


def test_recover_hard_trims_by_atomic_tool_groups_when_summary_fails(
    tmp_path: Path,
) -> None:
    llm = FakeSummaryLLM(error=RuntimeError("summary unavailable"))
    manager = ContextManager(
        llm=llm,
        workdir=tmp_path,
        keep_recent_groups=2,
    )
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "old"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call-1", "type": "function", "function": {}}],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "result"},
        {"role": "assistant", "content": "latest"},
    ]

    update = manager.recover(messages, run_id="run-reactive")

    assert update.hard_trimmed is True
    assert update.messages[0]["role"] == "system"
    assert update.messages[1]["content"].startswith("<context_compacted")
    assistant_index = next(
        index
        for index, message in enumerate(update.messages)
        if message.get("tool_calls")
    )
    assert update.messages[assistant_index + 1]["role"] == "tool"
    assert update.messages[assistant_index + 1]["tool_call_id"] == "call-1"
