# Multi-Agent Coding Harness 项目复盘与面试主讲稿

## 1. 项目定位

### 1.1 一句话介绍

这是一个用 Python 从零实现的 **Multi-Agent Coding Harness**：它把不同厂商的 LLM tool calling、可靠代码工具、权限与隔离、上下文和记忆、可恢复工作流、结构化协作产物以及执行追踪组合成一套可测试的 Agent Runtime。

它不是聊天机器人，也不是简单的“调用模型再执行函数”。项目要解决的是：

> 当模型被允许读取仓库、修改代码、运行命令，并由多个角色连续协作时，如何让执行过程可控、可恢复、可验证、可审计。

### 1.2 项目背景

一个能演示 tool calling 的 Agent Loop 很容易写，但一旦任务变长，就会出现工程问题：

- OpenAI 与 Anthropic 的消息、工具定义和工具结果协议不同，上层逻辑容易被供应商格式污染。
- 模型输出只是“建议”，工具执行却会产生真实副作用，需要权限、安全边界和可回放记录。
- 长任务会超出上下文窗口；单纯保留聊天历史既昂贵，也不能支持进程退出后的恢复。
- 多角色只靠自然语言交接，容易丢失约束、重复工作，甚至“报告完成但没有改代码”。
- QA 可能口头声称测试通过，但真实命令失败；Harness 必须拥有独立于模型叙述的事实来源。
- 网络超时、输出截断、依赖缺失和进程中断是常态，不能把所有异常都当成一次性失败。

因此，本项目的重点不是堆叠更多 Agent，而是补齐 **Agent 执行基础设施**。

### 1.3 设计目标

1. **统一模型协议**：Agent 只处理统一消息、响应和 Tool Call，不依赖厂商 SDK 对象。
2. **控制真实副作用**：代码操作受工作区边界、权限策略、Worktree 和结构化验证约束。
3. **支持长任务**：区分运行内上下文、工作流短期状态和跨会话长期记忆。
4. **协作有协议**：Task 表达控制状态，Artifact 表达角色交付物，Workflow 负责状态迁移。
5. **结果可证伪**：以 Git diff 和验证工具结果为事实，不以模型自述作为最终证据。
6. **过程可恢复、可观察**：Workflow checkpoint 支持 resume，Trace 支持定位每一步成本和失败。
7. **保持轻量**：优先使用文件存储、串行编排和组合式模块，避免为了“完整”过早引入分布式系统。

### 1.4 明确不做什么

当前版本不是：

- 完整容器沙箱或云端 Agent 平台；
- 并行 Agent Team 调度系统；
- 通用向量记忆平台；
- 全量实现 MCP 的所有能力；
- 已达到生产级成功率的自主软件工程产品。

这些边界很重要。面试时应说明：项目选择先把串行闭环做可靠，再用评测决定是否增加并行和分布式复杂度。

## 2. 总体架构

### 2.1 分层视图

```text
┌──────────────────────────────────────────────────────────────┐
│ CLI / Workflow API                                           │
│ 多行输入、事件展示、workflow run/show/resume、MCP 状态       │
└─────────────────────────────┬────────────────────────────────┘
                              │
┌─────────────────────────────▼────────────────────────────────┐
│ Orchestration                                                │
│ SerialCodingWorkflow / SubagentRunner / BackgroundJobManager │
└───────────────┬──────────────────────────────┬───────────────┘
                │                              │
┌───────────────▼──────────────┐ ┌────────────▼────────────────┐
│ Agent Runtime               │ │ Collaboration State          │
│ Agent Loop / Hook / Recovery│ │ Task / Artifact / Checkpoint │
│ Context / Event             │ │ Attempt Handoff / Memory     │
└───────────────┬──────────────┘ └────────────┬────────────────┘
                │                              │
┌───────────────▼──────────────────────────────▼────────────────┐
│ Capability Layer                                             │
│ ToolRegistry / Coding Tools / Verification / Skills / MCP     │
└───────────────┬──────────────────────────────┬────────────────┘
                │                              │
┌───────────────▼──────────────┐ ┌────────────▼────────────────┐
│ Provider Adapter            │ │ Execution & Observability    │
│ OpenAI SDK / Anthropic SDK  │ │ Worktree / Security / Trace  │
└──────────────────────────────┘ └─────────────────────────────┘
```

