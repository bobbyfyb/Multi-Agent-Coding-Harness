# Tool System 源码精读

> 核心源码：[`src/llm_agent/tool_registry.py`](../../src/llm_agent/tool_registry.py)、[`src/llm_agent/tools/`](../../src/llm_agent/tools)、[`src/llm_agent/command_runner.py`](../../src/llm_agent/command_runner.py)  
> 核心测试：[`tests/test_tool_registry.py`](../../tests/test_tool_registry.py)、[`tests/test_basic_tools.py`](../../tests/test_basic_tools.py)、[`tests/test_verification_tools.py`](../../tests/test_verification_tools.py)

## 1. 模块目标

Tool System 将模型生成的结构化 Tool Call 转换为 Harness 内部的真实能力调用。

它解决四个问题：

1. 如何统一声明工具名称、描述、JSON Schema 和处理函数。
2. 如何只把 schema 暴露给模型，不暴露 Python callable 和运行上下文。
3. 如何按 Main Agent、Subagent、PM、Engineer、QA 裁剪能力集合。
4. 如何把成功值、参数错误、运行异常和业务失败统一反馈给 Agent Loop。

完整链路：

```text
ToolDefinition
  name + description + parameters + callable + requires_context
                      |
                      v
                 ToolRegistry
                  |        |
       tool_specs()         | call(name, arguments, context)
                  |        |
                  v        v
             LLMClient    Python Tool Facade
                  |        |
                  v        v
             model call   Manager / Runner / Command
                      |     |
                      +--+--+
                         v
                     Agent Loop
                   tool result 回灌
```

Tool System 不承担所有安全职责：

- `PreToolUse` Hook 决定是否需要用户确认或拒绝。
- `security.py` 提供路径、命令和子进程环境的硬约束。
- 各 Manager 负责业务状态机和持久化一致性。
- Agent 负责工具调用顺序、事件和 Provider 结果回灌。

## 2. 文件地图

### 2.1 核心基础设施

| 文件 | 职责 |
| --- | --- |
| [`tool_registry.py`](../../src/llm_agent/tool_registry.py) | 工具数据模型、注册、裁剪、调用和 ToolSpec 导出 |
| [`tools/__init__.py`](../../src/llm_agent/tools/__init__.py) | 组合内置、可选和 MCP 工具，构建默认 Registry |
| [`command_runner.py`](../../src/llm_agent/command_runner.py) | 运行同步子进程，持久化 stdout/stderr，处理超时和进程组 |

### 2.2 内置工具文件

| 文件 | 对模型暴露的工具 |
| --- | --- |
| [`basic_tools.py`](../../src/llm_agent/tools/basic_tools.py) | `bash`、`read_file`、`write_file`、`edit_file`、`search_text`、`glob` |
| [`verification_tools.py`](../../src/llm_agent/tools/verification_tools.py) | `run_tests`、`run_lint` |
| [`search_tools.py`](../../src/llm_agent/tools/search_tools.py) | `search` |
| [`task_tools.py`](../../src/llm_agent/tools/task_tools.py) | 6 个 Task 生命周期工具 |
| [`artifact_tools.py`](../../src/llm_agent/tools/artifact_tools.py) | 4 个 Artifact CRUD/查询工具 |
| [`memory_tools.py`](../../src/llm_agent/tools/memory_tools.py) | 7 个 Memory 生命周期工具 |
| [`skill_tools.py`](../../src/llm_agent/tools/skill_tools.py) | Skill 发现、加载和资源读取 |
| [`background_tools.py`](../../src/llm_agent/tools/background_tools.py) | Background Job 查询、输出、等待和取消 |
| [`subagent_tools.py`](../../src/llm_agent/tools/subagent_tools.py) | `subagent_run` |
| [`worktree_tools.py`](../../src/llm_agent/tools/worktree_tools.py) | Worktree 列表、diff、apply、remove |
| [`mcp_tools.py`](../../src/llm_agent/tools/mcp_tools.py) | 将外部 MCP 动态工具转换为本地 ToolDefinition |

全部可选能力都启用时，内置工具共有 39 个，另外再加 MCP 动态发现的工具。

### 2.3 源码符号模式

每个领域文件基本遵循相同结构：

```text
<Domain>Tools dataclass
  -> 对 Manager/Runner 的模型友好 Facade

<domain>_tool_definitions(...)
  -> 构造 ToolDefinition + JSON Schema

register_tools(registry, ...)
  -> registry.register_many(...)
```

Core Registry 和两个复杂 Coding Tool 文件还包含以下辅助符号：

