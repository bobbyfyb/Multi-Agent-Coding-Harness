# LLM Agent 项目复盘与秋招面试准备

## 1. 项目定位

本项目是一个面向 coding agent 场景的轻量级 LLM Agent 框架，核心目标是把大模型调用、工具调用、权限控制、上下文组织、事件输出等能力拆成清晰模块，形成一个可扩展、可测试、可继续演进的 agent runtime。

一句话介绍：

> 我实现了一个支持 OpenAI SDK 和 Anthropic SDK 的 LLM Agent 框架，通过统一的 LLMClient 适配层屏蔽不同厂商 tool calling 协议差异，并在 agent loop 中接入工具注册、权限检查、hook 扩展和事件打印，支持后续扩展为 coding agent。

适合在简历中描述为：

- 基于 Python 实现轻量级 LLM Agent runtime，支持 OpenAI / Anthropic 两类 SDK 的统一调用和 tool calling 输出适配。
- 设计 ToolRegistry、HookManager、PermissionHook、ContextManager 等模块，解耦模型调用、工具执行、权限确认、上下文生命周期和日志展示。
- 实现 bash、文件读写编辑、glob 搜索、Web search 等工具，并通过工作区路径校验、危险命令拦截、用户确认机制降低工具执行风险。

## 2. 当前已实现功能

### 2.1 LLM 调用适配

核心文件：

- `src/llm_agent/llm_client.py`

已支持：

- OpenAI SDK 调用。
- Anthropic SDK 调用。
- OpenAI compatible / Anthropic compatible 形式的 base_url 配置。
- 从环境变量或 `.env` 加载模型、API Key、base_url。
- 统一返回 `LLMResponse`。
- 统一抽象 tool call 为 `LLMToolCall`。
- 将工具定义转换成不同 provider 所需格式。
- 将工具执行结果按 provider 协议回灌给模型。

统一响应结构：

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

这个设计的关键点是：上层 agent 不直接依赖 OpenAI 或 Anthropic 的原始响应结构，而是只处理统一后的 `LLMResponse` 和 `LLMToolCall`。

### 2.2 Agent Loop

核心文件：

- `src/llm_agent/agent.py`

当前 agent loop 的流程：

```text
用户消息
  -> LLMClient.chat()
  -> 判断是否有 tool_calls
  -> 触发 tool_call event
  -> PreToolUse hooks
  -> ToolRegistry.call()
  -> PostToolUse hooks
  -> tool result 回灌给 LLM
  -> 继续下一轮
  -> 没有 tool_calls 时输出 final answer
```

关键设计：

- `Agent.run(messages)` 接收外部维护的 `messages`，因此多轮会话历史可以由调用方保留。
- `max_steps` 控制 agent 最多执行多少轮工具循环，防止模型无限调用工具。
- `on_event` 用于输出中间步骤，如 `step`、`tool_call`、`tool_result`、`final`。
- hook 系统可以影响流程，但 event 系统只负责观察和展示。

### 2.3 ToolRegistry 工具注册机制

核心文件：

- `src/llm_agent/tool_registry.py`
- `src/llm_agent/tools/basic_tools.py`
- `src/llm_agent/tools/search_tools.py`
- `src/llm_agent/tools/__init__.py`

`ToolRegistry` 负责：

- 注册工具。
- 检查重名工具。
- 调用工具函数。
- 将工具定义转换成 LLM 可识别的 tool spec。
- 捕获工具执行异常，并统一返回 `{"ok": False, "error": ...}`。

工具定义结构：

```python
@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]
    func: ToolFunction
```

当前基础工具：

- `bash`：在 workspace 内执行 shell 命令。
- `read_file`：读取 UTF-8 文件。
- `write_file`：写入文件。
- `edit_file`：替换文件中的一段文本。
- `glob`：按 glob pattern 查找文件。
- `search`：通过 SerpAPI 搜索网络信息。

