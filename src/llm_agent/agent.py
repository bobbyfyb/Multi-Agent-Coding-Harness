import json
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter
from typing import Any, Callable, Literal
from uuid import uuid4

from llm_agent.background_jobs import (
    BackgroundJobManager,
    format_background_notifications,
)
from llm_agent.context_manager import ContextManager, ContextUpdate
from llm_agent.hooks import HookContext, HookManager
from llm_agent.llm_client import (
    BATCHED_TOOL_INPUTS_KEY,
    INVALID_TOOL_INPUT_KEY,
    LLMClient,
    LLMClientError,
    LLMContextLengthError,
    LLMToolCall,
)
from llm_agent.recovery import (
    CONTINUATION_PROMPT,
    RecoveryNotice,
    RecoveryPolicy,
    RecoveryState,
    current_recovery_state,
    emit_recovery_notice,
    recovery_scope,
)
from llm_agent.tool_registry import ToolRegistry
from llm_agent.trace_system import (
    TraceRecorder,
    current_trace_context,
    current_trace_recorder,
    elapsed_ms,
    record_agent_event,
    record_trace,
    summarize_text,
    trace_operation,
    trace_scope,
    update_trace_context,
)


AgentEventType = Literal[
    "step",
    "task_context",
    "task_reminder",
    "artifact_context",
    "memory_context",
    "memory_extracted",
    "memory_extract_failed",
    "progress",
    "tool_call",
    "permission_granted",
    "permission_denied",
    "tool_result",
    "final",
    "max_steps",
    "subagent_started",
    "subagent_completed",
    "subagent_failed",
    "context_compacted",
    "context_compact_failed",
    "tool_result_persisted",
    "recovery",
    "background_completed",
    "background_failed",
    "background_cancelled",
]
AgentRunStatus = Literal["completed", "incomplete", "max_steps"]


@dataclass(frozen=True)
class AgentEvent:
    type: AgentEventType
    step: int
    data: dict[str, Any]
    agent_id: str = "agent"
    run_id: str = ""
    parent_run_id: str | None = None
    depth: int = 0
    duration_ms: float | None = None


AgentCallback = Callable[[AgentEvent], None]


@dataclass(frozen=True)
class ToolExecutionContext:
    run_id: str
    agent_id: str
    parent_run_id: str | None
    depth: int
    step: int
    workdir: Path
    on_event: AgentCallback | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentRunResult:
    status: AgentRunStatus
    content: str
    steps: int
    tool_calls: int
    usage: dict[str, Any]
    run_id: str
    agent_id: str


ANSI_RESET = "\033[0m"
ANSI_BOLD = "\033[1m"
ANSI_DIM = "\033[2m"
ANSI_BLUE = "\033[34m"
ANSI_YELLOW = "\033[33m"
ANSI_GREEN = "\033[32m"
ANSI_RED = "\033[31m"
ANSI_MAGENTA = "\033[35m"


