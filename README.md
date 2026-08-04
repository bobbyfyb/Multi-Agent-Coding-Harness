# LLM Agent Harness

一个支持 OpenAI / Anthropic 官方 SDK、tool calling、权限 Hook、持久化
Task System、长期 Memory、同步 Subagent、按需 Skill 加载和结构化 Trace 的
Python Agent Harness，并支持通过官方 SDK 接入外部 MCP Tools，提供可靠代码
验证、有界错误恢复和托管后台进程。
修改型 Subagent 可选择在独立 Git Worktree 中执行。

## 运行

项目默认从 `src/.env`、`src/llm_agent/.env` 或项目根目录 `.env` 加载配置。
根目录的 `.env.example` 提供了不含密钥的完整配置模板。

```dotenv
ANTHROPIC_API_KEY=...
ANTHROPIC_MODEL=...
# ANTHROPIC_BASE_URL=...
LLM_CONTEXT_WINDOW=100000
```

启动交互式 CLI：

```bash
uv run python main.py
```

CLI 使用多行编辑器：

- `Enter`：插入换行
- `Esc` 后按 `Enter`：提交完整 Prompt
- `Ctrl+C`：清空当前输入并重新开始
- `Ctrl+D`：退出
- `q`、`exit`、`/exit`、`/quit`：退出

输入历史保存在 `.llm_agent/input_history`，上下方向键可以搜索历史 Prompt。
终端输出通过 `patch_stdout` 与编辑区协调，不会覆盖尚未提交的内容。

## 同步 Subagent

主 Agent 可以通过 `subagent_run` 将范围明确的多步骤工作交给一个独立
Worker。Worker 使用全新的消息上下文，完成后只将结构化报告回灌给父 Agent。

两种工具模式：

- `explore`：只读文件、Glob、文本搜索和网络搜索
- `general`：增加 Bash、文件修改、验证工具和允许暴露给 Subagent 的 MCP Tools

子 Agent 不具备 Task 工具或 `subagent_run`，不会修改父 Agent 的任务状态，也
不能继续递归委派。文件修改和危险命令仍然经过权限 Hook。

示例输入：

```text
使用 subagent 调查 src/llm_agent 中权限检查的执行路径。
不要修改文件，列出关键类、调用顺序和相关测试。
```

程序化初始化：

```python
from pathlib import Path

from llm_agent.agent import Agent
from llm_agent.background_jobs import (
    BACKGROUND_JOB_INSTRUCTIONS,
    BackgroundJobManager,
)
from llm_agent.context_manager import ContextManager, PromptSection
from llm_agent.hooks import build_default_hook_manager
from llm_agent.hooks.permission_hooks import CliApprovalProvider
from llm_agent.llm_client import LLMClient
from llm_agent.memory_system import (
    MemoryManager,
    build_memory_policy_section,
)
from llm_agent.skill_system import SkillRegistry, build_skill_catalog_section
from llm_agent.subagent import (
    SUBAGENT_PARENT_INSTRUCTIONS,
    SubagentRunner,
)
from llm_agent.tools import build_default_registry
from llm_agent.worktree import WorktreeManager

workdir = Path.cwd()
llm = LLMClient(provider="anthropic")
approval_provider = CliApprovalProvider()
skill_registry = SkillRegistry.for_workdir(workdir)
memory_manager = MemoryManager.for_workdir(workdir, llm=llm)
worktree_manager = WorktreeManager.for_workdir(workdir)
background_jobs = BackgroundJobManager.for_workdir(workdir)
runner = SubagentRunner(
    llm=llm,
    workdir=workdir,
    max_steps=12,
    skill_registry=skill_registry,
    memory_manager=memory_manager,
    worktree_manager=worktree_manager,
    approval_provider=approval_provider,
)
registry = build_default_registry(
    workdir=workdir,
    subagent_runner=runner,
    skill_registry=skill_registry,
    memory_manager=memory_manager,
    background_manager=background_jobs,
    worktree_manager=worktree_manager,
)

agent = Agent(
    llm=llm,
    tools=registry,
    hooks=build_default_hook_manager(
        workdir=workdir,
        approval_provider=approval_provider,
        llm=llm,
        memory_manager=memory_manager,
    ),
    context_manager=ContextManager(
        llm=llm,
        workdir=workdir,
        sections=[
            PromptSection("workspace", f"Working directory: {workdir}", 20),
            PromptSection("delegation", SUBAGENT_PARENT_INSTRUCTIONS, 30),
            PromptSection("background_jobs", BACKGROUND_JOB_INSTRUCTIONS, 35),
            build_memory_policy_section(),
            build_skill_catalog_section(skill_registry),
        ]
    ),
    workdir=workdir,
    agent_id="main",
    max_steps=None,
    background_jobs=background_jobs,
)
```

