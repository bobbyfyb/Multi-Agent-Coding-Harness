# Multi-Agent Coding Harness 开发 TODO

## 0. 当前阶段

核心功能已进入 **Feature Freeze**。后续不再以增加模块数量为目标，优先完成：

- 仓库与配置模板整理
- 2-3 个可复现 Demo
- 小规模对照评估与指标汇总
- README 架构图、Artifact/Trace 样例和发布说明

OAuth、并行 Agent Team、复杂 Web UI、向量数据库记忆和完整 MCP 生态继续保留为
非阻塞的后续方向。

## 1. 上层开发目标

当前项目后续统一朝 **Multi-Agent Coding Harness** 方向演进。

目标不是简单做几个角色 prompt，而是实现一个可以模拟真实软件开发流程的 coding agent runtime：

```text
用户需求
  -> PM Agent 需求澄清 / PRD / 任务拆解
  -> Engineer Agent 执行开发任务
  -> QA Agent 生成测试计划 / 执行测试 / 反馈缺陷
  -> Engineer Agent 根据反馈修复
  -> PM Agent 验收交付
```

整体架构定位：

- **Orchestrator-Worker**：PM/Orchestrator 负责分配任务，Engineer/QA 负责执行。
- **Planner**：PM 负责任务规划、PRD 和 TaskSpec 生成。
- **Evaluator-Optimizer / Reflexion**：QA 基于测试结果反馈，Engineer 根据反馈迭代修复。
- **Agent Harness**：底层提供 LLM 调用、tool calling、权限、上下文、trace、skill、MCP 等通用能力。

阶段性原则：

```text
先打牢单 Agent Harness
  -> 再引入结构化 Artifact
  -> 最后做 PM / Engineer / QA 多 Agent 编排
```

这样能避免系统过早变成“多个 prompt 互相聊天”，也更适合作为秋招简历项目讲深。

## 2. 当前已经实现的内容

### 2.1 LLM 调用层

- [x] 基于官方 SDK 实现 `LLMClient`
- [x] 支持 OpenAI SDK
- [x] 支持 Anthropic SDK
- [x] 支持 OpenAI compatible / Anthropic compatible base_url
- [x] 支持从 `.env` / 环境变量加载模型、API key、base_url
- [x] 抽象统一响应结构 `LLMResponse`
- [x] 抽象统一工具调用结构 `LLMToolCall`
- [x] 适配 OpenAI tool call 解析
- [x] 适配 Anthropic tool_use 解析
- [x] 支持 provider-specific tool result 回灌
- [x] 支持 Anthropic 多工具结果批量回灌方式

### 2.2 Agent Loop

- [x] 实现基础 agent loop
- [x] 支持 tool calling
- [x] 支持多轮工具调用
- [x] 支持外部维护 `messages` 历史
- [x] 支持 `max_steps` 防止无限工具循环
- [x] 支持 `max_steps=None` 的无限循环模式
- [x] 支持中间事件输出 `AgentEvent`
- [x] 支持彩色打印 agent 中间步骤

### 2.3 Tool Registry 和基础工具

- [x] 实现 `ToolRegistry`
- [x] 实现 `ToolDefinition`
- [x] 支持工具注册、批量注册、重复工具名检查
- [x] 支持工具 schema 暴露给 LLM
- [x] 工具异常统一包装为 `{"ok": False, "error": ...}`
- [x] 实现 `bash`
- [x] 实现 `read_file`
- [x] 实现 `write_file`
- [x] 实现 `edit_file`
- [x] 实现 `glob`
- [x] 实现 `search`
- [x] 实现基于 `rg --json` 的本地 `search_text`
- [x] 同步 Bash 返回 exit code、stdout/stderr、耗时和超时状态
- [x] 命令完整输出持久化到 `.llm_agent/tool-results/`
- [x] 实现基于 Pytest JUnit XML 的 `run_tests`
- [x] 实现基于 Ruff JSON 的 `run_lint`
- [x] `read_file` 支持行范围和 SHA256
- [x] `edit_file` 支持唯一匹配、版本校验和原子写入

### 2.4 权限与 Hook

- [x] 实现 `HookManager`
- [x] 实现 `HookContext`
- [x] 实现 `HookResult`
- [x] 支持 `UserPromptSubmit`
- [x] 支持 `PreToolUse`
- [x] 支持 `PostToolUse`
- [x] 支持 `Stop`
- [x] 实现 `PermissionHook`
- [x] 支持危险 bash hard deny
- [x] 支持 workspace 路径越界拒绝
- [x] 支持文件写入/编辑前用户确认
- [x] 支持危险 bash 前用户确认
- [x] 支持 CLI approval provider
- [x] 支持测试用 auto approval provider
- [x] 区分 hook 控制逻辑和 event 展示逻辑

### 2.5 Context Manager

- [x] 使用统一 `ContextManager` 管理初始 Prompt 与运行时消息历史
- [x] 实现 `PromptSection`
- [x] 支持 system prompt 分 section 组织
- [x] 支持 section priority
- [x] 支持 Provider 兼容的 tool call/result 原子消息组
- [x] 支持大工具结果落盘与预览
- [x] 支持旧工具结果占位压缩
- [x] 支持基于上下文预算的自动 LLM 摘要
- [x] 保留 system prompt、conversation summary 和最近消息组
- [x] 支持压缩前 JSONL Transcript
- [x] 支持 `LLMContextLengthError` 与一次 reactive compact retry
- [x] 压缩后重新注入持久化 Task 状态
- [x] 支持主 Agent 与 Subagent 使用独立 ContextManager

