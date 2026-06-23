# Task System 设计复盘与面试准备

## 1. 为什么要做 Task System

当前项目后续目标是演进成 **Multi-Agent Coding Harness**。在这个方向里，agent 不应该只靠对话上下文记住“接下来要做什么”，而应该有一套由 harness 托管的结构化任务状态。

如果只靠模型在上下文里维护 todo list，会遇到几个问题：

- 长任务中模型容易忘记前面的计划。
- 每次让模型重新生成完整列表，容易覆盖旧任务、漏任务、改错状态。
- 多 agent 场景下，不同 agent 各写各的 todo，会出现多个事实来源。
- 跨会话恢复困难，进程退出后任务状态丢失。
- QA、PM、Engineer 之间无法围绕统一任务状态协作。

因此本项目没有单独实现 TodoWrite，而是直接实现一套统一的 **Task System**：

```text
session task  = 当前单 agent 会话里的轻量执行计划
project task  = 跨会话、多 agent、带依赖的项目级任务
```

它们底层使用同一套 `TaskManager / TaskStore / Task`，避免 Todo 和 Task 两套系统状态不一致。

## 2. 总体设计

核心代码：

- `src/llm_agent/task_system.py`
- `src/llm_agent/tools/task_tools.py`
- `src/llm_agent/hooks/task_hooks.py`

架构：

```text
Agent
  |
  | BeforeLLM hook
  v
TaskPlanningHook
  |
  v
TaskManager
  |
  v
TaskStore
  |
  v
.llm_agent/tasks/<task_list_id>/<task_id>.json
```

工具调用路径：

```text
LLM tool call
  -> ToolRegistry
  -> task_create / task_update / task_list / task_get / task_claim / task_complete
  -> TaskManager
  -> TaskStore
  -> JSON file
```

状态注入路径：

```text
Agent.run()
  -> BeforeLLM hook
  -> TaskPlanningHook 读取当前任务状态
  -> 注入 <current_tasks> 或 <task_reminder>
  -> LLM 获得最新任务上下文
```

## 3. 数据模型

当前 `Task` 字段：

```python
@dataclass
class Task:
    id: str
    title: str
    description: str = ""
    status: TaskStatus = "pending"
    scope: TaskScope = "session"
    owner: str | None = None
    blocked_by: list[str] = field(default_factory=list)
    parent_id: str | None = None
    priority: int = 100
    evidence: str | None = None
    notes: str | None = None
    created_at: str = ""
    updated_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
```

### status

当前支持：

```text
pending      等待开始
in_progress  正在执行
completed    已完成
blocked      阻塞中
cancelled    已取消
```

其中 `pending / in_progress / blocked` 被视为 open task。

### scope

当前支持：

```text
session  当前会话/当前 agent 的执行计划
project  跨会话、多 agent、项目级任务
```

这也是本项目替代 TodoWrite 的关键：Todo 的语义由 `scope="session"` 表达，而不是实现另一套 todo 系统。

### blocked_by

`blocked_by` 表示当前任务依赖哪些上游任务。只有依赖任务都完成后，当前任务才能被 claim 或进入 `in_progress`。

例如：

```text
task_0001: 设计 Task schema
task_0002: 编写 Task tools，blocked_by=["task_0001"]
task_0003: 编写 Task tests，blocked_by=["task_0002"]
```

### evidence

`evidence` 用于记录完成任务的验证证据，例如：

- `uv run pytest tests/test_task_system.py passed`
- `ruff check passed`
- `manual review completed`
- `implemented in src/llm_agent/task_system.py`

这个字段对后续 QA agent 很重要，因为它可以判断任务完成是否有真实验证，而不是只听 Engineer agent 自称完成。

## 4. 持久化设计

任务文件默认存储在当前工作目录：

```text
.llm_agent/tasks/default/
  .highwatermark
  task_0001.json
  task_0002.json
```

设计点：

