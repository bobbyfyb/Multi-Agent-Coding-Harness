# ContextManager 源码精读

> 对应源码：[`src/llm_agent/context_manager.py`](../../src/llm_agent/context_manager.py)  
> 核心测试：[`tests/test_context_manager.py`](../../tests/test_context_manager.py)、[`tests/test_agent.py`](../../tests/test_agent.py)、[`tests/test_memory_system.py`](../../tests/test_memory_system.py)

## 1. 模块目标

`ContextManager` 回答的是：**在有限上下文窗口内，这一次模型请求应该看到什么？**

它统一管理五类内容：

1. 稳定的 system prompt 和具名 Prompt Section。
2. 调用方持有的 canonical conversation history。
3. Task、Artifact、Memory Hook 生成的 request-scoped runtime context。
4. 工具 schema 对请求 token 的额外占用。
5. 长会话中的大工具结果、旧历史摘要与 context-length 恢复。

它不是长期记忆数据库，也不是 Workflow checkpoint：

```text
ContextManager
  负责：当前请求视图、Run 内历史压缩、transcript

WorkflowStore / handoff
  负责：任务特定、跨 Agent Run 的短期工作状态

MemoryManager
  负责：跨会话稳定知识的保存、检索、反思和遗忘
```

模块位于 Agent 与其他状态系统之间：

```text
Prompt Sections ---------------------+
Canonical messages -----------------+|
Task/Artifact/Memory runtime context ||
Tool schemas -----------------------+||
                                      vv
                               ContextManager
                                      |
                     request messages within budget
                                      |
                                      v
                                  LLMClient
```

ContextManager 不决定模型是否调用工具，也不直接执行工具；它只在请求前重建可发送的消息视图，并在必要时减少上下文体积。

## 2. 文件与依赖地图

### 2.1 直接依赖

| 依赖 | 使用内容 | 作用 |
| --- | --- | --- |
| [`llm_client.py`](../../src/llm_agent/llm_client.py) | `LLMClient` | 调用模型生成旧历史摘要 |
| [`tool_registry.py`](../../src/llm_agent/tool_registry.py) | `ToolSpec` | 把工具 schema 计入请求估算，生成兼容工具摘要 |
| [`trace_system.py`](../../src/llm_agent/trace_system.py) | `trace_operation` | 将摘要调用标记为 `context_summary` |

模块本身只使用标准库完成 deepcopy、JSON 序列化、路径处理、原子写入和粗略 token 估算。

### 2.2 主要调用方

| 调用方 | 使用方式 |
| --- | --- |
| [`agent.py`](../../src/llm_agent/agent.py) | 每个 step 调用 `prepare()`、`build_request_messages()` 和 token 估算；context error 时调用 `recover()` |
| [`main.py`](../../main.py) | 为主 Agent 组合 workspace、delegation、background、memory、artifact、skill sections |
| [`subagent.py`](../../src/llm_agent/subagent.py) | 为委派任务组合 mode、workspace、skill 和 recalled memory sections |
| [`serial_workflow.py`](../../src/llm_agent/serial_workflow.py) | 为 PM/Engineer/QA 组合 role、workflow、artifact、skill 和 worktree sections |

### 2.3 源码符号总表

