import json

from llm_agent.schemas import ToolSpec


def build_reAct_system_prompt(tool_specs: list[ToolSpec]) -> str:
    tool_docs = json.dumps(tool_specs, ensure_ascii=False, indent=2)
    return f"""
请注意，你是一个有能力调用外部工具的智能助手。你可以根据需要调用工具解决问题。

可用工具如下：
{tool_docs}

请严格按照以下格式进行回应：

你每次只能输出以下三种 JSON 之一。

{{"type": "thought", "content": "你的思考过程，用于分析问题、拆解任务和规划下一步行动。"}}

{{"type": "tool_call", "tool": "你要调用的tool_name", "arguments": {{"arg1": "value1", "arg2": "value2"}}}}

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

def build_reAct_user_prompt(user_input: str, history: str) -> str:
    return f"""
现在，请开始解决以下问题：
Question: {user_input}
History: {history}
""".strip()

def build_plan_system_prompt() -> str:
    return f"""
    你是一个顶级的AI规划专家。你的任务是将用户提出的复杂问题分解成一个由多个简单步骤组成的行动计划。
    请确保计划中的每个步骤都是一个独立的、可执行的子任务，并且严格按照逻辑顺序排列。
    你的输出必须严格满足以下格式：

    {{"type": "plan", "content": ["步骤1", "步骤2", "步骤3", ...]}}
    
    规则：
    - 只输出一个 JSON 对象。
    - 不要输出 Markdown。
    - 不要解释 JSON。
    """

def build_excutor_system_prompt() -> str:
    return f"""
    你是一位顶级的AI执行专家。你的任务是严格按照给定的计划，一步步地解决问题。
    你将收到原始问题、完整的计划、以及到目前为止已经完成的步骤和结果。
    请你专注于解决“当前步骤”，并仅输出该步骤的最终答案，不要输出任何额外的解释或对话。     
    """

def build_excutor_user_prompt(user_input: str, history: str, plan: str, current_step: str) -> str:
    return f"""
    # 原始问题：
    {user_input}
    
    # 完整计划：
    {plan}
    
    # 历史步骤与结果：
    {history}
    
    # 当前步骤：
    {current_step}
    
    请仅输出针对“当前步骤”的回答：
    """