`Agent.run()` 返回 `AgentRunResult`：

```python
messages = agent.new_messages()
messages.append({"role": "user", "content": "分析当前项目的权限设计"})
result = agent.run(messages)

print(result.status)
print(result.content)
print(result.steps, result.tool_calls, result.usage)
```

## Reliable Coding Tools

同步 Bash 返回结构化进程结果，而不是只返回合并文本：

```json
{
  "status": "failed",
  "exit_code": 1,
  "stdout": "...",
  "stderr": "...",
  "duration_ms": 125.4,
  "timed_out": false,
  "stdout_path": ".llm_agent/tool-results/.../stdout.log",
  "stderr_path": ".llm_agent/tool-results/.../stderr.log"
}
```

非零退出码是正常工具结果，外层仍为 `ok=true`；只有参数、路径或进程启动错误
才返回 `ok=false`。完整输出落盘，返回给模型的内容有长度限制并优先保留尾部。

文件与搜索工具：

- `read_file` 支持 `start_line/limit`，并返回完整文件的 SHA256。
- `edit_file` 要求 `old_text` 恰好出现一次，可使用 `expected_sha256`
  防止覆盖读取后发生的修改。
- `write_file/edit_file` 使用同目录临时文件进行原子替换。
- `search_text` 使用 `rg --json` 返回有上限的结构化匹配。

结构化验证工具：

```text
run_tests  -> pytest + JUnit XML
run_lint   -> Ruff JSON diagnostics
```

两者当前同步执行，使用固定参数数组而不是模型拼接的 Shell 命令。测试失败或
发现 Lint 问题会返回 `outcome=failed/issues_found`，不会触发工具异常，也不会
自动修改文件。长时间完整测试仍可使用后台 Bash。

## Worktree Isolation

修改型 Subagent 可以显式请求隔离工作区：

```json
{
  "task": "修改 app.py 并运行目标测试",
  "mode": "general",
  "isolation": "worktree",
  "task_id": "task_0001"
}
```

系统基于当前 `HEAD` 创建临时分支和 Git Worktree，子 Agent 的文件、命令、
权限检查、Context、测试和 Lint 都绑定到隔离目录。主工作区在执行期间保持
不变。

有修改时，`subagent_run` 返回：

```text
worktree id / branch / base commit
changed_files
diff preview
diff_path
```

父 Agent 使用以下工具处理结果：

```text
worktree_list
worktree_diff
worktree_apply
worktree_remove
```

`worktree_apply` 会检查主工作区 `HEAD` 仍等于创建时的 base commit，再通过
`git apply --check` 验证完整 Patch，确认后才应用到主工作区。Apply 和强制
丢弃修改都需要权限确认；有未应用修改时普通 Remove 会被拒绝。没有产生修改
的 Worktree 会在 Subagent 结束后自动清理。

MVP 要求创建时主 Git 工作区干净，不自动 Stash、Commit、Merge 或解决冲突。
Worktree 只提供代码目录隔离，不是运行不可信代码的安全沙箱。

串行 Workflow 默认也使用 Worktree 隔离，但生命周期按整个 Workflow 管理：

```text
PM planning（主工作区）
  -> 创建一个 Workflow Worktree
  -> Engineer / QA / Fix / Regression / Acceptance 共用该 Worktree
  -> 输出 changed_files 和 Patch
  -> 用户显式调用 worktree_apply
```

Artifact、Task、Skill、Workflow Record 和 Trace 保留在主工作区；代码工具、权限
边界和验证工具绑定到隔离目录。Workflow 不自动 Apply，失败时也会保留 Worktree，
以便检查或 `/workflow-resume` 继续执行。运行时 `.llm_agent` 目录不会进入 Patch。

## Background Jobs

主 Agent 的 `bash` 默认同步执行。模型只有显式传入
`run_in_background=true` 时才会启动托管后台进程：

```json
{
  "command": "uv run pytest",
  "run_in_background": true,
  "task_id": "task_0001"
}
```

调用会立即返回 `job_id`，原始 Tool Call 在此结束。进程退出后，系统会在
Agent 的下一次安全步骤中加入独立的 `<background_notifications>` 消息，不会
再次复用原始 `tool_call_id`。

