import os
from pathlib import Path
import sys
from uuid import uuid4

from prompt_toolkit.patch_stdout import patch_stdout

sys.path.insert(0, str(Path(__file__).parent / "src"))

from llm_agent.agent import Agent, print_agent_event
from llm_agent.artifact_system import (
    ArtifactManager,
    build_artifact_policy_section,
)
from llm_agent.background_jobs import (
    BACKGROUND_JOB_INSTRUCTIONS,
    BackgroundJobManager,
)
from llm_agent.cli import (
    build_approval_prompt_session,
    build_user_prompt_session,
    read_user_prompt,
)
from llm_agent.context_manager import ContextManager, PromptSection
from llm_agent.hooks import HookContext, build_default_hook_manager
from llm_agent.hooks.permission_hooks import (
    ApprovalProvider,
    CliApprovalProvider,
)
from llm_agent.llm_client import LLMClient
from llm_agent.memory_system import (
    MemoryManager,
    build_memory_policy_section,
)
from llm_agent.mcp_config import load_mcp_config
from llm_agent.mcp_system import MCPManager
from llm_agent.recovery import RecoveryPolicy
from llm_agent.serial_workflow import (
    SerialCodingWorkflow,
    WorkflowResult,
    parse_workflow_command,
)
from llm_agent.skill_system import SkillRegistry, build_skill_catalog_section
from llm_agent.subagent import SUBAGENT_PARENT_INSTRUCTIONS, SubagentRunner
from llm_agent.tools import build_default_registry
from llm_agent.trace_system import (
    TraceRecorder,
    record_trace,
    summarize_text,
    trace_scope,
)
from llm_agent.workflow_config import load_workflow_config
from llm_agent.workflow_store import WorkflowRunRecord
from llm_agent.worktree import WorktreeManager


def build_agent(
    *,
    approval_provider: ApprovalProvider | None = None,
    llm: LLMClient | None = None,
    mcp_manager: MCPManager | None = None,
) -> Agent:
    workdir = Path.cwd()
    if llm is None:
        llm = _build_llm()
    if approval_provider is None:
        approval_provider = CliApprovalProvider()
    skill_registry = SkillRegistry.for_workdir(workdir)
    memory_manager = MemoryManager.for_workdir(workdir, llm=llm)
    artifact_manager = ArtifactManager.for_workdir(workdir)
    worktree_manager = WorktreeManager.for_workdir(workdir)
    background_jobs = BackgroundJobManager.for_workdir(
        workdir,
        max_concurrent=int(os.getenv("BACKGROUND_MAX_CONCURRENT", "4")),
        max_runtime_seconds=float(
            os.getenv("BACKGROUND_MAX_RUNTIME_SECONDS", "1800")
        ),
    )
    subagent_runner = SubagentRunner(
        llm=llm,
        workdir=workdir,
        skill_registry=skill_registry,
        memory_manager=memory_manager,
        worktree_manager=worktree_manager,
        mcp_manager=mcp_manager,
        approval_provider=approval_provider,
    )
    registry = build_default_registry(
        workdir=workdir,
        subagent_runner=subagent_runner,
        skill_registry=skill_registry,
        memory_manager=memory_manager,
        artifact_manager=artifact_manager,
        background_manager=background_jobs,
        worktree_manager=worktree_manager,
        mcp_manager=mcp_manager,
        mcp_scope="main",
    )
    return Agent(
        llm=llm,
        tools=registry,
        hooks=build_default_hook_manager(
            workdir=workdir,
            approval_provider=approval_provider,
            llm=llm,
            memory_manager=memory_manager,
            artifact_manager=artifact_manager,
            mcp_manager=mcp_manager,
        ),
        workdir=workdir,
        max_steps=None,
        agent_id="main",
        background_jobs=background_jobs,
        context_manager=ContextManager(
            llm=llm,
            workdir=workdir,
            max_context_tokens=int(
                os.getenv("LLM_CONTEXT_WINDOW", "100000")
            ),
            sections=[
                PromptSection(
                    name="workspace",
                    content=f"Current Working DIR: {workdir}",
                    priority=20,
                ),
                PromptSection(
                    name="delegation",
                    content=SUBAGENT_PARENT_INSTRUCTIONS,
                    priority=30,
                ),
                PromptSection(
                    name="background_jobs",
                    content=BACKGROUND_JOB_INSTRUCTIONS,
                    priority=35,
                ),
                build_memory_policy_section(),
                build_artifact_policy_section(),
                build_skill_catalog_section(skill_registry),
            ]
        ),
    )


