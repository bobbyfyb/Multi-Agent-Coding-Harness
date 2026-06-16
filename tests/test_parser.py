from llm_agent.parser import parse_agent_action
from llm_agent.schemas import FinalAnswer, ToolCall


def test_parse_tool_call() -> None:
    action = parse_agent_action(
        '{"type": "tool_call", "tool": "add", "arguments": {"a": 1, "b": 2}}'
    )

    assert action == ToolCall(tool="add", arguments={"a": 1, "b": 2})


def test_parse_final_answer() -> None:
    action = parse_agent_action('{"type": "final", "answer": "done"}')

    assert action == FinalAnswer(answer="done")


def test_parse_json_code_fence() -> None:
    action = parse_agent_action(
        '```json\n{"type": "final", "answer": "done"}\n```'
    )

    assert action == FinalAnswer(answer="done")