| 文件 | 主要辅助符号 |
| --- | --- |
| `tool_registry.py` | `ToolFunction`、`Tool`、`ToolDefinition`、`ToolSpec`、`ToolRegistry` |
| `command_runner.py` | `command_artifact_dir`、`run_command`、`_read_preview`、`_terminate_process`、`_safe_component` |
| `basic_tools.py` | `_iter_search_files`、`_matches_search_globs`、`_file_sha256`、`_atomic_write_text`、`_diff_preview` |
| `verification_tools.py` | 环境错误 marker、`_module_command`、`_classify_process_error`、`_parse_junit`、`_parse_ruff` 等 |

## 3. Core Registry 数据结构

### 3.1 `ToolFunction`

```python
ToolFunction = Callable[..., Any]
```

Registry 不强制具体函数签名。参数契约由 JSON Schema 向模型描述，运行时通过 `func(**arguments)` 调用。

这种设计简单，但意味着 Registry 本身不会做 schema validation；Python 参数错误最终作为工具异常处理。

### 3.2 `ToolDefinition`

```python
@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]
    func: ToolFunction
    requires_context: bool = False
```

它是工具模块向 Registry 提交的声明，绑定了两部分：

- 给模型看的协议：name、description、parameters。
- Harness 内部实现：func、requires_context。

Schema 与 callable 放在同一声明中，可以避免静态 JSON 文件和实现函数逐渐失配。

### 3.3 `Tool`

`Tool` 字段与 `ToolDefinition` 相同，但语义不同：

- `ToolDefinition` 是尚未注册的模块声明。
- `Tool` 是 Registry 内部已经接管的运行记录。

当前二者结构完全一致，理论上可以合并；保留两层让注册 API 和内部状态边界更明确，也允许未来给二者增加不同元数据。

### 3.4 `ToolSpec`

```python
class ToolSpec(TypedDict):
    name: str
    description: str
    parameters: dict[str, Any]
```

它是唯一发给 `LLMClient` 的工具结构。`func` 和 `requires_context` 被剥离，模型无法看到 Python 对象或伪造 Harness execution context。

## 4. `ToolRegistry`

### 4.1 内部存储

```python
self._tools: dict[str, Tool] = {}
```

Python dict 保持插入顺序，所以 `names()`、`tool_specs()` 和 `subset()` 都保持原注册顺序。

### 4.2 `register()`

直接接收字段并构造内部 `Tool`。同名工具立即抛出：

```text
ValueError: Tool already registered: <name>
```

冲突在依赖装配阶段暴露，不允许后注册工具静默覆盖内置工具。这对 MCP public name 与内置工具共存尤其重要。

当前没有在此验证 name 是否符合 Provider 约束、parameters 是否是合法 JSON Schema、callable 签名是否与 schema 一致；这些主要由模块测试和 Provider 契约发现。

### 4.3 `register_definition()` 与 `register_many()`

前者把 dataclass 字段转发给 `register()`；后者顺序注册一组定义。每个领域文件都采用：

```python
def register_tools(registry, ...):
    registry.register_many(domain_tool_definitions(...))
```

新增领域工具只需维护自己的 Facade、Definition Factory 和 Register 函数。

### 4.4 `call()`

执行状态机：

```text
按 name 查找
  | missing -> {ok: false, error: Unknown tool}
  v
requires_context?
  | yes but missing -> {ok: false, error: Tool requires context}
  | yes -> func(context=context, **arguments)
  | no  -> func(**arguments)
  v
返回 {ok: true, result: value}
```

工具函数抛出的所有 `Exception` 被包装为：

```json
{"ok": false, "error": "异常文本"}
```

因此大多数错误成为模型可观察的 Observation，而不是终止 Agent Run。Registry 不捕获 `BaseException`，所以 `KeyboardInterrupt`、`SystemExit` 等控制流异常不会被误吞。

### 4.5 双层成功语义

Registry 的 `ok` 表示“工具调用基础设施是否成功完成”，不一定表示业务目标成功。

pytest 确实执行但存在失败测试：

```json
{
  "ok": true,
  "result": {"outcome": "failed", "exit_code": 1}
}
```

参数非法或进程无法启动则可能是：

```json
{"ok": false, "error": "..."}
```

这让 Evidence Gate 能区分“测试有效运行并失败”和“Harness 没有成功运行测试”。

### 4.6 `subset()`

`subset(names)` 创建新 Registry，仅复制白名单中的 Tool：

- callable 仍指向同一 Facade/Manager。
- `requires_context` 原样保留。
- 白名单中不存在的名称被静默忽略。
- 不会复制出新的 Manager 状态。

Subagent mode 和 Workflow role 都用它做最小权限能力裁剪。

### 4.7 `tool_specs()`

为每个 Tool 生成 Provider-neutral `ToolSpec`。LLMClient 再把 parameters 转为 OpenAI `function.parameters` 或 Anthropic `input_schema`。

## 5. 为什么没有静态 tools.json

项目早期讨论过启动时从静态 JSON 注册工具。当前选择把 Facade implementation、JSON Schema、description 和 register function 放在同一个领域文件。

理由：

