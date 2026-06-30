# Multi-Agent Coding Harness 开发 TODO

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
- 子 Agent 不直接 claim/complete 父 Task；父 Agent 负责验收并更新 Task。
- 总运行预算目前由 `max_steps`、LLM timeout 和工具 timeout 共同约束。

### 2.9 Skill System MVP

- [x] 实现只读 `SkillRegistry`
- [x] 使用 `.llm_agent/skills/<name>/SKILL.md` 目录结构
- [x] 支持 YAML frontmatter：`name / description / when_to_use`
- [x] 支持无 frontmatter 时从目录名和首个标题生成基础元数据
- [x] 启动时扫描 Skill 并检测非法 YAML、非法名称和重复名称
- [x] 将有字符预算限制的 Skill Catalog 注入 system prompt
- [x] 实现 `skill_load`，按需将完整正文作为 tool result 注入 messages
- [x] 实现 `skill_read_resource`
- [x] 防止 Skill 资源路径穿越
- [x] 限制 Skill 正文、资源和 Catalog 大小
- [x] Skill 内容不能绕过 system/user 指令、workspace 和权限策略
- [x] 主 Agent 和 Subagent 共用 Skill 索引
- [x] Subagent 在独立上下文中重新按需加载 Skill
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

## 3. 接下来优先补全的单 Agent Harness 能力

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

- [ ] 优化 `edit_file`，避免简单字符串替换带来的误改
- [ ] 新增 `replace_file_range`
- [ ] 新增 `insert_file_text`
- [ ] 新增 `list_dir`
- [ ] 新增 `file_info`
- [ ] 新增 `run_tests`
- [ ] 新增 `run_lint`
- [ ] 新增 `python_repl` 或安全计算工具
- [ ] 对 bash 命令做更细粒度权限分级
- [ ] 对工具输出做统一截断和摘要

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
- [x] 支持通过 `skill_load(name)` 手动指定 Skill
- [x] 将 Skill Catalog 注入 `ContextManager`
- [x] 将完整 Skill 内容通过 tool result 按需注入 messages
- [x] 提供 `code-review` 默认示例 Skill
- [ ] 为 test/debug task 准备默认 skill
- [ ] 为 frontend/backend task 准备默认 skill
- [ ] 支持用户级和额外目录 Skill 来源
- [ ] 支持 Orchestrator 程序化指定或预加载 Skill
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

- [ ] 学习 MCP tool schema
- [ ] 设计 `MCPToolAdapter`
- [ ] 将 MCP tool 转为 `ToolDefinition`
- [ ] 支持调用 MCP tool
- [ ] 支持 MCP tool error 包装
- [ ] 支持 MCP server 配置
- [ ] 支持启动/连接本地 MCP server
- [ ] 为常见 filesystem/git/search MCP 做 demo

初版目标：

```text
MCP tools -> ToolRegistry -> Agent loop 不变
```

### 3.6 更完整的权限策略

目标：让 coding agent 可以安全执行真实开发任务。

- [ ] 设计权限等级：allow / confirm / deny
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

- [ ] `Requirement`
- [ ] `PRD`
- [ ] `TaskSpec`
- [ ] `ImplementationPlan`
- [ ] `WorkReport`
- [ ] `TestPlan`
- [ ] `TestCase`
- [ ] `TestReport`
- [ ] `DefectReport`
- [ ] `AcceptanceReport`

推荐数据格式：

- 初期使用 Pydantic model。
- 存储为 JSON / Markdown。
- 每个 artifact 有 id、status、owner、created_at、updated_at。
- Task System 已经提供了任务级持久化状态，Artifact store 后续可以复用相同的持久化和 ID 思路。

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

- [ ] PM 生成 PRD
- [ ] PM 生成 TaskSpec
- [ ] Engineer 根据 TaskSpec 修改代码
- [ ] QA 生成 TestPlan
- [ ] QA 执行测试并生成 TestReport
- [ ] PM 根据 TestReport 生成 AcceptanceReport

## 5. 多 Agent 编排系统

### 5.1 RoleAgent 抽象

目标：把当前单 Agent 包装成可复用角色 agent。

- [ ] 设计 `RoleAgent`
- [ ] 每个 RoleAgent 有独立 name
- [ ] 每个 RoleAgent 有独立 system prompt
- [ ] 每个 RoleAgent 有独立 tools
- [ ] 每个 RoleAgent 有独立 context builder
- [ ] 每个 RoleAgent 可以读写 artifact
- [ ] 每个 RoleAgent 可以输出结构化 result

