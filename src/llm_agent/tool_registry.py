from collections.abc import Iterable
from dataclasses import dataclass
from importlib import import_module
from typing import Any, cast

from llm_agent.schemas import ToolConfig, ToolFunction, ToolSpec


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

    def register_from_config(self, config: ToolConfig) -> None:
        self.register(
            name=config["name"],
            description=config["description"],
            parameters=config["parameters"],
            func=_load_tool_function(config["function"]),
        )

    def register_many(self, configs: Iterable[ToolConfig]) -> None:
        for config in configs:
            self.register_from_config(config)

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


def _load_tool_function(import_path: str) -> ToolFunction:
    module_name, separator, function_name = import_path.partition(":")
    if not separator or not module_name or not function_name:
        raise ValueError(
            f"Tool function import path must use 'module:function': {import_path}"
        )

    module = import_module(module_name)
    func = getattr(module, function_name)
    if not callable(func):
        raise TypeError(f"Configured tool is not callable: {import_path}")

    return cast(ToolFunction, func)
