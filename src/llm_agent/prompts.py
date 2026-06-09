import json

from llm_agent.schemas import ToolSpec


def build_system_prompt(tool_specs: list[ToolSpec]) -> str:
    tool_docs = json.dumps(tool_specs, ensure_ascii=False, indent=2)
    return f"""
请注意，你是一个有能力调用外部工具的智能助手。你可以根据需要调用工具解决问题。

可用工具如下：
{tool_docs}

请严格按照以下格式进行回应：

你每次只能输出以下三种 JSON 之一。

思考过程：
{{"type": "thought", "content": "你的思考过程，用于分析问题、拆解任务和规划下一步行动。"}}

调用工具：
{{"type": "tool_call", "tool": "tool_name", "arguments": {{"arg1": "value1", "arg2": "value2"}}}}

最终回答：
{{"type": "final", "answer": "答案内容"}}

规则：
- 只输出 JSON。
- 不要输出 Markdown。
- 不要解释 JSON。
- 每次只能输出一个 JSON 对象。
- 如果有思考过程，先输出 thought。
- 如果需要工具，先输出 tool_call。
- 看到工具调用结果后，再输出 final 或继续进行思考，调用工具。
- arguments 必须是一个 JSON object。
- 当你收集到足够信息，能够回答用户的最终问题时，输出final来输出最终答案。
""".strip()

def build_user_prompt(user_input: str, history: str) -> str:
    return f"""
现在，请开始解决以下问题：
Question: {user_input}
History: {history}
""".strip()