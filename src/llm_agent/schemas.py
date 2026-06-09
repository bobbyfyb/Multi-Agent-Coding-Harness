from dataclasses import dataclass
from typing import Any, Callable, Literal, TypedDict


class ChatMessage(TypedDict):
    role: Literal["system", "user", "assistant"]
    content: str


class ToolSpec(TypedDict):
    name: str
    description: str
    parameters: dict[str, Any]


class ToolConfig(ToolSpec):
    function: str


@dataclass(frozen=True)
class ToolCall:
    tool: str
    arguments: dict[str, Any]

@dataclass(frozen=True)
class Thought:
    content: str
@dataclass(frozen=True)
class FinalAnswer:
    answer: str
    
@dataclass(frozen=True)
class AgentPlan:
    content: list[str]



AgentAction = ToolCall | FinalAnswer | Thought | AgentPlan


ToolFunction = Callable[..., Any]
