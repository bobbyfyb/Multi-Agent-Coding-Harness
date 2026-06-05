import json

from llm_agent.schemas import ToolSpec


def build_system_prompt(tool_specs: list[ToolSpec]) -> str:
    tool_docs = json.dumps(tool_specs, ensure_ascii=False, indent=2)
    return f"""
你是一个 minimal agent。你可以根据需要调用工具解决问题。

可用工具如下：
{tool_docs}

你每次只能输出以下两种 JSON 之一。

调用工具：
{{"type": "tool_call", "tool": "add", "arguments": {{"a": 1, "b": 2}}}}

最终回答：
{{"type": "final", "answer": "答案内容"}}

规则：
- 只输出 JSON。
- 不要输出 Markdown。
- 不要解释 JSON。
- 如果需要工具，先输出 tool_call。
- 看到工具调用结果后，再输出 final 或继续调用工具。
- arguments 必须是一个 JSON object。
""".strip()

