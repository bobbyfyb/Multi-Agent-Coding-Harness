# Agent 与 ReAct Loop 源码精读

> 对应源码：[`src/llm_agent/agent.py`](../../src/llm_agent/agent.py)  
> 核心测试：[`tests/test_agent.py`](../../tests/test_agent.py)、[`tests/test_error_recovery.py`](../../tests/test_error_recovery.py)、[`tests/test_hooks.py`](../../tests/test_hooks.py)、[`tests/test_trace_system.py`](../../tests/test_trace_system.py)

## 1. 模块目标

`Agent` 是单个智能体的运行时编排器。上一篇介绍的 `LLMClient` 负责“如何调用模型”，而 `Agent` 负责“拿到模型决策以后如何推进任务”。

它组织的主链路是：

```text
准备上下文
    -> 调用模型
    -> 观察模型输出
    -> 执行工具动作
    -> 回灌工具结果
    -> 再次调用模型
    -> 返回最终回复
```

从行为模式看，这是一个工程化的 ReAct Loop：

- **Reason/Progress**：模型在带工具调用的响应中输出简短进展文本。
- **Act**：模型通过结构化 Tool Calling 选择工具和参数。
- **Observation**：Harness 执行工具并把结果回灌模型。
- **Repeat/Answer**：继续循环，直到模型不再调用工具并给出最终回复。

它不是严格复刻论文中显式的 `Thought:` 文本。系统展示的是模型主动提供的 progress summary，不暴露隐藏 Chain of Thought。

`Agent` 还把多个横切能力插入这条主链：

- Context 构建、主动压缩和超限恢复。
- BeforeLLM、PreToolUse、PostToolUse、Stop Hook。
- 工具权限检查和结果替换。
- 网络恢复事件、输出截断恢复。
- Trace 与面向 CLI 的 Agent Event。
- 后台任务完成通知。
- 父子 Agent 身份和运行关联。

它不负责以下内容：

- Provider 协议转换和传输重试：由 `LLMClient` 负责。
- 工具注册和异常包装：由 `ToolRegistry` 负责。
- Hook 的具体策略：由各 Hook 实现负责。
- 长期记忆存储、Task 或 Artifact 业务逻辑：Agent 只提供注入点。
- 多角色阶段转移：由 `SerialCodingWorkflow` 负责。

## 2. 文件与依赖地图

### 2.1 直接依赖

| 模块 | Agent 使用的内容 | 作用 |
| --- | --- | --- |
| [`llm_client.py`](../../src/llm_agent/llm_client.py) | `LLMClient`、响应/错误/工具参数标记 | 获取模型决策并回灌 Provider 正确消息 |
| [`context_manager.py`](../../src/llm_agent/context_manager.py) | `ContextManager`、`ContextUpdate` | 构建请求上下文、估算 token、压缩和恢复 |
| [`hooks/__init__.py`](../../src/llm_agent/hooks/__init__.py) | `HookManager`、`HookContext` | 注入动态上下文、权限、结果后处理和停止逻辑 |
| [`tool_registry.py`](../../src/llm_agent/tool_registry.py) | `ToolRegistry` | 提供 ToolSpec 并执行工具 |
| [`recovery.py`](../../src/llm_agent/recovery.py) | Policy、State、Scope、Notice | 共享一次 Run 的恢复状态 |
| [`trace_system.py`](../../src/llm_agent/trace_system.py) | Trace Scope、operation、记录函数 | 记录 Run、Step、Hook、LLM 和 Event |
| [`background_jobs.py`](../../src/llm_agent/background_jobs.py) | Manager、通知格式化 | 在下一轮把已完成后台任务注入对话 |

### 2.2 上层调用方

| 调用方 | 使用方式 |
| --- | --- |
| [`main.py`](../../main.py) | 构造主 Agent；在 CLI 各轮间持有同一个 `messages` 列表 |
| [`subagent.py`](../../src/llm_agent/subagent.py) | 创建带 `parent_run_id` 和更深 `depth` 的子 Agent |
| [`serial_workflow.py`](../../src/llm_agent/serial_workflow.py) | 为 PM、Engineer、QA 创建不同工具、Prompt、Hook 和 step budget 的角色 Agent |

Agent 自身不区分“主 Agent”“Subagent”或“角色 Agent”。这些身份差异都通过构造参数和依赖装配表达。

### 2.3 源码符号总表

