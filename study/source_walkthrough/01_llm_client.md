# LLMClient 模块源码精读

> 对应源码：[`src/llm_agent/llm_client.py`](../../src/llm_agent/llm_client.py)  
> 主要测试：[`tests/test_llm_client.py`](../../tests/test_llm_client.py)、[`tests/test_error_recovery.py`](../../tests/test_error_recovery.py)、[`tests/test_trace_system.py`](../../tests/test_trace_system.py)

## 1. 模块目标

`LLMClient` 是 Agent 系统与模型 Provider 之间的协议适配层。它不是 Agent Loop，也不执行工具。它负责把上层统一输入转换为 OpenAI 或 Anthropic 官方 SDK 所需的请求，再把不同 SDK 的响应统一为 `LLMResponse`。

它解决五类差异：

1. **客户端差异**：`OpenAI().chat.completions.create()` 与 `Anthropic().messages.create()`。
2. **消息差异**：Anthropic 的 `system` 不在 `messages` 内，工具结果也不是 `role="tool"`。
3. **工具协议差异**：OpenAI 使用 `function.parameters`，Anthropic 使用 `input_schema`。
4. **响应差异**：OpenAI 的工具调用位于 `choices[0].message.tool_calls`，Anthropic 使用 `content` 中的 `tool_use` block。
5. **运行可靠性差异**：统一错误分类、重试、备用模型切换和 Trace，而不让上层依赖 SDK 异常类型。

模块边界可以概括为：

```text
Agent / Context / Memory
        |
        | unified messages + ToolSpec
        v
    LLMClient
        |-- request adapter --> OpenAI SDK
        |-- request adapter --> Anthropic SDK
        |-- response parser --> LLMResponse
        |-- recovery --------> retry / fallback / notice
        `-- observability ----> TraceRecorder
