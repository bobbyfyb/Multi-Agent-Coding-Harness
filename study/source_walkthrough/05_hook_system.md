# Hook System 源码精读

> 核心源码：[src/llm_agent/hooks/__init__.py](../../src/llm_agent/hooks/__init__.py)、[src/llm_agent/hooks/](../../src/llm_agent/hooks)  
> 主要调用方：[src/llm_agent/agent.py](../../src/llm_agent/agent.py)、[main.py](../../main.py)、[src/llm_agent/serial_workflow.py](../../src/llm_agent/serial_workflow.py)、[src/llm_agent/subagent.py](../../src/llm_agent/subagent.py)  
> 核心测试：[tests/test_hooks.py](../../tests/test_hooks.py)

## 1. 模块目标

Hook System 是 Agent Harness 的同步控制面扩展机制。

它把容易不断膨胀的横切逻辑从 Agent Loop 中拆出来：

- LLM 调用前注入 Task、Artifact 和 Memory 上下文。
- 工具执行前检查路径、命令和用户授权。
- 工具执行后检查或替换回灌结果。
- 正常结束时提取长期记忆。
- 为每个 Hook callback 自动记录 Trace。

Hook 的核心价值不是“少写几个 `if`”，而是形成稳定扩展协议：

```text
Agent Loop 只声明生命周期时机
        |
        v
HookManager 按注册顺序调度
        |
        +--> 领域 Hook 读取 HookContext
        |
        +--> 返回 HookResult
        |
        v
Agent 按当前事件的语义消费结果
```

职责边界：

| 组件 | 负责什么 |
| --- | --- |
| `Agent` | 决定何时触发 Hook，以及如何解释 HookResult |
| `HookManager` | 注册、顺序执行、合并、短路和 Trace |
| 具体 Hook | 某一项策略或上下文注入逻辑 |
| `ApprovalProvider` | 与 CLI、自动测试或未来 UI 完成用户确认 |
| `security.py` | 结构化路径、已知危险命令和子进程环境的硬约束 |
| Manager | Task、Artifact、Memory、MCP 的状态、检索和持久化 |
| `AgentEvent` | 对外展示已经发生的事情，不参与决策 |
| Trace | 持久化执行过程，不参与决策 |

## 2. 文件地图

| 文件 | 主要职责 |
| --- | --- |
| `hooks/__init__.py` | Hook 类型、上下文、结果、Manager、默认装配 |
| `hooks/permission_hooks.py` | 本地工具路径检查、命令检查和用户确认 |
| `hooks/task_hooks.py` | 当前 Task 状态和计划提醒注入 |
| `hooks/artifact_hooks.py` | 相关 Artifact 排序与注入 |
| `hooks/memory_hooks.py` | 长期记忆召回和正常结束后的记忆提取 |
| `hooks/mcp_hooks.py` | 外部 MCP 工具的 allow/confirm/deny 策略 |
| `task_intent.py` | 判断当前请求是否需要持久 Task 计划 |
| `agent.py` | 五个 Hook 时机中的四种实际消费方式 |
| `trace_system.py` | Hook callback 的 started/completed/failed 记录 |
| `security.py` | Hook 调用的路径和 Shell 安全原语 |

当前 Hook 包约 840 行。核心调度器很薄，大部分代码是具体策略。

## 3. 五个事件槽位

`HookEvent` 声明：

```python
HookEvent = Literal[
    "UserPromptSubmit",
    "BeforeLLM",
    "PreToolUse",
    "PostToolUse",
    "Stop",
]
```

当前项目中的实际状态：

| 事件 | 触发时机 | callback 参数 | Agent 实际消费 |
| --- | --- | --- | --- |
| `UserPromptSubmit` | 当前未触发 | 约定未落地 | 预留槽位 |
| `BeforeLLM` | 每个 Agent step、LLM 请求前 | `HookContext` | 读取 `result.data["messages"]` |
| `PreToolUse` | 参数格式有效、工具真正执行前 | `LLMToolCall, HookContext` | `deny` 阻断；带 reason 的允许结果产生授权事件 |
| `PostToolUse` | Registry 返回结果后 | `LLMToolCall, result, HookContext` | 只消费 `replace`，替换模型收到的结果 |
| `Stop` | 模型无 Tool Call、即将正常返回时 | `messages, HookContext` | 当前只读取记忆提取 data，用于事件展示 |

这里必须区分“通用数据结构支持某动作”和“具体事件消费该动作”。

例如，`HookResult.deny()` 可以从任何 Hook 返回，但当前 Agent 只在 `PreToolUse` 检查 `denied`。`BeforeLLM`、`PostToolUse` 和 `Stop` 返回 deny 并不会自动终止 Run。

## 4. `HookContext`

定义：

```python
@dataclass
class HookContext:
    messages: list[dict[str, Any]]
    step: int = 0
    workdir: Path | str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
```

### 4.1 `messages`

这是 Agent 的持久会话消息列表引用，不是副本。

当前内置 Hook 不直接修改它，而是通过 `HookResult.data["messages"]` 返回临时上下文。这样可以避免 Task、Artifact、Memory 内容永久进入历史，随每轮重复累积。

