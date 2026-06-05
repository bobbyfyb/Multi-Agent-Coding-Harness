import json
from dataclasses import dataclass

from llm_agent.llm_client import LLMClient
from llm_agent.parser import parse_agent_action
from llm_agent.schemas import ChatMessage, FinalAnswer, ToolCall
from llm_agent.tool_registry import ToolRegistry


@dataclass
class Agent:
    llm: LLMClient
    tools: ToolRegistry
    system_prompt: str
    max_steps: int = 5

    def run(self, user_input: str, show_reasoning_step: bool = False) -> str:
        messages: list[ChatMessage] = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": user_input},
        ]

        reasoning = []
            
        for _ in range(self.max_steps):
            try:
                raw_output = self.llm.complete(messages)
            except Exception as exc:
                raise RuntimeError("LLM request failed.") from exc
            
            try:
                action = parse_agent_action(raw_output)
            except Exception as exc:
                print(f"Error: {exc}. The raw output is: {raw_output}")
                messages.append({"role": "assistant", "content": raw_output})
                messages.append({
                    "role": "user",
                    "content": (
                        f"你的上一次输出无法解析为合法 agent JSON。错误是：{exc}\n"
                        "请重新输出。只能输出 JSON，格式必须是 tool_call 或 final。"
                    ),
                })
                action = None
            
            if action is None:
                print("Try to response again.")
                continue
            
            
            if isinstance(action, FinalAnswer):
                
                if show_reasoning_step:
                    reasoning.append(raw_output)
                    return "\n".join(reasoning)
                                    
                return action.answer

            if isinstance(action, ToolCall):
                tool_result = self.tools.call(action.tool, action.arguments)
                messages.append({"role": "assistant", "content": raw_output})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"工具 {action.tool} 的调用结果如下：\n"
                            f"{json.dumps(tool_result, ensure_ascii=False)}\n"
                            "请继续。仍然只输出 JSON。"
                        ),
                    }
                )
                
                if show_reasoning_step:
                    reasoning.append(f"工具 {action.tool} 的调用结果如下：\n 输入参数：{json.dumps(action.arguments, ensure_ascii=False)}\n 输出结果：{json.dumps(tool_result, ensure_ascii=False)}")
                
                continue

        raise RuntimeError("Agent reached max_steps before producing a final answer.")

