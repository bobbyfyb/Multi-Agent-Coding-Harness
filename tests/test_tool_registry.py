from llm_agent.tool_registry import ToolDefinition, ToolRegistry


def test_tool_registry_calls_registered_tool() -> None:
    registry = ToolRegistry()
    registry.register(
        name="add",
        description="Add two numbers.",
        parameters={},
        func=lambda a, b: a + b,
    )

    assert registry.call("add", {"a": 1, "b": 2}) == {"ok": True, "result": 3}


def test_tool_registry_reports_unknown_tool() -> None:
    registry = ToolRegistry()

    result = registry.call("missing", {})

    assert result["ok"] is False
    assert "Unknown tool" in result["error"]


def test_tool_registry_registers_tool_definitions() -> None:
    registry = ToolRegistry()
    registry.register_many(
        [
            ToolDefinition(
                name="double",
                description="Double a number.",
                parameters={},
                func=lambda value: value * 2,
            )
        ]
    )

    assert registry.call("double", {"value": 4}) == {"ok": True, "result": 8}
