# Task System 源码精读

> 核心源码：[task_system.py](../../src/llm_agent/task_system.py)  
> 模型接口：[task_tools.py](../../src/llm_agent/tools/task_tools.py)  
> 规划接入：[task_hooks.py](../../src/llm_agent/hooks/task_hooks.py)、[task_intent.py](../../src/llm_agent/task_intent.py)  
> 编排集成：[serial_workflow.py](../../src/llm_agent/serial_workflow.py)、[workflow_checkpoint.py](../../src/llm_agent/workflow_checkpoint.py)、[artifact_hooks.py](../../src/llm_agent/hooks/artifact_hooks.py)  
> 测试：[test_task_system.py](../../tests/test_task_system.py)、[test_task_intent.py](../../tests/test_task_intent.py)、[test_hooks.py](../../tests/test_hooks.py)、[test_serial_workflow.py](../../tests/test_serial_workflow.py)

## 1. 模块目标

Task System 把执行计划交给 Harness 托管，解决模型在长对话中遗忘计划、整表重写遗漏条目、任务引用不稳定，以及进程退出后状态丢失的问题。

当前没有单独维护 TodoWrite。单 Agent 计划和多角色任务都使用 Task：

```text
scope=session：本次工作相关的执行计划
scope=project：项目级、可跨会话复用的任务
```

两个 scope 都持久化。session 是用途标签，不代表进程结束后自动删除，也不自动创建独立会话列表。

Task 回答“工作是什么、由谁执行、依赖什么、目前是什么状态”；Artifact 保存交付文档；Worktree 保存真实代码；WorkflowStore 保存阶段和 attempt 恢复状态。

## 2. 源码地图与整体链路

| 组件 | 职责 |
| --- | --- |
| `Task` | 任务数据模型 |
| `TaskStore` | 每个任务一个 JSON 文件、ID 分配和列表目录 |
| `TaskManager` | 创建、状态转换、依赖检查、完成与摘要 |
| `TaskTools` | 六个模型工具的参数和输出适配 |
| `TaskPlanningHook` | 每步读取状态并提供规划提醒 |
| `TaskIntentClassifier` | 轻量 LLM 判断是否需要多步骤计划 |
| `SerialCodingWorkflow` | 固定角色阶段与 Task contract/Gate |

```text
BeforeLLM
  -> TaskPlanningHook
  -> TaskManager reads JSON tasks
  -> current_tasks + task_reminder
  -> ContextManager builds temporary request context
  -> LLM
       |
       +-> task tool call
           -> ToolRegistry validates arguments
           -> TaskTools
           -> TaskManager checks state/dependencies/evidence
           -> TaskStore writes JSON
           -> result feedback
       |
next BeforeLLM reloads latest persisted state
```

Task Hook 提供上下文和提醒，不直接创建任务或运行工具；模型仍通过 Tool Calling 执行规划操作。

## 3. 常量与类型

```python
TaskStatus = Literal["pending", "in_progress", "completed", "blocked", "cancelled"]
TaskScope = Literal["session", "project"]
OPEN_TASK_STATUSES = {"pending", "in_progress", "blocked"}
```

| 状态 | 含义 |
| --- | --- |
| pending | 等待认领 |
| in_progress | 已开始 |
| blocked | 执行过程中存在阻碍 |
| completed | 当前任务工作已完成，有 evidence |
| cancelled | 不再执行 |

blocked 状态和 blocked_by 依赖是两件事。一个 pending 任务可以因依赖未完成而不可开始；一个 blocked 任务即使依赖全部完成，也不自动变成 pending。

`TASK_SUMMARY_FIELD_CHARS=240`、`TASK_SUMMARY_MAX_CHARS=4000` 控制自动注入长度。

`TASK_SUMMARY_SECRET_PATTERNS` 为摘要中 description、notes、evidence 做常见 Secret 脱敏。它不是所有 Task 内容的存储过滤器。

## 4. `Task` 数据模型

`Task` 是可变 dataclass：Manager 从文件加载后修改，再保存。