### 2.6 统一 Task System

- [x] 不再单独实现 TodoWrite
- [x] 实现统一 `TaskSystem`
- [x] 支持 session-scope task 作为当前会话执行计划
- [x] 支持 project-scope task 作为后续多 agent 跨会话任务
- [x] 支持任务文件持久化到 `.llm_agent/tasks/<task_list_id>/`
- [x] 支持 `.highwatermark` 递增 ID，避免任务 ID 复用
- [x] 支持 `pending / in_progress / completed / blocked / cancelled` 状态
- [x] 支持 `blocked_by` 依赖检查
- [x] 支持 `owner`
- [x] 支持 `evidence` 记录验证证据
- [x] 实现 `task_create`
- [x] 实现 `task_update`
- [x] 实现 `task_list`
- [x] 实现 `task_get`
- [x] 实现 `task_claim`
- [x] 实现 `task_complete`
- [x] 实现基于状态的 `TaskPlanningHook`
- [x] 在 `BeforeLLM` 阶段注入当前 task summary / planning reminder

### 2.7 测试与文档

- [x] 覆盖 LLMClient 测试
- [x] 覆盖 Agent loop 测试
- [x] 覆盖 ToolRegistry 测试
- [x] 覆盖 BasicTools 测试
- [x] 覆盖 Hook / PermissionHook 测试
- [x] 覆盖 TaskSystem / task tools 测试
- [x] 覆盖 ContextManager 构建、压缩和恢复测试
- [x] 整理项目复盘与秋招面试准备文档

### 2.8 同步 Subagent MVP

- [x] 基于现有 `Agent` 复用同一套 agent loop，不维护第二套子循环
- [x] `Agent.run()` 返回结构化 `AgentRunResult`
- [x] 为工具调用增加显式 `ToolExecutionContext`
- [x] 为事件增加 `agent_id / run_id / parent_run_id / depth`
- [x] 实现 `SubagentRunner`
- [x] 子 Agent 使用独立 system prompt 和全新 messages
- [x] 实现 `SubagentRequest` 和 `SubagentResult`
- [x] 实现 `explore` 只读调查工具 profile
- [x] 实现 `general` 可修改工作区工具 profile
- [x] 子 Agent 不暴露 Task 工具和 `subagent_run`，禁止递归委派
- [x] 子 Agent 复用权限 Hook 和 approval provider
- [x] 实现 `subagent_run` 上层工具适配
- [x] 父 Agent 将结构化子 Agent 结果作为普通 tool result 回灌
- [x] 支持父子 Agent 关键步骤的带来源事件输出
- [x] 覆盖同步委派、上下文隔离、工具隔离和深度限制测试

当前边界：

- 同步执行，父 Agent 等待子 Agent 完成后继续。
- 暂不支持后台执行、并行子 Agent、取消和恢复。
- `general` Subagent 可显式使用 Git Worktree 隔离修改。
- 子 Agent 不直接 claim/complete 父 Task；父 Agent 负责验收并更新 Task。
- 总运行预算目前由 `max_steps`、LLM timeout 和工具 timeout 共同约束。

### 2.9 Skill System MVP

- [x] 实现只读 `SkillRegistry`
- [x] 使用 `.llm_agent/skills/<name>/SKILL.md` 目录结构
- [x] 支持 YAML frontmatter：`name / description / when_to_use`
- [x] 支持无 frontmatter 时从目录名和首个标题生成基础元数据
- [x] 启动时扫描 Skill 并检测非法 YAML、非法名称和重复名称
- [x] 将有字符预算限制的 Skill Catalog 注入 system prompt
- [x] 实现 `skill_list`，支持运行时重新发现可用 Skill
- [x] 实现 `skill_load`，按需将完整正文作为 tool result 注入 messages
- [x] 实现 `skill_read_resource`
- [x] 防止 Skill 资源路径穿越
- [x] 限制 Skill 正文、资源和 Catalog 大小
- [x] Skill 内容不能绕过 system/user 指令、workspace 和权限策略
- [x] 主 Agent 和 Subagent 共用 Skill 索引
- [x] Subagent 在独立上下文中重新按需加载 Skill
- [x] Workflow role agent 可发现、动态加载和读取 Skill 资源
- [x] 添加 `code-review` 示例 Skill
- [x] 覆盖解析、索引、预算、资源安全、Agent 加载和 Subagent 加载测试

当前边界：

- MVP 只加载项目级本地 Skill，不加载用户级、插件或远程 Skill。
- Skill 选择由模型根据 Catalog 的语义信息完成，不使用关键词规则路由。
- 暂不支持 `context: fork`、模型覆盖、Skill Hook 和 `allowed-tools` 自动授权。
- Skill 修改后需要调用 `SkillRegistry.refresh()` 或重新启动进程。

### 2.10 Task Intent Classification

