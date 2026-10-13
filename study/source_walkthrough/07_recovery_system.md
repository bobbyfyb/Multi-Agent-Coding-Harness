# Recovery System 源码精读

> 核心源码：[src/llm_agent/recovery.py](../../src/llm_agent/recovery.py)  
> 传输层接入：[src/llm_agent/llm_client.py](../../src/llm_agent/llm_client.py)  
> Agent 语义恢复：[src/llm_agent/agent.py](../../src/llm_agent/agent.py)、[src/llm_agent/context_manager.py](../../src/llm_agent/context_manager.py)  
> 上层恢复边界：[src/llm_agent/serial_workflow.py](../../src/llm_agent/serial_workflow.py)、[src/llm_agent/workflow_checkpoint.py](../../src/llm_agent/workflow_checkpoint.py)、[src/llm_agent/workflow_store.py](../../src/llm_agent/workflow_store.py)  
> 主要测试：[tests/test_error_recovery.py](../../tests/test_error_recovery.py)、[tests/test_agent.py](../../tests/test_agent.py)、[tests/test_llm_client.py](../../tests/test_llm_client.py)

## 1. 模块目标

Recovery System 解决的不是一种错误，而是三类不同层级的失败：

1. **传输故障**：限流、过载、连接中断、超时和服务端错误。
2. **请求/响应形态故障**：上下文过长、输出被 token 上限截断。
3. **任务阶段未达标**：Agent 达到 step 上限，或 Workflow 的 Evidence Gate 未通过。

当前系统没有用一个万能 `try/except` 把它们混在一起，而是按最了解问题语义的层负责恢复：

```text
Provider / Network
      |
      | 429, 529, timeout, connection, 5xx
      v
LLMClient transport recovery
      | retry / exponential backoff / model fallback
      |
      | context_length or truncated response
      v
Agent semantic recovery
      | reactive compact / output escalation / continuation
      |
      | incomplete, max_steps, evidence gate failure
      v
SerialWorkflow phase recovery
      | attempt checkpoint / corrective prompt / phase retry
      |
      | process interruption or failed workflow
      v
WorkflowStore + explicit resume()
```

这一区分是整个设计的核心。只有拥有足够上下文的层，才能做正确的恢复动作。

## 2. 职责边界

| 组件 | 负责什么 | 不负责什么 |
| --- | --- | --- |
| `recovery.py` | 策略、状态、错误分类、退避、事件和动态作用域 | 不直接调用 SDK，不修改 messages |
| `LLMClient.chat()` | 同一 LLM 请求的传输重试和模型 fallback | 不压缩对话，不执行工具，不重跑 Workflow 阶段 |
| `Agent._run_loop()` | 上下文超限和输出截断的语义恢复 | 不判断项目交付是否达标 |
| `ContextManager.recover()` | 真正执行响应式上下文压缩 | 不决定重试次数 |
| `SerialCodingWorkflow._run_phase()` | Evidence Gate 驱动的角色阶段重试 | 不替代 SDK 重试；provider 异常会让阶段失败 |
| `WorkflowStore` / `resume()` | 跨进程持久化和显式恢复 | 不透明地吞掉错误或无限自动重试 |

因此项目中的以下三个 continuation 含义不同：

- `RecoveryAction == "continuation"`：一条 LLM 输出被截断后，让模型接着输出。
- `AgentRunResult.status == "max_steps"`：一次 Agent Loop 的工具轮次预算耗尽。
- Workflow `continuation_checkpoint`：下一次角色 attempt 或 `resume()` 继续未完成工作。

## 3. `recovery.py` 源码地图

| 符号 | 作用 |
| --- | --- |
| `RecoveryKind` | 统一 Provider 错误类别 |
| `RecoveryAction` | 对外可观测的恢复动作类别 |
| `CONTINUATION_PROMPT` | 输出截断后的内部续写指令 |
| `RecoveryPolicy` | 不可变的恢复配置 |
| `RecoveryState` | 一次 Agent run 内可变的恢复状态 |
| `ClassifiedError` | 分类后的错误事实 |
| `RecoveryNotice` | Trace 和 CLI 共用的恢复事件 |
| `recovery_scope()` | 用 `ContextVar` 绑定当前状态和回调 |
| `emit_recovery_notice()` | 同时写 Trace 并通知 Agent 事件层 |
| `retry_delay()` | `Retry-After` 或指数退避加抖动 |
| `classify_llm_error()` | 从异常链中提取错误类型和元数据 |

## 4. 错误类型 `RecoveryKind`

`RecoveryKind` 是一个 `Literal` 联合类型：

