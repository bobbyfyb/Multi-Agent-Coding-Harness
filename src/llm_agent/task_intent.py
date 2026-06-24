from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any

from llm_agent.llm_client import LLMClient


TASK_INTENT_SYSTEM_PROMPT = """
Decide whether the user request needs a multi-step execution plan with persistent
task tracking.

Reply YES when the request is substantial enough to benefit from explicit task
creation, progress tracking, and verification evidence.

Reply NO for greetings, explanations, general questions, simple lookups, and
small one-step changes that do not need persistent planning.

Reply with exactly YES or NO.
""".strip()

PLAN_KEYWORDS = (
    "implement",
    "create",
    "add",
    "modify",
    "change",
    "refactor",
    "fix",
    "debug",
    "test",
    "write",
    "build",
    "实现",
    "创建",
    "新增",
    "修改",
    "重构",
    "修复",
    "测试",
    "开发",
    "补全",
)


@dataclass
class TaskIntentClassifier:
    llm: LLMClient
    _cache: dict[str, bool] = field(default_factory=dict, init=False)

    def requires_plan(self, text: str) -> bool:
        normalized = text.strip()
        if not normalized:
            return False
        if normalized in self._cache:
            return self._cache[normalized]

        try:
            response = self.llm.chat(
                [
                    {"role": "system", "content": TASK_INTENT_SYSTEM_PROMPT},
                    {"role": "user", "content": normalized},
                ],
                tools=None,
                max_tokens=8,
                temperature=0,
            )
            decision = _parse_decision(response.content)
        except Exception:
            decision = None

        result = (
            rule_based_requires_plan(normalized)
            if decision is None
            else decision
        )
        self._cache[normalized] = result
        return result


def rule_based_requires_plan(text: str) -> bool:
    normalized = text.strip()
    if not normalized:
        return False

    lowered = normalized.lower()
    if len(normalized) >= 80:
        return True
    return any(keyword in lowered for keyword in PLAN_KEYWORDS)


def _parse_decision(content: Any) -> bool | None:
    if not isinstance(content, str):
        return None
    match = re.fullmatch(r"\s*(YES|NO)[.!]?\s*", content, re.IGNORECASE)
    if match is None:
        return None
    return match.group(1).upper() == "YES"


__all__ = [
    "PLAN_KEYWORDS",
    "TASK_INTENT_SYSTEM_PROMPT",
    "TaskIntentClassifier",
    "rule_based_requires_plan",
]
