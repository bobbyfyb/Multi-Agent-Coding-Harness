from llm_agent.tool_registry import ToolDefinition, ToolRegistry


def get_weather(city: str) -> str:
    return f"{city} 今天晴，气温 25 度。"


def tool_definitions() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="get_weather",
            description="Get a mock weather report for a city.",
            parameters={
                "type": "object",
                "properties": {
                    "city": {
                        "type": "string",
                        "description": "The city name.",
                    }
                },
                "required": ["city"],
            },
            func=get_weather,
        )
    ]


def register_tools(registry: ToolRegistry) -> None:
    registry.register_many(tool_definitions())