自定义 Hook 理论上可以原地修改 `messages`，但这会绕过 ContextManager 的显式装配语义，不是推荐模式。

### 4.2 `step`

从 1 开始，表示当前 Agent Loop step。它可以帮助 Hook 做限频或阶段判断。

当前内置 Hook 主要依赖会话、工作目录和 metadata，没有直接用 step 做策略。

### 4.3 `workdir`

`__post_init__()` 将其规范化为绝对 `Path`：

- 未提供时使用 `Path.cwd()`。
- 提供字符串或 Path 时调用 `resolve()`。

这让权限 Hook 和领域 Hook 使用一致的工作区根目录。

### 4.4 `metadata`

Agent 每个 step 创建 HookContext 时写入：

| key | 含义 |
| --- | --- |
| `run_id` | 当前 Run 标识 |
| `agent_id` | Main、PM、Engineer、QA 或 Subagent 标识 |
| `parent_run_id` | 父 Workflow/Subagent Run |
| `depth` | Agent 嵌套深度 |
| `context_compacted` | 本轮前是否刚发生上下文重建 |
| `turn_user_text` | 本次 run 开始时的原始外部用户输入 |
| `called_tools` | 本次 run 已调用工具名的可变集合 |

正常结束前还会写入：

```python
hook_context.metadata["final_content"] = final_content
```

因此 Stop Hook 能同时获得原始用户请求、最终回复、Run ID 和调用过的工具。

`metadata` 是开放字典，降低了新增上下文字段的成本；代价是 key 缺少静态类型检查。

## 5. `HookResult`

`HookResult` 是 frozen dataclass：

```python
@dataclass(frozen=True)
class HookResult:
    action: Literal["allow", "deny", "replace"]
    reason: str | None = None
    value: Any = None
    data: dict[str, Any] = field(default_factory=dict)
```

四个字段的分工：

- `action`：控制语义。
- `reason`：供授权事件、拒绝反馈和 Trace 使用。
- `value`：deny 的替代错误结果或 PostToolUse 的替换结果。
- `data`：多个 Hook 可组合的附加数据。

### 5.1 `allow()`

```python
HookResult.allow(
    reason="bash executes a shell command",
    data={"approved": True},
)
```

允许继续。BeforeLLM Hook 也用 allow 携带临时 messages，它不是权限批准专用结果。

### 5.2 `deny()`

默认生成模型可理解的工具错误：

```python
{
    "ok": False,
    "error": "Permission denied: <reason>",
}
```

也可以通过 `value=...` 自定义反馈。Agent 将该值当作正常 Tool Result 回灌，让模型知道本次动作被拒绝并选择替代方案，而不是直接让整个 Run 抛异常。

### 5.3 `replace_result()`

用于替换已执行工具的结果：

```python
HookResult.replace_result(
    {"ok": True, "result": "trimmed"}
)
```

它不回滚工具副作用，只改变 Agent Event、会话历史和下一轮模型看到的 Tool Result。

### 5.4 便利属性

- `denied` 等价于 `action == "deny"`。
- `replaces_result` 等价于 `action == "replace"`。

这些属性让 Agent 不依赖字符串比较细节。

## 6. `HookManager`

内部状态：

```python
hooks: dict[str, list[HookCallback]]
```

`_default_hooks()` 为五个事件分别创建新列表，避免 dataclass 实例共享可变默认值。

### 6.1 注册

```python
manager.register_hook("PreToolUse", callback)
```

callback 按注册顺序追加。

`register()` 是 `register_hook()` 的简写别名；`trigger()` 同理代理 `trigger_hooks()`。

虽然 `HookEvent` 是 Literal，注册 API 接收普通 `str`。未知事件会通过 `setdefault` 创建，触发未知事件也只会得到空结果。这便于扩展，但拼写错误不会立即报错。

### 6.2 触发算法

`trigger_hooks()` 的流程：

```text
effective_result = None

for callback in hooks[event]:
    记录 hook.<event> started
    调用 callback(*args)
    校验结果是 HookResult 或 None
    记录 completed；异常则记录 failed 并重新抛出

    None:
        继续
    deny:
        立即返回，后续 callback 不再运行
    replace:
        成为新的 effective_result
    allow:
        没有有效结果时保存
        前一个也是 allow 时合并 data

return effective_result
```

### 6.3 短路规则

`deny` 永远立即短路。

这保证后续 Hook 不会把拒绝覆盖为允许，也避免权限拒绝后继续执行依赖“已批准”前提的 callback。

### 6.4 replace 规则

后出现的 `replace` 会覆盖前一个有效 allow 或 replace。

replace 后出现的 allow 不会覆盖 replace，也不会把 allow data 合并进 replace。若多个 PostToolUse Hook 都要修改结果，当前协议是“最后一个 replace 获胜”，不是管道式变换。

### 6.5 allow 合并

`_merge_allow_results()`：

- 两边同名 data 都是 list：拼接。
- 其他冲突：后一个覆盖前一个。
- reason：优先后一个非空 reason。
- 最终仍返回 allow。

这正是 Task、Artifact、Memory 三个 BeforeLLM Hook 能同时注入消息的基础：

