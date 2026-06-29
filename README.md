# LLM Agent Harness

一个支持 OpenAI / Anthropic 官方 SDK、tool calling、权限 Hook、持久化
Task System、同步 Subagent 和按需 Skill 加载的 Python Agent Harness。

## 运行

项目默认从 `src/.env`、`src/llm_agent/.env` 或项目根目录 `.env` 加载配置。

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

## 同步 Subagent

主 Agent 可以通过 `subagent_run` 将范围明确的多步骤工作交给一个独立
Worker。Worker 使用全新的消息上下文，完成后只将结构化报告回灌给父 Agent。

两种工具模式：

- `explore`：`bash`、`read_file`、`glob`、`search`
- `general`：在 `explore` 基础上增加 `write_file`、`edit_file`

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
from llm_agent.context_manager import ContextManager, PromptSection
from llm_agent.hooks import build_default_hook_manager
from llm_agent.hooks.permission_hooks import CliApprovalProvider
from llm_agent.llm_client import LLMClient
from llm_agent.skill_system import SkillRegistry, build_skill_catalog_section
from llm_agent.subagent import (
    SUBAGENT_PARENT_INSTRUCTIONS,
    SubagentRunner,
)
from llm_agent.tools import build_default_registry

workdir = Path.cwd()
llm = LLMClient(provider="anthropic")
approval_provider = CliApprovalProvider()
skill_registry = SkillRegistry.for_workdir(workdir)
runner = SubagentRunner(
    llm=llm,
    workdir=workdir,
    max_steps=12,
    skill_registry=skill_registry,
    approval_provider=approval_provider,
)
registry = build_default_registry(
    workdir=workdir,
    subagent_runner=runner,
    skill_registry=skill_registry,
)

agent = Agent(
    llm=llm,
    tools=registry,
    hooks=build_default_hook_manager(
        workdir=workdir,
        approval_provider=approval_provider,
        llm=llm,
    ),
    context_manager=ContextManager(
        llm=llm,
        workdir=workdir,
        sections=[
            PromptSection("workspace", f"Working directory: {workdir}", 20),
            PromptSection("delegation", SUBAGENT_PARENT_INSTRUCTIONS, 30),
            build_skill_catalog_section(skill_registry),
        ]
    ),
    workdir=workdir,
    agent_id="main",
    max_steps=None,
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

模型上下文窗口由调用方显式配置，不根据模型名称硬编码。压缩完成后，
`TaskPlanningHook` 会重新注入持久化任务状态；Skill Catalog 位于保留的
system prompt 中。

也可以在 CLI 或应用边界手动调用：

```python
update = context_manager.compact(
    messages,
    run_id="manual-run",
    reason="manual",
)
messages[:] = update.messages
```

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
