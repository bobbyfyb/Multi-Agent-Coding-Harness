from pathlib import Path
import sys

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent / "src"))

from llm_agent.agent import Agent
from llm_agent.llm_client import LLMClient
from llm_agent.prompts import build_system_prompt
from llm_agent.tools import build_tool_registry

load_dotenv()

def build_agent() -> Agent:
    registry = build_tool_registry()
    llm = LLMClient()
    return Agent(
        llm=llm,
        tools=registry,
        history=[],
        system_prompt=build_system_prompt(registry.tool_specs()),
    )


def main() -> None:
    agent = build_agent()
    # answer = agent.run("请计算 123 + 456，然后用一句话告诉我结果。", show_reasoning_step=True)
    # print(answer)
    # answer = agent.run("请告诉我上海的天气。", show_reasoning_step=True)
    # print(answer)
    answer = agent.run("极限竞速地平线的最新作的舞台是哪里。", show_reasoning_step=True)
    print(answer)




if __name__ == "__main__":
    main()
