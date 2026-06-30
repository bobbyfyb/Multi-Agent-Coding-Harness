from pathlib import Path
import json
import shlex
import sys
from typing import Any

import pytest

from llm_agent.agent import Agent, AgentEvent, ToolExecutionContext
from llm_agent.background_jobs import (
    BackgroundJob,
    BackgroundJobError,
    BackgroundJobManager,
    BackgroundJobStore,
)
from llm_agent.llm_client import LLMResponse
from llm_agent.tools import build_default_registry
from llm_agent.trace_system import TraceRecorder, trace_scope


def _python_command(source: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(source)}"


class FakeLLM:
    model = "fake"

    def __init__(self) -> None:
        self.messages: list[list[dict[str, Any]]] = []

    def chat(
        self,
        messages: list[dict[str, Any]],
        **_: Any,
    ) -> LLMResponse:
        self.messages.append([dict(message) for message in messages])
        return LLMResponse(content="handled", tool_calls=[], raw={})


def test_background_job_completes_and_persists_output(
    tmp_path: Path,
) -> None:
    manager = BackgroundJobManager.for_workdir(
        tmp_path,
        terminate_grace_seconds=0.1,
    )
    try:
        job = manager.start_shell(
            _python_command(
                "from pathlib import Path; "
                "print(Path.cwd()); "
                "print('warning', file=__import__('sys').stderr)"
            ),
            owner_run_id="run-test",
            owner_agent_id="main",
        )

        completed = manager.wait(job.id, timeout_seconds=3)
        output = manager.read_output(job.id)

        assert completed.status == "completed"
        assert completed.return_code == 0
        assert str(tmp_path) in output["stdout"]
        assert "warning" in output["stderr"]
        assert Path(completed.stdout_path).exists()
        assert Path(completed.stderr_path).exists()
    finally:
        manager.shutdown()


def test_background_job_records_nonzero_exit_as_failed(
    tmp_path: Path,
) -> None:
    manager = BackgroundJobManager.for_workdir(tmp_path)
    try:
        job = manager.start_shell(
            _python_command("import sys; print('bad', file=sys.stderr); sys.exit(7)")
        )

        failed = manager.wait(job.id, timeout_seconds=3)

        assert failed.status == "failed"
        assert failed.return_code == 7
        assert "code 7" in str(failed.error)
        assert "bad" in manager.read_output(job.id)["stderr"]
    finally:
        manager.shutdown()


def test_background_job_can_be_cancelled(tmp_path: Path) -> None:
    manager = BackgroundJobManager.for_workdir(
        tmp_path,
        terminate_grace_seconds=0.1,
    )
    try:
        job = manager.start_shell(_python_command("import time; time.sleep(30)"))

        cancelled = manager.cancel(job.id)

        assert cancelled.status == "cancelled"
        assert cancelled.finished_at is not None
    finally:
        manager.shutdown()


def test_background_job_times_out(tmp_path: Path) -> None:
    manager = BackgroundJobManager.for_workdir(
        tmp_path,
        max_runtime_seconds=0.05,
        terminate_grace_seconds=0.1,
    )
    try:
        job = manager.start_shell(
            _python_command("import time; time.sleep(30)")
        )

        timed_out = manager.wait(job.id, timeout_seconds=3)

        assert timed_out.status == "timed_out"
        assert "timed out" in str(timed_out.error)
    finally:
        manager.shutdown()


def test_background_job_enforces_concurrency_limit(
    tmp_path: Path,
) -> None:
    manager = BackgroundJobManager.for_workdir(
        tmp_path,
        max_concurrent=1,
        terminate_grace_seconds=0.1,
    )
    try:
        manager.start_shell(
            _python_command("import time; time.sleep(30)")
        )

        with pytest.raises(
            BackgroundJobError,
            match="concurrency limit",
        ):
            manager.start_shell(_python_command("print('second')"))
    finally:
        manager.shutdown()


def test_background_notifications_are_acknowledged_once(
    tmp_path: Path,
) -> None:
    manager = BackgroundJobManager.for_workdir(tmp_path)
    try:
        job = manager.start_shell(
            _python_command("print('done')"),
            owner_agent_id="main",
        )
        manager.wait(job.id, timeout_seconds=3)

        notifications = manager.drain_notifications(owner_agent_id="main")
        manager.acknowledge_notifications(
            [notification.job_id for notification in notifications]
        )

        assert [notification.job_id for notification in notifications] == [job.id]
        assert manager.drain_notifications(owner_agent_id="main") == []
        assert manager.get_job(job.id).notified_at is not None
    finally:
        manager.shutdown()


