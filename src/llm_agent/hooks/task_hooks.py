from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from llm_agent.hooks import HookContext, HookResult
from llm_agent.task_system import OPEN_TASK_STATUSES, TaskManager


CODING_TASK_KEYWORDS = (
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
class TaskPlanningHook:
    workdir: Path | str | None = None
    task_list_id: str = "default"
    inject_task_summary: bool = True
    remind_for_complex_tasks: bool = True
    _last_task_summary: str | None = field(default=None, init=False)
    _last_reminder_key: str | None = field(default=None, init=False)

    def __call__(self, context: HookContext) -> HookResult | None:
        manager = self._manager(context)
        messages: list[str] = []

        if self.inject_task_summary:
            summary = manager.summary(include_completed=False)
            if summary != "No tasks." and summary != self._last_task_summary:
                messages.append(f"<current_tasks>\n{summary}\n</current_tasks>")
                self._last_task_summary = summary

        reminder = self._build_reminder(manager, context)
        if reminder is not None:
            key = reminder[:240]
            if key != self._last_reminder_key:
                messages.append(f"<task_reminder>\n{reminder}\n</task_reminder>")
                self._last_reminder_key = key

        if not messages:
            return None
        return HookResult.allow(data={"messages": messages})

    def _manager(self, context: HookContext) -> TaskManager:
        workdir = context.workdir if self.workdir is None else self.workdir
        return TaskManager.for_workdir(workdir, task_list_id=self.task_list_id)

    def _build_reminder(
        self,
        manager: TaskManager,
        context: HookContext,
    ) -> str | None:
        tasks = manager.list_tasks(include_completed=False)
        open_tasks = [task for task in tasks if task.status in OPEN_TASK_STATUSES]
        in_progress_tasks = [task for task in open_tasks if task.status == "in_progress"]

        latest_user_text = _latest_plain_user_message(context.messages)
        if not open_tasks and self.remind_for_complex_tasks:
            if latest_user_text and _looks_like_coding_task(latest_user_text):
                return (
                    "This looks like a multi-step coding task. Before changing files "
                    "or running broad commands, create session-scope tasks with "
                    "task_create. Keep task status current and add verification "
                    "evidence when completing work."
                )

        if open_tasks and not in_progress_tasks:
            return (
                "There are open tasks but no in-progress task. Claim a task with "
                "task_claim or update one to in_progress before continuing the work."
            )

        completed_without_evidence = [
            task
            for task in manager.list_tasks(include_completed=True)
            if task.status == "completed" and not task.evidence
        ]
        if (
            completed_without_evidence
            and not open_tasks
            and latest_user_text
            and _looks_like_coding_task(latest_user_text)
        ):
            return (
                "All open tasks are completed, but at least one completed task has no "
                "verification evidence. Add evidence with task_update or task_complete "
                "after running checks."
            )

        return None


def _latest_plain_user_message(messages: list[dict[str, Any]]) -> str | None:
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
    return None


def _looks_like_coding_task(text: str) -> bool:
    lowered = text.lower()
    if len(text) >= 80:
        return True
    return any(keyword in lowered for keyword in CODING_TASK_KEYWORDS)


__all__ = ["TaskPlanningHook"]