```

`LLMClient` **不负责**：

- 决定下一步是否执行工具；这是 `Agent._run_loop()` 的职责。
- 根据名称寻找并运行工具；这是 `ToolRegistry`、Hook 与工具执行链的职责。
- 管理完整对话、压缩上下文或召回记忆；这些由 Context 和 Memory 模块负责。
- 对截断输出进行续写；`LLMResponse` 只暴露截断状态，续写策略在 Agent 层。

## 2. 文件与依赖地图

### 2.1 直接依赖

`llm_client.py` 依赖三个项目内模块：

| 依赖 | 使用内容 | 作用 |
| --- | --- | --- |
| [`recovery.py`](../../src/llm_agent/recovery.py) | `RecoveryPolicy`、`RecoveryState`、错误分类、退避计算、通知 | 为 SDK 请求提供传输层恢复 |
| [`tool_registry.py`](../../src/llm_agent/tool_registry.py) | `ToolSpec` | 接收 Provider 无关的工具定义 |
| [`trace_system.py`](../../src/llm_agent/trace_system.py) | 当前 Trace 上下文、记录和摘要函数 | 记录 LLM 请求生命周期 |

外部依赖只有官方 SDK 和 `python-dotenv`：

- `openai.OpenAI`
- `anthropic.Anthropic`
- `dotenv.load_dotenv`

两个 SDK 都采用延迟导入：只有真正选择该 Provider 并首次调用时才导入和创建客户端。这样测试可以注入 Fake Client，未使用的 Provider 也不会成为启动条件。

### 2.2 主要调用方

| 调用方 | 如何使用 `LLMClient` |
| --- | --- |
| [`main.py`](../../main.py) | 读取环境变量，构造共享 Client 和 `RecoveryPolicy` |
| [`agent.py`](../../src/llm_agent/agent.py) | 调用 `chat()`；用 `assistant_message()` 和 `tool_result_messages()`维护 Provider 正确的历史 |
| [`context_manager.py`](../../src/llm_agent/context_manager.py) | 调用模型生成上下文摘要 |
| Intent Classifier | 用低 token、低温度请求分类任务意图 |
| Memory 模块 | 用模型完成记忆提取、筛选和反思 |
| Workflow 角色 Agent | PM、Engineer、QA 共享同一适配能力 |

这意味着 `LLMClient` 并非只服务主 Agent Loop，它是所有模型调用的统一基础设施。

### 2.3 源码符号总表

下面按源码出现顺序列出主要符号，便于阅读时按名称反查：

| 符号 | 类型 | 职责 |
| --- | --- | --- |
| `Provider` | 类型别名 | 接受 Provider 名称及 compatible 别名 |
| `ToolChoice` | 类型别名 | 统一工具选择参数的输入范围 |
| `BATCHED_TOOL_INPUTS_KEY` | 常量 | 标记允许分批执行的数组参数 |
| `INVALID_TOOL_INPUT_KEY` | 常量 | 保存无法按 object 执行的原始参数 |
| `BATCHABLE_ARRAY_INPUT_TOOLS` | 常量集合 | 当前允许数组输入恢复的工具白名单 |
| `ChatMessage` | `TypedDict` | 描述最小文本消息形状 |
| `LLMClientError` | 异常类 | 统一 SDK、协议和解析错误 |
| `LLMContextLengthError` | 异常类 | 显式表示不可直接重试的上下文超限 |
| `LLMToolCall` | 不可变 dataclass | Provider 无关的工具调用 |
| `LLMResponse` | 不可变 dataclass | Provider 无关的模型响应 |
| `LLMClient` | dataclass | 模型调用、协议适配、恢复和 Trace 门面 |
| `LLMClient.__post_init__()` | 方法 | 加载环境并补齐 Client 配置 |
| `LLMClient.complete()` | 方法 | 只返回文本的便利调用 |
| `LLMClient.chat()` | 方法 | 管理一次逻辑 LLM 调用的完整生命周期 |
| `LLMClient._trace_request_data()` | 方法 | 生成默认脱敏的请求 Trace 数据 |
| `LLMClient._chat_once()` | 方法 | 按 Provider 分发一次 SDK 请求 |
| `LLMClient.assistant_message()` | 方法 | 重建可回灌的 assistant 消息 |
| `LLMClient.tool_result_message()` | 方法 | 单工具结果便利包装 |
| `LLMClient.tool_result_messages()` | 方法 | 按 Provider 批量构造工具结果消息 |
| `LLMClient._chat_openai()` | 方法 | 构造并执行 OpenAI Chat Completions 请求 |
| `LLMClient._chat_anthropic()` | 方法 | 构造并执行 Anthropic Messages 请求 |
| `LLMClient._openai_client()` | 方法 | 延迟创建并缓存 OpenAI SDK Client |
| `LLMClient._anthropic_client()` | 方法 | 延迟创建并缓存 Anthropic SDK Client |
| `LLMClient._require_model()` | 方法 | 在真正发请求前校验模型配置 |
| `_normalize_provider()` | 函数 | 将 Provider 别名收敛为两个内部值 |
| `_to_llm_client_error()` | 函数 | 将已分类错误转换为公共异常 |
| `_without_sdk_retries()` | 函数 | 关闭注入 Client 可能携带的 SDK 重试 |
| `_openai_local_api_key()` | 函数 | 为无需认证的本地兼容服务提供 SDK 占位 key |
| `_load_dotenv()` | 函数 | 按项目约定寻找并加载 env 文件 |
| `load_llm_environment()` | 公共函数 | 暴露给依赖装配入口的 env 加载函数 |
| `_first_env()` | 函数 | 按优先级返回首个非空环境变量 |
| `_to_openai_messages()` | 函数 | 复制并输出 OpenAI 消息列表 |
| `_to_anthropic_messages()` | 函数 | 分离 system 并转换 Anthropic 特有消息 |
| `_to_openai_tool()` | 函数 | `ToolSpec` 转 OpenAI function tool |
| `_to_anthropic_tool()` | 函数 | `ToolSpec` 转 Anthropic tool schema |
| `_openai_tool_choice()` | 函数 | 转换 OpenAI 工具选择策略 |
| `_anthropic_tool_choice()` | 函数 | 转换 Anthropic 工具选择策略 |
| `_parse_openai_response()` | 函数 | 解析 OpenAI 顶层响应 |
| `_parse_openai_tool_call()` | 函数 | 解析现代 OpenAI tool call |
| `_parse_openai_function_call()` | 函数 | 兼容旧式 OpenAI function call |
| `_parse_anthropic_response()` | 函数 | 解析并规范化 Anthropic content blocks |
| `_parse_anthropic_tool_call()` | 函数 | 解析 Anthropic `tool_use` block |
| `_parse_arguments()` | 函数 | 严格解析 OpenAI JSON object 参数 |
| `_normalize_tool_input()` | 函数 | 防御性处理 Anthropic-compatible 参数偏差 |
| `_response_to_dict()` | 函数 | 统一 dict、`to_dict()` 和 Pydantic 响应 |
| `_content_to_text()` | 函数 | 从字符串或 text blocks 提取文本 |
| `_tool_call_id()` | 函数 | 接受调用对象或字符串并取得 id |
| `_serialize_tool_result()` | 函数 | 把非字符串工具结果编码为 JSON |
| `_empty_parameters()` | 函数 | 生成合法的空 object schema |
| `_combined_extra_body()` | 函数 | 合并 Client 级和调用级扩展请求体 |

## 3. 公共数据结构

### 3.1 `Provider` 与 `ToolChoice`

```python
Provider = str
ToolChoice = str | Mapping[str, Any] | None
```

`Provider` 没有定义成 `Literal["openai", "anthropic"]`，因为构造时还允许 `openai-compatible`、`anthropic-compatible` 和 `claude` 等别名。`__post_init__()` 最终会把它们收敛为 `openai` 或 `anthropic`。

`ToolChoice` 支持三类输入：

- `None`：不显式传递，由 Provider 使用默认策略。
- 字符串：`auto`、`none`、`required`，或者指定工具名。
- Mapping：调用方已经提供 Provider 所需的高级结构时原样透传副本。

### 3.2 `ChatMessage`

```python
class ChatMessage(TypedDict):
    role: Literal["system", "user", "assistant"]
    content: str
