from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from math import ceil
from pathlib import Path
import re
from typing import Any
from uuid import uuid4

from llm_agent.llm_client import LLMClient
from llm_agent.tool_registry import ToolSpec


IDENTITY_INSTRUCTIONS = """
You are a coding agent working in the user's workspace.

Use the provided tools to inspect, modify, and verify the work. Prefer acting on
observed project state over guessing. Keep changes focused on the user's request.
""".strip()

CONTEXT_INSTRUCTIONS = """
Context:
- Treat any structured sections below as authoritative working context.
- Use retrieved context, memory, and workspace notes when relevant, but do not
  invent details that are not present.
- If context conflicts with fresh tool results, prefer the fresh tool results.
""".strip()

TOOL_INSTRUCTIONS = """
Tool use:
- Use tools when you need to inspect files, search, run commands, edit files,
  compute, or verify behavior.
- Do not describe a tool call as text when a real tool call is needed.
- Do not fabricate tool results.
- Keep file operations inside the workspace.
- If a tool fails, use the error to choose the next reasonable step.
""".strip()

FINAL_ANSWER_INSTRUCTIONS = """
Final answer:
- Answer in natural language.
- Be concise, clear, and accurate.
- Summarize what changed or what was found.
- Mention verification results when available.
- If something could not be completed, say why and what remains.
""".strip()

DEFAULT_BASE_INSTRUCTIONS = "\n\n".join(
    [
        IDENTITY_INSTRUCTIONS,
        CONTEXT_INSTRUCTIONS,
        TOOL_INSTRUCTIONS,
        FINAL_ANSWER_INSTRUCTIONS,
    ]
)

SUMMARY_SYSTEM_PROMPT = """
Summarize an earlier portion of a coding-agent conversation so another model call
can continue the work without the original messages.

Return text only and do not call tools. Preserve:
- the current goal and latest user request;
- explicit user constraints and preferences;
- important findings and technical decisions;
- files inspected, created, or modified;
- commands, tests, and concrete verification evidence;
- current errors, blockers, and failed approaches;
- task progress and remaining work;
- names of loaded skills that may need to be loaded again.

Be compact but concrete. Do not invent facts.
""".strip()

PERSISTED_RESULT_PREFIX = "<persisted_tool_result>"
COMPACTED_TOOL_RESULT = (
    "[Earlier tool result compacted. Re-run the tool if its full output is needed.]"
)

Message = dict[str, Any]


@dataclass(frozen=True)
class PromptSection:
    name: str
    content: str
    priority: int = 100
    token_budget: int | None = None


@dataclass(frozen=True)
class ContextUpdate:
    messages: list[Message]
    reason: str
    before_tokens: int
    after_tokens: int
    transcript_path: Path | None = None
    persisted_results: int = 0
    compacted_results: int = 0
    summary_created: bool = False
    hard_trimmed: bool = False
    error: str | None = None

    @property
    def context_rebuilt(self) -> bool:
        return self.summary_created or self.hard_trimmed