后台管理工具：

```text
background_list
background_get
background_output
background_wait
background_cancel
```

Job 元数据和日志存储在：

```text
.llm_agent/background/bg_<id>/
├── job.json
├── stdout.log
└── stderr.log
```

后台进程使用独立进程组，支持超时、`SIGTERM`/`SIGKILL` 取消、并发上限和
Trace 关联。CLI 默认最多并发 4 个 Job、单个 Job 最长运行 1800 秒：

```dotenv
BACKGROUND_MAX_CONCURRENT=4
BACKGROUND_MAX_RUNTIME_SECONDS=1800
```

程序正常退出时会终止仍在运行的 Job；重启后残留的 `running` 元数据会被标记
为 `interrupted`。程序化使用时，应用退出前应调用
`background_jobs.shutdown()`。Subagent 当前仍使用同步 Bash，不会创建后台
Job。

## Trace / Observability

交互式 CLI 会为每次用户请求创建一份 Trace，并在结束时打印 JSONL 文件路径：

```text
.llm_agent/traces/<UTC-date>/<run-id>.jsonl
.llm_agent/traces/<UTC-date>/<run-id>.md
```

JSONL 是可供程序消费的事实记录，Markdown 是延迟生成的可读时间线。记录内容
包括 run 生命周期、AgentEvent、LLM metadata/usage/耗时、工具参数与结果、
权限决策、Hook 结果，以及父子 Agent 的 `run_id/parent_run_id/depth`。

`LLMClient` 会用 `operation` 区分主循环和内部调用：

```text
agent_step
task_intent
context_summary
memory_select
memory_extract
```

程序化使用：

```python
from llm_agent.trace_system import TraceConfig, TraceRecorder

run_id = "run-example"
trace = TraceRecorder.for_run(
    workdir,
    run_id=run_id,
    config=TraceConfig(
        capture_llm_content=False,
        capture_tool_content=True,
    ),
)
result = agent.run(messages, run_id=run_id, trace=trace)
print(trace.jsonl_path, trace.markdown_path)
```

默认不保存完整 LLM prompt 和原始 response，只记录长度、哈希和调用 metadata；
最终回答会作为 `final` 业务事件保留，工具内容也默认保留。所有字段写入前都会
递归脱敏和截断。Trace 写入采用 best-effort 策略，默认不会因为观测系统失败
而中断 Agent；需要强制审计时可设置 `TraceConfig(strict=True)`。

## Error Recovery

系统按照错误发生层级执行恢复：

- `LLMClient`：连接失败、超时、408、409、429 和 5xx 使用指数退避与 jitter。
- `ContextManager`：context-length 错误触发一次 reactive compact。
- `Agent`：输出截断时先提高 `max_tokens`，仍截断时最多续写两次。
- `ToolRegistry`：工具错误回灌模型，不自动重试可能产生副作用的工具。

OpenAI 和 Anthropic SDK 自带的重试会被关闭，由 `RecoveryPolicy` 统一控制，
从而避免双层重试。认证、权限、参数和响应解析错误会直接失败；重复 overload
可以选择在当前 Agent run 内切换同 provider 的 fallback model。

```python
from llm_agent.recovery import RecoveryPolicy

policy = RecoveryPolicy(
    max_retries=4,
    max_retry_elapsed_seconds=30,
    fallback_model=None,
    fallback_after_overloads=3,
    escalated_max_tokens=8_192,
    max_continuations=2,
)
llm = LLMClient(provider="anthropic", recovery_policy=policy)
```

CLI 支持以下环境变量：

```dotenv
LLM_PROVIDER=anthropic
LLM_MAX_TOKENS=4096
LLM_TIMEOUT=240
LLM_MAX_RETRIES=4
LLM_MAX_RETRY_ELAPSED_SECONDS=30
LLM_FALLBACK_MODEL=
LLM_ESCALATED_MAX_TOKENS=8192
LLM_MAX_CONTINUATIONS=2
```

恢复过程通过 `recovery` AgentEvent 输出，并写入 Trace。续写预算耗尽但仍有可用
文本时，`AgentRunResult.status` 为 `incomplete`；截断的工具调用永远不会执行。

## Context Management

`ContextManager` 统一负责初始 system prompt 和运行时历史管理：

