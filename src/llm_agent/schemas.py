from typing import Any, Callable, Literal, TypedDict


class ChatMessage(TypedDict):
    role: Literal["system", "user", "assistant"]
    content: str


class ToolSpec(TypedDict):
    name: str
    description: str
    parameters: dict[str, Any]


ToolFunction = Callable[..., Any]
