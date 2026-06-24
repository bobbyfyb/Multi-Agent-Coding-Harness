import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal
from uuid import uuid4

from llm_agent.context_builder import ContextBuilder, StaticContextBuilder
from llm_agent.hooks import HookContext, HookManager
from llm_agent.llm_client import LLMClient
from llm_agent.tool_registry import ToolRegistry


AgentEventType = Literal[
    "step",
    "task_context",
    "task_reminder",
    "tool_call",
    "permission_granted",
    "permission_denied",
    "tool_result",
    "final",
    "max_steps",
    "subagent_started",
    "subagent_completed",
    "subagent_failed",
]
AgentRunStatus = Literal["completed", "max_steps"]


@dataclass(frozen=True)
class AgentEvent:
    type: AgentEventType
    step: int
    data: dict[str, Any]
    agent_id: str = "agent"
    run_id: str = ""
    parent_run_id: str | None = None
    depth: int = 0


AgentCallback = Callable[[AgentEvent], None]


@dataclass(frozen=True)
class ToolExecutionContext:
    run_id: str
    agent_id: str
    parent_run_id: str | None
    depth: int
    step: int
    workdir: Path
    on_event: AgentCallback | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentRunResult:
    status: AgentRunStatus
    content: str
    steps: int
    tool_calls: int
    usage: dict[str, Any]
    run_id: str
    agent_id: str


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
    agent_id: str = "agent"
    parent_run_id: str | None = None
    depth: int = 0

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
        run_id: str | None = None,
    ) -> AgentRunResult:
        tool_specs = self.tools.tool_specs()
        resolved_run_id = run_id or f"run-{uuid4().hex[:12]}"
        tool_call_count = 0
        usage: dict[str, Any] = {}
        last_content = ""

        step_index = 0
        while self.max_steps is None or step_index < self.max_steps:
            step = step_index + 1
            hook_context = HookContext(
                messages=messages,
                step=step,
                workdir=self.workdir,
                metadata={
                    "run_id": resolved_run_id,
                    "agent_id": self.agent_id,
                    "parent_run_id": self.parent_run_id,
                    "depth": self.depth,
                },
            )
            before_llm_result = self.hooks.trigger_hooks("BeforeLLM", hook_context)
            for hook_message in _hook_messages(before_llm_result):
                messages.append({"role": "user", "content": hook_message})
                event_type: AgentEventType = (
                    "task_context"
                    if hook_message.startswith("<current_tasks>")
                    else "task_reminder"
                )
                self._emit(
                    on_event,
                    event_type,
                    step,
                    {"content": hook_message},
                    resolved_run_id,
                )

            self._emit(
                on_event,
                "step",
                step,
                {"message": "calling llm"},
                resolved_run_id,
            )

            try:
                response = self.llm.chat(
                    messages,
                    tools=tool_specs,
                    tool_choice="auto",
                )
            except Exception as exc:
                raise RuntimeError("LLM request failed.") from exc

            last_content = response.content
            _merge_usage(usage, response.usage)
            if not response.tool_calls:
                messages.append({"role": "assistant", "content": response.content})
                self._emit(
                    on_event,
                    "final",
                    step,
                    {"content": response.content},
                    resolved_run_id,
                )
                self.hooks.trigger_hooks("Stop", messages, hook_context)
                return AgentRunResult(
                    status="completed",
                    content=response.content,
                    steps=step,
                    tool_calls=tool_call_count,
                    usage=usage,
                    run_id=resolved_run_id,
                    agent_id=self.agent_id,
                )

            messages.append(self.llm.assistant_message(response))

            tool_results = []
            for tool_call in response.tool_calls:
                tool_call_count += 1
                self._emit(
                    on_event,
                    "tool_call",
                    step,
                    {
                        "id": tool_call.id,
                        "name": tool_call.name,
                        "arguments": tool_call.arguments,
                    },
                    resolved_run_id,
                )

                pre_tool_result = self.hooks.trigger_hooks(
                    "PreToolUse",
                    tool_call,
                    hook_context,
                )
                if pre_tool_result is not None and pre_tool_result.denied:
                    tool_result = pre_tool_result.value
                    tool_results.append((tool_call, tool_result))
                    self._emit(
                        on_event,
                        "permission_denied",
                        step,
                        {
                            "id": tool_call.id,
                            "name": tool_call.name,
                            "reason": pre_tool_result.reason,
                            "result": tool_result,
                        },
                        resolved_run_id,
                    )
                    self._emit(
                        on_event,
                        "tool_result",
                        step,
                        {
                            "id": tool_call.id,
                            "name": tool_call.name,
                            "result": tool_result,
                        },
                        resolved_run_id,
                    )
                    continue

                if pre_tool_result is not None and pre_tool_result.reason:
                    self._emit(
                        on_event,
                        "permission_granted",
                        step,
                        {
                            "id": tool_call.id,
                            "name": tool_call.name,
                            "reason": pre_tool_result.reason,
                        },
                        resolved_run_id,
                    )

                tool_context = ToolExecutionContext(
                    run_id=resolved_run_id,
                    agent_id=self.agent_id,
                    parent_run_id=self.parent_run_id,
                    depth=self.depth,
                    step=step,
                    workdir=self.workdir,
                    on_event=on_event,
                )
                tool_result = self.tools.call(
                    tool_call.name,
                    tool_call.arguments,
                    context=tool_context,
                )
                post_tool_result = self.hooks.trigger_hooks(
                    "PostToolUse",
                    tool_call,
                    tool_result,
                    hook_context,
                )
                if post_tool_result is not None and post_tool_result.replaces_result:
                    tool_result = post_tool_result.value

                tool_results.append((tool_call, tool_result))
                self._emit(
                    on_event,
                    "tool_result",
                    step,
                    {
                        "id": tool_call.id,
                        "name": tool_call.name,
                        "result": tool_result,
                    },
                    resolved_run_id,
                )

            messages.extend(self.llm.tool_result_messages(tool_results))
            step_index += 1

        self._emit(
            on_event,
            "max_steps",
            step_index,
            {"content": last_content, "max_steps": self.max_steps},
            resolved_run_id,
        )
        return AgentRunResult(
            status="max_steps",
            content=last_content,
            steps=step_index,
            tool_calls=tool_call_count,
            usage=usage,
            run_id=resolved_run_id,
            agent_id=self.agent_id,
        )

    def _emit(
        self,
        on_event: AgentCallback | None,
        event_type: AgentEventType,
        step: int,
        data: dict[str, Any],
        run_id: str,
    ) -> None:
        _emit(
            on_event,
            event_type,
            step,
            data,
            agent_id=self.agent_id,
            run_id=run_id,
            parent_run_id=self.parent_run_id,
            depth=self.depth,
        )


