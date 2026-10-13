# Memory System 源码精读

> 核心源码：[memory_system.py](../../src/llm_agent/memory_system.py)  
> 生命周期接入：[memory_hooks.py](../../src/llm_agent/hooks/memory_hooks.py)、[hooks/__init__.py](../../src/llm_agent/hooks/__init__.py)  
> 模型工具：[memory_tools.py](../../src/llm_agent/tools/memory_tools.py)  
> 上下文与工作记忆：[context_manager.py](../../src/llm_agent/context_manager.py)、[serial_workflow.py](../../src/llm_agent/serial_workflow.py)、[workflow_checkpoint.py](../../src/llm_agent/workflow_checkpoint.py)、[workflow_store.py](../../src/llm_agent/workflow_store.py)  
> 测试：[test_memory_system.py](../../tests/test_memory_system.py)

## 1. 模块目标与三层记忆

Memory 解决跨会话重复使用稳定知识的问题，例如用户编码偏好、项目测试命令、架构约定和可靠参考资料。复杂 coding task 还需要保留当前进度，但这些进度具有不同生命周期，不能全部放进长期 Memory。

当前实现用三层状态共同完成记忆管理：

| 层次 | 所属组件 | 保存内容 | 生命周期 |
| --- | --- | --- | --- |
| 运行内上下文 | `messages` + `ContextManager` | 当前目标、近期 Tool Call/Result、历史摘要 | 调用方维护的对话；CLI 进程内可跨用户 turn |
| 任务工作记忆 | `WorkflowStore` + `AttemptCheckpoint` | 已完成/剩余工作、失败、验证、文件变化、下一步 | 特定 Workflow，可跨进程 resume |
| 长期记忆 | `MemoryManager` + `MemoryStore` | 用户偏好、稳定事实、持久反馈和参考 | 项目目录内跨会话 |

需要准确描述：`memory_system.py` 本身主要实现第三层。短期管理来自 ContextManager 和 Workflow，而不是 MemoryManager 的另一组方法。

普通 CLI 对话会复用内存中的 messages，但目前没有为任意 Main Agent turn 自动持久化可恢复的工作 checkpoint。持久化 working memory 的完整链路主要存在于串行 Workflow。

## 2. 模块分工与依赖

```text
main.py: shared MemoryManager
   |
   +--> MemoryTools --> remember/search/get/status/delete
   |
   +--> BeforeLLM MemoryContextHook
   |       retrieve --> runtime messages --> ContextManager --> LLM
   |
   +--> Stop MemoryExtractionHook
   |       original user + final reply --> gated extraction --> remember
   |
   +--> Workflow workers: read-only memory
   |       BeforeLLM recall + memory_search/get
   |       attempt checkpoint --> retry/resume working context
   |
   +--> Workflow completed + QA pass
   |       artifacts + verification --> reflect --> remember
   |
   +--> SubagentRunner
           task-based recall --> delegated_memory PromptSection
```

| 组件 | 责任 |
| --- | --- |
| `Memory` | 一条记忆的不可变数据模型 |
| `MemoryStore` | Markdown/YAML 文件读写、索引重建 |
| `MemoryManager` | 更新、去重、状态、召回、抽取和反思策略 |
| `MemoryTools` | 把 Manager 方法适配成模型可调用工具 |
| `MemoryContextHook` | 在 LLM 请求前提供相关记忆 |
| `MemoryExtractionHook` | 在完成的 Main Agent turn 后尝试抽取 |
| `ContextManager` | 组装真实请求并控制上下文大小 |
| `SerialCodingWorkflow` | 工作 checkpoint、角色读权限、成功后反思 |

## 3. 常量、类型与 Prompt

### 3.1 类型和状态

```python
MemoryType = Literal["user", "feedback", "project", "reference"]
MemoryStatus = Literal["active", "stale", "archived"]
```

| Type | 适合保存的内容 |
| --- | --- |
| `user` | 用户明确偏好，例如回答语言、编码风格 |
| `feedback` | 持久指导，例如不要 mock 集成测试数据库 |
| `project` | 项目稳定事实、架构约定 |
| `reference` | 文档入口、可靠命令和外部资源 |

四类主要用于检索描述和数据校验，不是四套独立存储，也没有自动的优先级层级。任何类型都可以 pinned。

### 3.2 ID 与信号检测

`MEMORY_ID_PATTERN` 要求 `mem_` 加 8 至 32 位小写十六进制。新 ID 使用 UUID 的前 12 位。