@dataclass
class ContextManager:
    base_instructions: str = DEFAULT_BASE_INSTRUCTIONS
    sections: list[PromptSection] = field(default_factory=list)
    llm: LLMClient | None = None
    workdir: Path | str | None = None
    max_context_tokens: int = 100_000
    auto_compact_ratio: float = 0.75
    keep_recent_groups: int = 6
    keep_recent_tool_results: int = 3
    max_tool_result_chars: int = 50_000
    tool_result_preview_chars: int = 2_000
    summary_max_tokens: int = 2_000
    max_compact_failures: int = 3
    max_reactive_retries: int = 1
    _consecutive_compact_failures: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if self.workdir is not None:
            self.workdir = Path(self.workdir).resolve()
        if self.max_context_tokens <= 0:
            raise ValueError("max_context_tokens must be greater than zero.")
        if not 0 < self.auto_compact_ratio < 1:
            raise ValueError("auto_compact_ratio must be between zero and one.")
        if self.keep_recent_groups < 0:
            raise ValueError("keep_recent_groups cannot be negative.")
        if self.keep_recent_tool_results < 0:
            raise ValueError("keep_recent_tool_results cannot be negative.")
        if self.max_tool_result_chars <= 0:
            raise ValueError("max_tool_result_chars must be greater than zero.")
        if self.summary_max_tokens <= 0:
            raise ValueError("summary_max_tokens must be greater than zero.")
        if self.max_compact_failures <= 0:
            raise ValueError("max_compact_failures must be greater than zero.")
        if self.max_reactive_retries < 0:
            raise ValueError("max_reactive_retries cannot be negative.")

    @property
    def resolved_workdir(self) -> Path:
        if self.workdir is None:
            return Path.cwd().resolve()
        return Path(self.workdir).resolve()

    @property
    def compact_threshold_tokens(self) -> int:
        return int(self.max_context_tokens * self.auto_compact_ratio)

    def bind(
        self,
        *,
        llm: LLMClient,
        workdir: Path | str,
    ) -> None:
        if self.llm is None:
            self.llm = llm
        if self.workdir is None:
            self.workdir = Path(workdir).resolve()

    def add_section(
        self,
        name: str,
        content: str,
        *,
        priority: int = 100,
        token_budget: int | None = None,
    ) -> None:
        self.sections.append(
            PromptSection(
                name=name,
                content=content,
                priority=priority,
                token_budget=token_budget,
            )
        )

    def build_system_prompt(self) -> str:
        parts = [self.base_instructions.strip()]
        for section in sorted(self.sections, key=lambda item: (item.priority, item.name)):
            content = section.content.strip()
            if not content:
                continue
            parts.append(f"<{section.name}>\n{content}\n</{section.name}>")
        return "\n\n".join(parts).strip()

    def new_messages(self) -> list[Message]:
        return [{"role": "system", "content": self.build_system_prompt()}]

    def estimate_tokens(self, messages: list[Message]) -> int:
        serialized = json.dumps(messages, ensure_ascii=False, default=str)
        return max(1, ceil(len(serialized.encode("utf-8")) / 3))

    def build_request_messages(
        self,
        messages: list[Message],
        *,
        runtime_messages: list[str] | None = None,
    ) -> list[Message]:
        request_messages = deepcopy(messages)
        runtime_context = [
            {"role": "user", "content": content}
            for content in runtime_messages or []
            if content.strip()
        ]
        if not runtime_context:
            return request_messages

        insert_at = _latest_external_user_index(request_messages)
        if insert_at is None:
            request_messages.extend(runtime_context)
        else:
            request_messages[insert_at:insert_at] = runtime_context
        return request_messages

    def estimate_request_tokens(
        self,
        messages: list[Message],
        *,
        runtime_messages: list[str] | None = None,
        tools: list[ToolSpec] | None = None,
    ) -> int:
        payload = {
            "messages": self.build_request_messages(
                messages,
                runtime_messages=runtime_messages,
            ),
            "tools": tools or [],
        }
        serialized = json.dumps(payload, ensure_ascii=False, default=str)
        return max(1, ceil(len(serialized.encode("utf-8")) / 3))

    def prepare(
        self,
        messages: list[Message],
        *,
        run_id: str,
        reserved_tokens: int = 0,
    ) -> ContextUpdate | None:
        if reserved_tokens < 0:
            raise ValueError("reserved_tokens cannot be negative.")
        before_tokens = self.estimate_tokens(messages)
        working = deepcopy(messages)
        persisted = self._persist_large_tool_results(working, run_id=run_id)
        compacted = self._micro_compact_tool_results(working)
        changed = persisted > 0 or compacted > 0
        transcript_path = (
            self._write_transcript(messages, run_id=run_id, reason="preflight")
            if changed
            else None
        )

        after_tokens = self.estimate_tokens(working)
        if (
            after_tokens + reserved_tokens > self.compact_threshold_tokens
            and self._consecutive_compact_failures < self.max_compact_failures
        ):
            if transcript_path is None:
                transcript_path = self._write_transcript(
                    messages,
                    run_id=run_id,
                    reason="auto",
                )
            try:
                working = self._summarize_history(
                    working,
                    transcript_path=transcript_path,
                    keep_recent_groups=self.keep_recent_groups,
                    reason="auto",
                )
            except Exception as exc:
                self._consecutive_compact_failures += 1
                if not changed:
                    return ContextUpdate(
                        messages=list(messages),
                        reason="auto",
                        before_tokens=before_tokens,
                        after_tokens=before_tokens,
                        transcript_path=transcript_path,
                        error=str(exc),
                    )
                return ContextUpdate(
                    messages=working,
                    reason="preflight",
                    before_tokens=before_tokens,
                    after_tokens=after_tokens,
                    transcript_path=transcript_path,
                    persisted_results=persisted,
                    compacted_results=compacted,
                    error=str(exc),
                )
            self._consecutive_compact_failures = 0
            return ContextUpdate(
                messages=working,
                reason="auto",
                before_tokens=before_tokens,
                after_tokens=self.estimate_tokens(working),
                transcript_path=transcript_path,
                persisted_results=persisted,
                compacted_results=compacted,
                summary_created=True,
            )

        if not changed:
            return None
        return ContextUpdate(
            messages=working,
            reason="preflight",
            before_tokens=before_tokens,
            after_tokens=after_tokens,
            transcript_path=transcript_path,
            persisted_results=persisted,
            compacted_results=compacted,
        )

    def compact(
        self,
        messages: list[Message],
        *,
        run_id: str,
        reason: str = "manual",
    ) -> ContextUpdate:
        before_tokens = self.estimate_tokens(messages)
        working = deepcopy(messages)
        persisted = self._persist_large_tool_results(working, run_id=run_id)
        compacted = self._micro_compact_tool_results(working)
        transcript_path = self._write_transcript(
            messages,
            run_id=run_id,
            reason=reason,
        )
        try:
            compacted_messages = self._summarize_history(
                working,
                transcript_path=transcript_path,
                keep_recent_groups=self.keep_recent_groups,
                reason=reason,
            )
        except Exception as exc:
            self._consecutive_compact_failures += 1
            return ContextUpdate(
                messages=working,
                reason=reason,
                before_tokens=before_tokens,
                after_tokens=self.estimate_tokens(working),
                transcript_path=transcript_path,
                persisted_results=persisted,
                compacted_results=compacted,
                error=str(exc),
            )

        self._consecutive_compact_failures = 0
        return ContextUpdate(
            messages=compacted_messages,
            reason=reason,
            before_tokens=before_tokens,
            after_tokens=self.estimate_tokens(compacted_messages),
            transcript_path=transcript_path,
            persisted_results=persisted,
            compacted_results=compacted,
            summary_created=True,
        )

    def recover(
        self,
        messages: list[Message],
        *,
        run_id: str,
    ) -> ContextUpdate:
        before_tokens = self.estimate_tokens(messages)
        working = deepcopy(messages)
        persisted = self._persist_large_tool_results(working, run_id=run_id)
        compacted = self._micro_compact_tool_results(working)
        transcript_path = self._write_transcript(
            messages,
            run_id=run_id,
            reason="reactive",
        )

        error: str | None = None
        if self.llm is not None:
            try:
                recovered = self._summarize_history(
                    working,
                    transcript_path=transcript_path,
                    keep_recent_groups=min(3, self.keep_recent_groups),
                    reason="reactive",
                )
                if self.estimate_tokens(recovered) <= self.compact_threshold_tokens:
                    self._consecutive_compact_failures = 0
                    return ContextUpdate(
                        messages=recovered,
                        reason="reactive",
                        before_tokens=before_tokens,
                        after_tokens=self.estimate_tokens(recovered),
                        transcript_path=transcript_path,
                        persisted_results=persisted,
                        compacted_results=compacted,
                        summary_created=True,
                    )
                working = recovered
            except Exception as exc:
                error = str(exc)

        recovered = self._hard_trim(working, keep_recent_groups=2)
        return ContextUpdate(
            messages=recovered,
            reason="reactive",
            before_tokens=before_tokens,
            after_tokens=self.estimate_tokens(recovered),
            transcript_path=transcript_path,
            persisted_results=persisted,
            compacted_results=compacted,
            hard_trimmed=True,
            error=error,
        )

    def _summarize_history(
        self,
        messages: list[Message],
        *,
        transcript_path: Path,
        keep_recent_groups: int,
        reason: str,
    ) -> list[Message]:
        if self.llm is None:
            raise RuntimeError("Context summary requires an LLMClient.")

        system_messages, conversation = _split_leading_system_messages(messages)
        groups = _group_messages(conversation)
        if not groups:
            raise RuntimeError("No conversation messages are available to summarize.")

        tail_count = min(keep_recent_groups, max(0, len(groups) - 1))
        if tail_count:
            old_groups = groups[:-tail_count]
            recent_groups = groups[-tail_count:]
        else:
            old_groups = groups
            recent_groups = []

        old_messages = _flatten_groups(old_groups)
        response = self.llm.chat(
            [
                {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        old_messages,
                        ensure_ascii=False,
                        default=str,
                    ),
                },
            ],
            tools=None,
            max_tokens=self.summary_max_tokens,
            temperature=0,
        )
        summary = response.content.strip()
        if not summary:
            raise RuntimeError("Context summary was empty.")

        transcript_display = _display_path(transcript_path, self.resolved_workdir)
        summary_message = {
            "role": "user",
            "content": (
                f'<conversation_summary reason="{reason}" '
                f'transcript="{transcript_display}">\n'
                f"{summary}\n"
                "</conversation_summary>"
            ),
        }
        return [
            *system_messages,
            summary_message,
            *_flatten_groups(recent_groups),
        ]

    def _hard_trim(
        self,
        messages: list[Message],
        *,
        keep_recent_groups: int,
    ) -> list[Message]:
        system_messages, conversation = _split_leading_system_messages(messages)
        groups = _group_messages(conversation)
        recent_groups = groups[-keep_recent_groups:] if keep_recent_groups else []
        marker = {
            "role": "user",
            "content": (
                "<context_compacted mode=\"hard\">\n"
                "Earlier conversation was removed after a context-length failure. "
                "Use persistent tasks, transcripts, and workspace state to recover "
                "details when needed.\n"
                "</context_compacted>"
            ),
        }
        return [*system_messages, marker, *_flatten_groups(recent_groups)]

    def _persist_large_tool_results(
        self,
        messages: list[Message],
        *,
        run_id: str,
    ) -> int:
        persisted = 0
        for index, (container, key, tool_call_id) in enumerate(
            _tool_result_slots(messages)
        ):
            content = _content_text(container.get(key))
            if (
                len(content) <= self.max_tool_result_chars
                or content.startswith(PERSISTED_RESULT_PREFIX)
            ):
                continue

            output_dir = (
                self.resolved_workdir
                / ".llm_agent"
                / "context"
                / "tool-results"
                / _safe_component(run_id)
            )
            output_dir.mkdir(parents=True, exist_ok=True)
            filename = f"{index:04d}-{_safe_component(tool_call_id)}.txt"
            path = output_dir / filename
            _atomic_write(path, content)
            display_path = _display_path(path, self.resolved_workdir)
            preview = content[: self.tool_result_preview_chars]
            container[key] = (
                f"{PERSISTED_RESULT_PREFIX}\n"
                f"<path>{display_path}</path>\n"
                f"<tool_call_id>{tool_call_id}</tool_call_id>\n"
                f"<preview>\n{preview}\n</preview>\n"
                "</persisted_tool_result>"
            )
            persisted += 1
        return persisted

    def _micro_compact_tool_results(self, messages: list[Message]) -> int:
        slots = list(_tool_result_slots(messages))
        if len(slots) <= self.keep_recent_tool_results:
            return 0

        compacted = 0
        old_slots = slots[: len(slots) - self.keep_recent_tool_results]
        for container, key, _ in old_slots:
            content = _content_text(container.get(key))
            replacement = _compacted_tool_content(content)
            if replacement == content:
                continue
            container[key] = replacement
            compacted += 1
        return compacted

    def _write_transcript(
        self,
        messages: list[Message],
        *,
        run_id: str,
        reason: str,
    ) -> Path:
        transcript_dir = self.resolved_workdir / ".llm_agent" / "transcripts"
        transcript_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        path = transcript_dir / (
            f"{timestamp}-{_safe_component(run_id)}-{_safe_component(reason)}-"
            f"{uuid4().hex[:8]}.jsonl"
        )
        lines = [
            json.dumps(message, ensure_ascii=False, default=str)
            for message in messages
        ]
        _atomic_write(path, "\n".join(lines) + "\n")
        return path


