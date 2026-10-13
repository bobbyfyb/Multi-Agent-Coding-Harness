# Workflow 配置、持久化与 Resume 源码精读

> 配置：[workflow_config.py](../../src/llm_agent/workflow_config.py)  
> 状态：[workflow_store.py](../../src/llm_agent/workflow_store.py)  
> 工作记忆：[workflow_checkpoint.py](../../src/llm_agent/workflow_checkpoint.py)  
> 恢复调用：[serial_workflow.py](../../src/llm_agent/serial_workflow.py)、[main.py](../../main.py)  
> 配套：[编排](12_serial_workflow.md)、[Evidence Gate](13_workflow_evidence.md)  
> 测试：[test_workflow_config.py](../../tests/test_workflow_config.py)、[test_workflow_store.py](../../tests/test_workflow_store.py)、[test_workflow_checkpoint.py](../../tests/test_workflow_checkpoint.py)、[test_serial_workflow.py](../../tests/test_serial_workflow.py)

## 1. 两个问题与三个组件

WorkflowConfig 管理运行参数；WorkflowStore 保存阶段控制状态；AttemptCheckpoint 把任务工作经验压成可注入模型的有界结构。

```text
workflow.yaml -> role_specs + budgets + isolation
run.json      -> phase/checkpoint/attempt/worktree identifiers
Task/Artifact/Worktree -> actual persisted work
AttemptCheckpoint -> bounded continuation context
resume(id) -> load -> reuse completed phases -> continue unfinished work
```

resume 不是继续原 Python 栈，也不恢复旧 SDK 请求。它根据磁盘状态重新执行固定流程，跳过可复用阶段，建立新的角色 Agent。

## 2. WorkflowConfig

frozen dataclass：role_specs、isolation、max_fix_cycles、max_phase_retries、max_context_tokens、warnings。

默认 worktree，fix=1，phase retry=1，context=100000。role_specs 来源 ROLE_SPECS，dict/内部对象不是深度不可变。

load_workflow_config：

1. 解析默认 `.llm_agent/workflow.yaml` 或显式 config_path。
2. 文件不存在用 defaults；空 YAML/null 当空配置。
3. yaml.safe_load，根必须 mapping。
4. 顶层仅 version/workflow/roles，version 目前为 1。
5. 校验预算和 isolation。
6. 应用角色覆盖，返回 warnings。

## 3. 可配置内容

```yaml
version: 1
workflow:
  isolation: worktree
  max_fix_cycles: 1
  max_phase_retries: 1
  max_context_tokens: 100000
roles:
  engineer:
    max_steps: 24
    optional_skills: []
  pm:
    required_skills: []
```

角色只允许 required_skills、optional_skills、max_steps，不能通过 YAML 改 tool_names 或任意 instructions。角色名必须存在于 base roles。

_positive_int/_non_negative_int 拒绝 bool 和非法数值；技能必须 list of nonempty strings。

required 缺失且提供 Registry 时报错；optional 缺失过滤并 warning。未配置角色使用代码基线。

当前 workflow 子 mapping 没有独立未知键检查，拼错 budget key 可能被忽略并用默认值。version 使用值相等判断，严格类型检查比预算字段弱。

## 4. 配置与复现边界

WorkflowRunRecord 没有保存完整 WorkflowConfig、角色 Prompt、模型配置或 Skill 内容 hash。resume 使用当前进程的 Workflow 实例配置。

重启后改 isolation、max_fix_cycles、role budgets 或 Skill 文件可能改变恢复行为。要做可复现评测，应额外保留代码版本、配置、模型和 Skill 版本。

尤其 record.fix_cycles 已经存在时，_execute_record 仍从初始 QA 和局部 cycle=0 重走已持久化阶段；改变 max_fix_cycles 可能改变遍历后续修复阶段的范围。

## 5. WorkflowRunRecord