```python
{
    "messages": [
        "<current_tasks>...</current_tasks>",
        "<relevant_artifacts>...</relevant_artifacts>",
        "<relevant_memories>...</relevant_memories>",
    ],
    "artifact_ids": [...],
    "memory_ids": [...],
}
```

### 6.6 callback 返回值校验

callback 只能返回 `HookResult` 或 `None`。其他类型触发 `TypeError`。

教学版使用任意非 None 字符串表示阻断，当前实现通过结构化结果避免把“附加信息”和“拒绝”混淆。

## 7. Hook 自身的 Trace

每个 callback 都有独立的相关 ID：

```text
hook-<16 hex chars>
```

执行前记录：

- category：`hook`
- name：`hook.<event>`
- phase：`started`
- data：callback 名称

执行后记录：

- phase：`completed`
- status：`ok` 或 `denied`
- duration
- action、reason、value 和 result_data

发生异常时记录 `failed/error`，然后重新抛出。

`record_trace()` 在没有活动 TraceRecorder 时是 no-op，因此 HookManager 不需要判断当前是不是评测或 Trace 模式。

Trace 系统还会对敏感 key、Secret 文本和超长内容做脱敏/截断。Hook 层负责提供结构化数据，Trace 层负责落盘治理。

## 8. Agent Loop 完整协作链

```text
ContextManager.prepare(messages)
        |
        v
构造 HookContext
        |
        v
BeforeLLM
  Task + Artifact + Memory
        |
        v
runtime messages 临时插入请求
        |
        v
LLM.chat()
        |
        +-- 无 tool call --> final event --> Stop --> memory extraction --> return
        |
        v
LLMToolCall
        |
        v
参数对象检查
        |
        v
PreToolUse
  local permission + MCP permission
        |
        +-- deny --> permission_denied
        |            生成失败 Tool Result
        |            不执行 Registry
        |
        v
ToolRegistry.call()
        |
        v
PostToolUse
        |
        +-- replace --> 替换 Tool Result
        |
        v
tool_result event
        |
        v
LLMClient.tool_result_messages()
        |
        v
下一 Agent step
```

多个工具调用仍按模型给出的顺序逐个经历 Pre/execute/Post，最后统一批量回灌 Provider。

## 9. BeforeLLM：临时上下文注入

Agent 在每个 step 调用：

```python
before_llm_result = hooks.trigger_hooks("BeforeLLM", hook_context)
runtime_messages = _hook_messages(before_llm_result)
```

`_hook_messages()` 只接受 `data["messages"]` 中非空字符串，其他数据保留给事件元数据使用。

ContextManager 将每条临时上下文包装成 user message，并插入“最新外部用户输入”之前：

```text
system
历史消息
<current_tasks>...</current_tasks>
<relevant_artifacts>...</relevant_artifacts>
<relevant_memories>...</relevant_memories>
最新真实用户输入
```

这样既让模型看到当前状态，也让真实用户请求保持请求末尾的高注意力位置。

这些消息只存在于本次 `request_messages` 副本，不写回持久 `messages`。这避免：

- 每个 step 重复累积同一 Task/Memory。
- 被 Provider 当成真实历史长期保存。
- Context Compact 再次摘要大量派生状态。

### 9.1 上下文与 token 预算

BeforeLLM 在初次 `ContextManager.prepare()` 后触发。

Agent 使用 runtime messages 和 tools 一起估算完整请求 token。如果附加上下文使请求超阈值，会再次压缩持久消息，再用同一批 runtime messages 构造请求。

### 9.2 展示去重

Agent 会把已识别标签映射为事件：

- `<current_tasks>` → `task_context`
- `<task_reminder>` → `task_reminder`
- `<relevant_artifacts>` → `artifact_context`
- `<relevant_memories>` → `memory_context`

`emitted_runtime_context` 按标签和内容避免 CLI 重复打印完全相同的上下文。

这只是展示去重。LLM 每个 step 仍会收到当前有效的 runtime context。

## 10. PreToolUse：阻断而不终止 Run

触发前提：

1. 模型已经返回结构化 Tool Call。
2. `arguments` 已经是 JSON object。
3. 还没有调用 ToolRegistry。

若 deny：

1. 不执行工具。
2. 使用 `HookResult.value` 作为结果。
3. 发出 `permission_denied`。
4. 再发出 `tool_result`。
5. 与其他工具结果一起回灌 LLM。

这是一种“可恢复拒绝”。例如模型试图读取 `.env`，收到拒绝 Observation 后仍能继续完成不依赖敏感文件的部分。

如果有效结果带 reason 且未拒绝，Agent 发出 `permission_granted`。这包括用户确认成功，也包括 MCP policy 的显式 allow。

PreToolUse 当前不会：

- 修改 Tool Call arguments。
- 用 replace 跳过真实工具并伪造结果。
- 并行审批多个调用。

## 11. PostToolUse：结果治理

Registry 已经执行完成后调用：

```python
post = hooks.trigger_hooks(
    "PostToolUse",
    tool_call,
    tool_result,
    hook_context,
)
```

若 `post.replaces_result`，Agent 替换本地变量 `tool_result`。

后续三处看到的都是替换值：

- `tool_result` AgentEvent。
- Provider Tool Result 消息。
- 会话历史和后续压缩。

