# Runtime、Provider 适配与错误恢复

## 1. 模块边界

这一层解决四个问题：

- 如何用同一套 Agent Loop 调用 OpenAI 和 Anthropic；
- 如何把一次模型响应变成可执行的工具动作；
- 如何在不污染主循环的前提下接入权限、上下文和日志；
- 如何处理超时、过载、上下文超限和输出截断。

核心文件：

- `src/llm_agent/llm_client.py`
- `src/llm_agent/agent.py`
- `src/llm_agent/hooks/__init__.py`
- `src/llm_agent/recovery.py`
- `src/llm_agent/background_jobs.py`

职责边界：

- `LLMClient` 只负责 provider API 边界，不执行工具。
- `Agent` 负责状态循环和时序，不实现具体工具业务。
- `HookManager` 负责可改变行为的扩展点。
- `AgentEvent` 只面向实时展示。
- `TraceRecorder` 负责持久化观测，不参与流程裁决。
- `Recovery` 处理模型调用失败，不重放已经执行的副作用工具。

## 2. LLMClient 设计

### 2.1 为什么使用官方 SDK

项目最早可以用 HTTP 手写请求，但最终保留 OpenAI 与 Anthropic 官方 SDK，原因包括：

- SDK 已处理认证头、连接池、序列化和错误对象；
- compatible endpoint 仍可通过 `base_url` 使用；
- 超时、取消和服务端错误保留更准确的语义；
- 跟随官方协议变化的维护成本更低；
- 避免项目自己再实现一套不完整 SDK。

项目关闭或约束 SDK 内建重试，把重试权收口到 `RecoveryPolicy`。否则 SDK 重试和 Agent 重试叠加后，实际等待时间和请求次数不可控。

### 2.2 统一数据模型

上层只依赖两个核心对象：

```python
@dataclass(frozen=True)
class LLMToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    raw: dict[str, Any]


@dataclass(frozen=True)
class LLMResponse:
    content: str
    tool_calls: list[LLMToolCall]
    raw: dict[str, Any]
    stop_reason: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
```

这个模型刻意很薄：

- 保留 `raw` 方便排障；
- 标准化 Agent 真正依赖的 text、tool_calls、stop_reason、usage；
- 不试图构造覆盖所有厂商特性的“万能协议”。

### 2.3 Provider 差异收敛在哪里

`LLMClient.chat()` 选择 provider，内部完成以下转换：

| 语义 | OpenAI | Anthropic |
|---|---|---|
| system prompt | messages 中的 system role | `system` 顶层参数 |
| tool schema | `type=function` 包装 | `name/input_schema` |
| tool call | `message.tool_calls` | content 中的 `tool_use` block |
| tool result | `role=tool` 消息 | user content 中的 `tool_result` block |
| finish reason | `finish_reason` | `stop_reason` |

Agent 通过以下适配方法生成 provider 正确的后续消息：

- `assistant_message(response)`
- `tool_result_message(...)`
- `tool_result_messages(...)`

Anthropic 多工具调用的关键点是：同一轮所有工具结果以一个 user message 的多个 `tool_result` block 回灌，而不是每个结果各发一条消息。这个规则由 `LLMClient` 保证，Agent 不需要知道具体格式。

### 2.4 Tool 参数兼容处理

正常情况下 Tool Call 的 input 必须是 JSON object。但 compatible API 或截断响应可能返回：

- JSON 字符串；
- 非法 JSON；
- list 而不是 object；
- 把多个工具输入错误地批在一个 list 中。

边界层会解析、标准化或写入内部错误标记。Agent 检测到非法参数后将其作为结构化工具失败回灌，绝不把 list 强行交给要求 dict 的工具，更不会执行截断的副作用调用。

这样解决过 `artifact_create` 收到 list、Anthropic SDK 序列化告警后运行失败的问题。

### 2.5 配置加载

`load_llm_environment()` 从项目约定的 `.env` 加载配置，`LLMClient` 再解析：

- provider；
- model；
- API key；
- base URL；
- max tokens；
- timeout。

模型必须显式配置。缺少 `LLM_MODEL`、`OPENAI_MODEL` 或 `ANTHROPIC_MODEL` 时尽早失败，避免发出含糊请求。

## 3. Agent Loop 实现

### 3.1 状态由调用方持有

`Agent.run(messages, ...)` 接受可变消息列表，完成后直接追加新消息。CLI 在循环外创建一次 `messages = agent.new_messages()`，因此连续用户输入共享会话历史。