1. Schema 与 Python 默认值/签名更容易一起修改。
2. Manager、Runner 和 workdir 需要运行时依赖注入，静态 JSON 无法表达 bound callable。
3. IDE 重构和类型检查可以定位引用。
4. 每个领域模块可以独立测试 definition 和实现。
5. 不需要维护“JSON name -> Python import path”的动态加载协议。

静态配置适合外部插件 manifest；项目内工具更适合 source-adjacent definition。MCP 则承担真正的运行时外部发现。

## 6. 默认工具装配

### 6.1 `register_default_tools()`

注册顺序：

```text
1. Basic                         always
2. Background controls          if manager exists
3. Worktree controls            if manager exists
4. Web search                   always
5. Verification                 always
6. Task                         always
7. Artifact                     always
8. Skill                        if registry exists
9. Memory                       if manager exists
10. Subagent                    if runner exists
11. MCP                         if manager exists
```

ArtifactManager 未显式注入时会按 workdir 创建默认实例；Task 同样按 workdir/task_list_id 创建。

### 6.2 `build_default_registry()`

便利函数创建空 Registry，调用 `register_default_tools()` 后返回。`main.build_agent()` 使用它完成依赖装配。

### 6.3 为什么部分注册函数延迟导入

Subagent、Background、Worktree、MCP 的 register wrapper 在函数内部 import，类型只放在 `TYPE_CHECKING`：

- 避免 Agent、Subagent、Tools 之间循环导入。
- 未配置可选能力时不加载对应实现。
- `tools` 包仍提供统一公共入口。

Basic、Search、Verification、Task、Artifact 等核心工具直接导入。

## 7. Agent 执行工具的完整链路

```text
1. Agent 启动时：tool_specs = registry.tool_specs()
2. LLMClient 把 schema 发给模型
3. 模型返回 LLMToolCall(name, arguments, id)
4. Agent 发 tool_call event
5. Agent 检查 incompatible argument marker
6. HookManager.trigger_hooks("PreToolUse", ...)
7. denied -> 拒绝结果直接回灌
8. allowed -> 创建 ToolExecutionContext
9. Agent._execute_tool_call()
10. registry.call(name, arguments, context)
11. Tool Facade 调用 Manager/Runner/Command
12. HookManager.trigger_hooks("PostToolUse", ...)
13. 可选替换结果
14. Agent 发 tool_result event
15. LLMClient 按 Provider 协议批量回灌
```

权限位于 Registry 之外。这允许同一工具在 Main/Workflow/Subagent 中使用不同策略，但意味着直接调用 `registry.call()` 会绕过 Hook。Basic Tool 自身还有路径和命令硬约束，Memory Forget、Worktree Apply 等业务破坏性操作则依赖标准 Agent Hook 入口。

## 8. `ToolExecutionContext`

需要上下文的工具由 Agent 注入：

```text
run_id, agent_id, parent_run_id, depth,
step, workdir, on_event, metadata
```

典型用途：

- Background Bash 记录 owner run/agent。
- Command Runner 用 tool_call_id 组织输出文件。
- Verification Tool 生成独立报告。
- Memory Remember 记录 source run id。
- Subagent 建立父子 run 和事件传播。
- MCP 使用当前 execution workdir。

模型 JSON Schema 不包含这些字段，不能伪造运行身份或越过当前 worktree。

## 9. BasicTools

### 9.1 配置

```python
@dataclass(frozen=True)
class BasicTools:
    workdir: Path
    bash_timeout: float = 120.0
    output_limit: int = 50_000
    max_read_lines: int = 2_000
    background_manager: BackgroundJobManager | None = None
```

`__post_init__()` 用 `object.__setattr__()` 将 workdir 解析成绝对路径。`safe_path()` 调用 `resolve_workspace_path()`，阻止路径逃逸和敏感目录访问。

### 9.2 `bash`

同步模式：

- 先用 `validate_shell_command()` 检查命令。
- 调用 `run_command(shell=True)`。
- cwd 固定为 Agent workdir。
- stdout/stderr 完整落盘，结果只返回有界预览。

后台模式：

- 必须配置 BackgroundJobManager。
- `start_shell()` 后立即返回 job id、pid、状态和日志路径。
- owner run/agent 来自 ToolExecutionContext。
- 后续用 background tools 或 Agent notification 获取结果。

当 Background Manager 存在时，bash definition 被标为 `requires_context=True`；否则 context 不注入。当前 Workflow Role 没有 Background Manager，因此同步 Bash 的 command artifact 使用 `manual` run id，而 Verification Tool 始终带 context。这是现有的关联粒度差异。

### 9.3 `read_file`

- 路径必须位于 workspace 且不是敏感文件。
- 以 bytes 读取并计算整个文件 SHA256。
- UTF-8 解码错误替换。
- 支持 `start_line` 和 `limit`，内部最多 2000 行。
- 返回实际行范围、总行数和 `truncated`。

