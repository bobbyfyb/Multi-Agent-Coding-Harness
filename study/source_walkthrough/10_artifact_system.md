# Artifact System 源码精读

> 核心源码：[artifact_system.py](../../src/llm_agent/artifact_system.py)  
> 工具接口：[artifact_tools.py](../../src/llm_agent/tools/artifact_tools.py)  
> 上下文接入：[artifact_hooks.py](../../src/llm_agent/hooks/artifact_hooks.py)、[context_manager.py](../../src/llm_agent/context_manager.py)  
> 编排与证据：[serial_workflow.py](../../src/llm_agent/serial_workflow.py)、[workflow_store.py](../../src/llm_agent/workflow_store.py)、[workflow_checkpoint.py](../../src/llm_agent/workflow_checkpoint.py)  
> 测试：[test_artifact_system.py](../../tests/test_artifact_system.py)、[test_serial_workflow.py](../../tests/test_serial_workflow.py)

## 1. 模块目标

Artifact 是持久化的角色交付物。PM 的 PRD、Engineer 的实施说明、QA 的测试结论不能只留在某个 Agent 的聊天回复中，否则下一角色难以准确读取，进程恢复也没有明确交接对象。

当前 Artifact 把交付物建模成带 ID、kind、version、status 和 metadata 的文档，使角色能通过统一工具创建、读取和更新，Orchestrator 能引用并检查它们。

它与其他系统的分工：

| 系统 | 保存什么 |
| --- | --- |
| Task | 执行目标、依赖、owner 和工作状态 |
| Artifact | 需求、交接契约、实施说明、测试和验收报告 |
| Worktree | 实际代码、配置和 Git diff |
| Verification | 实际运行的测试/lint 结果 |
| WorkflowStore | phase、attempt、Gate 和恢复 checkpoint |
| Memory | 从可靠交付中提炼的长期知识 |

Artifact 可以承载协作协议，但当前并不是 mailbox、消息路由器或自动任务调度器。串行 Workflow 负责调用角色和推进阶段。

## 2. 源码地图与整体链路

| 符号 | 作用 |
| --- | --- |
| `ArtifactKind` / `ArtifactStatus` | 支持的交付物类型与状态 |
| `ARTIFACT_POLICY_INSTRUCTIONS` | System Prompt 中的交付物规则 |
| `Artifact` | 可变 dataclass 数据模型 |
| `ArtifactStore` | JSON、collection、ID 和单文件保存 |
| `ArtifactManager` | 创建、过滤、版本检查和更新 |
| `ArtifactTools` | 四个模型工具及返回格式 |
| `ArtifactContextHook` | 按任务/workflow 排序，提供预览 |
| `format_artifact*()` | 完整展示、列表和 runtime context |
| `_record_artifact_trace()` | 创建/更新的 domain Trace |
| Workflow `_gate_*()` | 本阶段交付和机器证据校验 |

```text
Role Agent
   -> artifact tool call
   -> ToolRegistry
   -> ArtifactTools
   -> ArtifactManager
   -> ArtifactStore JSON + domain Trace
   -> successful tool_result
   -> Orchestrator captures artifact version + Worktree diff
   -> phase Gate
   -> next role reads artifact preview / artifact_get
```

## 3. Artifact 类型

```python
ArtifactKind = Literal[
    "prd", "task_spec", "implementation_report",
    "test_report", "acceptance_report", "note",
]
```

| Kind | 当前用途 |
| --- | --- |
| prd | 用户需求、边界和验收目标 |
| task_spec | 可执行交接契约及角色 Task ID 映射 |
| implementation_report | 改动、验证、剩余风险和结果声明 |
| test_report | QA 检查、缺陷和 verdict |
| acceptance_report | PM 最终验收及依据 |
| note | 其他重要说明 |

类型校验只限制枚举，不强制正文必须包含某组 Markdown 标题。一个 kind=prd 的文档可以正文为空；Workflow 也没有完整 PRD 内容 schema。

实际机器检查字段主要位于 metadata，正文承担角色理解和人工审查。

## 4. 状态的实际语义