- [x] 实现轻量 `TaskIntentClassifier`
- [x] 直接复用 `LLMClient`，不创建 Subagent 或第二套 Agent Loop
- [x] 分类调用不提供工具，限制为 `max_tokens=8 / temperature=0`
- [x] 判断目标从“是否为 coding task”调整为“是否需要持久化多步骤计划”
- [x] 按完整用户 prompt 缓存分类结果
- [x] LLM 调用失败或输出不明确时使用本地规则降级
- [x] `TaskPlanningHook` 只负责消费分类结果并生成提醒
- [x] 跳过 `<current_tasks>` 和 `<task_reminder>` 等内部 user 消息
- [x] 提醒在同一 prompt 的后续 step 中去重，新 prompt 可再次提醒
- [x] 默认 Hook 工厂支持注入现有 `LLMClient`
- [x] 覆盖 YES/NO 解析、缓存、降级和 Hook 集成测试

当前边界：

- 分类目前使用主 Agent 的同一模型，暂未支持单独配置低成本分类模型。
- 分类调用不转换为 `AgentEvent`，由 `LLMClient` 以
  `operation=task_intent` 写入统一 Trace。
- 本地降级规则仍是启发式判断，只用于模型不可用或响应不规范的情况。

### 2.11 Long-term Memory System

- [x] 实现 `Memory / MemoryStore / MemoryManager`
- [x] 使用 `.llm_agent/memory/` 下的 Markdown + YAML frontmatter 存储
- [x] 使用稳定 Memory ID 和原子文件写入
- [x] 自动重建可读的 `MEMORY.md` 索引
- [x] 支持 `user / feedback / project / reference` 四种记忆
- [x] 支持 pinned Memory 和按请求选择的 relevant Memory
- [x] 使用轻量 LLM side-query 选择相关 Memory
- [x] 选择失败时降级到 name/description 关键词匹配
- [x] 按用户请求和索引版本缓存选择结果
- [x] 限制召回条数、单条大小和总字符预算
- [x] 实现 `memory_remember / memory_search / memory_get / memory_forget`
- [x] `memory_forget` 接入权限确认
- [x] 实现保守的 Stop Hook 自动提取
- [x] 拒绝保存疑似密钥、Task 状态、临时日志和猜测
- [x] Memory 作为 request-scoped context 注入，不污染 canonical history
- [x] 将 runtime context 和工具 schema 纳入压缩预留预算
- [x] 压缩后从持久化存储重新召回 Memory
- [x] Subagent 只读接收相关 Memory，不获得 Memory 工具
- [x] 增加召回、提取成功和提取失败 AgentEvent
- [x] 覆盖持久化、更新、检索、降级、提取、工具和 Agent 集成测试

当前边界：

- Memory 当前按项目存储，不提供用户级全局 Memory。
- Markdown 文件是事实来源，暂不使用 embedding 或向量数据库。
- 自动提取只在检测到明确长期信号时运行，避免每轮额外 LLM 调用。
- 暂未实现低频语义合并（Dream）、跨进程写锁和 Memory 版本历史。
- 当前 conversation summary 负责会话内 compact 连续性；可恢复 Session
  Memory 等 `AgentSession` 落地后再实现。

### 2.12 Error Recovery

- [x] 实现 `RecoveryPolicy / RecoveryState / RecoveryNotice`
- [x] 基于异常类型、HTTP status 和 error code 分类 LLM 错误
- [x] 对连接失败、超时、408、409、429 和 5xx 执行有界指数退避
- [x] 支持 `Retry-After`、jitter 和累计等待时间限制
- [x] 关闭 OpenAI / Anthropic SDK 内置重试，避免双层重试
- [x] 连续 overload 后支持同 provider fallback model
- [x] 复用 `ContextManager.recover()` 处理 context-length
- [x] 输出截断时先提升 `max_tokens`，再执行有界 continuation
- [x] 截断工具调用不会执行
- [x] 工具错误继续回灌模型，不自动重试副作用工具
- [x] 恢复动作接入 AgentEvent、终端输出和 Trace
- [x] CLI 在恢复耗尽后报告错误并保持交互进程
- [x] 覆盖重试、fallback、截断恢复、Trace 和工具副作用测试

当前边界：

- 仅支持同一 provider 内的 fallback model，不自动跨 Provider 切换。
- 当前为同步非流式恢复，尚未处理流式中断与断点续传。
- Error Recovery 只负责当前进程内执行，不等同于跨会话 Session 恢复。
- 工具需要先引入 idempotent 元数据，才会考虑只读工具自动重试。

### 2.13 Managed Background Jobs

- [x] 实现 `BackgroundJob / BackgroundJobStore / BackgroundJobManager`
- [x] 使用 `subprocess.Popen` 启动独立进程，不阻塞当前 Agent Loop
- [x] `bash` 支持显式 `run_in_background=true`
- [x] 默认保持同步执行，不使用关键词启发式自动切换
- [x] 立即返回稳定 `job_id`，保证原 Tool Call 只回灌一次结果
- [x] 实现 `background_list / background_get / background_output`
- [x] 实现有界 `background_wait`
- [x] 实现 `background_cancel` 和进程组 `SIGTERM / SIGKILL`
- [x] 支持最大并发数和单 Job 运行超时
- [x] stdout/stderr 直接写入文件，避免 Pipe 堵塞和上下文膨胀
- [x] Job 元数据和日志持久化到 `.llm_agent/background/`
- [x] 重启时将未完成 Job 标记为 `interrupted`
- [x] 完成通知在 Agent 安全边界作为独立内部消息回灌
- [x] 通知不复用原始 `tool_call_id`，并持久化确认状态
- [x] 支持可选 `task_id` 关联，但不自动完成 Task
- [x] 后台生命周期接入 `AgentEvent`、彩色终端输出和 Trace
- [x] 显式捕获 `TraceRecorder / TraceContext`，避免依赖线程间
  `ContextVar` 传播