### 2.4 权限与 Hook 系统

核心文件：

- `src/llm_agent/hooks/__init__.py`
- `src/llm_agent/hooks/permission_hooks.py`

Hook 事件：

```text
UserPromptSubmit   用户输入后、进入 LLM 前
PreToolUse         工具执行前
PostToolUse        工具执行后
Stop               agent 最终停止时
```

HookResult 支持三种行为：

```text
allow    允许继续
deny     拒绝当前工具调用
replace  替换工具执行结果
```

权限 hook 当前策略：

- 硬拒绝危险 bash 片段：`sudo`、`rm -rf /`、`shutdown`、`reboot`、`mkfs`、`dd if=` 等。
- 对 `read_file`、`write_file`、`edit_file` 做 workspace 路径越界检查。
- 对 `write_file`、`edit_file` 默认要求用户确认。
- 对部分可能有破坏性的 bash 命令要求用户确认。
- 支持 CLI 确认，也支持测试中的自动确认/拒绝 provider。

为什么用 hook：

- 权限控制属于 agent 执行前后的扩展逻辑，不应该写死在 agent loop。
- 未来可以继续接入审计日志、输出截断、敏感信息脱敏、上下文注入、记忆保存等能力。

### 2.5 ContextManager 上下文生命周期

核心文件：

- `src/llm_agent/context_manager.py`

当前实现：

- `ContextManager`：统一管理初始 system prompt 和运行时 messages。
- `PromptSection` 支持 `name`、`content`、`priority`、`token_budget`。
- 支持 OpenAI / Anthropic 工具消息原子分组，避免压缩拆散调用与结果。
- 支持大工具结果落盘、旧工具结果占位、自动摘要和 reactive compact。
- 压缩前历史保存为 JSONL Transcript。

设计目的：

- system prompt 不直接散落在 agent loop 中。
- 上下文构建、预算、压缩和恢复由同一组件管理。
- 为后续长期记忆和更精确的 token estimator 预留入口。

### 2.6 事件输出系统

核心文件：

- `src/llm_agent/agent.py`

`AgentEvent` 用于把内部执行过程传给外部 UI 或 CLI：

```python
@dataclass(frozen=True)
class AgentEvent:
    type: AgentEventType
    step: int
    data: dict[str, Any]
```

当前事件：

- `step`
- `tool_call`
- `permission_granted`
- `permission_denied`
- `tool_result`
- `final`

与 hook 的区别：

- hook 可以改变 agent 行为。
- event 只负责观察和展示，不改变流程。

这是项目里一个比较重要的边界设计。

## 3. 整体架构图

```text
main.py
  |
  | build_agent()
  v
Agent
  |
  | uses
  +--> LLMClient
  |      |
  |      +--> OpenAI SDK
  |      +--> Anthropic SDK
  |
  +--> ToolRegistry
  |      |
  |      +--> basic_tools
  |      +--> search_tools
  |
  +--> HookManager
  |      |
  |      +--> PermissionHook
  |      +--> future hooks
  |
  +--> ContextManager
  |
  +--> AgentEvent / print_agent_event
```

工具调用时序：

```text
LLM 返回 tool_calls
  |
  v
Agent emit(tool_call)
  |
  v
HookManager.trigger("PreToolUse")
  |
  +-- deny -> 回灌 Permission denied tool_result
  |
  +-- allow -> ToolRegistry.call()
                 |
                 v
              HookManager.trigger("PostToolUse")
                 |
                 v
              回灌 tool_result
```

## 4. 关键设计取舍

### 4.1 为什么使用官方 SDK，而不是直接 HTTP 调用

直接 HTTP 的优点是透明、可控，但缺点是：

- 不同 provider 的接口细节变化时维护成本高。
- 类型和错误处理都要自己做。
- tool calling 的消息结构容易写错。

使用官方 SDK 的优点：

- 请求构造、鉴权、超时、错误类型由 SDK 托管。
- 更贴近官方协议。
- 适配层只需要关注项目内部统一接口。