| 符号 | 类型 | 职责 |
| --- | --- | --- |
| `IDENTITY_INSTRUCTIONS` | 常量 | 定义 coding agent 身份和基本行为 |
| `CONTEXT_INSTRUCTIONS` | 常量 | 定义上下文可信度和冲突处理原则 |
| `TOOL_INSTRUCTIONS` | 常量 | 定义何时使用工具及文件/验证约束 |
| `PROGRESS_INSTRUCTIONS` | 常量 | 要求简短可见进展，不暴露隐藏推理 |
| `FINAL_ANSWER_INSTRUCTIONS` | 常量 | 约束最终回答内容 |
| `DEFAULT_BASE_INSTRUCTIONS` | 常量 | 拼接上述五组基础指令 |
| `SUMMARY_SYSTEM_PROMPT` | 常量 | 指导模型生成可继续工作的历史摘要 |
| `PERSISTED_RESULT_PREFIX` | 常量 | 识别已落盘工具结果 marker |
| `COMPACTED_TOOL_RESULT` | 常量 | 无结构旧结果的占位文本 |
| `COMPACTED_TOOL_RESULT_PREFIX` | 常量 | 识别结构化微压缩结果 |
| `Message` | 类型别名 | `dict[str, Any]` 消息结构 |
| `PromptSection` | frozen dataclass | 具名 system prompt 片段 |
| `ContextUpdate` | frozen dataclass | 一次上下文变换的结构化结果 |
| `ContextManager` | dataclass | 请求上下文装配与窗口治理 |
| `ContextManager.__post_init__()` | 方法 | 校验配置并规范化 workdir |
| `resolved_workdir` | 属性 | 返回可用于持久化的绝对工作目录 |
| `compact_threshold_tokens` | 属性 | 计算自动压缩阈值 |
| `bind()` | 方法 | 在 Agent 装配阶段补齐 LLM/workdir |
| `add_section()` | 方法 | 动态增加 Prompt Section |
| `build_system_prompt()` | 方法 | 排序并拼接基础指令和 sections |
| `new_messages()` | 方法 | 创建以 system message 开头的新会话 |
| `estimate_tokens()` | 方法 | 粗略估算 canonical messages token |
| `build_request_messages()` | 方法 | 深拷贝历史并插入临时 runtime context |
| `estimate_request_tokens()` | 方法 | 联合估算消息、runtime context 和工具 schema |
| `prepare()` | 方法 | 每轮请求前执行便宜压缩与自动摘要 |
| `compact()` | 方法 | 无条件尝试一次显式历史摘要 |
| `recover()` | 方法 | Provider context error 后激进恢复 |
| `_summarize_history()` | 方法 | 保留近期原子组并让 LLM 总结旧组 |
| `_hard_trim()` | 方法 | 摘要不可用时按原子组硬裁剪 |
| `_persist_large_tool_results()` | 方法 | 将单条超大工具结果写入文件 |
| `_micro_compact_tool_results()` | 方法 | 压缩较旧工具结果，保留近期结果 |
| `_write_transcript()` | 方法 | 将压缩前完整消息写入 JSONL |
| `build_tool_summary_section()` | 函数 | 生成工具名称/描述的兼容 Prompt Section |
| `build_system_prompt()` | 模块函数 | 兼容旧调用方式的便利入口 |
| `_split_leading_system_messages()` | 函数 | 分离开头连续 system messages |
| `_group_messages()` | 函数 | 构造不拆散 Tool Call/Result 的原子组 |
| `_assistant_tool_call_ids()` | 函数 | 兼容两种 Provider 提取调用 id |
| `_tool_result_ids()` | 函数 | 兼容两种 Provider 提取结果 id |
| `_tool_result_slots()` | 函数 | 定位可替换的所有工具结果 content |
| `_flatten_groups()` | 函数 | 将原子组还原为消息列表 |
| `_content_text()` | 函数 | 把任意工具结果转成文本 |
| `_compacted_tool_content()` | 函数 | 选择路径 marker、结构化摘要或普通占位 |
| `_structured_tool_result_summary()` | 函数 | 保留验证和文件操作的关键证据字段 |
| `_bounded_json_value()` | 函数 | 限制结构化字段的序列化长度 |
| `_json_preview()` | 函数 | 生成有界 JSON 预览 |
| `_safe_component()` | 函数 | 清洗用于文件名的 run/tool id |
| `_display_path()` | 函数 | 优先返回相对 workdir 的展示路径 |
| `_latest_external_user_index()` | 函数 | 找到 runtime context 的插入位置 |
| `_atomic_write()` | 函数 | 临时文件写入后原子替换目标文件 |

## 3. 基础 System Prompt

### 3.1 五组默认指令

`DEFAULT_BASE_INSTRUCTIONS` 由五块文本组成：

| 区块 | 约束重点 |
| --- | --- |
| Identity | coding agent 身份、先观察再行动、保持改动聚焦 |
| Context | 结构化上下文可信但可能过期，新工具证据优先 |
| Tool use | 必要时真实调用工具，不伪造结果，限制工作区，结构化验证 |
| Progress | 工具前给简短进展，不输出 private chain-of-thought |
| Final answer | 总结改动/发现、验证结果和未完成项 |

这些指令属于所有角色共享的最低运行契约。Main Agent、Subagent 和 Workflow Role 再通过 section 增加各自职责。

### 3.2 为什么拆成常量

相比一段难以维护的巨型字符串，按语义拆分可以：

- 单独测试某类行为要求。
- 修改 Progress 规则时不影响 Tool Policy。
- 让默认 Prompt 与角色 Prompt 保持清晰层级。
- 后续将某部分替换成配置文件时有明确边界。

这些常量最终仍会拼成一条 system message，不会额外消耗多个 message role。

## 4. `PromptSection`

```python
@dataclass(frozen=True)
class PromptSection:
    name: str
    content: str
    priority: int = 100
    token_budget: int | None = None
```

### 4.1 字段语义

- `name`：生成 XML-like tag，例如 `<workspace>`。
- `content`：该能力模块提供的静态或构造时内容。
- `priority`：数值越小越靠前。
- `token_budget`：为 section 预算预留的元数据字段。

### 4.2 当前真实行为

`build_system_prompt()` 按 `(priority, name)` 排序，而不是只按添加顺序。空 content 被跳过，其他 section 生成：

```text
<section_name>
content
</section_name>
```

当前 `token_budget` **没有被 ContextManager 执行或裁剪**。Skill Catalog 在生成 content 时已经自行按字符预算截断，然后把该值同时写入字段；ContextManager 目前只保存元数据。因此面试中不能声称系统已经实现“按 section token budget 自动淘汰”。

同名 section 和非法 XML tag 名也没有在这里校验。当前 section 都由可信项目代码构造，而不是用户直接输入。

### 4.3 典型组合

主 Agent：

```text
base
<workspace>
<delegation>
<memory_policy>
<artifact_policy>
<available_skills>
<background_jobs>
```

Workflow Role：

```text
base
<workspace>
<workspace_isolation>
<workflow>
<role>
<workflow_worker_output>
<memory_policy>
<role_skill_*>
<artifact_policy>
<legacy_available_tools_summary>
```

角色差异通过组合 section 表达，不需要在 Agent Loop 中判断 `if role == "engineer"`。

## 5. `ContextUpdate`

