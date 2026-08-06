# Context、Memory 与 Skill

## 1. 为什么要拆成三个系统

三者都可能向 system/request prompt 注入文本，但语义不同：

| 系统 | 回答的问题 | 生命周期 |
|---|---|---|
| Context | 这一步模型需要看到什么？ | 当前 Agent run |
| Memory | 过去有哪些稳定事实值得再次使用？ | 跨会话 |
| Skill | 完成某类任务应该遵循什么方法？ | 可复用能力包 |

如果合并成一个“PromptManager”，很快会失去数据来源、更新规则和可信度边界。当前实现由 `ContextManager` 统一装配请求，但 Memory 和 Skill 各自维护生命周期。

核心文件：

- `src/llm_agent/context_manager.py`
- `src/llm_agent/memory_system.py`
- `src/llm_agent/skill_system.py`
- `src/llm_agent/hooks/memory_hooks.py`
- `src/llm_agent/hooks/task_hooks.py`

## 2. ContextManager

### 2.1 System Prompt 不是一个巨型字符串

`PromptSection` 将 system prompt 拆成具名片段：

```python
@dataclass(frozen=True)
class PromptSection:
    name: str
    content: str
    priority: int
    token_budget: int | None = None
```

典型 section：

- core behavior；
- workspace；
- tool summary；
- delegation；
- background jobs；
- memory policy；
- artifact policy；
- skill catalog；
- Workflow role instructions。

这样做的价值：

- 每类指令有明确 owner；
- 可以按 priority 和 budget 取舍；
- Main Agent、Subagent、PM/Engineer/QA 可以组合不同 section；
- 后续上下文工程不需要修改 Agent Loop。

### 2.2 请求 token 预算包含什么

不能只统计 `messages`。一次真实请求还包含：

- system prompt；
- canonical messages；
- BeforeLLM runtime messages；
- tool schemas；
- 预留输出 token。

`estimate_request_tokens()` 将这些部分一起估算，在请求前决定是否压缩。估算是启发式，不等于 provider tokenizer，因此保留了 context-length error 的 reactive recovery。

### 2.3 消息原子分组

Tool Call 与 Tool Result 存在协议约束。若压缩时留下 assistant 的 tool call，却删除对应结果，下一次请求可能被 provider 拒绝或让模型误判动作未执行。

`_group_messages()` 会把相关 assistant/tool 或 Anthropic tool-use/tool-result 组成原子组；trim、summary 和保留近期消息都以组为单位。

### 2.4 压缩策略

当前不是单一“让 LLM 总结全部历史”，而是分层处理：

1. **大工具结果落盘**：完整内容写入 `.llm_agent` transcript/result 文件，上下文保留路径和摘要。
2. **Tool micro-compaction**：较旧的工具结果替换为结构化预览，保留 ok/error、目标、关键统计等。
3. **History summary**：达到阈值后，让 LLM 总结较早的消息组。
4. **保留近期窗口**：最近用户目标、工具因果链和未完成工作保留原文。
5. **Hard trim**：总结仍超预算时按原子组做最后裁剪。
6. **Reactive recovery**：provider 仍报告 context overflow 时，使用更激进预算重建请求。

压缩前会保存 transcript，避免“为了模型窗口而永久丢失审计信息”。

### 2.5 `ContextUpdate`

每次 prepare/compact 返回结构化变化，例如：

- 是否发生 compact；
- 压缩前后估算 token；
- 是否重建上下文；
- transcript 路径；
- micro-compacted tool result 数量。

Agent 把这些信息转成 context event 和 Trace，便于评测压缩频率。

### 2.6 当前问题

自托管评测暴露了两个限制：

- 配置的 context window 与真实模型窗口不一致时，预估再准确也会触发 reactive compact；
- Artifact 等 runtime context 每步重复注入，会产生大量 input token，即使 canonical history 已压缩。

后续优化应先做 context source 去重、delta 注入和真实 tokenizer 适配，而不是简单提高 summary 频率。

## 3. 三层状态与记忆

### 3.1 第一层：运行内短期上下文

由 `ContextManager` 管理：

- 当前用户目标；
- 最近动作和结果；
- 历史 summary；
- 当前请求动态上下文。

它解决同一个 Agent run 内“窗口装不下”的问题，但进程结束后不能作为恢复依据。

### 3.2 第二层：Workflow 工作记忆