| 字段 | 意义 |
| --- | --- |
| workflow_id / request | 运行身份与原任务 |
| status | running/completed/failed/interrupted |
| current_phase/current_phase_key | 当前执行位置标记 |
| phases | 每个 phase_key 的结果 |
| checkpoints | PhaseCheckpoint 字典 |
| initial_artifact_ids | 启动前已有交付物，不作为本次新增 |
| artifact_ids | 最终收集的交付物 |
| worktree_id/base_commit | 隔离成果位置标识 |
| qa_verdict/fix_cycles | 验证结论和修复进度 |
| created_at/updated_at/error | 生命周期和诊断 |

record 不保存 Worktree path，实际路径从 WorktreeManager metadata 查；不保存全部 Agent messages。

from_dict 主要转换字段，没有完整 run schema 版本、status 枚举验证或跨字段一致性检查。信任前提是 Harness 管理文件。

## 6. WorkflowPhaseCheckpoint

字段：phase_key、phase、role、status、before_versions、started_at/completed_at、attempts、run_ids、artifact_ids、error、data。

before_versions 保留阶段最初的 Artifact baseline；data 是滚动 state、verification、Gate 和 handoff 的承载处。

这是阶段控制 checkpoint，与 data 内的 AttemptCheckpoint 不是同一模型。

## 7. WorkflowStore 文件布局

```text
.llm_agent/workflows/<workflow_id>/run.json
.llm_agent/tasks/<workflow_id>/task_*.json
.llm_agent/artifacts/default/artifact_*.json
.llm_agent/worktree-state/wt_*.json
.llm_agent/worktrees/wt_ID/...
```

run.json 用临时文件 replace 保存；save_run 每次更新 updated_at。

create_run 拒绝已经存在的 run ID。load_run 包装非法 JSON/非 object 为 WorkflowStoreError；list_runs 跳过捕获到的 WorkflowStoreError，按更新时间逆序。

from_dict 的其他 KeyError/类型错误不一定被 list_runs 隔离。无文件锁或跨目录事务，Task/Artifact/Worktree/run 状态可能在崩溃时处于不同提交点。

## 8. Store 生命周期方法

| 方法 | 状态变化 |
| --- | --- |
| mark_phase_started | record running，设置 current phase；checkpoint running |
| mark_phase_completed | checkpoint completed，upsert phase result，清 current/error |
| mark_phase_failed | checkpoint failed，写错误和 phase result，清 current |
| mark_finished | 设置最终 status/artifacts/verdict/cycles，清 current |

started 复用 checkpoint 时保留其 before_versions 和 data，但清 attempts/run_ids/artifact_ids 等控制字段。_run_phase 已在调用前计算 previous_attempts，且 data.current_attempt/handoffs 保留恢复依据。

mark_phase_completed 要求 result 和 data 都声明 agent_status=completed，否则拒绝落盘成功。这是防止 max_steps 被误记为阶段完成的最后保护。

_upsert_phase_result 按 phase_key 更新一个位置，不为每个 attempt 追加 phase。完整 attempt 信息在 handoffs 和 Trace 中。

## 9. 滚动 attempt state

Agent 的 phase_on_event 先捕获状态并保存，再捕获 verification，再转发 CLI callback。

_capture_attempt_event 保留：工具计数、最近 action 12、失败 6、progress 4、Artifact snapshot 12、最后 mutation 等。

滚动内容存在 checkpoint.data.current_attempt_state。即使没有最终总结，也可根据最近事实构建中断 handoff。

事件保存发生在工具动作之后。进程在工具副作用与保存之间中断，状态可能不完整；恢复不保证该工具永不重复。

## 10. AttemptCheckpoint 数据模型

`@dataclass(slots=True)`，字段包括：

- phase_key、attempt、run_id、agent_status、continuation_reason。
- objective、summary。
- completed、remaining、next_actions、decisions、blockers。
- changed_files、open_tasks、verification。
- tool_counts、recent_actions、recent_failures、artifact_ids、worktree_diff_sha256。

to_dict 附 schema_version=1；from_dict 检查版本后重新调用 builder 规范化和限长。

## 11. build_attempt_checkpoint() 的输入与限长

