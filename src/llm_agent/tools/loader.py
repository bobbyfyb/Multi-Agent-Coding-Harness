import json
from collections.abc import Iterable
from importlib.resources import files
from pathlib import Path
from typing import cast

from llm_agent.schemas import ToolConfig
from llm_agent.tool_registry import ToolRegistry


def load_tool_configs(path: str | Path | None = None) -> list[ToolConfig]:
    if path is None:
        text = files("llm_agent.tools").joinpath("tool_configs.json").read_text(
            encoding="utf-8"
        )
    else:
        text = Path(path).read_text(encoding="utf-8")

    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError("Tool config JSON must be a list.")

    return cast(list[ToolConfig], data)


def build_tool_registry(configs: Iterable[ToolConfig] | None = None) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register_many(configs if configs is not None else load_tool_configs())
    return registry
