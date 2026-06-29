from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence, TypedDict

from llm_agent.tool_registry import ToolSpec


try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - python-dotenv is a project dependency.
    load_dotenv = None


Provider = str
ToolChoice = str | Mapping[str, Any] | None

class ChatMessage(TypedDict):
    role: Literal["system", "user", "assistant"]
    content: str

class LLMClientError(RuntimeError):
    """Raised when a model SDK call cannot be made or parsed."""


class LLMContextLengthError(LLMClientError):
    """Raised when a provider rejects a request because its context is too long."""


@dataclass(frozen=True)
class LLMToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LLMResponse:
    content: str
    tool_calls: list[LLMToolCall]
    raw: dict[str, Any]
    stop_reason: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return self.content

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


@dataclass
class LLMClient:
    provider: Provider = "openai"
    model: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    max_tokens: int = 1024
    temperature: float | None = None
    timeout: float = 60.0
    extra_headers: Mapping[str, str] = field(default_factory=dict)
    extra_body: Mapping[str, Any] = field(default_factory=dict)
    env_file: str | Path | None = None
    openai_client: Any | None = field(default=None, repr=False)
    anthropic_client: Any | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        _load_dotenv(self.env_file)

        self.provider = _normalize_provider(self.provider)
        env_prefix = "OPENAI" if self.provider == "openai" else "ANTHROPIC"

        if self.model is None:
            self.model = _first_env("LLM_MODEL", f"{env_prefix}_MODEL", "MODEL_ID")
        if self.api_key is None:
            self.api_key = os.getenv(f"{env_prefix}_API_KEY")
        if self.base_url is None:
            self.base_url = os.getenv("LLM_BASE_URL") or os.getenv(
                f"{env_prefix}_BASE_URL"
            )

    def complete(
        self,
        messages: Sequence[ChatMessage | Mapping[str, Any]],
        *,
        tools: Sequence[ToolSpec] | None = None,
        tool_choice: ToolChoice = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        extra_body: Mapping[str, Any] | None = None,
    ) -> str:
        response = self.chat(
            messages,
            tools=tools,
            tool_choice=tool_choice,
            max_tokens=max_tokens,
            temperature=temperature,
            extra_body=extra_body,
        )
        return response.content

    def chat(
        self,
        messages: Sequence[ChatMessage | Mapping[str, Any]],
        *,
        tools: Sequence[ToolSpec] | None = None,
        tool_choice: ToolChoice = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        extra_body: Mapping[str, Any] | None = None,
    ) -> LLMResponse:
        if self.provider == "openai":
            return self._chat_openai(
                messages,
                tools=tools,
                tool_choice=tool_choice,
                max_tokens=max_tokens,
                temperature=temperature,
                extra_body=extra_body,
            )

        return self._chat_anthropic(
            messages,
            tools=tools,
            tool_choice=tool_choice,
            max_tokens=max_tokens,
            temperature=temperature,
            extra_body=extra_body,
        )

    def assistant_message(self, response: LLMResponse) -> dict[str, Any]:
        if self.provider == "openai":
            return dict(response.raw["choices"][0]["message"])

        return {"role": "assistant", "content": response.raw.get("content", [])}

    def tool_result_message(
        self,
        tool_call: LLMToolCall | str,
        result: Any,
    ) -> dict[str, Any]:
        messages = self.tool_result_messages([(tool_call, result)])
        if not messages:
            raise LLMClientError("No tool result message was generated.")
        return messages[0]

    def tool_result_messages(
        self,
        tool_results: Sequence[tuple[LLMToolCall | str, Any]],
    ) -> list[dict[str, Any]]:
        if self.provider == "openai":
            return [
                {
                    "role": "tool",
                    "tool_call_id": _tool_call_id(tool_call),
                    "content": _serialize_tool_result(result),
                }
                for tool_call, result in tool_results
            ]

        content = [
            {
                "type": "tool_result",
                "tool_use_id": _tool_call_id(tool_call),
                "content": _serialize_tool_result(result),
            }
            for tool_call, result in tool_results
        ]
        if not content:
            return []
        return [{"role": "user", "content": content}]

    def _chat_openai(
        self,
        messages: Sequence[ChatMessage | Mapping[str, Any]],
        *,
        tools: Sequence[ToolSpec] | None,
        tool_choice: ToolChoice,
        max_tokens: int | None,
        temperature: float | None,
        extra_body: Mapping[str, Any] | None,
    ) -> LLMResponse:
        params: dict[str, Any] = {
            "model": self._require_model(),
            "messages": _to_openai_messages(messages),
            "max_tokens": self.max_tokens if max_tokens is None else max_tokens,
        }

        resolved_temperature = self.temperature if temperature is None else temperature
        if resolved_temperature is not None:
            params["temperature"] = resolved_temperature

        if tools:
            params["tools"] = [_to_openai_tool(tool) for tool in tools]
            resolved_tool_choice = _openai_tool_choice(tool_choice)
            if resolved_tool_choice is not None:
                params["tool_choice"] = resolved_tool_choice

        sdk_extra_body = _combined_extra_body(self.extra_body, extra_body)
        if sdk_extra_body:
            params["extra_body"] = sdk_extra_body
        if self.extra_headers:
            params["extra_headers"] = dict(self.extra_headers)

        try:
            response = self._openai_client().chat.completions.create(**params)
        except Exception as exc:
            if _is_context_length_error(exc):
                raise LLMContextLengthError(
                    f"OpenAI context length exceeded: {exc}"
                ) from exc
            raise LLMClientError(f"OpenAI SDK request failed: {exc}") from exc

        return _parse_openai_response(_response_to_dict(response))

    def _chat_anthropic(
        self,
        messages: Sequence[ChatMessage | Mapping[str, Any]],
        *,
        tools: Sequence[ToolSpec] | None,
        tool_choice: ToolChoice,
        max_tokens: int | None,
        temperature: float | None,
        extra_body: Mapping[str, Any] | None,
    ) -> LLMResponse:
        anthropic_messages, system = _to_anthropic_messages(messages)
        params: dict[str, Any] = {
            "model": self._require_model(),
            "max_tokens": self.max_tokens if max_tokens is None else max_tokens,
            "messages": anthropic_messages,
        }
        if system:
            params["system"] = system

        resolved_temperature = self.temperature if temperature is None else temperature
        if resolved_temperature is not None:
            params["temperature"] = resolved_temperature

        if tools:
            params["tools"] = [_to_anthropic_tool(tool) for tool in tools]
            resolved_tool_choice = _anthropic_tool_choice(tool_choice)
            if resolved_tool_choice is not None:
                params["tool_choice"] = resolved_tool_choice

        sdk_extra_body = _combined_extra_body(self.extra_body, extra_body)
        if sdk_extra_body:
            params["extra_body"] = sdk_extra_body
        if self.extra_headers:
            params["extra_headers"] = dict(self.extra_headers)

        try:
            response = self._anthropic_client().messages.create(**params)
        except Exception as exc:
            if _is_context_length_error(exc):
                raise LLMContextLengthError(
                    f"Anthropic context length exceeded: {exc}"
                ) from exc
            raise LLMClientError(f"Anthropic SDK request failed: {exc}") from exc

        return _parse_anthropic_response(_response_to_dict(response))

    def _openai_client(self) -> Any:
        if self.openai_client is not None:
            return self.openai_client

        try:
            from openai import OpenAI
        except ImportError as exc:
            raise LLMClientError("Install the openai package to use provider='openai'.") from exc

        kwargs: dict[str, Any] = {
            "api_key": self.api_key or _openai_local_api_key(self.base_url),
            "timeout": self.timeout,
        }
        if self.base_url:
            kwargs["base_url"] = self.base_url
        if self.extra_headers:
            kwargs["default_headers"] = dict(self.extra_headers)

        self.openai_client = OpenAI(**kwargs)
        return self.openai_client

    def _anthropic_client(self) -> Any:
        if self.anthropic_client is not None:
            return self.anthropic_client

        try:
            from anthropic import Anthropic
        except ImportError as exc:
            raise LLMClientError(
                "Install the anthropic package to use provider='anthropic'."
            ) from exc

        kwargs: dict[str, Any] = {"api_key": self.api_key, "timeout": self.timeout}
        if self.base_url:
            kwargs["base_url"] = self.base_url
        if self.extra_headers:
            kwargs["default_headers"] = dict(self.extra_headers)

        self.anthropic_client = Anthropic(**kwargs)
        return self.anthropic_client

    def _require_model(self) -> str:
        if not self.model:
            raise LLMClientError(
                "LLM model is required. Pass model=... or set LLM_MODEL, "
                "OPENAI_MODEL, ANTHROPIC_MODEL, or MODEL_ID."
            )
        return self.model