候选角色：

- [ ] `PMAgent`
- [ ] `EngineerAgent`
- [ ] `QAAgent`

不要一开始强行区分 FE/BE：

- 初版可以只有一个 Engineer。
- 第二阶段再按 capability 拆成 frontend/backend。

### 5.2 Orchestrator 状态机

目标：用代码层状态机控制多 agent 流程，而不是完全交给 LLM 自由发挥。

- [ ] 设计 `Orchestrator`
- [ ] 设计任务状态枚举
- [ ] 支持 planning 状态
- [ ] 支持 implementation 状态
- [ ] 支持 qa 状态
- [ ] 支持 fix 状态
- [ ] 支持 acceptance 状态
- [ ] 支持 failed / aborted 状态
- [ ] 支持最大迭代次数
- [ ] 支持 trace 每个状态转移

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
- [ ] 输出 PRD
- [ ] 拆分 TaskSpec
- [ ] 分配任务给 Engineer
- [ ] 根据 QA 反馈决定是否返工
- [ ] 最终验收

需要的工具/上下文：

- 读写 artifact
- 查看项目结构
- 查看 QA 报告
- 生成验收报告

### 5.4 Engineer Agent

职责：

- [ ] 理解 TaskSpec
- [ ] 制定实现计划
- [ ] 读取代码
- [ ] 修改代码
- [ ] 运行局部测试
- [ ] 输出 WorkReport

后续可以拆分：

- [ ] Frontend Engineer
- [ ] Backend Engineer
- [ ] Infra Engineer
- [ ] Test-Fix Engineer

### 5.5 QA Agent

职责：

- [ ] 根据 PRD 生成 TestPlan
- [ ] 根据 TaskSpec 生成 TestCase
- [ ] 运行测试命令
- [ ] 分析失败日志
- [ ] 生成 TestReport
- [ ] 生成 DefectReport
- [ ] 判断是否满足验收标准

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

- [ ] run_tests
- [ ] run_lint
- [ ] 稳定文件编辑工具
- [ ] 工具输出摘要

验收标准：

- agent 可以完成一个小型代码修改任务。
- agent 可以运行测试并根据失败结果修复。

### Milestone 3：上下文与 Skill

- [x] Context compression
- [x] Conversation summary
- [x] Skill loader
- [x] Catalog 驱动的模型语义选择
- [ ] 多来源 Skill loader
- [ ] Orchestrator 显式 Skill 路由

验收标准：

- 长对话不会无限增长。
- [x] 指定任务可以加载对应 Skill。

### Milestone 4：Artifact 工作流

- [ ] PRD model
- [ ] TaskSpec model
- [ ] WorkReport model
- [ ] TestReport model
- [ ] AcceptanceReport model
- [ ] Artifact store

验收标准：

- 一个需求可以被转换为 PRD 和 TaskSpec。
- 开发和测试过程都能沉淀为 artifact。

### Milestone 5：多 Agent MVP

- [ ] PM Agent
- [ ] Engineer Agent
- [ ] QA Agent
- [ ] Orchestrator 状态机
- [ ] QA feedback loop

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
5. Context：优化 token 估算、摘要质量和后压缩恢复。
6. Memory：实现 session summary 和长期记忆。
7. Skills：实现本地 skill 加载。
8. MCP：将 MCP tools 接入 ToolRegistry。
9. Multi-agent：最后再做 PM/Engineer/QA 编排。

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

- [ ] 有清楚 README
- [ ] 有架构图
- [ ] 有可运行 demo
- [ ] 有完整 trace 输出
- [ ] 有 artifact 样例
- [ ] 有测试覆盖
- [ ] 有权限与安全设计
- [x] 有上下文压缩或 memory 能力
- [ ] 有一个多 agent 闭环 demo
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

## 10. 最近一个开发周期的建议任务

建议下一步只做一个小闭环：

### Sprint 1：Trace + Run Tests

- [x] 实现 `TraceRecorder`
- [x] `Agent.run` 支持传入 trace recorder
- [x] trace 记录 step/tool/permission/final
- [ ] 新增 `run_tests` 工具
- [ ] 新增 `run_lint` 工具
- [ ] QA 思路先不单独成 agent，而是让当前 agent 能调用 test 工具并总结失败
- [x] 为 trace 写测试
- [ ] 为 test tools 写测试

完成后，项目就会从“能调用工具”升级成“能复盘执行过程并验证结果”的 coding harness。

这是后面多 agent 的地基。
