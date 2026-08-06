# Coding Tools、安全、验证、Trace 与 MCP

## 1. 设计原则

LLM 只产生结构化意图，真正改变文件和进程的是 Tool 层。因此 Tool 层是 Coding Harness 最重要的可信边界之一。

项目遵循五个原则：

1. 工具 schema、执行函数和依赖一起注册；
2. 所有路径先解析到 workspace，再执行 I/O；
3. 权限策略、安全校验和隔离分层防护；
4. 验证工具返回机器可消费的结构化证据；
5. 每次工具调用都可追踪，但展示层只打印有界预览。

核心文件：

- `src/llm_agent/tool_registry.py`
- `src/llm_agent/tools/`
- `src/llm_agent/command_runner.py`
- `src/llm_agent/security.py`
- `src/llm_agent/hooks/permission_hooks.py`
- `src/llm_agent/trace_system.py`
- `src/llm_agent/mcp_config.py`
- `src/llm_agent/mcp_system.py`

## 2. ToolRegistry

### 2.1 数据结构

每个工具由 `ToolDefinition` 描述：

```python
@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]
    func: ToolFunction
    requires_context: bool = False
```

Registry 负责：

- 注册并拒绝重名；
- 输出统一 `ToolSpec` 给 LLMClient；
- 按名称执行函数；
- 将异常转为一致的错误结果；
- 通过 `subset()` 构建角色专属工具集合。

### 2.2 为什么不使用静态 tools.json

静态 JSON 适合声明 schema，但工具还需要闭包或依赖注入，例如：

- Artifact tool 需要 `ArtifactManager`；
- Memory tool 需要 `MemoryManager`；
- Background tool 需要 `BackgroundJobManager`；
- MCP tool 需要活跃 session；
- 所有本地工具都需要具体 workdir。

当前让每个模块暴露 `register_tools(registry, ...)`，统一由 `tools/__init__.py` 组合。Schema 和实现位于同一模块，角色再从完整集合中做 allowlist subset，减少声明漂移。

### 2.3 工具分类

```text
Coding I/O:      read_file / write_file / edit_file / glob / search_text
Execution:       bash / background bash
Verification:    run_tests / run_lint
Coordination:    task_* / artifact_*
Knowledge:       memory_* / skill_*
Delegation:      subagent_*
Isolation:       worktree_*
External:        search / mcp__<server>__<tool>
```

工具数量不是越多越好。Workflow 每个角色只看到完成该阶段所需的子集，降低误调用概率和 schema token 成本。

## 3. Reliable Coding Tools

### 3.1 文件读取

`read_file` 支持工作区相对路径、有界读取和必要的文本信息。它不允许通过 `../` 或绝对路径越出 workspace，也拒绝访问敏感路径。

为什么不直接给模型一个任意文件读取 API：

- workspace 外可能包含 SSH key、云凭据和其他项目；
- `.env`、Git credential 等即使位于 workspace，也不应默认暴露；
- 大文件必须限制返回体，否则会直接挤占上下文。

### 3.2 写入与编辑

`write_file` 和 `edit_file` 采用：

- workspace 路径解析；
- 敏感路径拒绝；
- 父目录显式处理；
- 临时文件 + replace 的原子写；
- 可选内容 SHA / expected state，避免基于旧内容覆盖新修改；
- 结构化返回 changed、path、hash 等信息。

`edit_file` 使用确定性文本替换，不让 Harness 猜测模糊 patch。匹配不存在或不唯一时失败，让 Agent 重新读取后再编辑。

### 3.3 Glob 与文本搜索

- `glob` 用于发现路径，限制结果数量并确保结果仍在 workspace。
- `search_text` 优先使用 `rg`，返回 path、line、column、text 等结构；不可用时有 Python fallback。

结构化结果比大段 shell 输出更容易压缩、追踪和被下一阶段复用。

### 3.4 Bash

`bash` 最灵活，也最危险。它经过以下链路：

```text
Tool Call
  -> PreToolUse PermissionHook
  -> command/path/environment validation
  -> CommandRunner
  -> stdout/stderr 截断返回 + 完整日志落盘
  -> PostToolUse / Trace
```

`CommandRunner` 返回：

- argv/command；
- exit code；
- stdout、stderr；
- duration；
- timed_out；
- 输出是否截断；
- 完整日志路径。

这样 Agent 可以使用短结果，评测和人工排障仍可查看完整日志。