`MEMORY_SIGNAL_PATTERN` 检测 `remember`、`from now on`、`I prefer`、`记住`、`以后不要`、`我偏好` 等表达。它只决定是否值得发起自动抽取，不是完整意图识别器。

`SECRET_PATTERN` 检测常见 key/value secret、较长 Bearer Token 和私钥头。它是启发式过滤，不能识别所有敏感信息。

### 3.3 四组 Prompt 的职责

| 常量 | 使用位置 | 约束 |
| --- | --- | --- |
| `MEMORY_POLICY_INSTRUCTIONS` | System Prompt section | 当前用户和新证据优先；禁止存进度、猜测和 secret |
| `MEMORY_SELECTION_SYSTEM_PROMPT` | selector LLM call | 只返回相关 memory id 数组 |
| `MEMORY_EXTRACTION_SYSTEM_PROMPT` | turn extraction | 只提取明确、持久的跨会话信息 |
| `MEMORY_REFLECTION_SYSTEM_PROMPT` | workflow reflection | 从已验证交付物和证据中提炼可复用知识 |

记忆内容被定义为 recalled context。它不能覆盖 system instruction、工具权限或 workspace 边界。

## 4. `Memory` 数据模型

`Memory` 是 `@dataclass(frozen=True)`：更新时创建新对象，再写文件。

| 字段 | 含义 |
| --- | --- |
| `id` | 稳定标识，更新不会换 ID |
| `name` | 人可读名称，也参与名称去重 |
| `description` | 一行检索说明 |
| `type` | user/feedback/project/reference |
| `body` | Markdown 正文 |
| `pinned` | 自动召回时优先加入预算 |
| `status` | active/stale/archived |
| `confidence` | 0 至 1 的置信字段，默认 1.0 |
| `evidence` | 例如 `workflow:wf-id`、`file:src/app.py` |
| `last_used_at` / `use_count` | 自动召回统计 |
| `status_reason` | 失效或归档原因 |
| `source` / `source_run_id` | 显式、工具、自动抽取或 Workflow 反思来源 |
| `created_at` / `updated_at` | UTC 时间；写入格式精确到秒 |

`to_dict()` 使用 `asdict()`，并把 evidence tuple 转为 list。

`from_dict(metadata, body)` 负责文件还原和基础校验；旧文件缺少 lifecycle 字段时补默认值：active、confidence=1.0、空 evidence、use_count=0。`stale_reason` 也可兼容为 `status_reason`。

`confidence` 不是系统标定过的正确率。显式/工具/自动抽取通常使用默认值；反思使用模型返回值并做阈值过滤。

## 5. `MemoryStore` 文件存储

### 5.1 目录布局

`MemoryStore.for_workdir(root)` 对应：

```text
root/.llm_agent/memory/
  MEMORY.md
  mem_0123456789ab.md
  mem_abcdef012345.md
```

每条文件包含 YAML frontmatter 和 Markdown 正文：

```markdown
---
id: mem_0123456789ab
name: Project test command
description: Reliable test command for this repository.
type: reference
pinned: false
status: active
confidence: 0.92
evidence:
- workflow:wf-example
source: workflow_reflection
---

Run `uv run pytest` from the repository root.
```

示例省略了部分时间和使用统计字段。

### 5.2 `save()` 与原子替换

流程：

1. `_validate_memory()` 校验模型。
2. 从 `to_dict()` 中分离 body。
3. `yaml.safe_dump()` 序列化 metadata。
4. `_atomic_write()` 写 UUID 临时文件，再 `replace()` 目标。
5. 默认重建 `MEMORY.md`。

原子替换避免单文件读到半写内容，但不等于多文件事务。`save_many()` 逐个保存，最后一次重建索引；中途失败时前面文件可能已经写入。

### 5.3 `load()`、`list()`、`delete()`

- `load(id)`：先校验 ID，再读取对应文件；不存在抛 `MemoryNotFoundError`。
- `list()`：扫描 `mem_*.md` 并解析所有正文；按 pinned、type、name、id 排序。
- `delete(id)`：加载后删除，重建索引，返回被删除对象。

`_parse_memory_file()` 使用 `yaml.safe_load()`，拒绝缺 frontmatter、错误 YAML、非 mapping metadata 和非法 UTF-8。

当前 `list()` 没有隔离单个损坏文件。一个坏文件可能让整个列表和召回失败，这是文件存储需要明确保留的边界。

### 5.4 `MEMORY.md` 的真实作用

索引记录 name、type、status、pinned、confidence 和 description，方便人阅读和审计。