```python
RecoveryKind = Literal[
    "authentication",
    "conflict",
    "connection",
    "context_length",
    "invalid_request",
    "not_found",
    "overloaded",
    "permission",
    "rate_limit",
    "server",
    "timeout",
    "unknown",
]
```

它把 OpenAI SDK、Anthropic SDK和 compatible server 的不同异常统一成 Harness 内部语义。

### 4.1 可重试类型

当前自动重试：

- `rate_limit`：HTTP 429。
- `overloaded`：HTTP 529 或 overload 文本/类型。
- `conflict`：HTTP 409。
- `timeout`：HTTP 408、`TimeoutError` 或 timeout 标记。
- `connection`：SDK connection error、`OSError` 或连接中断标记。
- `server`：HTTP 5xx 或内部服务异常。

### 4.2 不可直接重试类型

当前不做传输重试：

- `authentication`：重试不会修复错误 API Key。
- `permission`：重试不会改变服务端授权。
- `not_found`：通常是模型名、endpoint 或资源配置错误。
- `invalid_request`：请求参数或消息协议有问题。
- `unknown`：系统没有足够证据证明它是瞬态错误。
- `context_length`：原请求不变时重试必然继续失败，必须交给 Agent 修改上下文。

这里采用保守原则：无法确认瞬态性的错误不自动重试，避免重复费用和无意义等待。

## 5. 恢复动作 `RecoveryAction`

| Action | 触发位置 | 含义 |
| --- | --- | --- |
| `retry` | `LLMClient` | 等待后重发同一请求 |
| `fallback` | `LLMClient` | 连续 overload 后切换备用模型 |
| `context_compact` | `Agent` | 修改对话上下文后重新调用 |
| `output_escalate` | `Agent` | 提高 `max_tokens`，丢弃首次截断草稿并重试 |
| `continuation` | `Agent` | 保存部分输出并注入续写指令 |
| `exhausted` | `LLMClient` 或 `Agent` | 对应恢复预算已耗尽 |

Action 被建模为事件，而不是把打印散落在重试逻辑里，因此同一个动作可以同时进入 Trace、CLI 和上层观察者。

## 6. `RecoveryPolicy`

`RecoveryPolicy` 使用 `@dataclass(frozen=True)`，创建后不可修改字段：

| 字段 | 默认值 | 含义 |
| --- | ---: | --- |
| `max_retries` | 4 | 单次 LLM call 允许的额外传输重试次数 |
| `base_delay_seconds` | 0.5 | 指数退避初始延迟 |
| `max_delay_seconds` | 8.0 | 基础延迟和 `Retry-After` 的截断上限 |
| `jitter_ratio` | 0.25 | 在基础延迟上增加的随机比例 |
| `max_retry_elapsed_seconds` | 300 | 是否允许启动下一次重试的累计时间预算 |
| `fallback_model` | `None` | 过载时可切换的模型 |
| `fallback_after_overloads` | 3 | 连续 overload 多少次后 fallback |
| `escalated_max_tokens` | 8192 | 第一次输出截断后的目标 token 上限 |
| `max_continuations` | 2 | 提升上限后仍截断时允许的续写次数 |

`__post_init__()` 校验负数、零值等非法配置，让错误在启动阶段暴露。

### 6.1 `max_retries` 的准确含义

判断条件是：

```python
attempts <= policy.max_retries
```

`attempts` 在第一次请求前已经加一，因此：

```text
总请求次数上限 = 1 次初始请求 + max_retries 次重试
```

默认 `max_retries=4` 时最多发出 5 次 SDK 请求。事件中的 `max_attempts` 也使用 `max_retries + 1`。

### 6.2 CLI 环境变量

`main._build_llm()` 当前暴露：

```text
LLM_MAX_RETRIES
LLM_MAX_RETRY_ELAPSED_SECONDS
LLM_FALLBACK_MODEL / FALLBACK_MODEL_ID
LLM_ESCALATED_MAX_TOKENS
LLM_MAX_CONTINUATIONS
```

退避初值、最大延迟、jitter 和 fallback 阈值仍使用代码默认值。直接构造 `RecoveryPolicy` 时可以修改全部字段。

## 7. `RecoveryState`

`RecoveryPolicy` 描述规则，`RecoveryState` 记录一次运行中规则已经走到了哪里：