```python
ArtifactStatus = Literal["draft", "ready", "accepted", "superseded", "archived"]
```

| Status | 建议用途 |
| --- | --- |
| draft | 工作草稿 |
| ready | 可交给下一角色 |
| accepted | 验收接受 |
| superseded | 被新版或另一份交付物取代 |
| archived | 默认列表和 Hook 不再展示 |

ArtifactManager 校验状态是否合法，但没有像 Task 那样的转换表。可以直接创建 accepted，或从 accepted 更新为 draft；也没有自动把旧同类文档标 superseded。

这些状态是数据标签，具体约束来自上层 Gate。例如 acceptance_report 的 pass 必须 accepted，fail 必须 ready。

## 5. `Artifact` 数据模型

| 字段 | 含义 |
| --- | --- |
| id | collection 内稳定 ID，如 artifact_0001 |
| kind | 支持的六种类型 |
| title | 非空标题 |
| content | Markdown 正文 |
| status | 默认 draft |
| task_id | 可选关联 Task |
| owner | 可选作者/角色归属 |
| version | 初始 1，每次 update 增加 |
| created_at / updated_at | UTC ISO 时间，精确到秒 |
| metadata | Workflow id、verdict、changed_files 等 |
| history | created/updated 变更摘要 |

to_dict 使用 asdict；from_dict 校验 kind/status，转换 version 和 metadata，规范化 history。

`_normalize_history()` 只保留 list 中的 dict，不验证每条历史记录的完整 schema。

### 5.1 owner 与 metadata.role

owner 是一等字段，可列表过滤；metadata.role 是 Workflow Prompt 建议提供的角色标签。它们没有自动同步，也不等同于真实调用者身份。

ArtifactTools 没有 requires_context，因此不自动填入 run_id/agent_id。Workflow id、role、task_id 主要由模型或可信 Harness 提供；上层再检查相应契约。

### 5.2 task_id 的边界

Manager 不加载 Task 来验证 task_id，也没有保存 task_list_id。不同 Task 列表可存在相同 task_0001，因此需要结合 metadata.workflow_id 或上层作用域解释引用。

## 6. `ArtifactStore`

默认路径：

```text
.llm_agent/artifacts/default/
  .highwatermark
  artifact_0001.json
  artifact_0002.json
```

也可通过 collection_id 指定另一 collection。不同 collection 的 ID 可重复。

当前 Main Agent 和 Workflow 通常共享根项目 ArtifactManager 的 default collection；Workflow 通过 metadata 和初始 ID 集合区分交付物，而不是每个角色单独存一套文件。

### 6.1 ID 分配

next_id 读取 .highwatermark，递增写回，生成 artifact_0001。分配发生在全部字段校验前，失败创建可留下编号空洞。

marker 没有锁或原子事务。并发分配可能冲突；删除 marker 后没有自动扫描文件重建计数。

### 6.2 保存与读取

save 先写同 ID 的 .tmp，再 replace 到 JSON。load 检查路径存在、JSON object，再还原 Artifact。list 扫描 artifact_*.json。

这是单文件替换，不提供多写者锁、fsync 持久化承诺或多文件事务。固定 tmp 名并发时也可能竞争。

单个 JSON 损坏不会被 list 隔离，可能让 Hook 或 Workflow 整体读取失败。

### 6.3 路径保护

artifact id 拒绝斜线、反斜线、点开头，并要求 artifact_ 前缀。

collection_id 当前没有对应的安全校验，它由可信 Python 装配提供，模型工具未开放。不能将任意用户提供的 collection_id 直接当作安全的 namespace。

## 7. `create_artifact()`

执行过程：

1. 获取当前时间和新 ID。
2. 校验 kind、title 和 status。
3. content 转字符串；task_id/owner strip，空文本转 None。
4. 复制 metadata。
5. version=1，写 created history。
6. 保存 JSON。
7. 发出 artifact.created Trace。

它不去重标题、不检查正文非空、不验证 kind 专属 metadata，也没有像 MemoryManager 的 SECRET_PATTERN 存储过滤。

ARTIFACT_POLICY_INSTRUCTIONS 要求模型不存 secret，这是 Prompt 约束，不是硬性内容扫描。