| 符号 | 类型 | 职责 |
| --- | --- | --- |
| `AgentEventType` | Literal 类型 | 定义可展示和可追踪的运行事件集合 |
| `AgentRunStatus` | Literal 类型 | `completed`、`incomplete`、`max_steps` |
| `AgentEvent` | frozen dataclass | 一条结构化运行事件 |
| `AgentCallback` | Callable 别名 | 事件消费者接口 |
| `ToolExecutionContext` | frozen dataclass | 向需要上下文的工具传递 Run/Agent/Workdir 信息 |
| `AgentRunResult` | frozen dataclass | 一次 Agent Run 的结构化返回值 |
| ANSI 常量 | 常量 | CLI 事件颜色和样式 |
| `TOOL_CALL_PREVIEW_CHARS` | 常量 | 工具参数预览上限，当前为 400 |
| `TOOL_RESULT_PREVIEW_CHARS` | 常量 | 工具结果预览上限，当前为 600 |
| `Agent` | dataclass | 单 Agent ReAct 运行时 |
| `Agent.__post_init__()` | 方法 | 规范化 workdir，绑定 Context 和 RecoveryPolicy |
| `Agent.new_messages()` | 方法 | 创建新会话消息列表 |
| `Agent.run()` | 方法 | 建立 Run/Trace/Recovery Scope 并处理顶层结果 |
| `Agent._run_loop()` | 方法 | Agent 状态机核心 |
| `Agent._invalid_tool_arguments_result()` | 方法 | 把兼容模型非法参数转为可回灌错误 |
| `Agent._execute_tool_call()` | 方法 | 执行单次或适配层标记的批量工具输入 |
| `Agent._emit()` | 方法 | 自动补齐当前 Agent 身份后发送事件 |
| `Agent._forward_recovery_notice()` | 方法 | 把 Recovery Notice 转成 Agent Event |
| `Agent._emit_context_update()` | 方法 | 把 ContextUpdate 拆成可见事件 |
| `Agent._emit_memory_hook_result()` | 方法 | 展示 Stop Hook 的记忆提取结果 |
| `Agent._collect_background_notifications()` | 方法 | 拉取并注入已结束后台任务通知 |
| `print_agent_event()` | 公共函数 | 把结构化事件渲染成彩色 CLI 输出 |
| `_print_recovery_event()` | 函数 | 渲染不同恢复动作 |
| `_emit()` | 函数 | 同时记录 Trace 并调用事件回调 |
| `_hook_messages()` | 函数 | 从 HookResult 中安全提取运行时消息 |
| `_runtime_context_event_type()` | 函数 | 根据 XML-like tag 识别动态上下文类型 |
| `_runtime_context_key()` | 函数 | 提取上下文 tag 作为事件去重 key |
| `_latest_external_user_message()` | 函数 | 跳过内部消息，寻找本轮真实用户输入 |
| `_combine_outputs()` | 函数 | 拼接截断恢复产生的多个文本片段 |
| `_merge_usage()` | 函数 | 累加各模型调用的数值 usage 字段 |
| `_event_prefix()` | 函数 | 为子 Agent 事件增加 agent id 前缀 |
| `_step_budget_instruction()` | 函数 | 在剩余 6 步和最后一步提醒模型收敛 |
| `_append_system_instruction()` | 函数 | 将 step budget 临时加入 system message |
| `_tool_result_color()` | 函数 | 根据工具结果语义选择绿/黄/红色 |
| `_format_event_payload()` | 函数 | JSON 序列化并截断冗长事件内容 |

## 3. 公共数据结构

### 3.1 `AgentEvent`

```python
@dataclass(frozen=True)
class AgentEvent:
    type: AgentEventType
    step: int
    data: dict[str, Any]
    agent_id: str = "agent"
    run_id: str = ""
    parent_run_id: str | None = None
    depth: int = 0
    duration_ms: float | None = None
```

它将“运行行为”和“如何展示”分离：

- Agent 只发 `tool_call`、`progress`、`final` 等结构化事件。
- CLI 使用 `print_agent_event()` 消费事件。
- Trace 使用同一事件生成持久化记录。
- Workflow 可以包装回调，并补充阶段级 Trace 或状态。

身份字段支持嵌套执行：

- `agent_id`：如 `main`、`pm`、`engineer`、`qa`、`explore-xxxx`。
- `run_id`：当前 Agent Run。
- `parent_run_id`：父 Workflow 或父 Agent Run。
- `depth`：CLI 是否显示 `[agent_id]` 前缀，也用于 Trace 层级。

`AgentEventType` 中有些事件由 `Agent` 直接发出，有些由持有 `ToolExecutionContext.on_event` 的工具或 SubagentRunner 发出。把事件词汇集中在这里，可以让 CLI 使用同一个渲染入口。

### 3.2 `ToolExecutionContext`

```python
@dataclass(frozen=True)
class ToolExecutionContext:
    run_id: str
    agent_id: str
    parent_run_id: str | None
    depth: int
    step: int
    workdir: Path
    on_event: AgentCallback | None
    metadata: dict[str, Any]
```

普通纯函数工具只需要模型参数；Subagent、后台任务、Worktree 等工具还需要当前运行身份和目录。`ToolRegistry` 使用 `requires_context` 决定是否把该对象作为 `context=` 注入。

这样工具 schema 中不必暴露 `run_id`、`workdir` 等 Harness 内部参数，模型也无法伪造它们。

虽然 dataclass 是 frozen，`metadata` 字典本身仍可变，所以依然是浅层不可变。

### 3.3 `AgentRunResult`

```python
@dataclass(frozen=True)
class AgentRunResult:
    status: AgentRunStatus
    content: str
    steps: int
    tool_calls: int
    usage: dict[str, Any]
    run_id: str
    agent_id: str
```

三种状态：

- `completed`：模型最终返回无工具调用的完整回答。
- `incomplete`：输出截断恢复耗尽，但已经得到可用的部分文本。
- `max_steps`：连续工具轮次达到预算，没有得到最终回答。

上层不应只看 `content` 判断成功。Workflow 和 Subagent 可以根据 `status` 决定重试、失败或形成交接结果。

## 4. Agent 配置与初始化

### 4.1 dataclass 字段

| 字段 | 含义 |
| --- | --- |
| `llm` | 统一模型客户端 |
| `tools` | 当前 Agent 可见且可执行的工具集合 |
| `context_manager` | ContextManager，或简化构造时的一段 system 文本 |
| `max_steps` | 工具决策轮次预算；`None` 表示不设内部上限 |
| `hooks` | 生命周期扩展和权限策略 |
| `workdir` | 工具和 Context 的工作目录 |
| `agent_id` | 当前 Agent 身份 |
| `parent_run_id` | 父运行关联 id |
| `depth` | 嵌套深度 |
| `recovery_policy` | 输出截断等 Agent 层恢复策略 |
| `background_jobs` | 可选后台任务管理器 |

主 CLI Agent 使用 `max_steps=None`，允许长会话自主完成；Workflow 角色和 Subagent 使用有限预算，避免某一阶段无限循环。