def _build_llm() -> LLMClient:
    llm = LLMClient(
        provider=os.getenv("LLM_PROVIDER", "anthropic"),
        max_tokens=int(os.getenv("LLM_MAX_TOKENS", "4096")),
        timeout=float(os.getenv("LLM_TIMEOUT", "240")),
    )
    llm.recovery_policy = RecoveryPolicy(
        max_retries=int(os.getenv("LLM_MAX_RETRIES", "4")),
        max_retry_elapsed_seconds=float(
            os.getenv("LLM_MAX_RETRY_ELAPSED_SECONDS", "30")
        ),
        fallback_model=(
            os.getenv("LLM_FALLBACK_MODEL")
            or os.getenv("FALLBACK_MODEL_ID")
        ),
        escalated_max_tokens=int(
            os.getenv("LLM_ESCALATED_MAX_TOKENS", "8192")
        ),
        max_continuations=int(os.getenv("LLM_MAX_CONTINUATIONS", "2")),
    )
    return llm


def main() -> None:
    workdir = Path.cwd()
    prompt_session = build_user_prompt_session(workdir)
    approval_session = build_approval_prompt_session()
    approval_provider = CliApprovalProvider(
        input_func=approval_session.prompt,
    )
    llm = _build_llm()
    mcp_manager = MCPManager(load_mcp_config(workdir), workdir=workdir)
    mcp_manager.start()
    for warning in mcp_manager.warnings:
        print(f"\033[33m[mcp]\033[0m {warning}")
    agent: Agent | None = None
    try:
        agent = build_agent(
            approval_provider=approval_provider,
            llm=llm,
            mcp_manager=mcp_manager,
        )
        workflow_skill_registry = SkillRegistry.for_workdir(workdir)
        workflow_config = load_workflow_config(
            workdir,
            skill_registry=workflow_skill_registry,
        )
        for warning in workflow_config.warnings:
            print(f"\033[33m[workflow config]\033[0m {warning}")
        workflow = SerialCodingWorkflow(
            llm=agent.llm,
            workdir=workdir,
            artifact_manager=ArtifactManager.for_workdir(workdir),
            approval_provider=approval_provider,
            skill_registry=workflow_skill_registry,
            worktree_manager=WorktreeManager.for_workdir(workdir),
            isolation=workflow_config.isolation,
            role_specs=workflow_config.role_specs,
            max_fix_cycles=workflow_config.max_fix_cycles,
            max_phase_retries=workflow_config.max_phase_retries,
            max_context_tokens=workflow_config.max_context_tokens,
            mcp_manager=mcp_manager,
        )
        messages = agent.new_messages()
    except Exception:
        if agent is not None and agent.background_jobs is not None:
            agent.background_jobs.shutdown()
        mcp_manager.close()
        raise

    try:
        with patch_stdout(raw=True):
            while True:
                query = read_user_prompt(prompt_session)
                if query is None:
                    break
                run_id = f"run-{uuid4().hex[:12]}"
                trace = TraceRecorder.for_run(workdir, run_id=run_id)
                try:
                    with trace_scope(
                        trace,
                        run_id=run_id,
                        agent_id=agent.agent_id,
                        parent_run_id=agent.parent_run_id,
                        depth=agent.depth,
                    ):
                        record_trace(
                            category="run",
                            name="run.input",
                            phase="received",
                            data={"content": summarize_text(query)},
                        )
                        if query.strip() == "/mcp-list":
                            print(_format_mcp_status(mcp_manager))
                            continue
                        workflow_command = _parse_workflow_cli_command(query)
                        if workflow_command is not None:
                            command, argument = workflow_command
                            if command == "list":
                                print(
                                    _format_workflow_runs(
                                        workflow.workflow_store.list_runs()
                                        if workflow.workflow_store is not None
                                        else []
                                    )
                                )
                                continue
                            if command == "show":
                                if not argument:
                                    print(
                                        "\033[33m[workflow]\033[0m "
                                        "usage: /workflow-show <workflow_id>"
                                    )
                                    continue
                                record = workflow.workflow_store.load_run(argument)
                                print(_format_workflow_record(record))
                                continue
                            if command == "resume":
                                if not argument:
                                    print(
                                        "\033[33m[workflow]\033[0m "
                                        "usage: /workflow-resume <workflow_id>"
                                    )
                                    continue
                                result = workflow.resume(
                                    argument,
                                    on_event=print_agent_event,
                                    trace=trace,
                                )
                                print(_format_workflow_result(result))
                                continue

                        workflow_request = parse_workflow_command(query)
                        if workflow_request is not None:
                            if not workflow_request:
                                print(
                                    "\033[33m[workflow]\033[0m "
                                    "usage: /workflow <request>"
                                )
                                continue
                            result = workflow.run(
                                workflow_request,
                                on_event=print_agent_event,
                                run_id=run_id,
                                trace=trace,
                            )
                            print(_format_workflow_result(result))
                            continue

                        agent.hooks.trigger_hooks(
                            "UserPromptSubmit",
                            query,
                            HookContext(
                                messages=messages,
                                workdir=workdir,
                                metadata={"run_id": run_id},
                            ),
                        )
                        messages.append({"role": "user", "content": query})
                        agent.run(
                            messages,
                            on_event=print_agent_event,
                            run_id=run_id,
                            trace=trace,
                        )
                except Exception as exc:
                    print(f"\033[31m[run failed]\033[0m {exc}")
                finally:
                    trace.render_markdown()
                    print(f"\033[2m[trace] {trace.jsonl_path}\033[0m")
    finally:
        if agent is not None and agent.background_jobs is not None:
            agent.background_jobs.shutdown()
        mcp_manager.close()


