from __future__ import annotations

import json
from dataclasses import dataclass

from llm_agent.background_jobs import (
    BACKGROUND_JOB_STATUSES,
    BackgroundJob,
    BackgroundJobManager,
)
from llm_agent.tool_registry import ToolDefinition, ToolRegistry


@dataclass(frozen=True)
class BackgroundTools:
    manager: BackgroundJobManager

    def list_jobs(
        self,
        status: str | None = None,
        limit: int = 20,
    ) -> str:
        jobs = self.manager.list_jobs(status=status, limit=limit)  # type: ignore[arg-type]
        return _format_jobs(jobs)

    def get(self, job_id: str) -> str:
        return _format_job(self.manager.get_job(job_id))

    def output(
        self,
        job_id: str,
        stream: str = "combined",
        tail_chars: int = 8_000,
    ) -> str:
        output = self.manager.read_output(
            job_id,
            stream=stream,  # type: ignore[arg-type]
            tail_chars=tail_chars,
        )
        return json.dumps(output, ensure_ascii=False, indent=2)

    def wait(
        self,
        job_id: str,
        timeout_seconds: float = 30.0,
    ) -> str:
        resolved_timeout = min(timeout_seconds, 30.0)
        return _format_job(self.manager.wait(job_id, timeout_seconds=resolved_timeout))

    def cancel(self, job_id: str) -> str:
        return _format_job(self.manager.cancel(job_id))


def background_tool_definitions(
    manager: BackgroundJobManager,
) -> list[ToolDefinition]:
    tools = BackgroundTools(manager)
    status_values = sorted(BACKGROUND_JOB_STATUSES)
    return [
        ToolDefinition(
            name="background_list",
            description=(
                "List recent managed background jobs and their current status."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": status_values,
                        "description": "Optional status filter.",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 100,
                        "description": "Maximum number of jobs to return.",
                    },
                },
            },
            func=tools.list_jobs,
        ),
        ToolDefinition(
            name="background_get",
            description="Get metadata and status for one background job.",
            parameters={
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "Background job id returned by bash.",
                    }
                },
                "required": ["job_id"],
            },
            func=tools.get,
        ),
        ToolDefinition(
            name="background_output",
            description=(
                "Read the tail of a background job's stdout, stderr, or both."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "Background job id.",
                    },
                    "stream": {
                        "type": "string",
                        "enum": ["combined", "stdout", "stderr"],
                        "description": "Log stream to read.",
                    },
                    "tail_chars": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": manager.max_output_chars,
                        "description": "Maximum trailing characters per stream.",
                    },
                },
                "required": ["job_id"],
            },
            func=tools.output,
        ),
        ToolDefinition(
            name="background_wait",
            description=(
                "Wait briefly for a background job, then return its current "
                "status. The wait is capped at 30 seconds."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "Background job id.",
                    },
                    "timeout_seconds": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 30,
                        "description": "Maximum time to wait.",
                    },
                },
                "required": ["job_id"],
            },
            func=tools.wait,
        ),
        ToolDefinition(
            name="background_cancel",
            description="Cancel a running managed background job.",
            parameters={
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "Background job id.",
                    }
                },
                "required": ["job_id"],
            },
            func=tools.cancel,
        ),
    ]


def register_tools(
    registry: ToolRegistry,
    *,
    manager: BackgroundJobManager,
) -> None:
    registry.register_many(background_tool_definitions(manager))


def _format_job(job: BackgroundJob) -> str:
    return json.dumps(job.to_dict(), ensure_ascii=False, indent=2)


def _format_jobs(jobs: list[BackgroundJob]) -> str:
    if not jobs:
        return "No background jobs."
    return json.dumps(
        [job.to_dict() for job in jobs],
        ensure_ascii=False,
        indent=2,
    )


__all__ = [
    "BackgroundTools",
    "background_tool_definitions",
    "register_tools",
]