```python
@dataclass(frozen=True)
class ContextUpdate:
    messages: list[Message]
    reason: str
    before_tokens: int
    after_tokens: int
    transcript_path: Path | None
    persisted_results: int
    compacted_results: int
    summary_created: bool
    hard_trimmed: bool
    error: str | None
```

它不仅返回变换后的消息，还记录发生了什么：

- `reason`：`preflight`、`auto`、`manual`、`reactive` 等。
- `persisted_results`：完整输出落盘数量。
- `compacted_results`：旧结果被微压缩数量。
- `summary_created`：是否生成 LLM summary。
- `hard_trimmed`：是否执行最终硬裁剪。
- `error`：摘要失败但仍返回便宜压缩结果时保留错误。

`context_rebuilt` 只在 summary 或 hard trim 时为真：

```python
return self.summary_created or self.hard_trimmed
```

原因是这两种操作真正替换了对话结构，Task/Memory Hook 需要把运行态重新注入。仅缩短某条 tool result 不算完整上下文重建。

## 6. ContextManager 配置

| 字段 | 默认值 | 含义 |
| --- | ---: | --- |
| `max_context_tokens` | 100,000 | 声明的模型上下文窗口 |
| `auto_compact_ratio` | 0.75 | 自动摘要触发比例 |
| `keep_recent_groups` | 6 | 摘要时保留的近期协议原子组 |
| `keep_recent_tool_results` | 3 | 微压缩时保留的近期完整工具结果数 |
| `max_tool_result_chars` | 50,000 | 单条工具结果落盘阈值 |
| `tool_result_preview_chars` | 2,000 | 落盘 marker 的预览字符数 |
| `summary_max_tokens` | 2,000 | 摘要模型输出上限 |
| `max_compact_failures` | 3 | 自动摘要连续失败熔断阈值 |
| `max_reactive_retries` | 1 | Agent context error 的恢复次数 |

`__post_init__()` 对所有边界做前置校验，例如 window 必须为正、ratio 必须位于 `(0, 1)`、保留数量不能为负。

`bind()` 只补齐尚未设置的 `llm` 和 `workdir`，不会覆盖调用方显式配置。这让 Agent 可以提供默认依赖，同时尊重为 Subagent/Workflow 定制的 ContextManager。

`_consecutive_compact_failures` 是 Manager 实例级状态。主 Agent 跨多次 `run()` 复用同一个 Manager，因此熔断状态也会跨 CLI turn 保留，直到某次 compact/recover 摘要成功或进程重启。

## 7. 新会话与 Canonical History

### 7.1 `build_system_prompt()`

流程：

```text
base_instructions.strip()
    + sections 按 priority/name 排序
    + 跳过空 section
    + XML-like tag 包装
    -> 单个字符串
```

### 7.2 `new_messages()`

```python
return [{"role": "system", "content": self.build_system_prompt()}]
```

调用方随后追加用户输入。主 CLI 在多轮交互中复用同一个列表，所以 canonical history 包含：

- 初始 system prompt。
- 真实用户输入。
- 模型 assistant 回复和 tool calls。
- tool results。
- background notifications。
- conversation summary / hard compact marker。

BeforeLLM 产生的动态 Task/Memory/Artifact 内容不进入这个列表。

## 8. Runtime Context 的临时注入

### 8.1 `build_request_messages()`

该方法首先 `deepcopy(messages)`，所以：

- 不会修改调用方的 canonical history。
- Anthropic content blocks、OpenAI tool call dict 等嵌套结构也不会共享引用。
- Step Budget 或 Provider 转换可以安全修改请求副本。

每条 runtime string 被包装为：

```python
{"role": "user", "content": runtime_content}
```

### 8.2 为什么插在最新用户请求之前

`_latest_external_user_index()` 从后向前寻找最近一条外部 user text，并跳过 Task、Memory、Summary、Background 等内部 tag。runtime context 插在该位置：

```text
... old history
<relevant_memories>
<current_tasks>
latest real user request
assistant/tool history after that request
```

这样模型把动态上下文视为当前请求的前置背景，同时不会在消息尾部插入内容而拆散：

```text
assistant(tool_call) -> tool_result
```

如果找不到外部用户消息，则将 runtime context 追加到末尾。

### 8.3 为什么不持久化 runtime context

以 Memory Recall 为例，同一条记忆每个 step 都写入 history 会造成：

- 重复 token 成本。
- 记忆内容更新后旧版本仍存在。
- 压缩后恢复出多份相互冲突的状态。
- 内部注入被误当成用户原话。

request-scoped 设计让 Hook 每轮根据当前 Task、Artifact 和 query 重建最新视图。

## 9. Token 估算

### 9.1 `estimate_tokens()`

```python
serialized = json.dumps(messages, ensure_ascii=False, default=str)
ceil(len(serialized.encode("utf-8")) / 3)
```

这是字节数启发式，不是 Provider tokenizer：

- ASCII 大约每 3 字节估为 1 token。
- 中文 UTF-8 通常每字 3 字节，约估为 1 token。
- JSON 字段名和协议包装也被计入。
- 结果至少为 1。

优势是无额外 tokenizer 依赖，OpenAI/Anthropic/自托管模型共用；缺点是模型特定误差不可避免。

### 9.2 `estimate_request_tokens()`

只估 canonical messages 会漏掉动态上下文和工具 schema。该方法序列化：