运行时检索调用 `store.list()`，重新生成 `_catalog()`；不会把 `MEMORY.md` 全部注入 system，也不依赖索引解析。独立 memory 文件是事实来源。

这意味着“模型只看目录元数据”成立，但“磁盘只读取选中的正文”不成立：当前 Python 实现先读所有文件，再限制发给 selector 的内容。

## 6. `MemoryManager` 配置与缓存

| 字段 | 默认值 | 用途 |
| --- | ---: | --- |
| `max_items` | 5 | 自动召回最终条数；pinned 也占名额 |
| `max_memory_chars` | 4096 | 每条被召回正文的字符上限 |
| `max_retrieved_chars` | 12000 | 召回内容的字符预算 |
| `max_catalog_chars` | 16000 | selector/extractor/reflection 的目录预算 |
| `max_reflection_items` | 3 | 每次反思最多处理的结果条数 |

这些都是字符限制，不是 tokenizer 精确预算。ContextManager 还会对真实请求估算 token。

两个运行内缓存：

- `_selection_cache`：相同 query 和候选 revision 不重复调用 selector。
- `_recorded_recalls`：同一个 run 中同一 memory 不重复累计 use_count。

它们都不落盘。重新启动后缓存消失，memory 文件仍保留。

## 7. `remember()` 创建与更新

```python
remember(
    name=...,
    description=...,
    body=...,
    memory_type="project",
    pinned=False,
    memory_id=None,
    confidence=1.0,
    evidence=None,
    source="explicit",
    source_run_id=None,
)
```

完整流程：

1. 校验 name、description、body 非空。
2. 检查 description/body 是否含常见 secret。
3. `_find_existing()` 查找已有记忆。
4. 保留已有 ID、created_at、use_count 和 last_used_at。
5. 使用本次参数构造新的 `Memory`。
6. status 设为 active，清除 status_reason。
7. 保存文件、重建索引、清除 selection cache。

### 7.1 去重规则

提供 `memory_id` 时，必须加载这个已有 ID；它不是创建指定 ID 的入口。

未提供 ID 时，按 `_normalize_name()` 后的名称查找：保留英文数字和中文，移除空格、标点，英文小写。

这是名称去重，没有 embedding 相似性或事实级冲突检测。不同名称的同一事实仍可重复；同名的不同事实可能覆盖。

### 7.2 更新不是字段 patch

`remember()` 用本次参数重建正文和 metadata。除明确保留的字段外，旧 evidence、pinned、confidence 等不会自动合并。

更新 stale/archived 条目也会恢复为 active。调用者需要确认这是新的有效事实，而不是无意重新激活旧知识。

## 8. `search()` 与关键词评分

`search(query, limit=10, include_inactive=False)` 是显式搜索 API：

1. 默认只取 active。
2. `_score_memories(..., include_body=True)` 匹配 metadata 和正文。
3. 按得分排序取前 limit。
4. 空 query 返回列表前 limit；非空且无命中返回空。

评分公式：

```text
lexical_score = 3 * metadata 命中词数 + 1 * body 命中词数
score = lexical_score * (0.5 + confidence / 2)
        + min(use_count, 10) * 0.01
```

metadata 是 name、description、type。英文提取长度至少 2 的字母数字词；中文连续片段生成双字组合，长度不超过 4 时也保留完整词。

匹配使用子串包含判断，不是 BM25，也不是向量相似度。使用次数只能给已经有词汇命中的项增加很小的分数，不能把完全无关的项拉进结果。

`search()` 和 `get()` 不增加 use_count。统计的是自动召回，不是所有读取。

## 9. `retrieve_relevant()` 自动召回

```text
all memory files
   -> filter active
   -> pinned + non-pinned candidates
   -> select candidate IDs
   -> combine pinned first
   -> fit item/character budget
   -> record recall
   -> return selected Memory objects
```

### 9.1 pinned 不是无条件全部注入

pinned 只意味着优先。所有记忆仍受 `max_items` 和字符预算限制。pinned 数量过多会挤占普通候选名额。

### 9.2 selector LLM call

`_select_ids_with_llm()` 只给模型发送 name、description、type、status、confidence，不发送完整正文。

请求参数：

```text
operation = memory_select
query preview = 2000 chars
catalog = max_catalog_chars
tools = None
max_tokens = 200
temperature = 0
```

结果仅接受存在于候选集合的 ID，过滤重复并限制条数。

这能用模型语义理解选择少量记忆，但不是向量检索。目录超预算会提前停止追加，靠后条目可能没有机会被模型看到。

### 9.3 fallback 的精确条件

