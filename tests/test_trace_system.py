import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from llm_agent.agent import Agent
from llm_agent.hooks import HookManager, HookResult
from llm_agent.llm_client import LLMClient
from llm_agent.tool_registry import ToolRegistry
from llm_agent.trace_system import (
    TraceConfig,
    TraceRecorder,
    record_trace,
    trace_operation,
    trace_scope,
)


class SequenceCreate:
    def __init__(self, responses: list[dict[str, Any] | Exception]) -> None:
        self.responses = responses

    def create(self, **kwargs: Any) -> dict[str, Any]:
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeOpenAIClient:
    def __init__(self, responses: list[dict[str, Any] | Exception]) -> None:
        create = SequenceCreate(responses)
        self.chat = SimpleNamespace(completions=create)


def _read_records(recorder: TraceRecorder) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in recorder.jsonl_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_trace_recorder_orders_redacts_and_renders(tmp_path: Path) -> None:
    recorder = TraceRecorder.for_run(
        tmp_path,
        run_id="run-redaction",
        config=TraceConfig(max_inline_chars=12),
    )

    with trace_scope(
        recorder,
        run_id="run-redaction",
        agent_id="main",
    ):
        record_trace(
            category="llm",
            name="llm.call",
            data={
                "api_key": "top-secret",
                "input_tokens": 12,
                "content": "a" * 40,
            },
        )
        record_trace(category="agent", name="final", data={"content": "done"})

    records = _read_records(recorder)

    assert [record["sequence"] for record in records] == [1, 2]
    assert records[0]["data"]["api_key"] == "[REDACTED]"
    assert records[0]["data"]["input_tokens"] == 12
    assert records[0]["data"]["content"]["truncated"] is True

    report = recorder.render_markdown()

    assert report == recorder.markdown_path
    assert report is not None
    assert "## Timeline" in report.read_text(encoding="utf-8")


def test_trace_can_summarize_tool_arguments_and_results(tmp_path: Path) -> None:
    recorder = TraceRecorder.for_run(
        tmp_path,
        run_id="run-private-tool",
        config=TraceConfig(capture_tool_content=False),
    )

    with trace_scope(
        recorder,
        run_id="run-private-tool",
        agent_id="main",
    ):
        record_trace(
            category="tool",
            name="tool_result",
            data={
                "arguments": {"content": "private input"},
                "result": {"ok": True, "result": "private output"},
            },
        )

    data = _read_records(recorder)[0]["data"]

    assert data["arguments"]["type"] == "dict"
    assert data["result"]["type"] == "dict"
    assert "private" not in json.dumps(data)


def test_llm_client_records_operation_metadata_without_prompt_content(
    tmp_path: Path,
) -> None:
    sdk = FakeOpenAIClient(
        [
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "selected"},
                    }
                ],
                "usage": {"input_tokens": 9, "output_tokens": 2},
            }
        ]
    )
    client = LLMClient(
        provider="openai",
        model="test-model",
        openai_client=sdk,
    )
    recorder = TraceRecorder.for_run(tmp_path, run_id="run-llm")

    with trace_scope(recorder, run_id="run-llm", agent_id="main"):
        with trace_operation("memory_select", candidate_count=3):
            client.chat([{"role": "user", "content": "private prompt"}])

    records = _read_records(recorder)
    started, completed = records

    assert started["phase"] == "started"
    assert started["data"]["operation"] == "memory_select"
    assert started["data"]["operation_data"] == {"candidate_count": 3}
    assert started["data"]["messages"]["type"] == "list"
    assert "private prompt" not in json.dumps(started, ensure_ascii=False)
    assert completed["phase"] == "completed"
    assert completed["data"]["usage"] == {
        "input_tokens": 9,
        "output_tokens": 2,
    }
    assert completed["duration_ms"] is not None
    assert started["correlation_id"] == completed["correlation_id"]


def test_agent_trace_covers_llm_hook_tool_and_final(tmp_path: Path) -> None:
    sdk = FakeOpenAIClient(
        [
            {
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "add",
                                        "arguments": '{"a": 1, "b": 2}',
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {"input_tokens": 10, "output_tokens": 4},
            },
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "3"},
                    }
                ],
                "usage": {"input_tokens": 15, "output_tokens": 1},
            },
        ]
    )
    llm = LLMClient(
        provider="openai",
        model="test-model",
        openai_client=sdk,
    )
    tools = ToolRegistry()
    tools.register("add", "Add numbers.", {}, lambda a, b: a + b)
    hooks = HookManager()
    hooks.register_hook(
        "PreToolUse",
        lambda tool_call, context: HookResult.allow("test approval"),
    )
    agent = Agent(
        llm=llm,
        tools=tools,
        hooks=hooks,
        context_manager="system",
        workdir=tmp_path,
        agent_id="main",
    )
    messages = agent.new_messages()
    messages.append({"role": "user", "content": "Add 1 and 2."})
    recorder = TraceRecorder.for_run(tmp_path, run_id="run-agent")

    result = agent.run(messages, run_id="run-agent", trace=recorder)
    records = _read_records(recorder)
    names = [record["name"] for record in records]

    assert result.content == "3"
    assert names[0] == "run.started"
    assert names[-1] == "run.completed"
    assert names.count("llm.call") == 4
    assert "hook.PreToolUse" in names
    assert "tool_call" in names
    assert "tool_result" in names
    assert "final" in names
    tool_result = next(record for record in records if record["name"] == "tool_result")
    assert tool_result["duration_ms"] is not None
    assert recorder.markdown_path.exists()


def test_agent_trace_closes_failed_run(tmp_path: Path) -> None:
    llm = LLMClient(
        provider="openai",
        model="test-model",
        openai_client=FakeOpenAIClient([RuntimeError("provider unavailable")]),
    )
    agent = Agent(
        llm=llm,
        tools=ToolRegistry(),
        context_manager="system",
        workdir=tmp_path,
    )
    messages = agent.new_messages()
    messages.append({"role": "user", "content": "hello"})
    recorder = TraceRecorder.for_run(tmp_path, run_id="run-failed")

    with pytest.raises(RuntimeError, match="LLM request failed"):
        agent.run(messages, run_id="run-failed", trace=recorder)

    records = _read_records(recorder)
    assert records[-1]["name"] == "run.failed"
    assert records[-1]["status"] == "error"
    assert any(
        record["category"] == "llm" and record["phase"] == "failed"
        for record in records
    )
    assert recorder.markdown_path.exists()