- [x] CLI 退出时终止仍由当前进程管理的后台 Job
- [x] 覆盖完成、失败、超时、取消、并发限制、恢复和通知测试

当前边界：

- 只有主 Agent 的 Bash 可以后台执行，文件工具和 Subagent 仍保持同步。
- Job 是运行时执行句柄，Task 是持久化工作计划，两者不会自动合并状态。
- 后台进程失败不会进入 LLM Error Recovery 自动重试，避免重复副作用。
- CLI 空闲等待输入时不异步刷新终端；通知在下一次 Agent run 或管理工具调用时
  展示。
- 崩溃后保留日志和元数据，但 MVP 不尝试重新连接旧 PID。

### 2.14 Worktree Isolation

- [x] 实现 `WorktreeInfo / WorktreeManager`
- [x] Git Worktree、分支和 base commit 使用系统生成标识
- [x] 主工作区不干净时拒绝创建，避免遗漏未提交修改
- [x] `subagent_run` 支持 `isolation=shared/worktree`
- [x] Worktree 隔离仅允许 `general` 修改模式
- [x] 子 Agent 的工具、权限、Context、测试和 Lint 自动切换工作目录
- [x] 无修改 Worktree 自动清理
- [x] 有修改 Worktree 保留并返回文件列表、Diff 预览和完整 Patch
- [x] 实现 `worktree_list / worktree_diff / worktree_apply / worktree_remove`
- [x] Apply 前校验 base commit 和 `git apply --check`
- [x] 普通 Remove 拒绝删除未应用修改
- [x] Apply 和强制丢弃接入 PermissionHook
- [x] Worktree 生命周期接入 Trace
- [x] 元数据记录 `task_id / agent_id / run_id`
- [x] 使用 `RLock` 保护生命周期操作
- [x] 实现轻量 `reconcile()` 标记目录或 Git 注册信息丢失
- [x] 覆盖创建、隔离修改、Apply、过期 HEAD、清理和对账测试

当前边界：

- 仍然是同步 Subagent，Worktree 先解决目录隔离和审查，不提供并行执行。
- 不自动 Commit、Merge、Rebase、Push 或解决冲突。
- MVP 要求主工作区干净，不实现 Dirty Workspace Snapshot。
- Worktree 是文件修改隔离，不是容器或恶意代码安全边界。
- Worktree 与 Task 保持独立，只通过元数据关联，不自动改变 Task 状态。

### 2.15 CLI Interaction

- [x] 使用 `prompt_toolkit` 替换单行 `input()`
- [x] 支持多行 Prompt 编辑和提交
- [x] 使用 `.llm_agent/input_history` 持久化输入历史
- [x] 支持历史搜索和历史内容建议
- [x] 使用独立单行 Session 处理权限确认
- [x] 使用 `patch_stdout` 避免终端输出破坏当前编辑区
- [x] 支持 `Ctrl+C` 取消当前输入和 `Ctrl+D` 退出
- [x] 保留 `q / exit` 并增加 `/exit / /quit` 退出命令

当前只增强交互式 CLI，不改变 Agent 的字符串输入接口。后续 Textual TUI 或
Web UI 可以继续复用同一个 Agent、Hook 和 Event 边界。

### 2.16 Artifact System MVP

- [x] 实现 `Artifact / ArtifactStore / ArtifactManager`
- [x] 使用 `.llm_agent/artifacts/<collection_id>/` 持久化结构化工作产物
- [x] 使用 `.highwatermark` 递增生成稳定 `artifact_0001` ID
- [x] 支持 `prd / task_spec / implementation_report / test_report /
  acceptance_report / note` 类型
- [x] 支持 `draft / ready / accepted / superseded / archived` 状态
- [x] 支持 `task_id / owner / metadata` 关联任务、角色和额外上下文
- [x] 支持 `version` 和 `expected_version` 乐观锁，避免旧版本覆盖
- [x] 记录 artifact 创建和更新 history
- [x] 实现 `artifact_create / artifact_update / artifact_get / artifact_list`
- [x] 将 artifact 工具接入默认 `ToolRegistry`
- [x] 实现 `ArtifactContextHook`，在 `BeforeLLM` 阶段注入相关 artifact
- [x] 优先注入当前 open task 相关 artifact，其次注入 PRD/TaskSpec 等基础产物
- [x] Runtime context 使用 `<relevant_artifacts>`，不写入 canonical history
- [x] Artifact 创建和更新接入 `TraceRecorder`
- [x] 在 system prompt 中加入 Artifact 使用策略
- [x] 覆盖持久化、工具、上下文注入、版本冲突和 Trace 测试

当前边界：

- MVP 使用统一 Markdown `content`，尚未为每种 artifact 建立强 schema。
- Artifact 是多 agent 通讯协议和交接产物，Task 仍然负责工作状态流转。
- 当前只做 create/update/get/list，不提供 delete；归档通过 `status=archived` 表达。
- 暂未实现 artifact diff、依赖图、文件锁和自动 LLM 总结。
- Subagent 暂不直接获得 artifact 工具，后续 PM/Engineer/QA 编排时再按角色分配。