没有 LLM 或 selector 调用抛异常时返回 `None`，随后使用 metadata 关键词评分，`include_body=False`。

LLM 明确返回 `[]` 时，尊重空选择。当前 JSON 无法解析也被 `_parse_json_array()` 转为空数组，因而同样不会触发关键词 fallback。这个行为需要与“异常时降级”区分。

### 9.4 selection cache

cache key 是：

```python
(query.strip(), "|".join(f"{id}:{updated_at}:{status}" ...))
```

Manager 写入/更新/变更状态/删除时清缓存。召回计数更新不改变 updated_at，因此每步记录 use_count 不会导致重新选择。

它不是跨进程缓存。直接修改文件但不更新 metadata 时间，或多个 Manager 并发写入时，不能依靠它保证严格一致性。

## 10. `_fit_budget()` 与召回统计

`_fit_budget()` 遍历 pinned + selected：

1. 达到 max_items 停止。
2. 将 body 截到 max_memory_chars，创建临时 `replace(memory, body=...)`。
3. 估算单条 `format_memory_context()` 的字符长度。
4. 累计超过 max_retrieved_chars 则跳过，继续看下一项。

截断不改原文件。预算估算包含单条 wrapper，偏保守；正文直接按字符截断，可能截在 Markdown 代码块中间。

`_record_recall()` 用 `(recall_key, memory.id)` 去重：Hook 传 run_id，因此同 run 多个 step 只累计一次。没有 recall_key 的直接调用每次累计。

它从 Store 重新加载原对象，再增加 use_count 和 last_used_at，批量保存。返回给调用方的 relevant 对象没有同步替换为更新后的计数；最新统计应重新 `get()`。

集合超过 5000 条后整体清空。selection cache 当前没有容量上限。

## 11. `MemoryContextHook` 请求前注入

默认 HookManager 在 `BeforeLLM` 注册它，流程为：

1. 从 canonical messages 找最近一条外部用户文本。
2. `retrieve_relevant(user_text, recall_key=run_id)`。
3. `format_memory_context()` 包装为 `<relevant_memories>`。
4. 返回 `HookResult.allow(data={"messages": [content], "memory_ids": [...]})`。

Agent 收集 BeforeLLM runtime messages，然后调用：

```python
context_manager.build_request_messages(messages, runtime_messages=...)
```

这个方法复制 messages，把记忆作为 user-role context 插在最近用户请求前面，形成真实 LLM 请求。

canonical messages 不保留这些临时 memory block，因此不会随着 step 累积多份相同记忆。

Agent 用内容缓存避免重复打印相同 `memory_context` 事件，但模型请求仍会每步包含召回内容。事件去重不等于 token 复用或 delta 注入。

## 12. 与 ContextManager 压缩协作

记忆由独立文件保存，而压缩改变的是 canonical history。下一次 BeforeLLM 会重新召回，因此历史摘要或 hard trim 不会永久删除长期记忆。

真实请求 token 估算包含 runtime memories 和 tool schemas；ContextManager 可以为这些额外内容预留空间，再压缩历史。

需要区分三种内容：

- 长期 memory 文件：压缩不修改。
- 临时自动召回 block：每步重建，不进入 canonical history。
- `memory_get`/`memory_search` 的 tool result：属于对话历史，会进入正常工具结果治理和压缩链路。

当前有一个源码边界：Memory Hook 的内部 user prefix 列表未包括 `<recovery_continuation>` 和 `<background_notifications>`。这些内部消息如果成为最新字符串 user message，可能被误当作召回 query，改变非 pinned 记忆选择。Agent 用于 turn extraction 的原始用户文本有更完整的过滤，但 MemoryContextHook 使用的是自己的扫描函数。

这篇文档记录现状；该边界尚没有在这里修改实现。

## 13. `extract_from_turn()` 与 Stop Hook

自动抽取采用“信号门控 + LLM 结构化结果 + Manager 校验”：

```text
Agent completed final response
   -> Stop Hook
   -> original turn_user_text + final_content
   -> should_extract() signal check
   -> LLM memory_extract
   -> JSON array (first 3 items)
   -> remember(source=auto_extracted)
```

`extract_from_turn()` 参数与输入边界：

- 无 LLM 或没有偏好/记住信号：直接返回空。
- 用户文本截至 4000 字符，assistant 至 2000。
- 加入已有记忆目录，帮助避免重复。
- tools=None、max_tokens=800、temperature=0。
- 对结果前 3 个元素处理；非法类型、空字段、secret 等跳过。

### 13.1 为什么取原始 turn metadata