```

它描述最简单的公共消息，但真实方法签名是：

```python
Sequence[ChatMessage | Mapping[str, Any]]
```

原因是 Tool Calling 历史会出现结构化 content、`role="tool"`、`tool_calls` 等字段，无法只用这个窄类型表达。这里的 `ChatMessage` 更像“普通文本消息的便利类型”，不是完整协议模型。

### 3.3 `LLMToolCall`

```python
@dataclass(frozen=True)
class LLMToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    raw: dict[str, Any]
```

字段含义：

- `id`：用于把工具结果与调用对应起来。
- `name`：交给 `ToolRegistry` 查找处理器。
- `arguments`：统一后的参数对象，上层无需解析 OpenAI 的 JSON 字符串。
- `raw`：保留 Provider 原始 block，便于回灌和诊断。

`frozen=True` 防止工具调用在执行链中被无意修改。参数字典自身仍是可变对象，因此它提供的是浅层不可变，而不是深不可变。

### 3.4 `LLMResponse`

```python
@dataclass(frozen=True)
class LLMResponse:
    content: str
    tool_calls: list[LLMToolCall]
    raw: dict[str, Any]
    stop_reason: str | None
    usage: dict[str, Any]
```

它是 Provider 适配层最重要的稳定输出：

- `content`：从不同响应结构中提取的文本。
- `tool_calls`：统一工具调用列表。
- `raw`：序列化后的完整 SDK 响应，用于构造合法 assistant 历史。
- `stop_reason`：保留 Provider 的停止原因。
- `usage`：保留 token 统计，但不强制统一不同 Provider 的字段名。

三个属性提供上层常用语义：

- `text`：`content` 的别名。
- `has_tool_calls`：是否包含结构化工具调用。
- `is_output_truncated`：兼容 `length`、`max_output_tokens` 和 `max_tokens`。

这里没有强行统一所有 usage 字段，是因为 Provider 和兼容服务对 usage 的命名并不完全一致。当前上层主要聚合和展示它，过早定义严格 schema 反而容易丢信息。

### 3.5 异常类型

`LLMClientError` 是对外统一异常，除错误文本外携带：

- `kind`：如 `timeout`、`connection`、`rate_limit`、`authentication`。
- `retryable`：是否允许传输层重试。
- `status_code`：若 SDK 异常中可以提取 HTTP 状态码则保留。
- `retry_after_seconds`：支持服务端 `Retry-After`。

`LLMContextLengthError` 是特化子类。它明确不可重试，使 Context Manager 或 Agent 能把“上下文太长”与普通网络失败区分开。

## 4. `LLMClient` 的配置与初始化

### 4.1 dataclass 字段分组

`LLMClient` 的字段可以按职责分为五组：

| 分组 | 字段 |
| --- | --- |
| Provider | `provider`、`model`、`base_url`、`api_key` |
| 生成参数 | `max_tokens`、`temperature` |
| SDK 请求 | `timeout`、`extra_headers`、`extra_body` |
| 恢复 | `recovery_policy`、`retry_sleep`、`retry_random` |
| 依赖注入 | `openai_client`、`anthropic_client` |

`retry_sleep` 和 `retry_random` 看似只是内部函数，却被设计成字段，主要是为了确定性测试：测试可以收集等待时长并固定 jitter，而不需要真的 sleep。

SDK Client 同样可注入。单元测试因此只验证传给 SDK 的参数和解析结果，不访问网络。

### 4.2 `__post_init__()`

dataclass 自动生成 `__init__()` 后，`__post_init__()` 完成三件事：

1. 加载 `.env`。
2. 规范化 Provider 名称。
3. 为未显式提供的配置读取环境变量。

配置优先级是“构造参数优先，环境变量补缺”，并且 dotenv 使用 `override=False`，不会覆盖进程已经存在的变量。

模型变量按以下顺序读取：

```text
LLM_MODEL
-> OPENAI_MODEL / ANTHROPIC_MODEL
-> MODEL_ID
-> LLM_MODEL_ID
```

API Key 顺序：

```text
OPENAI_API_KEY / ANTHROPIC_API_KEY
-> LLM_API_KEY
```

Base URL 顺序：

```text
LLM_BASE_URL
-> OPENAI_BASE_URL / ANTHROPIC_BASE_URL
```

当未显式指定 `env_file` 时，依次检查：

```text
src/.env
src/llm_agent/.env
项目根目录/.env
```

`load_llm_environment()` 是公开包装函数，供 `main.py` 在读取 `LLM_PROVIDER`、`LLM_MAX_TOKENS` 等外围配置之前主动加载环境。`LLMClient.__post_init__()` 再调用一次是幂等的防御措施。

### 4.3 Provider 规范化

`_normalize_provider()` 先执行 trim、转小写和下划线转连字符，再映射别名：

```text
openai / openai-compatible / openai-compatible-api -> openai
anthropic / anthropic-compatible / claude          -> anthropic
```

这里的 “compatible” 表示 API 协议兼容，最终仍使用相应官方 SDK 并设置自定义 `base_url`，不是回退到手写 HTTP。

## 5. 公共调用接口

### 5.1 `complete()`

`complete()` 是 `chat()` 的文本便利封装：

```python
text = client.complete(messages)
```

它只返回 `response.content`，适合摘要、分类等只关心文本的简单调用。需要工具调用、usage 或停止原因时必须使用 `chat()`。

### 5.2 `chat()` 的总体流程

`chat()` 不直接拼 Provider 参数，而是负责一层更上游的请求生命周期控制：

```text
生成 call_id，读取 Trace/Recovery 上下文
                |
                v
