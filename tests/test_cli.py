from typing import Any

import pytest

from llm_agent.cli import read_user_prompt


class FakePromptSession:
    def __init__(self, results: list[str | BaseException]) -> None:
        self.results = iter(results)
        self.messages: list[Any] = []

    def prompt(self, message: Any = None, **kwargs: Any) -> str:
        self.messages.append(message)
        result = next(self.results)
        if isinstance(result, BaseException):
            raise result
        return result


def test_read_user_prompt_preserves_multiline_content() -> None:
    session = FakePromptSession(["first line\nsecond line"])

    assert read_user_prompt(session) == "first line\nsecond line"


@pytest.mark.parametrize("command", ["q", "EXIT", "/exit", " /QUIT "])
def test_read_user_prompt_recognizes_exit_commands(command: str) -> None:
    session = FakePromptSession([command])

    assert read_user_prompt(session) is None


def test_read_user_prompt_retries_after_interrupt_and_blank_input() -> None:
    session = FakePromptSession([KeyboardInterrupt(), "  \n ", "next prompt"])
    output: list[str] = []

    assert read_user_prompt(session, output_func=output.append) == "next prompt"
    assert output == [""]
    assert len(session.messages) == 3


def test_read_user_prompt_exits_on_eof() -> None:
    session = FakePromptSession([EOFError()])

    assert read_user_prompt(session) is None