- 使用 `PromptSection` 组装初始上下文
- 兼容 OpenAI 与 Anthropic 的工具消息格式
- 大工具结果落盘到 `.llm_agent/context/tool-results/`
- 将较旧的工具结果替换为可重新执行的占位符
- 超过上下文预算时调用 LLM 摘要旧历史
- 保留 system prompt、摘要和最近消息组
- 将压缩前历史保存到 `.llm_agent/transcripts/`
- context-length 错误时执行一次应急压缩和重试
- 将工具 schema 和临时 Task/Memory 上下文计入完整请求预算

默认使用保守的 UTF-8 字节估算，自动压缩阈值为配置窗口的 75%：

```python
context_manager = ContextManager(
    llm=llm,
    workdir=workdir,
    max_context_tokens=100_000,
    auto_compact_ratio=0.75,
    keep_recent_groups=6,
    keep_recent_tool_results=3,
)
```

模型上下文窗口由调用方显式配置，不根据模型名称硬编码。Task 和 Memory
上下文只加入当前 LLM request，不会写入 canonical history；压缩后会从各自的
持久化存储重新生成。Skill Catalog 位于保留的 system prompt 中。

也可以在 CLI 或应用边界手动调用：

```python
update = context_manager.compact(
    messages,
    run_id="manual-run",
    reason="manual",
)
messages[:] = update.messages
```

## Memory System

长期 Memory 默认存储在：

```text
.llm_agent/memory/
├── MEMORY.md
└── mem_<id>.md
```

每条 Memory 使用 Markdown 正文和 YAML frontmatter，支持四种类型：

- `user`：用户偏好
- `feedback`：长期有效的做事反馈
- `project`：稳定项目事实
- `reference`：命令、文档或外部信息入口

Memory 使用两级加载：

1. `pinned` Memory 在小预算内始终召回。
2. 其余 Memory 根据最新用户请求，由轻量 LLM side-query 选择；调用失败时
   使用 name/description 关键词匹配降级。

最多加载 5 条，单条默认限制为 4096 字符，总召回预算为 12000 字符。召回
内容以 `<relevant_memories>` 临时上下文加入请求，不会反复积累进历史，也
不会随 conversation compact 永久丢失。每条 Memory 还保存 `status`、
`confidence`、`evidence`、`last_used_at` 和 `use_count`；只自动召回 `active`
Memory，召回统计按 run 去重。

主 Agent 提供：

```text
memory_remember
memory_search
memory_get
memory_mark_stale
memory_archive
memory_restore
memory_forget
```

`memory_mark_stale` 用于有新证据推翻旧事实，`memory_archive` 是可恢复遗忘，
两者都会退出自动召回；`memory_restore` 可重新启用。`memory_forget` 是永久删除，
仍需用户确认。Stop Hook 只在用户表达“记住”“以后都”“我偏好”等明确长期
信号时尝试自动提取，并拒绝保存疑似密钥、临时日志、Task 状态和未经验证的
猜测。

主 Agent、Subagent 和 Workflow 角色共享项目级 Memory。Subagent 与 Workflow
角色只获得召回和 `memory_search / memory_get`；Workflow 仅在最终状态为
`completed` 且 QA verdict 为 `pass` 时执行一次反思，从 Artifact、结构化验证
证据和 changed files 中提取高置信度稳定事实。当前 attempt 进展不会写入长期
Memory，避免失败过程污染跨会话知识。

当前 Memory 是项目级文件存储。暂未实现用户级全局 Memory、向量索引、自动
语义冲突消解、跨进程写锁和基于时间自动归档。

## Task Intent Classification

默认 `TaskPlanningHook` 使用轻量 `TaskIntentClassifier` 判断用户请求是否需要
持久化任务计划。它直接调用一次现有 `LLMClient`：

- 不创建 Subagent
- 不运行 Agent Loop
- 不提供工具
- `max_tokens=8`
- `temperature=0`
- 按完整用户 prompt 缓存结果
- 调用失败或没有返回明确 `YES/NO` 时退回规则判断

分类目标是“是否需要多步骤计划和任务跟踪”，而不是简单判断输入是否和代码
有关。解释性问题或小型单步修改可以不创建 Task。

通过默认工厂启用：

```python
hooks = build_default_hook_manager(
    workdir=workdir,
    approval_provider=approval_provider,
    llm=llm,
)
```

如果不传 `llm`，`TaskPlanningHook` 会继续使用本地规则判断，不会额外调用模型。

## Skill System

项目 Skill 默认位于：

```text
.llm_agent/skills/<skill-name>/SKILL.md
```

系统使用两级加载：

1. 启动时只把 Skill 名称、描述和 `when_to_use` 目录放入 system prompt。
2. Agent 判断 Skill 与当前任务相关后，调用 `skill_load` 加载完整正文。

