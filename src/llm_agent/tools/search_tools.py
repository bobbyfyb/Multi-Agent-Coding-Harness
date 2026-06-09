import os
from typing import Any
from serpapi import SerpApiClient


def search_info(query: str, top_k: int = 5) -> str:
    print(f"🔍 正在执行 [SerpApi] 网页搜索: {query} --- 搜索结果数:{top_k}")
    try:
        api_key = os.getenv("SEARCH_API_KEY")
        if not api_key:
            return "Error: 未找到SEARCH_API_KEY 环境变量。"

        params = {
            "engine": "google",
            "q": query,
            "api_key": api_key,
            "gl": "cn",
            "hl": "zh-cn",
        }
        
        client = SerpApiClient(params)
        results = client.get_dict()
        
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