- 每个任务一个 JSON 文件。
- `task_list_id` 默认是 `default`，后续可以按 session/project/branch 分组。
- `.highwatermark` 保存已分配过的最大 ID。
- ID 递增生成，例如 `task_0001`、`task_0002`。
- 保存时先写 `.tmp` 文件，再 `replace` 成正式文件，降低写一半损坏的概率。

为什么不用模型生成 ID：

- 模型容易编错或重复。
- 系统生成 ID 可以保证稳定引用。
- 后续多 agent 依赖、owner、trace 都需要稳定 ID。

## 5. 工具接口

当前注册到 `ToolRegistry` 的工具：

```text
task_create
task_update
task_list
task_get
task_claim
task_complete
```

### task_create

创建任务。

常见参数：

```json
{
  "title": "Implement TraceRecorder",
  "description": "Record agent events to JSONL.",
  "scope": "session",
  "owner": "agent",
  "blocked_by": [],
  "priority": 100
}
```

使用场景：

- 用户给出复杂任务后，先创建 session task。
- PM agent 后续创建 project task。
- QA agent 创建 verification task。

### task_list

列出任务，可按 `scope`、`status`、`owner` 过滤。

常见用法：

```json
{"scope": "session", "include_completed": false}
```

### task_get

查看单个任务完整 JSON。

适合跨会话恢复时读取任务详情。

### task_update

更新任务任意字段。

常见用法：

```json
{
  "task_id": "task_0001",
  "status": "blocked",
  "notes": "Waiting for failing test details."
}
```

或者补充验证证据：

```json
{
  "task_id": "task_0001",
  "evidence": "uv run pytest passed"
}
```

### task_claim

认领 pending 任务并切换到 `in_progress`。

规则：

- 只有 `pending` task 可以 claim。
- 如果 `blocked_by` 依赖未完成，claim 会失败。
- claim 后会写入 `owner`。

### task_complete

完成 `in_progress` 任务。

规则：

- 只有 `in_progress` task 可以 complete。
- 可以附带 `evidence` 和 `notes`。
- 完成后会返回被解锁的下游任务。

## 6. 状态机

当前主要状态流：

```text
pending
  -> task_claim
  -> in_progress
  -> task_complete
  -> completed
```

也可以通过 `task_update` 标记：

```text
pending / in_progress
  -> blocked
  -> pending / in_progress

pending / in_progress / blocked
  -> cancelled
```

依赖规则：

```text
如果 task.blocked_by 中任意任务不是 completed
  -> 当前 task 不能 claim
  -> 当前 task 不能 update 到 in_progress
```

缺失依赖任务也会被视为 blocked，避免模型引用错误 ID 后直接崩溃。

## 7. TaskPlanningHook

核心文件：

- `src/llm_agent/hooks/task_hooks.py`

它接入在 `BeforeLLM` 阶段，每次调用模型前检查当前任务状态。

当前能力：

1. 如果存在 open task，注入：

```text
<current_tasks>
- task_0001 [in_progress/session] owner=agent: Implement TaskSystem
</current_tasks>
```

2. 如果最新用户输入像一个复杂 coding task，但当前没有 open task，注入：

```text
<task_reminder>
This looks like a multi-step coding task. Before changing files ...
</task_reminder>
```

3. 如果存在 open task 但没有 in-progress task，提醒先 claim 或 update 当前任务。

4. 如果任务完成但没有 evidence，且用户继续发起 coding task，提醒补验证证据。

这个机制替代了 learn-claude-code s05 里的固定 “3 轮没更新 todo 就提醒”。本项目的提醒是基于状态和行为，而不是基于固定步数。

## 8. 为什么不单独实现 TodoWrite

最终选择：

```text
不实现独立 TodoWrite。
只实现统一 Task System。
Todo 语义由 session-scope task 表达。
```

原因：