当前默认 HookManager 没有注册 PostToolUse Hook。这个槽位为以下需求保留：

- 更严格的输出裁剪或脱敏。
- 将外部工具结果转换成统一结构。
- 给结果添加 provenance。
- 对测试结果做领域级归一化。

大输出持久化目前主要由 ContextManager 负责，而不是 PostToolUse Hook，避免把历史治理和执行拦截拆成两套不一致规则。

## 12. Stop：正常结束后的收尾

只有模型返回“无 Tool Call 的最终回复”时才触发 Stop。

Agent 在触发前已经：

1. 把 assistant final content 追加到 messages。
2. 发出 `final` AgentEvent。
3. 将 `final_content` 写入 HookContext metadata。

当前默认 Stop callback 是 `MemoryExtractionHook`。

Stop 返回值目前不会：

- 阻止结束。
- 要求模型继续运行。
- 替换最终回复。

它的 data 只被 `_emit_memory_hook_result()` 读取，用于发出：

- `memory_extracted`
- `memory_extract_failed`

以下退出路径不触发 Stop：

- LLM/Hook/工具基础设施抛异常。
- `max_steps`。
- 输出恢复耗尽后返回 `incomplete`。

因此它目前更准确的语义是 `CompletedTurn`，而不是覆盖所有结束原因的 `SessionEnd`。

## 13. UserPromptSubmit：已声明但尚未接线

`_default_hooks()` 和 `HookEvent` 都包含 `UserPromptSubmit`，但当前 `main.py` 和 `Agent.run()` 没有触发它。

目前用户输入直接追加到 messages，然后调用 Agent。输入相关治理主要由：

- CLI `prompt_toolkit`。
- ContextManager。
- BeforeLLM Hook。

所以不能把 UserPromptSubmit 描述成已实现能力。它只是兼容教学模型和未来输入验证、命令路由、会话元数据注入的预留事件。

## 14. 默认装配：`build_default_hook_manager()`

注册顺序：

```text
BeforeLLM:
  1. TaskPlanningHook
  2. ArtifactContextHook       可选
  3. MemoryContextHook         可选

Stop:
  1. MemoryExtractionHook      Memory 启用且允许自动提取时

PreToolUse:
  1. PermissionHook
  2. MCPPermissionHook         MCP 启用时
```

顺序的意义：

- 三个 BeforeLLM allow 结果按 Task → Artifact → Memory 合并。
- 本地 PermissionHook 先执行基础安全策略。
- MCPPermissionHook 只处理 MCP 动态工具。
- 任一 PreToolUse deny 都会短路。

参数中 `task_workdir` 和 `workdir` 可以不同。这是 Workflow Worktree 集成需要的：

- 文件和命令权限以隔离 worktree 为根。
- Workflow Task 状态仍存储在主仓库的 Workflow 任务列表中。

## 15. `TaskPlanningHook`

### 15.1 职责

每个 LLM step 读取 TaskManager 当前状态，并按需要注入：

- `<current_tasks>`：未完成 Task 摘要。
- `<task_reminder>`：计划、claim 或 evidence 提醒。

Hook 不直接替模型创建、claim 或完成 Task。它只向模型提供状态和行为约束，真实变更仍通过 Task Tools 完成。

### 15.2 TaskManager 获取

`_manager()` 根据配置 workdir 和 `task_list_id` 调用：

```python
TaskManager.for_workdir(...)
```

这样同一 Hook 类可用于：

- Main Agent 的 default task list。
- 每个 Workflow 独立的 task list。
- Worktree 执行目录与任务持久化目录分离。

### 15.3 提醒规则

主要规则：

1. 没有 open task，且请求需要多步计划：提醒先 `task_create`。
2. 有 open task 但没有 in_progress：提醒 `task_claim`。
3. 复杂任务全部完成，但存在无 evidence 的 completed task：提醒补证据。

它取“最新外部 user message”，跳过 Task、Artifact、Memory、Summary 等内部标签，避免把 Harness 自己注入的文本误分类成用户需求。

### 15.4 Intent Classifier

有 LLMClient 时默认构建 `TaskIntentClassifier`：

- 对规范化 prompt 缓存判断。
- 用独立小请求要求只返回 YES/NO。
- 输出异常或 LLM 失败时回退到规则判断。
- 规则使用请求长度和 coding 关键词。

因此分类失败不会阻断 Agent 主流程。

### 15.5 去重

`_last_reminder_key` 使用：

```python
(latest_external_user_message, reminder)
```

同一 prompt 下相同 reminder 只注入一次。新用户 prompt 可以再次提醒。

发生 Context Compact 时清空 reminder key，让关键计划约束在摘要重建后可以重新出现。

Task summary 本身不做 Hook 级去重，因为其状态可能随工具调用变化，而且它应该在每个请求中反映最新状态。CLI 事件另有内容去重。

## 16. `ArtifactContextHook`

### 16.1 选择流程

1. 从 TaskManager 找出 open task IDs。
2. 读取未归档 Artifact。
3. 若配置 workflow_id，过滤其他 Workflow 私有 Artifact，同时保留无 workflow_id 的全局 Artifact。
4. 排序后取前 `max_artifacts`，默认 5 个。
5. 使用 `format_artifact_context()` 控制正文长度，默认每项最多 3000 字符。

