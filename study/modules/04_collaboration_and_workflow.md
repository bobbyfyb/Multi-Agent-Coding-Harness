# Task、Artifact、Subagent、Worktree 与串行 Workflow

## 1. 协作层要解决什么

单 Agent 可以在一个消息列表里临时记住计划，但多角色、长任务和进程恢复需要结构化协议：

- 谁负责当前工作；
- 前一角色交付了什么；
- 文件实际变成了什么；
- 验证是否真的成功；
- 中断后从哪个阶段继续；
- 失败重试时如何避免从头开始。

项目将这些问题拆到不同组件，而不是让一个 Orchestrator prompt 自己记住全部状态。

核心文件：

- `src/llm_agent/task_system.py`
- `src/llm_agent/artifact_system.py`
- `src/llm_agent/subagent.py`
- `src/llm_agent/worktree.py`
- `src/llm_agent/workflow_store.py`
- `src/llm_agent/serial_workflow.py`
- `src/llm_agent/workflow_config.py`

## 2. Task 是控制面

Task 表达“工作状态”，包括：

- title、description；
- pending/in_progress/completed/blocked/cancelled；
- session/project scope；
- owner；
- `blocked_by` 依赖；
- parent、priority、evidence、notes。

### 2.1 为什么不再做一套 Todo

模型每次重写完整 Todo list 容易丢项和覆盖旧状态。项目使用一套 Task System：

- `scope=session` 表达当前 Agent 的轻量计划；
- `scope=project` 表达跨会话和多角色任务。

更新是按 task id 的增量操作，状态持久化，不让模型每轮重建整个事实源。

### 2.2 为什么有 Store、Manager、Tools 三层

- `TaskStore`：JSON 文件 I/O、id 和序列化。
- `TaskManager`：状态迁移、依赖、claim/complete 规则。
- `task_* tools`：LLM schema 和参数适配。

如果把逻辑全放在 Tool 内，CLI、Hook、Workflow 或未来 API 想复用时必须伪造 Tool Call；业务规则也会和 LLM schema 混在一起。

完整设计见 [`../task_system_design.md`](../task_system_design.md)。

## 3. Artifact 是数据面

### 3.1 Artifact 类型

```text
prd
task_spec
implementation_report
test_report
acceptance_report
note
```

每个 Artifact 包含 kind、title、content、status、task_id、owner、version、metadata 和 history。

### 3.2 Task 与 Artifact 为什么都需要

```text
Task:     Engineer 正在实现登录接口，状态 in_progress
Artifact: 具体接口约束、改动说明、测试证据和验收结论
```

只用 Task 会把长文档塞进状态字段；只用 Artifact 又难以查询开放任务、依赖和 owner。两者通过 `task_id` / workflow metadata 关联。

### 3.3 乐观并发控制

Artifact update 要先读取当前版本，并传 `expected_version`：

```text
read version=2
  -> update(expected_version=2)
  -> success, version=3
```

若另一个角色已经更新为 version=3，旧更新失败，避免静默覆盖。当前 Workflow 串行，但该规则仍能防止模型基于旧内容修改，也为未来并行协作保留契约。

### 3.4 ArtifactContextHook

Hook 会按当前 task/workflow 选择相关 Artifact，以 runtime message 注入。不会把所有 Artifact 永久追加进 conversation。

Artifact 是交接协议，但不是最终事实：changed files 仍以 Git diff 为准，测试 verdict 仍以 verification event 为准。

## 4. Subagent

### 4.1 定位

Subagent 用于“把一个边界清楚的子问题交给隔离上下文的 Worker”，例如：

- Explore 仓库并返回相关文件；
- 调研一个实现方案；
- 完成局部、明确的编码任务。

它不是 Workflow 状态机，也不维护跨阶段业务流程。

### 4.2 运行方式

`SubagentRunner`：

1. 校验请求、mode 和 depth；
2. 根据 mode 选择工具 allowlist；
3. 可选创建独立 Worktree；
4. 创建新的 Agent、ContextManager 和 messages；
5. 同步运行到 final/max steps/error；
6. 返回结构化 `SubagentResult` 给父 Agent；
7. 在 Trace 中建立 parent/child run 关系。

父 Agent 不接收子 Agent 全部对话，只接收目标、摘要、状态和必要结果，从而隔离 token 与错误上下文。

### 4.3 Explore 与 General

- Explore 只获得 read/glob/search 等只读能力。
- General 可按设计获得更多工具，但仍经过 Permission/Security。

只读由 Registry subset 保证，而不是完全依赖 prompt。

### 4.4 为什么当前同步

同步 MVP 简化：

- 父子结果时序；
- 取消和超时；
- Trace 父子关系；
- Worktree 生命周期；
- 多个 Worker 同时写代码的冲突。