由 `WorkflowStore` 的 phase checkpoint 和 `attempt_handoffs` 管理：

- phase 状态与尝试次数；
- 最近执行的工具动作；
- 已发现失败；
- 当前验证结果；
- Worktree changed files；
- 下一尝试应继续的位置。

它是短期、任务特定、可跨进程的 working state。内容有数量和长度上限，避免把完整 Trace 再塞回模型。

### 3.3 第三层：长期 Memory

由 `MemoryManager` 管理跨会话稳定知识：

- 用户偏好；
- 用户明确反馈；
- 项目约定和稳定事实；
- 可复用 reference；
- 成功 Workflow 中经过证据支持的经验。

临时失败、未经验证的猜测、一次尝试进度不应进入这一层。

### 3.4 为什么不是“短期和长期都放向量库”

短期状态需要精确恢复和确定性覆盖，最适合 checkpoint；长期知识才需要检索。向量相似度不能表达 phase 已完成、attempt 次数和 Worktree 路径等操作状态。

## 4. Memory System 实现

### 4.1 数据模型

`Memory` 包含：

- `id`、`name`、`description`、`body`；
- type：`user`、`feedback`、`project`、`reference`；
- status：`active`、`stale`、`archived`；
- `pinned`、`confidence` 和 evidence；
- source、source run、created/updated time；
- `last_used_at`、`use_count` 和 status reason 等生命周期信息。

Memory 不是原始聊天记录，而是经过筛选的知识条目。

### 4.2 Store 与 Manager 分层

- `MemoryStore` 负责 Markdown + YAML frontmatter 的读写、删除、索引重建和原子写入。
- `MemoryManager` 负责业务语义：remember、search、retrieve、extract、reflect、stale、archive、forget。

这样存储格式可以替换，而生命周期规则和检索策略不必进入文件 I/O 代码。

### 4.3 为什么先用文件存储

当前规模下文件方案更合适：

- 可读、可手工审查，适合简历项目展示；
- 无外部数据库依赖；
- Git/备份友好；
- 原子写足以满足单进程 CLI；
- 实际瓶颈是写入质量，不是百万级检索吞吐。

当 memory 数量增长、语义召回成为明确瓶颈时，可以在 Store 上增加 embedding index，而不改上层 Memory contract。

### 4.4 记忆写入

有三条路径：

1. **显式工具**：模型调用 `memory_remember`，适合用户明确要求记住的内容。
2. **Turn extraction**：Stop Hook 在用户明确表达偏好/反馈时提取，启发式先判断是否值得调用 LLM。
3. **Workflow reflection**：仅在权威 QA pass 和成功验收后，从 Artifact 与验证证据提炼可复用项目经验。

写入前检查：

- 是否包含 API key、token、密码、私钥等敏感模式；
- 是否与已有 memory 重复或冲突；
- 类型与 confidence 是否合法；
- 失败 Workflow 是否试图写入未验证结论。

### 4.5 召回机制

召回不是把整个 `MEMORY.md` 注入每一步：

1. 过滤 active memory；
2. 将 pinned memory 作为优先候选；
3. 可选用一次轻量 LLM side-query，根据 name、description、type 和 confidence
   从目录中选择相关 id；
4. LLM 选择失败时，回退到关键词、confidence 和历史 use_count 评分；
5. 按 token/字符预算截断；
6. 由 MemoryContextHook 作为 runtime context 注入。

这是 hybrid retrieval 的轻量版本。LLM side-query 只做选择，不生成事实，最终内容仍来自文件中的 memory。

### 4.6 反思、遗忘与冲突

- **Reflection**：把成功结果提炼为稳定经验，不保存完整过程。
- **Mark stale**：事实可能过期或被新证据否定时保留记录但不默认召回。
- **Archive**：不再活跃但仍值得审计。
- **Forget**：显式物理删除。
- **Restore**：将 stale/archived 条目恢复为 active。

实时文件、Git diff 和验证结果始终高于 Memory。Memory 是先验提示，不是当前仓库事实。

### 4.7 评测中的行为

在失败 Workflow 中长期 Memory 没有新增条目，这是预期行为，不代表 Memory 未生效。真正需要续接的失败进度应进入 attempt handoff。评测曾出现重试从头开始，修复点也因此落在 Workflow 工作记忆，而不是放宽长期 Memory 写入。

