from llm_agent.tools import build_tool_registry, load_tool_configs


def test_load_tool_configs_reads_default_json() -> None:
    configs = load_tool_configs()

    assert [config["name"] for config in configs] == [
        "add",
        "get_weather",
        "search_info",
    ]


def test_build_tool_registry_from_default_json() -> None:
    registry = build_tool_registry()

    assert registry.call("add", {"a": 1, "b": 2}) == {"ok": True, "result": 3}
    assert registry.call("get_weather", {"city": "上海"}) == {
        "ok": True,
        "result": "上海 今天晴，气温 25 度。",
    }

    search_result = registry.call("search_info", {"query": "agent", "top_k": 2})

    assert search_result["ok"] is True
    assert len(search_result["result"]) == 2