| 字段 | 生命周期内含义 |
| --- | --- |
| `primary_model` | Agent run 开始时的主模型 |
| `current_model` | 当前实际请求模型，fallback 后会保留 |
| `consecutive_overloads` | 连续过载计数，成功或其他错误会清零 |
| `transport_retries` | 本次 Agent run 累计传输重试次数 |
| `fallback_used` | 是否发生过模型 fallback |
| `output_escalated` | 当前逻辑输出是否已经提升过 token 上限 |
| `output_continuations` | 当前逻辑输出已经续写几次 |
| `partial_outputs` | 已接受的截断文本片段 |

### 7.1 状态生命周期

`Agent.run()` 每次创建一个新 `RecoveryState`，并在整个 Agent Loop 中复用：

```text
Agent.run A: primary -> fallback -> 后续 step 继续使用 fallback
Agent.run 结束: state 被释放
Agent.run B: 重新从 primary 开始
```

如果独立调用 `LLMClient.chat()`，外部没有 `recovery_scope`，Client 会为这一次 chat 临时创建状态。此时 fallback 只在该 chat 的重试序列中有效。

### 7.2 `reset_output()` 为什么只重置输出字段

工具结果成功回灌后，Agent 调用：

```python
recovery_state.reset_output()
output_max_tokens = None
```

它清空 `output_escalated`、续写次数和部分输出，但保留当前模型及传输统计。

原因是：一次工具调用后的下一轮模型响应属于新的逻辑输出，应重新获得一次输出恢复预算；模型 fallback 则代表这次 Agent run 的 Provider 状态，不能每轮又切回过载模型。

## 8. `ClassifiedError` 与 `RecoveryNotice`

### 8.1 `ClassifiedError`

`ClassifiedError` 是错误分类后的纯事实：

```python
ClassifiedError(
    kind="rate_limit",
    retryable=True,
    status_code=429,
    retry_after_seconds=1.5,
)
```

它不包含恢复计数，也不决定是否还有预算。`retryable=True` 只表示错误类型允许重试，最终还要同时满足次数和 elapsed budget。

### 8.2 `RecoveryNotice`

`RecoveryNotice` 是恢复动作的统一数据协议，包含：

- action、reason。
- attempt、max_attempts、delay。
- 当前模型或 fallback 前后模型。
- HTTP status、LLM call id、detail。
- 动作专属的 `data`。

`to_dict()` 删除值为 `None` 的字段，使 CLI 和 JSONL 更紧凑。

设计上要注意：`data` 最后合并，当前实现允许它覆盖 `action` 等同名字段。内部调用均未这样使用，但未来接受外部构造的 Notice 时应保留字段名约束。

## 9. `ContextVar` 与 `recovery_scope()`

系统没有把 `RecoveryState` 和 callback 逐层传进：

```text
Agent -> LLMClient -> retry helper -> event emitter
```

而是使用两个 `ContextVar`：

```python
_CURRENT_STATE
_CURRENT_CALLBACK
```

`recovery_scope()` 的运行过程是：

```python
state_token = _CURRENT_STATE.set(state)
callback_token = _CURRENT_CALLBACK.set(callback)
try:
    yield state
finally:
    _CURRENT_CALLBACK.reset(callback_token)
    _CURRENT_STATE.reset(state_token)
```

### 9.1 `yield` 在这里做什么

`@contextmanager` 会把 `yield` 前的代码当作 `with` 进入逻辑，把 `yield` 后的代码当作退出逻辑：

```python
with recovery_scope(state, callback):
    llm.chat(...)
```

等价过程：

1. 设置当前 state 和 callback。
2. `yield state` 把控制权交给 `with` 内部。
3. 内部正常返回或抛异常。
4. `finally` 恢复进入 scope 前的 ContextVar 值。

token 不是业务 token，而是 ContextVar 的恢复凭证。嵌套 scope 退出后会恢复父 scope，而不是粗暴设置成 `None`。

### 9.2 为什么不用全局变量

普通全局变量会让并发 Agent 相互覆盖状态。ContextVar 的值绑定到当前执行上下文，更适合嵌套调用和 asyncio task。

当前 Recovery 主链路是同步的。新线程默认不会自动继承调用线程的 ContextVar，因此不能假设后台线程天然拥有相同 Recovery scope。

## 10. `emit_recovery_notice()`

每次恢复动作先写 Trace：

```text
category = recovery
name = recovery.<action>
status = warning | error
correlation_id = LLM call_id（如果有）
```

然后读取当前 callback。`Agent.run()` 注册的 callback 会把 Notice 转为：

```python
AgentEvent(type="recovery", data=notice.to_dict(), ...)
```

最终 `print_agent_event()` 以简短彩色文本展示，例如：