@dataclass
class Agent:
    llm: LLMClient
    tools: ToolRegistry
    context_manager: ContextManager | str
    max_steps: int | None = 5
    hooks: HookManager = field(default_factory=HookManager)
    workdir: Path | str | None = None
    agent_id: str = "agent"
    parent_run_id: str | None = None
    depth: int = 0
    recovery_policy: RecoveryPolicy | None = None
    background_jobs: BackgroundJobManager | None = None

    def __post_init__(self) -> None:
        if self.workdir is None:
            self.workdir = Path.cwd()
        else:
            self.workdir = Path(self.workdir).resolve()
        if isinstance(self.context_manager, str):
            self.context_manager = ContextManager(
                base_instructions=self.context_manager,
                llm=self.llm,
                workdir=self.workdir,
            )
        else:
            self.context_manager.bind(llm=self.llm, workdir=self.workdir)
        if self.recovery_policy is None:
            llm_policy = getattr(self.llm, "recovery_policy", None)
            self.recovery_policy = (
                llm_policy
                if isinstance(llm_policy, RecoveryPolicy)
                else RecoveryPolicy()
            )

    def new_messages(self) -> list[dict[str, Any]]:
        return self.context_manager.new_messages()

    def run(
        self,
        messages: list[dict[str, Any]],
        *,
        on_event: AgentCallback | None = None,
        run_id: str | None = None,
        trace: TraceRecorder | None = None,
    ) -> AgentRunResult:
        resolved_run_id = run_id or f"run-{uuid4().hex[:12]}"
        active_trace = trace or current_trace_recorder()
        started_at = perf_counter()
        recovery_state = RecoveryState(
            primary_model=getattr(self.llm, "model", None),
        )

        def recovery_callback(notice: RecoveryNotice) -> None:
            self._forward_recovery_notice(
                on_event,
                notice,
                run_id=resolved_run_id,
            )

        with (
            trace_scope(
                active_trace,
                run_id=resolved_run_id,
                agent_id=self.agent_id,
                parent_run_id=self.parent_run_id,
                depth=self.depth,
            ),
            recovery_scope(recovery_state, recovery_callback),
        ):
            record_trace(
                category="run",
                name="run.started",
                phase="started",
                data={
                    "message_count": len(messages),
                    "max_steps": self.max_steps,
                },
            )
            try:
                result = self._run_loop(
                    messages,
                    on_event=on_event,
                    run_id=resolved_run_id,
                )
            except Exception as exc:
                record_trace(
                    category="run",
                    name="run.failed",
                    phase="failed",
                    status="error",
                    duration_ms=elapsed_ms(started_at),
                    data={"error": exc},
                )
                raise
            else:
                record_trace(
                    category="run",
                    name="run.completed",
                    phase="completed",
                    status=("ok" if result.status == "completed" else "warning"),
                    duration_ms=elapsed_ms(started_at),
                    data={
                        "status": result.status,
                        "steps": result.steps,
                        "tool_calls": result.tool_calls,
                        "usage": result.usage,
                        "content": summarize_text(result.content),
                    },
                )
                return result
            finally:
                if trace is not None and self.depth == 0:
                    trace.render_markdown()

    def _run_loop(
        self,
        messages: list[dict[str, Any]],
        *,
        on_event: AgentCallback | None,
        run_id: str,
    ) -> AgentRunResult:
        tool_specs = self.tools.tool_specs()
        resolved_run_id = run_id
        tool_call_count = 0
        usage: dict[str, Any] = {}
        last_content = ""
        reactive_retries = 0
        pending_context_rebuilt = False
        turn_user_text = _latest_external_user_message(messages) or ""
        emitted_runtime_context: dict[str, str] = {}
        called_tools: set[str] = set()
        recovery_state = current_recovery_state() or RecoveryState()
        recovery_policy = self.recovery_policy or RecoveryPolicy()
        output_max_tokens: int | None = None

        step_index = 0
        while self.max_steps is None or step_index < self.max_steps:
            step = step_index + 1
            update_trace_context(step=step)
            self._collect_background_notifications(
                messages,
                on_event=on_event,
                step=step,
                run_id=resolved_run_id,
            )
            context_rebuilt = pending_context_rebuilt
            pending_context_rebuilt = False
            context_update = self.context_manager.prepare(
                messages,
                run_id=resolved_run_id,
            )
            if context_update is not None:
                messages[:] = context_update.messages
                context_rebuilt = (
                    context_rebuilt or context_update.context_rebuilt
                )
                self._emit_context_update(
                    on_event,
                    context_update,
                    step=step,
                    run_id=resolved_run_id,
                )

            hook_context = HookContext(
                messages=messages,
                step=step,
                workdir=self.workdir,
                metadata={
                    "run_id": resolved_run_id,
                    "agent_id": self.agent_id,
                    "parent_run_id": self.parent_run_id,
                    "depth": self.depth,
                    "context_compacted": context_rebuilt,
                    "turn_user_text": turn_user_text,
                    "called_tools": called_tools,
                },
            )
            before_llm_result = self.hooks.trigger_hooks("BeforeLLM", hook_context)
            runtime_messages = _hook_messages(before_llm_result)
            for hook_message in runtime_messages:
                event_type = _runtime_context_event_type(hook_message)
                if event_type is None:
                    continue
                context_key = _runtime_context_key(hook_message)
                if emitted_runtime_context.get(context_key) == hook_message:
                    continue
                emitted_runtime_context[context_key] = hook_message
                data: dict[str, Any] = {"content": hook_message}
                if (
                    event_type == "memory_context"
                    and before_llm_result is not None
                ):
                    data["memory_ids"] = before_llm_result.data.get(
                        "memory_ids",
                        [],
                    )
                if (
                    event_type == "artifact_context"
                    and before_llm_result is not None
                ):
                    data["artifact_ids"] = before_llm_result.data.get(
                        "artifact_ids",
                        [],
                    )
                self._emit(
                    on_event,
                    event_type,
                    step,
                    data,
                    resolved_run_id,
                )

            request_tokens = self.context_manager.estimate_request_tokens(
                messages,
                runtime_messages=runtime_messages,
                tools=tool_specs,
            )
            if request_tokens > self.context_manager.compact_threshold_tokens:
                history_tokens = self.context_manager.estimate_tokens(messages)
                context_update = self.context_manager.prepare(
                    messages,
                    run_id=resolved_run_id,
                    reserved_tokens=max(0, request_tokens - history_tokens),
                )
                if context_update is not None:
                    messages[:] = context_update.messages
                    context_rebuilt = (
                        context_rebuilt or context_update.context_rebuilt
                    )
                    hook_context.messages = messages
                    hook_context.metadata["context_compacted"] = context_rebuilt
                    self._emit_context_update(
                        on_event,
                        context_update,
                        step=step,
                        run_id=resolved_run_id,
                    )

            request_messages = self.context_manager.build_request_messages(
                messages,
                runtime_messages=runtime_messages,
            )
            self._emit(
                on_event,
                "step",
                step,
                {"message": "calling llm"},
                resolved_run_id,
            )

            try:
                with trace_operation(
                    "agent_step",
                    request_tokens=request_tokens,
                ):
                    chat_kwargs: dict[str, Any] = {
                        "tools": tool_specs,
                        "tool_choice": "auto",
                    }
                    if output_max_tokens is not None:
                        chat_kwargs["max_tokens"] = output_max_tokens
                    response = self.llm.chat(request_messages, **chat_kwargs)
            except LLMContextLengthError as exc:
                if reactive_retries >= self.context_manager.max_reactive_retries:
                    emit_recovery_notice(
                        RecoveryNotice(
                            action="exhausted",
                            reason="context_length",
                            attempt=reactive_retries + 1,
                            max_attempts=(
                                self.context_manager.max_reactive_retries + 1
                            ),
                            detail=str(exc),
                        )
                    )
                    raise RuntimeError(
                        "LLM context remained too long after reactive compaction."
                    ) from exc
                emit_recovery_notice(
                    RecoveryNotice(
                        action="context_compact",
                        reason="context_length",
                        attempt=reactive_retries + 1,
                        max_attempts=self.context_manager.max_reactive_retries,
                    )
                )
                context_update = self.context_manager.recover(
                    messages,
                    run_id=resolved_run_id,
                )
                messages[:] = context_update.messages
                self._emit_context_update(
                    on_event,
                    context_update,
                    step=step,
                    run_id=resolved_run_id,
                )
                pending_context_rebuilt = context_update.context_rebuilt
                reactive_retries += 1
                continue
            except LLMClientError as exc:
                raise RuntimeError(
                    f"LLM request failed ({exc.kind}): {exc}"
                ) from exc
            except Exception as exc:
                raise RuntimeError("LLM request failed.") from exc

            reactive_retries = 0
            _merge_usage(usage, response.usage)
            if response.is_output_truncated:
                requested_max_tokens = output_max_tokens or int(
                    getattr(self.llm, "max_tokens", 1_024)
                )
                escalated_max_tokens = max(
                    requested_max_tokens,
                    recovery_policy.escalated_max_tokens,
                )
                if not recovery_state.output_escalated:
                    recovery_state.output_escalated = True
                    if escalated_max_tokens > requested_max_tokens:
                        output_max_tokens = escalated_max_tokens
                        emit_recovery_notice(
                            RecoveryNotice(
                                action="output_escalate",
                                reason="max_tokens",
                                data={
                                    "before_max_tokens": requested_max_tokens,
                                    "after_max_tokens": escalated_max_tokens,
                                },
                            )
                        )
                        continue

                if response.tool_calls:
                    emit_recovery_notice(
                        RecoveryNotice(
                            action="exhausted",
                            reason="truncated_tool_call",
                            detail=(
                                "The truncated response contained tool calls; "
                                "none were executed."
                            ),
                        )
                    )
                    raise RuntimeError(
                        "LLM output was truncated while producing tool calls."
                    )

                if response.content:
                    messages.append(self.llm.assistant_message(response))
                    recovery_state.partial_outputs.append(response.content)
                    last_content = _combine_outputs(
                        recovery_state.partial_outputs
                    )

                if (
                    recovery_state.output_continuations
                    < recovery_policy.max_continuations
                ):
                    recovery_state.output_continuations += 1
                    messages.append(
                        {
                            "role": "user",
                            "content": CONTINUATION_PROMPT,
                        }
                    )
                    emit_recovery_notice(
                        RecoveryNotice(
                            action="continuation",
                            reason="max_tokens",
                            attempt=recovery_state.output_continuations,
                            max_attempts=recovery_policy.max_continuations,
                        )
                    )
                    continue

                emit_recovery_notice(
                    RecoveryNotice(
                        action="exhausted",
                        reason="max_tokens",
                        attempt=recovery_state.output_continuations,
                        max_attempts=recovery_policy.max_continuations,
                    )
                )
                if not last_content:
                    raise RuntimeError(
                        "LLM output recovery exhausted without usable content."
                    )
                self._emit(
                    on_event,
                    "final",
                    step,
                    {
                        "content": last_content,
                        "status": "incomplete",
                    },
                    resolved_run_id,
                )
                return AgentRunResult(
                    status="incomplete",
                    content=last_content,
                    steps=step,
                    tool_calls=tool_call_count,
                    usage=usage,
                    run_id=resolved_run_id,
                    agent_id=self.agent_id,
                )

            final_content = _combine_outputs(
                [*recovery_state.partial_outputs, response.content]
            )
            last_content = final_content
            if not response.tool_calls:
                messages.append({"role": "assistant", "content": response.content})
                self._emit(
                    on_event,
                    "final",
                    step,
                    {"content": final_content},
                    resolved_run_id,
                )
                hook_context.metadata["final_content"] = final_content
                stop_result = self.hooks.trigger_hooks(
                    "Stop",
                    messages,
                    hook_context,
                )
                self._emit_memory_hook_result(
                    on_event,
                    stop_result,
                    step=step,
                    run_id=resolved_run_id,
                )
                return AgentRunResult(
                    status="completed",
                    content=final_content,
                    steps=step,
                    tool_calls=tool_call_count,
                    usage=usage,
                    run_id=resolved_run_id,
                    agent_id=self.agent_id,
                )

            progress = response.content.strip()
            if progress:
                self._emit(
                    on_event,
                    "progress",
                    step,
                    {"content": progress},
                    resolved_run_id,
                )

            messages.append(self.llm.assistant_message(response))

            tool_results = []
            for tool_call in response.tool_calls:
                tool_started_at = perf_counter()
                tool_call_count += 1
                called_tools.add(tool_call.name)
                self._emit(
                    on_event,
                    "tool_call",
                    step,
                    {
                        "id": tool_call.id,
                        "name": tool_call.name,
                        "arguments": tool_call.arguments,
                    },
                    resolved_run_id,
                )

                invalid_arguments = self._invalid_tool_arguments_result(tool_call)
                if invalid_arguments is not None:
                    tool_results.append((tool_call, invalid_arguments))
                    self._emit(
                        on_event,
                        "tool_result",
                        step,
                        {
                            "id": tool_call.id,
                            "name": tool_call.name,
                            "result": invalid_arguments,
                        },
                        resolved_run_id,
                        duration_ms=elapsed_ms(tool_started_at),
                    )
                    continue

                pre_tool_result = self.hooks.trigger_hooks(
                    "PreToolUse",
                    tool_call,
                    hook_context,
                )
                if pre_tool_result is not None and pre_tool_result.denied:
                    tool_result = pre_tool_result.value
                    tool_results.append((tool_call, tool_result))
                    self._emit(
                        on_event,
                        "permission_denied",
                        step,
                        {
                            "id": tool_call.id,
                            "name": tool_call.name,
                            "reason": pre_tool_result.reason,
                            "result": tool_result,
                        },
                        resolved_run_id,
                        duration_ms=elapsed_ms(tool_started_at),
                    )
                    self._emit(
                        on_event,
                        "tool_result",
                        step,
                        {
                            "id": tool_call.id,
                            "name": tool_call.name,
                            "result": tool_result,
                        },
                        resolved_run_id,
                        duration_ms=elapsed_ms(tool_started_at),
                    )
                    continue

                if pre_tool_result is not None and pre_tool_result.reason:
                    self._emit(
                        on_event,
                        "permission_granted",
                        step,
                        {
                            "id": tool_call.id,
                            "name": tool_call.name,
                            "reason": pre_tool_result.reason,
                        },
                        resolved_run_id,
                    )

                tool_context = ToolExecutionContext(
                    run_id=resolved_run_id,
                    agent_id=self.agent_id,
                    parent_run_id=self.parent_run_id,
                    depth=self.depth,
                    step=step,
                    workdir=self.workdir,
                    on_event=on_event,
                    metadata={
                        "tool_call_id": tool_call.id,
                        "tool_name": tool_call.name,
                    },
                )
                tool_result = self._execute_tool_call(tool_call, tool_context)
                post_tool_result = self.hooks.trigger_hooks(
                    "PostToolUse",
                    tool_call,
                    tool_result,
                    hook_context,
                )
                if post_tool_result is not None and post_tool_result.replaces_result:
                    tool_result = post_tool_result.value

                tool_results.append((tool_call, tool_result))
                self._emit(
                    on_event,
                    "tool_result",
                    step,
                    {
                        "id": tool_call.id,
                        "name": tool_call.name,
                        "result": tool_result,
                    },
                    resolved_run_id,
                    duration_ms=elapsed_ms(tool_started_at),
                )

            messages.extend(self.llm.tool_result_messages(tool_results))
            recovery_state.reset_output()
            output_max_tokens = None
            step_index += 1

        self._emit(
            on_event,
            "max_steps",
            step_index,
            {"content": last_content, "max_steps": self.max_steps},
            resolved_run_id,
        )
        return AgentRunResult(
            status="max_steps",
            content=last_content,
            steps=step_index,
            tool_calls=tool_call_count,
            usage=usage,
            run_id=resolved_run_id,
            agent_id=self.agent_id,
        )

    def _invalid_tool_arguments_result(
        self,
        tool_call: LLMToolCall,
    ) -> dict[str, Any] | None:
        invalid_input = tool_call.arguments.get(INVALID_TOOL_INPUT_KEY)
        if invalid_input is None:
            return None
        return {
            "ok": False,
            "error": tool_call.arguments.get(
                "error",
                (
                    f"Tool arguments for {tool_call.name} must be a JSON object. "
                    "Call the tool once per object instead of passing an array."
                ),
            ),
            "received_type": type(invalid_input).__name__,
        }

    def _execute_tool_call(
        self,
        tool_call: LLMToolCall,
        tool_context: ToolExecutionContext,
    ) -> dict[str, Any]:
        batched_inputs = tool_call.arguments.get(BATCHED_TOOL_INPUTS_KEY)
        if batched_inputs is None:
            return self.tools.call(
                tool_call.name,
                tool_call.arguments,
                context=tool_context,
            )

        if not isinstance(batched_inputs, list):
            return {
                "ok": False,
                "error": (
                    f"Batched tool inputs for {tool_call.name} must be a list."
                ),
            }

        results = []
        all_ok = True
        for index, arguments in enumerate(batched_inputs, start=1):
            if not isinstance(arguments, dict):
                result = {
                    "ok": False,
                    "error": (
                        f"Batched input #{index} for {tool_call.name} must be "
                        "a JSON object."
                    ),
                }
            else:
                result = self.tools.call(
                    tool_call.name,
                    arguments,
                    context=tool_context,
                )
            all_ok = all_ok and bool(result.get("ok"))
            results.append(
                {
                    "index": index,
                    "result": result,
                }
            )

        return {
            "ok": all_ok,
            "batched": True,
            "count": len(results),
            "results": results,
        }

    def _emit(
        self,
        on_event: AgentCallback | None,
        event_type: AgentEventType,
        step: int,
        data: dict[str, Any],
        run_id: str,
        *,
        duration_ms: float | None = None,
    ) -> None:
        _emit(
            on_event,
            event_type,
            step,
            data,
            agent_id=self.agent_id,
            run_id=run_id,
            parent_run_id=self.parent_run_id,
            depth=self.depth,
            duration_ms=duration_ms,
        )

    def _forward_recovery_notice(
        self,
        on_event: AgentCallback | None,
        notice: RecoveryNotice,
        *,
        run_id: str,
    ) -> None:
        if on_event is None:
            return
        trace_context = current_trace_context()
        on_event(
            AgentEvent(
                type="recovery",
                step=trace_context.step if trace_context else 0,
                data=notice.to_dict(),
                agent_id=self.agent_id,
                run_id=run_id,
                parent_run_id=self.parent_run_id,
                depth=self.depth,
            )
        )

    def _emit_context_update(
        self,
        on_event: AgentCallback | None,
        update: ContextUpdate,
        *,
        step: int,
        run_id: str,
    ) -> None:
        data = {
            "reason": update.reason,
            "before_tokens": update.before_tokens,
            "after_tokens": update.after_tokens,
            "transcript_path": (
                str(update.transcript_path) if update.transcript_path else None
            ),
            "persisted_results": update.persisted_results,
            "compacted_results": update.compacted_results,
            "summary_created": update.summary_created,
            "hard_trimmed": update.hard_trimmed,
        }
        if update.persisted_results:
            self._emit(
                on_event,
                "tool_result_persisted",
                step,
                data,
                run_id,
            )
        if update.summary_created or update.hard_trimmed or update.compacted_results:
            self._emit(
                on_event,
                "context_compacted",
                step,
                data,
                run_id,
            )
        if update.error:
            self._emit(
                on_event,
                "context_compact_failed",
                step,
                {**data, "error": update.error},
                run_id,
            )

    def _emit_memory_hook_result(
        self,
        on_event: AgentCallback | None,
        result: Any,
        *,
        step: int,
        run_id: str,
    ) -> None:
        if result is None:
            return
        extracted = result.data.get("extracted_memories", [])
        if isinstance(extracted, list) and extracted:
            self._emit(
                on_event,
                "memory_extracted",
                step,
                {
                    "count": len(extracted),
                    "memories": extracted,
                },
                run_id,
            )
        error = result.data.get("memory_error")
        if error:
            self._emit(
                on_event,
                "memory_extract_failed",
                step,
                {"error": str(error)},
                run_id,
            )

    def _collect_background_notifications(
        self,
        messages: list[dict[str, Any]],
        *,
        on_event: AgentCallback | None,
        step: int,
        run_id: str,
    ) -> None:
        if self.background_jobs is None:
            return
        notifications = self.background_jobs.drain_notifications(
            owner_agent_id=self.agent_id,
        )
        if not notifications:
            return

        messages.append(
            {
                "role": "user",
                "content": format_background_notifications(notifications),
            }
        )
        self.background_jobs.acknowledge_notifications(
            [notification.job_id for notification in notifications]
        )
        for notification in notifications:
            event_type: AgentEventType
            if notification.status == "completed":
                event_type = "background_completed"
            elif notification.status in {"cancelled", "interrupted"}:
                event_type = "background_cancelled"
            else:
                event_type = "background_failed"
            self._emit(
                on_event,
                event_type,
                step,
                notification.to_dict(),
                run_id,
            )


