from dataclasses import dataclass
from typing import Any, Callable, TypedDict


ToolFunction = Callable[..., Any]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    func: ToolFunction
    requires_context: bool = False


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]
    func: ToolFunction
    requires_context: bool = False


class ToolSpec(TypedDict):
    name: str
    description: str
    parameters: dict[str, Any]


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        func: ToolFunction,
        *,
        requires_context: bool = False,
    ) -> None:
        if name in self._tools:
            raise ValueError(f"Tool already registered: {name}")

        self._tools[name] = Tool(
            name=name,
            description=description,
            parameters=parameters,
            func=func,
            requires_context=requires_context,
        )

    def register_definition(self, definition: ToolDefinition) -> None:
        self.register(
            name=definition.name,
            description=definition.description,
            parameters=definition.parameters,
            func=definition.func,
            requires_context=definition.requires_context,
        )

    def register_many(self, definitions: list[ToolDefinition]) -> None:
        for definition in definitions:
            self.register_definition(definition)

    def call(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        context: Any = None,
    ) -> dict[str, Any]:
        tool = self._tools.get(name)
        if tool is None:
            return {"ok": False, "error": f"Unknown tool: {name}"}

        try:
            if tool.requires_context:
                if context is None:
                    return {
                        "ok": False,
                        "error": f"Tool requires execution context: {name}",
                    }
                result = tool.func(context=context, **arguments)
            else:
                result = tool.func(**arguments)
            return {"ok": True, "result": result}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def names(self) -> list[str]:
        return list(self._tools)

    def subset(self, names: set[str]) -> "ToolRegistry":
        registry = ToolRegistry()
        for name, tool in self._tools.items():
            if name not in names:
                continue
            registry.register(
                name=tool.name,
                description=tool.description,
                parameters=tool.parameters,
                func=tool.func,
                requires_context=tool.requires_context,
            )
        return registry

    def tool_specs(self) -> list[ToolSpec]:
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            }
            for tool in self._tools.values()
        ]