## 4. 安全模型

### 4.1 四层防护

```text
角色工具 allowlist
  -> PermissionHook 策略与用户确认
  -> Tool 内部路径/命令/环境校验
  -> Git Worktree 隔离和显式 apply
```

没有一层被宣称为完整沙箱。它们分别降低不同风险：

- allowlist 降低模型可见能力；
- PermissionHook 管决策和人工确认；
- Tool validation 阻止越界与已知危险操作；
- Worktree 避免修改直接落到主工作区。

### 4.2 路径保护

`resolve_workspace_path()` 先 resolve 最终路径，再使用 `is_relative_to(workspace)` 校验，避免简单前缀比较和 `..` 绕过。

敏感路径策略额外保护：

- `.env` 和常见 credential 文件；
- SSH、云服务和认证目录；
- Git 内部敏感区域；
- Harness 自身运行状态中不应由模型直接修改的文件。

路径检查必须同时存在于 PermissionHook 和 Tool 内。Hook 是策略层，Tool 内检查是不可绕过的执行前置条件。

### 4.3 子进程环境隔离

`safe_subprocess_env()` 不会把父进程所有环境变量原样传给模型命令，而是：

- 保留执行所需的基础环境；
- 删除 API key、token、secret、credential 等敏感名称；
- 对 MCP stdio server 只注入配置中显式引用的环境变量；
- 验证目标项目时清理 Harness 的 `VIRTUAL_ENV`。

这能降低 `env`、异常堆栈或子进程把 LLM 凭据带出的风险。

### 4.4 命令策略

安全层拒绝或要求确认：

- 明显破坏系统的命令；
- 工作区外的 `cd/pushd`；
- 通过 shell 控制符绕过目录约束；
- 修改共享 Python 环境的 pip/uv 操作；
- 对受保护路径的读写；
- MCP 配置指定为 deny/confirm 的调用。

规则匹配不可能覆盖全部 shell 语义，所以高风险生产场景仍需容器、namespace、seccomp 或远程 sandbox。面试中不能把字符串规则称作“安全沙箱”。

### 4.5 只读 Explore 的含义

Explore Subagent 和 QA 角色通过 allowlist 不获得 write/edit/bash 等修改能力。只读是 capability 层保证，不依赖 prompt 中一句“不要修改”。

QA 仍可使用专门的 `run_tests` / `run_lint`，这些工具由 Harness 控制命令形态和证据输出；它不能借普通 bash 修改源码。

## 5. 项目环境验证

### 5.1 为什么不能直接运行 Harness 的 Python

目标仓库和 Harness 是两个项目。若使用当前 `sys.executable`：

- Harness 已安装的依赖会掩盖目标项目漏声明依赖；
- 目标仓库锁定的版本不会生效；
- QA 结论不可复现。

### 5.2 环境选择策略

验证工具优先识别项目元数据：

```text
pyproject.toml + uv.lock
  -> uv run --locked --no-env-file <test/lint command>
  -> 清除继承的 VIRTUAL_ENV
否则
  -> 使用明确的项目/回退解释器执行受控命令
```

`--locked` 的意义是：QA 验证已声明且已锁定的依赖，不在测试阶段偷偷修改 lockfile。`--no-env-file` 避免目标 `.env` 被自动加载到测试进程。

### 5.3 结构化错误分类

工具从输出中识别：

- `missing_dependency`；
- `lock_outdated`；
- `environment_setup_failed`；
- 普通 test/lint failure；
- timeout。

分类决定责任归属：

- dependency manifest 或 lock 问题交给 Engineer；
- QA 只记录证据并给出 fail；
- 基础设施暂时故障可触发 phase retry；
- 测试断言失败进入代码修复循环。

### 5.4 权威证据

Workflow 捕获当前 phase 的 verification event，而不是从 Artifact 文本中重新解析“测试通过”。证据包含 tool、exit status、failure kind、命令和时间等信息。

这让 Evidence Gate 可以机械判断：一个 pass TestReport 是否有同一阶段的成功运行支撑。

## 6. TraceRecorder

### 6.1 Trace 记录什么

`TraceRecord` 包含：

- trace/run/root/parent id；
- agent id、depth、step；
- category、name、phase、status；
- correlation id；
- duration；
- sanitized data；
- 全局递增 sequence 和 timestamp。