- 避免 Todo 和 Task 两套状态不一致。
- 让单 agent 和多 agent 都围绕同一个任务系统。
- 便于跨会话恢复。
- 便于后续 PM / Engineer / QA 共享任务状态。
- 面试中更容易讲成“统一任务状态机”，而不是复制 Claude Code 的历史兼容设计。

对比：

| 设计 | 优点 | 缺点 |
|---|---|---|
| TodoWrite + Task System 两套 | 贴近 Claude Code | 状态重复，复杂度高 |
| 只做 TodoWrite | 实现简单 | 不适合多 agent 和跨会话 |
| 统一 Task System | 单一事实来源，可扩展 | 初期设计稍重 |

本项目选择第三种。

## 9. 和未来多 Agent 的关系

Task System 是后续多 agent 编排的基础。

未来角色映射：

```text
PM Agent
  -> 创建 project task
  -> 拆解 blocked_by 依赖
  -> 设置 priority

Engineer Agent
  -> claim task
  -> 修改代码
  -> complete task with evidence

QA Agent
  -> 创建 verification task
  -> 执行测试
  -> 如果失败，将任务标记 blocked 或创建 defect task

Orchestrator
  -> 读取 open tasks
  -> 找出可执行任务
  -> 分配给合适 agent
  -> 根据状态推进流程
```

示例：

```text
task_0001 [project] 生成 PRD
task_0002 [project] 实现 backend API，blocked_by=task_0001
task_0003 [project] 实现 frontend UI，blocked_by=task_0001
task_0004 [project] QA 测试，blocked_by=task_0002,task_0003
task_0005 [project] PM 验收，blocked_by=task_0004
```

这样 Multi-Agent Coding Harness 不再是几个 agent 闲聊，而是围绕同一个任务图协作。

## 10. 当前使用方式

### 10.1 通过工具调用使用

默认工具注册已包含 task tools。

用户发起复杂任务后，模型应该先创建任务：

```json
{
  "title": "Add TraceRecorder",
  "description": "Record agent events to JSONL and expose a markdown summary.",
  "scope": "session",
  "owner": "agent"
}
```

然后 claim：

```json
{
  "task_id": "task_0001",
  "owner": "agent"
}
```

完成时带 evidence：

```json
{
  "task_id": "task_0001",
  "evidence": "uv run pytest passed; uv run ruff check . passed"
}
```

### 10.2 通过代码直接使用

```python
from llm_agent.task_system import TaskManager

manager = TaskManager.for_workdir("/path/to/workspace")

task = manager.create_task(
    "Implement TaskSystem",
    description="Persistent task state for the harness.",
    scope="session",
    owner="agent",
)

manager.claim_task(task.id, owner="agent")
manager.complete_task(task.id, evidence="tests passed")
```

### 10.3 查看持久化文件

任务会写入：

```text
.llm_agent/tasks/default/task_0001.json
```

示例结构：

```json
{
  "id": "task_0001",
  "title": "Implement TaskSystem",
  "description": "Persistent task state for the harness.",
  "status": "completed",
  "scope": "session",
  "owner": "agent",
  "blocked_by": [],
  "parent_id": null,
  "priority": 100,
  "evidence": "tests passed",
  "notes": null,
  "created_at": "2026-06-23T00:00:00Z",
  "updated_at": "2026-06-23T00:10:00Z",
  "metadata": {}
}
```

## 11. 当前测试覆盖

核心测试文件：

- `tests/test_task_system.py`
- `tests/test_hooks.py`
- `tests/test_basic_tools.py`

已覆盖：

- task 持久化跨 `TaskManager` 实例恢复。
- 任务 ID 从 `task_0001` 开始递增。
- 任务文件写入 `.llm_agent/tasks/default/`。
- `blocked_by` 依赖阻塞 claim。
- 上游任务完成后返回 unblocked 下游任务。
- task tools 注册与调用。
- task tool 错误由 `ToolRegistry` 包装。
- `TaskPlanningHook` 对复杂任务注入 reminder。
- `Agent.run` 在 `BeforeLLM` 阶段注入 hook message。