def _normalize_provider(provider: str) -> str:
    normalized = provider.strip().lower().replace("_", "-")
    if normalized in {"openai", "openai-compatible", "openai-compatible-api"}:
        return "openai"
    if normalized in {"anthropic", "anthropic-compatible", "claude"}:
        return "anthropic"
    raise ValueError(f"Unsupported LLM provider: {provider}")


def _is_context_length_error(exc: Exception) -> bool:
    markers = (
        "context_length_exceeded",
        "context length exceeded",
        "maximum context length",
        "prompt_too_long",
        "prompt is too long",
        "too many tokens",
        "request too large",
    )
    current: BaseException | None = exc
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        text = str(current).lower()
        if any(marker in text for marker in markers):
            return True

        status_code = getattr(current, "status_code", None)
        code = getattr(current, "code", None)
        if status_code == 413 or str(code).lower() in {
            "context_length_exceeded",
            "prompt_too_long",
        }:
            return True
        current = current.__cause__ or current.__context__
    return False


def _openai_local_api_key(base_url: str | None) -> str | None:
    if base_url and "api.openai.com" not in base_url:
        return "EMPTY"
    return None


def _load_dotenv(env_file: str | Path | None) -> None:
    if load_dotenv is None:
        return

    if env_file is not None:
        load_dotenv(Path(env_file), override=False)
        return

    package_dir = Path(__file__).resolve().parent
    src_dir = package_dir.parent
    project_dir = src_dir.parent
    for candidate in (
        src_dir / ".env",
        package_dir / ".env",
        project_dir / ".env",
    ):
        if candidate.exists():
            load_dotenv(candidate, override=False)