### 4.2 `__post_init__()`

初始化执行三项规范化：

1. `workdir` 为空时取当前目录，否则转为绝对路径。
2. `context_manager` 若为字符串，则自动包装成 `ContextManager`。
3. 未显式传 `recovery_policy` 时，优先复用 `llm.recovery_policy`，否则使用默认策略。

如果已经传入 ContextManager，则调用 `bind(llm=..., workdir=...)`。这样同一个 Context 类型既能独立构造，也能由 Agent 完成依赖绑定。

### 4.3 `new_messages()` 与消息所有权

`new_messages()` 委托给 ContextManager 创建新消息列表，通常以组装好的 system message 开始。

关键设计是：**Agent 不把历史保存在自身字段中，而是原地修改调用方传入的 `messages` 列表。**

CLI 的会话方式：

```python
messages = agent.new_messages()

while True:
    query = read_user_prompt()
    messages.append({"role": "user", "content": query})
    agent.run(messages)
```

因此连续用户输入会共享历史。如果调用方每次都重新调用 `new_messages()`，那就是新会话。这个选择让 Session、持久化和分支策略留在上层，而 Agent 只处理一次 Run。

## 5. `run()`：一次 Run 的外壳

`run()` 不包含具体 ReAct 决策，它负责建立一次运行的公共上下文。

### 5.1 生成运行身份

如果调用方不提供 `run_id`，自动生成：

```text
run-<12位随机十六进制>
```

Trace 优先使用显式 `trace`，否则复用当前 ContextVar 中的 Recorder。这样：

- CLI 可以为每个用户输入创建独立 Trace。
- Workflow 可以让各角色共享同一 Recorder，同时使用不同 run id。
- Subagent 可以自然继承外层 Trace。

### 5.2 建立两个 Scope

```python
with (
    trace_scope(...),
    recovery_scope(recovery_state, recovery_callback),
):
    ...
```

两个 context manager 基于 `ContextVar` 保存当前运行态：

- `trace_scope`：让 LLM、Hook、Tool 等深层代码无需逐层传参即可取得当前 run/agent/step。
- `recovery_scope`：让 `LLMClient` 和 Agent 共享同一个 `RecoveryState`，并把 notice 转发为 AgentEvent。

这种并列 `with` 等价于嵌套两个 context manager，但层级更平。

### 5.3 Run 生命周期 Trace

进入 Scope 后记录：

```text
run.started
    -> _run_loop()
run.completed 或 run.failed
```

完成记录包含状态、steps、tool_calls、usage 和内容摘要。异常会原样继续抛给 CLI、Subagent 或 Workflow。

当调用方显式传入 Trace 且当前 Agent 是根 Agent 时，`finally` 会渲染 Markdown Trace。CLI 外层仍会再次调用渲染，当前实现依赖渲染操作可重复执行。

## 6. `_run_loop()` 的状态变量

进入循环前只获取一次 `tool_specs`，并初始化：

| 变量 | 作用 |
| --- | --- |
| `tool_call_count` | 本次 Run 已处理的工具调用数量 |
| `usage` | 累计每次成功模型响应的 usage |
| `last_content` | 最近可返回的文本，供 max_steps/incomplete 使用 |
| `reactive_retries` | Context length 的连续恢复次数 |
| `pending_context_rebuilt` | 将本轮恢复结果告知下一轮 Hook |
| `turn_user_text` | 本次 Run 开始时最近的真实用户输入 |
| `emitted_runtime_context` | 对 Task/Memory/Artifact 上下文展示事件去重 |
| `called_tools` | 本 Run 已尝试调用过的工具名集合 |
| `recovery_state` | 输出升级、续写和 partial output 状态 |
| `recovery_policy` | Agent 层恢复预算 |
| `output_max_tokens` | 输出升级后覆盖 LLMClient 默认值 |
| `step_index` | 已完成并回灌的工具决策轮次 |

`turn_user_text` 通过 `_latest_external_user_message()` 得到。函数会跳过 `<current_tasks>`、`<relevant_memories>`、`<conversation_summary>`、`<background_notifications>` 等 Harness 内部 user message，避免 Hook 把内部上下文误判为用户的新要求。

## 7. Step 的精确定义

循环条件：

```python
while self.max_steps is None or step_index < self.max_steps:
    step = step_index + 1
```

`step_index` 只在以下流程完整结束后增加：

```text
LLM 返回 tool_calls
-> 所有工具处理完成
-> 工具结果成功加入 messages
-> step_index += 1
```

以下情况不会增加 `step_index`：

- Context length error 后压缩并重试。
- 首次输出截断后提升 max_tokens 并重试。
- 文本截断后添加 continuation prompt。
- 当前 step 直接返回最终答案。

所以：

```text
AgentRunResult.steps != 一定等于 SDK 请求次数
```

更准确地说，`steps` 表示模型任务推进轮次；恢复性请求被视为同一步的重试。对于直接回答，结果 `steps=1`；对于两轮工具调用后触发预算退出，结果 `steps=2`。

## 8. 每个 Step 的完整流程

### 8.1 更新 Trace Step

```python
update_trace_context(step=step)
```

后续 LLM、Hook、Tool 和 Recovery Trace 自动带上当前 step，不必在每个函数调用里重复传递。

### 8.2 收集后台任务通知

`_collect_background_notifications()`：

1. 按 `owner_agent_id` 拉取完成通知。
2. 格式化为 `<background_notifications>` user message。
3. 追加到持久的 `messages`。
4. acknowledge 已消费通知。
5. 发出 completed/failed/cancelled 事件。

通知在 step 开头注入，模型下一次调用即可看到结果。它与 BeforeLLM runtime message 不同：后台通知进入会话历史，因为它是一个异步 Observation，后续 ContextManager 可以再压缩它。