def build_tool_summary_section(tool_specs: list[ToolSpec]) -> PromptSection:
    tool_summaries = "\n".join(
        f"- {tool['name']}: {tool.get('description', '')}".strip()
        for tool in tool_specs
    )
    return PromptSection(
        name="legacy_available_tools_summary",
        content=tool_summaries or "- None",
        priority=50,
    )


def build_system_prompt(tool_specs: list[ToolSpec] | None = None) -> str:
    sections = []
    if tool_specs is not None:
        sections.append(build_tool_summary_section(tool_specs))
    return ContextManager(sections=sections).build_system_prompt()


def _split_leading_system_messages(
    messages: list[Message],
) -> tuple[list[Message], list[Message]]:
    index = 0
    while index < len(messages) and messages[index].get("role") == "system":
        index += 1
    return list(messages[:index]), list(messages[index:])


def _group_messages(messages: list[Message]) -> list[list[Message]]:
    groups: list[list[Message]] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        group = [message]
        expected_ids = _assistant_tool_call_ids(message)
        index += 1

        if expected_ids:
            result_ids: set[str] = set()
            while index < len(messages):
                current_ids = _tool_result_ids(messages[index])
                if not current_ids:
                    break
                group.append(messages[index])
                result_ids.update(current_ids)
                index += 1
                if expected_ids.issubset(result_ids):
                    break
        groups.append(group)
    return groups


