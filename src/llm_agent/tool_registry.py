from dataclasses import dataclass
from typing import Any

from llm_agent.schemas import ToolFunction, ToolSpec


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    func: ToolFunction


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        func: ToolFunction,
    ) -> None:
        if name in self._tools:
            raise ValueError(f"Tool already registered: {name}")

        self._tools[name] = Tool(
            name=name,
            description=description,
            parameters=parameters,
            func=func,
        )

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        tool = self._tools.get(name)
        if tool is None:
            return {"ok": False, "error": f"Unknown tool: {name}"}

        try:
            return {"ok": True, "result": tool.func(**arguments)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def tool_specs(self) -> list[ToolSpec]:
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            }
            for tool in self._tools.values()
        ]

