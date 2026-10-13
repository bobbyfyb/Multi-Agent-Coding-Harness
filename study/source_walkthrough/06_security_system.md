# Security System 源码精读

> 核心源码：[src/llm_agent/security.py](../../src/llm_agent/security.py)  
> 执行入口：[src/llm_agent/tools/basic_tools.py](../../src/llm_agent/tools/basic_tools.py)、[src/llm_agent/command_runner.py](../../src/llm_agent/command_runner.py)、[src/llm_agent/tools/verification_tools.py](../../src/llm_agent/tools/verification_tools.py)、[src/llm_agent/background_jobs.py](../../src/llm_agent/background_jobs.py)  
> 相关边界：[src/llm_agent/hooks/permission_hooks.py](../../src/llm_agent/hooks/permission_hooks.py)、[src/llm_agent/worktree.py](../../src/llm_agent/worktree.py)、[src/llm_agent/mcp_system.py](../../src/llm_agent/mcp_system.py)、[src/llm_agent/trace_system.py](../../src/llm_agent/trace_system.py)

## 1. 模块目标

Security System 假定模型不可靠：它可能幻觉路径、生成危险命令、泄露环境变量或错误覆盖文件。

系统重点限制：

1. 结构化文件工具逃逸 workspace 或访问敏感路径。
2. 已知高风险 Shell 命令和共享环境修改。
3. 子进程自动继承 LLM API Key 等宿主 Secret。
4. 长任务、输出和遗留子进程失控。
5. PM、QA、Explore 等角色拥有过多能力。
6. Engineer 修改未经审查直接进入主工作区。
7. MCP、Skill、Trace 等扩展面扩大信任范围。

完整链路：

```text
Role/Subagent capability allowlist
              |
              v
       JSON Schema / 参数检查
              |
              v
       PreToolUse PermissionHook
       硬检查 + 人工确认
              |
              v
       Tool 内部再次校验
              |
              v
    safe_subprocess_env + timeout
              |
              v
  Worktree / Manager business invariant
              |
              v
     bounded output + sanitized Trace
```

每层只降低一类风险，没有任何一层被当作完整沙箱。

## 2. 威胁模型

### 2.1 主要防护对象

当前实现主要防护非恶意但不可靠的 Agent 行为：

- 把 `../` 或绝对路径当成项目文件。
- 读取或覆盖 `.env`、SSH Key、云凭据。
- 运行 `sudo`、`rm -rf /` 等明显危险命令。
- 在 Harness 共享 Python 环境执行 `pip install`。
- 通过 `cd ..` 离开项目目录。
- 子进程通过 `env` 看到 Provider Key。
- 把 verification target 伪装成 CLI option。
- QA 或 Explore 获得通用 Bash/写文件能力。
- 工具输出和 Trace 无限增长或直接记录 Secret。

### 2.2 信任主体

当前默认信任：

- 启动 Harness 的本地用户和 Harness Python 代码。
- 用户选择启用的项目配置。
- 用户明确批准的工具调用。
- 用户明确配置和授权的 MCP Server。
- 用户允许执行的目标仓库测试、lint 和依赖代码。

### 2.3 不在保证内

当前不能抵御：

- 恶意本地用户或被入侵的宿主机。
- 获批 Bash 用替代语法绕过字符串规则。
- 获批进程用宿主用户权限读取任意绝对路径。
- 目标代码主动访问网络、文件系统或耗尽资源。
- 恶意 MCP Server 滥用显式提供的凭据。
- Prompt Injection 后用户又自动批准危险操作。
- 内核级、系统调用级和网络级攻击。

准确表述是“Harness 级纵深防御”，不是“安全沙箱”。

## 3. 源码地图