### 2.2 三类关键数据

系统刻意区分三类数据，避免一个 `messages` 列表承担所有职责：

| 数据 | 作用 | 生命周期 | 事实来源 |
|---|---|---|---|
| Conversation Context | 当前 Agent 推理所需的近期对话和工具结果 | 单次运行或交互会话 | `ContextManager` |
| Workflow State | 当前需求处于哪个阶段、已尝试什么、如何恢复 | 单个 Workflow，可跨进程 | `WorkflowStore` / checkpoint |
| Durable Knowledge | 用户偏好、项目约定、验证过的经验 | 跨会话 | `MemoryManager` |

此外还有两类协作数据：

- **Task**：谁要做什么、状态是什么、依赖是否满足，是控制面。
- **Artifact**：PRD、TaskSpec、ImplementationReport、TestReport、AcceptanceReport，是数据面。

## 3. 两条核心运行链路

### 3.1 单 Agent ReAct 链路

当前 Agent Loop 属于工程化的 ReAct 变体：模型交替进行决策和行动，但系统只展示模型主动生成的 progress summary，不伪造或暴露隐藏 chain-of-thought。

```text
用户输入
  -> UserPromptSubmit Hook
  -> ContextManager 预算与压缩
  -> BeforeLLM Hook 注入 Task/Artifact/Memory 运行时上下文
  -> LLMClient.chat
  -> 无 Tool Call：输出 final，触发 Stop Hook
  -> 有 Tool Call：PreToolUse Hook
  -> ToolRegistry 执行
  -> PostToolUse Hook
  -> 按 provider 协议批量回灌工具结果
  -> 进入下一 step
```

关键约束：

- `max_steps` 或角色预算防止无限循环。
- 厂商 SDK 重试被收口到自己的 Recovery 策略，避免双重重试。
- Tool Call 与 Tool Result 被视为原子消息组，压缩时不能拆开。
- Hook 注入的上下文只进入本轮请求，不污染 canonical conversation。
- 多工具调用先全部执行，再按 Anthropic 官方要求以同一条 `user` 消息批量回灌 `tool_result`。

### 3.2 串行 Coding Workflow 链路

```text
用户需求
  -> PM：创建 PRD + TaskSpec
  -> Evidence Gate：检查规划产物和 change_required
  -> 为 workflow 创建共享 Git Worktree
  -> Engineer：实际修改代码 + ImplementationReport
  -> Gate：用 Worktree diff 校验是否真的改了代码
  -> QA：只读运行 test/lint + TestReport
  -> Gate：用结构化命令证据决定 pass/fail
  -> fail：把证据和短期 handoff 交给 Engineer 修复
  -> QA 回归，循环次数受限
  -> PM：基于权威 QA 结果生成 AcceptanceReport
  -> 成功时生成 patch，等待显式 apply
```

Workflow 是 Orchestrator-Worker 模式：Orchestrator 决定阶段、输入、角色工具权限、重试和验收；PM、Engineer、QA 是 Worker。角色 Agent 不直接决定整个流程走向。

## 4. 模块地图