示例 Skill：

```markdown
---
name: code-review
description: Review code changes for bugs, regressions, and missing tests.
when_to_use: Use when reviewing an implementation or patch.
---

# Code Review

Inspect the complete change before reporting findings.
```

Skill 可以包含 `references/`、`scripts/` 或其他文本资源。正文中引用资源时，
Agent 使用：

```text
skill_read_resource(
  name="code-review",
  path="references/checklist.md"
)
```

资源路径始终限制在对应 Skill 目录内。Skill 内容不能覆盖 system 指令、用户
要求、workspace 边界或权限策略；Skill 中提到的脚本也不会自动执行。

主 Agent 和 Subagent 共用同一个 Skill 索引，但消息上下文保持隔离。Subagent
需要在自己的运行中重新调用 `skill_load`，不会继承父 Agent 已加载的正文。

示例输入：

```text
加载 code-review skill，检查当前代码改动并按严重程度报告问题。
```

## MCP Tools

项目保留本地核心工具，并把 MCP 作为可选的外部工具来源。启动时
`MCPManager` 使用官方 Python SDK 连接服务器、发现 Tools，并将其转换为现有
`ToolDefinition`；之后仍经过同一个 Agent loop、权限 Hook 和 TraceRecorder。

项目级配置位于：

```text
.llm_agent/mcp.yaml
```

没有该文件时不会启动 MCP 运行时，现有行为保持不变。stdio 示例：

```yaml
version: 1

servers:
  demo:
    transport: stdio
    command: python
    args:
      - "{workspace}/examples/mcp_demo_server.py"
    cwd: "{workspace}"
    workspace_scoped: true
    expose_to: [main, engineer, subagent]
    include_tools: [echo, workspace_info]
    permission: confirm
    tool_permissions:
      echo: allow
    env:
      MCP_DEMO_VALUE: "${MCP_DEMO_VALUE}"
    timeout_seconds: 30
    max_tools: 16
```

Streamable HTTP 示例：

```yaml
version: 1

servers:
  issue_tracker:
    transport: streamable_http
    url: https://mcp.example.com/mcp
    headers:
      Authorization: "Bearer ${MCP_ACCESS_TOKEN}"
    expose_to: [main, pm, engineer, qa]
    include_tools: [issue_get, issue_create]
    permission: confirm
    tool_permissions:
      issue_get: allow
      issue_create: confirm
```

`examples/mcp.yaml` 提供了已验证的 Context7 配置模板；将其内容放入
`.llm_agent/mcp.yaml` 并设置 `CONTEXT7_API_KEY` 后即可连接远程文档工具。

运行规则：

- 工具以 `mcp__<server>__<tool>` 注册，避免与本地工具或其他服务器冲突。
- `expose_to` 支持 `main`、`pm`、`engineer`、`qa`、`pm_acceptance` 和
  `subagent`；`explore` Subagent 始终不接收 MCP Tools。
- `workspace_scoped: true` 仅用于 stdio。主工作区与不同 Worktree 会建立独立
  session，`{workspace}` 可用于 `cwd`、`args` 和显式环境变量。
- 权限以本地 `permission` / `tool_permissions` 为准。MCP annotations 只作为
  展示信息，不会自动降低权限等级。
- stdio 子进程默认使用清洗后的环境，只额外注入 `env` 中显式声明的变量；
  `${NAME}` 从 Harness 进程环境解析，缺失时连接失败。
- HTTP URL 必须使用 HTTPS，只有 loopback 地址允许 HTTP；静态 headers 支持
  `${NAME}`，当前 MVP 不实现 OAuth 登录流程。
- `include_tools`、`exclude_tools` 和 `max_tools` 控制注入模型的 schema 规模。
  optional server 连接失败只打印 warning；`required: true` 会阻止系统启动。
- MCP `isError=true` 会转成普通工具错误回灌给模型，不自动重试可能产生副作用的
  调用。图片、音频和 blob 不直接进入 LLM 上下文，文本结果也有大小上限。

启动 CLI 后可查看连接、工具和 session 数量：

```text
/mcp-list
```

仓库中的 `examples/mcp_demo_server.py` 可以用于 smoke test。例如配置 demo 后输入：

```text
调用 demo MCP 的 echo 工具返回 "hello MCP"，然后读取 workspace_info 并总结。
```

MVP 只接入 MCP Tools；Resources、Prompts、Sampling、动态 tool-list 订阅和 OAuth
留给确有使用场景时扩展。