| 文件 | 安全职责 |
| --- | --- |
| `security.py` | 路径、敏感文件、环境和 Shell 公共原语 |
| `permission_hooks.py` | 执行前调用原语并接入用户确认 |
| `basic_tools.py` | 文件、搜索、Glob、Bash 内部二次校验 |
| `command_runner.py` | 同步进程环境、日志、超时和进程组 |
| `background_jobs.py` | 后台进程并发、超时、取消和恢复 |
| `verification_tools.py` | 受控 pytest/Ruff 命令和 target 校验 |
| `subagent.py` | Explore/General capability allowlist |
| `serial_workflow.py` | PM/Engineer/QA capability allowlist |
| `worktree.py` | 修改隔离、补丁审查和显式 apply |
| `mcp_config.py` / `mcp_system.py` | MCP 配置、环境、scope 和 permission |
| `skill_system.py` | Skill 根目录、资源路径和大小限制 |
| `trace_system.py` | Secret 脱敏、内容开关和长度限制 |

## 4. `security.py` 常量

### 4.1 敏感目录

`SENSITIVE_DIR_NAMES` 包括：

- `.aws`、`.gcloud`、`.kube`
- `.ssh`
- `.git`
- `.llm_agent`

保护 `.llm_agent` 可防止模型通过普通文件工具篡改 Task、Memory、Trace、Worktree metadata 和工具日志；这些状态只能经过对应 Manager/Tool API。

### 4.2 敏感文件

`SENSITIVE_FILE_NAMES` 包括 `.env`、`.netrc`、`.npmrc`、`.pypirc`、credentials 文件和常见 SSH 私钥名。

`SENSITIVE_SUFFIXES` 包括：

- `.env`
- `.key`
- `.pem`
- `.p12`
- `.pfx`

所以 `config.env` 也会被拒绝，不只精确文件名 `.env`。

### 4.3 Shell 硬拒绝

`HARD_DENY_COMMAND_FRAGMENTS` 包含 `rm -rf /`、`sudo`、`shutdown`、`reboot`、`mkfs`、`dd if=` 和写入 `/dev/sda` 等常见高风险模式。

匹配使用 `casefold()`，不区分大小写。

### 4.4 环境变量分类

`SECRET_ENV_MARKERS`：

- API_KEY
- AUTH
- CREDENTIAL
- PASSWORD
- SECRET
- TOKEN

`PASSTHROUGH_ENV_KEYS` 只保留 locale、PATH、TERM、TMPDIR、TZ 和 VIRTUAL_ENV 等基础变量。Secret marker 检查优先，因此 `LC_AUTH_TOKEN` 仍会被删除。

## 5. `resolve_workspace_path()`

```python
resolve_workspace_path(
    workdir,
    path,
    *,
    allow_sensitive=False,
) -> Path
```

步骤：

1. `workdir.resolve()` 得到 workspace。
2. `(workspace / path).resolve()` 得到最终 candidate。
3. `candidate.is_relative_to(workspace)` 判断边界。
4. 默认调用 `is_sensitive_workspace_path()`。
5. 失败抛 `ValueError`，成功返回绝对 Path。

### 5.1 为什么先 resolve

它可以处理：

- `../outside`
- 绝对路径
- 字符串前缀碰撞
- 指向 workspace 外的已有 symlink

这比 `str(path).startswith(str(workspace))` 可靠。

### 5.2 绝对路径和 `allow_sensitive`

工作区外绝对路径会被拒绝；工作区内绝对路径可通过，但 schema 仍要求相对路径。

`allow_sensitive=True` 是可信 Harness 代码的逃生参数。当前模型可见 Basic/Verification Tool 都未开放该参数。

### 5.3 TOCTOU

resolve 与实际 open 不是原子系统调用。并发恶意进程若在两者间替换 symlink，仍有 time-of-check/time-of-use 风险。更强方案需要 `openat`/dirfd 或 OS 沙箱。

## 6. `is_sensitive_workspace_path()`

流程：

1. resolve workspace 和 candidate。
2. workspace 外直接视为 sensitive。
3. 相对路径 segment 全部 `casefold()`。
4. 任一 segment 命中敏感目录即拒绝。
5. 最终文件名命中精确名称或后缀即拒绝。