def _first_env(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return None


def _to_openai_messages(
    messages: Sequence[ChatMessage | Mapping[str, Any]],
) -> list[dict[str, Any]]:
    return [dict(message) for message in messages]


def _to_anthropic_messages(
    messages: Sequence[ChatMessage | Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], str | None]:
    system_parts: list[str] = []
    anthropic_messages: list[dict[str, Any]] = []

    for message in messages:
        role = message.get("role")
        content = message.get("content", "")

        if role == "system":
            system_parts.append(_content_to_text(content))
            continue

        if role == "tool":
            tool_call_id = message.get("tool_call_id") or message.get("id")
            if not tool_call_id:
                raise LLMClientError("Tool result messages require tool_call_id.")
            anthropic_messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_call_id,
                            "content": _content_to_text(content),
                        }
                    ],
                }
            )
            continue

        if role not in {"user", "assistant"}:
            raise LLMClientError(f"Unsupported Anthropic message role: {role}")

        anthropic_messages.append({"role": role, "content": content})

    system = "\n\n".join(part for part in system_parts if part).strip() or None
    return anthropic_messages, system


def _to_openai_tool(tool: ToolSpec) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": tool.get("parameters") or _empty_parameters(),
        },
    }


def _to_anthropic_tool(tool: ToolSpec) -> dict[str, Any]:
    return {
        "name": tool["name"],
        "description": tool.get("description", ""),
        "input_schema": tool.get("parameters") or _empty_parameters(),
    }


def _openai_tool_choice(tool_choice: ToolChoice) -> Any:
    if tool_choice is None:
        return None
    if isinstance(tool_choice, Mapping):
        return dict(tool_choice)
    if tool_choice in {"auto", "none", "required"}:
        return tool_choice
    return {"type": "function", "function": {"name": tool_choice}}


def _anthropic_tool_choice(tool_choice: ToolChoice) -> Any:
    if tool_choice is None:
        return None
    if isinstance(tool_choice, Mapping):
        return dict(tool_choice)
    if tool_choice == "required":
        return {"type": "any"}
    if tool_choice in {"auto", "any", "none"}:
        return {"type": tool_choice}
    return {"type": "tool", "name": tool_choice}