| 字段 | 默认值/作用 |
| --- | --- |
| id | Store 分配，例如 task_0001 |
| title | 非空标题 |
| description | 可执行细节、交付和验收条件 |
| status | pending |
| scope | session |
| owner | 可选角色名称 |
| blocked_by | 前置任务 ID 列表 |
| parent_id | 可选父任务 ID |
| priority | 100；数值越小越靠前 |
| evidence | 完成依据文本 |
| notes | 进展、阻碍或补充说明 |
| created_at / updated_at | UTC，精确到秒 |
| metadata | Workflow id、task_key、role 等扩展字段 |

`to_dict()` 用 asdict；`from_dict()` 还原 status/scope、列表、priority、metadata 等字段。

`is_open` 只检查 status 是否属于 OPEN_TASK_STATUSES。

### 4.1 parent 与 dependency 的区别

parent_id 描述分解关系，blocked_by 描述执行顺序。子任务挂在 root 下，不意味着必须先完成 root；否则 root 等子任务、子任务又等 root，会形成死锁。

TaskManager 没有自动聚合子任务状态，也没有校验 parent_id 存在。Workflow 在自己的规划 Gate 中检查 required child 的 parent。

### 4.2 owner 是归属字段

owner 帮助角色交接和列表筛选，但不是认证身份或锁。工具调用者可以提供 owner；TaskManager 不验证“当前 Agent 是否为 owner”。Workflow 会额外检查 required phase task 的 owner。

## 5. `TaskStore` 与任务列表命名空间

```text
.llm_agent/tasks/
  default/
    .highwatermark
    task_0001.json
  wf-example/
    .highwatermark
    task_0001.json
    task_0002.json
```

`TaskStore.for_workdir(root, task_list_id="default")` 固定根目录；`list_dir` 为 tasks_dir / task_list_id。

task_list_id 决定真实隔离，scope 是列表内标签。不同列表可以同时有 task_0001，所以完整任务身份需要结合列表或 workflow id。

`_validate_task_list_id()` 拒绝空值、`.`、`..`、正反斜线。`_task_path()` 对 task id 拒绝斜线和点开头，避免路径逃逸；它没有强制只接受 task_数字格式。

## 6. ID 分配与文件保存

### 6.1 `next_id()`

1. 读取 .highwatermark，没有则从 0 开始。
2. 递增并写回 marker。
3. 返回 `f"task_{next_id:04d}"`。

四位只是最小展示宽度，不是最大 ID。

ID 在 create 的全部校验完成前分配，因此创建失败也可能消耗编号。允许有空洞，不影响稳定引用。

该方案在正常顺序操作下避免删除文件后复用旧 ID。但 marker 没有锁或原子事务；损坏或人为删除 marker 后也没有扫描现存文件重建计数。

### 6.2 `save()`

先把 JSON 写到同任务的 `.tmp` 文件，再 replace 到 `.json`，降低单文件半写风险。

临时文件名是固定的 task_0001.tmp，多写者并发操作同任务仍会竞争。原子替换不提供并发 claim、compare-and-swap 或多文件事务。

### 6.3 `load()` 与 `list()`

load 检查文件存在、JSON 是否 object，再调用 Task.from_dict。不存在抛 TaskNotFoundError。

list 扫描 task_*.json。它不会跳过损坏 JSON；读取错误可能影响 Task Hook 和 Workflow。

Store 不负责验证状态转换。直接 save Task 属于 trusted Python 操作，可能绕过 Manager 约束。

## 7. `create_task()`

创建过程：

1. 获取 UTC 时间和新 ID。
2. trim title/description，校验 scope。
3. 规范化 blocked_by；复制 metadata。
4. 状态固定为 pending。
5. 检查 title 非空，保存文件。

blocked_by 必须是字符串 list，空字符串被移除；当前不做 strip、去重、引用存在检查、自依赖检查或环检测。

因此 Task 数据可以表达依赖图，但尚不能保证它是合法 DAG。

## 8. `list_tasks()`

支持 scope、status、owner 过滤，结果按 `(priority, created_at, id)` 排序。

