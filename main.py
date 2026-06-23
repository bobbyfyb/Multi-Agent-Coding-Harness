from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent / "src"))

from llm_agent.agent import Agent, print_agent_event
from llm_agent.context_builder import AgentContextBuilder, PromptSection
from llm_agent.hooks import HookContext, build_default_hook_manager
from llm_agent.llm_client import LLMClient
from llm_agent.tools import build_default_registry


def build_agent() -> Agent:
    workdir = Path.cwd()
    registry = build_default_registry(workdir=workdir)
    llm = LLMClient(provider="anthropic")
    return Agent(
        llm=llm,
        tools=registry,
        hooks=build_default_hook_manager(workdir=workdir),
        workdir=workdir,
        max_steps=None,
        context_builder=AgentContextBuilder(
            sections=[
                PromptSection(
                    name="workspace",
                    content=f"Current Working DIR: {workdir}",
                    priority=20,
                )
            ]
        ),
    )


def main() -> None:
    agent = build_agent()
    messages = agent.new_messages()
    
    while True:
        try:
            query = input("\033[36mInput your question >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        agent.hooks.trigger_hooks(
            "UserPromptSubmit",
            query,
            HookContext(messages=messages, workdir=Path.cwd()),
        )
        messages.append({"role": "user", "content": query})
        agent.run(messages, on_event=print_agent_event)
        


if __name__ == "__main__":
    main()