这个选择比把 history 藏在 Agent 内更灵活：

- CLI 可以持续会话；
- 测试可以传入精确历史；
- Subagent 可以自然创建独立消息列表；
- Workflow 每个角色/尝试可以控制自己的上下文边界。

代价是调用方必须明确决定何时新建会话。

### 3.2 一次 step 的实际顺序

简化后的伪代码：

```python
with trace_scope(...), recovery_scope(...):
    trigger("UserPromptSubmit")
    while within_step_budget():
        collect_background_notifications()
        context.prepare(messages)
        runtime_messages = trigger("BeforeLLM")
        context.preflight_compact(messages, runtime_messages, tools)
        emit(STEP)
        response = llm.chat(request_messages, tools)
        messages.append(llm.assistant_message(response))

        if response.is_output_truncated:
            recover_or_continue()
            continue

        if not response.tool_calls:
            trigger("Stop")
            emit(FINAL)
            return response.content

        results = []
        for call in response.tool_calls:
            pre = trigger("PreToolUse", call)
            result = pre.value if pre.denied else tools.call(call)
            post = trigger("PostToolUse", call, result)
            results.append(post.value if post.replaces_result else result)

        messages.extend(llm.tool_result_messages(results))
```

真实实现还包含 Trace、usage、progress、输出 token 升级、上下文超限恢复和异常分类，但主循环保持上述单一结构。

### 3.3 Canonical message 与 runtime message

`messages` 是 canonical conversation，即模型真实说过和工具真实返回过的内容。`BeforeLLM` Hook 注入的 Task、Artifact、Memory 等内容只用于当前请求。

如果把运行时上下文永久 append 到 history，会出现：

- 每一步重复注入相同 Artifact；
- Task 状态更新后仍残留旧快照；
- Memory 召回内容不断复制；
- 压缩摘要把基础设施提示误认为用户对话。

因此 `ContextManager.build_request_messages()` 临时组合两者，请求结束后不把 runtime messages写回 canonical history。

### 3.4 Progress 与隐藏推理

CLI 展示：

- 当前 step；
- 模型主动给出的短 progress；
- Tool Call 与截断后的 Tool Result；
- Recovery、压缩、权限和最终答案。

这与原生 reasoning summary 不同。当前 progress 是 Harness 要求模型输出的工作状态，不是模型内部 chain-of-thought，也不能保证等价于真实隐式推理。设计目的只是让用户知道“正在做什么、下一步是什么、是否遇到阻塞”。

### 3.5 Step budget

主 Agent 可以不设固定上限，但 Workflow 角色由配置提供 `max_steps`。接近预算时，Agent 会注入进度提示，要求模型收敛、保存必要 Artifact 或明确阻塞。

预算是最后一道保险，而不是主要规划机制。Task、Artifact 和 phase prompt 才负责让模型知道当前目标。

## 4. Hook、Event 与 Trace

### 4.1 Hook 生命周期

当前生命周期：

```text
UserPromptSubmit -> BeforeLLM -> PreToolUse -> PostToolUse -> Stop
```

`HookResult` 支持：

- `allow`：继续，并可携带合并后的 data；
- `deny`：短路当前动作，返回拒绝结果；
- `replace`：替换工具执行结果。

同一事件可以注册多个 Hook。`HookManager` 按顺序执行：deny 立即返回；allow data 合并；replace 成为有效结果。这里的 `effective_result` 就是多个 Hook 组合后的当前有效决策。

### 4.2 为什么 Hook 不等于 Event

```text
Hook:  execution policy，可能改变程序结果
Event: presentation signal，只通知外部观察者
Trace: durable telemetry，用于事后分析
```

例如 PermissionHook 拒绝 bash 是 Hook；CLI 打印黄色 permission denied 是 Event；把拒绝原因和耗时写入 JSONL 是 Trace。

### 4.3 fail-open 与 fail-closed

不同组件采用不同失败策略：

- 权限与安全检查失败：fail-closed，不能冒险执行工具。
- Trace 写入失败：默认 fail-open，不能因为日志目录异常让编码任务失败；strict 模式可改为抛错。
- Memory 提取失败：不应影响最终回答。
- Artifact/Evidence Gate 失败：Workflow fail-closed，因为它决定是否接受代码。

这不是统一使用一种策略，而是根据组件是否处于安全/正确性关键路径决定。

## 5. Recovery 设计

### 5.1 错误分类