本项目采用官方 SDK + 自己的轻量适配层：

```text
OpenAI SDK / Anthropic SDK
        |
        v
LLMClient 统一抽象
        |
        v
Agent 不关心底层 provider
```

### 4.2 为什么要统一 LLMToolCall

OpenAI 和 Anthropic 的 tool calling 结构不同。

OpenAI 大致是：

```json
{
  "tool_calls": [
    {
      "id": "call_1",
      "type": "function",
      "function": {
        "name": "add",
        "arguments": "{\"a\":1,\"b\":2}"
      }
    }
  ]
}
```

Anthropic 大致是：

```json
{
  "content": [
    {
      "type": "tool_use",
      "id": "toolu_1",
      "name": "add",
      "input": {"a": 1, "b": 2}
    }
  ]
}
```

如果 agent loop 直接处理这些原始结构，会导致 provider 逻辑污染 agent。统一成 `LLMToolCall` 后，agent 只关心：

```python
tool_call.name
tool_call.arguments
tool_call.id
```

### 4.3 为什么消息历史由调用方维护

`Agent.run(messages)` 接收消息列表，而不是只接收用户字符串。

好处：

- 多轮对话可以保留历史。
- 外部可以插入 system message、memory、summary、tool result。
- 未来可以实现 `AgentSession`，统一封装 history、memory、budget。

当前 `main.py` 中：

```python
messages = agent.new_messages()
messages.append({"role": "user", "content": query})
agent.run(messages, on_event=print_agent_event)
```

同一个 `messages` 在 while loop 中持续复用，因此会话历史不会丢。

### 4.4 为什么 hook 不直接 print

项目中区分了两个机制：

```text
Hook  负责控制行为
Event 负责展示状态
```

如果 hook 直接 print，会带来：

- 输出重复。
- CLI、Web UI、测试环境难以复用。
- 权限逻辑和展示逻辑耦合。

所以权限 hook 只返回 `HookResult.deny(...)` 或 `HookResult.allow(...)`，agent 再 emit `permission_denied` / `permission_granted` 事件。

### 4.5 为什么权限做双层防护

目前有两层安全机制：

1. Tool 层硬约束：例如 `safe_path()` 和 bash deny list。
2. Hook 层策略控制：例如用户确认、路径越界检查、危险命令确认。

原因是：

- hook 是可配置的，可能被替换或关闭。
- tool 是最后一道防线，不应该完全依赖外部策略。

这对于 coding agent 很重要，因为模型可能会生成高风险工具调用。

## 5. 项目亮点

可以在面试中重点强调：

1. 不是简单调用 API，而是做了 provider-agnostic 的 tool calling 抽象。
2. 不是把逻辑堆在一个 while loop 里，而是拆成 LLMClient、Agent、ToolRegistry、HookManager、ContextManager。
3. 支持 Anthropic 官方推荐的多工具结果批量回灌方式。
4. 工具执行前有权限检查和用户确认。
5. event 和 hook 分离，既能输出中间步骤，又能扩展 agent 行为。
6. 有 pytest 覆盖 agent loop、LLMClient、工具注册、hooks、权限拒绝和结果替换。

## 6. 当前不足与后续规划

当前不足：

- 还没有 streaming 输出。
- 工具调用是串行执行，多工具并发还未实现。
- 工具参数只用 JSON schema 描述，还没有用 Pydantic 做运行时强校验。
- 权限规则目前是写在代码里的，后续可以配置化。
- bash 工具仍然基于 `shell=True`，需要更严格的 sandbox 或命令白名单。
- 长期 memory 机制尚未实现，当前已具备 ContextManager 摘要和 UserPromptSubmit hook。
- 还没有 trace 文件、审计日志、token 统计和成本统计。

后续开发方向：