### 8.3 Context 主动准备

第一次调用：

```python
context_update = context_manager.prepare(messages, run_id=...)
```

ContextManager 可以持久化超长工具结果、生成摘要或硬裁剪。Agent 使用切片赋值：

```python
messages[:] = context_update.messages
```

这样保留原列表对象身份。CLI 或其他调用方持有的仍是同一个 messages，而不是被替换成仅 Agent 内部可见的新列表。

`_emit_context_update()` 根据更新内容发出：

- `tool_result_persisted`
- `context_compacted`
- `context_compact_failed`

### 8.4 构造 HookContext

每个 step 创建新的 `HookContext`，metadata 包括：

```text
run_id
agent_id
parent_run_id
depth
context_compacted
turn_user_text
called_tools
```

`called_tools` 是同一个持续增长的 set，因此 Task/Memory Hook 可以知道本 Run 已采取过哪些动作。

### 8.5 `BeforeLLM` Hook 与临时上下文

默认 Hook 可以生成：

- `<current_tasks>` / `<task_reminder>`
- `<relevant_artifacts>`
- `<relevant_memories>`

`_hook_messages()` 只接受 `HookResult.data["messages"]` 中非空字符串，忽略错误类型。

这些消息随后传给：

```python
context_manager.build_request_messages(
    messages,
    runtime_messages=runtime_messages,
)
```

它们只存在于本次请求副本中，不写入持久 messages。这避免每一步重复累积同一 Task/Memory 上下文，也允许记忆召回结果随最新任务动态变化。

Agent 会根据 XML-like tag 发出 task/memory/artifact 事件。`emitted_runtime_context` 只负责减少重复展示；即使事件不再打印，Hook 生成的内容仍会注入当前模型请求。

### 8.6 包含工具 schema 的二次 token 检查

第一次 `prepare()` 主要处理历史。Hook 消息和完整 ToolSpec 加入后，请求可能再次超过阈值，因此 Agent 调用：

```python
estimate_request_tokens(
    messages,
    runtime_messages=runtime_messages,
    tools=tool_specs,
)
```

若超过 `compact_threshold_tokens`，它根据“总请求估算与历史估算的差值”计算 `reserved_tokens`，再次调用 `prepare()`，为 runtime message 和工具 schema 预留空间。

这是 Context 与 Agent 的重要协作边界：ContextManager 管理压缩算法，Agent 知道本轮还会附加哪些额外请求内容。

### 8.7 Step Budget 提醒

`_step_budget_instruction()` 只在两个节点返回提示：

- 剩余 6 个模型轮次：停止宽泛探索，聚焦实现、验证和交接。
- 最后一个模型轮次：禁止再调用工具，诚实返回最终状态。

提示被包装在 `<step_budget>` 中，临时追加到 request 的 system message，并发出 progress event。它不会污染持久历史。

这替代了教学型“连续三次提醒”：提醒由剩余预算触发，与任务长度相关，也不会每轮重复打扰模型。

当 `max_steps=None` 时不生成预算提示。

### 8.8 调用模型

Agent 先发出 `step` 事件，再在 Trace operation 下调用：

```python
with trace_operation("agent_step", request_tokens=request_tokens):
    response = llm.chat(
        request_messages,
        tools=tool_specs,
        tool_choice="auto",
        max_tokens=output_max_tokens,  # 仅升级后传入
    )
```

Agent 始终把当前 ToolRegistry 的 schema 提供给模型，并允许模型自主决定是否调用。

## 9. Context Length 的响应式恢复

主动估算无法覆盖所有 Provider 的真实 tokenizer 和隐藏开销，因此 `LLMClient` 仍可能抛出 `LLMContextLengthError`。

Agent 的处理流程：

```text
捕获 LLMContextLengthError
    |
    | reactive budget available
    v
发 context_compact recovery notice
调用 context_manager.recover()
原地替换 messages
标记 pending_context_rebuilt
同一 step 重试
```

下一次循环构造 HookContext 时，`context_compacted=True`。Task/Memory 等 Hook 可以据此重新注入压缩后必须恢复的运行状态。

超过 `max_reactive_retries` 后，Agent 发 exhausted notice，并抛出：

```text
LLM context remained too long after reactive compaction.
```

其他 `LLMClientError` 被包装为带 kind 的 `RuntimeError`；未知异常包装成通用 `LLM request failed.`。原异常通过 exception chaining 保留。

## 10. Usage 累计

成功得到响应后，`_merge_usage()` 合并 usage：

- int/float 且不是 bool：按 key 累加。
- 其他值：以后一次响应覆盖前一次。

这允许 OpenAI、Anthropic 或 compatible endpoint 保留各自字段，不要求预先统一 schema。传输层失败的请求没有有效 response usage，因此不会进入累计。

## 11. 输出截断恢复

`LLMResponse.is_output_truncated` 为真时进入 Agent 层恢复，而不是正常执行工具或结束。

### 11.1 第一次截断：提升输出上限

Agent 计算：

```python
escalated = max(current_request_limit, policy.escalated_max_tokens)
```

如果可以提高且此前没有升级过：

1. 设置 `recovery_state.output_escalated=True`。
2. 设置 `output_max_tokens`。
3. 发出 `output_escalate` notice。
4. 丢弃当前截断草稿，同一步重新请求。

丢弃第一份草稿是为了避免模型续写一个本可以在更大预算下完整重新生成的半成品。

### 11.2 截断响应包含工具调用

如果升级后仍截断，并且响应含 tool calls，Agent 直接失败，**不会执行其中任何工具**。

原因是截断的工具调用集合或参数可能不完整。尤其对于写文件、Bash 等副作用工具，猜测执行会破坏状态。