## 8. `list_artifacts()`

支持 kind、status、task_id、owner、include_archived 和 limit。

排序按 updated_at、created_at、id 逆序，优先展示最近文档。时间精度是秒，id 是同时间下的补充排序条件，不是全局更新时间序列。

默认排除 archived；显式 status 时优先 status 过滤，所以 `status="archived"` 即使 include_archived=False 也可查到归档文档。

superseded 不在默认排除集合中，仍会进入列表和 Hook。

Manager 默认 limit=None，Tools 默认 limit=20；工具 schema 的 limit 范围是 1 至 100，Manager 只检查大于零。

## 9. `update_artifact()` 与版本检查

简化链路：

```python
artifact = store.load(id)
if expected_version is not None and artifact.version != expected_version:
    raise ArtifactSystemError("Artifact version conflict ...")

apply_provided_fields()
artifact.version += 1
artifact.history.append(change_record)
store.save(artifact)
record_trace(...)
```

### 9.1 expected_version 的准确能力

如果模型读到 v1，但另一顺序操作已经保存 v2，update(expected_version=1) 会被拒绝。

但 expected_version 在 Manager 和 tool schema 都是可选的；Prompt 只是推荐读取并提供。省略后不会检查旧版本。

即使提供版本，两并发请求也可能同时读取 v1 并通过检查，随后都写 v2。当前实现是读时版本检查，不是带锁或数据库 CAS 的原子乐观并发控制。

### 9.2 字段更新规则

- title 校验非空。
- content 可设为空字符串。
- kind 不能通过 update 改变。
- metadata 使用 update 合并，不删除未提供键。
- None 表示不修改；task_id/owner 提供空字符串可清成 None。
- 不提供任何可更新字段会报错。
- 只提供 change_summary 不算更新。

provided fields 与实际内容变化不等价。即使 content 与旧值相同，或 metadata={}，也会记字段并增加版本。

## 10. `history` 能做什么

初始记录有 action、version、timestamp、summary；更新记录额外保存 fields。

```json
{
  "action": "updated",
  "version": 2,
  "timestamp": "2026-10-13T00:00:00+00:00",
  "fields": ["content", "metadata"],
  "summary": "Clarified the API contract."
}
```

history 不包含旧 content、字段 diff 或旧 metadata。因此能回答“何时改过哪些字段、说明是什么”，不能恢复 v1 正文或执行版本 rollback。

history 没有条数上限，每次更新重写包含全部记录的 JSON。完整版本快照目前没有实现。

## 11. `ArtifactTools` 四个工具

| 工具 | 功能 | 输出 |
| --- | --- | --- |
| artifact_create | 创建单条交付物 | ID/version/status/metadata，不重复返回正文 |
| artifact_get | 获取当前完整正文 | 完整正文加展示字段 |
| artifact_list | 筛选列表 | 紧凑摘要 |
| artifact_update | 更新并可检查版本 | 新 version/status/metadata，不重复返回正文 |

create/update 返回“Content stored. Use artifact_get ...”，避免刚提交的长正文在 tool result 中再次回灌。

注意 format_artifact 的完整展示也不包含 history。需要在 Python 中 get_artifact().history 或查看文件才能读变更记录；模型工具尚无 artifact_history。

### 11.1 Tool schema 与注册

create 要求 object 中有 kind/title/content，一次创建一条。update 要求 artifact_id，expected_version 最小为 1，但不在 required 中。

register_tools 可传共享 manager；未传则按 workdir/collection 创建。默认工具 registry 和 Workflow role allowlist 提供这些工具。

错误通过 ToolRegistry 包装为 ok=False/error，模型可根据 version conflict 重新 get 再更新。

## 12. `ArtifactContextHook` 选择策略

默认 max_artifacts=5，每条正文 preview 最多 3000 字符。

每次 BeforeLLM：

1. TaskManager 读取当前列表的 open task IDs。
2. ArtifactManager 读取非 archived 文档。
3. 若配置 workflow_id，保留同 Workflow 和无 workflow metadata 的文档。
4. 排序，取前 max_artifacts。
5. format_artifact_context 生成 relevant_artifacts。
6. 返回 runtime messages 与 artifact_ids。

