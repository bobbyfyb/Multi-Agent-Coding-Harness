import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

from llm_agent.context_builder import ContextBuilder, StaticContextBuilder
from llm_agent.hooks import HookContext, HookManager
from llm_agent.llm_client import LLMClient
from llm_agent.tool_registry import ToolRegistry


AgentEventType = Literal[
    "step",
    "tool_call",
    "permission_granted",
    "permission_denied",
    "tool_result",
    "final",
]


@dataclass(frozen=True)
class AgentEvent:
    type: AgentEventType
    step: int
    data: dict[str, Any]


AgentCallback = Callable[[AgentEvent], None]

ANSI_RESET = "\033[0m"
ANSI_BOLD = "\033[1m"
ANSI_DIM = "\033[2m"
ANSI_BLUE = "\033[34m"
ANSI_YELLOW = "\033[33m"
ANSI_GREEN = "\033[32m"
ANSI_RED = "\033[31m"
ANSI_MAGENTA = "\033[35m"


@dataclass
class Agent:
    llm: LLMClient
    tools: ToolRegistry
    context_builder: ContextBuilder | str
    max_steps: int | None = 5
    hooks: HookManager = field(default_factory=HookManager)
    workdir: Path | str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.context_builder, str):
            self.context_builder = StaticContextBuilder(self.context_builder)
        if self.workdir is None:
            self.workdir = Path.cwd()
        else:
            self.workdir = Path(self.workdir).resolve()

    def new_messages(self) -> list[dict[str, Any]]:
        return self.context_builder.new_messages()

    def run(
        self,
        messages: list[dict[str, Any]],
        *,
        on_event: AgentCallback | None = None,
    ) -> None:
        tool_specs = self.tools.tool_specs()

        step_index = 0
        while self.max_steps is None or step_index < self.max_steps:
            step = step_index + 1
            hook_context = HookContext(
                messages=messages,
                step=step,
                workdir=self.workdir,
            )
            _emit(on_event, "step", step, {"message": "calling llm"})

            try:
                response = self.llm.chat(
                    messages,
                    tools=tool_specs,
                    tool_choice="auto",
                )
            except Exception as exc:
                raise RuntimeError("LLM request failed.") from exc

            if not response.tool_calls:
                messages.append({"role": "assistant", "content": response.content})
                _emit(on_event, "final", step, {"content": response.content})
                self.hooks.trigger_hooks("Stop", messages, hook_context)
                return

            messages.append(self.llm.assistant_message(response))

            tool_results = []
            for tool_call in response.tool_calls:
                _emit(
                    on_event,
                    "tool_call",
                    step,
                    {
                        "id": tool_call.id,
                        "name": tool_call.name,
                        "arguments": tool_call.arguments,
                    },
                )

                pre_tool_result = self.hooks.trigger_hooks(
                    "PreToolUse",
                    tool_call,
                    hook_context,
                )
                if pre_tool_result is not None and pre_tool_result.denied:
                    tool_result = pre_tool_result.value
                    tool_results.append((tool_call, tool_result))
                    _emit(
                        on_event,
                        "permission_denied",
                        step,
                        {
                            "id": tool_call.id,
                            "name": tool_call.name,
                            "reason": pre_tool_result.reason,
                            "result": tool_result,
                        },
                    )
                    _emit(
                        on_event,
                        "tool_result",
                        step,
                        {
                            "id": tool_call.id,
                            "name": tool_call.name,
                            "result": tool_result,
                        },
                    )
                    continue

                if pre_tool_result is not None and pre_tool_result.reason:
                    _emit(
                        on_event,
                        "permission_granted",
                        step,
                        {
                            "id": tool_call.id,
                            "name": tool_call.name,
                            "reason": pre_tool_result.reason,
                        },
                    )

                tool_result = self.tools.call(tool_call.name, tool_call.arguments)
                post_tool_result = self.hooks.trigger_hooks(
                    "PostToolUse",
                    tool_call,
                    tool_result,
                    hook_context,
                )
                if post_tool_result is not None and post_tool_result.replaces_result:
                    tool_result = post_tool_result.value

                tool_results.append((tool_call, tool_result))
                _emit(
                    on_event,
                    "tool_result",
                    step,
                    {
                        "id": tool_call.id,
                        "name": tool_call.name,
                        "result": tool_result,
                    },
                )

            messages.extend(self.llm.tool_result_messages(tool_results))
            step_index += 1

        raise RuntimeError("Agent reached max_steps before producing a final answer.")


def print_agent_event(event: AgentEvent) -> None:
    if event.type == "step":
        print(f"\n{ANSI_BLUE}[step {event.step}] calling llm{ANSI_RESET}")
        return

    if event.type == "tool_call":
        arguments = json.dumps(event.data["arguments"], ensure_ascii=False)
        print(
            f"{ANSI_YELLOW}> [tool call] {event.data['name']}{ANSI_RESET} "
            f"{ANSI_DIM}{arguments}{ANSI_RESET}"
        )
        return

    if event.type == "tool_result":
        result = event.data["result"]
        result_color = ANSI_GREEN
        if isinstance(result, dict) and result.get("ok") is False:
            result_color = ANSI_RED

        print(
            f"{result_color}[tool result] {event.data['name']} ->{ANSI_RESET} "
            f"{json.dumps(result, ensure_ascii=False)}"
        )
        return

    if event.type == "permission_granted":
        print(
            f"{ANSI_GREEN}[permission granted] {event.data['name']} ->{ANSI_RESET} "
            f"{ANSI_DIM}{event.data['reason']}{ANSI_RESET}"
        )
        return

    if event.type == "permission_denied":
        print(
            f"{ANSI_RED}[permission denied] {event.data['name']} ->{ANSI_RESET} "
            f"{event.data['reason']}"
        )
        return

    if event.type == "final":
        print(f"\n{ANSI_BOLD}{ANSI_MAGENTA}[final]{ANSI_RESET}\n{event.data['content']}")


def _emit(
    on_event: AgentCallback | None,
    event_type: AgentEventType,
    step: int,
    data: dict[str, Any],
) -> None:
    if on_event is None:
        return

    on_event(AgentEvent(type=event_type, step=step, data=data))