`include_completed=False` 同时排除 completed 和 cancelled，只保留 open tasks。

它没有根据依赖计算 ready 队列，也不会自动选择下一角色。priority 影响展示次序；Workflow 调度仍按固定 phase。

## 9. 状态机与 `update_task()`

允许转换以 `_ALLOWED_UPDATE_TRANSITIONS` 为准：

| 当前状态 | 可更新为 |
| --- | --- |
| pending | pending、in_progress、blocked、cancelled |
| in_progress | pending、in_progress、completed、blocked、cancelled |
| blocked | pending、in_progress、blocked、cancelled |
| completed | completed |
| cancelled | cancelled |

completed/cancelled 是终态，状态不能 reopen；标题、notes、metadata 等字段仍可更新。

### 9.1 完成与开始检查

更新到 in_progress 必须 can_start；更新到 completed 必须有非空 evidence，可使用已有 evidence 或本次新值。

pending 不能直接更新到 completed，即使提供 evidence 也拒绝。

已 completed 时不能把 evidence 设为空或纯空白。

### 9.2 部分更新语义

- None 表示不修改。
- title 不能空，description 可设空。
- blocked_by=[] 可以清除依赖。
- metadata 使用 dict.update 合并，不替换整个字典。
- owner/parent_id 不能通过 None 清除，因为 None 被当作未提供。
- 普通字段没有版本号或历史记录。

### 9.3 同时改状态和依赖的边界

update_task 先检查 status，调用 can_start(task.id) 重新读取持久化旧依赖，再修改 blocked_by。

所以一次调用同时 `status="in_progress"` 和修改 blocked_by，检查的是旧依赖而非最终依赖。新增未完成依赖时可能通过开始检查；移除旧阻塞时也可能先被旧依赖拒绝。

此外 in_progress 后可以单独改依赖，complete 不重新检查依赖。这些是当前 Manager 的一致性边界，文档记录现状。

## 10. `can_start()` 与 `claim_task()`

can_start 加载当前任务，对每个 blocked_by 要求：

```text
依赖存在 AND dependency.status == completed
```

缺失依赖返回 False；cancelled 上游也不满足。这里只检查依赖，不检查当前任务是不是 pending。

claim_task 的执行顺序：

1. 加载任务。
2. 要求 status=pending。
3. 要求 can_start。
4. 写 owner、in_progress、updated_at。
5. 保存。

不能 claim blocked；应先明确更新为 pending，或 update 到 in_progress。第二次顺序 claim 会因状态变化失败，但两并发 claim 仍可能同时读到 pending，因此不能宣称 exactly-once 认领。

## 11. `complete_task()` 与下游解锁

complete_task 要求 in_progress 和非空 evidence，保存 completed，返回 `(completed_task, unblocked_tasks)`。

下游查询条件：

```python
candidate 是 open task
and completed_task.id in candidate.blocked_by
and can_start(candidate.id)
```

它只是报告依赖已满足的相关任务，不更改其 status、不认领、不自动派发。候选不限 pending，也可能是 blocked 或 in_progress。

通过 update_task(status=completed) 完成不会返回下游解锁信息；需要这一结果时用 complete_task。

### 11.1 evidence 的强度

Manager 只检查文本非空，不验证命令确实运行或代码确实存在。

例如 `evidence="done"` 可以通过 Manager，但不保证通过工程验收。Workflow 在外层结合真实 tool event、Worktree diff、Artifact 和验证结果做 Evidence Gate。

QA Task completed 表示“QA 已执行并产出结论”，不代表结论一定 pass。QA 完成发现缺陷的工作后，Workflow 可以创建 Engineer fix task。

## 12. 摘要与格式化

`summary()` 是 BeforeLLM 的有界状态视图：

- 默认取前 12 个 open tasks。
- 每条显示 ID、status/scope、owner、blocked_by、title。
- description、notes、evidence 各最多 240 字符。
- 总长度最多 4000 字符，截断带 marker。
- 额外任务显示 more tasks 计数。