### 2.17 Serial Orchestrator-Worker Workflow MVP

- [x] 实现 `SerialCodingWorkflow`
- [x] 实现轻量 `RoleSpec`
- [x] 实现 `WorkflowPhaseResult / WorkflowResult`
- [x] 使用代码层状态机控制 PM -> Engineer -> QA -> Engineer Fix -> QA -> PM Acceptance
- [x] PM worker 负责生成 `prd` 和 `task_spec`
- [x] Engineer worker 负责实现并生成/更新 `implementation_report`
- [x] QA worker 负责验证并生成 `test_report`
- [x] PM Acceptance worker 负责生成 `acceptance_report`
- [x] 每个 worker 使用独立 `ContextManager`、system prompt 和工具白名单
- [x] 每个 worker 复用同一个 `LLMClient / ArtifactManager`
- [x] Workflow 默认创建一个共享 Worktree 作为代码执行空间
- [x] PM Planning 使用主工作区，Engineer / QA / Fix / Acceptance 复用同一 Worktree
- [x] 将代码工具与权限绑定到 Worktree，将 Task / Artifact / Skill 状态保留在主工作区
- [x] Workflow Record 持久化 `worktree_id / worktree_base_commit`
- [x] Resume 复用原 Worktree，隔离目录丢失时明确失败
- [x] Workflow 结果返回 changed files、Diff 路径和待 Apply 状态
- [x] 最终 Apply 保持显式权限确认，不由 Workflow 自动执行
- [x] 每个 worker 可通过 `RoleSpec` 绑定 required/optional Skill
- [x] Required Skill 自动注入 role context，Optional Skill 保持按需加载
- [x] 支持 `.llm_agent/workflow.yaml` 配置 workflow 预算和 role skill
- [x] 配置层只开放 `required_skills / optional_skills / max_steps`，不开放工具权限
- [x] Required Skill 缺失时 fail fast，Optional Skill 缺失时忽略并 warning
- [x] Artifact gate 检查每个阶段是否新建或更新了必需 artifact
- [x] QA gate 要求 `metadata.verdict` 明确为 `pass` 或 `fail`
- [x] TaskSpec 要求声明布尔值 `metadata.change_required`
- [x] Worktree phase 记录前后 Diff SHA、changed files，并持久化到 checkpoint
- [x] ImplementationReport 的 `outcome / changed_files` 与真实 Worktree Diff 对账
- [x] QA 验证工具结果在调用完成后立即持久化，不依赖 Trace 回放
- [x] QA pass 要求真实成功的 `run_tests/run_lint`，且无失败证据和 QA 代码修改
- [x] Acceptance prompt 注入由 Orchestrator 汇总的实现与验证证据
- [x] 阶段 completion evidence gate 失败时自动给同一 worker 一次纠正机会
- [x] QA verdict 为 `fail` 时触发一次 Engineer fix cycle 和 QA regression
- [x] Workflow 生命周期和 phase 事件写入 Trace
- [x] CLI 支持显式 `/workflow <request>` 入口
- [x] Workflow run record 持久化到 `.llm_agent/workflows/<workflow_id>/run.json`
- [x] 支持 `/workflow-list`、`/workflow-show <id>`、`/workflow-resume <id>`
- [x] Resume 基于 phase checkpoint 和 completion gate，不做完整 message replay
- [x] 覆盖成功、no-change、虚假实现声明、无验证 QA pass、gate retry 和 fix cycle

当前边界：

- 目前是同步串行 Orchestrator-Worker，不支持并行 Agent Team。
- Workflow 已支持 checkpoint resume，但暂不支持跨机器锁、并发 resume 或 phase 内精确断点。
- Role 目前使用 `RoleSpec` 配置，没有单独抽象 `PMAgent / EngineerAgent / QAAgent` 类。
- Artifact metadata 仍是轻量字典校验，尚未为各类 Artifact 引入独立强 schema。
- Workflow 已默认使用 Git Worktree；非 Git 场景可配置 `isolation=shared`。
- Evidence Gate 已校验 ImplementationReport、Worktree Diff 与验证工具结果；
  `shared` 兼容模式没有独立 Diff 事实源，因此只执行较弱的 Artifact gate。

## 3. Harness 能力状态与可选扩展

本节未完成项均为 Feature Freeze 后的可选增强，不阻塞 Demo、评估或 `v1.0.0`
发布。

### 3.1 Trace / Observability

目标：让每一次 agent 执行都可以复盘。

- [x] 设计 `TraceRecorder`
- [x] 记录每轮 LLM request / response metadata
- [x] 记录每个 tool call 的参数、结果、耗时
- [x] 记录 permission decision
- [x] 记录 hook result
- [x] 记录 final answer
- [x] 支持 trace 输出为 JSONL
- [x] 支持 trace 输出为 Markdown summary
- [x] CLI 运行时生成 trace 文件路径
- [x] 关联父子 Agent 的 `run_id / parent_run_id / depth`
- [x] 区分 Agent、Context、Memory、Task Intent 等 LLM operation
- [x] 默认关闭完整 LLM 内容记录，并支持递归脱敏和截断
- [x] Trace 写入失败默认不影响 Agent 主流程

### 3.2 更可靠的 Coding Tools