rank 是互斥优先级：

| 条件 | rank |
| --- | ---: |
| task_id 命中 open task | 3 |
| kind 是 prd/task_spec | 2 |
| status 是 ready/accepted | 1 |
| 其他 | 0 |

同 rank 再按时间/id 逆序。draft report 关联 open task 时可以排在 ready PRD 前；superseded PRD 也可能 rank=2。

这是规则排序，没有 query 语义检索或 LLM selector。Main Agent 无 workflow_id 过滤，会看到跨 Workflow 的候选；无 metadata 的 Artifact 被当作可共享文档。

## 13. 预览与 ContextManager 协作

format_artifact_context 包含 ID、kind、version、status、title、task、owner、updated 和 body preview，不包含完整 metadata。

因此模型要准确读取 verdict、change_required、task_ids 等字段，应调用 artifact_get；只看 preview 不一定能看到机器契约。

正文超限时附 omitted chars 和 artifact_get 提示。这里限制的是正文字符，不是整个 context 的 token；标题等展示字段也可能增加长度。

Agent 将 Hook runtime context 插入 request_messages 副本，不永久追加 canonical messages。历史压缩后下一步重新读取文件，交付物仍存在。

相同内容可避免重复打印 artifact_context，但每步请求仍会发送预览。不会因此自动获得增量 token 注入。

## 14. Workflow 交付契约

| 阶段 | 必需 Artifact | 关键 metadata/状态 |
| --- | --- | --- |
| PM planning | prd + task_spec | ready/accepted；task_spec.task_ids；Worktree 模式的 change_required 布尔值 |
| Engineer | implementation_report | outcome=changed/no_change/blocked；changed_files；可选 no_change_reason |
| QA | test_report | verdict=pass/fail，Harness 可规范化 effective_verdict |
| PM acceptance | acceptance_report | verdict 匹配权威 QA；pass=accepted，fail=ready |

Prompt 要求设置 workflow_id/role。但通用 Manager 不校验这些约定，Gate 也不是对全部 kind/role/body 的完整 schema 校验。

implementation/test report 的 `_gate_required_artifacts()` 使用 statuses=None，因此底层必需性检查不限定 ready，甚至不会直接排除 archived。其他证据检查决定是否通过。这与“所有报告必须 ready”不同。

## 15. 本阶段交付识别

Workflow 开始记 initial_artifact_ids；phase 开始记 before_versions。

_changed_workflow_artifacts 条件：

1. Artifact 不属于 workflow 启动前的 initial IDs。
2. metadata.workflow_id 为当前 id，或没有该 metadata。
3. phase baseline 中不存在，或当前 version 与 baseline 不同。

因此旧阶段已有报告可以通过 update 新版作为本阶段交付，不能仅靠一份未变化的旧报告过关。

初始阶段之前就存在的 Artifact 即使更新，也因 initial IDs 被排除。没有 workflow metadata 的新文档可以被接纳，这是共享兼容边界，未来并行编排需要收紧归属。

同 kind 多个候选时选时间/id 排序的最新一个，不自动选内容最充分或版本最大的文档。

## 16. 写入快照如何捕获

_capture_attempt_event 在 tool_call 中记录 pending artifact_create/update，成功 tool_result 后：

1. update 从参数获取 artifact_id。
2. create 从工具结果展示文本解析 ID。
3. Manager 重读当前 Artifact 的 kind/version。
4. Worktree 模式收集当前 diff_sha256。
5. 保存 artifact_id、version、run_id、step、diff hash。

最近 12 个 snapshots 保留在 attempt state，再进入 handoff/checkpoint。

_latest_artifact_snapshot 从当前 attempt、历史 handoff 和 Orchestrator normalized snapshot 选择该 ID 的最新版本快照。

它绑定的是“报告写入时对应哪份代码状态”，不保存报告旧正文。create ID 解析依赖当前 Tool 输出格式，是一处明确的文本耦合。

## 17. Engineer 报告与真实代码证据