def print_agent_event(event: AgentEvent) -> None:
    prefix = _event_prefix(event)

    if event.type == "step":
        print(f"\n{prefix}{ANSI_BLUE}[step {event.step}] calling llm{ANSI_RESET}")
        return

    if event.type == "recovery":
        _print_recovery_event(prefix, event.data)
        return

    if event.type == "progress":
        print(
            f"{prefix}{ANSI_MAGENTA}[progress]{ANSI_RESET} "
            f"{event.data['content']}"
        )
        return

    if event.type == "tool_call":
        arguments = json.dumps(event.data["arguments"], ensure_ascii=False)
        print(
            f"{prefix}{ANSI_YELLOW}> [tool call] {event.data['name']}{ANSI_RESET} "
            f"{ANSI_DIM}{arguments}{ANSI_RESET}"
        )
        return

    if event.type == "task_context":
        print(f"{prefix}{ANSI_DIM}[task context updated]{ANSI_RESET}")
        return

    if event.type == "task_reminder":
        print(
            f"{prefix}{ANSI_YELLOW}[task reminder]{ANSI_RESET} "
            f"{event.data['content']}"
        )
        return

    if event.type == "memory_context":
        memory_ids = event.data.get("memory_ids", [])
        print(
            f"{prefix}{ANSI_DIM}[memory recalled]{ANSI_RESET} "
            f"{', '.join(str(memory_id) for memory_id in memory_ids)}"
        )
        return

    if event.type == "artifact_context":
        artifact_ids = event.data.get("artifact_ids", [])
        print(
            f"{prefix}{ANSI_DIM}[artifacts loaded]{ANSI_RESET} "
            f"{', '.join(str(artifact_id) for artifact_id in artifact_ids)}"
        )
        return

    if event.type == "memory_extracted":
        print(
            f"{prefix}{ANSI_YELLOW}[memory saved]{ANSI_RESET} "
            f"{event.data['count']} new or updated"
        )
        return

    if event.type == "memory_extract_failed":
        print(
            f"{prefix}{ANSI_RED}[memory extraction failed]{ANSI_RESET} "
            f"{event.data['error']}"
        )
        return

    if event.type == "tool_result":
        result = event.data["result"]
        result_color = _tool_result_color(result)

        print(
            f"{prefix}{result_color}[tool result] "
            f"{event.data['name']} ->{ANSI_RESET} "
            f"{json.dumps(result, ensure_ascii=False)}"
        )
        return

    if event.type == "permission_granted":
        print(
            f"{prefix}{ANSI_GREEN}[permission granted] "
            f"{event.data['name']} ->{ANSI_RESET} "
            f"{ANSI_DIM}{event.data['reason']}{ANSI_RESET}"
        )
        return

    if event.type == "permission_denied":
        print(
            f"{prefix}{ANSI_RED}[permission denied] "
            f"{event.data['name']} ->{ANSI_RESET} "
            f"{event.data['reason']}"
        )
        return

    if event.type == "final":
        label = (
            "[final: incomplete]"
            if event.data.get("status") == "incomplete"
            else "[final]"
        )
        print(
            f"\n{prefix}{ANSI_BOLD}{ANSI_MAGENTA}{label}{ANSI_RESET}\n"
            f"{event.data['content']}"
        )
        return

    if event.type == "max_steps":
        print(
            f"{prefix}{ANSI_RED}[max steps reached]{ANSI_RESET} "
            f"{event.data['max_steps']}"
        )
        return

    if event.type == "subagent_started":
        print(
            f"{prefix}{ANSI_MAGENTA}[subagent started]{ANSI_RESET} "
            f"{event.data['task']}"
        )
        return

    if event.type == "subagent_completed":
        print(
            f"{prefix}{ANSI_GREEN}[subagent completed]{ANSI_RESET} "
            f"{event.data['status']} in {event.data['steps']} steps"
        )
        return

    if event.type == "subagent_failed":
        print(
            f"{prefix}{ANSI_RED}[subagent failed]{ANSI_RESET} "
            f"{event.data['error']}"
        )
        return

    if event.type == "tool_result_persisted":
        print(
            f"{prefix}{ANSI_DIM}[tool results persisted]{ANSI_RESET} "
            f"{event.data['persisted_results']}"
        )
        return

    if event.type == "context_compacted":
        print(
            f"{prefix}{ANSI_YELLOW}[context compacted]{ANSI_RESET} "
            f"{event.data['reason']} "
            f"{event.data['before_tokens']} -> {event.data['after_tokens']} tokens"
        )
        return

    if event.type == "context_compact_failed":
        print(
            f"{prefix}{ANSI_RED}[context compact failed]{ANSI_RESET} "
            f"{event.data['error']}"
        )
        return

    if event.type == "background_completed":
        print(
            f"{prefix}{ANSI_GREEN}[background completed]{ANSI_RESET} "
            f"{event.data['job_id']} exit={event.data['return_code']}"
        )
        return

    if event.type == "background_failed":
        print(
            f"{prefix}{ANSI_RED}[background failed]{ANSI_RESET} "
            f"{event.data['job_id']} status={event.data['status']} "
            f"{event.data.get('error') or ''}".rstrip()
        )
        return

    if event.type == "background_cancelled":
        print(
            f"{prefix}{ANSI_YELLOW}[background stopped]{ANSI_RESET} "
            f"{event.data['job_id']} status={event.data['status']}"
        )