## 12. 当前不足与后续优化

当前不足：

- 还没有文件锁，并发多 agent 同时写任务时可能竞争。
- 还没有 DAG 环检测。
- `task_update` 可以直接设置 `completed`，后续可收紧状态流。
- 没有 release / unclaim 机制。
- 没有 task history，更新记录只保留最新状态。
- 没有 trace 集成，任务变化尚未写入执行轨迹。
- 没有 task list 分支隔离策略。

后续优化：

- 增加文件锁或原子 compare-and-swap。
- 增加 DAG cycle detection。
- 增加 `task_release`。
- 增加 task event log。
- 增加 task 与 trace 的关联。
- 增加 task 与 Artifact 的关联，例如 PRD、WorkReport、TestReport。
- 增加 orchestrator 查询 “ready tasks” 的接口。

## 13. 面试问题与参考回答

### Q1：为什么要实现 Task System？

参考回答：

> 因为 coding agent 做复杂任务时需要稳定的任务状态。如果只依赖上下文或模型自己生成 todo list，长对话容易丢计划，跨会话也不能恢复。Task System 把任务状态变成 harness 托管的结构化数据，支持持久化、依赖、owner 和 evidence，后续 PM、Engineer、QA 多 agent 都可以围绕同一个任务图协作。

### Q2：为什么不做 TodoWrite，而是统一 Task System？

参考回答：

> TodoWrite 更像当前会话里的轻量计划，Task System 更适合跨会话和多 agent。为了避免两个状态源不一致，我没有单独做 Todo，而是用 `scope=session` 的 task 表达当前会话计划，用 `scope=project` 的 task 表达项目级任务。这样单 agent 和多 agent 共用一套状态机。

### Q3：session task 和 project task 有什么区别？

参考回答：

> 它们底层都是 Task，只是 scope 不同。session task 用于当前 agent 当前会话里的执行步骤，类似 todo；project task 用于跨会话、多 agent 协作，比如 PM 拆出的需求任务、QA 验证任务。这样可以用一套工具处理不同粒度的任务。

### Q4：Task System 如何保证跨会话恢复？

参考回答：

> 每个 task 都会保存成 JSON 文件，路径是 `.llm_agent/tasks/<task_list_id>/<task_id>.json`。重新启动后，只要从同一个工作目录创建 `TaskManager`，就可以读取之前的任务状态。ID 由 `.highwatermark` 递增生成，避免任务删除后 ID 被复用。

### Q5：blocked_by 是怎么工作的？

参考回答：

> `blocked_by` 是当前任务依赖的上游任务 ID 列表。`claim_task` 和把任务更新为 `in_progress` 时都会检查依赖，如果任意依赖不存在或不是 completed，就拒绝开始。完成一个任务后，系统会扫描下游任务，返回刚刚被解锁的任务。

### Q6：owner 字段有什么用？

参考回答：

> owner 用来表示任务当前归属哪个 agent。单 agent 阶段可以是 `agent`，多 agent 阶段可以是 `pm`、`frontend_engineer`、`backend_engineer`、`qa`。后续 orchestrator 可以根据 owner 和任务状态分配工作，也可以避免多个 agent 同时做同一个任务。

### Q7：evidence 字段为什么重要？

参考回答：

> evidence 用于记录任务完成的验证依据，比如测试命令通过、lint 通过、修改了哪些文件。对 coding agent 来说，不能只让模型说“完成了”，还需要真实证据。后续 QA agent 可以基于 evidence 判断是否验收。

### Q8：TaskPlanningHook 的作用是什么？

参考回答：

> 它是任务系统和 agent loop 的连接层。每次 LLM 调用前，hook 会读取当前任务状态，如果有 open task，就注入 current_tasks；如果用户发起复杂 coding task 但还没有任务，就注入 task_reminder。这样模型每一轮都能看到最新任务状态，而不是靠自己记忆。

