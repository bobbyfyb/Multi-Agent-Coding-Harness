from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from llm_agent.agent import Agent, AgentEvent
from llm_agent.hooks import HookManager
from llm_agent.hooks.memory_hooks import MemoryContextHook, MemoryExtractionHook
from llm_agent.llm_client import LLMResponse
from llm_agent.memory_system import (
    MemoryManager,
    MemorySystemError,
    format_memory_context,
)
from llm_agent.tool_registry import ToolRegistry
from llm_agent.tools.memory_tools import register_tools


class FakeLLM:
    def __init__(self, outputs: list[LLMResponse | Exception]) -> None:
        self.outputs = outputs
        self.calls: list[dict[str, Any]] = []

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        self.calls.append(
            {
                "messages": [dict(message) for message in messages],
                "tools": tools,
                "tool_choice": tool_choice,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
        )
        output = self.outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        return output

    @staticmethod
    def assistant_message(response: LLMResponse) -> dict[str, Any]:
        return {"role": "assistant", "content": response.content}

    @staticmethod
    def tool_result_messages(tool_results: list[Any]) -> list[dict[str, Any]]:
        return []


def _response(content: str) -> LLMResponse:
    return LLMResponse(content=content, tool_calls=[], raw={})


def test_memory_manager_persists_updates_and_rebuilds_index(
    tmp_path: Path,
) -> None:
    manager = MemoryManager.for_workdir(tmp_path)
    created = manager.remember(
        name="Python style",
        memory_type="user",
        description="User prefers single quotes in Python.",
        body="Use single quotes for Python strings.",
        pinned=True,
    )

    restored_manager = MemoryManager.for_workdir(tmp_path)
    restored = restored_manager.get(created.id)
    updated = restored_manager.remember(
        name="Python style",
        memory_type="user",
        description="User prefers tabs and single quotes in Python.",
        body="Use tabs and single quotes for Python code.",
        pinned=True,
    )

    assert restored.body == "Use single quotes for Python strings."
    assert updated.id == created.id
    assert len(restored_manager.list()) == 1
    assert "Python style" in (
        tmp_path / ".llm_agent" / "memory" / "MEMORY.md"
    ).read_text(encoding="utf-8")


def test_memory_manager_rejects_invalid_types_and_possible_secrets(
    tmp_path: Path,
) -> None:
    manager = MemoryManager.for_workdir(tmp_path)

    with pytest.raises(MemorySystemError, match="Invalid memory type"):
        manager.remember(
            name="Invalid",
            memory_type="temporary",
            description="Invalid type.",
            body="Do not keep this.",
        )

    with pytest.raises(MemorySystemError, match="possible secret"):
        manager.remember(
            name="Credential",
            description="Project credential.",
            body="api_key = sk-this-should-not-be-stored",
        )


def test_memory_retrieval_loads_pinned_and_llm_selected_items_and_caches(
    tmp_path: Path,
) -> None:
    llm = FakeLLM([])
    manager = MemoryManager.for_workdir(tmp_path, llm=llm)
    pinned = manager.remember(
        name="No database mocks",
        memory_type="feedback",
        description="Never mock the database in integration tests.",
        body="Use the real test database for integration tests.",
        pinned=True,
    )
    selected = manager.remember(
        name="Python formatting",
        memory_type="user",
        description="Formatting preferences for Python files.",
        body="Use tabs in Python files.",
    )
    manager.remember(
        name="Frontend framework",
        memory_type="project",
        description="The frontend uses React.",
        body="Use React for frontend components.",
    )
    llm.outputs.append(_response(f'["{selected.id}"]'))

    first = manager.retrieve_relevant("Create a Python integration test.")
    second = manager.retrieve_relevant("Create a Python integration test.")

    assert [memory.id for memory in first] == [pinned.id, selected.id]
    assert [memory.id for memory in second] == [pinned.id, selected.id]
    assert len(llm.calls) == 1


def test_memory_retrieval_falls_back_to_metadata_matching(
    tmp_path: Path,
) -> None:
    llm = FakeLLM([RuntimeError("selector unavailable")])
    manager = MemoryManager.for_workdir(tmp_path, llm=llm)
    relevant = manager.remember(
        name="Python formatting",
        memory_type="user",
        description="Python indentation preference.",
        body="Use tabs.",
    )
    manager.remember(
        name="Frontend framework",
        memory_type="project",
        description="React frontend.",
        body="Use React.",
    )

    selected = manager.retrieve_relevant("Format this Python module.")

    assert [memory.id for memory in selected] == [relevant.id]


def test_memory_extraction_is_gated_and_upserts_durable_information(
    tmp_path: Path,
) -> None:
    llm = FakeLLM(
        [
            _response(
                """[
                  {
                    "name": "Python indentation",
                    "type": "user",
                    "description": "User prefers tabs in Python.",
                    "body": "Use tabs for Python indentation.",
                    "pinned": true
                  }
                ]"""
            )
        ]
    )
    manager = MemoryManager.for_workdir(tmp_path, llm=llm)

    assert (
        manager.extract_from_turn(
            user_text="Hello there.",
            assistant_text="Hello.",
            run_id="run-ignored",
        )
        == []
    )
    extracted = manager.extract_from_turn(
        user_text="Remember that I prefer tabs in Python.",
        assistant_text="Understood.",
        run_id="run-memory",
    )

    assert len(extracted) == 1
    assert extracted[0].pinned is True
    assert extracted[0].source == "auto_extracted"
    assert extracted[0].source_run_id == "run-memory"
    assert len(llm.calls) == 1


def test_memory_tools_support_explicit_lifecycle(tmp_path: Path) -> None:
    manager = MemoryManager.for_workdir(tmp_path)
    registry = ToolRegistry()
    register_tools(registry, memory_manager=manager)

    created = registry.call(
        "memory_remember",
        {
            "name": "Test command",
            "description": "Project test command.",
            "body": "Run uv run pytest.",
            "type": "reference",
        },
        context=SimpleNamespace(run_id="run-tool"),
    )
    memory_id = manager.list()[0].id
    assert manager.list()[0].source_run_id == "run-tool"
    searched = registry.call("memory_search", {"query": "test command"})
    loaded = registry.call("memory_get", {"memory_id": memory_id})
    forgotten = registry.call("memory_forget", {"memory_id": memory_id})

    assert created["ok"] is True
    assert memory_id in searched["result"]
    assert "uv run pytest" in loaded["result"]
    assert forgotten["ok"] is True
    assert manager.list() == []


def test_agent_recalled_memory_is_ephemeral_request_context(
    tmp_path: Path,
) -> None:
    manager = MemoryManager.for_workdir(tmp_path)
    memory = manager.remember(
        name="Python indentation",
        memory_type="user",
        description="Python indentation preference.",
        body="Use tabs for Python indentation.",
        pinned=True,
    )
    llm = FakeLLM([_response("I will follow the preference.")])
    hooks = HookManager()
    hooks.register_hook("BeforeLLM", MemoryContextHook(manager))
    hooks.register_hook("Stop", MemoryExtractionHook(manager))
    agent = Agent(
        llm=llm,
        tools=ToolRegistry(),
        context_manager="system",
        hooks=hooks,
        workdir=tmp_path,
    )
    messages = agent.new_messages()
    messages.append({"role": "user", "content": "Create a Python file."})
    events: list[AgentEvent] = []

    agent.run(messages, on_event=events.append)

    request_memory = next(
        message["content"]
        for message in llm.calls[0]["messages"]
        if str(message.get("content", "")).startswith("<relevant_memories>")
    )
    assert memory.id in request_memory
    assert request_memory == format_memory_context([memory])
    assert llm.calls[0]["messages"][-1]["content"] == "Create a Python file."
    assert all(
        not str(message.get("content", "")).startswith("<relevant_memories>")
        for message in messages
    )
    assert [event.type for event in events] == [
        "memory_context",
        "step",
        "final",
    ]


def test_agent_stop_hook_extracts_memory_from_original_turn(
    tmp_path: Path,
) -> None:
    llm = FakeLLM(
        [
            _response("I will remember that."),
            _response(
                """[{
                  "name": "Python quotes",
                  "type": "user",
                  "description": "User prefers single quotes.",
                  "body": "Use single quotes in Python.",
                  "pinned": true
                }]"""
            ),
        ]
    )
    manager = MemoryManager.for_workdir(tmp_path, llm=llm)
    hooks = HookManager()
    hooks.register_hook("BeforeLLM", MemoryContextHook(manager))
    hooks.register_hook("Stop", MemoryExtractionHook(manager))
    agent = Agent(
        llm=llm,
        tools=ToolRegistry(),
        context_manager="system",
        hooks=hooks,
        workdir=tmp_path,
    )
    messages = agent.new_messages()
    messages.append(
        {"role": "user", "content": "Remember that I prefer single quotes."}
    )
    events: list[AgentEvent] = []

    agent.run(messages, on_event=events.append, run_id="run-extract")

    assert len(manager.list()) == 1
    assert manager.list()[0].source_run_id == "run-extract"
    assert [event.type for event in events] == [
        "step",
        "final",
        "memory_extracted",
    ]