```text
[recovery retry] rate_limit 2/5, wait 0.5s
[model fallback] primary -> fallback
[recovery compact] context length exceeded
[output limit increased] 4096 -> 8192
[output continuation] 1/2
[recovery exhausted] timeout
```

Trace 保留机器可分析信息，CLI 只打印关键状态，两者不会冲突。

当前 callback 没有异常隔离。如果用户提供的 `on_event` 自身抛错，该异常会中断恢复链路；默认 CLI printer 不存在这个问题。

## 11. `classify_llm_error()`

分类器先遍历异常链：

```python
current = exc
current = current.__cause__ or current.__context__
```

并用对象 id 集合防止循环引用。它从整条链汇总：

- 异常文本。
- 异常类名。
- `status_code`。
- error `code`。
- `Retry-After`。

这适合 SDK 封装：外层可能是 Harness 的错误，真正的 HTTP 异常位于 `__cause__`。

### 11.1 分类优先级

分类顺序不是随意的：

1. context length / 413。
2. rate limit / 429。
3. overloaded / 529。
4. conflict / 409。
5. timeout / 408。
6. connection。
7. generic server / 5xx。
8. authentication / 401。
9. permission / 403。
10. not found / 404。
11. generic invalid request / 4xx。
12. unknown。

具体类别必须放在宽泛类别之前。例如 529 也是 5xx，但应被识别为 `overloaded`，因为只有它会推动 fallback 计数。

### 11.2 状态码提取

`_status_code()` 依次检查异常的：

```text
exc.status_code
exc.response.status_code
```

只接受整数，找不到则返回 `None`。

### 11.3 error code 提取

`_error_code()` 依次尝试：

```text
exc.code
exc.body["code"]
exc.body["error"]["code"]
```

这让 `context_length_exceeded` 和 `prompt_too_long` 不必依赖英文 message。

### 11.4 `Retry-After` 提取

`_retry_after_seconds()` 支持：

- `retry-after-ms`：毫秒转秒。
- `retry-after` 数字：秒。
- `retry-after` HTTP date：计算距当前 UTC 的秒数。

负值会归零。无法解析时继续检查异常链。

当前对普通 `dict` 使用小写 key 查询；官方 SDK 的 header 容器通常大小写不敏感，但自行构造大小写敏感的 `{"Retry-After": ...}` 不会命中。

## 12. 退避算法 `retry_delay()`

如果 Provider 返回 `Retry-After`，优先使用并限制到 `[0, max_delay]`。

否则：

```text
base = min(base_delay * 2^retry_index, max_delay)
jitter = base * jitter_ratio * clamp(random_value, 0, 1)
delay = base + jitter
```

默认延迟基础序列：

| retry index | base | jitter 范围 |
| ---: | ---: | ---: |
| 0 | 0.5s | 0 - 0.125s |
| 1 | 1s | 0 - 0.25s |
| 2 | 2s | 0 - 0.5s |
| 3 | 4s | 0 - 1s |
| 4+ | 8s | 0 - 2s |

抖动用于减少多个客户端同时恢复后再次同步撞向 Provider 的惊群效应。

一个精确边界是：代码先把 `base` 限制为 8 秒，再加 jitter，所以最终随机延迟最高可到 10 秒；`max_delay_seconds` 并不是最终延迟的严格上限。

## 13. `LLMClient.chat()` 传输恢复

### 13.1 初始化

每次 `chat()` 创建：

- `call_id`：关联一次逻辑 LLM call 的所有 attempt 和 recovery trace。
- `started_at`：计算累计时间预算。
- `state`：优先取当前 `recovery_scope`，否则临时创建。
- `active_model`：优先取 `state.current_model`。
- `attempts=0`。

这里“一次逻辑 LLM call”可以包含多次真实 SDK 请求。

### 13.2 核心循环

简化后的流程：

```python
while True:
    attempts += 1
    try:
        response = _chat_once(same_messages, model=active_model)
    except Exception as exc:
        classified = classify_or_reuse(exc)
    else:
        state.consecutive_overloads = 0
        break

    can_retry = classified.retryable and attempts <= max_retries
    can_retry = can_retry and elapsed_plus_delay_within_budget

    if can_retry:
        maybe_switch_fallback()
        emit_retry()
        sleep(delay)
        continue

    maybe_emit_exhausted()
    raise normalized_llm_error
```

每次 retry 都重发相同 messages、tools、tool choice、max tokens 和 extra body；只有 fallback 时 model 改变。

### 13.3 两类异常入口

如果 `_chat_once()` 已抛 `LLMClientError`，Client 直接复用它携带的 kind 和 retryable。