```python
{
    "messages": build_request_messages(...),
    "tools": tools or [],
}
```

因此 Agent 能在真正请求前预估较完整的输入成本。

### 9.3 阈值与输出空间

默认自动阈值为：

```text
100,000 x 0.75 = 75,000 estimated input tokens
```

剩余 25% 是对输出、Provider 包装和估算误差的整体余量。当前实现没有根据本次 `max_tokens` 显式减去输出预算；这是比例式 headroom，不是精确的 input + output token accounting。

Agent 在 Hook 和工具 schema 加入后还会计算 `reserved_tokens`，再次调用 `prepare()` 为这些额外输入预留空间。

## 10. 压缩总策略

当前策略遵循“便宜且确定性的处理优先，语义摘要最后”：

```text
deepcopy canonical messages
        |
        v
1. persist large tool results       0 LLM call
        |
        v
2. micro-compact old tool results   0 LLM call
        |
        v
3. estimate history + reserved
        |
        | over 75% threshold
        v
4. summarize old message groups     1 LLM call
        |
        v
return ContextUpdate
```

在 Provider 已经拒绝请求时，进入更激进的 reactive 路径：

```text
persist -> micro -> summary with smaller recent tail
                   |
                   | fails or still over threshold
                   v
              hard trim by atomic groups
```

完整对话在破坏性缩减前写入 transcript，保留审计证据。

## 11. `prepare()`：每轮请求前预处理

### 11.1 输入与复制

`reserved_tokens` 不能为负。方法记录原始估算后，对 messages 深拷贝，所有压缩仅作用于 working copy。

### 11.2 先落盘，再微压缩

顺序不能交换：

```text
persist large result -> 保存完整内容
micro compact old result -> prompt 中只留关键证据/占位
```

如果先 micro compact，大结果原文已经丢失，无法再写入 transcript 之外的独立 result 文件。

### 11.3 便宜变换后的 transcript

只要发生落盘或微压缩，`prepare()` 就把**原始 messages** 写入 reason=`preflight` 的 transcript。这样 ContextUpdate 中的工作副本即使只剩占位，原始消息仍有审计副本。

### 11.4 自动摘要判定

条件：

```python
after_tokens + reserved_tokens > compact_threshold_tokens
and consecutive_failures < max_compact_failures
```

若此前没有写 transcript，再以 reason=`auto` 保存一次原始历史，然后调用 `_summarize_history()`。

### 11.5 摘要成功

- 连续失败计数归零。
- 返回 reason=`auto`。
- `summary_created=True`。
- 保留前面发生的 persisted/compacted 计数。

### 11.6 摘要失败

失败计数加一，并按是否已有便宜变换分两种返回：

- 没有便宜变化：返回原 messages 的等价列表和 error。
- 已落盘/微压缩：保留 working copy，reason=`preflight`，同时附 error。

这样摘要服务失败不会撤销已经安全完成的确定性缩减。

达到 `max_compact_failures` 后，后续 `prepare()` 暂停自动摘要，避免每个 Agent step 都浪费一次失败的模型调用；大结果落盘和微压缩仍照常执行。

### 11.7 无变化

既没有便宜变换，也未触发摘要时返回 `None`。Agent 因此无需每轮发“上下文更新”事件。

## 12. `compact()`：显式摘要接口

`compact()` 无视当前 token 是否超阈值，始终：

```text
persist -> micro -> transcript -> summarize
```

调用方可以提供 reason，默认 `manual`。成功时重置失败计数；失败时返回便宜变换后的 messages 和 error，不抛出摘要异常。

当前项目没有把它注册成模型可调用的 `compact` 工具，源码中也没有生产调用方。它是为显式控制或未来管理命令保留的公共 API。

## 13. `recover()`：Context Error 最后防线

Agent 捕获 `LLMContextLengthError` 后调用 `recover()`。

### 13.1 总是保存 reactive transcript

即使没有 LLM、摘要失败或最终硬裁剪，原始对话都先写入：

```text
.llm_agent/transcripts/<timestamp>-<run>-reactive-<uuid>.jsonl
```

### 13.2 更激进的摘要

若绑定了 LLM，调用 `_summarize_history()`，但近期保留组数改为：

```python
min(3, keep_recent_groups)
```

成功且估算不超过 auto threshold 时直接返回 summary，`summary_created=True`。

### 13.3 摘要仍过大或失败

- 摘要成功但仍超过阈值：在摘要结果基础上继续 hard trim。
- 摘要抛异常：记录 error，对便宜压缩后的 working history hard trim。
- 没有 LLM：直接 hard trim。

Hard trim 固定保留最近 2 个协议原子组，并插入：

```xml
<context_compacted mode="hard">
Earlier conversation was removed ...
</context_compacted>
```

`hard_trimmed=True` 告知 Agent：下一轮 Hook 必须重建 Task/Memory 等运行上下文。

`recover()` 自己不循环；重试次数由 Agent 的 `max_reactive_retries` 控制，防止无限压缩重试。

## 14. 协议原子分组

### 14.1 为什么不能按消息下标直接裁剪

以下两组必须保持完整：

OpenAI：

```text
assistant(tool_calls=[call_1, call_2])
tool(tool_call_id=call_1)
tool(tool_call_id=call_2)
```

Anthropic：

