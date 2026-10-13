# Skill System 源码精读

> 核心源码：[skill_system.py](../../src/llm_agent/skill_system.py)  
> 工具：[skill_tools.py](../../src/llm_agent/tools/skill_tools.py)  
> 装配：[main.py](../../main.py)、[tools/__init__.py](../../src/llm_agent/tools/__init__.py)  
> 角色集成：[serial_workflow.py](../../src/llm_agent/serial_workflow.py)、[workflow_config.py](../../src/llm_agent/workflow_config.py)、[subagent.py](../../src/llm_agent/subagent.py)  
> 测试：[test_skill_system.py](../../tests/test_skill_system.py)、[test_workflow_config.py](../../tests/test_workflow_config.py)、[test_subagent.py](../../tests/test_subagent.py)、[test_serial_workflow.py](../../tests/test_serial_workflow.py)

## 1. 模块目标

Skill 为 Agent 提供可复用的任务方法、领域规范和参考资源。例如 PM 可以使用 PRD 写作方法，Engineer 使用代码审查规范，QA 使用检查清单。

当前实现由三部分组成：

1. Registry 发现并解析项目 Skill。
2. 三个工具让模型发现、加载正文和读取补充资源。
3. Workflow 根据角色配置预注入 required Skill，或提示 optional Skill。

Skill 改变模型获得的方法指导。工具 allowlist 和 Permission/Security 仍决定它能执行哪些操作。

## 2. 源码地图与整体链路

| 符号 | 功能 |
| --- | --- |
| `SkillMetadata` | name、description、when_to_use、路径和资源目录 |
| `SkillDocument` | metadata + instructions |
| `SkillRegistry` | 扫描、缓存、catalog 和资源读取 |
| `SkillTools` | skill_list/load/read_resource |
| `build_skill_catalog_section()` | 轻量发现目录与 precedence policy |
| `RoleSpec.required_skills/optional_skills` | 角色方法配置 |
| Workflow `_build_role_skill_sections()` | 角色上下文注入 |
| WorkflowConfig | 配置解析、缺失 Skill 的报错或警告 |
| SubagentRunner | 独立子上下文的 Skill 工具和 catalog |

```text
Startup
  -> SkillRegistry.refresh()
  -> metadata + documents caches
  -> available_skills PromptSection
  -> model discovers relevant skill
       -> skill_load(name)
       -> instructions as tool_result
       -> skill_read_resource(name, relative_path)
       -> referenced text as tool_result
       -> normal tools execute permitted actions

Workflow role construction
  -> global catalog
  -> optional role hints
  -> required instructions preloaded into System Prompt
```

Registry 既不调用 LLM 选择 Skill，也不运行脚本；模型选择和后续 Tool Calling 在 Agent Loop 中完成。

## 3. 目录布局与发现深度

默认根是 `.llm_agent/skills`：

```text
.llm_agent/skills/
  prd-writer/
    SKILL.md
    references/
      template.md
  code-review/
    SKILL.md
    scripts/
      inspect.py
```

refresh 使用 `skills_root.iterdir()`，只检查直接子目录中的 SKILL.md：

```text
skills/frontend/SKILL.md                  -> 注册一个 Skill
skills/frontend/ui/SKILL.md               -> 不独立注册 ui
skills/frontend/javascript/SKILL.md       -> 不独立注册 javascript
```

如果 frontend 自身有 manifest，其内部文件会作为资源列出；如果它只有多个子 Skill 目录而自己没有 manifest，整个包不会自动注册。

需要多个独立 Skill 时，将各自目录放到根下一层。clone 仓库是否可用取决于实际布局，不能只依据仓库名称。

## 4. `SkillMetadata` 与 `SkillDocument`

二者都是 frozen dataclass：Registry 构造后供调用方读取。

| Metadata 字段 | 含义 |
| --- | --- |
| name | 模型使用的精确查找名 |
| description | catalog 摘要 |
| when_to_use | 可选使用条件 |
| root | Skill 目录 resolve 后的绝对 Path |
| manifest_path | SKILL.md 的 resolve 路径 |
| resources | 相对资源路径 tuple |