如果是 SDK 原始异常：

1. `classify_llm_error()` 统一分类。
2. `_to_llm_client_error()` 包装为带 Provider 标签的 `LLMClientError`。
3. context length 被专门包装为 `LLMContextLengthError`。

这样 Agent 不需要依赖 OpenAI 或 Anthropic 的具体异常类。

### 13.4 elapsed budget 的真实语义

Client 检查：

```python
elapsed_seconds + next_delay <= max_retry_elapsed_seconds
```

它决定“是否允许等待并启动下一次请求”，不是整次 chat 的硬 deadline。下一次 SDK 请求本身仍可能让总耗时超过该值，单次请求上限由 `LLMClient.timeout` 控制。

## 14. 模型 fallback

fallback 只由连续 `overloaded` 触发：

```text
primary attempt 1 -> 529, count 1
primary attempt 2 -> 529, count 2
primary attempt 3 -> 529, count 3
fallback attempt 4 -> success
```

满足以下所有条件才切换：

1. 配置了 `fallback_model`。
2. 连续 overload 达到 `fallback_after_overloads`。
3. 当前还不是 fallback model。
4. 本次错误仍有 retry budget。

切换时更新：

```python
state.current_model = fallback_model
state.fallback_used = True
state.consecutive_overloads = 0
```

成功后当前 Agent run 的后续 LLM step 继续使用 fallback。下一次 `Agent.run()` 才回到 primary。

当前不会因 rate limit、timeout 或 generic 5xx 切换模型，也没有预检 fallback 是否拥有相同上下文窗口和 Tool Calling 能力。这是简化配置的明确边界。

## 15. 为什么关闭官方 SDK 重试

OpenAI 和 Anthropic SDK 都可能自带 retry。项目在实际调用前执行：

```python
client.with_options(max_retries=0)
```

如果 fake/compatible client 不支持 `with_options`，则保留原对象。

关闭 SDK retry 的原因：

1. 避免 SDK retry 与 Harness retry 相乘，导致请求次数和等待时间失控。
2. 统一 OpenAI/Anthropic 的行为。
3. 能为每次 retry、fallback 和 exhausted 记录一致 Trace。
4. Harness 可以共享 Agent run 内的 fallback 状态。
5. 测试可以精确断言真实请求次数。

代价是项目需要自己跟进 Provider 新错误类型和 header 约定。

## 16. 工具副作用与重试安全

传输重试发生在 `LLMClient.chat()` 内部，Agent 只有在 chat 成功返回后才执行 tool call：

```text
LLM request -> accepted response -> Agent executes tool -> tool result feedback
```

假设第一轮模型成功返回 `increment()`，Agent 执行一次；第二轮 LLM feedback 遇到 429，那么 Client 只重试第二轮 LLM 请求，不会倒退并重新执行第一轮工具。

测试 `test_transport_retry_does_not_repeat_completed_tool_side_effect` 明确断言工具只执行一次。

仍需区分 timeout 的服务端语义：Provider 可能已经生成响应但客户端没收到，retry 可能产生重复模型费用；不过响应尚未交给 Agent，因此不会产生本地工具副作用。

## 17. Agent 上下文超限恢复

`context_length` 被 LLMClient 标记为不可传输重试，并抛出 `LLMContextLengthError`。Agent 捕获后：

1. 检查 `reactive_retries` 是否超过 `ContextManager.max_reactive_retries`。
2. 发出 `context_compact` Notice。
3. 调用 `ContextManager.recover(messages, run_id)`。
4. 原地替换 `messages[:]`。
5. 发出 ContextUpdate 对应事件。
6. `continue`，用压缩后的消息重试当前 Agent step。

`ContextManager.recover()` 会：

1. 持久化超大工具结果。
2. micro compact 工具结果。
3. 写原始 transcript。
4. 尝试 LLM summary，并只保留更少的最近消息组。
5. 如果摘要失败或仍太长，执行 deterministic hard trim。

默认响应式恢复一次。再次 context length 时发出 `exhausted`，然后抛：

```text
LLM context remained too long after reactive compaction.
```

### 17.1 为什么 Agent step 不增加

上下文压缩没有完成一次工具行动，只是修复同一个模型请求。因此它消耗 SDK call，但不消耗 `step_index`。

### 17.2 与主动压缩的关系

正常情况下，`ContextManager.prepare()` 会在达到阈值前主动压缩。reactive recovery 是 Provider 仍然拒绝请求时的最后防线，二者不是重复逻辑。

## 18. Agent 输出截断恢复

`LLMResponse.is_output_truncated` 统一识别：

