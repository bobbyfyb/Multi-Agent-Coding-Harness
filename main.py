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
from llm_agent.recovery import RecoveryPolicy
from llm_agent.skill_system import SkillRegistry, build_skill_catalog_section
from llm_agent.subagent import SUBAGENT_PARENT_INSTRUCTIONS, SubagentRunner
from llm_agent.tools import build_default_registry
from llm_agent.trace_system import (
    TraceRecorder,
    record_trace,
    summarize_text,
    trace_scope,
)
from llm_agent.worktree import WorktreeManager


def build_agent(
    *,
    approval_provider: ApprovalProvider | None = None,
) -> Agent:
    workdir = Path.cwd()
    llm = LLMClient(provider="anthropic")
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


def main() -> None:
    workdir = Path.cwd()
    prompt_session = build_user_prompt_session(workdir)
    approval_session = build_approval_prompt_session()
    agent = build_agent(
        approval_provider=CliApprovalProvider(
            input_func=approval_session.prompt,
        )
    )
    messages = agent.new_messages()

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
        if agent.background_jobs is not None:
            agent.background_jobs.shutdown()


if __name__ == "__main__":
    main()