| 内容 | 默认上限 |
| --- | ---: |
| objective / summary | 各 2000 字符 |
| 每组 semantic list | 8 项，每项 600 字符 |
| changed_files | 40 项，每路径 500 字符 |
| open_tasks | 20 项，description 800 字符 |
| verification | 最近 12 项 |
| tool_counts | 32 项 |
| recent_actions | 最近 12 项 |
| recent_failures | 最近 6 项 |
| artifact IDs | 24 项 |

嵌套 JSON 另外限制 24 fields、20 items、深度 3、字符串 1000；过深内容用 bounded marker，非有限 float 转文本，部分数字 clamp。

这是局部内容/数量上限，不是整个 checkpoint 固定 tokenizer budget。原 phase_data.verification 仍可持续增长，不被这些上限直接截断。

## 12. deterministic semantic 提取

_build_semantic_lists：

1. 先取显式 semantic 字段及 aliases。
2. 缺失字段从 summary 的 Completed/Remaining/Next actions/Decisions/Blockers 英文标题解析。
3. 无标题的完整 summary：completed status 放 completed；未完成放 remaining。
4. 未完成又无可用 summary：objective 作为 remaining/next_actions 降级。
5. blockers 从 recent failures 提取，必要时用 continuation_reason。
6. 未完成且 next_actions 空：使用 remaining。

Parser 支持 Markdown 标题、冒号内联和列表前缀；当前 aliases 不含中文标题。语义正确性取决于角色总结，确定性 parser 不证明内容真实。

没有额外 LLM summary call，失败恢复不会为了总结再依赖一条模型请求。

## 13. _build_attempt_handoff()

在 attempt 结束/异常时汇总：

- 本 attempt verification。
- 真实 Worktree snapshot。
- 当前 open Tasks。
- role summary、Gate issue、Artifact IDs 和捕获事件。

调用 builder；Gate issue 会显式进入 remaining/next_actions，确保下一次知道准确阻碍。

handoff 外层还保留 progress、Artifact snapshots 和一些原始有界事件。_append_attempt_handoff 留最近 4 个，更新 continuation_checkpoint，清 current_attempt_state。

## 14. 中断恢复的精确路径

_interrupted_attempt_handoff 检查 current_attempt_state/current_attempt，并要求至少有 tool_counts/actions/failures/progress 才构建中断信息。

当前 _run_phase 仅在没有已有 handoffs 时从滚动状态重建 interrupted handoff。如果前面已有结束 handoff、后来一个 attempt 中途被打断，最新滚动状态可能没有优先成为下一次注入的 handoff。这是当前恢复覆盖的边界。

recent Artifact/verification 也有别的 phase data 来源，但不能因此宣称任意中断点都能完整恢复最新工作经验。

## 15. continuation prompt 的组成

新 attempt 总是先拿阶段 prompt + 精确 Task contract。

同阶段 retry 注入：

```text
Issue
<working_state>可读进展与失败检查</working_state>
<attempt_handoff>最近尝试的结构化数据</attempt_handoff>
<attempt_checkpoint>有界语义工作状态</attempt_checkpoint>
```

这些块部分重复，当前优先让模型看到明确继续点，尚未合并成完全去重的单一注入。

同角色进入另一个 phase 时可注入 prior_role_checkpoint，例如 Engineer 从 implement 进入 fix。当前文件和 fresh evidence 仍高于 checkpoint 中的历史描述。

## 16. resume() 三条路径

### 16.1 完成 run 的快速返回

record completed 且 _completed_record_is_trusted 检查所有预期阶段有一致 completed result/checkpoint 和 agent_status 时，直接 record_to_result，不调用 LLM。

它会获取 Worktree review，但不会重跑所有 Gate、测试或验证报告新版本；不能当作当前工作区重新验收。

### 16.2 已完成阶段的快速复用

_ensure_phase 若存在 result 且 _phase_result_is_trusted 满足相同标记条件，直接返回阶段结果，无须 Gate。

trusted 在这里表示持久化完成标记一致，不是 cryptographic trust 或实时业务正确性。