记录 llm.call started
                |
                v
循环调用 _chat_once()
  | success                 | exception
  |                         v
  |                  分类并统一异常
  |                         |
  |                 可重试且预算充足？
  |                   | yes       | no
  |                   v           v
  |              退避/备用模型   记录失败并抛出
  |                   |
  `-------------------+
                |
                v
重置 overload 状态，记录 completed，返回 LLMResponse
```

它先创建 `call_id` 和计时器，再尝试读取两个 `ContextVar` 上下文：

- 当前 Trace 上下文决定 operation 名和关联信息。
- 当前 Recovery 状态让同一次 Agent Run 内的多个请求共享重试统计和备用模型状态。

如果调用发生在 Agent 之外，没有 Recovery Scope，则创建本地 `RecoveryState`，所以 `LLMClient` 可以独立使用。

### 5.3 `_chat_once()`

`_chat_once()` 是一个很薄的 Provider 分发点：

```python
if self.provider == "openai":
    return self._chat_openai(...)
return self._chat_anthropic(...)
```

Provider 已经在初始化阶段被规范化，因此这里只需要两个分支。恢复逻辑放在分支外层，保证 OpenAI 和 Anthropic 共享完全相同的重试语义。

## 6. OpenAI 请求适配

### 6.1 `_chat_openai()`

该函数构造 Chat Completions 参数：

```python
{
    "model": ...,
    "messages": ...,
    "max_tokens": ...,
    "temperature": ...,
    "tools": ...,
    "tool_choice": ...,
    "extra_body": ...,
    "extra_headers": ...,
}
```

关键细节：

- 当前使用 OpenAI **Chat Completions API**，不是 Responses API。
- `_to_openai_messages()` 对每条消息做浅拷贝，OpenAI 风格本身就是项目公共消息格式的基础之一。
- 单次调用的 `max_tokens`、`temperature`、`extra_body` 可以覆盖 Client 默认值。
- `_combined_extra_body()` 先复制默认字典，再用本次调用值更新。
- 工具列表为空时不传 `tools` 和 `tool_choice`。

### 6.2 OpenAI ToolSpec 转换

项目内部工具定义：

```python
{
    "name": "read_file",
    "description": "Read a file.",
    "parameters": {"type": "object", ...},
}
```

`_to_openai_tool()` 转换为：

```python
{
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "Read a file.",
        "parameters": {"type": "object", ...},
    },
}
```

未提供 parameters 时使用空 object schema，避免发送 `None`。

`_openai_tool_choice()` 的映射规则：

| 公共输入 | OpenAI 请求值 |
| --- | --- |
| `None` | 不传 |
| `auto` / `none` / `required` | 原字符串 |
| `read_file` | `{"type":"function","function":{"name":"read_file"}}` |
| Mapping | 字典副本 |

### 6.3 OpenAI 响应解析

`_parse_openai_response()`：

1. 验证 `choices` 是非空列表。
2. 读取第一项 `choice` 和 `message`。
3. 解析现代 `message.tool_calls`。
4. 兼容旧式 `message.function_call`。
5. 提取文本、`finish_reason` 和 usage。

OpenAI 的 `function.arguments` 通常是 JSON 字符串。`_parse_arguments()` 会：

- 把空值转为 `{}`。
- 接受已经是 `dict` 的兼容服务响应。
- 对字符串执行 `json.loads()`。
- 拒绝无效 JSON 和非 object JSON。

因此 Agent 接收到的 `LLMToolCall.arguments` 始终是字典，不需要了解 SDK 的字符串编码。

## 7. Anthropic 请求适配

### 7.1 `_to_anthropic_messages()`

Anthropic 的 system prompt 是顶层参数，而不是 messages 中的一条消息。转换函数遍历公共消息：

- 所有 `role="system"` 内容被取出，用两个换行连接为 `system`。
- `user` 和 `assistant` 保留结构化 content。
- 通用 `role="tool"` 被转换为 user 消息中的 `tool_result` block。
- 其他 role 直接报错。

返回值是二元组：

```python
(anthropic_messages, system_or_none)
```

这使上层 ContextBuilder 可以继续产生统一 system 消息，而不用知道 Anthropic 的顶层参数约束。

### 7.2 `_chat_anthropic()`

该函数调用：

```python
client.messages.create(
    model=...,
    system=...,
    messages=...,
    tools=...,
    max_tokens=...,
)
```

工具 schema 由 `_to_anthropic_tool()` 转为：

```python
{
    "name": "read_file",
    "description": "Read a file.",
    "input_schema": {"type": "object", ...},
}
```

`_anthropic_tool_choice()` 处理协议差异：

| 公共输入 | Anthropic 请求值 |
| --- | --- |
| `required` | `{"type":"any"}` |
| `auto` / `any` / `none` | `{"type": value}` |
| `read_file` | `{"type":"tool","name":"read_file"}` |
| Mapping | 字典副本 |

### 7.3 Anthropic 响应解析

Anthropic 把文本和工具调用都放在 `content` block 列表中：

```json
[
  {"type": "text", "text": "I will inspect the file."},
  {"type": "tool_use", "id": "call_1", "name": "read_file", "input": {"path": "a.py"}}
]
```

`_parse_anthropic_response()` 遍历 block：