目标：让 agent 能更稳定地改代码、验证代码。

- [x] 优化 `edit_file`，要求唯一匹配并支持 SHA256 乐观锁
- [ ] 新增 `replace_file_range`
- [ ] 新增 `insert_file_text`
- [ ] 新增 `list_dir`
- [ ] 新增 `file_info`
- [x] 新增结构化 Pytest `run_tests`
- [x] 新增结构化 Ruff `run_lint`
- [ ] 新增 `python_repl` 或安全计算工具
- [ ] 对 bash 命令做更细粒度权限分级
- [ ] 对工具输出做统一截断和摘要
- [x] Bash/测试/Lint 输出落盘并返回有界预览
- [x] 非零退出码与工具基础设施错误使用不同语义

优先级最高：

- `run_tests`
- `run_lint`
- 更稳的文件编辑工具

### 3.3 Context Compression

目标：解决多轮开发时上下文越来越长的问题。

- [x] 将压缩能力统一到 `ContextManager`
- [x] 估算 messages token 占用
- [x] 超过阈值时压缩历史
- [x] 保留 system prompt
- [x] 按 Provider 安全消息组保留最近 N 轮
- [x] 压缩后重新注入未完成任务状态
- [x] 大工具结果落盘，旧工具结果保留占位和恢复路径
- [x] 生成 conversation summary
- [x] 将 summary 作为内部 user context 注入
- [x] 保存完整 JSONL Transcript
- [x] 支持 prompt-too-long reactive retry
- [x] Task/Memory Hook 上下文只进入 LLM request，不写入 canonical history
- [x] 将工具 schema 和 runtime context 纳入完整请求预算

当前流程：

```text
工具结果落盘
  -> 旧结果占位
  -> 构造 Task / Memory runtime context
  -> 计算 messages + runtime context + tools 完整预算
  -> LLM 总结旧历史
  -> system + summary + 最近消息组 + 临时 runtime context
  -> context-length 失败时应急恢复一次
```

### 3.4 Skill 系统

目标：让 agent 可以按任务加载专项能力说明。

- [x] 设计 `SkillMetadata / SkillDocument / SkillRegistry`
- [x] 设计本地 `.llm_agent/skills/` 目录结构
- [x] 支持读取 `skills/<name>/SKILL.md`
- [x] 支持 Skill metadata
- [x] 由模型根据精简 Catalog 进行语义选择
- [x] 支持通过 `skill_list()` 运行时发现 Skill
- [x] 支持通过 `skill_load(name)` 手动指定 Skill
- [x] 将 Skill Catalog 注入 `ContextManager`
- [x] 将完整 Skill 内容通过 tool result 按需注入 messages
- [x] 支持 Orchestrator 通过 `RoleSpec` 程序化指定或预加载 Skill
- [x] 提供 `code-review` 默认示例 Skill
- [ ] 为 test/debug task 准备默认 skill
- [ ] 为 frontend/backend task 准备默认 skill
- [ ] 支持用户级和额外目录 Skill 来源
- [ ] 与 Context Compression 协作保留或摘要已加载 Skill

当前实现：

```text
本地 Markdown Skill
  -> Catalog 注入 system prompt
  -> 模型按语义调用 skill_load
  -> 完整正文进入当前 messages
```

### 3.5 MCP 接入

目标：让外部 MCP tools 可以进入当前 ToolRegistry。

- [x] 基于官方 Python SDK v2 接入 MCP tool schema
- [x] 通过薄适配层将 MCP tool 转为 `ToolDefinition`
- [x] 支持 stdio 与 Streamable HTTP transport
- [x] 使用 AnyIO BlockingPortal 保持同步 Agent loop
- [x] 支持工具调用和 `isError` / 协议错误包装
- [x] 支持 `.llm_agent/mcp.yaml` server 配置
- [x] 支持工具命名空间、scope、include/exclude 和数量上限
- [x] 支持 allow / confirm / deny 权限策略
- [x] 支持主 Agent、Workflow 角色和 general Subagent
- [x] 支持 worktree-aware stdio session
- [x] 隔离 stdio 子进程环境并限制远程 HTTP URL
- [x] 提供本地 stdio demo 和真实协议集成测试
- [x] 接入并验证 Context7 远程 MCP server
- [ ] 按需评估 Resources、Prompts、OAuth 与动态 tool-list 订阅

初版目标：

```text
MCP tools -> ToolRegistry -> Agent loop 不变
```

当前实现保留本地核心 coding tools。MCP 是外部能力扩展协议，不替代 Harness
内部高频、强权限约束且需要 Worktree/Trace 深度协作的原生工具。

### 3.6 更完整的权限策略

目标：让 coding agent 可以安全执行真实开发任务。

- [x] 设计权限等级：allow / confirm / deny
- [ ] 将权限规则配置化
- [ ] 支持 `permissions.yaml`
- [ ] 区分读操作、写操作、网络操作、进程操作
- [ ] 支持命令 prefix allowlist
- [ ] 支持危险命令 denylist
- [ ] 支持对工具调用进行 dry-run 展示
- [ ] 支持本轮会话记住用户授权
- [ ] 支持 audit log

## 4. 结构化 Artifact 系统

目标：从“聊天式协作”升级为“软件工程式协作”。

核心 Artifact：