def _print_recovery_event(prefix: str, data: dict[str, Any]) -> None:
    action = str(data.get("action", "recovery"))
    reason = str(data.get("reason", "unknown"))
    if action == "retry":
        attempt = data.get("attempt", "?")
        maximum = data.get("max_attempts", "?")
        delay = data.get("delay_seconds", 0)
        print(
            f"{prefix}{ANSI_YELLOW}[recovery retry]{ANSI_RESET} "
            f"{reason} {attempt}/{maximum}, wait {delay}s"
        )
        return
    if action == "fallback":
        print(
            f"{prefix}{ANSI_MAGENTA}[model fallback]{ANSI_RESET} "
            f"{data.get('from_model')} -> {data.get('to_model')}"
        )
        return
    if action == "context_compact":
        print(
            f"{prefix}{ANSI_YELLOW}[recovery compact]{ANSI_RESET} "
            "context length exceeded"
        )
        return
    if action == "output_escalate":
        print(
            f"{prefix}{ANSI_YELLOW}[output limit increased]{ANSI_RESET} "
            f"{data.get('before_max_tokens')} -> "
            f"{data.get('after_max_tokens')}"
        )
        return
    if action == "continuation":
        print(
            f"{prefix}{ANSI_YELLOW}[output continuation]{ANSI_RESET} "
            f"{data.get('attempt')}/{data.get('max_attempts')}"
        )
        return
    color = ANSI_RED if action == "exhausted" else ANSI_YELLOW
    print(f"{prefix}{color}[recovery {action}]{ANSI_RESET} {reason}")


