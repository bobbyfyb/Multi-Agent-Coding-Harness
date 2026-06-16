from pathlib import Path
import sys

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent / "src"))

from llm_agent.agent import PlanAndExecuteAgent, ReActAgent, ReflectionAgent
from llm_agent.llm_client import LLMClient
from llm_agent.prompts import build_reAct_system_prompt
from llm_agent.tools import build_tool_registry

load_dotenv()

def build_reAct_agent() -> ReActAgent:
    registry = build_tool_registry()
    llm = LLMClient()
    return ReActAgent(
        llm=llm,
        tools=registry,
        history=[],
        system_prompt=build_reAct_system_prompt(registry.tool_specs()),
    )

def build_plan_and_execute_agent() -> PlanAndExecuteAgent:
    llm = LLMClient()
    return PlanAndExecuteAgent(
        llm=llm,
    )
    
def build_reflection_agent() -> ReflectionAgent:
    llm = LLMClient()
    return ReflectionAgent(
        llm=llm,
    )




def main() -> None:
    # agent = build_reAct_agent()
    # answer = agent.run("小米汽车的最新款是什么，什么时候发售", show_reasoning_step=True)
    # print(answer)

    # agent = build_plan_and_execute_agent()
    # answer = agent.run("一个水果店周一卖出了15个苹果。周二卖出的苹果数量是周一的两倍。周三卖出的数量比周二少了5个。请问这三天总共卖出了多少个苹果？")
    # print(answer)

    agent = build_reflection_agent()
    answer = agent.run("编写一个Python函数，找出1到n之间所有的素数 (prime numbers)。")
    print(answer)



if __name__ == "__main__":
    main()