| 模块 | 核心职责 | 关键文件 | 深挖文档 |
|---|---|---|---|
| LLM Adapter | 屏蔽 OpenAI/Anthropic 协议差异 | `llm_client.py` | [Runtime](modules/01_runtime_and_provider.md) |
| Agent Runtime | ReAct 循环、工具回灌、事件、预算 | `agent.py` | [Runtime](modules/01_runtime_and_provider.md) |
| Hook / Event | 行为扩展与只读观测分离 | `hooks/`, `agent.py` | [Runtime](modules/01_runtime_and_provider.md) |
| Recovery / Background | 重试、截断续写、后台 Bash | `recovery.py`, `background_jobs.py` | [Runtime](modules/01_runtime_and_provider.md) |
| Context | Prompt section、token 预算、压缩 | `context_manager.py` | [Context](modules/02_context_memory_and_skills.md) |
| Memory | 检索、反思、遗忘、长期知识 | `memory_system.py` | [Context](modules/02_context_memory_and_skills.md) |
| Skill | 发现、按需加载、角色能力增强 | `skill_system.py` | [Context](modules/02_context_memory_and_skills.md) |
| Tools / Security | 文件、命令、验证和权限边界 | `tools/`, `security.py` | [Tools](modules/03_tools_security_and_observability.md) |
| Trace | 层级执行记录、脱敏和成本诊断 | `trace_system.py` | [Tools](modules/03_tools_security_and_observability.md) |
| MCP | 外部工具协议接入 | `mcp_system.py` | [Tools](modules/03_tools_security_and_observability.md) |
| Task | 结构化任务状态和依赖 | `task_system.py` | [Task 专题](task_system_design.md) |
| Artifact | 角色间持久化交付协议 | `artifact_system.py` | [Workflow](modules/04_collaboration_and_workflow.md) |
| Subagent | 隔离上下文的同步委派 | `subagent.py` | [Workflow](modules/04_collaboration_and_workflow.md) |
| Worktree | 修改隔离、diff、patch、显式应用 | `worktree.py` | [Workflow](modules/04_collaboration_and_workflow.md) |
| Workflow | 串行状态机、checkpoint、evidence gate | `serial_workflow.py` | [Workflow](modules/04_collaboration_and_workflow.md) |

## 5. 最重要的设计取舍

### 5.1 为什么只在 LLMClient 做一层适配

OpenAI 和 Anthropic 的差异集中在 API 边界：

- system message 的位置不同；
- Tool schema 的包装不同；
- Tool Call 的字段结构不同；
- Anthropic 的多个 `tool_result` 要在同一条 user content 中回灌；
- stop reason 和 usage 字段不同。

因此项目使用官方 SDK 保留类型、超时和错误语义，只在边界转换成 `LLMResponse`、`LLMToolCall` 和统一消息。Agent Loop 不出现 provider 分支。这比自己维护 HTTP 请求更可靠，也比在所有上层模块传播 SDK 对象更低耦合。

### 5.2 为什么 Tool 定义放在 Python，而不是静态 JSON

工具 schema、执行函数和依赖对象应共同演进。纯 JSON 只能描述 schema，仍需额外映射执行函数，容易产生“定义存在但实现未注册”的漂移。

当前采用：每个工具模块暴露 `register_tools()`，`tools/__init__.py` 负责组合。这样既支持按角色裁剪工具，又能把 `MemoryManager`、`WorktreeManager`、MCP session 等运行时依赖显式注入。

### 5.3 为什么 Hook、Event 和 Trace 分开

- **Hook** 可以改变行为：拒绝、允许、替换结果、注入上下文。
- **Event** 面向 CLI/UI：展示 step、progress、tool call、final，不改变执行。
- **Trace** 面向事后诊断：持久化结构化事件、父子关系、耗时和 usage。

如果三者合并，日志代码可能意外控制流程，UI 也会被持久化格式绑定。当前边界让权限策略可以单测，CLI 可以更换，Trace 写入失败也默认不阻断任务。

### 5.4 为什么不把短期进展全部写入长期 Memory

失败尝试、临时文件和未验证结论会污染长期记忆。项目采用三层策略：

1. 当前 Agent 内由 Context summary 保留近期因果链；
2. Workflow retry/resume 由 checkpoint 和 bounded attempt handoff 保留进展；
3. 只有稳定偏好、项目事实和成功工作流反思进入长期 Memory。

这让“下一次重试不要从头开始”和“下一次会话不要继承错误结论”同时成立。

### 5.5 为什么每个 Workflow 共享一个 Worktree

Engineer 的修改必须立即对 QA 可见，QA 的失败又必须能交给下一轮 Engineer。若每个角色各建 Worktree，就需要在每一阶段做 merge/cherry-pick 和冲突处理，复杂度远超串行 MVP 的收益。

一个 Workflow 一个 Worktree 能提供：

- 与主分支隔离；
- 角色间共享连续文件状态；
- phase-local diff 和最终 patch；
- 成功后由用户显式 apply。

并行 Worker 出现后，才有必要升级为每个并行分支一个 Worktree。

### 5.6 为什么 QA 结论不能只相信 TestReport

LLM 可能把失败描述成成功，也可能漏写 changed files。Evidence Gate 以 Harness 采集的机器证据为准：