### 11.3 纯文本截断：Continuation

对于有文本但无 tool call 的截断响应：

1. 用 `llm.assistant_message()` 把部分响应加入历史。
2. 把文本放入 `recovery_state.partial_outputs`。
3. 追加内部 user message `CONTINUATION_PROMPT`。
4. 在 `max_continuations` 预算内同一步继续调用。

最终完整文本由 `_combine_outputs()` 用换行拼接 partial outputs 和最后响应。

### 11.4 恢复耗尽

若有部分文本，返回：

```python
AgentRunResult(status="incomplete", content=last_content, ...)
```

并发出 `[final: incomplete]`。若连可用文本都没有，则抛出异常。

## 12. 最终回答路径

正常响应没有 tool calls 时：

1. 合并之前的 partial outputs 与当前文本。
2. 向持久 messages 追加普通 assistant 文本。
3. 发出 `final` 事件。
4. 在 HookContext metadata 写入 `final_content`。
5. 触发 `Stop` Hooks。
6. 根据 HookResult 发出 memory saved/failed 事件。
7. 返回 `AgentRunResult(status="completed")`。

没有工具调用时使用普通 assistant message，而不是 `llm.assistant_message()`，因为此时无需保留 Provider 特有的工具 block。

当前顺序是先发 `final`，再执行 Stop Hook。这让用户尽早看到结果，Memory Extraction 可以随后展示；但如果自定义 Stop Hook 抛异常，外层仍会把 Run 标记为 failed，此时终端可能已经打印 final。这是需要了解的当前边界。

## 13. 工具调用路径

### 13.1 Progress

如果响应同时包含文本和工具调用，文本去除首尾空白后作为 `progress` 事件发出：

```text
[progress] I will inspect the configuration first.
```

它是模型面向用户的工作摘要，不是隐藏推理。空文本不会产生 progress。

### 13.2 保存 assistant 工具调用

在执行工具之前：

```python
messages.append(llm.assistant_message(response))
```

这样下一轮历史包含模型原始结构化 tool call。不同 Provider 的消息格式由 LLMClient 处理。

### 13.3 多工具执行顺序

Agent 遍历 `response.tool_calls`，按响应顺序逐个执行。当前没有并行工具执行。

每个调用先：

- 增加 `tool_call_count`。
- 将工具名加入 `called_tools`。
- 发出带 id/name/arguments 的 `tool_call` 事件。
- 开始独立 duration 计时。

### 13.4 非法参数短路

如果 LLMClient 把 incompatible 参数标记为 `__invalid_tool_input__`：

- Agent 不触发权限 Hook。
- 不调用 ToolRegistry。
- 直接生成 `{ok: false, error, received_type}`。
- 作为普通 tool result 回灌，让模型下一轮修正。

这种错误属于“调用根本不满足工具输入协议”，在权限判断之前处理更合理。

### 13.5 `PreToolUse` Hook

权限和 MCP 策略在真正执行前运行：

```python
pre_tool_result = hooks.trigger_hooks(
    "PreToolUse", tool_call, hook_context
)
```

三种情况：

- `None`：直接继续。
- allow 且有 reason：发出 `permission_granted`。
- deny：不执行工具，将 Hook 提供的拒绝结果回灌，并发出 `permission_denied` 和 `tool_result`。

拒绝不是整个 Run 的异常。模型看到拒绝原因后，可以换方案、询问用户或给出受限结果。

### 13.6 创建 `ToolExecutionContext`

通过权限后，Agent 创建包含当前 run/agent/step/workdir 的上下文。metadata 当前包含：

```text
tool_call_id
tool_name
```

需要 Context 的工具由 ToolRegistry 自动注入；普通工具不会收到该参数。

### 13.7 `_execute_tool_call()`

普通参数直接调用：

```python
tools.call(name, arguments, context=tool_context)
```

ToolRegistry 捕获工具异常并统一返回：

```json
{"ok": false, "error": "..."}
```

因此大多数工具失败是可观察结果，不会中断 Agent Loop。

若参数含 `__batched_tool_inputs__`，Agent 顺序执行每个 object，并返回：

```json
{
  "ok": true,
  "batched": true,
  "count": 2,
  "results": [
    {"index": 1, "result": {...}},
    {"index": 2, "result": {...}}
  ]
}
```

只要一个子调用失败，顶层 `ok` 就为 false。当前批量能力主要承接 compatible 模型对 `artifact_create` 的数组调用修复。

### 13.8 `PostToolUse` Hook

工具执行后触发：

```python
hooks.trigger_hooks(
    "PostToolUse", tool_call, tool_result, hook_context
)
```

HookResult 为 replace 时，Agent 用其 value 替换最终回灌结果。适用场景包括：

- 结果脱敏。
- 输出持久化后只回灌摘要和路径。
- 统一补充验证元数据。

### 13.9 批量回灌并推进 step

同一模型响应里的所有工具处理完成后：

```python
messages.extend(llm.tool_result_messages(tool_results))
recovery_state.reset_output()
output_max_tokens = None
step_index += 1
```

“执行顺序”和“消息批量回灌”是两个概念：工具当前逐个执行，但 Anthropic 可以把多个结果组织在同一 user content 中。

成功完成一轮工具后会清空输出截断状态。下一轮模型重新使用默认 max tokens，不让一次恢复永久影响后续步骤。

## 14. Max Steps 路径

若每一轮都继续调用工具，循环最终达到预算：

1. 发出 `max_steps` 事件。
2. 返回 `AgentRunResult(status="max_steps")`。
3. `content` 使用最近一次模型文本，可能只是 progress，也可能为空。

它不抛异常，因为“达到预算”是可预期的受控状态。Workflow 可以据此决定阶段重试，Subagent 可以把状态交给父 Agent。