| 路径 | 结果 |
| --- | --- |
| `src/app.py` | 允许 |
| `src/.env` | 拒绝 |
| `config.env` | 拒绝 |
| `.ssh/config` | 拒绝 |
| `docs/key.pem` | 拒绝 |
| `.github/workflows/test.yml` | 允许 |
| `../outside.txt` | 拒绝 |

它不做内容扫描。普通 `notes.txt` 即使包含 Token，也不会因内容被自动屏蔽。

## 7. Search 与 Glob 防护

Search 使用三层过滤：

1. 搜索根先经过 `resolve_workspace_path()`。
2. rg 命令追加 `sensitive_glob_excludes()`。
3. 每个 match 再调用 `is_sensitive_workspace_path()`。

用户显式传 `globs=["*.env"]` 时，Harness 追加的 exclude 仍生效。

rg 不存在时 Python fallback：

- 跳过 hidden 目录和文件。
- 每个路径再次检查 sensitive。
- NUL 字符视为二进制边界。
- 有 30 秒 deadline 和 max_results。

`glob()` 也 resolve 每个候选并过滤 workspace 外和 sensitive 路径。

正常 rg/glob 仍允许 `.github` 等非敏感项目配置；fallback 为简洁起见更保守地跳过所有 hidden 项。

## 8. `safe_subprocess_env()`

默认子进程会继承完整 `os.environ`。当前函数改为重建 allowlist：

```text
遍历父环境
  -> key 含 Secret marker：删除
  -> key 是基础 allowlist 或 LC_*：保留
  -> 其他：删除
  -> PATH 缺失时使用 DEFAULT_PATH
  -> HOME = cwd
  -> NO_COLOR / PYTHONIOENCODING / PYTHONUNBUFFERED
```

### 8.1 `HOME = cwd`

这让 `~` 默认指向 workspace，减少工具自动读取用户 Home 配置和 credential 的机会。

它不是文件系统隔离。命令仍可显式访问 `/home/user/...`。

### 8.2 固定变量

- `NO_COLOR=1`：减少 ANSI 噪声。
- `PYTHONIOENCODING=utf-8`：稳定日志编码。
- `PYTHONUNBUFFERED=1`：长任务及时写日志。

### 8.3 PATH 语义

父环境已有 PATH 时会保留，只有缺失时才回退到 `/usr/local/bin:/usr/bin:/bin`。

因此它不是 hermetic executable allowlist；父 PATH 中的自定义目录和同名程序仍可能被调用。

### 8.4 功能代价

严格 allowlist 也会删除 HTTP_PROXY、SSL_CERT_FILE、Git 特殊变量和编译器配置，某些项目命令可能因此失效。额外变量应通过受审查的专用配置注入，而不是恢复完整父环境。

## 9. `validate_shell_command()`

完整流程：

```text
command.strip() 非空
        |
        v
case-insensitive hard fragment scan
        |
        v
shlex 分段检查共享环境修改
        |
        v
提供 workdir 时检查 cd/pushd target
```

失败统一抛 `ValueError`。

`fragments` 可由可信调用方替换默认规则；传空 iterable 因为使用 `fragments or defaults`，仍采用默认列表。

## 10. 硬拒绝的价值与边界

优点：

- 对常见灾难性误操作快速 fail closed。
- 在用户确认前执行，不能输入 `y` 绕过。
- 错误能告诉模型命中了哪个规则。

局限：

- substring 不理解完整 Shell AST。
- 可能误伤只是打印危险字符串的命令。
- `rm -fr /`、变量拼接、alias、脚本封装等可能绕过。
- 无法判断任意程序内部副作用。

它是常见事故保护，不是恶意命令证明器。

## 11. Shell 分词

共享环境和 cd 检查都使用：

```python
shlex.shlex(
    command,
    posix=True,
    punctuation_chars=";&|()",
)
```

