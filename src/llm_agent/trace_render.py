from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
from typing import Any
from uuid import uuid4


def render_trace_markdown(
    jsonl_path: Path | str,
    output_path: Path | str,
) -> Path:
    source = Path(jsonl_path)
    target = Path(output_path)
    records = _read_records(source)
    lines = _render(records, source)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target.with_suffix(target.suffix + f".{uuid4().hex[:8]}.tmp")
    tmp_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    tmp_path.replace(target)
    return target


def _read_records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid trace JSON at {path}:{line_number}") from exc
        if isinstance(record, dict):
            records.append(record)
    return records


def _render(
    records: list[dict[str, Any]],
    source: Path,
) -> list[str]:
    if not records:
        return ["# Agent Trace", "", "No trace records."]

    first = records[0]
    run_end = next(
        (
            record
            for record in reversed(records)
            if record.get("category") == "run"
            and record.get("name") in {"run.completed", "run.failed"}
        ),
        None,
    )
    categories = Counter(str(record.get("category", "unknown")) for record in records)
    llm_records = [
        record
        for record in records
        if record.get("category") == "llm" and record.get("phase") == "completed"
    ]
    tool_records = [
        record
        for record in records
        if record.get("category") == "tool" and record.get("name") == "tool_result"
    ]
    usage = _sum_usage(llm_records)
    status = (
        str(run_end.get("status", "unknown")) if run_end is not None else "incomplete"
    )
    duration = run_end.get("duration_ms") if run_end else None

    lines = [
        "# Agent Trace",
        "",
        f"- Trace: `{first.get('trace_id', '')}`",
        f"- Root run: `{first.get('root_run_id', '')}`",
        f"- Status: `{status}`",
        f"- Records: `{len(records)}`",
        f"- Source: `{source}`",
    ]
    if duration is not None:
        lines.append(f"- Duration: `{duration} ms`")

    lines.extend(["", "## Summary", ""])
    lines.append(f"- LLM calls: `{len(llm_records)}`")
    lines.append(f"- Tool results: `{len(tool_records)}`")
    if usage:
        lines.append(
            "- Usage: `"
            + ", ".join(f"{key}={value}" for key, value in sorted(usage.items()))
            + "`"
        )
    lines.append(
        "- Categories: `"
        + ", ".join(f"{key}={value}" for key, value in sorted(categories.items()))
        + "`"
    )

    lines.extend(
        [
            "",
            "## Timeline",
            "",
            "| # | Time | Agent | Step | Event | Status | Duration | Detail |",
            "|---:|---|---|---:|---|---|---:|---|",
        ]
    )
    for record in records:
        lines.append(
            "| "
            + " | ".join(
                [
                    str(record.get("sequence", "")),
                    _escape(_short_time(record.get("timestamp"))),
                    _escape(str(record.get("agent_id", ""))),
                    str(record.get("step", "")),
                    _escape(_event_label(record)),
                    _escape(str(record.get("status", ""))),
                    _duration(record.get("duration_ms")),
                    _escape(_detail(record)),
                ]
            )
            + " |"
        )
    return lines


def _sum_usage(records: list[dict[str, Any]]) -> dict[str, int | float]:
    totals: dict[str, int | float] = {}
    for record in records:
        data = record.get("data") or {}
        usage = data.get("usage") if isinstance(data, dict) else None
        if not isinstance(usage, dict):
            continue
        for key, value in usage.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                totals[key] = totals.get(key, 0) + value
    return totals


def _event_label(record: dict[str, Any]) -> str:
    name = str(record.get("name", ""))
    phase = str(record.get("phase", ""))
    return f"{name}:{phase}" if phase and phase != "event" else name


def _detail(record: dict[str, Any]) -> str:
    data = record.get("data")
    if not isinstance(data, dict) or not data:
        return ""
    preferred = (
        "operation",
        "model",
        "provider",
        "name",
        "reason",
        "error",
        "content",
        "message",
    )
    for key in preferred:
        if key not in data:
            continue
        value = data[key]
        if isinstance(value, dict):
            value = value.get("preview") or json.dumps(value, ensure_ascii=False)
        text = str(value).replace("\n", " ")
        return text[:180]
    return ", ".join(sorted(str(key) for key in data)[:8])


def _short_time(value: Any) -> str:
    text = str(value or "")
    return text.split("T", 1)[-1].removesuffix("Z")


def _duration(value: Any) -> str:
    if isinstance(value, (int, float)):
        return f"{value:.3f}"
    return ""


def _escape(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


__all__ = ["render_trace_markdown"]