- `text` 进入 `text_parts`。
- `tool_use` 进入统一 `LLMToolCall`。
- 未识别 block 保留在 normalized raw 中，但不转成文本。

解析后会把 `raw.content` 中 `tool_use.input` 替换成规范化参数。这样后续 `assistant_message()` 回灌的是可再次序列化的普通字典，也避免兼容 API 返回异常 input 形状时触发 Anthropic SDK/Pydantic 序列化告警。

### 7.4 兼容服务的异常工具参数

官方 Anthropic 协议要求 `tool_use.input` 是 object，但部分兼容模型曾返回 list。项目没有对所有异常形状立即终止，而由 `_normalize_tool_input()` 分层处理：

| 输入 | 规范化结果 |
| --- | --- |
| `None` / 空字符串 | `{}` |
| dict | 原 dict |
| 只含一个 dict 的 list | 解包为该 dict |
| `artifact_create` 的多个 dict | 放入 `__batched_tool_inputs__` |
| 其他 list 或标量 | 放入 `__invalid_tool_input__` 并附错误信息 |

这里选择“形成可恢复的参数对象”而不是直接抛异常，是为了让 Agent/Tool 层把结构化错误作为 tool result 回给模型，模型还有机会修正调用。`artifact_create` 的批量兼容是一个有意限制范围的例外，不代表所有工具都允许数组参数。

这是适配层中最具工程妥协色彩的一处：它提升了自托管兼容模型的可用性，但也让 `LLMClient` 知道了一个具体工具名。长期可以把“可批量工具”作为 `ToolSpec` 元数据下沉，去掉常量耦合。

## 8. Tool Calling 历史回灌

只把 `LLMResponse.content` 追加进历史是不够的。模型下一轮还需要看到自己刚刚发出的结构化工具调用，以及每个调用对应的结果。

### 8.1 `assistant_message()`

- OpenAI：返回 `raw["choices"][0]["message"]`，保留 `tool_calls`。
- Anthropic：返回 `{"role":"assistant","content": raw["content"]}`，保留 `tool_use` blocks。

使用 raw 而不是根据 `LLMToolCall` 重新生成消息，可以减少协议字段丢失，也更符合各 SDK 对历史消息的要求。

### 8.2 `tool_result_messages()`

OpenAI 对每个结果生成一条独立消息：

```json
{"role":"tool","tool_call_id":"call_1","content":"..."}
{"role":"tool","tool_call_id":"call_2","content":"..."}
```

Anthropic 把同一轮多个结果合并成一条 user 消息：

```json
{
  "role": "user",
  "content": [
    {"type":"tool_result","tool_use_id":"call_1","content":"..."},
    {"type":"tool_result","tool_use_id":"call_2","content":"..."}
  ]
}
```

这种批量回灌遵循 Anthropic 的消息组织方式，也保持“模型一次响应中的多个工具调用，对应用户侧一次结果反馈”的轮次关系。

非字符串工具结果由 `_serialize_tool_result()` 编码为 JSON，并设置 `ensure_ascii=False`，保证中文结果可读。

`tool_result_message()` 只是单结果便利接口，内部仍调用批量版本。Agent Loop 应优先使用批量接口，因为一轮可能有多个 tool call。

## 9. 官方 SDK Client 生命周期

### 9.1 延迟创建与缓存

`_openai_client()` 和 `_anthropic_client()` 的流程相同：

1. 若已有注入或缓存的 Client，直接返回。
2. 延迟导入官方 SDK。
3. 组装 key、timeout、base URL、headers 和 `max_retries=0`。
4. 创建并缓存实例。

Client 复用可以利用 SDK 的连接池，也避免每个 Agent step 反复初始化。

### 9.2 为什么关闭 SDK 自动重试

两个 SDK 初始化时都设置 `max_retries=0`，每次调用前 `_without_sdk_retries()` 还会尝试：

```python
client.with_options(max_retries=0)
```

这是为了避免两层独立重试：

```text
LLMClient 重试 4 次 x SDK 内部重试 3 次 = 最坏 12 次不可见请求
```

统一由 `LLMClient.chat()` 控制后，重试次数、等待时间、Trace 和终端事件保持一致。`with_options()` 的额外防御也覆盖了测试注入或外部构造的 Client。

### 9.3 OpenAI-compatible 的空 Key

`_openai_local_api_key()` 在 base URL 不是 `api.openai.com` 时返回 `"EMPTY"`。许多本地 vLLM/OpenAI-compatible 服务不校验 key，但 OpenAI SDK 仍要求提供一个值；占位 key 解决的是 SDK 客户端校验，不是绕过远端认证。

Anthropic-compatible 路径没有同样的占位逻辑，当前要求通过显式参数或环境变量提供 key。

## 10. 错误恢复流程

### 10.1 异常统一

`chat()` 捕获两类异常：

- 已经是 `LLMClientError`：直接复用其中的分类元数据。
- SDK 或网络原始异常：交给 `classify_llm_error()`，再由 `_to_llm_client_error()` 包装。

上下文长度错误转换为 `LLMContextLengthError`；其余转换为带 Provider 标签的 `LLMClientError`。

### 10.2 重试判定

一次错误只有同时满足以下条件才重试：