`_task_summary_preview()` 先脱敏，再合并空白和截断。敏感文本不会因为只截掉一部分而原样暴露在这些字段里。

但 title、owner、dependency 文本未经过同样脱敏；原 JSON 和 format_task 的完整输出也没有此处的过滤。

`format_task()` 返回完整 JSON，`format_task_list()` 返回紧凑列表。后者的 evidence=yes 仅依据字符串真值，不等于验收成功。

## 13. `TaskTools` 六个工具

| 工具 | Manager | 模型可见结果 |
| --- | --- | --- |
| task_create | create_task | Created task + 完整 JSON |
| task_get | get_task | 完整 JSON |
| task_list | list_tasks | 紧凑状态列表 |
| task_update | update_task | Updated task + 完整 JSON |
| task_claim | claim_task | Claimed task + 完整 JSON |
| task_complete | complete_task | Completed task + 下游解锁列表 |

task_tool_definitions 绑定一个 TaskManager，register_tools 注册到 ToolRegistry。

task_complete 的模型 schema 要求同时提供 task_id 和 evidence；Python Manager 可在已有 evidence 存在时省略 evidence。这是工具约定比内部 API 更明确的例子。

TaskTools 不实现业务状态机，错误由 TaskSystemError 抛到 ToolRegistry，再包装为 ok=False/error 回灌模型。

这些工具没有 requires_context，不自动读取调用 Agent 的真实身份；owner 是模型参数或默认 agent。

## 14. 默认注册与 Worktree 路径

register_default_tools 无条件注册 Task tools。`task_workdir` 优先于文件工具的 workdir：

```text
file/verification tools -> execution Worktree
task tools -> root project/.llm_agent/tasks/<workflow_id>
```

TaskPlanningHook 也使用相同 root/task_list_id。这样多个角色虽然在 Worktree 内操作代码，任务状态仍保持同一个事实来源。

Main Agent 默认 task_list_id=default。没有按每个用户 turn 自动创建列表，因此早先未完成任务可能继续注入后续请求。

## 15. `TaskPlanningHook`

字段控制 summary 注入、复杂任务提醒、路径/list id 和 intent classifier。每次 BeforeLLM：

1. context_compacted=True 时清空 reminder 去重键。
2. 新建指向相同文件列表的 TaskManager。
3. 读取 summary，包装 current_tasks。
4. 根据真实任务状态计算 reminder。
5. 用 `(latest_user_text, reminder)` 去重。
6. 返回 HookResult.allow(data={messages: ...})。

摘要每次都会重新注入，只有 reminder 去重。临时 runtime context 由 ContextManager 插入请求副本，canonical history 不不断累加相同 Task 状态。

### 15.1 提醒优先级

_build_reminder 按顺序判断：

1. 没有 open task 且复杂：提醒先创建 session tasks。
2. 有 open task、没有 in_progress：提醒 claim/update。
3. 没有 open task、已有 completed 缺 evidence、请求复杂：提醒补 evidence。

第三分支对正常 Manager 创建的数据已较少有用，因为新状态机禁止无 evidence 完成。默认启用复杂任务提醒时，第一分支对同样条件更早返回，也可能遮住第三分支。不能把它描述为总会触发的验收保护。

### 15.2 提醒不是强制执行门禁

Hook 不拦截 write_file/bash，不证明模型先规划，也不要求全部 open tasks 完成后才能 final。

Workflow 中任务完成要求由 Gate 强制；Main Agent 的规划主要依靠提示与工具状态约束。

### 15.3 列表粒度的边界

Hook 不按当前角色 owner 过滤 in_progress。Workflow root 由 orchestrator 长期保持 in_progress，因此“列表没有 in_progress”的通用提醒不能保证每个 worker 已 claim 自己的任务；phase task contract/Gate 补足这一约束。

内部用户 prefix 列表未覆盖 recovery continuation/background notification，可能误把内部消息当作分类输入；与 Memory Hook 的前缀维护问题相同。

## 16. `TaskIntentClassifier`