SHA256 是后续 optimistic concurrency control 的版本标记。

### 9.4 `write_file`

- 可创建父目录。
- 若传 `expected_sha256`，当前文件 hash 必须一致。
- 新文件的 `before_sha256=None`。
- 使用同目录临时文件 + atomic replace。
- 已存在文件的 mode 会复制到临时文件。
- 返回写入字节数和前后 hash。

`expected_sha256` 在 schema 中可选，但默认 system prompt 要求编辑现有文件时使用 read_file 返回的 hash。

### 9.5 `edit_file`

- `old_text` 不能为空，文件必须存在。
- 可进行 SHA256 stale-write 检查。
- `old_text` 必须恰好出现一次。
- old/new 相同导致无变化时拒绝。
- 原子写回并返回最多 12,000 字符的 unified diff。

“恰好一次”比教学版的第一次 replace 更可靠，可以阻止模型对模糊片段误改位置。

### 9.6 `search_text`

默认使用：

```text
rg --json --line-number --column --color never --fixed-strings
```

特性：

- `regex=true` 时移除 fixed-string 模式。
- 支持 include/exclude globs，并追加敏感路径 exclude。
- 返回 path/line/column/text 的结构化 match。
- `max_results` 限制 1 到 200，超时 30 秒。
- rg exit code 1 表示无匹配，不是异常。

若没有 ripgrep，回退到 Python 遍历：

- 跳过隐藏目录、隐藏文件和敏感路径。
- 支持正则和基本 glob。
- 遇到 NUL 内容按二进制停止读取。
- 同样受 30 秒和结果数限制。

### 9.7 `glob`

使用 `glob(..., recursive=True)`，逐个 resolve 后确认仍在 workdir 内，并过滤敏感路径。结果排序并用换行返回；无匹配返回 `(no matches)`。

### 9.8 Basic helper 函数

| 函数 | 作用 |
| --- | --- |
| `_iter_search_files()` | 安全遍历 fallback search 文件 |
| `_matches_search_globs()` | 实现 include/`!exclude` 匹配 |
| `_file_sha256()` | 计算完整文件 hash |
| `_atomic_write_text()` | 保留 mode 的原子文本写入 |
| `_diff_preview()` | 生成有界 unified diff |

## 10. Command Runner

### 10.1 `command_artifact_dir()`

按以下结构为命令创建证据目录：

```text
.llm_agent/tool-results/<run-id>/<tool-call-id>/
```

没有 context 时 run id 为 `manual`；没有 call id 时使用 tool name + 随机值。路径组件会清洗并限长。

### 10.2 `run_command()`

支持字符串 + `shell=True`（Bash），或 argv sequence + `shell=False`（pytest/ruff）。

前置校验：

- timeout 和 preview 必须为正。
- shell 模式必须接收字符串。
- shell 命令再次执行安全校验，形成 defense in depth。

子进程设置：

- cwd 固定。
- stdin=`DEVNULL`，防止等待交互输入。
- env 来自 `safe_subprocess_env()`，不继承 API keys 等敏感变量。
- stdout/stderr 直接写文件，避免大输出占满内存。
- POSIX 下 `start_new_session=True`。

### 10.3 超时终止

`_terminate_process()` 先向整个进程组发送 SIGTERM，等待 grace period，仍未结束则 SIGKILL；非 POSIX 使用 terminate/kill。终止进程组可以清理测试命令产生的子进程，而不只杀父 shell。

### 10.4 结果结构

```text
status: completed / failed / timed_out
command, exit_code
stdout / stderr tail preview
stdout_path / stderr_path
duration_ms, timed_out, truncated
```

`_read_preview()` 保留文件尾部；失败栈和测试总结通常在输出末尾。若截断，会标注省略的早期字节数。

进程成功启动但 exit code 非 0 仍返回结构；只有启动失败、非法参数等抛 RuntimeError，随后由 Registry 包成 `ok=false`。

## 11. VerificationTools

### 11.1 为什么不只用 Bash 跑测试

普通 Bash 很难稳定区分断言失败、没有测试、缺少依赖、uv.lock 过期、环境创建失败和超时。`run_tests`、`run_lint` 将它们转成结构化 Evidence。

### 11.2 执行环境选择

`_module_command()`：

```text
pyproject.toml + uv.lock exists
  -> uv run --locked --no-env-file python -m <module>
otherwise
  -> <harness sys.executable> -m <module>
```

uv 模式从环境移除父 `VIRTUAL_ENV`，避免 Harness venv 污染目标项目。`--locked` 不允许工具自动改 lockfile；依赖配置问题会成为环境错误。

### 11.3 `run_tests`

流程：

1. 验证 target 是工作区内路径或 pytest node id，且不能以 `-` 开头。
2. 创建独立 artifact dir 和 `junit.xml`。
3. 执行 `pytest -q ... --junitxml=...`。
4. 解析统计和最多 100 条 failure。
5. 分类环境错误。
6. 映射 outcome。