def _emit(
    on_event: AgentCallback | None,
    event_type: AgentEventType,
    step: int,
    data: dict[str, Any],
    *,
    agent_id: str = "agent",
    run_id: str = "",
    parent_run_id: str | None = None,
    depth: int = 0,
    duration_ms: float | None = None,
) -> None:
    event = AgentEvent(
        type=event_type,
        step=step,
        data=data,
        agent_id=agent_id,
        run_id=run_id,
        parent_run_id=parent_run_id,
        depth=depth,
        duration_ms=duration_ms,
    )
    record_agent_event(event)
    if on_event is not None:
        on_event(event)


def _hook_messages(result: Any) -> list[str]:
    if result is None:
        return []
    messages = result.data.get("messages", [])
    if not isinstance(messages, list):
        return []
    return [message for message in messages if isinstance(message, str) and message]


def _runtime_context_event_type(content: str) -> AgentEventType | None:
    if content.startswith("<current_tasks>"):
        return "task_context"
    if content.startswith("<task_reminder>"):
        return "task_reminder"
    if content.startswith("<relevant_artifacts>"):
        return "artifact_context"
    if content.startswith("<relevant_memories>"):
        return "memory_context"
    return None


def _runtime_context_key(content: str) -> str:
    match = content.lstrip().removeprefix("<").split(">", 1)[0]
    return match.split(" ", 1)[0] or content