SkillMetadata.to_dict 将 Path 转字符串、tuple 转 list，用于 skill_list。

SkillDocument 保存 metadata 和 instructions。to_dict 返回 name/description/when_to_use、instructions、resource_root、resources 和 policy。

没有执行入口、工具定义、依赖列表或权限授予字段。frontmatter 中其他插件/平台专属键不会自动产生相应能力。

## 5. 名称与描述解析

`SKILL_NAME_PATTERN`：首字符字母或数字，之后可用字母、数字、下划线、连字符，总长最多 64。

- name 缺失/空时使用目录名。
- description 缺失时取正文首个非空行，去掉开头 `#`。
- when_to_use 缺失时为空。
- 相应 metadata 存在但不是字符串时抛 SkillSystemError。
- 重名报错，不静默覆盖。

精确查找区分大小写；regex 不支持中文名或带 `/` 的 namespace。

描述被截到 max_description_chars，默认 300。正文未按描述预算截断。

## 6. YAML frontmatter

`_parse_frontmatter(text, manifest_path)`：

1. 文本不以 `---` 开头则全部当正文。
2. 从第二行起找整行恰好为 `---` 的结束分隔。
3. yaml.safe_load 解析 metadata。
4. 要求结果为 dict。
5. 结束分隔后为 instructions。

未闭合、非法 YAML 或非 mapping 会报错。safe_load 避免 YAML 反序列化任意 Python 对象，但不能证明正文内容可信。

没有 frontmatter 的普通 Markdown 也可用：目录名作为 name，第一条正文作为 description。

空正文在存在有效描述时可以注册；目前没有“Skill 必须含有效步骤”的语义校验。

## 7. Registry 配置与 `refresh()`

| 配置 | 默认值 | 作用 |
| --- | ---: | --- |
| max_skill_chars | 50000 | 单个 manifest 最大字符数 |
| max_resource_chars | 50000 | 单个资源读取最大字符数 |
| max_catalog_chars | 8000 | catalog 文本预算 |
| max_description_chars | 300 | description/when_to_use 截断长度 |

初始化 resolve skills_root、校验配置为正数，然后 refresh。

refresh 流程：

1. 建立新的局部 skills/documents dict。
2. 根不存在：注册空集合，不创建目录。
3. 根不是目录：报错。
4. 顺序扫描直接子目录，有 manifest 才处理。
5. 校验 manifest resolve 后位于该 Skill root 内。
6. 有限长度读取、解析、名称/描述校验。
7. 递归枚举资源路径。
8. 构建 Metadata/Document。
9. 全部完成后替换 `_skills` 和 `_documents`。

单个坏 manifest 会使整个 refresh 失败。若已有缓存，因最后才替换 dict，它一般仍保持旧集合；首次构造则失败。

### 7.1 渐进披露是模型侧行为

Registry 启动时读取并缓存所有 manifest 正文。load 从 `_documents` 返回，不再读盘。

渐进披露意味着默认 System Prompt 只带 catalog；需要的完整 instructions 才进入模型上下文。不能说所有正文都在调用 skill_load 时才从磁盘加载。

### 7.2 更新与刷新

运行中修改 SKILL.md，不会自动影响已缓存 Document。修改资源正文则下一次 read_resource 会读到新内容，因为资源不是正文缓存。

refresh 是 Python API，没有暴露 skill_refresh 工具或文件 watcher。catalog PromptSection 是创建时的字符串快照，单独 refresh Registry 也不自动重建已存在的 section。

CLI 重启是当前最清晰的重新发现路径。Main/Subagent 通常共享一个 Registry，Workflow 启动时另外构造一份；两份不会自动同步热更新。

## 8. `list_skills()`、`get()`、`load()`

- list_skills：按 name 排序返回全部 metadata。
- get(name)：取 metadata，不存在抛 SkillNotFoundError。
- load(name)：取缓存 Document，不存在同样报错。

没有“已加载集合”、自动依赖解析或一次只能启用某个 Skill 的状态机。重复 skill_load 会重复返回内容；Registry 不跟踪它是否已经进入某个 Agent 的 history。