`max_steps=None` 不会进入预算退出，必须依赖模型最终回答、外部中断或异常终止。因此只适合交互式主 Agent；受控 Workflow 应始终设置有限预算。

## 15. Hook 在 Loop 中的位置

完整 Hook 顺序：

```text
main.py: UserPromptSubmit
        |
Agent step: BeforeLLM
        |
LLM response with tool calls
        |
for each tool:
    PreToolUse
    ToolRegistry.call
    PostToolUse
        |
LLM final response
        |
Stop
```

`UserPromptSubmit` 不在 `Agent.run()` 内，而由 CLI 在把 query 追加进 messages 之前触发。这使入口层可以处理原始输入；但其他调用方若也需要此 Hook，必须主动触发。

HookManager 的 deny 会立即终止当前 Hook 链；多个 allow 的 data 会合并；replace 结果具有更高优先级。Agent 只解释与当前生命周期节点有关的结果语义。

## 16. Background Job 协作

后台 Bash 不是 Agent Loop 自己创建的线程。模型调用 background tool 后，BackgroundJobManager 在外部执行任务；Agent 每个 step 开头只做通知消费。

```text
Step N: 模型调用 background_bash -> 返回 job_id
Agent 继续其他工作或结束当前 Run
后台进程完成 -> Manager 持久化 notification
Step N+1 / 下一次 Run: Agent drain notification
-> 注入 <background_notifications>
-> 模型读取结果并继续
```

通知按 owner agent 隔离，并在成功注入后 acknowledge，测试验证不会重复投递。

## 17. Event 与 CLI 展示

### 17.1 `_emit()` 的双通道

Agent 方法 `_emit()` 先补齐自身身份，再调用模块级 `_emit()`。模块级函数：

1. 构造 `AgentEvent`。
2. 调用 `record_agent_event(event)` 写 Trace。
3. 若存在 `on_event`，把事件交给调用方。

因此日志打印系统和 Hook 系统不冲突：Hook 改变运行行为，Event 描述已经发生的行为；两者通过清晰的生命周期点连接。

### 17.2 `print_agent_event()`

默认 CLI callback 按 type 渲染：

- 蓝色：step。
- 紫色：progress、final、fallback。
- 黄色：tool call、提醒、恢复、部分异常状态。
- 绿色：成功结果和授权。
- 红色：失败、拒绝、max steps。
- dim：上下文提示和子 Agent 前缀。

当 `depth > 0` 时，`_event_prefix()` 增加 `[agent_id]`，所以 Workflow 输出可显示 `[pm]`、`[engineer]`、`[qa]`。

### 17.3 工具输出截断

`_format_event_payload()` 先用 JSON 序列化，再按字符限制截断，并显示原始总长度。它只影响终端展示：

- 完整结果仍会回灌模型。
- 完整结果仍由 ContextManager 决定是否持久化。
- 传入的原对象不会被修改。

`_tool_result_color()` 不只检查顶层 `ok`，还识别验证工具 payload 中的 `failed`、`timed_out`、`issues_found` 和 `no_tests` 等语义状态。

## 18. Trace 层级

一次带工具调用的 Run 大致产生：

```text
run.started
  hook.BeforeLLM started/completed
  agent event: step
  llm.call started/completed
  agent event: progress
  agent event: tool_call
  hook.PreToolUse started/completed
  tool trace
  hook.PostToolUse started/completed
  agent event: tool_result
  ... next step ...
  agent event: final
  hook.Stop started/completed
run.completed
```

Run、LLM 和 Hook 都有自己的 correlation id；Trace Context 的 run/agent/parent/depth/step 将它们组织到同一棵执行树中。

## 19. 主 Agent、Subagent 与 Workflow 角色复用

### 19.1 主 Agent

`main.build_agent()` 注入完整工具集合、Memory/Artifact/Task/Permission Hook、Background Manager 和组合 Prompt，`agent_id="main"`，`max_steps=None`。

### 19.2 Subagent

SubagentRunner 创建新的 Agent：

- 按 mode 限制工具集合，如 explore 只读。
- 使用独立 messages 和委派 Prompt。
- 设置 `parent_run_id` 和更高 `depth`。
- 复用父 Agent 的事件回调。
- 可在独立 Worktree 中运行。

子 Agent 的 `AgentRunResult` 被转换为 `SubagentResult`，再作为父 Agent 的工具 Observation。

### 19.3 Workflow 角色

SerialCodingWorkflow 为每个 role 构造 Agent：

- role-specific system prompt 和 Skill sections。
- role-specific tools 和 MCP scope。
- role-specific max_steps。
- Workflow Task/Artifact/Memory Hooks。
- 共享 workflow worktree，但可有不同执行职责。

Workflow 负责阶段重试和 PM/Engineer/QA 转移；每个角色内部仍运行同一个 Agent Loop。这样“单 Agent 推理执行”与“多角色上层编排”保持分层。

## 20. 三类恢复的职责边界

| 恢复类型 | 触发位置 | 是否推进 step | 是否可能重复工具副作用 |
| --- | --- | --- | --- |
| 429/timeout/503 传输重试 | `LLMClient.chat()` | 否 | 不会重做上一轮工具 |
| Context length 压缩重试 | `Agent._run_loop()` + ContextManager | 否 | 尚未获得本轮工具调用，不执行工具 |
| 输出截断升级/续写 | `Agent._run_loop()` | 否 | 截断 tool call 明确禁止执行 |
| Workflow phase retry | `SerialCodingWorkflow` | 新 Agent Run | 依赖 Artifact/Checkpoint/Evidence 保证阶段恢复 |

这张表是理解系统可靠性的关键。不同层只处理自己能够判断是否安全重试的问题。