Agent 在循环开始记录 turn_user_text，在最终回答时记录 final_content。Stop Hook 直接使用这两个字段，避免对压缩后的 history 再猜用户原意。

### 13.2 避免工具写入后再抽取

`called_tools` 中出现 `memory_remember` 时，ExtractionHook 跳过自动抽取，减少重复写入。

当前判断依据是“工具被调用过”，不是“工具写入成功”。一次被拒绝或失败的 memory_remember 也可能让本 turn 自动抽取被跳过。

### 13.3 抽取失败如何处理

Hook 捕获异常，在 HookResult data 中返回 `memory_error`；Agent 将其转换成 `memory_extract_failed` 事件。不会因为自动记忆失败让已经完成的主要回答变成失败。

仅正常 completed final 路径触发 Stop；max_steps、output incomplete 或异常退出不会依靠这个 Hook 自动保存进度。Workflow checkpoint 负责这些场景。

## 14. `MemoryTools` 模型操作接口

| 工具 | Manager 方法 | 用途 |
| --- | --- | --- |
| `memory_remember` | `remember()` | 创建/更新长期条目 |
| `memory_search` | `search()` | 显式查找，包括可选 inactive |
| `memory_get` | `get()` | 读取完整对象及 metadata |
| `memory_mark_stale` | `mark_stale()` | 标记与新证据矛盾的条目 |
| `memory_archive` | `archive()` | 停止召回不再有用的条目 |
| `memory_restore` | `restore()` | 恢复 active |
| `memory_forget` | `forget()` | 永久删除 |

`memory_remember` 的 `requires_context=True` 把 ToolExecutionContext 的 run_id 写入 source_run_id，source 为 tool。

工具层负责 schema 和格式化；Manager 保留独立业务 API，Workflow reflection 可以直接复用，无需伪造 tool call。

`memory_search` 返回 name/description 列表，需要正文时调用 `memory_get`。ToolRegistry 再包装统一的 ok/result/error。

PermissionHook 对 `memory_forget` 请求用户确认。其他 lifecycle 操作不具有同样的删除确认流程；直接调用 Manager 的 trusted Python 代码也不经过 PermissionHook。

## 15. 遗忘与状态转换

```text
active --mark_stale(reason)--> stale
active --archive(reason?)---> archived
stale/archived --restore----> active
任意状态 --forget-----------> 删除文件
任意已有状态 --remember----> active（更新正文）
```

`_set_status()` 使用 `dataclasses.replace()` 更新 status、reason 和 updated_at，保存后清 selection cache。

stale/archived 不参与自动召回和默认 search，但仍可显式 get，或 search(include_inactive=True)。

这实现的是可审计的主动遗忘。没有定时 TTL、按 age/use_count 自动衰减、自动矛盾检测或周期性清理。`last_used_at` 目前是观察字段，并没有驱动时间衰减算法。

## 16. Workflow 的短期工作记忆

评测曾出现：某次 attempt 已完成文件修改和问题定位，retry 却重新调查，浪费工具轮次。长期 memory 不适合记录“刚写了哪几个文件”，所以系统增加任务工作 checkpoint。

`_run_phase()` 包装 `phase_on_event()`，持续捕获：

- 工具调用次数和最近 actions。
- 最近失败和 progress。
- Artifact snapshots。
- 文件修改后的 Worktree 状态。
- 验证工具执行结果和对应 diff。

相关变化写入 `current_attempt_state`，用于进程中断后恢复。

attempt 结束或失败时，`_build_attempt_handoff()` 汇总真实 Worktree、Task 和 verification，调用 `build_attempt_checkpoint()`。

`AttemptCheckpoint` 保存：

| 内容 | 来源 |
| --- | --- |
| objective / summary | 原始请求与角色总结 |
| completed / remaining / next_actions / decisions / blockers | 显式 semantic 字段、带标签总结和降级规则 |
| changed_files / diff hash | Worktree snapshot |
| open_tasks | TaskManager |
| verification | 捕获的验证事件 |
| recent_actions / failures / tool_counts | AgentEvent |
| artifact_ids | Artifact Gate/交付物 |

这一步采用确定性解析和长度/条数限制，不额外调用 LLM 做 checkpoint 摘要。角色总结的事实可能不完整，因此 Gate issue 和结构化证据仍然重要。

最近 4 个 attempt handoff 保留在 phase data，最新 checkpoint 也写到 `continuation_checkpoint`。retry/resume 新建角色 messages，注入 `<working_state>`、`<attempt_checkpoint>`；同角色进入修复阶段时还可收到 `<prior_role_checkpoint>`。