```text
assistant(content=[tool_use call_1, tool_use call_2])
user(content=[tool_result call_1, tool_result call_2])
```

留下调用但删除结果，或只保留孤立结果，可能让 Provider 拒绝消息，也会让模型误解工具是否执行。

### 14.2 `_group_messages()`

函数逐条扫描：

1. 当前消息先形成新 group。
2. 若它是含 tool call 的 assistant，提取期望 id 集合。
3. 连续吸收后续包含 tool result id 的消息。
4. 收集到全部期望 id 后结束该 group。
5. 普通消息各自形成一个 group。

这里的 group 是“协议安全单元”，不等同于完整 user/assistant 对话轮次。

### 14.3 双 Provider id 提取

`_assistant_tool_call_ids()` 同时识别：

- OpenAI `message.tool_calls[*].id`
- Anthropic `content[type=tool_use].id`

`_tool_result_ids()` 同时识别：

- OpenAI `role=tool` 的 `tool_call_id`
- Anthropic user content 中 `tool_result.tool_use_id`

Summary 保留近期组和 hard trim 都基于这一统一协议结构。

## 15. 大工具结果落盘

### 15.1 `_tool_result_slots()`

它不依赖 Provider，返回可直接替换 content 的三元组：

```python
(container_dict, "content", tool_call_id)
```

支持 OpenAI role=tool message 和 Anthropic user/tool_result block。

### 15.2 落盘条件与路径

每条结果单独判断：

```text
len(content) > max_tool_result_chars
and not already persisted marker
```

路径：

```text
.llm_agent/context/tool-results/<safe-run-id>/
  <slot-index>-<safe-tool-call-id>.txt
```

marker 保留：

- 相对或绝对 path。
- tool call id。
- 前 `tool_result_preview_chars` 字符。

### 15.3 文件名和写入安全

`_safe_component()` 只保留字母、数字、下划线、点和连字符，去掉首尾点，最多 96 字符，避免 id 形成路径穿越。

`_atomic_write()` 在同目录创建随机 `.tmp`，写完后 `replace()`。这避免进程在正常写入中断时留下半个目标文件；它没有数据库事务或 `fsync` 级 durability。

### 15.4 落盘文件的可访问边界

`.llm_agent` 被 Coding Tool 的敏感路径策略阻止，模型不能直接用普通 `read_file` 打开这些内部文件。因此 marker 的主要作用是：

- Harness 审计和人工诊断。
- 在 prompt 中保留输出存在过及预览。
- 提示模型必要时重新运行原工具。

当前没有专门的 transcript/tool-result retrieval tool。不能把“落盘”表述为模型已经具备随时检索完整输出的能力。

## 16. Tool Result 微压缩

### 16.1 选择范围

`_micro_compact_tool_results()` 收集全部结果 slot，只保留最后 `keep_recent_tool_results` 条完整内容。更旧结果逐条交给 `_compacted_tool_content()`。

长度不超过 120 字符的旧结果不会处理，因为替换占位可能反而更长或损失信息。

### 16.2 三种缩减策略

1. **已持久化结果**：去掉 preview，只保留 path 和 tool id。
2. **可解析的结构化 JSON**：生成 `<compacted_tool_result>` 关键证据。
3. **普通长文本**：替换为通用占位，提示必要时重跑工具。

### 16.3 保留哪些结构化证据

顶层保留：

- `ok`
- 最多 500 字符的 `error`

嵌套 result 优先保留：

```text
path, sha256, outcome, exit_code, timed_out,
duration_ms, report_path, summary,
changed_files, change_count
```

这些字段对应 coding workflow 最关键的事实：操作对象、版本 hash、验证结果、退出码、报告和变更集合。

如果没有这些字段，则保留最多 400 字符的 result preview。这样比统一一句“Earlier result compacted”更适合 Evidence Gate 和故障恢复。

## 17. LLM History Summary

### 17.1 摘要范围

`_summarize_history()`：

1. 保留开头连续 system messages。
2. 将其余 conversation 分成协议原子组。
3. 至少选择一个旧组用于摘要。
4. 保留最后 `keep_recent_groups` 个组原文。
5. 仅将 old groups 发送给摘要模型。

`tail_count` 最多为 `len(groups) - 1`，保证不会出现“没有旧内容却调用摘要”的情况。

### 17.2 摘要请求

```python
llm.chat(
    [
        {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(old_messages)},
    ],
    tools=None,
    max_tokens=summary_max_tokens,
    temperature=0,
)
```

摘要 Prompt 要求保留：

- 当前目标和最新用户请求。
- 用户约束与偏好。
- 技术发现和决策。
- 已检查、创建、修改的文件。
- 命令、测试和验证证据。
- 错误、阻塞和失败方案。
- 进度、剩余工作和已加载 Skill。

调用被 `trace_operation("context_summary")` 标记，因此评测可以区分普通 Agent step 和压缩产生的模型成本。

### 17.3 摘要消息

结果作为 user message 插入：

```xml
<conversation_summary reason="auto" transcript="...">
summary text
</conversation_summary>
```

最终结构：

```text
leading system messages
conversation summary
recent protocol-safe groups
```

摘要为空视为失败，不使用模糊占位继续。

### 17.4 摘要不是可靠存储

LLM summary 可能遗漏或概括错误，因此：