## 21. 测试覆盖

### 21.1 `test_agent.py`

直接覆盖：

- 同轮多个工具结果批量回灌。
- compatible 模型批量 artifact 参数执行。
- step/tool/final 事件顺序。
- tool call 前的 progress。
- CLI 可读输出和大 payload 截断。
- 无工具直接回答。
- 调用方持有 messages 时跨 Run 保留历史。
- max steps 结构化退出。
- 剩余 6 步和最后一步的 budget prompt。
- Context length 后响应式压缩和同一步重试。

### 21.2 `test_hooks.py`

覆盖权限拒绝、授权、PostToolUse 替换、BeforeLLM 动态 Task 上下文，以及压缩后重新注入任务状态。

### 21.3 `test_error_recovery.py`

覆盖输出上限升级、continuation 拼接、恢复耗尽返回 incomplete、禁止执行截断 tool call，以及传输重试不重复工具副作用。

### 21.4 其他协作测试

- `test_memory_system.py`：召回内容只进入临时请求，Stop Hook 提取记忆。
- `test_background_jobs.py`：后台通知注入且只消费一次。
- `test_trace_system.py`：完整 LLM/Hook/Tool/Final Trace。
- `test_subagent.py`：父子 Agent 事件和结果传播。
- `test_serial_workflow.py`：角色 Agent 在阶段编排中的行为。

### 21.5 尚未充分覆盖的边界

- `max_steps=None` 下的外部取消机制。
- 同轮多个耗时工具的并行执行策略。
- Stop Hook 在 final 事件之后抛异常的终端一致性。
- BeforeLLM Hook 自身调用 LLM 失败时的细粒度降级。
- 非数值 usage 的跨 Provider 统一统计语义。

## 22. 设计思路与取舍

### 22.1 为什么 Agent 不自己保存 history

如果 history 成为 Agent 隐藏字段，会让以下操作更困难：

- CLI 明确创建新会话。
- Workflow 为每个阶段构造独立上下文。
- Resume 从存储恢复消息。
- 测试检查准确的请求历史。

由调用方持有 messages，使 Agent 成为“对显式状态执行一次 Run”的运行时。代价是调用方必须正确复用列表。

### 22.2 为什么使用原地替换 `messages[:]`

Context 压缩可能生成新列表，但调用方仍持有旧对象。切片赋值既采用新内容，又维持共享引用，是显式消息所有权设计的重要细节。

### 22.3 为什么 Hook 消息不写入历史

Task 状态、召回记忆和 Artifact 摘要是当前请求的运行态，不是用户真实发言。永久追加会造成重复、陈旧内容和上下文膨胀。临时 runtime messages 既能每轮刷新，也能在压缩后重建。

### 22.4 为什么权限拒绝要回灌模型

权限拒绝是环境 Observation，不等于 Harness 崩溃。模型需要知道哪个动作被拒绝以及原因，才能选择只读替代、向用户确认或诚实停止。

### 22.5 为什么同轮工具暂时顺序执行

顺序执行实现简单，事件顺序稳定，也更适合可能互相依赖或修改同一工作区的 coding tools。代价是多个独立 read/search 调用会增加延迟。

未来若并行，应只并行显式标记为只读、无副作用且相互独立的工具，而不是对所有 tool calls 使用线程池。

### 22.6 为什么 Event 与 print 分开

结构化 Event 可以同时服务 CLI、Trace、Workflow 包装器和未来 UI。若 Agent 内直接 print，Subagent 前缀、测试、Web 展示和静默运行都会变得困难。

### 22.7 为什么 Progress 不是隐藏推理

完整 Chain of Thought 不稳定、冗长，也不应作为系统可靠性的接口。Progress 是模型面向用户生成的简短状态，可被记录和展示；真正可验证的执行状态来自 tool call、tool result、Artifact 和 Trace。

### 22.8 为什么 `_run_loop()` 仍然较长

主循环需要表达严格时序：压缩必须在 LLM 前，权限必须在工具前，Post Hook 必须在回灌前，截断 tool call 必须在执行前阻止。过度拆成大量小对象会隐藏状态转移。

当前实现把复杂策略放进 Context/Hook/Recovery/Registry，只在 Agent 中保留编排顺序。可以继续提取纯辅助函数，但不宜把一次 step 分散到难以追踪的事件总线中。

## 23. 真实评测问题与演进经验

### 23.1 工具结果打印过长

早期 CLI 打印完整 read/search/test 结果，长 Workflow 很难观察关键决策。修复把展示预览限制为固定字符数，但保留完整回灌和 Trace/持久化链路。

经验是：可观测性不等于原样倾倒全部数据。用户界面应摘要，底层证据仍需可追溯。

### 23.2 Agent 在预算耗尽前仍继续探索

评测中 Engineer/QA 可能在最后一步继续调用工具，导致结果已经生成却没有下一轮读取 Observation。当前在剩余 6 步和最后一步注入 step budget，让模型提前收敛并预留最终回答轮次。

它不是硬保证，所以最终仍由 `max_steps` 状态和 Workflow 重试处理。

### 23.3 压缩后 Task/Memory 状态丢失

如果动态上下文只在 Run 开头注入，Context compact 后模型可能忘记当前计划或已发现信息。当前每个 step 运行 BeforeLLM，并通过 `context_compacted` metadata 提醒 Hook 重建必要状态。

### 23.4 多轮评测重复劳动

仅靠 messages 在单次会话内保留历史，不足以支持 Workflow fail/resume。后续通过 Working Memory、Artifact、Checkpoint 和 Workflow Store 在 Agent Loop 之外补足跨 Run 状态。Agent 保持通用，不直接内置某一种业务恢复格式。

## 24. 当前限制与演进方向

当前限制：

