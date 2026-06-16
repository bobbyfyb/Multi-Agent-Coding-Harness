from abc import ABC, abstractmethod
from turtle import st
from typing import Any, Callable, Dict, List

from pydantic import BaseModel


class ToolParameter(BaseModel):
    name: str
    type: str
    description: str
    required: bool = True
    default: Any = None

class Tool(ABC):
    def __init__(self, name: str, description: str) -> None:
        self.name = name
        self.description = description
    
    @abstractmethod
    def run(self, parameters: Dict[str, Any]) -> str:
        pass
    
    @abstractmethod
    def get_parameters(self) -> List[ToolParameter]:
        pass
    

class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._functions: dict[str, dict[str, Any]] = {}
    
    def register_tool(self, tool: Tool):
        if tool.name in self._tools:
            print(f"⚠️ 警告:工具 '{tool.name}' 已存在，将被覆盖。")
        self._tools[tool.name] = tool
        print(f"✅ 工具 '{tool.name}' 已注册。")
    
    def register_function(self, name: str, description: str, func: Callable[[str], str]):
        if name in self._functions:
            print(f"⚠️ 警告:工具 '{name}' 已存在，将被覆盖。")
        self._functions[name] = {
            "description": description,
            "func": func
        }
        print(f"✅ 工具 '{name}' 已注册。")

    def get_tools_description(self) -> str:
        descriptions = []

        for tool in self._tools.values():
            descriptions.append(f"- {tool.name}: {tool.description}")
            
        for name, info in self._functions.items():
            descriptions.append(f"- {name}: {info['description']}")
            
        return "\n".join(descriptions) if descriptions else "暂无可用工具"
    
    