识别 `;`、`&`、`&&`、`|`、`||` 和括号等控制 token。未闭合引号会被拒绝。

共享环境检查扫描 segment 的每个 token，因此 `env pip install x` 中的 `pip` 也可识别。

但它不是完整 parser：重定向、命令替换、函数、here-doc、`eval` 和 `sh -c` 内嵌命令没有完整建模。

## 12. 共享环境修改保护

系统拒绝：

- `pip/pip3/pipx install|uninstall`
- `python -m pip install|uninstall`
- `conda/mamba/micromamba install|remove|uninstall|update`
- `uv pip install|uninstall`

目的是防止 Agent 修改 Harness 或用户共享环境，导致依赖污染、评测漂移或安装脚本获得宿主权限。

错误信息建议修改 dependency manifest，或使用 workspace 内虚拟环境。

## 13. Workspace venv 例外

`_is_workspace_executable()` 允许：

```text
./.venv/bin/pip install ...
./.venv/bin/python -m pip install ...
```

条件：

1. 配置 workspace。
2. executable token 含 `/`。
3. 不含 `$` 或反引号。
4. resolve 后位于 workspace。

裸 `pip install` 即使存在 VIRTUAL_ENV 仍拒绝，因为目标环境不够明确。`pipx` 不允许此例外。

## 14. `cd` / `pushd` 约束

`_validate_shell_cd_targets()` 维护逻辑 current：

- 无 target：回到 workspace。
- `$HOME` 或 `~`：映射 workspace。
- `~/child`：映射 workspace/child。
- `cd -`：拒绝。
- target 含 `$` 或反引号：拒绝动态目录。
- resolve 后必须位于 workspace，且不能进入敏感目录。

限制同样明确：

- 只识别 literal `cd`/`pushd` token。
- 不追踪 alias、函数、`eval`、`sh -c`。
- 只限制工作目录切换，不限制其他命令的绝对路径参数。

## 15. Hook 与 Tool 二次校验

结构化文件调用：

```text
PermissionHook.resolve_workspace_path()
  -> 必要时用户确认
  -> BasicTools.safe_path()
  -> resolve_workspace_path() 再检查
```

同步 Bash：

```text
PermissionHook.validate_shell_command()
  -> 用户确认
  -> BasicTools.validate_shell_command()
  -> run_command(shell=True)
  -> validate_shell_command() 第三次
```

后台 Bash 在 `BackgroundJobManager.start_shell()` 再检查。

重复用于保护多个入口：

- Hook 保护标准 Agent 流程。
- Tool 保护直接 `ToolRegistry.call()`。
- Runner/Manager 保护未来内部调用方。

但多次使用同一个 parser 不会消除共同盲点，也不会升级成 OS 沙箱。

## 16. Basic File Tools

### 16.1 有界读取

`read_file` 默认 400 行、最多 2000 行，返回 SHA256、总行数和 truncated。它降低单次 Prompt Injection 与上下文爆炸风险。

### 16.2 乐观并发

write/edit 可带 `expected_sha256`。文件在 read 后发生变化时拒绝覆盖，防止用户、后台任务或其他 Agent 的修改被静默抹掉。

### 16.3 精确编辑

`old_text` 必须非空且恰好出现一次，替换后必须有变化，降低改错位置风险。

### 16.4 原子写入

`_atomic_write_text()`：

1. 同目录随机临时文件。
2. 写 UTF-8。
3. 保留已有文件 mode。
4. `replace(path)` 原子替换。
5. finally 清理临时文件。

它避免进程中断留下半写文件，但不解决 symlink TOCTOU 或跨文件事务。

## 17. `command_runner.run_command()`

Runner 统一设置：

- 固定 cwd。
- safe env。
- `stdin=DEVNULL`。
- stdout/stderr 写文件。
- POSIX `start_new_session=True`。
- timeout 和 preview 上限。

`stdin=DEVNULL` 防止子进程占用 Agent CLI 输入。

### 17.1 shell 与 argv