它恢复的是继续工作所需的有界状态，完整历史仍由 Trace/transcript 保存。Worktree 实际代码、Task 和 Artifact 才是工作成果来源。

## 17. 成功后的 `reflect_from_workflow()`

角色工作中主要读长期记忆，Workflow 成功后统一提炼，避免每个 worker 都写入未验证经验。

`_finish_result()` 只有同时满足以下条件才发起反思：

1. 最终 status=completed。
2. qa_verdict=pass。
3. 存在 MemoryManager。
4. 存在持久化 Workflow record。

输入包括原请求、verification evidence summary、交付 Artifact，以及已有 memory catalog。预算分别为 request 3000、evidence 6000、artifact context 10000 字符。

LLM 调用 tools=None、max_tokens=1000、temperature=0，Trace operation 为 memory_reflect。

结果过滤：

- 最多处理前 max_reflection_items 项，默认 3。
- 只接受 project/feedback/reference。
- confidence 必须合法且至少 0.7；省略时使用 0.8。
- memory_id 可复用已有事实。
- secret 和空字段经 remember 再检查。

每条记忆附 evidence：workflow id 和最多 20 个 changed-file 路径，source=workflow_reflection。

### 17.1 证据约束的实际强度

“只保存 evidence 支持的事实”主要依靠上层成功门控、Prompt 和输入证据。代码没有逐句证明 body 被 evidence 支持，也没有校准 confidence。

evidence 文件路径是来源线索，未保存 per-memory commit/hash；Worktree 已验收也不等于已 apply 到主工作区。因此反思记忆仍要通过后续新证据核验。

### 17.2 反思失败不撤销成功

Workflow 先持久化最终状态，再反思。LLM 调用异常在 Manager 内返回空，外部异常也由 Workflow 捕获，不撤销工程结果。

保存了反思结果时记录 `workflow.memory.reflected`，最终 Trace 包含 reflected_memory_ids。LLM 异常被内部降级为空，不一定产生专门的 reflection_failed 事件，但 LLM trace 可显示失败。

## 18. Main Agent、Workflow 和 Subagent 的差异

| 执行主体 | 召回方式 | 长期写入 |
| --- | --- | --- |
| Main Agent | 每步 BeforeLLM runtime context；支持 search/get | memory 工具 + 信号门控 Stop extraction |
| Workflow roles | 每步 BeforeLLM；默认工具 profile 只读 search/get | 禁用 Stop extraction，成功后 Harness 统一 reflection |
| Subagent | 委派开始按 task 召回，放进 delegated_memory section | 标记只读，通过父级统一管理 |

`main.py` 创建并共享根项目的 MemoryManager。Workflow 角色即使在独立 Worktree 开发，也读这一份共享 memory，而不是在各 Worktree 再生成长期库。

Subagent 的 memory section 属于 system prompt 装配，与 Main/Workflow 的 ephemeral user-role runtime injection 形式不同。正文说明只读、可能过时、以新证据为准；它不会像 MemoryContextHook 那样每步重新选择。

## 19. 一个完整使用示例

第一轮用户：

```text
记住：以后这个项目的测试都用 uv run pytest。
```

Main Agent 可直接调用 memory_remember。若它只回复确认，completed Stop Hook 也可根据“记住”信号抽取，保存 reference 或 user 条目。

下一会话用户：

```text
为 API 增加健康检查，并补充测试。
```

链路：

1. MemoryManager 读取 active 条目，selector 选择测试命令。
2. BeforeLLM 注入 relevant_memories；模型看到命令约定。
3. Workflow Engineer 在 Worktree 修改代码。
4. 工具事件、文件变化和测试结果进入 working checkpoint。
5. 达到 step 上限时，新 attempt 收到 checkpoint，继续补测。
6. QA 验证，PM 验收；通过后统一反思稳定约定。
7. 相同名称/ID 的事实可以更新，来源标记 workflow_reflection。

如果新工具证据表明项目已经改用另一条命令，模型可请求 mark_stale，再保存新的事实；系统不会自行扫描仓库并自动使旧命令失效。

## 20. 辅助函数与错误边界

| 函数 | 职责 |
| --- | --- |
| `_validate_memory_id/type/status/confidence()` | 限制 ID、枚举和 confidence 范围 |
| `_required_text()` / `_optional_text()` | 规范化必填/选填文本 |
| `_string_tuple()` | 把 evidence 转为去重字符串 tuple |
| `_normalize_name()` | 名称 upsert 的规范化 |
| `_parse_json_array()` | 支持 JSON、代码围栏或文本中的数组；失败返回空 |
| `_now()` | UTC 时间 |
| `_atomic_write()` | 单文件临时写入与替换 |
| `format_memory_context()` | LLM context wrapper |
| `format_memory()` | 完整对象的 JSON 格式 |
| `format_memory_list()` | 简短搜索列表 |