## 9. `catalog()` 与 System Prompt

catalog 每条为 name、description，可附 when_to_use。只包含元数据，不附完整 instructions、resources 列表或 root。

达到 max_catalog_chars 时加入 omitted count；必要时移除之前的行，给省略 marker 留空间。

catalog 被省略的 Skill 仍存在 Registry，可以通过 skill_list 发现和精确 name 加载。

`build_skill_catalog_section()` 返回：

```text
name=available_skills
priority=35（Workflow 使用 34）
content=precedence instructions + catalog
token_budget=max_catalog_chars
```

这里 token_budget 只是 PromptSection 元数据，ContextManager 当前不按它自动裁剪。catalog 自身以字符限制，policy/wrapper 还会额外增加长度；它不是整个 section 的 tokenizer 精确预算。

## 10. `_list_resources()`

资源枚举使用 `skill_dir.rglob("*")`：选择文件，排除顶层 manifest，返回相对 POSIX 路径。

一个 Skill 内可以有多层 references、templates、scripts。它们不作为独立 Skill 注册。

枚举没有扩展名或文件数量过滤：二进制、Git metadata、其他子目录 SKILL.md 也可能列出。skill_list/load 输出完整 resources，列表很多时仍可能很长。

枚举不等于授予读取许可。symlink 指向外部的文件名可能被列出，真正读取时还会 resolve 并检查边界。

## 11. `read_resource()`

流程：

1. get(name) 确认已注册 Skill。
2. path 非空且相对。
3. `(skill.root / path).resolve()`。
4. candidate 必须在 root 内。
5. 不能是主 manifest，提示用 skill_load。
6. 必须是文件。
7. UTF-8 有限长度读取。

资源是文本：Markdown、代码、JSON、YAML 等均可读取，不支持 PDF/图片解析；二进制或非 UTF-8 会报错。

`_read_text_with_limit()` 只读取 limit+1 字符，超限直接失败，不返回截断预览，也没有分页/offset 参数。

工具描述要求资源来自“已加载 Skill”，但 Registry 没有 loaded-state 检查：知道一个已注册 name 和路径，可以直接 read_resource。

路径也不要求出现在缓存 resources 清单里。启动后新添根内文件可直接读，清单需 refresh 才更新。

## 12. 三个模型工具

| 工具 | 参数 | 结果 |
| --- | --- | --- |
| skill_list | 空 object | 全部 metadata，包括路径和 resources |
| skill_load | name | 完整 instructions 和 policy/resources |
| skill_read_resource | name、相对 path | name/path/content |

SkillTools 是冻结适配对象，持有 Registry。工具定义通过 register_tools 加入 ToolRegistry，返回 dict 再由统一协议回灌。

未知 Skill、路径逃逸和超长文件最终变成 ok=False/error，模型可改名或缩小读取目标。

这些工具没有 LLM 调用。决定要不要加载的是当前角色模型；用户显式要求某个 Skill 也经同样工具路径完成。

## 13. required 与 optional 角色 Skill

RoleSpec 增加 required_skills 和 optional_skills tuple。

Workflow `_build_role_skill_sections()` 构造三块 section：

1. 全局 catalog，priority=34。
2. role_optional_skills，priority=35，仅描述。
3. role_required_skills，priority=36，包含正文和资源路径。

### 13.1 required

`_format_required_skills()` 直接 registry.load，拼接 instructions 到 System Prompt，并注明不能覆盖 Workflow、用户和权限约束。

它是 Harness 预注入，没有 skill_load Tool Call。resource 正文仍需工具读取；不会把整个 references/scripts 一并注入。

required 表示必定提供知识，不等于确定模型遵循了所有步骤。当前没有独立 Skill compliance Gate。

### 13.2 optional

`_format_optional_skills()` 只提示 role-relevant name/description，实际正文仍由模型 skill_load。

optional_skills 不是安全 allowlist。角色还看见全局 catalog，默认也拥有三个 Skill 工具，可以加载列表之外其他已注册 Skill。