Worktree 模式中 _gate_implementation_report 检查：

- required report 本阶段创建/更新。
- TaskSpec change_required 是布尔值。
- 存在 phase Worktree baseline。
- outcome 必须 changed/no_change/blocked。
- blocked 直接失败。
- changed 必须产生本阶段 diff 变化，并且有实际 changed files。
- changed 必须至少有 passed/clean 验证绑定最终 diff。
- no_change 只允许 change_required=False，diff 未变化并提供原因。
- 报告 snapshot 存在，version 和最终 diff 与当前报告一致。
- phase Task owner/status/evidence 正确。

所以“只写 ImplementationReport，没有改代码”不能满足要求改代码的 Worktree phase。

### 17.1 metadata 规范化

如果报告的 changed_files 与真实 Worktree 不同，Harness 用真实列表更新 metadata，记录 warning；同时绑定 worktree_diff_sha256。

这次 trusted Manager update 增加 version，因此 Gate 保存 normalized snapshot，避免新版本与原 tool snapshot 不一致。

声明字段不会替代真实 Git diff，但 Markdown 正文不会自动同步重写，正文和 metadata 仍可能需要人工/模型核对。

### 17.2 shared 模式

shared 模式跳过上述 Worktree 证据绑定，主要检查必需报告和 phase Task。不能把 Worktree 模式的强保证推广到所有 isolation 配置。

## 18. QA verdict 与机器证据

Worktree QA Gate 读取本 attempt verification：passed/clean 成功项必须匹配最终 diff；失败、lint issues、error、timeout、tool_error 等另外分类。

主要检查：

1. 报告 verdict 合法。
2. 有本 attempt 的结构化验证记录。
3. 声明 pass 时 QA 未改 tracked project files。
4. 只有工具/权限/environment_setup_failed 阻碍时，要求 QA retry，不直接当 Engineer 缺陷。
5. pass 缺少匹配最终 diff 的成功检查且没有 actionable failure 时不通过。
6. 报告 snapshot version/hash 匹配当前结果。
7. phase Task 已完成。

如果声明 pass 但存在 actionable failure，Harness 将 effective_verdict 降为 fail。QA phase 可以通过“已完成检查并报告失败”的契约，Workflow 再根据 fail 进入修复。

更新的 metadata 包括 reported_verdict、verdict、effective_verdict、verdict_overridden 和 tested_diff_sha256。保持模型原声明，便于复盘。

Prompt 要求最后验证后写报告；Gate 实际主要比对 snapshot/version/hash，没有对报告 step 必须晚于最后一次验证做独立的普遍时间顺序证明。相同 diff 并不能单独证明写作顺序。

## 19. 最终验收

_gate_acceptance_report 要求：

- 本阶段有新建/更新的 acceptance report。
- metadata.verdict 为 pass/fail。
- verdict 与权威 qa_verdict 匹配。
- pass 对应 accepted；fail 对应 ready。
- acceptance Task owner/status/evidence 正确。

PM 不能仅凭正文“我认为通过”覆盖 QA fail。最终报告也是需求说明和审查材料，但当前 Gate 没有逐项自动证明所有 PRD 验收条件已覆盖。

## 20. retry、resume 与长期记忆

phase Gate 不通过时，Workflow 给新 attempt 注入 issue 和 bounded working checkpoint；已有 Artifact 可以 get/read 后 expected_version update，避免重复创建无关报告。

WorkflowStore 保存 before_versions、artifact IDs、snapshots、verification 和 handoffs；resume 重用同 collection、Task list 和 Worktree，重新判定阶段。

完整 Artifact 正文从持久化文件读取，不塞进所有 checkpoint。checkpoint 主要保存引用、状态和证据。

Workflow completed 且 QA pass 后，_format_reflection_artifacts 取 metadata/body 预览给 MemoryManager.reflect_from_workflow，提炼可复用长期事实。Artifact 是本次交付，Memory 是经选择的跨会话知识。

## 21. 一条完整示例

任务：为 API 新增健康检查并补测试。