`MemorySystemError` 是统一业务异常，`MemoryNotFoundError` 是其子类。工具执行层会把异常转成模型可见错误；自动抽取/反思采用降级策略。

BeforeLLM 的召回 Hook 没有整体捕获存储错误，损坏文件或写 recall stats 失败可能中断 Agent。这与“selector LLM 失败可以 fallback”属于不同层的故障。

## 21. 可观测性与测试

Memory 辅助请求使用 trace_operation：memory_select、memory_extract、memory_reflect。它们同样经过 LLMClient 的传输恢复；在 Agent scope 内还会使用当前 RecoveryState。

Agent 输出 memory_context、memory_extracted、memory_extract_failed；Workflow 记录 working_context.injected、attempt.handoff、memory.reflected 等事件。

`test_memory_system.py` 的 11 项测试覆盖：

1. 文件持久化、同名更新、索引重建。
2. 旧格式 lifecycle 默认值。
3. 非法类型和 secret 拒绝。
4. pinned + LLM selection + selection cache。
5. selector 异常时关键词 fallback。
6. 信号门控的自动抽取。
7. memory 工具创建、搜索、读取和删除。
8. 同 run recall 去重及 stale/archive/restore。
9. Workflow reflection confidence 过滤和来源 evidence。
10. 召回只进入临时请求，不污染 canonical history。
11. Stop Hook 使用原 turn 抽取并发出事件。

Workflow 测试还验证角色只读记忆、四个角色阶段的召回统计、成功后反思，以及 checkpoint retry/resume。

这些测试证明调用行为和约束，不证明真实模型每次都能判断正确的长期事实。

## 22. 与 learn-claude-code s09 对比

| 维度 | s09 教学版 | 当前项目 |
| --- | --- | --- |
| 存储 | .memory 中按名称文件 | .llm_agent/memory，稳定 ID、YAML、安全解析 |
| 索引 | MEMORY.md 注入 system | 可读索引；运行时从独立文件生成目录 |
| 选择 | 最近 3 条用户文本，LLM 选数组序号 | 最近外部用户 query，LLM 选稳定 ID，cache、pinned 和预算 |
| 抽取 | 每 turn 看最近对话 | 明确信号门控，原始用户与最终回复 |
| 短期状态 | 主要会话与压缩 | 额外 Workflow attempt checkpoint 和持久化 resume |
| 整理/反思 | 文件数达到 10 时全库 consolidation | 成功且 QA pass 后提取/更新少量稳定经验 |
| 遗忘 | consolidation 可删旧文件再重建 | stale/archive/restore/forget，支持审计原因 |
| 工具 | 主要由 loop 隐式维护 | 7 个显式 memory 操作工具 |
| 来源 | 较少 metadata | source、run id、confidence、evidence、使用统计 |
| 安全 | 基础文件操作 | ID 校验、secret 检测、权限确认、角色只读 |

当前没有照搬教学版“删除全部旧文件，再按模型新数组重建”的 consolidation。增量更新和显式归档减少一次模型输出导致整库丢失的风险。

代价是没有全库语义去重、定期 Dream 或自动老化，规模增长后仍需要人工或专门策略整理。

## 23. 为什么这样设计

### 23.1 Store、Manager、Tool 分层

Store 只处理文件；Manager 统一事实生命周期和召回；Tool 处理模型 schema/context/输出。Hook、Workflow 和测试可以直接使用 Manager，避免业务规则散落在各入口。

### 23.2 为什么使用文件而不是向量数据库

当前长期记忆数量较少，内容是短小、可审查的项目约定。文件易备份、编辑和迁移，不需要服务依赖、embedding 更新或索引版本管理。

LLM catalog selection 提供一定语义选择能力，关键词提供离线降级。若目录规模大到持续截断，应再评估词法预筛、BM25 或向量索引；底层替换可保留 Manager API。

### 23.3 为什么长期库不存工作进度

任务进度随操作快速变化，也需要精确关联 Workflow、attempt 和 Worktree。长期召回会把它带到无关任务，产生过期完成状态。checkpoint 用结构化证据和明确作用域保留进度更合适。

### 23.4 为什么 worker 只读、成功后再反思