角色越权操作依然由 Tool profile 阻止；optional 名单本身只做方法提示。

## 14. Workflow 配置

默认 `.llm_agent/workflow.yaml`，示例假设对应三个 Skill 已注册：

```yaml
version: 1
roles:
  pm:
    required_skills:
      - prd-writer
  engineer:
    optional_skills:
      - code-review
  qa:
    optional_skills:
      - qa-checklist
```

角色 key 为 pm、engineer、qa、pm_acceptance。配置只允许 required_skills、optional_skills、max_steps，不通过 Skill 配置扩大 tool_names。

提供 Registry 时：

- required 不存在：WorkflowConfigError，启动报错。
- optional 不存在：过滤并记录 warning。
- 列表必须是非空字符串 list，结果转 tuple。

没有 Registry 时，配置层保留名字而不检查；角色构建在 Registry=None 时不添加 Skill section。直接 Python 装配应确保提供 Registry，避免把声明 required 当作自动加载保证。

字符串列表不自动去重；required/optional 重叠也不合并，可能重复描述同一 Skill。

## 15. Main Agent 与 Subagent

Main Agent 用 SkillRegistry.for_workdir，在 System Prompt 加 catalog，在默认 registry 加三个工具。

SubagentRunner 使用传入 Registry，独立上下文也带 catalog；explore/general 都有 Skill 读取工具。

所以 parent 加载的完整 Skill history 不自动复制给 child，但 child 可自己加载相同 Skill。Explore 仍无 bash/write/edit/test/lint；加载带脚本 Skill 也不改变该限制。

Workflow 的所有默认角色同样可用 Skill，PM 没有专属能力。

## 16. Skill 脚本和外部工具

带 scripts/ 的 Skill 当前可以枚举和读取脚本文本，但没有 skill_execute、自动安装依赖或自动运行 setup。

模型要执行脚本，需要使用已有 bash 等工具，经过角色 capability 和 PermissionHook。当前普通文件工具保护 .llm_agent，root Skill 的物理路径也可能不在 Workflow Worktree 内。

因此“文档包支持”不等于“任意开源 Skill 的脚本直接可运行”。可行流程是读取脚本、审查并适配到当前执行目录，再在获准工具内运行；还要处理依赖和输出。PM/QA/Explore 无通用 Bash 时不能因为加载 Skill 获得它。

Skill 提及某个 MCP/tool 名也不会自动注册那个工具；需要用户配置对应 MCP 或实现本地工具。

## 17. 与上下文压缩、恢复和交付协作

默认 catalog 和 required Skill 是 System Prompt sections；通常在 history 压缩时保留，但会每次请求占输入预算。

optional Skill 正文和 resource 是普通 tool results，可被 micro compact/summary/落盘处理。Registry 没有自动恢复“已加载 Skill 正文”的逻辑；模型可再次 skill_load/read_resource。

Workflow retry 新建角色 Agent/messages，再构造 required sections 和 catalog；optional 正文不自动完整复制上一 attempt，需结合 checkpoint 和任务判断重新加载。

Skill 指导如何生成 Artifact，但交付仍要遵守 kind/metadata/task contract 和 Evidence Gate。WORKFLOW_WORKER_OUTPUT_INSTRUCTIONS 要求适配 Skill 的互动澄清流程：在有可用核心需求时记录假设和风险，继续产生交付。

## 18. 一个完整运行示例

```text
Startup:
  scan prd-writer/code-review/qa-checklist
  workflow.yaml validates all names

PM construction:
  catalog + required prd-writer instructions
  no skill_load event needed

PM model:
  skill_read_resource("prd-writer", "references/template.md")
  applies template, creates PRD and TaskSpec artifacts

Engineer model:
  sees optional code-review
  skill_load("code-review")
  reads referenced checklist
  performs edits and normal verification tools
  writes ImplementationReport

QA model:
  skill_load("qa-checklist")
  run_tests/run_lint
  writes TestReport

Workflow Gate:
  checks real code/verification/task/artifact evidence
```