| 条件 | outcome |
| --- | --- |
| timeout | `timed_out` |
| env setup / stale lock | `error` |
| exit 0 | `passed` |
| exit 1 | `failed` |
| exit 5 | `no_tests` |
| 其他 | `error` |

缺失模块通过正则提取为 `error_kind=missing_dependency` 和 `missing_modules`。

### 11.4 `run_lint`

执行 `ruff check --output-format json`。Exit 0 映射 `clean`，exit 1 映射 `issues_found`，其他为 error/timed_out。

JSON 被解析成最多 200 条 path/line/column/code/message/fixable。返回对象清空 `stdout`，完整 Ruff JSON 留在 `stdout_path`，避免重复进入模型上下文。

### 11.5 Verification helper

| 符号 | 作用 |
| --- | --- |
| `_MISSING_MODULE_RE` | 提取 Python 缺失模块 |
| lock/environment marker 常量 | 识别 uv 配置和依赖准备失败 |
| `_module_command()` | 选择目标项目或 Harness 环境 |
| `_unset_env()` | uv 项目移除 `VIRTUAL_ENV` |
| `_classify_process_error()` | 标准化错误分类 |
| `_lock_is_outdated()` | 判断 lockfile stale |
| `_diagnostic_excerpt()` | 从长输出选关键尾部诊断 |
| `_validate_timeout()` | 限制 1 到 600 秒 |
| `_validate_targets()` | 阻止 option injection、路径逃逸和敏感路径 |
| `_parse_junit()` | 解析统计与失败明细 |
| `_parse_ruff()` | 解析 lint diagnostics |

## 12. Web Search Tool

`SearchTools.serpapi_search()` 使用 `SEARCH_API_KEY` 和 SerpAPI Client，默认：

```text
engine=google
top_k=5
gl=cn
hl=zh-cn
```

结果优先级：answer box list、answer box、knowledge graph、organic results、无结果提示。

当前实现捕获所有异常并返回 `"Error in Searching: ..."` 字符串，因此 Registry 外层仍是 `ok=true`。这与其他工具错误语义不一致。

其他限制：

- SerpAPI 在模块 import 时直接依赖，未延迟导入。
- engine 和 top_k schema 缺少 enum/range。
- 地区与语言硬编码为中国/简体中文。
- 没有专门单元测试覆盖 response variants。

Web Search 是当前内置工具中最值得后续收紧的实现。

## 13. Task Tools

`TaskTools` 是 `TaskManager` 的模型接口层，不实现 Task 状态机，只转发参数、格式化结果和声明 schema。

| 工具 | 作用 |
| --- | --- |
| `task_create` | 创建 session/project task，支持 owner、依赖、父任务、优先级 |
| `task_get` | 获取完整任务 |
| `task_list` | 按 scope/status/owner 筛选 |
| `task_update` | 更新字段、状态、证据和 metadata |
| `task_claim` | 校验依赖后认领并进入 in_progress |
| `task_complete` | 要求 evidence，完成并展示新解除阻塞的任务 |

`TaskTools.for_workdir()` 将 task list id 固定在绑定 Manager 中。Workflow 通过 `task_workdir` 和 `task_list_id=workflow_id` 让角色操作主工作区中的同一任务列表，即使代码执行发生在 worktree。

## 14. Artifact Tools

| 工具 | 作用 |
| --- | --- |
| `artifact_create` | 创建 PRD、TaskSpec、ImplementationReport、TestReport 等持久 Artifact |
| `artifact_get` | 读取完整内容和 version |
| `artifact_list` | 按 kind/status/task/owner 筛选 |
| `artifact_update` | 更新内容、状态和 metadata，支持 optimistic version check |

Create/Update 返回摘要而不回显完整 body，提示需要时用 get 读取，避免大 Artifact 重复进入上下文。

Schema 的 kind/status enum 直接来自 Manager 常量。`expected_version` 与文件工具 SHA256 类似：先读版本再更新，避免覆盖其他角色的新版本。

## 15. Memory Tools

| 工具 | 作用 |
| --- | --- |
| `memory_remember` | 创建/更新 user、feedback、project、reference memory |
| `memory_search` | 按主题检索，可包含 inactive |
| `memory_get` | 读取单条 Memory |
| `memory_mark_stale` | 保留审计但退出自动召回 |
| `memory_archive` | 可逆归档 |
| `memory_restore` | 恢复 active |
| `memory_forget` | 永久删除 |

`memory_remember` 需要 ToolExecutionContext，将 run id 写入 `source_run_id`，并设置 `source="tool"`。永久删除由 PermissionHook 要求用户确认。

Description 明确禁止保存任务进度、日志、猜测和 secret，避免把 Working Memory 污染为长期记忆。

## 16. Skill Tools