### 16.2 排序优先级

`_rank()`：

| rank | 条件 |
| --- | --- |
| 3 | Artifact 关联当前 open task |
| 2 | kind 是 `prd` 或 `task_spec` |
| 1 | status 是 `ready` 或 `accepted` |
| 0 | 其他 |

同 rank 内按更新时间、创建时间和 ID 倒序。

这让 Engineer/QA 优先看到当前任务契约，而不是简单读取最近五个文件。

### 16.3 输出

返回 messages 和 artifact_ids。消息供模型使用，IDs 用于 `artifact_context` AgentEvent 和 Trace，便于评测确认实际注入了哪些协议产物。

## 17. Memory Hooks

### 17.1 `MemoryContextHook`

它读取最新外部用户输入，调用：

```python
manager.retrieve_relevant(
    user_text,
    recall_key=run_id,
)
```

召回结果格式化为 `<relevant_memories>`。

MemoryManager 自己负责：

- pinned memory 优先。
- 候选选择和缓存。
- 字符预算。
- 按 `run_id + memory_id` 避免同一 Run 重复增加 use_count。

Hook 只负责在生命周期正确时机连接 MemoryManager 和 Agent 请求。

### 17.2 `MemoryExtractionHook`

正常结束时提取可能值得长期保存的信息。

跳过条件：

- 本轮缺少真实用户文本。
- 用户输入没有记忆信号。
- 本轮已显式调用 `memory_remember`，避免重复记忆。

执行时把原始用户文本、最终回复和 run_id 交给 MemoryManager。Manager 最多解析并保存少量结构化记忆。

提取异常被 Hook 捕获并转换为：

```python
HookResult.allow(data={"memory_error": str(exc)})
```

因此“自动记忆失败”不会让已经完成的用户任务变成失败，只会发出 `memory_extract_failed`。

### 17.3 Workflow 为什么关闭 Stop 提取

Workflow Role Agent 使用：

```python
enable_memory_extraction=False
```

原因是 PM、Engineer、QA 的单阶段回复可能只是中间判断，尚未经过最终 Evidence Gate。若每个角色 Stop 都写长期记忆，会保存重复、冲突或未经验证的结论。

Workflow 完成后由专门 reflection 流程基于验证证据生成长期记忆。

## 18. `PermissionHook`

PermissionHook 同时承担硬检查入口和需要交互确认的策略。

### 18.1 路径工具

`PATH_ARGUMENT_TOOLS`：

- `read_file`
- `write_file`
- `edit_file`
- `search_text`

只要存在字符串 path，就调用 `resolve_workspace_path()`。

以下情况直接 deny，不进入审批：

- 路径逃逸 workspace。
- 访问 `.env`、`.ssh`、`.git`、`.llm_agent`、密钥后缀等敏感路径。

### 18.2 Bash

先调用 `validate_shell_command()`，检查：

- 空命令。
- `sudo`、`rm -rf /`、`shutdown`、`mkfs` 等硬拒绝片段。
- 共享 Python/Conda 环境修改。
- `cd` 到工作区外。

硬检查通过后，默认所有 Bash 都需要用户确认。后台 Bash 使用不同 reason 说明它会启动后台进程。

### 18.3 固定确认工具

以下操作需要确认：

| 工具 | 原因 |
| --- | --- |
| `write_file` / `edit_file` | 修改工作区文件 |
| `run_tests` | 可能同步锁定依赖并执行项目测试代码 |
| `run_lint` | 可能同步锁定依赖并执行 lint 工具 |
| `memory_forget` | 永久删除长期记忆 |
| `worktree_apply` | 修改主工作区 |
| `worktree_remove(discard_changes=true)` | 丢弃隔离修改 |

read、glob、search、Task/Artifact 常规状态操作默认不确认，但仍受工具内部校验。

### 18.4 配置开关

- `confirm_file_mutations`：是否确认 write/edit。
- `confirm_shell_commands`：是否确认 Bash。
- `confirm_destructive_bash`：旧调用方兼容别名；非 None 时覆盖 shell 开关。

关闭确认不等于关闭硬拒绝。路径和命令验证始终先执行。

## 19. ApprovalProvider

`ApprovalProvider` 是 Protocol：

```python
class ApprovalProvider(Protocol):
    def approve(self, request: ApprovalRequest) -> bool: ...
```

`ApprovalRequest` 包含 tool_name、arguments 和 reason。

### 19.1 `CliApprovalProvider`

默认通过终端打印原因和调用参数，接受 `y` 或 `yes`，其他输入均拒绝。

`main.py` 将 prompt_toolkit session 的 `prompt` 注入 `input_func`，让授权输入与增强 CLI 共存。

### 19.2 `AutoApprovalProvider`

固定返回配置布尔值，用于测试和可信的非交互评测环境。

它不是默认的“跳过安全”。即使自动批准，路径/命令硬拒绝仍然优先执行。

### 19.3 无 Provider

需要确认但 provider 为 None 时 fail closed，返回 deny。

这避免无 TTY 的 Workflow 或服务端环境悄悄把“无法询问”解释为允许。

