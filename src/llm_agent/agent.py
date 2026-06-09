import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from llm_agent.llm_client import LLMClient
from llm_agent.parser import parse_agent_action
from llm_agent.prompts import build_excutor_system_prompt, build_excutor_user_prompt, build_plan_system_prompt, build_reAct_user_prompt, build_reflection_initial_prompt, build_reflection_refinement_prompt, build_reflection_reflect_prompt
from llm_agent.schemas import AgentPlan, ChatMessage, FinalAnswer, GenericAgentAction, Thought, ToolCall
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
            
            user_prompt = build_reAct_user_prompt(user_input, ",\n".join(self.history))
            
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


@dataclass
class Planner:
    llm: LLMClient
    system_prompt: str
    
    def plan(self, user_input: str) -> list[str]:
        """
        根据用户问题生成一个行动计划。
        """
        
        messages: list[ChatMessage]= [{"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": f"问题：{user_input}"}]
        
        print("\n--- 正在生成计划 ---")
        
        raw_output = self.llm.complete(messages)
        
        try:
            action = parse_agent_action(raw_output)
        except Exception as e:
            raise RuntimeError("LLM request failed.") from e
        
        
        if isinstance(action, AgentPlan):
            
            print(f"\n--- 计划已生成  ---\n计划内容:{action.content}")
            
            return action.content if isinstance(action.content, list) else []
        
        print("Error in agent plan.")
        return []         
            

@dataclass
class Executor:
    llm: LLMClient
    system_prompt: str
    
    def execute(self, user_input: str, plan: list[str]) -> str:
        """
        根据计划，逐步执行并解决问题。
        """
        history = []
        
        print("\n--- 正在执行计划 ---")
        
        for i, step in enumerate(plan):
            print(f"\n-> 正在执行步骤 {i + 1}/{len(plan)}: {step}")
            
            user_prompt = build_excutor_user_prompt(user_input, 
                                                    "\n".join(history) if history else "无", 
                                                    "\n".join(plan), step)
            
            messages: list[ChatMessage] = [{"role": "system", "content": self.system_prompt},
                                            {"role": "user", "content": user_prompt}]
            
            raw_output = self.llm.complete(messages)
            
            history.append(f"步骤 {i+1}：{step}\n结果：{raw_output}\n")
            
            print(f"✅ 步骤 {i+1} 已完成，结果: {raw_output}")
        
        print(f"\n--- 任务完成 ---\n最终答案: ")
        final_answer = raw_output
        return final_answer
        
    
@dataclass
class PlanAndExecuteAgent:
    llm: LLMClient
    
    def __post_init__(self):
        self.planner = Planner(self.llm, build_plan_system_prompt())
        self.executor = Executor(self.llm, build_excutor_system_prompt())
        

    def run(self, user_input: str) -> str:
        """
        使用 Plan-and-Execute 策略解决问题。
        """
        plan = self.planner.plan(user_input)
        if not plan:
            raise RuntimeError("Failed to generate a valid plan.")
        
        return self.executor.execute(user_input, plan)


class Memory:
    def __init__(self):
        self.records: List[Dict[str, Any]] = []
    
    def add_record(self, record_type: str, content: str):
        self.records.append({"type": record_type, "content": content})
        print(f"📝 记忆已更新，新增一条 '{record_type}' 记录。")

    def get_trajectory(self) -> str:
        
        trajectory_parts = []
        for record in self.records:
            if record['type'] == "execution":
                trajectory_parts.append(f"--- 上一轮尝试 （代码）---\n{record['content']}")
            elif record['type'] == "reflection":
                trajectory_parts.append(f"--- 评审员反馈 ---\n{record['content']}")
        
        return "\n\n".join(trajectory_parts)

    def get_last_execution(self) -> Optional[str]:
        for record in reversed(self.records):
            if record['type'] == "execution":
                return record['content']
        return None

@dataclass
class ReflectionAgent:
    llm: LLMClient
    max_iterations: int = 3
    memory: Memory = Memory()
    
    def run(self, user_input: str) -> str:
        print(f"\n --- 开始处理任务 ---\n任务：{user_input}")

        print("\n --- 正在进行初次尝试 ---")
        
        initial_prompt = build_reflection_initial_prompt(user_input)
        raw_output = self._get_llm_response(initial_prompt)
        
        try:
            initial_code = parse_agent_action(raw_output)
        except Exception as e:
            raise RuntimeError("LLM request failed.") from e
        
        assert isinstance(initial_code, GenericAgentAction) and initial_code.type == "execution"
        
        self.memory.add_record("execution", initial_code.content)
        
        for i in range(self.max_iterations):
            print(f"\n--- 第{i+1}/{self.max_iterations} 次反思 ---")

            print("\n--- 正在进行反思 ---")
            last_code = self.memory.get_last_execution() or ""
            reflection_prompt = build_reflection_reflect_prompt(user_input, last_code)
            raw_output = self._get_llm_response(reflection_prompt)
            
            try:
                feedback = parse_agent_action(raw_output)
            except Exception as e:
                raise RuntimeError("LLM request failed.") from e
            
            assert isinstance(feedback, GenericAgentAction) and feedback.type == "reflection"
            
            
            self.memory.add_record("reflection", feedback.content)
            
            if "无需改进" in feedback.content:
                print("\n✅ 反思认为代码已无需改进，任务完成。")
                break
            
            print("\n--- 正在进行代码优化 ---")
            
            refinement_prompt = build_reflection_refinement_prompt(user_input, last_code, feedback.content)
            raw_output = self._get_llm_response(refinement_prompt)
            
            try:
                new_code = parse_agent_action(raw_output)
            except Exception as e:
                raise RuntimeError("LLM request failed.") from e
            
            assert isinstance(new_code, GenericAgentAction) and new_code.type == "execution"
            
            self.memory.add_record("execution", new_code.content)
        
        final_code = self.memory.get_last_execution() or ""
        print(f"\n--- 任务完成 ---\n最终代码：\n```python\n{final_code}\n```")
        
        return self.memory.get_trajectory()
    
    def _get_llm_response(self, prompt: str) -> str:
        messages: list[ChatMessage] = [{"role": "user", "content": prompt}]
        return self.llm.complete(messages)