- 完整历史先写 transcript。
- Task 状态来自 TaskManager，不依赖 summary。
- Workflow checkpoint/handoff 保存阶段进度。
- 长期事实由 MemoryManager 筛选后保存。
- Workspace 和 Artifact 仍是可验证事实来源。

Summary 的职责是“让推理继续”，不是成为唯一事实数据库。

## 18. Transcript

`_write_transcript()` 为每条 message 写一行 JSON：

```text
.llm_agent/transcripts/
  <UTC timestamp>-<run-id>-<reason>-<uuid>.jsonl
```

特点：

- 使用压缩前的完整 messages。
- `ensure_ascii=False` 保留中文可读性。
- `default=str` 避免少量 SDK 派生值无法序列化。
- 文件名含微秒、run、reason 和随机后缀，减少冲突。
- 原子写入。

Transcript 当前主要服务审计、评测和人工恢复，不会自动重新注入模型，也没有专门检索工具。与 Trace 的区别是：

| Transcript | Trace |
| --- | --- |
| 保存某次压缩前的消息快照 | 保存整个运行的结构化事件流 |
| 关注模型对话内容 | 关注 Run/LLM/Hook/Tool 生命周期 |
| 只在上下文变化或恢复时产生 | 每次运行持续记录 |

## 19. 与 Agent 的完整协作链

一次普通 step：

```text
1. context.prepare(canonical messages)
2. Agent 原地应用 ContextUpdate.messages
3. BeforeLLM Hooks 召回 Task/Artifact/Memory
4. estimate_request_tokens(messages + runtime + tools)
5. 超阈值则带 reserved_tokens 再 prepare
6. build_request_messages() 生成深拷贝请求视图
7. 临时追加 step budget system instruction
8. LLMClient.chat(request_messages)
9. 成功后继续 Agent Tool/Final 路径
```

Provider 拒绝窗口时：

```text
LLMContextLengthError
-> Agent 发 recovery notice
-> ContextManager.recover(canonical messages)
-> Agent messages[:] 原地替换
-> 下一次 BeforeLLM 重新注入运行态
-> 同一 Agent step 重试
```

ContextManager 方法不原地改调用方 messages；是否采纳 ContextUpdate 由 Agent 决定。Agent 使用 `messages[:] = ...` 保持 CLI 所持列表引用有效。

## 20. 与 Task、Artifact、Memory 的协作

### 20.1 静态 Policy 与动态 Context

这些系统通常提供两类内容：

| 内容 | 注入方式 | 生命周期 |
| --- | --- | --- |
| Memory/Artifact/Skill 使用规则 | PromptSection | Agent 构造后稳定存在 |
| 当前 Task 列表 | BeforeLLM runtime message | 每个请求重新生成 |
| 当前相关 Artifact | BeforeLLM runtime message | 每个请求重新选择 |
| 当前召回 Memory | BeforeLLM runtime message | 按 query/run 重新检索 |
| Subagent delegated memory | PromptSection | 该子 Agent 生命周期内固定 |

ContextManager 只接受这些模块产生的文本，不拥有它们的存储和选择算法。

### 20.2 三层状态

```text
第一层：Run 内 Context
  canonical messages + summary + recent tool evidence

第二层：Workflow Working Memory
  phase checkpoint + attempt handoff + worktree state

第三层：Long-term Memory
  verified preferences/project knowledge/reusable experience
```

第一层解决“当前窗口放不下”，第二层解决“失败/resume 后从哪里继续”，第三层解决“新会话如何复用长期知识”。只实现 Context compact 无法替代后两层。

## 21. 与 learn-claude-code s08 的差异

教学版强调四层：budget、snip、micro、LLM summary，以及 reactive compact 和模型可调用的 compact 工具。

| 维度 | 教学版 s08 | 当前项目 |
| --- | --- | --- |
| 组织位置 | 压缩函数直接放 Agent Loop 文件 | 独立 ContextManager，由 Agent 调用 |
| Provider | Anthropic 对象结构 | OpenAI/Anthropic 统一 Mapping |
| token 估算 | `len(str(messages))` | JSON UTF-8 bytes / 3，并计 runtime/tools |
| 大结果策略 | 只看最后一条 Anthropic user 消息的总预算 | 扫描两种 Provider 的全部 tool result，逐条阈值落盘 |
| snip | 超过固定消息数裁中间 | 没有普通 preflight snip |
| micro | 只处理 Anthropic blocks，统一占位 | 双 Provider，优先保留结构化验证证据 |
| summary | 用全部历史替换成一条摘要 | 保留 system 和近期协议原子组 |
| Tool Call 完整性 | 在切口做局部检查 | 按调用 id 构造完整原子组 |
| reactive | 摘要加最后几条消息 | 摘要后检查阈值，失败则按原子组 hard trim |
| 熔断 | 教学型全局计数 | ContextManager 实例级 auto-summary 熔断 |
| compact tool | 模型可以主动调用 | 当前未注册；由 Harness 自动/响应式控制 |
| Transcript | `.transcripts` | `.llm_agent/transcripts`，run/reason/uuid 命名 |
| 结果输出 | print | ContextUpdate -> AgentEvent -> Trace/CLI |

当前没有 preflight snip 的考虑：按固定消息数量裁剪不理解 token 差异，也可能过早丢掉仍有价值的普通对话。系统优先缩减最占空间的工具结果，只有达到 token 阈值才用摘要重建旧历史；紧急情况下才 hard trim。