## 20. `MCPPermissionHook`

MCP Tool 名先由 MCPManager 判断和解析为 binding。

binding 带有：

- server_name
- remote_name
- permission
- annotations

三种策略：

| permission | 行为 |
| --- | --- |
| `deny` | 立即拒绝 |
| `allow` | 显式允许 |
| `confirm` | 调用 ApprovalProvider；缺失时拒绝 |

结果 data 会包含 server/tool/policy/annotations，供 Trace 和授权事件解释来源。

MCP 权限独立成 Hook，而不是塞入 PermissionHook，因为 MCP 策略来自动态 server binding；本地工具策略来自 Harness 静态安全规则。二者变化频率和所有权不同。

## 21. 三种运行形态的装配差异

### 21.1 Main Agent

`main.py` 使用 `build_default_hook_manager()`，通常启用：

- Task Planning
- Artifact Context
- Memory Recall
- Memory Extraction
- Local Permission
- MCP Permission

这是能力最完整的交互 Agent。

### 21.2 Workflow Role Agent

PM/Engineer/QA 同样使用默认 builder，但：

- 文件权限 workdir 指向 Workflow Worktree。
- Task 状态 workdir 指向主仓库。
- task_list_id 和 workflow_id 使用当前 Workflow ID。
- Memory Extraction 关闭。
- 仍启用 Memory Recall、Artifact 和权限。

角色的工具能力由 ToolRegistry subset 控制，Hook 再对实际可调用动作做安全检查。

### 21.3 Subagent

Subagent 自己构建精简 HookManager，只注册：

- PermissionHook
- 可选 MCPPermissionHook

其 Task/Artifact 上下文不通过 Hook 注入；Memory 相关上下文由 Subagent 的 Context 构建逻辑按请求单独组织。

这样避免把 Main Agent 的整个会话治理状态隐式复制进隔离子任务。

## 22. Hook、AgentEvent 与 Trace 为什么不冲突

三者位于不同平面：

| 机制 | 方向 | 返回值是否影响执行 | 主要用途 |
| --- | --- | --- | --- |
| Hook | Harness 内部同步控制 | 是 | 允许、拒绝、替换、注入 |
| AgentEvent | Harness → UI callback | 否 | CLI/前端进度展示 |
| Trace | Harness → 持久化记录 | 否 | 调试、评测、复盘 |

一次 write_file 可能产生：

```text
Hook Trace: hook.PreToolUse started/completed
AgentEvent: permission_granted
AgentEvent: tool_result
Trace: permission_granted + tool_result
```

这不是重复职责：

- Hook Trace 说明哪个策略 callback 做了什么决定。
- AgentEvent 说明用户应该看到什么。
- Tool Trace 说明执行结果和耗时。

需要注意：`on_event` callback 当前也是同步直接调用；虽然它的返回值被忽略，但 callback 自身抛异常仍可能中断 Run。语义上应把它实现为可靠的观察者。

## 23. 安全分层

完整安全链：

```text
ToolRegistry subset
  限制角色能够看到哪些工具
        |
        v
JSON Schema / arguments object check
  限制模型协议形状
        |
        v
PreToolUse Permission Hook
  路径/命令硬检查 + 用户确认
        |
        v
Tool 内部 resolve/validate
  防止绕过 Agent 直接调用 Registry
        |
        v
Command Runner safe environment
  隔离 secret env、stdin、cwd、timeout
        |
        v
Manager invariant
  业务状态、版本和所有权校验
```

关键不变式：对于结构化路径工具和当前已识别的 Shell 硬拒绝规则，用户批准不能覆盖检查，因为检查发生在 `_ask_for_approval()` 之前。Bash 本身不是文件系统沙箱，获批命令仍可能通过未识别的 Shell 语法或绝对路径越过这些字符串级规则。

Hook 不是唯一安全边界。代码若直接调用 `ToolRegistry.call()` 会绕过 PreToolUse，所以 Coding Tools 内部必须继续保留路径和命令校验。

## 24. 异常与恢复语义

### 24.1 Hook callback 抛异常

HookManager：

1. 记录 hook failed Trace。
2. 原样重新抛出。
3. Agent.run 记录 run.failed。
4. Run 终止。

它不会默认吞掉未知策略错误，因为权限或结果治理 Hook 半失败后继续，可能产生不确定行为。

### 24.2 领域 Hook 自己降级

可选增强可以局部 fail soft：

- TaskIntentClassifier 失败回退规则分类。
- MemoryExtractionHook 失败转为事件，不影响已完成回答。
- 无相关 Task/Artifact/Memory 时返回 None。

是否降级由领域 Hook 决定，不由 HookManager 一刀切。

### 24.3 Permission deny

deny 不是异常。它是可回灌的 Observation，Run 继续。

这种区别很重要：

- “用户不同意执行命令”是预期业务分支。
- “PermissionHook 代码抛 TypeError”是 Harness 缺陷。

## 25. 测试覆盖

### 25.1 Core 与 Agent 集成

`tests/test_hooks.py` 覆盖：

- 多个 BeforeLLM allow messages 合并。
- PreToolUse deny 确实不执行工具。
- deny 结果被回灌给 LLM。
- PostToolUse replace 影响模型收到的结果。
- BeforeLLM runtime message 不污染持久会话。