- Basic Bash：`shell=True`，先校验字符串。
- Verification：argv list + `shell=False`。

能使用固定 argv 时不走 Shell，可消除 metacharacter 注入。

### 17.2 日志

完整日志写入：

```text
.llm_agent/tool-results/<run-id>/<tool-call-id>/
```

返回模型的是尾部 preview。ID component 会清洗并限制长度，避免状态路径穿越。

### 17.3 timeout 和进程组

超时后对 process group 发 SIGTERM，等待 grace period，仍未退出再 SIGKILL。处理进程组降低遗留子进程概率。

当前没有 CPU、内存、磁盘、PID 和网络配额。

## 18. Background Job 安全

Background Manager 继续使用：

- Shell 校验和 safe env。
- DEVNULL stdin 和新 process session。
- max concurrency、max runtime。
- stdout/stderr 文件和 tail 上限。
- cancel/shutdown 终止进程组。
- 重启后把遗留 running 状态标成 interrupted。

Job ID 拒绝 slash/backslash，避免状态文件路径穿越。

它解决生命周期失控，不解决获批代码的宿主权限。

## 19. Verification Tools

QA 不拼通用 Bash，而使用固定 argv：

```text
uv project:
  uv run --locked --no-env-file python -m pytest ...
  uv run --locked --no-env-file python -m ruff ...

fallback:
  sys.executable -m pytest/ruff ...
```

### 19.1 target 保护

`_validate_targets()`：

- 拒绝空值和 `-` 开头，避免 option injection。
- pytest node id 按 `::` 取文件路径。
- 路径必须在 workspace、非敏感且存在。

keyword 作为 `-k` 后的独立 argv，不进入 Shell。

### 19.2 环境保护

uv 模式使用：

- `--locked`：不静默改 lockfile。
- `--no-env-file`：不加载目标 `.env`。
- unset `VIRTUAL_ENV`：防 Harness venv 干扰。
- safe subprocess env。

### 19.3 为什么仍需确认

pytest 会 import 和执行项目代码。固定命令形态降低注入风险，但测试本身仍是代码执行，因此 PermissionHook 默认要求确认。

## 20. Capability Allowlist

安全第一层是不给角色不需要的工具。

### 20.1 Explore Subagent

只有 read_file、glob、search_text、web search 和 Skill read tools。没有 Bash、write/edit、tests/lint。

### 20.2 General Subagent

才增加 Bash、写/编辑和验证，并继续经过 PermissionHook。

### 20.3 Workflow Role

- PM：读取、Task/Artifact、Memory read、Skill。
- Engineer：增加 Bash、写/编辑、tests/lint。
- QA：读取、Task/Artifact、tests/lint；没有 Bash、write/edit。

QA 能运行受控验证但不能随意修改源码。MCP 还按 scope 动态裁剪。

## 21. Worktree 隔离

Worktree 是修改 blast-radius 控制，不是运行时 sandbox。

关键不变式：

1. 创建前主工作区必须干净。
2. 保存创建时 base commit。
3. Agent 在 managed worktree 中修改。
4. runtime `.llm_agent` 不进入 patch。
5. diff 生成 binary patch 和 SHA256。
6. apply 前主 HEAD 仍须等于 base。
7. 先 `git apply --check` 再 apply。
8. apply 需要用户确认。
9. 未应用修改默认不能 remove。
10. discard 需要确认。

ID 有固定正则，metadata 内 ID 必须一致，active path 必须位于 managed root。

它保证 Engineer 的修改不会自动进入主工作区，但不限制进程读取宿主文件或访问网络。

## 22. MCP 安全边界

### 22.1 配置校验

`mcp_config.py` 使用 `yaml.safe_load` 并拒绝未知字段。

HTTP 默认必须 HTTPS，只有 localhost/loopback 可用 HTTP。Tool 还受 expose_to、include/exclude、permission、max_tools 和 schema 大小限制。

### 22.2 stdio Secret