| 工具 | 返回内容 |
| --- | --- |
| `skill_list` | Skill metadata 列表 |
| `skill_load` | 一个 Skill 的完整 manifest/instructions |
| `skill_read_resource` | Skill 根目录下的引用、模板或其他 UTF-8 资源 |

Catalog 只向 system prompt 提供摘要；确认相关后再调用 `skill_load`，避免所有 Skill 内容永久占用上下文。

路径逃逸和文件大小由 SkillRegistry 负责。Description 声明 Skill 不能覆盖 system/user/workspace/permission policy。

## 17. Background Tools

模型通过 `bash(run_in_background=true)` 创建任务，再使用：

| 工具 | 作用 |
| --- | --- |
| `background_list` | 列出近期任务，可按状态筛选 |
| `background_get` | 获取任务元数据 |
| `background_output` | 读取 stdout/stderr/combined 尾部 |
| `background_wait` | 最多等待 30 秒后返回状态 |
| `background_cancel` | 取消运行任务 |

Facade 返回 JSON 字符串保持 metadata 可读。`background_wait` 在方法层再次 `min(timeout, 30)`，即使绕过 schema 也不会长时间阻塞 Agent。

真正的并发限制、持久化、stale-running 恢复和通知队列属于 BackgroundJobManager。

## 18. Subagent Tool

`subagent_run` 参数：task、expected_output、mode、isolation、task_id。

Facade 构造 `SubagentRequest`，把当前 ToolExecutionContext 作为 parent context 传给 Runner，返回结构化 SubagentResult。

它必须 `requires_context=True`，因为父子 run id、depth、事件回调和 worktree 关联都来自 Context。

Schema 明确：worker 使用 fresh context、explore 只读、general 才可能编辑、worker 不能继续委派、返回报告需要父 Agent 复核。

## 19. Worktree Tools

| 工具 | 作用 |
| --- | --- |
| `worktree_list` | 列出 Harness 管理的隔离 worktree |
| `worktree_diff` | 查看 changed files 和有界 patch |
| `worktree_apply` | 将已审查 patch 应用到主工作区 |
| `worktree_remove` | 删除 worktree，可显式丢弃未应用变化 |

Facade 很薄，stale main HEAD、冲突、未应用变更保护由 WorktreeManager 实现。

PermissionHook 会确认 `worktree_apply` 和 `worktree_remove(discard_changes=true)`；普通 remove 默认不允许静默丢失改动。

## 20. MCP 动态工具

### 20.1 Binding 到 Definition

`MCPManager.tool_bindings(scope)` 返回动态 binding。`_to_definition()` 构造闭包：

```python
def call(*, context, **arguments):
    return manager.call(
        binding.public_name,
        arguments,
        workdir=context.workdir,
    )
```

Definition 使用 public name、server description/schema，并设置 `requires_context=True`，确保 workspace-scoped MCP 使用当前角色目录。

### 20.2 权限

MCPPermissionHook 根据 binding policy 执行 allow、deny 或用户 confirm。权限元数据留在 MCP config/Hook，不写进 Registry 核心。

### 20.3 为什么不把本地工具改成 MCP

本地工具同进程调用具有更少序列化和进程管理开销，能直接共享安全 Hook、workdir 和 run id。MCP 更适合外部能力和生态接入；转换成 ToolDefinition 后，Agent Loop 对来源无感。

## 21. 角色和模式能力裁剪

### 21.1 Subagent Profiles

Explore：

```text
read_file, glob, search_text, search,
skill_list, skill_load, skill_read_resource
```

General 额外拥有 bash、write_file、edit_file、run_tests、run_lint。Explore 没有 Bash，即使只想执行 `ls` 也不允许，防止通过 shell 绕过只读边界。

### 21.2 Workflow Roles

| 角色 | 关键能力 |
| --- | --- |
| PM | Artifact/Task、只读文件和搜索、Memory read、Skill |
| Engineer | PM 能力 + Bash/Write/Edit/Test/Lint |
| QA | Artifact/Task、只读文件和搜索、Test/Lint，无 Write/Edit/Bash |
| PM Acceptance | 与 PM 相同 |

MCP 工具按 role scope 动态加入白名单。Worktree 生命周期由 Orchestrator 管理，不直接交给 Workflow Worker。

Capability subset 比只在 Prompt 中写“QA 不要修改文件”更可靠：模型根本看不到相应 schema。

## 22. JSON Schema 的作用与边界

Schema 用于告知模型参数及 required、enum、minimum、maximum，并进入 OpenAI/Anthropic Tool Calling 协议。

ToolRegistry 当前不运行 JSON Schema validator。compatible server 产生越界参数时：

- Python 默认值和方法检查处理一部分。
- Manager 处理业务 enum/状态机。
- TypeError/ValueError 被 Registry 包装。
- Agent 把错误回灌模型修正。