- 实现阶段将报告中的 `changed_files` 与真实 Git diff 对齐；
- QA pass 必须有当前阶段成功的 `run_tests` / `run_lint` 事件；
- QA 角色没有源码写权限；
- Acceptance verdict 必须与权威 QA 状态一致。

这体现了项目最核心的工程原则：**模型负责提出和解释，Harness 负责验证和裁决。**

### 5.7 为什么保留本地工具，同时支持 MCP

MCP 适合接入 Context7、GitHub 等外部能力，但本地 Coding Tools 与 Worktree、路径保护、命令日志和验证证据深度耦合。全部改造成 MCP Server 会增加进程通信和部署成本，却不会自动获得更强隔离。

因此 MCP 被作为扩展适配层：远程工具转换成普通 `ToolDefinition`，之后仍走同一套 Registry、Permission Hook 和 Trace。

### 5.8 为什么先做串行而不是 Agent Team

并行会引入共享状态竞争、代码冲突、任务抢占、取消传播和成本放大。串行 PM/Engineer/QA 已足够展示：

- Orchestrator-Worker；
- 角色隔离；
- Artifact 协议；
- 验证反馈闭环；
- checkpoint/resume。

真实评测表明当前主要瓶颈仍是证据一致性和 token 效率，而不是吞吐量，因此并行不是收尾阶段的优先项。

## 6. 典型问题与解决过程

### 6.1 模型报告“完成”，实际没有修改代码

**原因**：最初只检查 `ImplementationReport` 是否存在，相当于让模型自己证明自己。

**修复**：Workflow 接入 Worktree snapshot；Evidence Gate 检查 phase-local Git diff，并将报告的 `changed_files` 与真实变化对齐。无改动任务必须显式声明 `no_change`，需要改动却无 diff 时直接失败。

### 6.2 QA 声称通过，但测试命令失败

**原因**：自然语言 Artifact 和工具事件是两个事实源，早期没有明确优先级。

**修复**：`run_tests` / `run_lint` 返回结构化结果，Workflow 捕获阶段内 verification event；机器失败会把 TestReport 的 pass 降级为 fail，PM 也不能绕过该结论。

### 6.3 Workflow retry 从头重复工作

**原因**：长期 Memory 不适合存临时进展，而新的角色 Agent 又没有上一尝试的完整上下文。

**修复**：为 phase checkpoint 增加 bounded attempt handoff，记录最近工具动作、失败、进展、验证结果和 Worktree changed files；重试和 resume 通过 `<working_state>` 注入。长期 Memory 只在成功后反思，避免失败污染。

### 6.4 在错误 Python 环境中验证，产生假缺依赖

**原因**：直接使用 Harness 自身解释器运行目标仓库测试，无法代表目标项目环境。

**修复**：验证工具优先识别 `pyproject.toml + uv.lock`，执行 `uv run --locked --no-env-file`，并清除共享 `VIRTUAL_ENV`；依赖缺失、lock 过期和环境建立失败分别结构化分类，交给正确角色处理。

### 6.5 依赖缺失时 Agent 试图污染共享环境

**原因**：模型会自然尝试 `pip install`，但这可能修改 Harness 运行环境，也破坏可复现性。

**修复**：安全层阻止共享 Python 环境变更，要求 Engineer 修改项目 dependency manifest/lockfile，或使用工作区内虚拟环境。QA 只报告环境证据，不自行修源码。

### 6.6 长输出超时或 Tool Call 截断

**原因**：模型生成长 Artifact 时会触发 timeout 或 max-token 截断；截断后的 Tool Call 参数可能不是合法 JSON 对象。

**修复**：Recovery 对 timeout/overload 做有界重试与退避；对 output truncation 提升 token 上限并请求 continuation；Tool Call 参数在边界归一化和校验，截断调用不会执行副作用工具。

更完整的时间线和评测数据见 [评测与故障复盘](modules/05_evaluation_and_incidents.md)。

## 7. 当前完成度与证据

### 7.1 已形成的能力闭环