def _assistant_tool_call_ids(message: Message) -> set[str]:
    if message.get("role") != "assistant":
        return set()

    ids: set[str] = set()
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        for call in tool_calls:
            if isinstance(call, dict) and call.get("id"):
                ids.add(str(call["id"]))

    content = message.get("content")
    if isinstance(content, list):
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_use"
                and block.get("id")
            ):
                ids.add(str(block["id"]))
    return ids


def _tool_result_ids(message: Message) -> set[str]:
    if message.get("role") == "tool":
        tool_call_id = message.get("tool_call_id") or message.get("id")
        return {str(tool_call_id)} if tool_call_id else set()

    ids: set[str] = set()
    content = message.get("content")
    if message.get("role") == "user" and isinstance(content, list):
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_result"
                and block.get("tool_use_id")
            ):
                ids.add(str(block["tool_use_id"]))
    return ids


def _tool_result_slots(
    messages: list[Message],
) -> list[tuple[dict[str, Any], str, str]]:
    slots: list[tuple[dict[str, Any], str, str]] = []
    for message in messages:
        if message.get("role") == "tool":
            tool_call_id = message.get("tool_call_id") or message.get("id")
            if tool_call_id:
                slots.append((message, "content", str(tool_call_id)))
            continue

        content = message.get("content")
        if message.get("role") != "user" or not isinstance(content, list):
            continue
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_result"
                and block.get("tool_use_id")
            ):
                slots.append((block, "content", str(block["tool_use_id"])))
    return slots


