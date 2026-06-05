import json
import urllib.request
from dataclasses import dataclass

from llm_agent.schemas import ChatMessage


@dataclass
class LLMClient:
    api_url: str = "http://localhost:8000/v1/chat/completions"
    model: str = "qwen-coder"
    temperature: float = 0.0
    max_tokens: int = 512
    timeout: float = 120.0

    def complete(self, messages: list[ChatMessage]) -> str:
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }

        request = urllib.request.Request(
            self.api_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            data = json.loads(response.read().decode("utf-8"))

        return data["choices"][0]["message"]["content"]