stdio Server 从 safe env 开始，再合并配置显式 env：

```python
env = safe_subprocess_env(workspace)
env.update(expanded_server_env)
```

父 `ANTHROPIC_API_KEY` 不会自动泄漏；配置显式引用 `$GITHUB_TOKEN` 时会有意委托给该 Server。

### 22.3 高信任配置

stdio command/args/cwd 由配置决定。cwd 当前只 resolve，不强制位于 workspace；显式 env 也能引用任意现有环境变量。

所以能修改 `.llm_agent/mcp.yaml` 的人等同于能配置本地程序执行和 Secret 接收方。普通模型文件工具不能访问 `.llm_agent`，但维护者仍必须审查配置。

远程 MCP 副作用也不受本地 Worktree 保护，最小权限 Token 和 confirm/deny policy 很重要。

## 23. Skill 安全边界

SkillRegistry：

- manifest 必须位于 skill root。
- resource 必须是相对路径且不能逃逸 root。
- manifest/resource/catalog 有长度上限。
- YAML 使用 `safe_load`。
- Skill 名有长度和字符正则。

Skill policy 明确不能覆盖 System、用户要求、workspace 和 permission。

Skill 脚本不会自动执行；模型必须调用 Bash，仍经过 capability 和审批。但 Skill 文本仍是不可信指令来源，Prompt precedence 不是形式化 Prompt Injection 防护，第三方 Skill 需要审查。

## 24. Trace 脱敏

TraceConfig 默认：

- `capture_llm_content=False`
- `capture_tool_content=True`
- `max_inline_chars=8000`

写入前递归处理：

- key 含 token、authorization、cookie、password、private key 等时替换 `[REDACTED]`。
- Bearer Token、key=value Secret、PEM private key 由正则替换。
- 超长值截断并记录 chars/bytes/SHA256。
- 嵌套超过 10 层停止。

限制：

- 未命中 key/regex 的 Secret 仍可能记录。
- Tool stdout 可能包含敏感业务数据。
- 完整 stdout/stderr 文件不经过 Trace sanitizer。

因此 `.llm_agent` 被普通文件工具屏蔽，且应按敏感本地数据管理。

## 25. 两条完整链路

### 25.1 write_file

```text
角色 capability 包含 write_file
        |
        v
LLM arguments 是 JSON object
        |
        v
PermissionHook resolve path
  escape/sensitive -> deny
        |
        v
用户确认修改
        |
        v
BasicTools.safe_path 再检查
        |
        v
expected_sha256 乐观锁
        |
        v
临时文件 + 原子 replace
        |
        v
Tool Result + Trace
```

### 25.2 Bash

```text
只有 Main/Engineer/General 有 Bash
        |
        v
PermissionHook validate_shell_command
        |
        v
用户确认所有 Bash
        |
        v
BasicTools / Runner 再验证
        |
        v
safe env + HOME=workspace + DEVNULL
        |
        v
process session + timeout + bounded preview
```

这降低常见事故，但命令仍运行在宿主 OS。

## 26. 与 learn-claude-code s03 的差异

| 维度 | s03 教学版 | 当前项目 |
| --- | --- | --- |
| 权限入口 | Loop 内 `check_permission` | PreToolUse Hook |
| 路径 | workspace 逃逸 | symlink-aware resolve + sensitive names |
| Bash | 简单 deny list | deny + env mutation + cd target |
| 审批 | 直接 `input()` | ApprovalProvider |
| 校验位置 | 上层一次 | Hook + Tool + Runner/Manager |
| 子进程环境 | 完整继承 | allowlist safe env |
| HOME | 宿主 Home | workspace |
| 进程管理 | 单 timeout | process group、后台生命周期 |
| 写文件 | 直接 write | SHA 乐观锁 + 原子替换 |
| 验证 | 任意 Bash | 固定 pytest/Ruff argv |
| 角色 | 单 Agent 全工具 | 角色 allowlist |
| 修改隔离 | 无 | Worktree + explicit apply |
| MCP | 无 | scope、policy、safe env、HTTPS |
| Trace | print | 结构化脱敏与长度治理 |

