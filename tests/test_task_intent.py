from typing import Any

from llm_agent.llm_client import LLMResponse
from llm_agent.task_intent import (
    TASK_INTENT_SYSTEM_PROMPT,
    TaskIntentClassifier,
    rule_based_requires_plan,
)


class FakeClassifierLLM:
    def __init__(
        self,
        responses: list[LLMResponse] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.responses = responses or []
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: Any = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        self.calls.append(
            {
                "messages": messages,
                "tools": tools,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
        )
        if self.error is not None:
            raise self.error
        return self.responses.pop(0)


def _response(content: str) -> LLMResponse:
    return LLMResponse(content=content, tool_calls=[], raw={})


def test_task_intent_classifier_uses_lightweight_llm_call_and_caches() -> None:
    llm = FakeClassifierLLM([_response("YES")])
    classifier = TaskIntentClassifier(llm=llm)
    prompt = "Refactor authentication and add regression tests."

    assert classifier.requires_plan(prompt) is True
    assert classifier.requires_plan(prompt) is True

    assert len(llm.calls) == 1
    call = llm.calls[0]
    assert call["tools"] is None
    assert call["max_tokens"] == 8
    assert call["temperature"] == 0
    assert call["messages"] == [
        {"role": "system", "content": TASK_INTENT_SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]


def test_task_intent_classifier_accepts_no_response() -> None:
    classifier = TaskIntentClassifier(llm=FakeClassifierLLM([_response("NO.")]))

    assert classifier.requires_plan("Explain what @dataclass does.") is False


def test_task_intent_classifier_falls_back_for_invalid_or_failed_response() -> None:
    invalid = TaskIntentClassifier(llm=FakeClassifierLLM([_response("MAYBE")]))
    failed = TaskIntentClassifier(
        llm=FakeClassifierLLM(error=RuntimeError("request failed"))
    )

    assert invalid.requires_plan("Implement tracing and add tests.") is True
    assert failed.requires_plan("Hello, introduce yourself.") is False


def test_rule_based_requires_plan_handles_empty_and_coding_requests() -> None:
    assert rule_based_requires_plan("") is False
    assert rule_based_requires_plan("Please fix the parser.") is True
    assert rule_based_requires_plan("What is a Python dataclass?") is False