Engineer/QA 的过程判断可能被下一轮推翻。统一在完成并通过验证后提炼，降低多角色竞争写入和错误经验污染；但具体事实仍需要新证据核验。

### 23.5 为什么自动抽取用信号门控

每 turn 抽取会增加请求、延迟和噪声。用户明确偏好适合轻量门控，工程知识另由成功 Workflow evidence 触发反思，两个来源各有判断依据。

## 24. 当前限制与改进方向

- 没有 vector/BM25 索引；每次列表读取全部 memory 文件。
- 目录按固定顺序截断，规模大时可能遗漏后排候选。
- JSON 解析失败和明确空选择未区分，可能静默漏召回。
- 内部消息前缀在多个模块重复维护，覆盖范围不一致。
- 名称去重无法处理语义重复或精细冲突。
- confidence 是模型/调用者给值，没有概率标定。
- 没有自动 age decay、TTL、Dream 和矛盾检测。
- 单文件原子写不提供并发锁、多文件事务或版本历史。
- secret 检测不全面，当前只检查 description/body。
- 格式化 context 未对正文做完整的不可信内容编码；Prompt 约束不等于消除注入风险。
- selection cache 无容量上限；使用统计和正文更新可能并发覆盖。
- 持久化 working memory 主要用于 Workflow，普通 Main Agent 重启后不能自动恢复任意未完成 turn。

下一步改进应先根据真实 Trace 观察漏召回、重复事实和恢复失败，再修正前缀识别、解析降级和存储容错；记忆规模确实增长后再增加检索基础设施。

## 25. 推荐源码阅读顺序

1. memory_system.py 的 Memory、MemoryStore。
2. MemoryManager.remember、_find_existing、_set_status。
3. search、_score_memories、_search_terms。
4. retrieve_relevant、_select_ids、_select_ids_with_llm。
5. _fit_budget、_record_recall。
6. MemoryContextHook 与 ContextManager.build_request_messages。
7. MemoryExtractionHook 与 extract_from_turn。
8. MemoryTools 和默认注册装配。
9. Workflow _capture_attempt_event、_build_attempt_handoff。
10. build_attempt_checkpoint 与 retry/resume 的 prompt 注入。
11. Workflow _finish_result 与 reflect_from_workflow。
12. SubagentRunner 的 delegated_memory section。
13. test_memory_system.py 与 Workflow memory/checkpoint 测试。

## 26. 面试问题与回答要点

### 为什么压缩上下文之后还需要 Memory？

压缩解决当前窗口容量，Memory 保存跨会话稳定事实。前者无法替代事实生命周期和召回；后者也不能替代本次工作因果链。

### 如何避免 resume 后从头调查？

保存任务级 checkpoint：changed files、验证、失败、open tasks 和下一步。新 attempt 在原 Worktree 上继续，并接收 bounded checkpoint，而不是只接收原始任务。

### 如何召回？是否用了 RAG？

当前 active 过滤、pinned 优先、LLM metadata catalog selection，异常时关键词 fallback，再做条数和字符预算。它是检索增强的记忆注入，没有使用 embedding 或向量数据库。

### 如何避免错误记忆越来越多？

抽取门控、worker 只读、QA pass 后统一反思、confidence 阈值、stale/archived 排除和新证据优先。当前没有自动逐事实核验，需要承认模型判断和名称去重的限制。

### 遗忘是自动的吗？

目前通过显式 lifecycle 操作实现，stale/archive 可恢复，forget 永久删除并在工具入口请求确认。使用统计只提供观察数据，没有驱动自动衰减。

### 记忆会污染 system prompt 或覆盖用户吗？

Main/Workflow 召回作为临时 user-role context 插入，请求中有 policy 约束优先级；canonical history 不重复保存。Subagent 的委派记忆采用只读 system section，需要明确这一装配差异和注入风险边界。

### 反思是否等于模型对自身做总结？

输入包含真实 verification 和交付物，触发也受 completed/QA pass 门控。输出只保留稳定项目知识；工作进度仍进入 checkpoint。它仍依赖模型提炼，不能宣称逐条事实已形式化证明。

### 文件存储支持并发多 Agent 吗？

当前串行 Workflow 的 worker 只读，主要写入由父级控制，降低竞争。Store 只有单文件原子替换，没有多写者锁和事务；并行团队拓展时应补并发一致性机制。

### 如何评估记忆是否有效？

对比启用/关闭记忆的相同任务：测相关条目召回、错误事实、用户重复说明次数、resume 重复工具调用、完成率及额外 token/延迟。use_count 是使用行为，不能单独证明效果。