演进方向是从“三道权限闸门”扩展为“能力、审批、执行、隔离、审计”多层模型。

## 27. 为什么不是完整 Sandbox

完整 Sandbox 通常需要：

- Docker/Podman 或远程 ephemeral runner。
- Linux namespace、seccomp/AppArmor。
- cgroup CPU/内存/PID 配额。
- 只读 bind mount 和临时写层。
- 网络 egress policy。
- 每任务独立用户。

这些能力平台相关且扩大运维范围。当前项目优先实现可解释、可测试的最小安全改进：常见越界保护、Secret 自动继承隔离、最小角色能力、Worktree、人工确认和 Trace。

对于本地、用户在环、受信仓库的 Coding Harness 是合理 MVP；服务化或运行不可信仓库时，真实 Sandbox 是必需基础设施。

## 28. 当前限制与潜在漏洞

1. Shell deny list 可被语法变体、脚本或替代工具绕过。
2. Bash 可用绝对路径读写 workspace 外；cd 检查不是文件系统 sandbox。
3. safe env 保留父 PATH，不保证 executable 来源。
4. 没有网络隔离。
5. 没有 CPU、内存、磁盘、PID 配额。
6. daemonized/disowned 进程可能逃离进程组治理。
7. resolve/open 存在 symlink TOCTOU。
8. sensitive path 基于文件名，不做内容 DLP。
9. Trace sanitizer 是启发式。
10. 完整工具日志可能含敏感输出。
11. AutoApprovalProvider 会放大 Bash/MCP 风险。
12. 目标测试代码以宿主用户权限运行。
13. MCP config 可执行本地程序、选择 workspace 外 cwd、委托 Secret。
14. HTTPS 不证明 MCP Server 可信。
15. Skill precedence 不能形式化阻止 Prompt Injection。
16. 直接 Registry 调用绕过 PermissionHook，但 Basic 内部仍保留部分校验。
17. Permission 规则还不能通过用户/项目配置合并。
18. 审计文件没有权限、加密和保留周期管理。

这些边界说明当前方案适合的场景，不抵消其对常见 Coding Agent 事故的实际价值。

## 29. 设计取舍

### 29.1 为什么集中在 `security.py`

路径、Shell 和 env 被多个执行入口复用。集中后语义一致、便于单点审计和测试。领域 Manager 仍负责自己的 ID、状态和业务 invariant。

### 29.2 为什么重复检查

调用入口不唯一。Hook 管策略，Tool/Runner 靠近副作用。上层遗漏时，底层仍能拒绝基础越界。

### 29.3 为什么 env 用 allowlist

Secret 名称 denylist 难以穷举。先只保留少量运行所需 key，再用 Secret marker 排除，默认暴露面更小。

### 29.4 为什么所有 Bash 都确认

任意 Shell 风险难以准确分类。MVP 让用户看到每条 Bash，read/search 等结构化操作直接放行。代价是确认疲劳，后续应增加声明式规则或真实 sandbox，而不是无限扩充字符串 deny list。

### 29.5 为什么 QA 有 tests 没有 Bash

QA 需要运行代码提供 Evidence，但不需任意命令。专用 Verification 固定命令、环境和输出，权限小于 Bash。

### 29.6 Worktree 为什么不能替代 Sandbox

Worktree 只隔离 Git 修改，不限制读取、网络或资源。它是变更隔离，不是执行隔离。

## 30. 测试覆盖

### 30.1 Basic/Permission

现有测试覆盖：

- workspace 逃逸。
- sensitive read/write/search/glob。
- dangerous Bash。
- pip/conda/uv 共享环境修改。
- 外部 cd 与内部 cd。
- 父 Secret env 不进入 Bash。
- stale SHA 和 ambiguous edit。
- Permission deny 后工具未执行。