```text
length
max_output_tokens
max_tokens
```

### 18.1 第一次截断：提升输出上限

Agent 计算：

```python
requested = output_max_tokens or llm.max_tokens
escalated = max(requested, policy.escalated_max_tokens)
```

如果尚未 escalation 且目标更大：

1. 标记 `output_escalated=True`。
2. 设置下一次调用的 `max_tokens`。
3. 发出 `output_escalate`。
4. 丢弃当前截断草稿，不追加到 messages。
5. 重发原请求。

丢弃草稿可以避免模型在不完整文本上续写，也让它利用更大上限重新组织完整的 tool call 或回答。

### 18.2 仍然截断：保存片段并续写

提升上限后仍截断，且响应不含 tool call 时：

1. 把 assistant 片段追加到 messages。
2. 把文本加入 `partial_outputs`。
3. 注入内部 `CONTINUATION_PROMPT`。
4. 增加 `output_continuations`。
5. 发出 `continuation`。
6. 重试同一 Agent step。

正常结束时：

```python
final_content = "\n".join([*partial_outputs, response.content])
```

对外只返回组合后的完整文本。

### 18.3 预算耗尽

续写次数达到 `max_continuations` 后：

- 有可用文本：返回 `AgentRunResult(status="incomplete")`。
- 没有可用文本：抛 RuntimeError。

返回 incomplete 而不是伪装 completed，让 Workflow 能继续做阶段恢复。

### 18.4 截断 tool call 为什么不能执行

如果截断响应中包含 tool call，Agent 发出：

```text
reason = truncated_tool_call
none were executed
```

然后直接失败。

即使 SDK parser 给出了结构化参数，也无法证明模型已经完整表达调用意图。执行可能缺参数或执行错误目标，特别是写文件、Shell 等非幂等工具，因此当前选择 fail closed。

## 19. 一条完整恢复链路

假设主模型连续过载，fallback 后输出又被截断：

```text
Agent.run()
  creates RecoveryState(primary="model-a")
  enters recovery_scope
  |
  +-- Agent step 1 calls LLMClient.chat()
      |
      +-- attempt 1 model-a -> 529
      |   recovery.retry, sleep 0.5s
      +-- attempt 2 model-a -> 529
      |   recovery.retry, sleep 1s
      +-- attempt 3 model-a -> 529
      |   recovery.fallback model-a -> model-b
      |   recovery.retry, sleep 2s
      +-- attempt 4 model-b -> stop_reason=max_tokens
  |
  +-- Agent discards first truncated draft
  |   recovery.output_escalate 4096 -> 8192
  |
  +-- same Agent step calls chat() again
  |   state.current_model remains model-b
  |   output still truncated, save part 1
  |   recovery.continuation 1/2
  |
  +-- same Agent step calls chat() again
  |   receives final part
  |   combines part 1 + final part
  |
  +-- emits final AgentEvent and returns completed
  |
  exits recovery_scope and restores parent ContextVars
```

如果最终响应是 tool call，则工具执行成功并回灌后才调用 `reset_output()`，下一轮 tool loop 获得新的输出恢复预算。

## 20. Workflow retry 与 Recovery System 的边界

`SerialCodingWorkflow._run_phase()` 的 `max_phase_retries` 是业务阶段 retry，而不是网络 retry。

每个 attempt 都会：

1. 新建角色 Agent。
2. 新建 messages。
3. 注入 phase task contract。
4. 首次执行时可注入同角色上一阶段 checkpoint。
5. retry 时注入当前阶段上一 attempt 的 bounded checkpoint 和 gate issue。
6. 在同一 Worktree 继续操作。
7. Agent 返回后运行 Evidence Gate。

如果 Agent 正常返回但状态不是 completed，或者 Evidence Gate 不通过，Workflow 可以在预算内新开 attempt。

### 20.1 attempt 不是盲目重头开始

每次 attempt 会记录：

- objective、completed、remaining、next actions。
- changed files 和 Worktree diff hash。
- open tasks、artifact ids。
- verification 结果。
- 最近 actions 和 failures。

下一次 attempt 的 corrective prompt 会注入这些信息，模型从已有 Worktree 和 checkpoint 继续，而不是只看到一句“再试一次”。

### 20.2 provider 异常的当前行为

如果 `agent.run()` 抛异常，`_run_phase()` 会立即 `_fail_phase_after_agent_error()`，不会在同一次 Workflow run 内进入下一个 phase attempt。

这是为了避免在基础设施持续故障时自动重复昂贵角色阶段。阶段状态和 attempt handoff 会持久化，用户修复 Provider 或配置后显式调用 `resume()`。