因此 schema 是模型协议，不是唯一安全边界。路径、命令、验证 target 必须在执行函数内再次校验。

## 23. 安全分层

```text
Layer 1: Capability filtering
  不向角色暴露不需要的工具

Layer 2: JSON Schema
  引导模型生成合法参数

Layer 3: PreToolUse Hook
  路径预检、权限策略、用户确认、MCP policy

Layer 4: Tool internal validation
  workspace path、shell command、target validation

Layer 5: Process isolation
  sanitized env、fixed cwd、no stdin、timeout、process group kill

Layer 6: Manager invariants
  Task dependency、Artifact version、Memory lifecycle、Worktree stale check
```

Hook 与 Tool 内部校验并不重复：Hook 提供可配置控制面和用户交互；执行函数内部约束保证调用路径变化后仍保有安全基线。

## 24. 与 learn-claude-code s02 的差异

教学版使用静态 `TOOLS`、全局 `TOOL_HANDLERS` 和 `handler(**block.input)`。

| 维度 | 教学版 | 当前项目 |
| --- | --- | --- |
| 工具定义 | 全局静态 list | 领域 ToolDefinition Factory |
| handler | 全局 name->function map | Registry 内 Tool record |
| Provider | Anthropic input_schema | Provider-neutral parameters |
| 依赖 | 全局 WORKDIR | 注入 Manager/workdir/Runner |
| 结果 | 任意字符串 | 统一 `{ok,result}` / `{ok,error}` |
| Context | 无 | `requires_context` 注入运行身份 |
| 工具集合 | 所有 Agent 相同 | subset 按角色/模式最小权限 |
| 文件编辑 | 第一次文本替换 | hash、防歧义、原子写、diff |
| 命令 | capture_output | 环境隔离、落盘、timeout、进程组终止 |
| 验证 | Bash 文本 | pytest/JUnit、Ruff JSON 结构证据 |
| 扩展 | 修改同一文件 | 领域 register function 或 MCP discovery |

教学版展示 Tool Calling 的最短路径；当前结构服务真实 coding task 的可靠性、安全、测试和多 Agent 能力边界。

## 25. 测试覆盖

### 25.1 Registry

`test_tool_registry.py` 覆盖正常调用、未知工具、Definition 注册、context 注入和 subset。尚未直接覆盖重复注册、context 缺失、handler 异常包装和 schema/callable 不一致。

### 25.2 Basic Tools

覆盖：

- write/read/edit/glob 文件流程。
- workspace path escape 和敏感路径。
- Bash cwd、非零退出码、危险命令。
- Python/Conda 共享环境 mutation 阻止。
- shell `cd` 边界和父 secret env 隔离。
- 模糊 edit、stale SHA、行范围读取。
- rg 结构化结果和 Python fallback。
- 默认 Registry 核心集合。

### 25.3 Verification

覆盖 pytest/JUnit、missing dependency、uv lock/environment error、locked command、`VIRTUAL_ENV` 清理、Ruff diagnostics、timeout 和敏感 target。

### 25.4 领域与外部工具

Task、Artifact、Memory、Skill、Background、Worktree、MCP 的 Manager 测试同时覆盖 Tool Facade 主要调用；Subagent 测试覆盖委派和模式隔离。

选定工具相关测试文件合计约 89 个测试入口，但 Web Search 缺少专门测试。

## 26. 设计思路与取舍

### 26.1 为什么 Registry 只做薄分发

Registry 只维护 name->callable、context injection、异常包装、schema export 和 capability subset。权限、业务状态、持久化和重试放在更合适的层，避免形成中心化大类。

### 26.2 为什么需要 Facade + Manager 两层

Manager 负责依赖、状态转换、持久化和并发语义；Tools Facade 负责模型友好参数、文本格式与 JSON Schema。Manager 还能被 Workflow 或测试直接调用，不被 Tool Calling 协议绑死。

### 26.3 为什么统一外层包装

Agent 不必为每个工具写 try/except，模型可以先按 `ok` 判断调用基础设施，再读取领域 outcome。代价是双层语义必须被 Prompt、CLI 和 Evidence Gate 正确解释。

### 26.4 为什么本地工具同步执行

文件编辑和命令常有顺序依赖；同步调用使因果和 Trace 可预测。真正长时间、可独立的命令通过显式 background Bash 运行。

### 26.5 为什么验证工具固定 pytest/Ruff

这是 Python 简历项目最小且高价值的 Evidence MVP。未来可按项目探测增加 npm/cargo/go adapter，无需改变 Registry。

## 27. 真实问题与修复经验

### 27.1 错误 Python 环境

早期 `python -m pytest` 使用 Harness venv，造成目标项目 dependency error 误判。修复为 uv 项目使用 locked 环境，并清除父 `VIRTUAL_ENV`。

### 27.2 测试失败与环境失败混合

