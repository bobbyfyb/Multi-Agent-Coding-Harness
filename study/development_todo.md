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

### 2.5 Context Builder

- [x] 实现 `StaticContextBuilder`
- [x] 实现 `AgentContextBuilder`
- [x] 实现 `PromptSection`
- [x] 支持 system prompt 分 section 组织
- [x] 支持 section priority
- [x] 为后续 memory/context engineering 预留入口

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
- [x] 覆盖 ContextBuilder 测试
- [x] 整理项目复盘与秋招面试准备文档

## 3. 接下来优先补全的单 Agent Harness 能力

### 3.1 Trace / Observability

目标：让每一次 agent 执行都可以复盘。

- [ ] 设计 `TraceRecorder`
- [ ] 记录每轮 LLM request / response metadata
- [ ] 记录每个 tool call 的参数、结果、耗时
- [ ] 记录 permission decision
- [ ] 记录 hook result
- [ ] 记录 final answer
- [ ] 支持 trace 输出为 JSONL
- [ ] 支持 trace 输出为 Markdown summary
- [ ] CLI 运行时生成 trace 文件路径

推荐实现顺序：

1. 先基于现有 `AgentEvent` 做 JSONL recorder。
2. 再扩展 LLM metadata、tool latency、token usage。
3. 最后做可读 Markdown report。

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

- [ ] 定义 `ConversationCompressor`
- [ ] 统计 messages 长度
- [ ] 超过阈值时压缩历史
- [ ] 保留 system prompt
- [ ] 保留最近 N 轮对话
- [ ] 保留未完成任务状态
- [ ] 保留重要 tool result 摘要
- [ ] 生成 session summary
- [ ] 将 summary 作为 context section 注入

建议先做简单版本：

```text
messages 太长
  -> 调用 LLM 总结旧历史
  -> 替换为 summary section + 最近若干轮消息
```

### 3.4 Skill 系统

目标：让 agent 可以按任务加载专项能力说明。

- [ ] 设计 `Skill`
- [ ] 设计本地 `skills/` 目录结构
- [ ] 支持读取 `skills/<name>/SKILL.md`
- [ ] 支持 skill metadata
- [ ] 支持关键词匹配选择 skill
- [ ] 支持手动指定 skill
- [ ] 将 skill 内容注入 `AgentContextBuilder`
- [ ] 为 coding task 准备默认 skill
- [ ] 为 test/debug task 准备默认 skill
- [ ] 为 frontend/backend task 准备默认 skill

初版不要复杂化：

```text
本地 Markdown skill
  -> 简单 keyword router
  -> 注入 prompt section
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
- [x] ContextBuilder 已接入
- [x] 统一 Task System MVP 已接入
- [x] TaskPlanningHook 已接入 `BeforeLLM`
- [x] 基础测试已覆盖

### Milestone 1：可观测单 Agent Harness

- [ ] TraceRecorder
- [ ] JSONL trace
- [ ] Markdown trace summary
- [ ] Tool latency / status 记录
- [ ] Permission decision 记录

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

- [ ] Context compression
- [ ] Session summary
- [ ] Skill loader
- [ ] Skill router

验收标准：

- 长对话不会无限增长。
- 指定任务可以加载对应 skill。

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
3. Hooks：完善 HookManager 生命周期和 trace。
4. Context：实现上下文压缩和 section 管理。
5. Memory：实现 session summary 和长期记忆。
6. Skills：实现本地 skill 加载。
7. MCP：将 MCP tools 接入 ToolRegistry。
8. Multi-agent：最后再做 PM/Engineer/QA 编排。

原则：

```text
每吸收一章内容
  -> 先转化为当前 harness 的模块能力
  -> 再写测试
  -> 最后更新文档和 demo
```

## 8. 暂时不做或延后做的内容

- [ ] 暂不做复杂 Web UI
- [ ] 暂不做真实并行多 agent 协作
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
- [ ] 有上下文压缩或 memory 能力
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

- [ ] 实现 `TraceRecorder`
- [ ] `Agent.run` 支持传入 trace recorder
- [ ] trace 记录 step/tool/permission/final
- [ ] 新增 `run_tests` 工具
- [ ] 新增 `run_lint` 工具
- [ ] QA 思路先不单独成 agent，而是让当前 agent 能调用 test 工具并总结失败
- [ ] 为 trace 和 test tools 写测试

完成后，项目就会从“能调用工具”升级成“能复盘执行过程并验证结果”的 coding harness。

这是后面多 agent 的地基。