工具加载可在 tool_call/tool_result 日志中看到。required 预注入没有同名工具日志；相关 System Prompt 是否包含完整正文取决于 Trace 内容设置，不能保证普通 CLI 自动打印它。

Registry 未单独记录 skill.loaded domain event，目前观察依赖正常 Tool/LLM Trace。

## 19. 最小 Skill 与 Python 使用

文件 `.llm_agent/skills/code-review/SKILL.md`：

```markdown
---
name: code-review
description: Review patches for behavioral regressions.
when_to_use: When reviewing code changes.
---

# Review Method

Inspect changed call sites and failure paths.
Read references/checklist.md for the detailed checks.
```

配套 references/checklist.md 放在同 Skill 目录。

```python
from llm_agent.skill_system import SkillRegistry

registry = SkillRegistry.for_workdir("/path/to/project")
print(registry.catalog())
document = registry.load("code-review")
checklist = registry.read_resource("code-review", "references/checklist.md")
```

正文给模型提供方法，没有自动执行 inspect 函数。目录 name 与 frontmatter name 可不同，工具参数使用解析后的 name。

## 20. 安全与信任边界

- manifest resolve 后必须位于当前 resolved Skill root。
- resource resolve 后不能逃逸该 root；absolute 和 ../ 逃逸被拒绝。
- UTF-8、单文件字符上限、名称和 YAML 校验减少解析/资源风险。
- Skill 不自动授予工具权限或执行代码。

当前没有校验 resolved Skill root 必须位于 resolved skills_root 内。直接子目录如果是指向外部的目录 symlink，可以注册外部 Skill；后续读资源仍受该外部 Skill root 限制。

这需要与“资源不能逃逸已注册 root”区分，不能宣称整个 Registry 只能读取项目内 Skill。

资源读取也没有一般 workspace sensitive-path 检查。放入或链接到 Skill root 内的 .env/私钥等文本可能被读取，调用方应审查导入内容。

Prompt precedence 是约束语言，不是完整 Prompt Injection 防护。required 内容进入 System Prompt 尤其需要审查第三方文本；当前没有签名、版本锁或依赖审核机制。

## 21. 与 learn-claude-code s07 对比

| 维度 | s07 教学版 | 当前项目 |
| --- | --- | --- |
| 根目录 | skills | .llm_agent/skills |
| 扫描深度 | 直接子目录 | 同样直接子目录 |
| startup 读取 | 扫描正文缓存 | metadata/document 双缓存 |
| 模型发现 | catalog | catalog + skill_list |
| 正文加载 | load_skill | skill_load，带资源与 precedence policy |
| 资源 | 依赖一般文件工具 | 独立相对 Skill-root 资源工具 |
| metadata | 简单 name/description | when_to_use、名称校验、描述预算 |
| 错误 | 较宽松示例 | YAML/重复名/文件长度显式报错 |
| Subagent | 示例明确不支持加载 Skill | explore/general 均可独立加载 |
| Workflow 角色 | 未提供角色配置 | required 预注入、optional 提示、配置校验 |
| 自动执行 | 无 | 仍无；沿既有工具权限执行 |

这里的演进重点是共享能力包和可靠装配，不是复制完整插件执行平台。

## 22. 测试覆盖

test_skill_system 的 6 项测试覆盖：

1. metadata 扫描、正文缓存读取、资源及 skill_list。
2. 无 frontmatter 的 Markdown。
3. 非法 YAML 和重名拒绝。
4. 未知 name、路径逃逸、超大资源和 manifest。
5. catalog 预算与 omitted marker。
6. 首轮仅 catalog，tool call 后才将正文交给模型。

test_workflow_config 验证角色技能与预算合并、required 缺失报错、optional 缺失警告、禁止 tool_names 配置。

test_subagent 验证 child 独立上下文能加载 Skill；test_serial_workflow 验证 PM required 正文和 Engineer/QA optional 目录，以及各角色都有工具。

测试未证明真实模型每次正确选 Skill、按流程执行，或 Skill 引入后交付质量必然提高。需用相同任务启用/关闭 Skill 对比质量、成功率和 token/延迟。