- 支持 streaming：边生成边输出，tool call 阶段仍保持结构化解析。
- 支持异步工具执行：多个独立 tool calls 可以并发执行。
- 引入 Pydantic Tool Schema：统一参数校验、默认值、文档生成。
- 权限策略配置化：例如 `permissions.yaml`。
- 引入短期/长期记忆：对话摘要、用户偏好、项目知识库。
- 继续优化上下文压缩：接入精确 tokenizer 和压缩质量评估。
- 增加审计日志：记录每次工具调用、参数、结果、审批状态。
- 增加 sandbox：进一步限制 bash 和文件写入能力。
- 支持 MCP：把外部工具生态接进 ToolRegistry。

## 7. 校招面试高频问题与参考回答

### Q1：这个项目解决了什么问题？

参考回答：

> 这个项目解决的是 LLM Agent 在真实执行任务时的工程化问题。单纯调用大模型只能得到文本，但 coding agent 需要能调用工具、读取文件、执行命令、修改代码，还要处理权限、安全、上下文和多轮历史。我做的是一个轻量 agent runtime，把模型调用、工具注册、tool calling 解析、权限检查、hook 扩展和事件输出拆成独立模块，方便后续继续扩展。

### Q2：为什么不直接在业务代码里调用 OpenAI 或 Anthropic？

参考回答：

> 因为不同 provider 的 tool calling 协议不一样。如果业务层直接依赖 SDK 原始结构，后面切 provider 或支持 compatible API 时会很难维护。所以我封装了 `LLMClient`，把 OpenAI 和 Anthropic 的请求格式、tool schema、tool result 回灌、响应解析都收敛到一层适配器里。上层 agent 只处理统一的 `LLMResponse` 和 `LLMToolCall`。

### Q3：OpenAI 和 Anthropic 的 tool calling 有什么差异？

参考回答：

> OpenAI 的工具调用通常在 assistant message 的 `tool_calls` 字段里，函数参数是 JSON 字符串；Anthropic 的工具调用在 `content` block 里，`type` 是 `tool_use`，参数是结构化的 `input` 对象。工具结果回灌也不同，OpenAI 用 role 为 `tool` 的消息并带 `tool_call_id`，Anthropic 推荐把多个 `tool_result` block 放到一个 user message 的 content 数组里。我在 `LLMClient` 里分别适配，然后统一暴露给 agent。

### Q4：Agent loop 是怎么工作的？

参考回答：

> Agent loop 每一步先调用 LLM。如果模型没有返回 tool call，就把文本作为 final answer。如果返回 tool call，就先把 assistant message 加入历史，再逐个触发工具执行流程：emit 工具调用事件、执行 PreToolUse hooks、调用 ToolRegistry、执行 PostToolUse hooks、把结果按 provider 协议回灌给模型，然后进入下一步。为了防止死循环，我加了 `max_steps`。

### Q5：多轮会话历史如何保留？

参考回答：

> 我没有让 `Agent.run()` 只接收一个字符串，而是让它接收 `messages` 列表。调用方在外部维护同一个 messages 对象，每次用户输入后 append 新 user message，然后调用 agent。agent 内部会继续 append assistant message 和 tool result message。所以同一个 session 的历史是持续保留的。

### Q6：为什么要设计 ToolRegistry？

参考回答：

> ToolRegistry 解决的是工具定义、工具查找、工具执行和 tool schema 暴露的问题。每个工具注册时提供 name、description、parameters 和实际函数。LLMClient 调用模型前会从 registry 获取 tool_specs，模型返回 tool call 后 agent 再通过 registry 按 name 调用实际函数。这样新增工具只需要新增 tool module 并注册，不需要改 agent loop。

### Q7：如何新增一个工具？

参考回答：

> 新增工具一般三步：先写一个实际函数，再包装成 `ToolDefinition`，最后在 `tools/__init__.py` 里注册。工具需要提供 JSON schema 参数描述，这样模型才能知道怎么调用。因为 ToolRegistry 的接口统一，agent loop 不需要感知具体工具。