### 20.3 checkpoint/resume

`WorkflowStore` 记录 phase status、attempts、run ids、artifact ids、错误和 bounded state。

`resume()` 会：

- 跳过有完整 completed checkpoint 的阶段。
- 对 running/failed/incomplete 阶段从 checkpoint 恢复。
- 复用持久化 Worktree。
- 为新 attempt 生成新的 run id。

因此 Recovery System 处理秒级、单请求故障；Workflow resume 处理跨进程、跨会话和人工介入后的恢复。

## 21. 与 learn-claude-code s11 的差异

| 维度 | s11 教学版 | 当前项目 |
| --- | --- | --- |
| Provider | Anthropic 单一示例 | OpenAI/Anthropic SDK 统一适配 |
| 瞬态错误 | 主要 429/529 | 429、529、409、408、timeout、connection、5xx |
| 分类方式 | 字符串和简单异常判断 | 异常链、类名、status、body code、Retry-After |
| SDK retry | 未统一控制 | 显式关闭，Harness 单点拥有 retry |
| retry budget | 固定次数 | 次数 + elapsed gate + Provider timeout |
| fallback 状态 | 单 loop | Agent run 内跨多个 LLM step 保留 |
| context recover | 教学版保留最后 5 条 | 工具结果治理、transcript、LLM summary、hard trim |
| output escalation | 8K 到 64K | 配置化，CLI 默认到 8192 |
| truncated tool call | 未专门保护 | 明确不执行并 fail closed |
| 可观测性 | `print()` | RecoveryNotice -> Trace + AgentEvent + CLI |
| 测试 | 以演示为主 | 请求次数、fallback、两 Provider、副作用和输出恢复测试 |
| 上层恢复 | 单次 Agent loop | Workflow phase retry + checkpoint + resume |

当前实现比教学版更适合真实 Harness，但仍没有覆盖 Claude Code 生产实现中的流式中断、图片修复、服务端 token budget continuation、精细 reason code 和多 endpoint 熔断。

## 22. 关键设计考量

### 22.1 为什么分类和策略分开

`classify_llm_error()` 只回答“发生了什么”；`RecoveryPolicy` 回答“允许怎么恢复”；调用层再结合运行状态回答“现在是否还有预算”。

这样错误识别可复用，策略也容易在测试中替换。

### 22.2 为什么上下文超限不在 LLMClient 压缩

LLMClient 只认识 Provider 消息协议，不拥有 transcript、tool result 文件化、近期消息分组和 system prompt 预算。让它修改 messages 会破坏适配层边界。

Agent 和 ContextManager 拥有完整对话语义，因此由它们恢复。

### 22.3 为什么输出恢复属于 Agent

续写会改变 messages，并需要决定部分输出能否对用户返回、tool call 是否可执行。这不是传输层行为。

### 22.4 为什么不无限重试

无限重试会导致：

- 隐藏永久配置错误。
- 延迟和费用失控。
- Provider 故障时形成持续压力。
- Agent 或 Workflow 永远无法进入可诊断 failed 状态。

所以每一层都有独立预算，并在耗尽时显式返回 incomplete 或抛错。

### 22.5 为什么 Workflow retry 不复用旧 messages

长 attempt 的原始对话可能包含大量工具结果和失败路径。新建 Agent/messages，并注入 bounded checkpoint，可以控制上下文大小和错误累积；真实工作成果仍保留在 Worktree、Task 和 Artifact 中。

## 23. 当前限制与潜在改进

### 23.1 已知边界

1. 退避的最终 delay 可因 jitter 超过 `max_delay_seconds`。
2. elapsed budget 不是整个请求序列的硬 deadline。
3. fallback 只支持一个模型，且只由 overload 触发。
4. 没有模型能力和上下文窗口兼容性预检。
5. 错误分类仍部分依赖 Provider 文本。
6. 普通大小写敏感 header dict 的 `Retry-After` 可能漏读。
7. Recovery callback 异常没有隔离。
8. 没有 circuit breaker、endpoint health score 或全局 retry budget。
9. 非流式 SDK 调用无法保存超时前已经传输的部分 token。
10. timeout retry 可能带来重复 Provider 生成费用。

### 23.2 为什么当前没有立刻增加更多机制

项目目标是可靠的本地 Multi-Agent Coding Harness，不是通用云网关。现有机制已经覆盖评测中最常见的 Provider 抖动、context overflow 和 output truncation。

熔断器、多 endpoint 调度和分布式 retry budget 会引入共享状态、并发控制和更多配置，应由真实评测数据驱动，而不是为了功能数量提前增加复杂度。