Recovery 对错误做语义分类，而不是捕获所有异常后盲目 retry：

- timeout / interrupted；
- rate limit；
- overload / server error；
- context length exceeded；
- output truncated；
- authentication / invalid request；
- unknown。

认证和参数错误通常不可重试；网络和过载错误才使用退避。

### 5.2 `recovery_scope` 的原理

`recovery_scope` 是 `contextmanager`，内部通过 `ContextVar` 设置当前运行的 `RecoveryState`：

```python
token = current_state.set(state)
try:
    yield state
finally:
    current_state.reset(token)
```

`yield state` 把状态交给 `with` 代码块使用；`finally` 保证退出、异常或嵌套调用时恢复上一个上下文。

使用 `ContextVar` 的原因是：LLMClient、Agent、Trace 等深层函数都能读取“当前 run”的 recovery 状态，不必在每层函数签名中传递参数；同时异步任务上下文隔离优于全局变量。

### 5.3 有界重试

`RecoveryPolicy` 同时约束：

- 最大重试次数；
- 最大累计等待时间；
- backoff 与 jitter；
- 可选 fallback model；
- output token 升级上限；
- continuation 次数。

有界性非常重要。无限重试会让一次 Workflow 看似仍在运行，实际持续消耗时间和 token。

### 5.4 副作用安全

模型请求发生在工具执行前，因此对一次 LLM 请求 retry 不会自动重放工具。工具执行结果一旦写入 messages，下一步模型调用失败时仍保留该结果。

项目没有对任意工具调用做透明 retry，因为 write/edit/bash 未必幂等。真正需要工具级重试时，应由具体工具根据幂等性单独实现。

## 6. Background Job

### 6.1 当前范围

后台执行只支持显式的 Bash job，而不是自动把所有慢工具异步化。模型必须选择对应 background tool；Harness 不猜测一个命令是否应该后台运行。

适合后台运行：

- 较长测试；
- 构建或静态检查；
- 本地开发服务器；
- 模型明确不需要立即结果的命令。

仍同步执行：

- read/edit/write；
- 结果决定下一步的短命令；
- Artifact/Task/Memory 操作；
- Subagent 与当前串行 Workflow phase。

### 6.2 Manager 为什么有两组 notification 状态

- `_notifications` 保存按发生顺序待交付的完成消息；
- `_queued_notifications` 保存已经入队的 job id，用于去重。

前者解决顺序与消费，后者解决后台线程和轮询可能重复发现同一完成任务的问题。Agent 每个 step 开始时收集通知，再把结果注入上下文。

### 6.3 Trace 上下文传播

后台线程会显式捕获当前 `TraceRecorder` 和 `TraceContext`。否则 ContextVar 不会自动跨所有线程边界传播，后台事件会丢失父 run 关系。

## 7. 设计评价

### 优点

- Provider 差异没有泄漏进 Agent 和 Workflow。
- Agent Loop 保持单一控制流，扩展能力主要通过 Hook/Registry 组合。
- Recovery 有语义分类、预算和副作用边界。
- 实时展示、行为控制和持久化观测互不绑定。

### 局限

- 没有 token streaming 和真正的 provider reasoning summary。
- ContextVar 简化了参数传递，但隐式依赖需要通过 scope 和测试约束。
- Background 目前只覆盖 Bash，任务取消也不是分布式取消。
- compatible API 的非标准行为只能做有限防御，无法保证完全兼容。

## 8. 面试追问

**为什么不让 Agent 直接处理 SDK response？**

因为这会让 Agent Loop 出现 provider 分支，之后 Context、Trace、Workflow 都被厂商协议感染。适配层只统一上层真正依赖的最小语义，同时保留 raw 排障。

**为什么不使用一个通用第三方 LLM 框架？**

项目目标之一是掌握协议和运行时边界，因此直接使用官方 SDK。生产业务可以评估 LiteLLM 等方案，但仍需要自己的 Tool、Security、Workflow 和 Evidence 层，通用 provider router 不能替代这些组件。

**ContextVar 会不会让代码难懂？**

会引入隐式上下文，所以只用于真正横切且与当前 run 绑定的 Trace/Recovery；业务状态仍显式传参。所有入口必须使用 scope，并在 `finally` reset。

**超时是否一定是模型服务的问题？**

不一定。可能是供应商、网络、请求体过大、输出过长或本地 timeout 配置。Trace 需要结合 request tokens、duration、重试和 provider 错误判断，不能只看最后一个 timeout 文本。