```text
PM:
  creates PRD artifact_0001 v1 ready
  creates TaskSpec artifact_0002 v1 ready
  TaskSpec metadata.task_ids ->真实 Engineer/QA/Acceptance Tasks
  TaskSpec change_required=true

Engineer:
  reads planning artifacts
  edits code in Worktree (diff H1)
  run_tests -> passed, bound H1
  creates ImplementationReport artifact_0003 v1 (snapshot H1)
  completes phase Task

Harness:
  checks phase-local diff + verification + report snapshot
  normalizes changed_files/hash -> report v2 if needed

QA:
  reads report v2
  run_tests -> passed, bound H1
  creates TestReport artifact_0004 v1, verdict=pass
  completes QA Task

Harness:
  normalizes effective verdict/hash -> v2 if needed

PM acceptance:
  reads reports and authoritative evidence
  creates AcceptanceReport artifact_0005 accepted, verdict=pass
  completes acceptance Task

Workflow completes:
  persists IDs + evidence; may reflect stable facts into Memory
  Worktree remains available for explicit review/apply
```

accepted Artifact 不会自动 merge/apply 代码。文件进入主工作区仍受 Worktree apply 流程控制。

## 22. Python 直接使用

```python
from llm_agent.artifact_system import ArtifactManager

manager = ArtifactManager.for_workdir("/path/to/project")
prd = manager.create_artifact(
    kind="prd",
    title="Health endpoint",
    content="## Goal\nExpose a health endpoint and verify it.",
    status="ready",
    metadata={"workflow_id": "wf-example", "role": "pm"},
)
current = manager.get_artifact(prd.id)
updated = manager.update_artifact(
    current.id,
    content=current.content + "\n## Constraint\nKeep existing routes stable.",
    expected_version=current.version,
    change_summary="Added compatibility constraint.",
)
```

直接 Manager 创建不会产生 Agent tool_result，因此也不会自动进入 Orchestrator 的 tool-write snapshot。需要 Worktree Evidence Gate 的 worker 应通过正常 Artifact tool 写入。

## 23. Trace 与测试

_record_artifact_trace 保存 artifact_id、kind、title、task_id、version、status，创建/更新用 artifact ID 作为 correlation。额外记录 changed_fields/change_summary，不默认记录正文。

工具参数/result 仍由正常 Tool Trace 记录，内容保护取决于 Trace 脱敏配置；Artifact 文件本身不自动脱敏。

test_artifact_system 的 6 项测试覆盖：

1. 创建、更新、过滤、跨 Manager 读取和旧版本冲突。
2. 四个工具的注册与执行。
3. open-task-linked Artifact 优先。
4. 排除其他明确 workflow 的 Artifact。
5. 长正文 preview。
6. 创建/更新的 domain Trace。

Workflow 测试覆盖报告无代码变化、无 verification、验证后再次修改、旧 diff 报告、false pass 降级、环境/权限阻碍 retry、修复及 acceptance 冲突。

这些测试不证明并发更新安全、正文事实正确或 PRD 需求覆盖完整。

## 24. 设计考量与教学参考

### 为什么 Artifact 不放进 Task.description

Task 是短小工作状态。多个 PRD/报告有自己的 kind、版本和验收状态，需要独立 ID 和读取接口；否则 Task 更新会混合工作进度与长交付物。

### 为什么通用 Manager 不包含全部报告 schema

Manager 服务 Main、Workflow 和 notes 等多种场景。角色契约和 Worktree verification 属于 Workflow；通用层保留基础枚举、title 和版本规则，降低相互依赖。

代价是离开 Workflow Gate 后，ready/accepted 文档并不自动拥有强证据保证。

### 为什么先使用持久化交付协议

串行角色可以围绕确定 ID 的文档协作，容易恢复、审计和展示。与 learn-claude-code s15/s16 的 mailbox/message/protocol 示例相比，这里优先解决交付内容及证据引用；尚未实现它们的常驻队友消息总线和 shutdown/plan approval 通信协议。

Artifact 并非直接复制某个单独教学章节，而是结合本项目串行交付和评测问题增加的模块。

### 为什么模型声明与权威证据并存