### Q8：为什么要做权限系统？

参考回答：

> coding agent 会执行 bash、写文件、编辑文件，如果没有权限控制，模型误调用或者 prompt injection 都可能造成破坏。我的权限系统分两层：工具内部有硬安全边界，比如 workspace 路径限制和危险命令拦截；hook 层做策略控制，比如写文件或危险 bash 前要求用户确认。这样即使 hook 被关闭，工具层也还有最后一道防线。

### Q9：Hook 和 Event 有什么区别？

参考回答：

> Hook 是控制点，可以改变 agent 行为，比如拒绝工具调用、替换工具结果。Event 是观察点，只负责把中间步骤传给 CLI 或 UI，比如打印 tool call、tool result、final answer。两者分离后，权限逻辑不会和展示逻辑耦合，后续接 Web UI 或日志系统也更方便。

### Q10：为什么 hook 返回 HookResult，而不是直接返回字符串？

参考回答：

> 因为字符串表达能力太弱，无法区分允许、拒绝、替换结果，也不好携带 reason、value、metadata。`HookResult` 结构化后，agent 可以根据 `action` 做不同处理，同时 event 输出也能拿到明确的 reason。

### Q11：工具执行失败时怎么处理？

参考回答：

> ToolRegistry 会捕获工具函数抛出的异常，并统一返回 `{"ok": False, "error": ...}`。这样错误不会直接打断 agent loop，而是作为 tool result 回灌给模型。模型可以基于错误信息调整下一步，比如修改参数或换一种方式完成任务。

### Q12：怎么防止 agent 无限调用工具？

参考回答：

> Agent 有 `max_steps` 参数。每完成一轮 LLM 调用和工具结果回灌，step 会递增。如果超过限制还没有 final answer，就抛出异常。CLI 中也可以显式设置 `max_steps=None` 允许无限循环，但默认设计应该保留上限。

### Q13：为什么使用统一 ContextManager？

参考回答：

> 因为上下文不只是初始 system prompt，还包含持续增长的对话、工具结果、任务状态和 Skill。ContextManager 用 section 组织初始 Prompt，并统一负责预算、工具结果落盘、历史摘要和超限恢复。这样 Agent Loop 只负责执行流程，Task 和 Skill 仍保留各自的领域职责。

### Q14：你觉得这个项目最难的点是什么？

参考回答：

> 最难的是把看似简单的 agent loop 拆出合理边界。比如 provider 协议差异应该放在 LLMClient，工具执行放在 ToolRegistry，权限拦截放在 Hook，展示放在 Event。如果边界不清楚，后续加功能会让 agent loop 越来越臃肿。另一个难点是 tool result 的消息格式，OpenAI 和 Anthropic 差异比较大，尤其是 Anthropic 的多工具结果需要按官方推荐方式批量回灌。

### Q15：如果让你继续优化，你会优先做什么？

参考回答：

> 我会优先做三件事。第一是 TraceRecorder，让上下文压缩和工具执行可以完整复盘。第二是 Pydantic 化工具参数，增强参数校验和自动生成 schema。第三是把权限规则配置化，并增加审计日志。之后再做长期 memory、精确 token 估算和异步工具并发。

### Q16：如何处理 prompt injection？

参考回答：

> 当前项目已经有一些基础防护，比如工具层限制 workspace、权限 hook 拦截危险操作、system prompt 强调不要伪造工具结果。但更完整的 prompt injection 防护还需要继续做，比如区分不可信文件内容和系统指令、对高风险工具调用强制确认、记录工具来源、限制可写路径、对网络搜索结果加不可信标记等。

### Q17：为什么 bash 工具有风险？你怎么控制？

参考回答：

> bash 的风险在于它能力过大，可能删除文件、修改系统、泄露环境变量、启动长进程。当前控制方式包括 workspace cwd、timeout、输出长度限制、危险命令硬拦截、PreToolUse 确认。更进一步可以做命令白名单、容器 sandbox、禁止读取敏感环境变量、对命令做 AST 或 shell parser 级分析。

