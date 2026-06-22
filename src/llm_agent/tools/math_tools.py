from llm_agent.tool_registry import ToolDefinition, ToolRegistry


def add(a: int, b: int) -> int:
    return a + b


def tool_definitions() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="add",
            description="Add two integers.",
            parameters={
                "type": "object",
                "properties": {
                    "a": {
                        "type": "integer",
                        "description": "The first integer.",
                    },
                    "b": {
                        "type": "integer",
                        "description": "The second integer.",
                    },
                },
                "required": ["a", "b"],
            },
            func=add,
        )
    ]


def register_tools(registry: ToolRegistry) -> None:
    registry.register_many(tool_definitions())