- [x] `PRD`
- [x] `TaskSpec`
- [x] `ImplementationReport`
- [x] `TestReport`
- [x] `AcceptanceReport`
- [x] `Note`
- [ ] `Requirement`
- [ ] `ImplementationPlan`
- [ ] `TestPlan`
- [ ] `TestCase`
- [ ] `DefectReport`

推荐数据格式：

- MVP 使用 dataclass model + JSON 存储，`content` 使用 Markdown 文本。
- 每个 artifact 有 id、kind、status、owner、task_id、version、created_at、updated_at。
- 使用 `expected_version` 进行乐观锁更新。
- 后续再为 PRD、TaskSpec、TestReport 等引入更严格的 Pydantic schema。

Artifact 流转：

```text
User Requirement
  -> PRD
  -> TaskSpec[]
  -> WorkReport[]
  -> TestPlan
  -> TestReport
  -> DefectReport[]
  -> Fix WorkReport[]
  -> AcceptanceReport
```

第一版最小闭环：

- [x] Artifact store / manager / tools
- [x] Artifact runtime context 注入
- [x] Artifact Trace 记录
- [x] PM 生成 PRD
- [x] PM 生成 TaskSpec
- [x] Engineer 根据 TaskSpec 修改代码或确认无需修改
- [ ] QA 生成 TestPlan
- [x] QA 执行测试并生成 TestReport
- [x] PM 根据 TestReport 生成 AcceptanceReport

## 5. 多 Agent 编排系统

### 5.1 RoleAgent 抽象

目标：把当前单 Agent 包装成可复用角色 agent。

- [x] 设计轻量 `RoleSpec`
- [x] 每个 Worker 有独立 name / agent_id
- [x] 每个 Worker 有独立 system prompt
- [x] 每个 Worker 有独立 tools 白名单
- [x] 每个 Worker 可配置 required/optional Skill
- [x] 每个 Worker 的 Skill 和步数预算可通过 `.llm_agent/workflow.yaml` 配置
- [x] 每个 Worker 有独立 ContextManager
- [x] 每个 Worker 可以读写 artifact
- [x] 每个 Worker 可以输出 phase result
- [ ] 提炼正式 `RoleAgent` 抽象

候选角色：

- [x] `PM` worker
- [x] `Engineer` worker
- [x] `QA` worker
- [ ] 独立 `PMAgent / EngineerAgent / QAAgent` 类

不要一开始强行区分 FE/BE：

- 初版可以只有一个 Engineer。
- 第二阶段再按 capability 拆成 frontend/backend。

### 5.2 Orchestrator 状态机

目标：用代码层状态机控制多 agent 流程，而不是完全交给 LLM 自由发挥。

- [x] 设计 `SerialCodingWorkflow` 作为轻量 Orchestrator
- [x] 设计 phase 状态和 result
- [x] 支持 planning 状态
- [x] 支持 implementation 状态
- [x] 支持 qa 状态
- [x] 支持 fix 状态
- [x] 支持 acceptance 状态
- [x] 支持 failed 状态
- [ ] 支持 aborted 状态
- [x] 支持最大迭代次数
- [x] 支持 trace 每个状态转移

状态流：

```text
INIT
  -> REQUIREMENT_CLARIFICATION
  -> PLANNING
  -> IMPLEMENTATION
  -> QA
  -> FIX
  -> QA
  -> ACCEPTANCE
  -> DONE
```

### 5.3 PM Agent

职责：

- [ ] 与用户交互并澄清需求
- [x] 输出 PRD
- [x] 拆分 TaskSpec
- [x] 由 Orchestrator 分配任务给 Engineer
- [x] 根据 QA 反馈触发返工
- [x] 最终验收

需要的工具/上下文：

- 读写 artifact
- 查看项目结构
- 查看 QA 报告
- 生成验收报告

### 5.4 Engineer Agent

职责：

- [x] 理解 TaskSpec
- [x] 制定并执行实现方案
- [x] 读取代码
- [x] 修改代码
- [x] 运行局部测试
- [x] 输出 ImplementationReport

后续可以拆分：

- [ ] Frontend Engineer
- [ ] Backend Engineer
- [ ] Infra Engineer
- [ ] Test-Fix Engineer

### 5.5 QA Agent

职责：

- [ ] 根据 PRD 生成 TestPlan
- [ ] 根据 TaskSpec 生成 TestCase
- [x] 运行测试命令
- [x] 分析失败日志
- [x] 生成 TestReport
- [ ] 生成 DefectReport
- [x] 判断是否满足验收标准

QA 必须基于真实测试结果：

```text
pytest / ruff / build / API test / UI test
```

不要只做 LLM judge。

## 6. 推荐开发里程碑

### Milestone 0：当前状态

- [x] 单 Agent 可调用 LLM
- [x] 单 Agent 可调用工具
- [x] 权限 hook 已接入
- [x] ContextManager 与自动压缩已接入
- [x] 统一 Task System MVP 已接入
- [x] TaskPlanningHook 已接入 `BeforeLLM`
- [x] 同步 Subagent MVP 已接入
- [x] Error Recovery 已接入
- [x] Managed Background Jobs 已接入
- [x] Worktree Isolation 已接入
- [x] 基础测试已覆盖

### Milestone 1：可观测单 Agent Harness

- [x] TraceRecorder
- [x] JSONL trace
- [x] Markdown trace summary
- [x] Tool latency / status 记录
- [x] Permission decision 记录

