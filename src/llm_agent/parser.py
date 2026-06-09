import json

from llm_agent.schemas import AgentAction, AgentPlan, FinalAnswer, GenericAgentAction, Thought, ToolCall


def parse_agent_action(text: str) -> AgentAction:
    data = json.loads(_strip_code_fence(text))
    action_type = data.get("type")

    if action_type == "thought":
        return Thought(content=data["content"])
    
    if action_type == "tool_call":
        return ToolCall(
            tool=data["tool"],
            arguments=data.get("arguments", {}),
        )

    if action_type == "final":
        return FinalAnswer(answer=data["answer"])
    
    if action_type == "plan":
        return AgentPlan(content=data["content"])
    
    if action_type == "execution":
        return GenericAgentAction(type="execution", content=data["content"])
    
    if action_type == "reflection":
        return GenericAgentAction(type="reflection", content=data["content"])


    raise ValueError(f"Unknown agent action type: {action_type}")


def _strip_code_fence(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped

    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()