只有评测证明并行能显著降低关键路径延迟时，才值得引入 async scheduler。

## 5. Git Worktree 隔离

### 5.1 生命周期

```text
create(base HEAD)
  -> Agent 在隔离路径读写和验证
  -> diff() 获取真实 changed files/diff hash
  -> finish() stage 并生成 patch
  -> 用户审查
  -> apply() 检查 base 未过期且 patch 可应用
  -> remove()
```

状态保存在 `.llm_agent`，项目运行产物不会计入候选 patch。

### 5.2 为什么要求主工作区干净

若创建 Worktree 时主工作区已有未提交改动，很难判断 base、用户修改和 Agent 修改的归属。默认要求 clean 能避免把用户未提交内容漏进或覆盖。

### 5.3 为什么 Workflow 共用一个 Worktree

串行流程需要：

- Engineer 的代码立即给 QA 验证；
- QA 的失败基于同一文件状态；
- 修复轮继续上一次 diff；
- PM 验收看到最终候选。

角色各自 Worktree 会引入每阶段 merge，违背最小串行编排的目标。未来并行实现不同方案时，再为并行分支各建 Worktree。

### 5.4 为什么显式 apply

Workflow 成功不等于用户自动接受代码。最终 patch 与主工作区合并是一个独立决策：

- 检查 main HEAD 是否仍是原 base；
- 检查 patch 是否可应用；
- 由用户显式触发；
- 失败时保留 Worktree 供审查。

## 6. SerialCodingWorkflow

### 6.1 Orchestrator-Worker

`SerialCodingWorkflow` 是确定性 Orchestrator：

- 决定角色顺序；
- 构造 phase prompt；
- 分配工具和 Skill；
- 设置 step/retry/fix budget；
- 检查 Artifact 和机器证据；
- 持久化 checkpoint；
- 决定继续、修复、验收或失败。

PM、Engineer、QA 是 Worker，只负责阶段任务。它们不拥有流程全局控制权。

### 6.2 角色能力

| 角色 | 主要工具 | 明确限制 |
|---|---|---|
| PM | read/search/task/artifact/memory/skill | 不改代码 |
| Engineer | read/write/edit/bash/test/task/artifact | 只在 Workflow Worktree 工作 |
| QA | read/search/run_tests/run_lint/artifact | 不写源码、不用任意 bash |
| PM Acceptance | read/artifact/相关外部工具 | 不能覆盖权威 QA 结论 |

MCP tools 也按 `main/pm/engineer/qa/pm_acceptance/subagent` scope 暴露，不会因为连接了一个 server 就自动给所有角色。

### 6.3 阶段状态机

```text
pm_plan
  -> engineer_implement
  -> qa_verify
      -> pass -> pm_acceptance
      -> fail -> engineer_fix -> qa_regression
                    ^                 |
                    |------ fail -----|  (bounded)
```

每个 phase 有独立 run id、attempt、artifact delta、checkpoint 和 evidence。`max_phase_retries` 处理同一阶段执行失败；`max_fix_cycles` 控制代码修复反馈环，不混为一个计数器。

### 6.4 配置文件化

`.llm_agent/workflow.yaml` 允许配置：

- isolation：`worktree` 或 `shared`；
- max_fix_cycles；
- max_phase_retries；
- max_context_tokens；
- 各角色 required/optional skills；
- 各角色 max_steps。

工具权限不允许通过普通配置任意扩大，仍由代码中的 RoleSpec 基线控制。配置是运营参数，不是安全策略逃生口。

## 7. Checkpoint 与 Resume

### 7.1 WorkflowStore

`WorkflowRunRecord` 持久化：

- workflow id、request、status；
- 当前 phase；
- phase results；
- Artifact ids；
- Worktree id/path/base；
- fix cycle；
- phase checkpoint；
- error 和最终状态。

写入采用临时文件替换，避免进程中断留下半个 JSON。

### 7.2 Phase checkpoint

每个阶段开始先保存 `started`，成功后保存 `completed`，失败则保存错误和可恢复数据。resume 时：

- 已完成 phase 不重复执行；
- started 但未完成的 phase 视为中断尝试；
- Worktree 和 Artifact 从持久化 id 恢复；
- 从正确 phase 继续，而不是重跑整个 Workflow。

### 7.3 Attempt handoff

新 Agent 实例没有旧 attempt 的完整 messages。为了避免重复探索，checkpoint 保存最多若干条 handoff：

- recent actions；
- progress summary；
- tool/verification failures；
- gate issue；
- Worktree changed files；
- 尚待处理的 next step。

下一 attempt 通过 `<working_state>` 获取有界状态。为什么不直接塞完整 Trace：Trace 过长、包含大量 UI/底层事件，也没有为模型续接组织语义。