没有 compact tool 的考虑：窗口治理属于 Harness 可以确定执行的基础设施，不需要依赖模型意识到自己快超限。这样也避免模型浪费一个 tool step 请求压缩。不过 `compact()` 公共方法仍为手动命令或未来策略入口保留。

## 22. 测试覆盖

### 22.1 `test_context_manager.py`

当前覆盖：

- 新会话 system message。
- 默认 Progress/Tool 指令存在。
- Prompt Section 按 priority 排序和动态添加。
- runtime context 只进入请求副本，并与 tools 一起估算。
- runtime context 不拆散 Tool Call/Result。
- `reserved_tokens` 触发提前摘要。
- legacy tool summary helper。
- OpenAI/Anthropic 旧工具结果微压缩。
- 结构化验证证据在微压缩后保留。
- 大工具结果完整落盘并留下 preview。
- 摘要保留 system 和最近原子组。
- 摘要失败后 reactive hard trim 不拆协议组。

### 22.2 跨模块测试

- `test_agent.py` 验证 context error 后 recover、同一步重试和 Hook 重注入标记。
- `test_memory_system.py` 验证 recalled memory 只进入 request 副本，不污染 canonical history。
- `test_hooks.py` 验证压缩后 Task context 重新注入。
- `test_trace_system.py` 验证 `context_summary` 等 operation metadata。

### 22.3 尚未直接覆盖

- `compact()` 成功、失败和自定义 reason 的独立测试。
- auto-summary 连续失败三次后的熔断与恢复。
- summary 成功但仍超阈值后 hard trim 的分支。
- 同一 assistant 调用多个工具、结果分布在多条消息时的复杂分组矩阵。
- transcript/path marker 与安全策略的端到端可恢复性。
- `PromptSection.token_budget` 的实际执行，因为当前尚未实现。

## 23. 设计思路与取舍

### 23.1 为什么 Context 逻辑集中在一个 Manager

System Prompt、runtime injection、token estimate 和 compact 都会改变最终请求。如果分散在 Agent、Memory Hook 和各工具中，就无法在发请求前计算完整预算，也难以保证压缩顺序。

集中并不意味着 Manager 拥有所有数据。Task/Memory/Artifact 仍各自维护生命周期，只通过 section 或 runtime message 接入。

### 23.2 为什么先压 Tool Result

Coding Agent 上下文的主要膨胀源通常不是用户对话，而是 read/search/test/bash 输出。先处理确定性的工具结果，比过早用 LLM 摘要整段历史更便宜、更可预测，也保留更多用户原话。

### 23.3 为什么摘要要保留近期原文

模型摘要最容易漏掉刚发生的失败、精确参数和下一步动作。保留近期协议组可以：

- 维持 Tool Call/Result 因果链。
- 减少 summary hallucination 的影响。
- 让模型继续最近的执行位置。

### 23.4 为什么仍需要 Hard Trim

摘要本身可能失败、为空或仍过大；Provider 真实窗口也可能小于配置值。Hard trim 不依赖模型，是最后可预测的生存策略。它牺牲早期语义完整性，换取下一次请求能够发送。

### 23.5 为什么不引入独立 `ContextCompactor`

当前规模下，压缩直接依赖 Prompt 装配、token 阈值和消息分组。单独再建一个类会增加依赖传递和状态同步，却没有第二种可替换压缩实现。把相关逻辑集中在 ContextManager 保持了用户要求的简洁性。

当未来需要多种模型 tokenizer、策略插件或异步压缩时，再提取 Strategy/Compactor 才有实际收益。

### 23.6 为什么使用启发式 token 估算

系统同时支持 OpenAI、Anthropic 和 compatible endpoint，绑定一个 tokenizer 可能比字节估算更不准确。比例阈值加 reactive recovery 是低依赖方案。

代价是在 32K 与 100K 配置不一致时仍会超限，所以部署配置必须绑定模型真实 capability。

## 24. 真实评测中的表现

`api-run-05` 提供了重要证据：

```text
251 completed LLM calls
1 context-length failure，成功 reactive recovery
143 context compaction events
  142 preflight micro-compactions
  1 reactive summary
  0 hard trims
4,455,644 input tokens
```

这说明两件事：

1. Reactive recovery 确实能在模型实际窗口小于配置值时保住 Workflow。
2. “发生很多 context_compacted 事件”不代表进行了很多昂贵 LLM summary；多数只是便宜的旧工具结果微压缩。

评测也暴露出成本问题：Artifact Context 在 241 个 role steps 中注入 232 次，总计 882 个 artifact 引用。即使 canonical history 被压缩，重复 runtime context 仍造成 445 万 input tokens。

因此下一步优化不应只是提高摘要频率，而应：

- 让配置窗口与模型真实窗口一致。
- 对 runtime context 做相关性选择、delta 和去重。
- 分开统计 persisted、micro、summary、hard trim。
- 为不同角色设置更贴近任务的工具和 Artifact 上下文预算。

## 25. 当前限制与风险