### 16.3 checkpoint 恢复或重跑

没有可直接复用 result 时，存在 checkpoint 则运行 Gate。只有 Gate ok、checkpoint.status=completed 且 agent_status=completed 才恢复 completed phase。

running/failed checkpoint 即使 Artifact Gate 已满足，也会重新执行角色，避免把尚未完成的 Agent 自动接受为成功。

若仍不能恢复，_run_phase 创建新 Agent/新 messages，沿已有 Worktree/Task/Artifact 继续。

## 17. 修正旧的错误 completed 记录

record 声称 completed 但持久化阶段不满足 agent_status 条件时，_invalidate_untrusted_phase_suffix：

1. 找到第一处不可信/缺失阶段。
2. 保留前缀 phases，去掉后续结果和部分 checkpoints。
3. 保留问题阶段工作 state，改 running。
4. record 改 interrupted，保存位置。
5. 再进入正常 execute_record。

它不回滚 Task、Artifact、代码或所有后续副作用。若对应 Task 已终态，恢复必须在现有状态上补足契约，不能假设所有数据都已回退。

## 18. Worktree 恢复约束

PM 已完成阶段可复用后，_ensure_execution_workdir 加载同 worktree_id。

missing、已 applied、run 归属或 base 不匹配会让 setup 失败，不静默生成一个新目录丢弃已有成果。

若 completed run 走快速返回，Worktree review 错误可能作为 worktree.error 返回；此路径没有 _finish 的成功降级逻辑。因此完成标记不等于当前 Worktree 仍完整可用。

## 19. 使用方式

```text
/workflow 实现指定需求并补充测试
/workflow-list
/workflow-show <id>
/workflow-resume <id>
```

Python：

```python
result = workflow.run("Add a health endpoint and tests.", run_id="demo-health")
record = workflow.workflow_store.load_run("demo-health")
resumed = workflow.resume("demo-health")
```

相同 ID 不能再调用 run 创建；已有运行应 resume。失败修复配置/网络后仍应复用原工作区，不手动删除其 state。

## 20. 设计、测试与边界

持久化控制状态让流程可追踪；bounded checkpoint 降低新 Agent 重复调查；共享真实工作成果避免用摘要替代代码。三者一起才构成恢复能力。

测试分别覆盖：配置合并/非法值、run/phase 持久化、Agent 非 completed 拒绝保存成功、checkpoint 限长/解析/roundtrip、completed 无 LLM、running 重跑、provider failure resume、滚动中断 state 和 Worktree 丢失。

保留限制：无全配置/模型/Skill 快照；无全局 attempt/deadline cap；无事务和并发 resume 锁；不 replay 原对话；无 exactly-once；快速 completed reuse 不重验实时文件；部分 checkpoint 字段重复或 unbounded 原 phase lists；异常并非都能落盘为 failed。

## 21. 面试问题

Q：resume 从 current_phase_key 直接跳转吗？

当前重新运行固定 execute_record，用 ensure_phase 跳过已完成阶段，current 字段主要用于状态展示和恢复记录。

Q：保存 JSON 足够支持恢复吗？

还要保留对应 Task、Artifact、Worktree、验证与角色 checkpoint。run.json 是引用和控制状态，不是全部成果。

Q：为什么不复用旧 messages？

旧工具结果冗长且包含失败路径；新 Agent 接收 bounded state 和真实文件，能控制上下文并避免错误累积，但信息可能不完整。

Q：恢复会重复执行工具吗？

可能。状态保存与外部副作用不是同一事务，LLM 也可能重新选择行动。当前靠 checkpoint 和 fresh evidence 降低重复，没有 exactly-once 保证。

Q：已完成阶段会重新验收吗？

通常直接复用完成标记；只有特定 checkpoint 恢复路径重跑 Gate。需要实时复核应另行运行验证，不能把 resume 当重新测试。

Q：复现能力如何继续完善？

优先记录代码/config/model/Skill 版本与环境，明确恢复策略和最终验证；并行恢复时再补锁、原子状态推进和幂等动作协议。
