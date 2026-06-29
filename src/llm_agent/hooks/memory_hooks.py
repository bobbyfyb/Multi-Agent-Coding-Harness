from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from llm_agent.hooks import HookContext, HookResult
from llm_agent.memory_system import MemoryManager, format_memory_context


INTERNAL_USER_MESSAGE_PREFIXES = (
    "<current_tasks>",
    "<task_reminder>",
    "<relevant_memories>",
    "<conversation_summary",
    "<context_compacted",
)


@dataclass(frozen=True)
class MemoryContextHook:
    manager: MemoryManager

    def __call__(self, context: HookContext) -> HookResult | None:
        user_text = _latest_external_user_message(context.messages)
        if not user_text:
            return None
        memories = self.manager.retrieve_relevant(user_text)
        content = format_memory_context(memories)
        if not content:
            return None
        return HookResult.allow(
            data={
                "messages": [content],
                "memory_ids": [memory.id for memory in memories],
            }
        )


@dataclass(frozen=True)
class MemoryExtractionHook:
    manager: MemoryManager

    def __call__(
        self,
        messages: list[dict[str, Any]],
        context: HookContext,
    ) -> HookResult | None:
        user_text = str(context.metadata.get("turn_user_text", "")).strip()
        assistant_text = str(context.metadata.get("final_content", "")).strip()
        run_id = str(context.metadata.get("run_id", "")).strip()
        called_tools = context.metadata.get("called_tools", set())
        if (
            isinstance(called_tools, (set, list, tuple))
            and "memory_remember" in called_tools
        ):
            return None
        if not user_text or not self.manager.should_extract(user_text):
            return None

        try:
            memories = self.manager.extract_from_turn(
                user_text=user_text,
                assistant_text=assistant_text,
                run_id=run_id,
            )
        except Exception as exc:
            return HookResult.allow(data={"memory_error": str(exc)})
        if not memories:
            return None
        return HookResult.allow(
            data={"extracted_memories": [memory.to_dict() for memory in memories]}
        )


def _latest_external_user_message(
    messages: list[dict[str, Any]],
) -> str | None:
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str) and not content.lstrip().startswith(
            INTERNAL_USER_MESSAGE_PREFIXES
        ):
            return content
    return None


__all__ = [
    "MemoryContextHook",
    "MemoryExtractionHook",
]