## 24. 测试覆盖

`tests/test_error_recovery.py` 主要验证：

- 429 按 `Retry-After` 等待并写 recovery trace。
- 401 不重试。
- timeout、connection、409 和 503 会重试。
- retry budget 耗尽后抛统一错误。
- 连续 529 切换 fallback model。
- 官方 SDK retry 被设置为 0。
- Anthropic 走同一套 transport recovery。
- 首次截断草稿被丢弃并提升输出上限。
- 多段截断输出被组合。
- continuation 预算耗尽返回 incomplete。
- 截断 tool call 永不执行。
- 传输重试不重复已完成工具副作用。
- CLI recovery 输出保持简洁。

其他测试：

- `tests/test_llm_client.py` 验证两 Provider 的 context length 都转成 `LLMContextLengthError`。
- `tests/test_agent.py` 验证 reactive compact 后重试，并把 `context_compacted=True` 暴露给 Hook。
- `tests/test_serial_workflow.py` 验证 phase retry、attempt checkpoint 和 resume。

## 25. 推荐源码阅读顺序

1. `recovery.py::RecoveryPolicy` 与 `RecoveryState`。
2. `recovery.py::classify_llm_error()`。
3. `recovery.py::retry_delay()`。
4. `recovery.py::recovery_scope()` 与 `emit_recovery_notice()`。
5. `llm_client.py::LLMClient.chat()`。
6. `llm_client.py::_to_llm_client_error()` 与 `_without_sdk_retries()`。
7. `agent.py::Agent.run()` 的双 scope。
8. `agent.py::Agent._run_loop()` 的 context/output 分支。
9. `context_manager.py::ContextManager.recover()`。
10. `serial_workflow.py::_run_phase()` 的 gate retry。
11. `workflow_checkpoint.py::build_attempt_checkpoint()`。
12. `tests/test_error_recovery.py`。

## 26. 面试问题与回答要点

### Q1：为什么不用官方 SDK 的自动重试？

关闭 SDK retry，由 Harness 统一控制，可以避免双层重试相乘，并统一两 Provider 的分类、退避、fallback、Trace 和测试语义。

### Q2：为什么 context length 被标成 `retryable=False`，Agent 却还会重试？

它表示“原请求不可原样重试”。LLMClient 不重试相同 messages；Agent 先通过 ContextManager 改写上下文，再发起语义上不同的新请求。

### Q3：重试会不会重复执行工具？

LLM transport retry 完全发生在 `chat()` 返回前。Agent 只对已成功接收的响应执行工具，因此下一轮模型请求的 retry 不会倒退重放工具。截断 tool call 也明确禁止执行。

### Q4：模型 fallback 为什么保留到整个 Agent run？

连续 overload 表明主模型在当前运行窗口不可用。每个 step 都切回主模型会重复等待；run 结束后再恢复 primary，避免永久黏在 fallback。

### Q5：RecoveryState 为什么用 ContextVar？

它让 LLMClient、Notice emitter 等深层代码访问当前 run 状态，同时保持嵌套调用和并发 task 的上下文隔离，避免全局变量串状态，也减少参数贯穿。

### Q6：输出截断为什么先重跑，再续写？

第一次给更大 token budget 重跑原请求，可得到结构完整的回答或 tool call；只有仍截断时才保留片段并续写，降低拼接不一致。

### Q7：为什么 Workflow 还需要自己的 retry？

LLM 请求成功不等于工程阶段完成。Workflow retry 由 Evidence Gate 和 Agent status 驱动，携带 Worktree、Artifact、Task 和 attempt checkpoint，是业务语义恢复。

### Q8：`max_retry_elapsed_seconds` 能保证 300 秒内返回吗？

不能。当前只在 sleep 前检查 elapsed 加 delay，决定是否启动下一 attempt；每个请求仍由 SDK timeout 控制。若需要严格端到端 deadline，要把剩余预算传给每次 SDK call 或使用外层取消机制。

### Q9：当前实现最值得继续改进什么？

优先基于 Trace 统计真实错误分布，再决定是否增加严格 deadline、header 规范化或 callback 隔离。熔断和多 endpoint 调度只有在高并发或多后端场景出现后才值得引入。

## 27. 一句话总结

当前 Recovery System 通过“统一错误事实、分层恢复语义、共享运行状态、有限预算和全链路可观测性”，让瞬态 Provider 故障、上下文超限、输出截断和阶段未完成分别在最合适的层得到处理，同时避免无限重试和工具副作用重放。