JSONL 适合追加写和流式分析，Markdown 是面向人的派生视图，不是权威原始数据。

### 6.2 父子关系

`TraceContext` 通过 `trace_scope` 和 `ContextVar` 传播：

```text
root CLI run
  -> workflow run
    -> PM agent run
    -> Engineer agent run
    -> QA agent run
  -> Subagent child run
  -> Background job event
```

每个角色/重试使用独立 run id，但共享 root_run_id，因此可以计算阶段耗时、调用量和失败位置。

### 6.3 脱敏和体积控制

Trace 对敏感 key 和 secret value pattern 做递归脱敏，并对大文本保存摘要/截断。`TraceConfig` 控制是否保存 LLM/tool content 和最大 inline chars。

CLI 的 Tool Result 预览和 Trace 存储是两套预算：CLI 为可读性截断，Trace 可以保留更多结构化细节或日志路径。

### 6.4 Trace、Workflow、Artifact 的区别

| 系统 | 主要用途 | 能否用于恢复 | 是否是交付物 |
|---|---|---|---|
| Trace | 发生了什么、耗时与成本 | 不直接恢复 | 否 |
| WorkflowStore | 当前应从哪里继续 | 是 | 否 |
| Artifact | 角色交付和评审内容 | 间接作为输入 | 是 |

不能通过“读最后一条 Trace”代替 checkpoint，因为 Trace 是事件流，不保证直接表达完整当前状态。

### 6.5 已知问题

自托管评测发现 Markdown renderer 曾把最后一个 child event 的状态/耗时当成 root summary。JSONL 原始记录仍正确。这说明派生报表必须基于 root event 或聚合规则，而不能把“最后一条事件”等同于“整个 run”。

## 7. MCP 集成

### 7.1 当前能力

项目使用官方 MCP Python SDK，支持：

- `stdio` transport；
- `streamable_http` transport；
- 启动时 tool discovery；
- include/exclude/max-tools 过滤；
- server/tool 级 allow/confirm/deny；
- `expose_to` 控制 main、PM、Engineer、QA、Subagent 等 scope；
- workspace-scoped session；
- timeout、结果大小限制和 Trace；
- required server 失败时启动失败，optional server 只给 warning。

### 7.2 适配链路

```text
.llm_agent/mcp.yaml
  -> MCPConfig 校验
  -> MCPManager 建立 transport/session
  -> discover remote tools
  -> MCPToolBinding
  -> ToolDefinition(name=mcp__server__tool)
  -> ToolRegistry
  -> PreToolUse PermissionHook
  -> MCPManager.call
  -> 统一 Tool Result / Trace
```

命名空间避免远程工具与本地工具重名，也能从 Trace 一眼看出来源。

### 7.3 为什么支持 Context7，但 GitHub HTTP OAuth 更复杂

Context7 可使用静态 API key 配置远程服务。GitHub 远程 MCP 常要求 OAuth：浏览器授权、redirect callback、authorization code 交换 token、refresh、加密存储和过期处理。

当前 CLI 没有 callback server、credential store 和 OAuth lifecycle，因此更适合通过已认证的本地 Docker/stdio server 接入 GitHub。stdio 子进程可以复用用户显式提供的 token，而 Harness 无需冒充 OAuth client。

### 7.4 尚未实现

- MCP resources 和 prompts 注入；
- sampling callback；
- OAuth 登录与 token refresh；
- server 动态变更订阅；
- 将本地 Coding Tools 作为 MCP Server 对外发布。

这些不是 Tool Calling MVP 的必要条件。

## 8. 面试追问

**Permission Hook 已确认命令，为什么 Tool 还要校验？**

Hook 可能被不同入口遗漏或配置错误，Tool 是最后执行边界；双层检查属于 defense in depth。确认只表示用户同意意图，不表示路径和参数天然安全。

**Worktree 是否等于沙箱？**

不是。Worktree 隔离 Git 修改，但进程仍运行在宿主机。它解决代码变更污染，不解决系统调用、网络和资源隔离。

**为什么 test result 比 QA Artifact 更可信？**

test result 来自 Harness 控制的命令和 exit code，Artifact 是模型生成的解释。模型可以总结机器证据，但不能改写事实。

**为什么不把所有工具都改成 MCP？**

本地工具需要深度集成 workspace、Worktree、权限、日志和验证证据。MCP 是互操作协议，不自动提供这些语义；额外进程边界反而增加部署和故障点。
