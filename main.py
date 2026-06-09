from pathlib import Path
import sys

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent / "src"))

from llm_agent.agent import ReActAgent
from llm_agent.llm_client import LLMClient
from llm_agent.prompts import build_system_prompt
from llm_agent.tools import build_tool_registry

load_dotenv()

def build_reAct_agent() -> ReActAgent:
    registry = build_tool_registry()
    llm = LLMClient()
    return ReActAgent(
        llm=llm,
        tools=registry,
        history=[],
        system_prompt=build_system_prompt(registry.tool_specs()),
    )


def main() -> None:
    agent = build_reAct_agent()
    # answer = agent.run("请计算 123 + 456，然后用一句话告诉我结果。", show_reasoning_step=True)
    # print(answer)
    # answer = agent.run("请告诉我上海的天气。", show_reasoning_step=True)
    # print(answer)
    answer = agent.run("小米汽车的最新款是什么，什么时候发售", show_reasoning_step=True)
    print(answer)




if __name__ == "__main__":
    main()
