from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import FileHistory, InMemoryHistory


USER_PROMPT = FormattedText([("ansicyan", "Input your question >> ")])
EXIT_COMMANDS = frozenset({"q", "exit", "/exit", "/quit"})


class PromptReader(Protocol):
    def prompt(self, message: Any = None, **kwargs: Any) -> str:
        """Read one submitted prompt."""


def build_user_prompt_session(workdir: Path | str) -> PromptSession[str]:
    state_dir = Path(workdir) / ".llm_agent"
    state_dir.mkdir(parents=True, exist_ok=True)
    return PromptSession(
        history=FileHistory(str(state_dir / "input_history")),
        auto_suggest=AutoSuggestFromHistory(),
        multiline=True,
        enable_history_search=True,
        prompt_continuation=FormattedText([("ansibrightblack", "... ")]),
    )


def build_approval_prompt_session() -> PromptSession[str]:
    return PromptSession(
        history=InMemoryHistory(),
        multiline=False,
    )


def read_user_prompt(
    session: PromptReader,
    *,
    output_func: Callable[[str], None] = print,
) -> str | None:
    while True:
        try:
            query = session.prompt(USER_PROMPT)
        except KeyboardInterrupt:
            output_func("")
            continue
        except EOFError:
            return None

        if not query.strip():
            continue
        if query.strip().lower() in EXIT_COMMANDS:
            return None
        return query


__all__ = [
    "EXIT_COMMANDS",
    "PromptReader",
    "USER_PROMPT",
    "build_approval_prompt_session",
    "build_user_prompt_session",
    "read_user_prompt",
]