```text
classified.retryable == True
attempts <= policy.max_retries
已耗时 + 下一次 delay <= policy.max_retry_elapsed_seconds
```

注意 `max_retries` 表示“首次请求之后最多再试几次”，所以最大请求次数是 `max_retries + 1`。

`retry_delay()` 综合：

- 服务端 `Retry-After`。
- 指数退避。
- 随机 jitter。

重试前通过 `emit_recovery_notice()` 发出事件，Agent 的 `recovery_scope()` 把它转发给 CLI 和 Trace。

### 10.3 过载与备用模型

当错误分类为 `overloaded` 时，`RecoveryState.consecutive_overloads` 累加。达到 `fallback_after_overloads` 且配置了 `fallback_model` 后：

1. `active_model` 切换为备用模型。
2. 更新 RecoveryState。
3. 发出 `fallback` notice。
4. 后续重试请求使用备用模型。

成功后连续过载计数归零。

### 10.4 为什么传输重试不会重复已完成的工具副作用

重试只包围一次 `_chat_once()`，也就是一次 LLM 网络请求。工具执行发生在 `Agent._run_loop()` 收到 `LLMResponse` 之后：

```text
LLM call -> 返回 tool call -> Agent 执行工具 -> 下一次 LLM call
```

如果“下一次 LLM call”因 429 重试，之前工具的结果已经在 messages 中，但 Agent 不会重新进入工具执行分支。因此同一已完成工具不会因为传输重试再次执行。对应行为由 `test_transport_retry_does_not_repeat_completed_tool_side_effect` 覆盖。

## 11. Trace 集成

`chat()` 为每次逻辑调用生成 `llm-...` correlation id，并记录一对生命周期事件：

```text
llm.call started
llm.call completed / failed
```

默认 started 数据包括：

- Provider、模型、operation。
- 消息数量、角色和结构摘要。
- 工具名、tool choice。
- max tokens、temperature。

默认 completed 数据包括：

- 实际使用模型和尝试次数。
- stop reason 和 usage。
- 文本摘要。
- 统一后的工具调用。

默认不记录完整 prompt 和完整响应文本。只有启用 `trace_llm_content_enabled()` 对应配置时才增加原文和完整 tool specs。这样兼顾诊断价值、Trace 大小与敏感信息暴露风险。

如果没有活动的 `TraceRecorder`，`chat()` 跳过记录逻辑，仍可独立运行。operation 则来自 `trace_operation()` 上下文，例如 Memory 模块可以把调用标记为 `memory_select`，而不需要修改 `LLMClient` 接口。

## 12. 与 Agent Loop 的完整协作链

以下是一轮包含两个工具调用的完整过程：

```text
1. Agent 从 ToolRegistry 获取统一 ToolSpec 列表
2. Agent 调用 llm.chat(messages, tools=tool_specs)
3. LLMClient 将 messages/tools 转为当前 Provider 请求
4. 官方 SDK 返回响应
5. LLMClient 解析为 LLMResponse(content, tool_calls, ...)
6. Agent 用 llm.assistant_message(response) 追加 assistant 原始工具调用
7. Agent 经 Hook/Permission/ToolRegistry 执行两个工具
8. Agent 把两个 (LLMToolCall, result) 交给 tool_result_messages()
9. LLMClient 按 Provider 生成合法结果消息
10. Agent 把结果消息加入 history，再次调用 chat()
11. 模型返回无 tool call 的最终文本，Agent 结束本轮
```

简化伪代码：

```python
response = llm.chat(messages, tools=registry.tool_specs())
messages.append(llm.assistant_message(response))

if response.has_tool_calls:
    results = [execute(call) for call in response.tool_calls]
    messages.extend(llm.tool_result_messages(results))
    # 下一轮继续调用模型
else:
    return response.content
```

真正的 [`agent.py`](../../src/llm_agent/agent.py) 还会在这条主链上插入 Context Prepare、BeforeLLM Hook、截断恢复、权限检查、Trace、后台通知和最大步数控制。`LLMClient` 不感知这些策略。

## 13. 与教学版的差异

[`learn-claude-code/s02_tool_use/code.py`](../../../learn-claude-code/s02_tool_use/code.py) 的教学版直接使用 Anthropic：

```python
response = client.messages.create(...)
messages.append({"role": "assistant", "content": response.content})
if response.stop_reason != "tool_use":
    return
```

这种写法非常适合教学，因为一眼就能看到 Anthropic 的 Tool Use 协议。但用于当前项目会产生几个问题：

| 维度 | 教学版 | 当前项目 |
| --- | --- | --- |
| Provider | 只支持 Anthropic | OpenAI/Anthropic 官方 SDK及 compatible endpoint |
| 上层响应 | 直接使用 SDK 对象 | 统一 `LLMResponse` |
| 工具定义 | Anthropic schema 写死 | `ToolSpec` 双向转换 |
| 多工具结果 | 在 loop 中手工拼 block | Provider-aware 批量回灌 |
| 错误 | SDK 异常直接退出 | 分类、预算重试、fallback、统一异常 |
| 可观测性 | print 工具名 | correlation id、Trace、Recovery notice |
| 可测试性 | 依赖全局 Client | SDK Client、sleep、random 均可注入 |