1. Agent Loop 是同步的；工具和模型调用都会阻塞当前线程，只有显式 background tool 异步。
2. 同轮多个工具调用顺序执行，没有只读并行优化。
3. 主 Agent 使用 `max_steps=None` 时缺少统一的 wall-clock/token/cost 总预算。
4. `AgentEventType` 同时承载 Agent 自发事件和工具/Subagent 事件，未来类型继续增加时可能需要按领域拆分。
5. Stop Hook 发生在 final event 之后，异常时展示状态与 Run 状态可能不一致。
6. `request_tokens` 在二次压缩后没有重新估算，Trace 中该 operation metadata 可能仍是压缩前估值。
7. Progress 依赖模型在 tool call 同轮主动输出文本，不保证每一步都有。

合理的演进优先级：

1. 先用当前评测集建立 steps、tool calls、成功率和恢复次数基线。
2. 增加主 Agent 的总 token/time budget 和可取消信号。
3. 根据 Trace 证明延迟瓶颈后，再考虑只读工具并行。
4. UI 化时继续消费 AgentEvent，不改 Agent 核心协议。

## 25. 面试追问与回答要点

### Q1：你们的 Agent Loop 是 ReAct 吗？

是工程化 ReAct：模型通过 progress 表达面向用户的推理摘要，通过结构化 tool call 执行动作，工具结果作为 Observation 回灌并继续循环。它不要求暴露完整 Chain of Thought。

### Q2：为什么 `Agent.run()` 接受 messages，而不是只接受字符串？

显式 messages 让调用方控制会话生命周期、持久化和分支。CLI 复用同一列表即可保留历史；Workflow 和 Subagent 可创建隔离上下文。原始字符串入口可以作为更上层便利封装，但不应取代显式状态。

### Q3：一个 step 到底是什么？

它是一次任务决策轮次预算。正常 tool call 执行并回灌后推进一步；Context 压缩重试和输出续写属于同一步恢复，不推进计数。因此 steps 与 SDK 请求次数不一定相同。

### Q4：多个工具调用是并行的吗？

目前按模型返回顺序执行，之后按 Provider 协议批量回灌。Coding 工具可能修改同一工作区，顺序语义更安全。未来只考虑并行显式无副作用的 read/search 工具。

### Q5：工具异常为什么不直接让 Agent 失败？

ToolRegistry 将大多数异常包装为 `{ok:false,error}`，作为环境 Observation 回灌，模型可以修复参数或换方案。Hook、Harness invariant 或无法安全恢复的截断 tool call 才会终止 Run。

### Q6：权限检查为什么使用 Hook？

Agent 只固定“工具执行前必须经过 PreToolUse”这个时序；具体路径保护、用户确认和 MCP 权限策略可以组合注册。这样不把安全规则硬编码进 Loop，也不会让工具各自重复实现。

### Q7：Context 压缩为什么既主动又响应式？

主动估算降低超限概率，但不同 Provider tokenizer 和工具 schema 开销可能有偏差；Provider 返回 context length 后再强制 recover 是最后防线。恢复后同一步重试，避免消耗任务预算。

### Q8：如何保证重试不重复写文件？

LLM 传输重试发生在获得 tool call 之前；工具执行在 Client 返回之后。输出若在 tool call 阶段截断则明确禁止执行。更上层 Workflow 重试则依赖 Worktree、Checkpoint、Artifact 和 Evidence，而不是假设所有工具幂等。

### Q9：Event 系统和 Hook 系统是否冲突？

不冲突。Hook 是控制面，在生命周期节点影响输入、授权或结果；Event 是观测面，描述发生了什么，并交给 CLI/Trace/UI。Agent 把二者接在同一时序上，但职责不同。

### Q10：为什么动态记忆不是直接 append 到 messages？

召回结果是当前请求视图，可能随 query 和压缩变化。临时注入避免重复膨胀和陈旧状态；真正需要跨会话保存的信息由 MemoryManager 持久化，而非混入对话事实。

### Q11：如何让同一个 Agent Loop 支持 PM、Engineer、QA？

Loop 不编码角色。Workflow 通过依赖注入改变 Prompt sections、Skill、工具子集、Hook、workdir 和 max_steps。角色差异是配置与能力边界，执行语义保持一致。

### Q12：这个模块最大的工程取舍是什么？

保留一个可直接阅读的顺序状态机，同时把 Provider、Context 算法、Hook 策略和工具实现下沉到独立组件。主循环仍较长，但关键安全时序集中，调试 Trace 时可以按源码从上到下还原。

## 26. 建议源码阅读顺序

1. `AgentEvent`、`ToolExecutionContext`、`AgentRunResult`：先理解输入输出边界。
2. `Agent.__post_init__()`、`new_messages()`：理解依赖绑定和消息所有权。
3. `Agent.run()`：理解 Trace/Recovery Scope。
4. `_run_loop()` 的循环前状态变量。
5. 从 step 开头读到 `llm.chat()`：理解 Context 和 BeforeLLM。
6. 分别阅读 context error、output truncated、final 三条响应路径。
7. 阅读 tool call for-loop：理解权限、执行、Post Hook 和回灌。
8. `_emit*()` 与 `print_agent_event()`：理解可观测性。
9. `tests/test_agent.py`：用最小 FakeLLM 观察每条状态转移。
10. 回到 `main.py`、`subagent.py` 和 `serial_workflow.py`：观察同一 Loop 如何被不同运行模式复用。

读完本模块后，下一步适合深入 `ContextManager`。因为 Agent 已经展示了 `prepare()`、`recover()`、`build_request_messages()` 和 token 估算的调用位置，继续向下可以解释 system prompt 组装、工具结果持久化、Working Memory 与上下文压缩算法。
