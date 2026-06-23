from dataclasses import dataclass
import os
from pathlib import Path
from serpapi import Client

from llm_agent.tool_registry import ToolDefinition, ToolRegistry

@dataclass(frozen=True)
class SearchTools:
    workdir: Path
    
    def __post_init__(self) -> None:
        object.__setattr__(self, "workdir", self.workdir.resolve())
    
    
    def serpapi_search(self, query: str, engine: str = 'google', top_k: int = 5) -> str:
        try:
            api_key = os.getenv("SEARCH_API_KEY")
            if not api_key:
                raise ValueError("No available search_api_key found!")

            params = {
                "engine": engine,
                "q": query,
                "api_key": api_key,
                "gl": "cn",
                "hl": "zh-cn",
            }
            
            
            SerpClient = Client(api_key=api_key)
            
            results = SerpClient.search(params=params).as_dict()
            
            if "answer_box_list" in results:
                return "\n".join(results["answer_box_list"])
            if "answer_box" in results and "answer" in results["answer_box"]:
                return results["answer_box"]["answer"]
            if "knowledge_graph" in results and "description" in results["knowledge_graph"]:
                return results["knowledge_graph"]["description"]
            if "organic_results" in results and results["organic_results"]:
                snippets = [
                    f"[{i+1}] {res.get('title', '')}\n{res.get('snippet', '')}"
                    for i, res in enumerate(results["organic_results"][:top_k])
                ]
                return "\n\n".join(snippets)

            return f"Sorry, no results found for {query}."
        except Exception as e:
            return f"Error in Searching: {e}"

def search_tool_definitions(workdir: Path | str | None = None) -> list[ToolDefinition]:
        tools = SearchTools(workdir=Path.cwd() if workdir is None else Path(workdir))
        
        return [
            ToolDefinition(
                name="search",
                description="search the web to get useful information",
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "The query to be searched."
                        },
                        "engine": {
                            "type": "string",
                            "description": "The search engine to be used.(google, bing, etc.)"
                        },
                        "top_k": {
                            "type": "integer",
                            "description": "The number of searched results to be used."
                        },
                    },
                    "required": ["query"],
                },
                func=tools.serpapi_search,
            )
        ]

def register_tools(
    registry: ToolRegistry,
    *,
    workdir: Path | str | None = None,
) ->None:
    registry.register_many(search_tool_definitions(workdir=workdir))