## 5. Skill System

### 5.1 Skill 的定位

Skill 是可复用的方法和领域知识，不是独立 Agent，也不是自动获得额外权限的插件。

典型内容：

- PRD 写作流程；
- 前端 UI 规范；
- 测试策略；
- 某技术栈的代码约定；
- 配套 reference 和脚本说明。

### 5.2 发现与加载

`SkillRegistry` 扫描 `.llm_agent/skills` 下的 `SKILL.md`：

- 解析 frontmatter 中的 name/description；
- 建立轻量 catalog；
- 启动时只注入 catalog，不注入全部正文；
- `skill_load` 按需返回 instructions；
- `skill_read_resource` 读取 Skill 目录中的补充文档；
- 对名称、路径、文件大小和总预算做限制。

多层目录只要每个独立 Skill 有自己的 `SKILL.md` 就能分别发现。一个 Skill 内可以包含 `REFERENCE.md`、模板和子目录资源。

### 5.3 Required 与 Optional Skill

- **required skill**：Workflow 配置为某角色指定，构建角色 Context 时直接注入；它是 Orchestrator 的确定性路由。
- **optional skill**：Agent 从 catalog 判断需要后调用 `skill_load`；用户能在 Tool Call 日志中看到。

所有角色都能拥有 skill，不只 PM。角色配置只改变知识注入，不自动扩大工具 allowlist。

### 5.4 带脚本 Skill 是否支持

当前支持“读取脚本和执行说明”，脚本是否能运行取决于角色本来拥有的 bash/file 工具和权限策略。Skill 本身不能绕过 Permission Hook，也不会在加载时自动执行任意代码。

这是有意的能力与权限分离：

```text
Skill 告诉 Agent 怎么做
Tool 决定 Agent 能做什么
Permission/Security 决定这次是否允许做
```

### 5.5 为什么不启动时加载所有 Skill

热门 Skill 可能有大量参考文档。全量加载会：

- 消耗 system token；
- 让无关规范互相干扰；
- 每步重复支付输入成本；
- 降低模型找到当前约束的能力。

因此使用 metadata discovery + lazy loading，并允许 Orchestrator 对关键角色做 required 注入。

## 6. Task Intent Classifier 的位置

`TaskPlanningHook` 需要判断用户输入是否是值得建立计划的 coding task。项目没有为这一个分类启动完整 Subagent，而是使用轻量 `TaskIntentClassifier`：

- 没有新的 Tool Registry、ContextManager 和多步循环；
- 只做一次受控 LLM classification；
- 失败时回退到启发式判断；
- 结果用于是否提醒 Task 规划，不直接执行副作用。

它体现了一个原则：需要 LLM 灵活性，不等于必须实例化一个 Agent。

## 7. 设计评价

### 优点

- Context、工作状态和知识记忆边界清楚。
- Memory 可审计，失败结果不会自动污染长期知识。
- Skill 采用渐进披露，能用于所有角色且不绕过权限。
- 状态组件都能独立测试，不依赖一个巨型 prompt。

### 局限

- token 估算和关键词检索仍是轻量实现。
- LLM summary/selection 本身可能失真，需要原始 transcript 和 source 保底。
- Skill 没有依赖解析、版本锁定和可信签名。
- Memory 文件存储不支持多进程并发写。
- runtime Artifact/Memory 注入仍有进一步做 delta caching 的空间。

## 8. 面试追问

**Context 和 Memory 的区别是什么？**

Context 是本次请求的工作集，Memory 是跨会话可检索知识。Context 可以包含未经验证的当前尝试，长期 Memory 不应包含这些临时状态。

**为什么失败 Workflow 不做 reflection？**

失败中也有经验，但自动区分“可复用教训”和“模型误判”风险较高。当前先以成功证据作为写入门槛；失败信息保存在 Trace/checkpoint，后续可做人工确认的 failure lesson。

**为什么不用向量数据库？**

当前条目数量小，文件 + lexical + LLM selection 已足够，且可读可审计。向量库解决的是规模和语义检索，不解决记忆质量、过期和冲突；应在这些生命周期规则稳定后再加。

**Summary 会不会丢信息？**

会，所以系统保留近期原文、工具原子组、完整 transcript 和落盘的大结果。Summary 是用于模型工作集的有损视图，不是唯一记录。