验收标准：

- 任意一次 agent run 都能生成可复盘 trace。
- trace 中能看到 LLM 调用、工具调用、权限决策、最终回答。

### Milestone 2：可靠 Coding Tools

- [x] run_tests
- [x] run_lint
- [x] 最小稳定文件编辑能力
- [x] 命令输出落盘和有界预览

验收标准：

- agent 可以完成一个小型代码修改任务。
- agent 可以运行测试并根据失败结果修复。

### Milestone 3：上下文与 Skill

- [x] Context compression
- [x] Conversation summary
- [x] Skill loader
- [x] Catalog 驱动的模型语义选择
- [ ] 多来源 Skill loader
- [x] Orchestrator 显式 Skill 路由

验收标准：

- 长对话不会无限增长。
- [x] 指定任务可以加载对应 Skill。

### Milestone 4：Artifact 工作流

- [x] Artifact store
- [x] Artifact tools
- [x] Artifact runtime context hook
- [x] Artifact trace events
- [ ] PRD strong schema
- [ ] TaskSpec strong schema
- [ ] WorkReport strong schema
- [ ] TestReport strong schema
- [ ] AcceptanceReport strong schema

验收标准：

- [x] 开发和测试过程可以沉淀为 artifact。
- [x] 一个需求可以由 PM Agent 自动转换为 PRD 和 TaskSpec。

### Milestone 5：多 Agent MVP

- [x] PM worker
- [x] Engineer worker
- [x] QA worker
- [x] Orchestrator 状态机
- [x] QA feedback loop

验收标准：

- 输入一个小需求，系统能自动完成：
  - PRD
  - 任务拆解
  - 代码修改
  - 测试执行
  - 缺陷反馈
  - 修复
  - 验收报告

### Milestone 6：简历级 Demo

- [ ] 准备 2-3 个固定 demo task
- [ ] 生成完整 trace
- [ ] 生成 artifact 报告
- [ ] README 展示架构图
- [ ] 录制演示 gif 或截图
- [ ] 增加项目亮点说明

验收标准：

- 面试时可以 3 分钟讲清楚架构。
- 面试官追问每个模块都有代码和测试支撑。

## 7. Learn-Claude-Code 学习路线映射

接下来可以继续参考 `learn-claude-code` 的章节逐步补功能。

建议吸收顺序：

1. Tool use：继续完善工具定义和工具调用。
2. Permission：扩展当前 PermissionHook。
3. Hooks：继续扩展生命周期；Hook trace 已接入。
4. Subagent：继续补充预算和取消；父子 trace 已接入，之后再考虑并行执行。
5. Error Recovery：继续补充流式中断和可取消 backoff。
6. Background Tasks：已实现托管后台 Bash，后续再考虑只读后台 Subagent。
7. Context：优化 token 估算、摘要质量和后压缩恢复。
8. Memory：实现 session summary 和长期记忆。
9. Skills：实现本地 skill 加载。
10. MCP：已将 MCP tools 接入 ToolRegistry；后续按业务场景扩充 server。
11. Multi-agent：最后再做 PM/Engineer/QA 编排。

原则：

```text
每吸收一章内容
  -> 先转化为当前 harness 的模块能力
  -> 再写测试
  -> 最后更新文档和 demo
```

## 8. 暂时不做或延后做的内容

- [ ] 暂不做复杂 Web UI
- [ ] 暂不做异步或并行 Subagent / Agent Team 协作
- [ ] 暂不做完整云端部署
- [ ] 暂不做复杂向量数据库记忆
- [ ] 暂不做完整 MCP server 生态
- [ ] 暂不做自动 skill 进化
- [ ] 暂不做大规模 benchmark

这些内容不是不重要，而是优先级低于 harness 稳定性和 demo 闭环。

## 9. 秋招简历导向的完成标准

这个项目想在简历上有说服力，至少需要满足：

- [x] 有清楚 README
- [ ] 有架构图
- [ ] 有可运行 demo
- [x] 有完整 trace 输出
- [ ] 有 artifact 样例
- [x] 有测试覆盖
- [x] 有权限与安全设计
- [x] 有上下文压缩或 memory 能力
- [x] 有一个多 agent 闭环 demo
- [ ] 能解释和 Codex / Claude Code 的差异

面试时重点讲：

- 为什么要做统一 LLMClient
- OpenAI / Anthropic tool calling 的差异
- Agent loop 如何防止无限循环
- ToolRegistry 如何扩展工具
- PermissionHook 如何降低风险
- Hook 和 Event 为什么分离
- Context compression 如何保留关键信息
- Artifact 如何让多 agent 协作更工程化
- QA 如何形成真实测试反馈闭环

## 10. 最近完成的开发周期

### Sprint 1：Trace + Run Tests

- [x] 实现 `TraceRecorder`
- [x] `Agent.run` 支持传入 trace recorder
- [x] trace 记录 step/tool/permission/final
- [x] 新增 `run_tests` 工具
- [x] 新增 `run_lint` 工具
- [ ] QA 思路先不单独成 agent，而是让当前 agent 能调用 test 工具并总结失败
- [x] 为 trace 写测试
- [x] 为 test tools 写测试

这一阶段将项目从“能调用工具”升级为“能复盘执行过程并验证结果”的 coding
harness。

这是后面多 agent 的地基。