教学版把 Provider 协议与 Agent Loop 放在一起，代码短且直观；当前项目把它们分开，代价是适配层代码更多，收益是 Agent、Memory、Context 和 Workflow 不再重复 Provider 分支。

## 14. 测试覆盖

### 14.1 `test_llm_client.py`

当前直接覆盖：

- OpenAI SDK 参数、ToolSpec 转换与响应解析。
- Anthropic system 提取、tool schema 与响应解析。
- Anthropic-compatible 单元素数组参数解包。
- `artifact_create` 批量参数恢复。
- 两种 Provider 的单个和多个工具结果回灌格式。
- 缺少 model 的前置错误。
- 两种 Provider 的 context length 特化异常。
- 显式 env 文件、`override=False` 和模型/API key 别名。

测试通过注入 Fake SDK Client 检查 `.create()` 实际收到的参数，不访问真实服务。

### 14.2 `test_error_recovery.py`

覆盖：

- 429 和 `Retry-After`。
- timeout、connection、409、503 等暂时性错误。
- 401 不重试。
- 重试预算耗尽。
- 连续 overload 后切换 fallback model。
- 强制关闭 SDK 内建重试。
- Anthropic 与 OpenAI 共享恢复策略。
- 传输重试不重复工具副作用。

输出截断后的增大 token、续写和禁止执行截断 tool call 也在该文件中，但这些是 Agent 层恢复，不属于 `LLMClient` 自身职责。

### 14.3 `test_trace_system.py`

覆盖 LLM Trace 的 operation metadata、默认不记录 prompt 原文、usage、duration 和 correlation id，以及它在完整 Agent Tool Calling 链中的事件顺序。

### 14.4 尚未充分覆盖的边界

- 命名工具 choice 和所有 `none/any/required` 映射组合。
- OpenAI 旧式 `function_call` 的独立回归测试。
- SDK 返回真实 Pydantic 对象时的多版本兼容矩阵。
- malformed JSON、缺失 tool id/name 等解析失败分支。
- 真实 OpenAI、Anthropic 和不同 compatible server 的契约测试。

## 15. 设计思路与取舍

### 15.1 为什么使用官方 SDK，而不是手写 HTTP

官方 SDK 已经处理 URL、认证、超时、响应模型、连接池和 Provider 特有错误。项目只增加必要的协议适配层，可以把精力放在 Agent 行为上，同时保留 compatible endpoint 的接入能力。

代价是要兼容 SDK 对象序列化行为和版本变化，例如 `_response_to_dict()` 同时处理 `dict`、`to_dict()` 和 `model_dump()`。

### 15.2 为什么不让 Agent 直接处理两个 Provider

如果 Provider 分支进入 Agent Loop，下面这些模块都可能重复判断：

- 主 Agent。
- Subagent。
- Context Summary。
- Memory Extraction/Reflection。
- Intent Classification。
- Workflow 中各角色 Agent。

适配层将变化限制在一个模块中，上层只依赖 `LLMResponse` 和消息回灌接口。这是典型的 Anti-Corruption Layer：隔离外部协议，不让其结构污染内部核心逻辑。

### 15.3 为什么保留 `raw`

只保留统一字段看起来更干净，但重新生成 Provider assistant 消息容易漏掉 block 或未来新增字段。保留序列化 raw 有三点价值：

- 准确回灌工具调用历史。
- Trace 和故障诊断。
- 在统一模型尚未覆盖新字段时避免数据直接丢失。

代价是上层仍有机会绕过抽象读取 raw，因此约定只有适配和诊断代码使用它。

### 15.4 为什么恢复放在 Client，而续写放在 Agent

网络超时、429 和 503 发生在“发送同一请求”的传输层，`LLMClient` 最清楚请求是否成功，可以透明重试。

输出截断则已经得到一个有效模型响应。是否丢弃初稿、增加 token、追加 continuation prompt，涉及对话状态和工具副作用，必须由 Agent Loop 决策。

这个边界可以用一句话记忆：

```text
请求没有成功到达稳定响应 -> LLMClient recovery
请求成功但结果不足以继续任务 -> Agent recovery
```

### 15.5 为什么不是完整的“统一消息模型”

当前实现选择轻量 Mapping + 辅助转换，而不是建立大量 Pydantic 消息类。优势是代码少、兼容字段容易透传；劣势是静态类型不够精确，一些协议错误只能运行时发现。

对当前简历项目规模，这是控制复杂度后的选择。若未来支持 Streaming、图片、音频、reasoning block 和更多 Provider，再引入判别联合类型会更合理。

## 16. 真实问题与修复经验

### 16.1 compatible API 返回数组工具参数

评测中 Anthropic-compatible 模型把 `artifact_create.input` 生成为数组，而官方协议和 Tool Registry 期望 object。最初结果是：

- Anthropic SDK/Pydantic 发出 serializer warning。
- 上层报 `Tool arguments ... must be a JSON object`。
- Workflow 在已经完成较多工作后失败。

修复没有简单放宽所有工具，而是：

1. 单元素对象数组安全解包。
2. 仅允许 `artifact_create` 进入显式批处理标记。
3. 其他错误形状转成可回灌的结构化错误。
4. 同步规范化 raw block，避免下一轮重放原始异常对象。