## Workflow Configuration

串行 workflow 支持项目级配置文件：

```text
.llm_agent/workflow.yaml
```

MVP 只开放隔离模式、role skill 和运行预算配置，不开放工具权限、phase 顺序或模型覆盖。
这样可以保持 PM / Engineer / QA 的安全边界稳定。
`examples/workflow.yaml` 提供了不依赖外部 Skill 的基础模板。

```yaml
version: 1

workflow:
  isolation: worktree
  max_fix_cycles: 1
  max_phase_retries: 1
  max_context_tokens: 100000

roles:
  pm:
    required_skills:
      - prd-writer
    max_steps: 12

  engineer:
    optional_skills:
      - code-review
    max_steps: 24

  qa:
    optional_skills:
      - qa-checklist
```

`required_skills` 在 role agent 启动时自动注入完整 Skill 正文；缺失会让启动失败。
`optional_skills` 只是候选增强能力，缺失时会被忽略并打印 warning，实际加载仍
通过 `skill_load` 工具调用发生。

`isolation` 支持 `worktree` 和 `shared`。CLI 默认使用 `worktree`；非 Git 目录或
兼容场景可以显式选择 `shared`。创建 Worktree 前要求主 Git 工作区干净。

## Workflow Persistence

每次 `/workflow <request>` 会写入一个可恢复 run record：

```text
.llm_agent/workflows/<workflow_id>/run.json
```

Workflow 恢复采用 checkpoint 策略，而不是完整 message replay。系统保存每个
phase 开始前的 artifact versions、Worktree Diff 基线和验证工具结果；如果进程
中断后 completion gate 已经满足，resume 会补写该 phase completed 并继续后续
阶段。否则会从该 phase 重新运行。
Run Record 同时保存 `worktree_id` 和 `worktree_base_commit`；Resume 会复用原
Worktree，若隔离目录已经丢失则明确失败，不会静默创建新目录并丢弃中间修改。

近期动作会滚动写入 checkpoint 的 `current_attempt_state`；attempt 正常结束或失败
时再固化为有界的 `data.attempt_handoffs`，记录最后状态摘要、门禁失败原因、工具
计数、近期动作与失败、结构化验证结果及 Worktree changed files。下一次 Retry
或 `/workflow-resume` 会将最新 handoff 注入角色 prompt，并要求从现有工作区继续；
进程被强制中断时也会先把滚动状态恢复成 interrupted handoff。该机制不会回放
完整历史，也不会把临时进展混入长期 Memory。每个 Workflow 角色额外保留最近
8 个完整工具结果，更早的结构化结果压缩后仍保留 outcome、路径、摘要或短预览。

## Workflow Evidence Gates

默认 `worktree` 模式不会只相信角色生成的报告。Workflow 将三类信息组合为
阶段完成证据：Artifact 是角色声明，Worktree Diff 是代码事实，`run_tests` /
`run_lint` 的结构化结果是验证事实。

- PM 的 TaskSpec 必须设置布尔值 `metadata.change_required`。
- Engineer 的 ImplementationReport 必须设置 `metadata.outcome` 和
  `metadata.changed_files`；`outcome=changed` 要求该阶段的 Diff 哈希确实变化，
  且声明文件与当前 Worktree changed files 完全一致。
- `outcome=no_change` 只允许用于 `change_required=false`，并要求提供
  `metadata.no_change_reason`。
- QA 的 `metadata.verdict=pass` 至少需要一次当前尝试中成功的 `run_tests` 或
  `run_lint`，且不能同时存在失败、超时或工具错误，也不能在 QA 阶段改变 Patch。
- 编排器把最终证据摘要注入 PM Acceptance，报告声明和运行事实冲突时以后者为准。

示例 Artifact metadata：

```json
{"change_required": true}
{"outcome": "changed", "changed_files": ["src/app.py", "tests/test_app.py"]}
{"verdict": "pass"}
```

Diff 快照和验证结果会立即写入 phase checkpoint，因此中断恢复不依赖 Trace 或
模型记忆。`shared` 模式没有独立 Diff 事实源，只保留原有 Artifact/verdict 门禁，
主要用于非 Git 目录兼容；需要完整证据链时应使用默认 `worktree` 模式。

CLI 命令：

```text
/workflow-list
/workflow-show <workflow_id>
/workflow-resume <workflow_id>
```

Artifacts 仍然是跨角色交接和恢复的主数据；Trace 负责审计和调试，workflow
record 只保存编排状态。
