from typing import Any


def search_info(query: str, top_k: int = 5) -> list[dict[str, Any]]:
    passages = [
        {"title": "test title_1", "content": f"test content for {query}"},
        {"title": "test title_2", "content": f"test content for {query}"},
        {"title": "test title_3", "content": f"test content for {query}"},
        {"title": "test title_4", "content": f"test content for {query}"},
        {"title": "test title_5", "content": f"test content for {query}"},
    ]
    return passages[:top_k]