模型写报告有助于解释改动，但它可能遗漏文件或错判测试。机器 diff/verification 决定可检查事实，reported/effective 字段保留二者差异，方便修复和评测。

## 25. 评测问题与当前应对

| 问题 | 当前应对 | 保留边界 |
| --- | --- | --- |
| 工具输入一次传多个 Artifact 数组 | Prompt 要求单条 object；参数验证/回灌错误 | 模型仍可能生成非法输入 |
| 工程师只写报告没有开发 | phase-local diff + verification Gate | shared 模式较弱 |
| 报告 changed_files 不完整 | authoritative Worktree metadata reconciliation | 正文不自动重写 |
| 验证早于最后修改 | verification hash 要匹配最终 diff | 不证明测试覆盖所有需求 |
| 报告属于旧代码状态 | write snapshot version/hash 校验 | 同 hash 不证明全部时间顺序 |
| QA 声明 pass 但机器失败 | effective_verdict 降为 fail | QA 仍可报告保守 fail |
| 依赖/权限阻碍误路由修复 | 阻碍分类，QA phase retry | 混合阻碍/缺陷仍需进一步判断 |
| PM acceptance 与 QA 不一致 | verdict/status Gate | 业务验收语义仍依赖角色审查 |

## 26. 当前限制与演进方向

- expected_version 可省略，检查与写入非原子。
- 单文件 replace 和 highwatermark 不提供并发安全。
- history 没有旧正文、diff、rollback 或长度限制。
- 无独立 per-kind body schema，content 可为空。
- 无严格状态转换或 owner 身份鉴权。
- metadata 合并不能删除键，旧字段可能残留。
- collection_id 未校验，模型入口当前未开放。
- 存储没有 hard secret 过滤，context 格式也不消除 Prompt Injection。
- 无 workflow metadata 的文档可跨 Workflow 共享/接纳。
- task_id 未包含 list namespace，通用 Hook 可能有同名 Task 关联歧义。
- Hook 规则排序没有 query relevance，superseded 默认仍可选。
- 全部 JSON 读取、损坏文件传播、正文/history 增长会影响规模。
- create snapshot ID 从展示字符串解析，尚无结构化工具 result 契约。
- 真实报告证据依赖 Worktree Workflow，普通 Agent 文档不具备同样 Gate。

当前串行场景优先保持清晰交付契约。并行拓展前应补原子更新与严格作用域；版本回滚和语义 schema 应由实际审查/评测需求驱动。

## 27. 推荐阅读顺序与面试追问

阅读顺序：Artifact -> Store -> Manager create/update/list -> Tools -> ContextHook -> Workflow changed selection -> tool-write snapshot -> implementation/QA/acceptance Gate -> checkpoint/reflection -> tests。

### Artifact 与消息有什么区别？

Artifact 是可反复读取、更新和持久化的交付对象；消息用于通知和通信。当前 Orchestrator 调用下一角色，角色根据 Artifact 读取交付，没有独立 mailbox 路由。

### version 是否防止并发覆盖？

能检测已经提交的新版本，但版本检查可选，也没有原子 CAS。未来真并发需要锁/数据库事务，不能只依靠整数版本字段。

### history 能恢复旧版本吗？

不能。它保存变更说明和字段名，没有旧正文或可逆 diff。

### status=accepted 就证明代码验收了吗？

通用层允许任意合法状态。只有在 Workflow acceptance Gate、QA evidence 和 Worktree 检查都满足时，才能讨论该次 Workflow 的工程验收；apply 仍是另一动作。

### 模型报告和机器证据冲突如何处理？

Worktree 文件列表和验证事件优先。Harness 规范化 metadata，保留 reported/effective verdict、warning 与 normalized snapshot，不简单相信正文。

### 交付物如何支持 resume？

JSON 保存当前正文和 version，WorkflowStore 保存 ID/baseline/snapshot；恢复后重读当前交付并继续 Gate。不会把另一角色整个对话历史直接交给下一个角色。

### 为什么不存向量数据库？

当前主要按 ID、kind、workflow 和 task 精确读取，正文是确定角色交付。向量检索不能替代版本、状态和证据一致性；规模增长时可另加搜索索引。