这体现了兼容层的原则：对已观察到的兼容偏差做窄修复，同时保留协议约束。

### 16.2 远程 vLLM 更新后的环境变量不匹配

远程模型部署更新后，Trace 中出现 model 为空。原因之一是部署脚本使用了通用 `MODEL_ID`/`LLM_MODEL_ID` 和 `LLM_API_KEY`，而 Client 原本主要读取 Provider 专用变量。

修复增加通用别名，同时保留清晰的优先级。这里的经验是：compatible endpoint 除了响应协议，还要考虑部署生态常用的配置命名。

### 16.3 超时不能简单归因于系统或模型单侧

长请求超时可能来自 Provider 推理时间、网络中断、SDK timeout 或 compatible gateway。当前实现通过错误分类、Trace duration、attempts 和 Recovery notice 提供定位证据，但不能仅凭一条 timeout 文本断言根因。

## 17. 当前限制与演进方向

当前限制：

1. 仅同步调用，不支持 Async Client。
2. 不支持流式输出，因此 progress 事件来自 Agent 自己生成，不是 token stream。
3. OpenAI 路径仍使用 Chat Completions，尚未适配 Responses API。
4. 公共消息类型较宽，复杂多模态内容缺少严格类型模型。
5. `artifact_create` 批量能力通过工具名常量识别，存在适配层与业务工具的轻微耦合。
6. compatible server 的行为无法仅靠“协议兼容”保证，需要契约测试。

合理的演进顺序不是立即扩充所有 Provider，而是：

1. 为当前 OpenAI/Anthropic 路径补稳定的真实服务 smoke test。
2. 根据展示需求决定是否增加 Streaming。
3. 当出现多模态或第三个 Provider 时，再升级公共消息模型。
4. 将工具参数兼容能力声明为 ToolSpec 元数据，消除具体工具名常量。

## 18. 面试追问与回答要点

### Q1：`LLMClient` 和 Agent 是什么关系？

`LLMClient` 只负责 Provider SDK、消息和 Tool Calling 协议适配，以及单次网络请求恢复；Agent 负责 ReAct 循环、上下文、工具执行、Hook 和终止条件。二者通过 `LLMResponse` 和消息构造接口协作。

### Q2：为什么不直接返回 SDK response？

直接返回会让 Agent、Memory、Context 等调用方都依赖 Provider 类型。统一响应把文本、工具调用、停止原因和 usage 固定下来，同时保留 raw 用于精确历史回灌和诊断。

### Q3：多个 Anthropic 工具结果为什么合并到一条 user 消息？

Anthropic 使用 user content 中的多个 `tool_result` block 表达同一 assistant turn 的结果。合并能保持轮次对应关系，也是其消息协议的自然形式；OpenAI 则要求每个结果是一条 `role="tool"` 消息。

### Q4：为什么关闭 SDK 重试？

避免 SDK 和 Harness 双层重试导致次数不可控、Trace 不完整和等待时间放大。由 Harness 统一控制后，可以实现 Retry-After、总时长预算、fallback model 和用户可见事件。

### Q5：重试会不会重复执行写文件或 Bash？

传输重试只重发当前 LLM 请求，不包围工具执行。工具调用一旦返回并执行，其结果进入历史；下一次 LLM 请求失败只重试该请求，不重新执行上一轮工具。测试覆盖了副作用只发生一次。

### Q6：为什么 compatible 模型的非法工具参数不全部直接报错？

直接终止会浪费长 Workflow 已完成的状态。对可安全识别的偏差做窄规范化，对其余偏差生成结构化错误回灌，让模型自我修正；但不会静默猜测任意参数含义。

### Q7：这个适配层是否过重？

相对教学 Demo 确实更重，但职责仍集中：请求/响应协议、SDK 生命周期、传输恢复和调用 Trace。它避免这些分支散落到 Agent、Context、Memory 和 Workflow。业务工具执行、权限和上下文策略没有进入 Client。

### Q8：如果要支持第三个 Provider，怎么做？

先判断它是否兼容现有两种协议。兼容则复用对应 SDK 路径和别名；不兼容则新增 request adapter、response parser、assistant/tool-result message builder，并继续输出同一 `LLMResponse`。当分支继续增长时可将 Provider adapter 提取为协议对象，而不是让 `LLMClient` 无限扩张。

## 19. 建议源码阅读顺序

第一次阅读不必从头逐行看，可按以下顺序：

1. `LLMToolCall`、`LLMResponse`：先理解统一边界。
2. `LLMClient.chat()`：理解请求生命周期和恢复循环。
3. `_chat_openai()`、`_chat_anthropic()`：理解请求差异。
4. `_parse_openai_response()`、`_parse_anthropic_response()`：理解响应归一化。
5. `assistant_message()`、`tool_result_messages()`：理解多轮 Tool Calling。
6. `_openai_client()`、`_anthropic_client()`：理解 SDK 生命周期。
7. env、序列化和参数辅助函数：补齐兼容边界。
8. 三个测试文件：用可执行例子验证理解。

读完后，再沿 [`agent.py`](../../src/llm_agent/agent.py) 中的 `Agent._run_loop()` 观察 `LLMResponse` 如何进入真正的 ReAct 循环，就能把“模型调用”与“Agent 编排”两层连接起来。