1. Token 估算不是 Provider tokenizer，且没有显式扣除本次 output max tokens。
2. `PromptSection.token_budget` 目前未执行。
3. Summary 是模型生成的有损信息，可能遗漏关键细节。
4. `prepare()` 的 summary 若仍超阈值不会立即 hard trim，要等真实请求失败进入 recover。
5. `.llm_agent` 被普通 Coding Tool 保护，模型不能直接读取 transcript 或落盘结果。
6. 没有 transcript retrieval/replay API，完整历史主要供审计和人工诊断。
7. Auto-summary 熔断状态保存在 Manager 实例内，没有持久化，也没有显式半开探测计时。
8. ContextManager 非线程安全；当前每个同步 Agent 通常拥有独立实例。
9. 内部 user-message prefix 在 Agent、Context 和不同 Hook 中有重复定义，列表并不完全一致；新增内部 tag 时可能被误判为真实用户输入。
10. `context_compacted` 事件同时覆盖 micro、summary 和 hard trim，不看字段无法判断成本层级。

## 26. 面试追问与回答要点

### Q1：ContextManager 和 MemoryManager 有什么区别？

ContextManager 决定当前请求看到什么，并治理有限窗口；MemoryManager 保存跨会话稳定知识并负责检索/生命周期。Memory 通过 Hook 进入 Context，但 Context 不拥有 Memory 数据。

### Q2：为什么不能每次都直接让 LLM 总结历史？

大多数膨胀来自工具输出，可以通过落盘和确定性微压缩低成本解决。每轮 summary 会增加延迟、token 成本和信息损失，也可能失败。当前策略是 cheap first、semantic summary last。

### Q3：如何避免压缩破坏 Tool Calling 协议？

按 tool call id 将 assistant 调用与后续结果组成原子组，OpenAI 和 Anthropic 都先归一识别。Summary 保留和 hard trim 只在组边界进行，不留下孤立 tool result。

### Q4：Runtime Memory 为什么插到最新用户消息之前？

它是当前请求的背景，应当先于用户要求出现；同时从中间插入可以避免把上下文追加到 assistant tool call 和 tool result 之间。操作发生在深拷贝上，不污染持久历史。

### Q5：大工具结果落盘后模型怎么恢复？

当前 marker 保留 preview 和内部路径，模型可以根据 preview 判断并重跑原工具；由于 `.llm_agent` 被敏感路径保护，它不能直接读取内部文件。落盘完整内容主要用于审计，尚未实现专用 retrieval tool。

### Q6：主动压缩和响应式压缩为什么都需要？

主动压缩减少绝大多数超限；但估算误差、Provider 隐藏开销和错误的窗口配置仍可能导致 context error。响应式 recover 使用更少近期组并在必要时 hard trim，是最后防线。

### Q7：`reserved_tokens` 是什么？

第一次 history prepare 后，Agent 才拿到 Hook runtime messages 和 ToolSpec 的完整大小。它将这些额外输入占用作为 reserved tokens 再调用 prepare，避免历史刚好位于阈值内、加上工具 schema 后立即超限。

### Q8：为什么 summary 之外还保存 transcript？

Summary 是有损且可能出错的。Transcript 保存压缩前原始消息，支持 Trace 复盘和人工诊断。它不是活跃 prompt，也不是当前自动恢复数据源。

### Q9：为什么不用向量数据库保存全部上下文？

Tool Call/Result 顺序、当前任务状态和最近操作需要精确因果关系，不适合只靠相似度召回。向量检索适合长期知识候选；当前执行历史应由近期窗口、summary、Task/Artifact 和 Workflow handoff 共同维护。

### Q10：这个实现相对教学版最重要的增强是什么？

不是压缩层数更多，而是工程边界更稳定：双 Provider 原子分组、完整请求预算、结构化证据保留、ContextUpdate 可观测性、摘要失败熔断，以及与 Task/Memory/Workflow 状态分层。

### Q11：评测中 143 次 context compaction 是否意味着调用了 143 次摘要模型？

不是。142 次是确定性的 tool-result preflight micro-compaction，只有 1 次 reactive summary。事件名称需要结合 `summary_created`、`compacted_results` 和 `hard_trimmed` 解读。

### Q12：下一步最值得优化什么？

先让 context window 配置匹配真实模型，并减少每步重复 Artifact/Memory runtime context。评测证明输入成本主要来自长角色运行和重复注入，不是 summary 调用本身。

## 27. 建议源码阅读顺序

1. 五组默认 instructions：理解 system prompt 基线。
2. `PromptSection`、`ContextUpdate`：理解输入组合和变换输出。
3. `build_system_prompt()`、`new_messages()`：理解 canonical history 起点。
4. `build_request_messages()`、`estimate_request_tokens()`：理解临时上下文和预算。
5. `prepare()`：理解正常请求前的压缩管线。
6. `_persist_large_tool_results()` 与 `_micro_compact_tool_results()`：理解 cheap layers。
7. `_group_messages()` 及 id helpers：理解协议安全边界。
8. `_summarize_history()`：理解有损语义重建。
9. `recover()` 与 `_hard_trim()`：理解 context error 最后防线。
10. `tests/test_context_manager.py`：用最小消息序列验证每条路径。
11. 回到 `Agent._run_loop()`：观察 prepare、reserved budget、runtime injection 和 recover 如何串起来。

下一模块适合梳理 `Memory System`。ContextManager 已经解释了记忆内容如何进入一次模型请求，继续向下可以拆解 MemoryStore、MemoryManager、召回、提取、反思、遗忘和与 Workflow working state 的边界。
