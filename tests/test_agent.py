from llm_agent.agent import Agent
from llm_agent.schemas import ChatMessage
from llm_agent.tool_registry import ToolRegistry


class FakeLLM:
    def __init__(self, outputs: list[str]) -> None:
        self.outputs = outputs
        self.messages: list[list[ChatMessage]] = []

    def complete(self, messages: list[ChatMessage]) -> str:
        self.messages.append(messages.copy())
        return self.outputs.pop(0)


def test_agent_runs_tool_call_then_final_answer() -> None:
    registry = ToolRegistry()
    registry.register(
        name="add",
        description="Add two numbers.",
        parameters={},
        func=lambda a, b: a + b,
    )
    llm = FakeLLM(
        [
            '{"type": "tool_call", "tool": "add", "arguments": {"a": 1, "b": 2}}',
            '{"type": "final", "answer": "1 + 2 = 3"}',
        ]
    )
    agent = Agent(llm=llm, tools=registry, system_prompt="system")

    assert agent.run("calculate") == "1 + 2 = 3"
    assert len(llm.messages) == 2


def test_agent_retries_after_parse_failure() -> None:
    registry = ToolRegistry()
    llm = FakeLLM(
        [
            "not json",
            '{"type": "final", "answer": "recovered"}',
        ]
    )
    agent = Agent(llm=llm, tools=registry, system_prompt="system")

    assert agent.run("recover") == "recovered"
    assert len(llm.messages) == 2

    retry_messages = llm.messages[1]
    assert retry_messages[-2] == {"role": "assistant", "content": "not json"}
    assert retry_messages[-1]["role"] == "user"
    assert "无法解析为合法 agent JSON" in retry_messages[-1]["content"]
    assert "只能输出 JSON" in retry_messages[-1]["content"]