def _format_mcp_status(manager: MCPManager) -> str:
    statuses = manager.status()
    if not statuses:
        return "\033[33m[mcp]\033[0m no servers configured"
    lines = ["\n\033[36m[mcp servers]\033[0m"]
    for status in statuses:
        line = (
            f"- {status.name} state={status.state} "
            f"transport={status.transport} tools={status.tool_count} "
            f"sessions={status.session_count}"
        )
        if status.error:
            line += f" error={status.error}"
        lines.append(line)
    return "\n".join(lines)


def _format_workflow_result(result: WorkflowResult) -> str:
    color = "\033[32m" if result.status == "completed" else "\033[31m"
    reset = "\033[0m"
    artifacts = ", ".join(result.artifact_ids) or "none"
    verdict = result.qa_verdict or "unknown"
    lines = [
        f"\n{color}[workflow {result.status}]{reset} {result.workflow_id}",
        f"qa_verdict={verdict} fix_cycles={result.fix_cycles}",
        f"artifacts={artifacts}",
    ]
    if result.error:
        lines.append(f"error={result.error}")
    if result.worktree is not None:
        worktree = result.worktree.get("worktree", {})
        changed_files = result.worktree.get("changed_files", [])
        status = worktree.get("status", "unknown")
        delivery = (
            "applied"
            if status == "applied"
            else "blocked"
            if result.status != "completed"
            else "ready_to_apply"
            if changed_files
            else "no_changes"
        )
        lines.append(
            f"delivery={delivery} worktree={worktree.get('id', 'unknown')}"
        )
        if changed_files:
            lines.append(f"changed_files={', '.join(changed_files)}")
        if result.worktree.get("diff_path"):
            lines.append(f"diff_path={result.worktree['diff_path']}")
        if result.worktree.get("error"):
            lines.append(f"worktree_error={result.worktree['error']}")
    return "\n".join(lines)


def _parse_workflow_cli_command(query: str) -> tuple[str, str] | None:
    stripped = query.strip()
    commands = {
        "/workflow-list": "list",
        "/workflow-show": "show",
        "/workflow-resume": "resume",
    }
    for prefix, command in commands.items():
        if stripped == prefix:
            return command, ""
        if stripped.startswith(prefix + " "):
            return command, stripped.removeprefix(prefix).strip()
    return None


def _format_workflow_runs(records: list[WorkflowRunRecord]) -> str:
    if not records:
        return "\033[33m[workflow]\033[0m no workflow runs found"
    lines = ["\n\033[36m[workflow runs]\033[0m"]
    for record in records[:20]:
        phase = record.current_phase_key or "-"
        verdict = record.qa_verdict or "-"
        lines.append(
            f"- {record.workflow_id} status={record.status} "
            f"phase={phase} verdict={verdict} updated={record.updated_at}"
        )
    return "\n".join(lines)


def _format_workflow_record(record: WorkflowRunRecord) -> str:
    lines = [
        f"\n\033[36m[workflow]\033[0m {record.workflow_id}",
        f"status={record.status} current_phase={record.current_phase_key or '-'}",
        f"qa_verdict={record.qa_verdict or '-'} fix_cycles={record.fix_cycles}",
        f"artifacts={', '.join(record.artifact_ids) or 'none'}",
        f"worktree={record.worktree_id or '-'}",
        f"base_commit={record.worktree_base_commit or '-'}",
        f"request={record.request}",
    ]
    if record.error:
        lines.append(f"error={record.error}")
    if record.phases:
        lines.append("phases:")
        for phase in record.phases:
            lines.append(
                f"- {phase.get('phase_key')} status={phase.get('status')} "
                f"role={phase.get('role')} attempts={phase.get('attempts')}"
            )
    return "\n".join(lines)


if __name__ == "__main__":
    main()