它判断的是“是否值得使用多步骤持久计划”，不只是“是否包含 coding 单词”。解释、问候和简单一步修改在 Prompt 中要求 NO。

requires_plan：

1. trim 文本，空输入 False。
2. 查 exact-text 运行内 cache。
3. LLM call：tools=None、max_tokens=8、temperature=0。
4. _parse_decision 只接受 YES/NO，可有末尾 `.` 或 `!`，忽略大小写和边缘空白。
5. 调用异常或结果非法时规则降级。
6. 缓存最终布尔值。

规则 fallback：长度至少 80 字符，或命中 implement/fix/test/实现/修改/测试等子串，返回 True。

规则不使用词边界，也不真正识别复杂程度；长解释问题也可能被归为需要规划。

辅助请求使用 trace_operation(task_intent)，经过现有 LLMClient recovery。轻量指输出和调用结构轻量，网络故障时仍可能等待正常 retry budget。

默认 HookManager 在有 llm 时创建 classifier。Hook 实例不共享全局 cache；Workflow 新角色 attempt 新建 Hook，分类缓存也重新开始。

## 17. Workflow 的真实任务结构

当前 Workflow root 和 required child 都使用 session scope，隔离来自 workflow_id task list：

```text
root: workflow_root [in_progress, owner=workflow-orchestrator]
  |
  +-- engineer_implement [pending, owner=engineer]
  +-- qa_verify [pending, owner=qa, blocked_by=engineer]
  +-- pm_acceptance [pending, owner=pm-acceptance, blocked_by=qa]
```

root 是父关系，不是上游依赖。

### 17.1 root 创建与恢复

_ensure_workflow_root_task 按 metadata.workflow_id/task_key 查找，重复 key 报错。

- 不存在：Harness 创建并 claim。
- blocked：resume 更新回 in_progress。
- cancelled：拒绝恢复。
- completed：保留终态。

### 17.2 PM planning contract

PM 要 task_list 找 root，创建三个 pending children，并在 TaskSpec metadata.task_ids 中写真实 ID。

_gate_planning_tasks 检查：

- root 存在。
- TaskSpec 映射覆盖三个 key，引用真实 Task。
- 同 workflow/task_key 唯一。
- workflow_id、task_key、role metadata 正确。
- parent_id、owner 正确。
- children 仍为 pending。
- blocked_by 与固定依赖精确一致。

PM retry 应复用已有 child，不重复创建。

### 17.3 worker phase contract 与 Gate

_format_phase_task_contract 将具体 Task ID 和状态注入角色 prompt：pending 则 claim，in_progress 则继续，completed 则补剩余 Artifact/最终回复，不 reopen。

_gate_phase_task 要求真实 owner、completed 和非空 evidence。它与 Artifact 和代码验证 Gate 共同决定阶段通过。

Task 文件持久化不等于 phase 已成功。角色可能完成 Task 却漏交 Artifact，仍需 continuation。

## 18. QA fail 与修复循环

QA 检测完缺陷，可以完成 qa_verify Task，报告 verdict=fail。随后 Harness 创建新任务：

```text
qa_verify completed (verdict fail)
   -> engineer_fix_1
   -> qa_regression_1
   -> engineer_fix_2 ...（预算内）
   -> pm_acceptance depends on latest QA task
```

_ensure_fix_cycle_tasks 设置 parent=root、role、task_key 和真实前序依赖，重复调用复用已有 Task。

_retarget_acceptance_task 将最终验收依赖改到最新 QA regression。Workflow 对 qa_verdict 的检查确保不会只因 QA Task completed 就当作验收通过。

_close_workflow_root_task：最终成功时完成 root，并记录 workflow/QA evidence；失败或未完成时把 root 标 blocked，保留错误 notes 供恢复。

## 19. 与 Artifact、Memory、Subagent 和后台任务协作