- OpenAI / Anthropic 官方 SDK 与统一 Tool Calling。
- ReAct Agent Loop、Hook、Event、Recovery、Background Bash。
- 工作区安全 Coding Tools 和项目环境验证工具。
- Context compact、Workflow 短期 handoff、长期 Memory 生命周期。
- Task、Artifact、Skill、Subagent、MCP 扩展。
- PM -> Engineer -> QA -> PM 串行 Workflow。
- Git Worktree 隔离、Workflow 持久化与 resume。
- Evidence Gate、结构化 Trace 和自托管评测脚手架。

### 7.2 评测应如何表述

不要把“流程跑到了最后”包装成“自主开发成功”。当前自托管 API Server 评测已经验证了 Harness 能够：

- 在多轮角色切换中保留 Worktree 和 checkpoint；
- 用机器证据推翻错误的 QA pass；
- 识别项目依赖和 lockfile 问题；
- 在失败时阻止错误验收和长期记忆写入；
- 从 Trace 定位 token 成本和重复工作。

但最新候选实现仍未通过外部验收，说明模型执行质量、上下文效率和基准仓库依赖完整性仍需优化。这个结果反而证明 Evidence Gate 没有“为了成功而放水”。

### 7.3 当前限制

- Workflow 仅串行执行，Subagent 也是同步的。
- Context token 估算是近似值，模型真实窗口配置错误时仍可能触发 reactive compact。
- Artifact metadata 目前是轻量字典，尚未全部升级为强类型 schema。
- 长期 Memory 使用文件、关键词和 LLM side-query，没有向量数据库。
- Shell 策略和 Worktree 不是 OS 级沙箱；高风险生产环境仍需容器隔离。
- MCP 主要支持 tools；OAuth、resources、prompts、sampling 尚未形成完整闭环。
- WorkflowStore 是本地文件实现，不支持多进程竞争和分布式锁。
- Trace Markdown 渲染和整体 token 效率仍有已知优化空间。

## 8. 三分钟面试讲法

可以按“问题 -> 架构 -> 难点 -> 结果”组织：

> 我做这个项目是因为普通 Agent Demo 只解决了模型调用工具的问题，但 coding 场景真正困难的是副作用控制、长任务恢复和结果可信度。我用 Python 实现了一套 Multi-Agent Coding Harness，底层通过一层 LLMClient 适配 OpenAI 和 Anthropic 官方 SDK，上层 Agent Loop 统一处理 Tool Call；工具执行统一经过 Registry、Permission Hook、工作区安全检查和 Trace。
>
> 长任务方面，我没有把所有信息都塞进聊天历史，而是拆成三层：ContextManager 管运行内压缩，Workflow checkpoint 和 attempt handoff 管重试与 resume，Memory 只保存跨会话稳定知识。多角色协作采用串行 Orchestrator-Worker：PM 产出 PRD 和 TaskSpec，Engineer 在 Workflow 专属 Worktree 修改代码，QA 只读验证，最后 PM 验收。角色之间用 Artifact 交付，用 Task 表达状态。
>
> 项目中最关键的改进是 Evidence Gate。早期模型会出现“报告写完但代码没改”或“测试失败却声称 pass”，所以我把 Git diff 和结构化 test/lint 事件设为权威事实，模型报告只负责解释。Workflow 支持 checkpoint/resume，失败尝试通过短期 handoff 续接，但不会污染长期 Memory。
>
> 我还用 Agent 自己开发 API Server 做自托管评测。评测确实暴露了依赖环境、上下文成本和重复尝试等问题，也验证了系统能阻止错误验收。这个项目让我关注的不只是 Prompt，而是如何把不稳定模型放进一个可验证、可恢复的工程系统里。

## 9. 面试展开顺序

面试官追问时建议按以下顺序展开，不要一次把所有模块都讲完：

1. 先讲 `LLMClient -> Agent -> ToolRegistry` 最小闭环。
2. 再讲 Hook/Event/Trace 为什么分离。
3. 用一次“假通过”事故引出 Evidence Gate。
4. 用重复尝试问题引出三层状态与 Memory 边界。
5. 用 Worktree 说明副作用隔离和显式 apply。
6. 最后说明为什么当前选择串行，以及如何演进到并行 Agent Team。

详细问题和回答要点见 [秋招面试问题库](modules/06_interview_question_bank.md)。