### 30.2 Runner/Background/Verification

覆盖：

- timeout 结构化返回。
- 后台 Secret env 隔离。
- cancel、timeout、并发和重启恢复。
- uv `--locked --no-env-file`。
- VIRTUAL_ENV unset。
- sensitive target。
- tests/lint 结构化结果。

### 30.3 Worktree/MCP/Subagent

覆盖：

- 主工作区 clean。
- stale HEAD 拒绝 apply。
- 未应用修改拒绝 remove。
- runtime state 不进 diff。
- Explore/General 能力差异。
- MCP stdio 不继承 ANTHROPIC_API_KEY。
- MCP 显式 env、scope 和 permission。

### 30.4 缺失测试

目前没有独立 `tests/test_security.py`。值得补充：

- symlink 指向 workspace 外。
- 大小写敏感文件名。
- `cd -`、pushd、动态 cd。
- workspace venv 例外。
- safe env 精确保留/删除矩阵。
- PATH 继承策略。
- Shell 已知 bypass 作为明确非保证。
- Trace Secret 正则和原始日志边界。

## 31. 面试追问

### Q1：这是 Sandbox 吗？

不是。它是 Harness 级纵深防御。生产多租户还需容器/namespace/cgroup/网络策略。

### Q2：为什么用 `resolve + is_relative_to`？

可处理 `..`、绝对路径、前缀碰撞和已有 symlink；但仍有 resolve/open TOCTOU。

### Q3：为什么 Hook 和 Tool 都检查？

Hook 负责策略与交互；Tool 靠近副作用，保护直接 Registry 调用。二者职责不同。

### Q4：safe env 解决什么？

防止子进程自动继承 Provider/MCP Secret，并把 HOME 指向 workspace。它不限制显式文件读取和网络。

### Q5：为什么 Verification unset VIRTUAL_ENV？

让 uv 选择目标项目锁定环境，避免 Harness venv 掩盖缺失依赖或版本问题。

### Q6：为什么禁止裸 pip install？

目标环境不明确，可能污染 Harness/共享环境。显式 workspace venv 才允许，更推荐更新 manifest/lock。

### Q7：deny list 有什么缺陷？

Shell 语义复杂，substring 有误报漏报，只适合常见事故。强安全必须依赖 OS/远程 sandbox。

### Q8：Worktree 提供什么？

隔离代码修改、固定 base、生成可审 patch 并显式 apply；不限制进程权限。

### Q9：为什么 QA 不给 Bash？

QA 只需受控 pytest/Ruff 生成 Evidence。Capability 层只读比 Prompt 约束可靠。

### Q10：MCP Secret 为什么不算自动泄漏？

safe env 不传父 Secret；只有配置显式引用环境变量才委托给 Server，因此配置和 Token scope 是高信任边界。

### Q11：Trace 能完全防密钥吗？

不能。它默认关闭 LLM content，并做 key/regex 脱敏与截断，但原始工具日志和未知格式 Secret 仍需保护。

### Q12：下一步最值得加强什么？

本地项目先补 `test_security.py` contract tests 和声明式 permission；服务化时优先真实 sandbox、网络和资源配额。

## 32. 建议阅读顺序

1. `security.py` 常量。
2. `resolve_workspace_path()` / `is_sensitive_workspace_path()`。
3. `safe_subprocess_env()`。
4. `validate_shell_command()`。
5. environment mutation 和 cd helper。
6. PermissionHook。
7. Basic safe_path/bash/search/glob。
8. `_atomic_write_text()`。
9. command runner。
10. Background monitor/terminate。
11. Verification module command/target。
12. Subagent/Workflow allowlist。
13. Worktree invariant。
14. MCP config/transport/permission。
15. Skill resource path。
16. Trace sanitizer。
17. 相关测试。

下一篇可以继续梳理 Memory System：Security 解释运行边界，Memory 则解释跨 step、跨 run 的信息如何选择、持久化、反思、遗忘和召回。