### 25.2 Permission

覆盖：

- workspace 逃逸。
- 敏感路径。
- Bash `cd` 逃逸。
- write/edit 审批。
- Bash、tests、lint 审批。
- memory 永久删除。
- worktree apply/discard。

### 25.3 Task

覆盖：

- 复杂任务提醒。
- LLM classifier 和内部消息过滤。
- classifier false。
- 按 prompt 去重。
- compact 后重新注入。
- Workflow task list 隔离。

### 25.4 Artifact、Memory、MCP

分别在领域测试中覆盖：

- Artifact 对 open task 的优先级和 workflow 过滤。
- Memory 临时召回、正常 Stop 提取和事件。
- MCP scope、allow/confirm/deny。
- Hook Trace 出现在完整 Agent Trace 中。

当前明显缺少的 contract tests：

- deny 是否在多个 callback 中严格短路。
- 多 replace 的覆盖顺序。
- callback 非 HookResult 的 TypeError。
- Hook callback 异常 Trace。
- 未接线的 UserPromptSubmit 行为。
- Stop 的非 completed 退出语义。

## 26. 与 learn-claude-code s04 的差异

| 维度 | 教学版 s04 | 当前项目 |
| --- | --- | --- |
| Registry | 全局 `HOOKS` 字典 | 实例化 `HookManager` |
| 返回值 | 任意非 None 值 | 结构化 `HookResult` |
| 调度 | 第一个非 None 就返回 | deny 短路，allow 合并，replace 覆盖 |
| Context | 直接传 block/messages | 统一 `HookContext` + metadata |
| LLM 前注入 | UserPromptSubmit 演示 | BeforeLLM 临时 Task/Artifact/Memory |
| Permission | 字符串 deny list | workspace、敏感路径、环境保护、ApprovalProvider |
| PostToolUse | 只打印大输出警告 | 支持真实结果替换 |
| Stop | 可返回字符串强制续跑 | 当前只做正常完成后的记忆提取 |
| 日志 | `print("[HOOK]")` | AgentEvent + 结构化 Trace |
| Provider | Anthropic block | Provider-neutral `LLMToolCall` |
| 运行形态 | 单 Agent | Main、Workflow Role、Subagent |
| 外部工具 | 无 | MCP 动态权限策略 |

当前实现保留了教学版“循环只暴露稳定时机”的核心思想，但没有照搬“任何非 None 都表示阻断”的教学捷径。

## 27. 与成熟 Coding Agent Hook 的差距

成熟系统通常还支持：

- SessionStart/End、Pre/PostCompact、SubagentStart/Stop 等更多事件。
- 基于工具名、命令或 matcher 的配置化注册。
- 外部命令/脚本 Hook。
- Hook timeout、取消和并发策略。
- 修改 tool input。
- Stop blocking + 防无限续跑状态。
- PostToolUseFailure。
- UI notification 和结构化 permission request。
- 项目级与用户级配置合并。

当前项目没有照搬全部能力，原因是主要目标仍是一个可解释、可评测的 Coding Harness，而不是通用 Hook 平台。五个槽位已经覆盖现阶段的上下文、权限、结果和记忆需求。

面试中应明确说“实现了最小生命周期扩展协议”，不要声称兼容 Claude Code 全量 Hook API。

## 28. 设计取舍

### 28.1 为什么 HookManager 不包含领域逻辑

Manager 只解决调度协议。Permission、Memory、Task 等放在独立 callback 中，可以单测、替换，并避免核心调度器依赖所有 Manager。

### 28.2 为什么同步执行

权限决定必须在工具执行前完成，BeforeLLM 上下文必须在请求构建前完成。同步顺序提供确定性，便于 Trace 和问题复现。

代价是慢 Hook 会直接增加 step 延迟，因此 callback 内的 LLM 调用需要缓存和降级。

### 28.3 为什么不是 Event Bus

Event Bus 适合“发生后通知多个观察者”；Hook 需要“发生前决定是否继续”和“修改结果”。有序返回值和短路规则比纯发布订阅更合适。

### 28.4 为什么 BeforeLLM 使用 data messages

若每类上下文都让 Agent Loop 知道具体 Hook 类型，Loop 会重新耦合 Task/Memory/Artifact。统一 messages 协议后，新增上下文 Hook 不需要修改主链。

### 28.5 为什么 Permission 使用 Provider

Hook 只定义何时问和问什么；CLI、自动评测、未来 Web UI 决定如何获得答案。这是依赖倒置，也让测试无需 monkeypatch `input()`。

### 28.6 为什么 HookContext 不是事件专属类型

单一 Context 降低当前代码量，适合事件数量较少的系统。

代价是 callback 参数签名和 metadata key 只能运行时检查。事件进一步增加时，应考虑 `BeforeLLMContext`、`ToolHookContext` 等类型化协议。

## 29. 当前限制与风险