def _parse_openai_response(data: dict[str, Any]) -> LLMResponse:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LLMClientError("OpenAI SDK response did not include choices.")

    choice = choices[0]
    message = choice.get("message") or {}
    tool_calls = [_parse_openai_tool_call(call) for call in message.get("tool_calls") or []]

    function_call = message.get("function_call")
    if function_call:
        tool_calls.append(_parse_openai_function_call(function_call))

    return LLMResponse(
        content=_content_to_text(message.get("content", "")),
        tool_calls=tool_calls,
        raw=data,
        stop_reason=choice.get("finish_reason"),
        usage=dict(data.get("usage") or {}),
    )


def _parse_openai_tool_call(call: Mapping[str, Any]) -> LLMToolCall:
    function = call.get("function") or {}
    name = function.get("name")
    if not name:
        raise LLMClientError("OpenAI tool call missing function.name.")

    return LLMToolCall(
        id=str(call.get("id") or name),
        name=name,
        arguments=_parse_arguments(function.get("arguments"), name),
        raw=dict(call),
    )


def _parse_openai_function_call(function_call: Mapping[str, Any]) -> LLMToolCall:
    name = function_call.get("name")
    if not name:
        raise LLMClientError("OpenAI function_call missing name.")

    return LLMToolCall(
        id=str(function_call.get("id") or name),
        name=name,
        arguments=_parse_arguments(function_call.get("arguments"), name),
        raw=dict(function_call),
    )


def _parse_anthropic_response(data: dict[str, Any]) -> LLMResponse:
    blocks = data.get("content")
    if not isinstance(blocks, list):
        raise LLMClientError("Anthropic SDK response did not include content.")

    text_parts: list[str] = []
    tool_calls: list[LLMToolCall] = []

    for block in blocks:
        if not isinstance(block, Mapping):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text_parts.append(str(block.get("text", "")))
        elif block_type == "tool_use":
            tool_calls.append(_parse_anthropic_tool_call(block))

    return LLMResponse(
        content="\n".join(part for part in text_parts if part),
        tool_calls=tool_calls,
        raw=data,
        stop_reason=data.get("stop_reason"),
        usage=dict(data.get("usage") or {}),
    )


def _parse_anthropic_tool_call(block: Mapping[str, Any]) -> LLMToolCall:
    name = block.get("name")
    tool_call_id = block.get("id")
    if not name or not tool_call_id:
        raise LLMClientError("Anthropic tool_use missing id or name.")

    arguments = block.get("input") or {}
    if not isinstance(arguments, dict):
        raise LLMClientError(f"Tool arguments for {name} must be a JSON object.")

    return LLMToolCall(
        id=str(tool_call_id),
        name=str(name),
        arguments=arguments,
        raw=dict(block),
    )


def _parse_arguments(value: Any, tool_name: str) -> dict[str, Any]:
    if value is None or value == "":
        return {}
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        raise LLMClientError(f"Tool arguments for {tool_name} must be JSON.")

    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise LLMClientError(
            f"Tool arguments for {tool_name} were not valid JSON: {value}"
        ) from exc

    if not isinstance(parsed, dict):
        raise LLMClientError(f"Tool arguments for {tool_name} must be a JSON object.")
    return parsed


def _response_to_dict(response: Any) -> dict[str, Any]:
    if isinstance(response, dict):
        return response
    if hasattr(response, "model_dump"):
        data = response.model_dump()
    elif hasattr(response, "to_dict"):
        data = response.to_dict()
    else:
        raise LLMClientError(f"Unsupported SDK response type: {type(response).__name__}")

    if not isinstance(data, dict):
        raise LLMClientError("SDK response did not serialize to a JSON object.")
    return data


def _content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, Mapping) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(part for part in parts if part)
    return json.dumps(content, ensure_ascii=False)


def _tool_call_id(tool_call: LLMToolCall | str) -> str:
    return tool_call.id if isinstance(tool_call, LLMToolCall) else tool_call


def _serialize_tool_result(result: Any) -> str:
    if isinstance(result, str):
        return result
    return json.dumps(result, ensure_ascii=False)


def _empty_parameters() -> dict[str, Any]:
    return {"type": "object", "properties": {}}


def _combined_extra_body(
    default_extra_body: Mapping[str, Any],
    call_extra_body: Mapping[str, Any] | None,
) -> dict[str, Any]:
    combined = dict(default_extra_body)
    if call_extra_body:
        combined.update(call_extra_body)
    return combined