## 23. 设计考量

### 为什么 Registry 没有 Manager/Store 分层

当前 Skill 是用户导入的只读文件集合，没有模型写入、状态迁移或版本管理。Registry 已覆盖发现/读取/缓存，额外 Store/Manager 尚未带来明显复杂度收益。

### 为什么默认仅注入 catalog

完整技能包可能很长且包含无关流程。让模型先发现方法、再加载必要正文和 reference，可控制 prompt 成本并降低干扰。

### 为什么仍支持 required

固定角色在特定业务中常有确定方法，例如 PM 必须用 PRD 模板。Harness 直接注入避免模型漏加载，但 required 数量/长度过多会持续占窗口和增加成本。

### 为什么 optional 不做硬 allowlist

当前角色权限边界由工具 profile 定义，Skill 只是方法提示。若未来需要限制某角色可见知识，应增加显式 Skill scope，而非误用 optional 名单。

### 为什么没有自动执行 scripts

脚本运行涉及依赖、工作目录、输出、超时和权限。复用已有可靠执行工具更符合当前 Harness；单独 Skill runtime 应由实际兼容需求推动。

## 24. 当前限制与改进方向

1. 不递归注册子 Skill，不支持自动 bundle 展开。
2. 启动缓存全部 manifest，不是磁盘 lazy loading。
3. 没有 watcher/refresh 工具，已构造 catalog section 不自动刷新。
4. 无 loaded-state、依赖解析、自动重新加载或重复加载去重。
5. resources 无数量/类型过滤；大型 clone 目录可能产生冗长清单。
6. binary、PDF、图片不解析，超长文件不分页。
7. required 多技能正文缺乏整体预算控制。
8. optional 是提示，不是 visibility/access allowlist。
9. resolved Skill root 的 symlink 可能指向项目外。
10. 未对资源套一般 sensitive-path/secret 策略。
11. 元数据未知键不自动扩展工具和执行能力。
12. 无版本锁、签名、安装器、升级或执行 runtime。
13. 原始 Skill 改动不进入 Workflow 的版本快照锁定，复现实验应记录所用技能内容/版本。

优先改进可从前缀/布局兼容、资源过滤、整体预算和可复现版本记录入手。脚本执行和复杂包管理需要明确业务需求再设计。

## 25. 阅读顺序与面试追问

阅读顺序：Metadata/Document -> refresh/parser -> catalog -> read_resource -> SkillTools -> main 装配 -> WorkflowConfig -> required/optional sections -> Subagent -> 测试。

### Skill 和工具有什么区别？

Skill 提供执行方法和领域知识；Tool 提供实际动作。Skill 不会因为提到 bash/MCP 就让角色获得对应工具。

### lazy loading 到底发生在哪里？

发生在模型上下文披露。Registry 启动已读正文，默认请求仅带 metadata，skill_load 才把正文作为工具结果交给模型。

### 能直接 clone 任意 Skill 仓库吗？

先检查 manifest 是否处于直接子目录，名称/描述合法、UTF-8 且未超限。子 Skill bundle 需要调整布局；脚本、依赖和平台工具需求还需适配。

### required Skill 一定被执行了吗？

只能保证正文被预注入，不能证明模型遵循全部步骤。真实输出仍由 Task/Artifact/verification Gate 验收。

### optional 列表限制哪些 Skill 可用吗？

不限制。角色看到全局 catalog，名单是优先提示。可用动作仍由工具权限控制。

### 压缩后 Skill 会不会消失？

catalog/required 是 System Prompt；optional 正文可能按工具结果被压缩。模型可重新加载，Registry 不自动恢复已加载正文。

### Skill 脚本是否安全？

读取和执行分离，执行走既有权限链，但 imported text 仍需审查，Skill root/sensitive-path 也有当前边界。不能只凭 precedence policy 宣称完全隔离。

### 如何说明它提升了角色能力？

用同任务、有无 Skill 的受控比较，评估 PRD 可实施性、需求覆盖、缺陷发现、Workflow 成功率与成本。成功加载日志只证明方法已可见，不证明质量提升。