## 8. Evidence Gate

### 8.1 规划 Gate

PM 必须在当前 phase 创建或更新：

- ready/accepted PRD；
- ready/accepted TaskSpec；
- `TaskSpec.metadata.change_required` 必须是 boolean。

`change_required` 消除“这个任务到底应不应该产生代码 diff”的歧义。

### 8.2 实现 Gate

ImplementationReport 必须声明：

- `outcome=changed | no_change | blocked`；
- changed 时应有真实 phase-local Worktree diff；
- no_change 时 TaskSpec 必须允许，且提供原因；
- blocked 直接作为阶段失败；
- 报告的 changed_files 与真实 Worktree 不同时由 Harness 自动校正。

这里使用 phase 开始前后的 diff SHA，而不只看“Worktree 最终有改动”。否则第二次 Engineer attempt 可以拿第一次遗留的 diff 冒充本轮完成。

### 8.3 QA Gate

TestReport 必须有 pass/fail verdict。pass 还要求：

- 当前 QA phase 有成功 verification event；
- 没有更新、更权威的失败结果；
- 没有 environment setup blocker；
- QA 没有修改源码；
- 测试针对当前 Worktree 状态。

模型写 pass 但命令失败时，Harness 将其降级为 fail。

### 8.4 Acceptance Gate

AcceptanceReport 的 metadata verdict 与 status 必须一致，并且不能与权威 QA 状态冲突：

- QA pass 才可能 accepted；
- QA fail 时 PM 不能把 Artifact 标为 accepted；
- 缺少必需 Artifact 或证据时 Workflow 失败。

### 8.5 为什么 Gate 写在 Orchestrator

这些规则跨越 Agent、Artifact、Trace verification 和 Worktree，单个 Tool 看不到完整阶段边界。放在 Orchestrator 能使用 phase-local state 做裁决，也避免让 Worker 自己决定是否通过。

## 9. 一个完整示例

用户运行：

```text
/workflow 为当前 Python 项目新增 /health API，并补充测试和 README
```

可能的执行链：

1. PM 读取仓库，创建 PRD 与 `change_required=true` 的 TaskSpec。
2. Gate 通过后创建 `workflow-xxxx` Worktree。
3. Engineer 在 Worktree 编辑 API、测试、依赖和 README，运行测试，创建 `outcome=changed` 的 ImplementationReport。
4. Harness 比较 phase 前后 diff，修正报告中的 changed files。
5. QA 只有只读和 verifier 权限，执行项目隔离环境中的 test/lint，创建 TestReport。
6. 若失败，错误类型和最近改动写入 attempt handoff，Engineer 在同一 Worktree 修复。
7. QA 回归通过后，PM 创建 AcceptanceReport。
8. Workflow 保存最终 patch 和 Trace；代码仍未自动进入主工作区。
9. 用户审查后显式 apply。

任何阶段进程退出，都可以通过 `/workflow-resume <id>` 从 checkpoint 继续。

## 10. 设计评价

### 优点

- 控制状态、交付内容、真实代码和机器证据分离。
- Role Agent 简单，复杂度集中在可测试的 Orchestrator 状态机。
- Worktree + Gate 能识别“说完成但没完成”。
- checkpoint 和 handoff 同时覆盖跨进程恢复与重试连续性。

### 局限

- 串行状态机代码较长，Artifact metadata 尚未全部强类型化。
- 本地 JSON Store 不支持多进程并发 claim 和锁。
- Worktree shared fallback 模式的证据强度弱于隔离模式。
- 当前只支持一个固定 PM/Engineer/QA 拓扑。
- 模型质量差时，多轮修复仍可能耗费大量 token 后失败。

## 11. 面试追问

**SubagentRunner、TaskManager、HookManager 定位类似吗？**

它们都封装一个领域，但层级不同：TaskManager 管领域状态，HookManager 管生命周期扩展，SubagentRunner 管一次子 Agent 的资源装配和执行。共同点是把复杂度从 Agent Loop 移出，区别在于管理对象不同。

**Artifact 是否就是 Agent 间通信协议？**

可以这样理解，但不止通信。Artifact 还是持久化、版本化、可验收的交付协议；实时消息只是传输，Artifact 才是可恢复的数据产品。

**为什么不让 LLM 自由决定下一角色？**

确定性状态机更可测试、可恢复，也能保证 QA 和 Evidence Gate 不被跳过。LLM 适合阶段内开放决策，Orchestrator 适合安全和流程约束。

**如何扩展到并行 Agent Team？**

需要增加任务 claim/lease、每分支 Worktree、冲突合并、并发 Artifact 版本处理、取消传播、预算调度和聚合 Gate。当前 Task/Artifact/Trace 已提供部分协议，但不能只把串行函数改成 `gather()`。