def _latest_external_user_message(
    messages: list[dict[str, Any]],
) -> str | None:
    internal_prefixes = (
        "<current_tasks>",
        "<task_reminder>",
        "<relevant_artifacts>",
        "<relevant_memories>",
        "<conversation_summary",
        "<context_compacted",
        "<recovery_continuation>",
        "<background_notifications>",
    )
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if (
            isinstance(content, str)
            and not content.lstrip().startswith(internal_prefixes)
        ):
            return content
    return None


def _combine_outputs(outputs: list[str]) -> str:
    return "\n".join(output for output in outputs if output)


def _merge_usage(target: dict[str, Any], usage: dict[str, Any]) -> None:
    for key, value in usage.items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            target[key] = target.get(key, 0) + value
        else:
            target[key] = value


def _event_prefix(event: AgentEvent) -> str:
    if event.depth <= 0:
        return ""
    return f"{ANSI_DIM}[{event.agent_id}] {ANSI_RESET}"


def _tool_result_color(result: Any) -> str:
    if not isinstance(result, dict):
        return ANSI_GREEN
    if result.get("ok") is False:
        return ANSI_RED
    payload = result.get("result")
    if not isinstance(payload, dict):
        return ANSI_GREEN
    if payload.get("status") in {"failed", "timed_out"}:
        return ANSI_RED
    if payload.get("outcome") in {"failed", "error", "timed_out"}:
        return ANSI_RED
    if payload.get("outcome") in {"issues_found", "no_tests"}:
        return ANSI_YELLOW
    return ANSI_GREEN