只看 exit code 无法判断 assertion、missing module、stale lock 或 download failure。VerificationTools 增加 JUnit/Ruff parser 和 process error classification，Evidence Gate 只接受结构化结果。

### 27.3 Read/Edit 之间文件变化

增加 SHA256 optimistic check。模型读文件取得版本，write/edit 时传回；不一致即拒绝，避免长 Run 覆盖用户或其他角色改动。

### 27.4 工具结果过大

Command Runner 将完整 stdout/stderr 写入 `.llm_agent`，只返回尾部预览；ContextManager 后续还能落盘或微压缩其他大结果。

## 28. 当前限制与风险

1. ToolRegistry 不执行 JSON Schema validation。
2. `Tool` 与 `ToolDefinition` 字段重复。
3. Registry 只返回异常文本，没有统一 error code/stack reference。
4. 直接调用 Registry 会绕过 PermissionHook。
5. 同一响应中的多个工具由 Agent 顺序执行。
6. Web Search 将异常作为成功字符串返回，配置和测试较弱。
7. Basic Bash 是否获得 Context 取决于 Background Manager，部分同步 command artifact 缺少真实 run/call id。
8. Verification 只支持 pytest 和 Ruff。
9. JUnit parser 对复杂嵌套 testsuites 的兼容有限。
10. Schema、Python 签名和 description 依赖人工保持一致。
11. 完整命令输出位于受保护的 `.llm_agent`，模型只能用预览或重跑工具。
12. SearchTools 顶层导入 SerpAPI，使其成为 tools 包导入条件。

## 29. 面试追问与回答要点

### Q1：为什么不用静态 JSON 注册工具？

本地工具要绑定 workdir、Manager、Runner 和 execution context。源码相邻 definition 让 schema、description 和 callable 一起演进；外部动态发现交给 MCP。

### Q2：三种 Tool 类型有什么区别？

ToolDefinition 是领域模块完整声明；Tool 是 Registry 内记录；ToolSpec 只保留给模型看的 name/description/parameters。

### Q3：`requires_context` 为什么不写进模型参数？

run id、workdir、parent id 和 callback 是 Harness 可信状态，不能让模型提供。Agent 在权限通过后构造 Context，Registry 按标记注入。

### Q4：为什么 pytest 失败时 `ok=true`？

工具成功运行并获得有效证据，业务 outcome 是 failed。`ok=false` 留给未知工具、参数非法或实现异常。

### Q5：如何防止改错文件？

能力白名单、PreToolUse 确认、workspace/sensitive path 校验、read SHA256、exact-once edit 和 atomic replace 多层协作。

### Q6：为什么还保留 Bash？

Coding task 命令空间开放，无法为每个构建器和脚本预建工具。Bash 提供通用能力；高价值验证和文件操作用结构化工具获得可靠语义。

### Q7：如何隔离子进程环境？

Command Runner 使用安全环境集合、固定 cwd、关闭 stdin、timeout 和进程组；目标 uv 项目额外移除父 `VIRTUAL_ENV`。

### Q8：为什么 QA 没有 Bash 和写工具？

角色能力通过 Registry subset 硬裁剪。QA 只需 read/search/test/lint；不给 Bash 可以阻止绕过只读约束。

### Q9：MCP 如何让 Agent Loop 无感？

MCP binding 被转换成同一 ToolDefinition，通过 closure 调用 MCPManager；权限由 MCP Hook 处理。

### Q10：为什么工具错误后继续？

文件不存在、参数过期和测试失败是环境 Observation。回灌后模型可以修正；只有 Harness invariant 才终止 Run。

### Q11：为什么不自动并行？

Coding 工具可能修改同一文件或相互依赖。当前顺序执行保证因果稳定；未来只并行显式无副作用工具。

### Q12：下一步最值得改什么？

先统一 Search Tool 错误语义并补 contract tests；再根据评测决定只读并行和多语言 Verification，而不是继续堆工具数量。

## 30. 建议源码阅读顺序

1. `ToolDefinition`、`ToolSpec`、`ToolRegistry.call()`。
2. `tools/__init__.py` 的默认装配。
3. `basic_tool_definitions()` 的 Facade/schema 绑定。
4. `BasicTools` 的可靠文件修改链。
5. `command_runner.run_command()` 的隔离和持久化。
6. `VerificationTools` 的双层结果语义。
7. Task/Artifact/Memory/Skill Facade 的 Manager 分层。
8. Background/Subagent/Worktree 的 Context 使用。
9. `mcp_tools._to_definition()` 的外部接入。
10. `Agent._execute_tool_call()` 的 Hook、Registry 和回灌。
11. 工具相关测试中的真实文件和子进程边界。

下一步可继续梳理 Hook 与 Security System：Tool System 解释“能力如何注册和执行”，下一篇深入“执行前如何允许、拒绝或确认”，以及路径、命令、环境变量和 MCP 权限如何协作。