### Q9：为什么不使用固定 3 轮 reminder？

参考回答：

> 固定轮数是教学机制，不太适合真实 harness。真实任务中是否提醒应该由状态决定，比如没有任务却要改代码、有 open task 但没有 in_progress、完成任务没有 evidence。基于状态的提醒比固定轮数更少打扰，也更可解释。

### Q10：Task System 和 Hook 系统是什么关系？

参考回答：

> Task System 负责存储和维护任务状态，Hook 系统负责在合适时机把任务状态注入 agent 或给出提醒。Task System 本身不直接控制 agent loop；TaskPlanningHook 在 `BeforeLLM` 阶段读取 TaskManager，然后通过结构化消息影响模型下一步行为。

### Q11：Task System 和 ToolRegistry 是什么关系？

参考回答：

> Task System 的能力通过 `task_tools.py` 注册到 ToolRegistry。模型看到的是 `task_create`、`task_update`、`task_claim` 这些工具，实际执行时 ToolRegistry 调用 TaskTools，再由 TaskTools 调 TaskManager。这样 agent loop 不需要关心任务存储细节。

### Q12：当前实现有哪些工程风险？

参考回答：

> 当前 MVP 没有文件锁，所以多 agent 真并发写同一个任务时可能有竞争；也还没有 DAG 环检测，模型可能创建循环依赖；此外 task_update 目前比较灵活，后续可能需要更严格状态机。这些是下一阶段要补的工程化点。

### Q13：这个 Task System 后续如何支持多 agent？

参考回答：

> PM agent 可以创建 project-scope task 和依赖关系；Engineer agent 可以 claim 可执行任务并提交 evidence；QA agent 可以创建 verification task 或 defect task；Orchestrator 根据 status、owner、blocked_by 找出 ready tasks 并分配给不同 agent。这样多 agent 是围绕任务图协作，而不是简单聊天。

### Q14：和 Claude Code 的 Todo/Task 设计有什么不同？

参考回答：

> Claude Code 同时存在 TodoWrite 和 Task System，两者可能来自不同交互模式和历史演进。本项目是新设计，所以选择统一 Task System，Todo 语义用 session-scope task 表达。这样减少状态重复，也更符合后续 Multi-Agent Coding Harness 的需求。

### Q15：如果要继续优化，你下一步做什么？

参考回答：

> 我会优先做三件事：第一是 TraceRecorder，把 task 创建、claim、complete 和 tool call 串成可复盘轨迹；第二是文件锁和 DAG 环检测，让多 agent 并发更安全；第三是 ready task 查询和 task event log，为 orchestrator 分配任务做准备。

## 14. 简历表达示例

可以写成：

> 设计并实现统一 Task System，替代传统 TodoWrite 方案，将当前会话计划和跨会话多 agent 任务统一建模为持久化任务状态机，支持 session/project scope、blocked_by 依赖、owner 认领、evidence 验证和 BeforeLLM 状态注入，为后续 PM/Engineer/QA 多 agent 编排提供统一任务图。

简历 bullet：

- 设计 `TaskManager / TaskStore / Task`，将 agent 执行计划持久化为 `.llm_agent/tasks` 下的 JSON task 文件，支持跨会话恢复。
- 实现 `task_create / task_update / task_list / task_get / task_claim / task_complete` 工具，接入现有 ToolRegistry。
- 通过 `blocked_by` 依赖检查和 `owner` 字段支持未来多 agent 任务认领与调度。
- 实现 `TaskPlanningHook`，在 `BeforeLLM` 阶段注入当前任务摘要和基于状态的规划提醒，替代固定轮数 todo reminder。
- 通过 `evidence` 字段记录测试与验证结果，为后续 QA agent 和 PM 验收提供依据。
