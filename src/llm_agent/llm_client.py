from __future__ import annotations

import json
import os
import random
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter, sleep
from typing import Any, Callable, Literal, Mapping, Sequence, TypedDict
from uuid import uuid4

from llm_agent.recovery import (
    ClassifiedError,
    RecoveryNotice,
    RecoveryPolicy,
    RecoveryState,
    classify_llm_error,
    current_recovery_state,
    emit_recovery_notice,
    retry_delay,
)
from llm_agent.tool_registry import ToolSpec
from llm_agent.trace_system import (
    current_trace_context,
    current_trace_recorder,
    elapsed_ms,
    record_trace,
    summarize_text,
    summarize_value,
    trace_llm_content_enabled,
)


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

    def __init__(
        self,
        message: str,
        *,
        kind: str = "unknown",
        retryable: bool = False,
        status_code: int | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds


class LLMContextLengthError(LLMClientError):
    """Raised when a provider rejects a request because its context is too long."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
    ) -> None:
        super().__init__(
            message,
            kind="context_length",
            retryable=False,
            status_code=status_code,
        )


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

    @property
    def is_output_truncated(self) -> bool:
        return str(self.stop_reason or "").lower() in {
            "length",
            "max_output_tokens",
            "max_tokens",
        }


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
    recovery_policy: RecoveryPolicy = field(default_factory=RecoveryPolicy)
    retry_sleep: Callable[[float], None] = field(default=sleep, repr=False)
    retry_random: Callable[[], float] = field(default=random.random, repr=False)
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
        call_id = f"llm-{uuid4().hex[:16]}"
        started_at = perf_counter()
        tracing = current_trace_recorder() is not None
        trace_context = current_trace_context()
        operation = trace_context.operation if trace_context else "llm.chat"
        state = current_recovery_state() or RecoveryState(
            primary_model=self.model,
        )
        active_model = state.current_model or self.model
        attempts = 0
        if tracing:
            record_trace(
                category="llm",
                name="llm.call",
                phase="started",
                correlation_id=call_id,
                data=self._trace_request_data(
                    messages,
                    tools=tools,
                    tool_choice=tool_choice,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    model=active_model,
                ),
            )

        while True:
            attempts += 1
            try:
                response = self._chat_once(
                    messages,
                    tools=tools,
                    tool_choice=tool_choice,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    extra_body=extra_body,
                    model=active_model,
                )
            except LLMClientError as exc:
                error = exc
                classified = ClassifiedError(
                    kind=exc.kind,
                    retryable=exc.retryable,
                    status_code=exc.status_code,
                    retry_after_seconds=exc.retry_after_seconds,
                )
                cause: BaseException = exc
            except Exception as exc:
                classified = classify_llm_error(exc)
                error = _to_llm_client_error(
                    self.provider,
                    exc,
                    classified,
                )
                cause = exc
            else:
                state.consecutive_overloads = 0
                break

            can_retry = (
                classified.retryable
                and attempts <= self.recovery_policy.max_retries
            )
            if can_retry:
                delay = retry_delay(
                    self.recovery_policy,
                    attempts - 1,
                    retry_after_seconds=classified.retry_after_seconds,
                    random_value=self.retry_random(),
                )
                elapsed_seconds = elapsed_ms(started_at) / 1_000
                can_retry = (
                    elapsed_seconds + delay
                    <= self.recovery_policy.max_retry_elapsed_seconds
                )

            if can_retry:
                if classified.kind == "overloaded":
                    state.consecutive_overloads += 1
                    fallback_model = self.recovery_policy.fallback_model
                    if (
                        fallback_model
                        and state.consecutive_overloads
                        >= self.recovery_policy.fallback_after_overloads
                        and active_model != fallback_model
                    ):
                        previous_model = active_model
                        active_model = fallback_model
                        state.current_model = fallback_model
                        state.fallback_used = True
                        state.consecutive_overloads = 0
                        emit_recovery_notice(
                            RecoveryNotice(
                                action="fallback",
                                reason="overloaded",
                                attempt=attempts + 1,
                                max_attempts=self.recovery_policy.max_retries
                                + 1,
                                from_model=previous_model,
                                to_model=fallback_model,
                                status_code=classified.status_code,
                                call_id=call_id,
                            )
                        )
                else:
                    state.consecutive_overloads = 0

                state.transport_retries += 1
                emit_recovery_notice(
                    RecoveryNotice(
                        action="retry",
                        reason=classified.kind,
                        attempt=attempts + 1,
                        max_attempts=self.recovery_policy.max_retries + 1,
                        delay_seconds=round(delay, 3),
                        model=active_model,
                        status_code=classified.status_code,
                        call_id=call_id,
                        detail=str(cause),
                    )
                )
                self.retry_sleep(delay)
                continue

            if classified.retryable:
                emit_recovery_notice(
                    RecoveryNotice(
                        action="exhausted",
                        reason=classified.kind,
                        attempt=attempts,
                        max_attempts=self.recovery_policy.max_retries + 1,
                        model=active_model,
                        status_code=classified.status_code,
                        call_id=call_id,
                        detail=str(cause),
                    )
                )
            if tracing:
                record_trace(
                    category="llm",
                    name="llm.call",
                    phase="failed",
                    status="error",
                    correlation_id=call_id,
                    duration_ms=elapsed_ms(started_at),
                    data={
                        "provider": self.provider,
                        "model": active_model,
                        "operation": operation,
                        "attempts": attempts,
                        "error_kind": classified.kind,
                        "status_code": classified.status_code,
                        "error": error,
                    },
                )
            if error is cause:
                raise error
            raise error from cause

        if tracing:
            response_data: dict[str, Any] = {
                "provider": self.provider,
                "model": active_model,
                "operation": operation,
                "attempts": attempts,
                "stop_reason": response.stop_reason,
                "usage": response.usage,
                "content": summarize_text(response.content),
                "tool_calls": [
                    {
                        "id": tool_call.id,
                        "name": tool_call.name,
                        "arguments": tool_call.arguments,
                    }
                    for tool_call in response.tool_calls
                ],
            }
            if trace_llm_content_enabled():
                response_data["content_text"] = response.content
            record_trace(
                category="llm",
                name="llm.call",
                phase="completed",
                correlation_id=call_id,
                duration_ms=elapsed_ms(started_at),
                data=response_data,
            )
        return response

    def _trace_request_data(
        self,
        messages: Sequence[ChatMessage | Mapping[str, Any]],
        *,
        tools: Sequence[ToolSpec] | None,
        tool_choice: ToolChoice,
        max_tokens: int | None,
        temperature: float | None,
        model: str | None,
    ) -> dict[str, Any]:
        context = current_trace_context()
        data: dict[str, Any] = {
            "provider": self.provider,
            "model": model,
            "operation": context.operation if context else "llm.chat",
            "operation_data": context.operation_data if context else {},
            "message_count": len(messages),
            "message_roles": [
                str(message.get("role", "")) for message in messages
            ],
            "messages": summarize_value(messages),
            "tool_names": [tool["name"] for tool in tools or []],
            "tool_choice": tool_choice,
            "max_tokens": self.max_tokens if max_tokens is None else max_tokens,
            "temperature": (
                self.temperature if temperature is None else temperature
            ),
        }
        if trace_llm_content_enabled():
            data["message_content"] = [dict(message) for message in messages]
            data["tool_specs"] = list(tools or [])
        return data

    def _chat_once(
        self,
        messages: Sequence[ChatMessage | Mapping[str, Any]],
        *,
        tools: Sequence[ToolSpec] | None,
        tool_choice: ToolChoice,
        max_tokens: int | None,
        temperature: float | None,
        extra_body: Mapping[str, Any] | None,
        model: str | None,
    ) -> LLMResponse:
        if self.provider == "openai":
            return self._chat_openai(
                messages,
                tools=tools,
                tool_choice=tool_choice,
                max_tokens=max_tokens,
                temperature=temperature,
                extra_body=extra_body,
                model=model,
            )
        return self._chat_anthropic(
            messages,
            tools=tools,
            tool_choice=tool_choice,
            max_tokens=max_tokens,
            temperature=temperature,
            extra_body=extra_body,
            model=model,
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
        model: str | None,
    ) -> LLMResponse:
        params: dict[str, Any] = {
            "model": model or self._require_model(),
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

        client = _without_sdk_retries(self._openai_client())
        response = client.chat.completions.create(**params)
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
        model: str | None,
    ) -> LLMResponse:
        anthropic_messages, system = _to_anthropic_messages(messages)
        params: dict[str, Any] = {
            "model": model or self._require_model(),
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

        client = _without_sdk_retries(self._anthropic_client())
        response = client.messages.create(**params)
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
            "max_retries": 0,
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

        kwargs: dict[str, Any] = {
            "api_key": self.api_key,
            "timeout": self.timeout,
            "max_retries": 0,
        }
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


def _to_llm_client_error(
    provider: str,
    exc: BaseException,
    classified: ClassifiedError,
) -> LLMClientError:
    label = "OpenAI" if provider == "openai" else "Anthropic"
    if classified.kind == "context_length":
        return LLMContextLengthError(
            f"{label} context length exceeded: {exc}",
            status_code=classified.status_code,
        )
    return LLMClientError(
        f"{label} SDK request failed: {exc}",
        kind=classified.kind,
        retryable=classified.retryable,
        status_code=classified.status_code,
        retry_after_seconds=classified.retry_after_seconds,
    )


def _without_sdk_retries(client: Any) -> Any:
    with_options = getattr(client, "with_options", None)
    if not callable(with_options):
        return client
    try:
        return with_options(max_retries=0)
    except TypeError:
        return client


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