def _flatten_groups(groups: list[list[Message]]) -> list[Message]:
    return [message for group in groups for message in group]


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False, default=str)


def _compacted_tool_content(content: str) -> str:
    if len(content) <= 120 or content == COMPACTED_TOOL_RESULT:
        return content
    if content.startswith(PERSISTED_RESULT_PREFIX):
        path_match = re.search(r"<path>(.*?)</path>", content, re.DOTALL)
        tool_id_match = re.search(
            r"<tool_call_id>(.*?)</tool_call_id>",
            content,
            re.DOTALL,
        )
        if path_match:
            tool_id = tool_id_match.group(1).strip() if tool_id_match else "unknown"
            return (
                f"{PERSISTED_RESULT_PREFIX}\n"
                f"<path>{path_match.group(1).strip()}</path>\n"
                f"<tool_call_id>{tool_id}</tool_call_id>\n"
                "</persisted_tool_result>"
            )
    return COMPACTED_TOOL_RESULT


def _safe_component(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value)).strip("._")
    return sanitized[:96] or "unknown"


def _display_path(path: Path, workdir: Path) -> str:
    resolved = path.resolve()
    if resolved.is_relative_to(workdir):
        return resolved.relative_to(workdir).as_posix()
    return str(resolved)


def _latest_external_user_index(messages: list[Message]) -> int | None:
    internal_prefixes = (
        "<current_tasks>",
        "<task_reminder>",
        "<relevant_memories>",
        "<conversation_summary",
        "<context_compacted",
    )
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if (
            isinstance(content, str)
            and not content.lstrip().startswith(internal_prefixes)
        ):
            return index
    return None


def _atomic_write(path: Path, content: str) -> None:
    tmp_path = path.with_suffix(path.suffix + f".{uuid4().hex[:8]}.tmp")
    tmp_path.write_text(content, encoding="utf-8")
    tmp_path.replace(path)


__all__ = [
    "COMPACTED_TOOL_RESULT",
    "ContextManager",
    "ContextUpdate",
    "DEFAULT_BASE_INSTRUCTIONS",
    "PromptSection",
    "build_system_prompt",
    "build_tool_summary_section",
]