def test_background_manager_marks_stale_running_job_interrupted(
    tmp_path: Path,
) -> None:
    store = BackgroundJobStore.for_workdir(tmp_path)
    stdout_path, stderr_path = store.create_job_paths("bg_stale")
    store.save(
        BackgroundJob(
            id="bg_stale",
            command="old command",
            cwd=str(tmp_path),
            status="running",
            owner_agent_id="main",
            stdout_path=str(stdout_path),
            stderr_path=str(stderr_path),
        )
    )

    manager = BackgroundJobManager(store)
    try:
        restored = manager.get_job("bg_stale")
        notifications = manager.drain_notifications(
            owner_agent_id="main"
        )

        assert restored.status == "interrupted"
        assert [notification.job_id for notification in notifications] == [
            "bg_stale"
        ]
    finally:
        manager.shutdown()


def test_background_job_records_trace_after_process_completion(
    tmp_path: Path,
) -> None:
    manager = BackgroundJobManager.for_workdir(tmp_path)
    trace = TraceRecorder.for_run(tmp_path, run_id="run-background")
    try:
        with trace_scope(
            trace,
            run_id="run-background",
            agent_id="main",
        ):
            job = manager.start_shell(_python_command("print('done')"))
        manager.wait(job.id, timeout_seconds=3)

        records = [
            json.loads(line)
            for line in trace.jsonl_path.read_text(
                encoding="utf-8"
            ).splitlines()
        ]

        assert [record["name"] for record in records] == [
            "background.started",
            "background.completed",
        ]
        assert all(
            record["correlation_id"] == job.id for record in records
        )
    finally:
        manager.shutdown()


def test_default_registry_supports_explicit_background_bash(
    tmp_path: Path,
) -> None:
    manager = BackgroundJobManager.for_workdir(tmp_path)
    registry = build_default_registry(
        workdir=tmp_path,
        background_manager=manager,
    )
    context = ToolExecutionContext(
        run_id="run-test",
        agent_id="main",
        parent_run_id=None,
        depth=0,
        step=1,
        workdir=tmp_path,
    )
    try:
        result = registry.call(
            "bash",
            {
                "command": _python_command(
                    "import time; time.sleep(0.2); print('done')"
                ),
                "run_in_background": True,
                "task_id": "task_0001",
            },
            context=context,
        )

        assert result["ok"] is True
        job_id = result["result"]["job_id"]
        assert job_id.startswith("bg_")
        assert result["result"]["task_id"] == "task_0001"
        assert manager.wait(job_id, timeout_seconds=3).status == "completed"
        assert "done" in manager.read_output(job_id)["stdout"]
        assert {
            "background_list",
            "background_get",
            "background_output",
            "background_wait",
            "background_cancel",
        } <= set(registry.names())
    finally:
        manager.shutdown()


def test_agent_injects_completed_job_as_separate_notification(
    tmp_path: Path,
) -> None:
    manager = BackgroundJobManager.for_workdir(tmp_path)
    job = manager.start_shell(
        _python_command("print('done')"),
        owner_agent_id="main",
    )
    manager.wait(job.id, timeout_seconds=3)
    llm = FakeLLM()
    events: list[AgentEvent] = []
    agent = Agent(
        llm=llm,  # type: ignore[arg-type]
        tools=build_default_registry(
            workdir=tmp_path,
            background_manager=manager,
        ),
        context_manager="system",
        workdir=tmp_path,
        agent_id="main",
        background_jobs=manager,
    )
    messages = agent.new_messages()
    messages.append({"role": "user", "content": "continue"})
    try:
        result = agent.run(messages, on_event=events.append)

        notification_messages = [
            message
            for message in llm.messages[0]
            if str(message.get("content", "")).startswith("<background_notifications>")
        ]
        assert result.content == "handled"
        assert len(notification_messages) == 1
        assert job.id in notification_messages[0]["content"]
        assert any(
            event.type == "background_completed" and event.data["job_id"] == job.id
            for event in events
        )
    finally:
        manager.shutdown()