- Artifact 的 task_id 可关联交付物；ArtifactContextHook 优先选择关联 open tasks 的 Artifact。
- TaskSpec 的 metadata.task_ids 在 Workflow 规划阶段建立明确交接引用。
- AttemptCheckpoint 保存 open_tasks，retry/resume 可以恢复目标和下一步。
- Task evidence/notes 是任务状态；长期 Memory 只提炼稳定经验，避免持久化过期进度。
- Subagent 的 task_id 用于结果、Trace 或 Worktree 关联，不会自动 claim/complete Task。
- Background job 的 task_id 也是关联字段，后台进程完成不自动把 Task 标 completed。

这些关联不等于统一的动态任务调度器。Workflow 顺序仍由确定性 phase 状态机决定。

## 20. 两条完整使用链路

### 20.1 单 Agent

```python
manager = TaskManager.for_workdir("/path/to/project")
implement = manager.create_task("实现健康检查", scope="session")
verify = manager.create_task("补充并运行测试", blocked_by=[implement.id])

manager.claim_task(implement.id, owner="agent")
manager.complete_task(implement.id, evidence="Added health endpoint; reviewed diff")
manager.claim_task(verify.id, owner="agent")
manager.complete_task(verify.id, evidence="Focused health tests passed")
```

模型实际使用时发出对应 task_create/claim/complete，下一步 BeforeLLM 读取最新 JSON 状态。

### 20.2 Workflow

1. Harness 创建 root。
2. PM 创建三个 role tasks 和 PRD/TaskSpec。
3. planning Gate 校验真实任务链。
4. Engineer claim 实现任务，写代码，验证并完成 Task，交 implementation_report。
5. QA claim 验证任务，产生 TestReport 和 evidence。
6. fail 则 Harness 增加 fix/regression tasks；pass 才推进 acceptance。
7. PM acceptance 完成验收 Task；Harness 完成 root。
8. 中断后根据 Workflow checkpoint、Task 文件和同一 Worktree resume。

## 21. 与 learn-claude-code s05/s12 对比

| 维度 | 教学方案 | 当前项目 |
| --- | --- | --- |
| Todo 更新 | s05 整体生成新 list | 每个 task_id 增量操作 |
| 提醒 | s05 固定若干轮提醒 | 状态 + intent + prompt 去重 |
| 存储 | s12 .tasks JSON | root/.llm_agent/tasks/list-id JSON |
| ID | s12 时间戳加随机数 | 列表内 highwatermark 递增 |
| 状态 | s12 pending/in_progress/completed | 加 blocked/cancelled 和转换表 |
| 完成依据 | s12 不强制 evidence | Manager 强制非空，Workflow 加真实 Evidence Gate |
| 认领 | s12 状态与依赖检查 | 类似检查，owner 与 Workflow contract 结合 |
| 解锁输出 | s12 扫描满足依赖的 pending 任务 | 只报告引用本次 completed Task 且依赖满足的 open tasks |
| 协作 | 示例数据模型 | PM 创建、worker 完成、修复与 acceptance 依赖重定向 |
| 恢复 | 任务文件跨运行 | 额外 phase checkpoint + Worktree + Artifact |

当前与 s12 一样没有并发原子认领或完整 DAG 检测。文件保存多了单文件 replace，不能据此称为分布式任务系统。

## 22. 设计考量

### 为什么统一 Todo 与 Task

两套系统容易出现“todo 已完成，project task 仍 pending”。统一 API、状态和持久化后，scope 只承担语义标签，角色协作使用真实 list namespace。

### 为什么分 Manager 和 Tools

Manager 保留核心规则，Hook、Workflow、测试都可直接调用。Tools 只服务模型协议，减少入口间规则重复。

### 为什么先固定 Workflow，再考虑动态 DAG 调度

当前固定 PM/Engineer/QA 便于验证交接、代码证据和恢复。动态 ready-task 派发还需要并发锁、租约、owner 验证、失败传播和任务图校验，不能只加一个循环就可靠运行。

### 为什么不根据 evidence 自动相信 completed

evidence 是必要的说明，但模型能生成任意文本。真实可信度来自验证事件、代码 diff 和 Gate；Task 本身只负责执行状态。

### 为什么不自动 reopen completed