def print_agent_event(event: AgentEvent) -> None:
    prefix = _event_prefix(event)

    if event.type == "step":
        print(f"\n{prefix}{ANSI_BLUE}[step {event.step}] calling llm{ANSI_RESET}")
        return

    if event.type == "tool_call":
        arguments = json.dumps(event.data["arguments"], ensure_ascii=False)
        print(
            f"{prefix}{ANSI_YELLOW}> [tool call] {event.data['name']}{ANSI_RESET} "
            f"{ANSI_DIM}{arguments}{ANSI_RESET}"
        )
        return

    if event.type == "task_context":
        print(f"{prefix}{ANSI_DIM}[task context updated]{ANSI_RESET}")
        return

    if event.type == "task_reminder":
        print(
            f"{prefix}{ANSI_YELLOW}[task reminder]{ANSI_RESET} "
            f"{event.data['content']}"
        )
        return

    if event.type == "tool_result":
        result = event.data["result"]
        result_color = ANSI_GREEN
        if isinstance(result, dict) and result.get("ok") is False:
            result_color = ANSI_RED

        print(
            f"{prefix}{result_color}[tool result] "
            f"{event.data['name']} ->{ANSI_RESET} "
            f"{json.dumps(result, ensure_ascii=False)}"
        )
        return

    if event.type == "permission_granted":
        print(
            f"{prefix}{ANSI_GREEN}[permission granted] "
            f"{event.data['name']} ->{ANSI_RESET} "
            f"{ANSI_DIM}{event.data['reason']}{ANSI_RESET}"
        )
        return

    if event.type == "permission_denied":
        print(
            f"{prefix}{ANSI_RED}[permission denied] "
            f"{event.data['name']} ->{ANSI_RESET} "
            f"{event.data['reason']}"
        )
        return

    if event.type == "final":
        print(
            f"\n{prefix}{ANSI_BOLD}{ANSI_MAGENTA}[final]{ANSI_RESET}\n"
            f"{event.data['content']}"
        )
        return

    if event.type == "max_steps":
        print(
            f"{prefix}{ANSI_RED}[max steps reached]{ANSI_RESET} "
            f"{event.data['max_steps']}"
        )
        return

    if event.type == "subagent_started":
        print(
            f"{prefix}{ANSI_MAGENTA}[subagent started]{ANSI_RESET} "
            f"{event.data['task']}"
        )
        return

    if event.type == "subagent_completed":
        print(
            f"{prefix}{ANSI_GREEN}[subagent completed]{ANSI_RESET} "
            f"{event.data['status']} in {event.data['steps']} steps"
        )
        return

    if event.type == "subagent_failed":
        print(
            f"{prefix}{ANSI_RED}[subagent failed]{ANSI_RESET} "
            f"{event.data['error']}"
        )


def _emit(
    on_event: AgentCallback | None,
    event_type: AgentEventType,
    step: int,
    data: dict[str, Any],
    *,
    agent_id: str = "agent",
    run_id: str = "",
    parent_run_id: str | None = None,
    depth: int = 0,
) -> None:
    if on_event is None:
        return

    on_event(
        AgentEvent(
            type=event_type,
            step=step,
            data=data,
            agent_id=agent_id,
            run_id=run_id,
            parent_run_id=parent_run_id,
            depth=depth,
        )
    )


def _hook_messages(result: Any) -> list[str]:
    if result is None:
        return []
    messages = result.data.get("messages", [])
    if not isinstance(messages, list):
        return []
    return [message for message in messages if isinstance(message, str) and message]


def _merge_usage(target: dict[str, Any], usage: dict[str, Any]) -> None:
    for key, value in usage.items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            target[key] = target.get(key, 0) + value
        else:
            target[key] = value


def _event_prefix(event: AgentEvent) -> str:
    if event.depth <= 0:
        return ""
    return f"{ANSI_DIM}[{event.agent_id}] {ANSI_RESET}"