### Q18：测试覆盖了哪些内容？

参考回答：

> 当前测试覆盖了 LLMClient 的 OpenAI/Anthropic tool call 解析和 tool result 消息构造，Agent 的多工具调用回灌、事件输出、多轮历史、max_steps，ToolRegistry 的注册和错误处理，basic tools 的文件读写编辑和路径限制，以及 hooks 的权限拒绝、用户确认和 PostToolUse 替换结果。

## 8. 面试讲项目的推荐顺序

建议 2 到 3 分钟版本：

1. 先讲项目目标：做一个可扩展的 LLM coding agent runtime。
2. 讲整体架构：LLMClient、Agent、ToolRegistry、HookManager、ContextManager。
3. 讲一个核心流程：模型返回 tool call，agent 执行工具，结果回灌，直到 final answer。
4. 讲两个亮点：provider-agnostic tool calling；权限 hook 和 event 分离。
5. 讲后续优化：streaming、Pydantic schema、memory、sandbox。

可以这样说：

> 我这个项目不是只封装一次 API 调用，而是围绕 LLM Agent 的执行链路做了模块化设计。底层 LLMClient 适配 OpenAI 和 Anthropic SDK，把不同 provider 的 tool calling 格式统一成 LLMToolCall；中间 Agent 负责多轮 tool loop 和消息历史维护；工具通过 ToolRegistry 注册；权限检查和用户确认通过 PreToolUse hook 插入；展示则通过 AgentEvent 输出。这样后续我要加 memory、审计日志、更多工具或者 Web UI，都不用大改 agent loop。

## 9. 简历项目描述示例

项目名称：

> 基于 OpenAI / Anthropic Tool Calling 的轻量级 LLM Agent 框架

项目描述：

> 使用 Python 实现一个可扩展的 LLM Agent runtime，支持 OpenAI SDK 与 Anthropic SDK 的统一调用、原生 tool calling、多轮工具执行、权限确认、上下文压缩和事件追踪。项目抽象了 LLMClient、ToolRegistry、HookManager、ContextManager 等模块，用于降低 provider 差异和 agent loop 复杂度。

简历 bullet：

- 封装 `LLMClient` 统一 OpenAI / Anthropic SDK 调用，适配两类 provider 的工具定义、tool call 解析和 tool result 回灌协议。
- 设计 `Agent` 多轮执行循环，支持外部维护会话历史、批量工具结果回灌、`max_steps` 防死循环和结构化事件输出。
- 实现 `ToolRegistry` 和基础工具集，支持 bash、文件读写编辑、glob、Web search 等工具，并统一异常返回。
- 接入 `HookManager` 和 `PermissionHook`，在工具执行前进行危险命令拦截、workspace 路径校验和用户确认，实现 hook 控制与 event 展示解耦。
- 使用 pytest 覆盖 LLM 适配、agent loop、工具注册、权限 hook、上下文构建等核心模块，提升可维护性。

## 10. 可继续完善的工程任务清单

短期：

- 给 `search_tools.py` 补充依赖声明和格式化。
- 增加 `README.md`，写清安装、环境变量、运行方式。
- 增加权限规则配置文件。
- 对 final content 做兼容 API marker 清理，例如过滤末尾 `<tool_call>`。

中期：

- 支持 streaming。
- 支持 async agent loop 和并发工具调用。
- 使用 Pydantic 定义工具 schema。
- 增加 tool call 审计日志。
- 为 ContextManager 接入精确 tokenizer 和压缩摘要质量评估。

长期：

- 实现 memory 模块。
- 支持 MCP 工具生态。
- 引入容器级 sandbox。
- 增加评测集，评估工具调用成功率和任务完成率。
- 提供 Web UI 或 TUI 展示 agent trace。