保留已完成阶段的证据；修复创建新 task_key，明确关联 QA 问题和新的 verification。这样阶段历史不会被简单覆盖成“原任务又没完成”。

## 23. 当前限制

1. 无并发锁、CAS 或 claim lease，不能保证并发唯一认领。
2. highwatermark 非事务写，固定 tmp 文件会竞争。
3. 无 DAG/self-cycle 检查，缺失依赖永久阻塞直到修正。
4. owner 不是身份鉴权，不能阻止其他 Agent 更新任务。
5. 同一次 update 改状态与依赖时检查旧依赖。
6. complete 不重新检查动态变更的依赖。
7. evidence 只要求非空，真实性依赖外层 Gate。
8. metadata merge 没有删除键接口；owner/parent 无明确清空语义。
9. session scope 不自动过期，default 列表可能积累旧任务。
10. 无完整 task history/domain event log；模型操作仍可由 Tool Trace 追踪。
11. 列表读取损坏文件会传播失败。
12. 规则 intent fallback 和内部消息识别仍有误判边界。
13. 摘要只保护部分字段，不保护所有存储内容。
14. 无 task_delete/release 专用工具；可用 in_progress -> pending 表达退回，但 owner 不自动释放。

当前设计适合本地串行 Harness。并行 Agent Team 需要优先补图校验和并发一致性，而不是仅增加 worker 数量。

## 24. 测试与阅读顺序

test_task_system 覆盖持久化、依赖阻塞、完成后解锁、状态转换、evidence、摘要脱敏/截断、工具错误和列表隔离。

test_task_intent 覆盖轻量请求参数、缓存、NO、非法响应/异常 fallback 和规则判断。

test_hooks 覆盖规划提醒、内部消息过滤、prompt 去重、默认装配、压缩后重注入和临时 request context。

test_serial_workflow 覆盖每 workflow 独立列表、规划任务图 Gate、角色任务完成、修复与 resume。

建议按以下顺序打开源码：

1. Task 与状态转换表。
2. TaskStore 的 list_dir、next_id、save/load。
3. create/list/update/claim/complete/can_start。
4. summary 和辅助校验/格式化函数。
5. TaskTools 与默认 registry 注册。
6. TaskIntentClassifier 与 TaskPlanningHook。
7. Workflow root、planning Gate、phase contract/Gate。
8. 修复任务和 acceptance 重定向。
9. checkpoint 与相关测试。

## 25. 面试追问

### session task 重启后会消失吗？

不会。两种 scope 都写 JSON。实际 namespace 是 task_list_id；session 当前没有自动清理机制。

### 依赖关系等于 DAG 吗？

数据可以表示有向图，但目前没有环检测，不能保证 DAG。can_start 只检查直接依赖完成情况。

### 认领是否保证只有一个 Agent 成功？

顺序调用时第二个 claim 会被状态拒绝；并发读改写没有锁，可能双重成功。并行拓展应加原子 claim/lease。

### completed 是否等于需求验收通过？

它表示任务工作完成且有 evidence。QA 执行完检查可 completed 并给 fail verdict，Workflow 再开启修复。需求通过由 QA/PM 和 Evidence Gate 决定。

### 为什么 Task 与 WorkflowStore 都要保存状态？

Task 是工作控制面和角色交接；WorkflowStore 保存 phase/attempt、Gate、checkpoint 和恢复游标。二者职责不同，通过 workflow/task_key 关联。

### 模型不创建 Task 怎么办？

Main Agent 会有 planning reminder，但当前不是硬门禁。Workflow 则要求真实 Task graph 和 phase completion，否则 Gate 不通过并给 corrective prompt。

### 优先级如何影响调度？

TaskManager 用 priority 排序展示，当前串行 Workflow 不根据它动态派发。不能把排序描述为完整调度能力。

### 如何证明比整表 Todo 稳定？

每次操作指定稳定 ID，规则由 Harness 检查，旧条目不会因模型重写全表被遗漏。仍需评测重复任务、非法转换、resume 重复动作和完成率，而不是仅凭接口推断效果。