1. `UserPromptSubmit` 已声明但未触发。
2. HookEvent Literal 没有约束运行时注册，事件拼写错误会静默创建新槽位。
3. 不同事件对 allow/deny/replace 的消费不一致，但类型系统没有表达这种差异。
4. Stop 只覆盖正常 final，不覆盖 failure、max_steps 和 incomplete。
5. Stop 不能阻止结束或续跑，因此没有 stop-loop 防护机制。
6. PreToolUse 不能修改 arguments。
7. PostToolUse 默认没有 Hook，结果治理主要由 ContextManager 后置完成。
8. callback 无 timeout；LLM 分类和记忆检索会增加关键路径延迟。
9. callback 异常会终止 Run，没有全局 fail-open/fail-closed 配置。
10. `CliApprovalProvider` 会打印完整 arguments，敏感信息主要依赖前置路径保护。
11. 权限策略是 Python 代码配置，不支持用户级/项目级声明式规则。
12. `on_event` 观察者抛异常也可能中断 Agent，尚未隔离 UI 故障。

最值得优先补的不是继续增加事件数量，而是：

1. 为 HookManager 合并、短路和异常补齐 contract tests。
2. 根据服务端展示需求决定是否接线 UserPromptSubmit。
3. 若需要 Stop 验收 Gate，再设计事件专属 Result 和防无限重入状态。
4. 只有出现真实用户需求后再做配置化 matcher。

## 30. 面试追问与回答要点

### Q1：为什么需要 Hook，而不是直接写进 Agent Loop？

Agent Loop 应稳定维护 ReAct 状态机。权限、记忆、Task 等是横切策略，独立 Hook 可以组合、替换、单测，并减少修改核心循环带来的回归风险。

### Q2：HookManager 的合并规则是什么？

None 忽略，deny 立即短路；多个 allow 的 list data 拼接、其他字段后者覆盖；replace 成为有效结果，后一个 replace 可覆盖前一个。

### Q3：为什么权限拒绝不抛异常？

拒绝是正常 Observation。将结构化错误作为 Tool Result 回灌后，模型可以换方案；Hook 实现崩溃才是需要终止 Run 的异常。

### Q4：Hook allow 能绕过硬拒绝吗？

对于结构化路径工具和已识别的命令规则不能：检查发生在询问 ApprovalProvider 前，Coding Tool 内部还会再次校验。但 Bash 不是 OS 沙箱，自动批准或人工批准后的命令仍拥有宿主用户权限，不能把 deny-list 描述成完整隔离。

### Q5：Hook 与日志 emit 是否冲突？

不冲突。Hook 是控制面，AgentEvent 是展示面，Trace 是持久化观察面。三者可以描述同一动作，但服务对象和是否影响执行不同。

### Q6：为什么 Task/Artifact/Memory 不直接写入 messages？

它们是可从持久状态重建的派生上下文。临时注入能保证每轮最新、压缩后可恢复，同时避免历史无限重复增长。

### Q7：为什么 Memory Extraction 放 Stop Hook？

只有最终回复生成后，才同时具备原始需求和完整回答。但 Workflow 中间角色的结论未经最终验证，所以关闭阶段级提取，改在 Workflow 验证后反思。

### Q8：为什么 MCP 单独有 Permission Hook？

MCP 工具和策略运行时动态发现，属于外部 server binding；本地工具安全规则由 Harness 静态拥有。拆开后两套策略可独立演进。

### Q9：为什么 Hook 是同步的？

PreToolUse 和 BeforeLLM 都处于必须先完成决策的关键路径。同步有序执行保证确定性和可复现性，后台工作则应由 Background Task System 承担。

### Q10：当前 Stop 和 Claude Code 有什么差别？

当前 Stop 只是 completed turn 的收尾通知，不能阻止退出或续跑，也不覆盖异常退出。成熟系统通常支持 blocking stop、更多结束事件和防无限重入。

### Q11：HookManager 会不会成为 God Object？

目前不会。它只保存 callback 列表并执行协议，领域逻辑、存储和 UI 都在外部。如果未来把权限规则、Memory 检索或事件展示塞回 Manager，才会越界。

### Q12：如何测试 Hook System？

分三层：纯 callback 单测；HookManager 合并/短路 contract test；FakeLLM + Agent 的集成测试，验证工具是否执行、临时消息是否污染历史以及结果是否正确回灌。

## 31. 建议源码阅读顺序

1. `HookContext` 和 `HookResult`。
2. `HookManager.trigger_hooks()`。
3. `_merge_allow_results()`。
4. Agent 的 `BeforeLLM` 调用点。
5. ContextManager 的 `build_request_messages()`。
6. Agent 的 `PreToolUse` 和 `PostToolUse` 调用点。
7. `PermissionHook` 与 `security.py`。
8. `TaskPlanningHook` 和 `TaskIntentClassifier`。
9. `ArtifactContextHook`。
10. `MemoryContextHook` 与 `MemoryExtractionHook`。
11. `MCPPermissionHook`。
12. `build_default_hook_manager()`。
13. Main、Workflow、Subagent 三处装配。
14. `tests/test_hooks.py` 及各领域测试。
15. Trace 中 `hook.<event>` 和对应 AgentEvent。

下一篇适合继续梳理 Security System：Hook 解释了“何时做权限决策”，Security 则解释路径、命令、环境变量和子进程分别有哪些硬约束，以及当前实现为什么仍不等同于 OS 沙箱。
