import json
from dataclasses import dataclass

from llm_agent.llm_client import LLMClient
from llm_agent.parser import parse_agent_action
from llm_agent.prompts import build_user_prompt
from llm_agent.schemas import ChatMessage, FinalAnswer, Thought, ToolCall
from llm_agent.tool_registry import ToolRegistry


@dataclass
class ReActAgent:
    llm: LLMClient
    tools: ToolRegistry
    history: list[str]
    system_prompt: str
    max_steps: int = 5
    

    def run(self, user_input: str, show_reasoning_step: bool = False) -> str:
        self.history = []
        
            
        for _ in range(self.max_steps):
            
            user_prompt = build_user_prompt(user_input, ",\n".join(self.history))
            
            messages: list[ChatMessage] = [
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": user_prompt},
                ]
            
            try:
                raw_output = self.llm.complete(messages)
            except Exception as exc:
                raise RuntimeError("LLM request failed.") from exc
            
            try:
                action = parse_agent_action(raw_output)
            except Exception as exc:
                print(f"Error: {exc}. The raw output is: {raw_output}")
                self.history.append(json.dumps({
                    "type": "observation",
                    "content": f"你的上一次输出:\n{raw_output}\n无法解析为合法 agent JSON。错误是：{exc}\n"
                        "请重新输出。只能输出 JSON，格式必须是 thought, tool_call 或 final。"
                }, ensure_ascii=False))
                action = None
            
            if action is None:
                print("No legal action.Try to response again.")
                continue
            
            
            if isinstance(action, FinalAnswer):
                
                if show_reasoning_step:
                    self.history.append(raw_output)
                    return '\n'.join(self.history)
                
                                    
                return action.answer
            
            if isinstance(action, Thought):
                self.history.append(raw_output)
                continue
                
            if isinstance(action, ToolCall):
                tool_result = self.tools.call(action.tool, action.arguments)
                self.history.append(raw_output)
                self.history.append(json.dumps({
                    "type": "observation",
                    "content": tool_result,
                    }, ensure_ascii=False))
                
                continue

        raise RuntimeError("Agent reached max_steps before producing a final answer.")

