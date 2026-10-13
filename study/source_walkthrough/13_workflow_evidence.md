# Workflow Worktree 与 Evidence Gate 源码精读

> 编排入口：[serial_workflow.py](../../src/llm_agent/serial_workflow.py)  
> 隔离与 patch：[worktree.py](../../src/llm_agent/worktree.py)  
> 验证：[verification_tools.py](../../src/llm_agent/tools/verification_tools.py)  
> 报告：[artifact_system.py](../../src/llm_agent/artifact_system.py)  
> 测试：[test_serial_workflow.py](../../tests/test_serial_workflow.py)、[test_worktree.py](../../tests/test_worktree.py)  
> 前后链路：[编排](12_serial_workflow.md)、[持久化与恢复](14_workflow_persistence_and_resume.md)

## 1. 职责

Evidence Gate 将模型交付声明与可检查事实对齐：有 PRD 不代表有完整任务链，有 ImplementationReport 不代表改过代码，有 TestReport pass 不代表测试真通过。

当前 Gate 是 SerialCodingWorkflow 的方法集合，没有独立 EvidenceGate 类。它读取 Task、Artifact、Worktree 和真实工具事件，返回 _GateResult。

## 2. 一个 Workflow 一个 Worktree

PM 规划通过后，_ensure_execution_workdir：

- shared：直接返回根 workdir。
- worktree 且无 ID：Manager.create，保存 worktree_id/base_commit。
- 已有 ID：reconcile/get，检查 missing、applied、run 归属和 base commit。

Engineer、QA、修复、回归和验收共用同一目录；PM planning 在根目录只读执行。

复用允许 info.run_id 为 None 或当前 workflow id，base_commit 必须与 record 相符。没有每次比较主工作区 HEAD 的一致性；apply 时另有 main HEAD 校验。

## 3. WorktreeManager 集成原理

create 要求有效 Git repository 和基本干净的根工作区，忽略 .llm_agent 运行路径。新建 branch=llm-agent/wt_ID，路径=root/.llm_agent/worktrees/wt_ID，base_commit=创建时 HEAD。

Task/Artifact/Skill/Memory 不迁入各角色目录，而通过根 Manager 访问。角色不能通过正常工具直接管理/合并自己的 Worktree。

这里是文件改动隔离，不是 OS 沙箱。允许执行的测试/脚本仍有宿主用户进程权限，详见 Security 精读。

## 4. diff_sha256 是什么

WorktreeManager.diff：

1. git add -A，把可追踪新增/修改/删除纳入 index。
2. reset .llm_agent runtime paths，避免运行产物作为交付。
3. git diff --cached --binary base_commit，排除 .llm_agent。
4. 获取 changed_files。
5. 保存完整 patch，返回 bounded diff preview。
6. 对完整 patch 的 UTF-8 字节计算 SHA-256。

hash 使用完整 patch，不依赖终端预览是否截断。它标识相对 base 的 Git 交付状态，不包含 ignored 文件、依赖环境和外部服务状态。

diff 是带副作用的快照：会 stage 文件、写 patch，可能将 active 标 ready，不只是内存读取。

## 5. 证据数据流

```text
phase begins
  -> worktree_before {base, diff hash, changed files}

run_tests/run_lint tool_result
  -> _capture_verification_event
  -> outcome + attempt/run_id + failures/environment info
  -> current Worktree hash

artifact_create/update successful tool_result
  -> _capture_attempt_event
  -> artifact ID/version + current hash

phase ends
  -> current worktree_after
  -> Gate compares baseline/report/verification/task
  -> normalized evidence into phase checkpoint
```

## 6. _capture_verification_event()

只接受 run_tests/run_lint 的 tool_result，解析两层结构：

```text
ToolRegistry outer: ok/result/error
Verification inner: outcome/exit_code/failures/error_kind/...
```

outer ok=True 仅表示工具调用执行完成；inner outcome 才表示测试 passed/failed、lint clean/issues_found 等。outer ok=False 分类为 tool_error。

保存 attempt、run_id、step、outcome、exit code、timeout、duration、report path、execution_environment、diagnostic、missing_modules、失败测试等，再绑定当前 diff hash。

一个 verification event 在 phase_data.verification 中追加并落盘。正文 bash 输出或模型声称“测试通过”不进入该结构化证明链。

## 7. 报告写入快照

工具调用与结果按 call ID 配对，成功时重读 Artifact 和 Worktree，记录 artifact_id/kind/version/run_id/step/diff hash。

create 的 ID 从返回展示文本解析，update 从参数取。最近 12 个快照进入 attempt state/handoff；_latest_artifact_snapshot 在当前状态、历史 handoff 和 normalized snapshot 中找最新版本。

只有保存 Artifact 文件而没有正常 tool event，并不自动生成这份快照。Harness 后续规范化报告时会自己保存 normalized snapshot。

## 8. 基础交付 Gate

_gate_required_artifacts 要求 Artifact 是当前 Workflow 启动后创建，属于当前 workflow 或没有 workflow_id，并相较 phase before_versions 有创建/版本变化。

按 required kind/status 查找最新匹配文档，缺失返回 issue。启动前的旧 Artifact 即使被更新也被 initial_artifact_ids 排除。

归属默认允许无 workflow metadata；同类型多个文档选时间/id 最新，没有内容完整性评分。

## 9. 规划 Gate

_gate_planning_artifacts：

1. ready/accepted 的 PRD 和 TaskSpec。
2. _gate_planning_tasks 校验实际角色任务链。
3. Worktree 模式要求 TaskSpec.metadata.change_required 是 bool。

Task graph Gate 验证 TaskSpec.task_ids 映射、真实存在/唯一 task_key、workflow/role metadata、parent root、owner、pending 和精确依赖。

这保证基本交接契约，不逐句验证 PRD 商业合理性或需求覆盖。

## 10. 通用阶段 Task Gate

_gate_phase_task 按 workflow/task_key 定位任务，要求 expected owner、completed、非空 evidence。

Task evidence 本身是模型文本，只作状态契约。真实验证来自 verifier events。QA Task completed 可以对应 fail verdict。

## 11. Engineer 实施 Gate

Worktree 模式主要条件：

| outcome | 条件 |
| --- | --- |
| changed | phase baseline 与最终 diff 不同，有 changed files，有成功验证匹配最终 hash |
| no_change | TaskSpec 不要求改动、phase diff 未变、原因非空 |
| blocked | 拒绝通过 |

所有有效交付还要求 report write snapshot 存在、version/hash 与最终状态一致，以及 phase Task 完成。

successful_verification 来自本 phase 的记录，可以跨 attempt 复用匹配最终 hash 的成功检查。Gate 没有要求该 phase 所有历史检查都成功；修复后的成功结果可以提供证明。

report.metadata.changed_files 不准确时以真实 Worktree 列表修正，记录 warning；绑定 worktree_diff_sha256，增加报告版本并保存 normalized snapshot。

这是当前“报告不能代替开发”的最小保证。changed 要求本阶段 diff 变化，但不是语义正确性证明；与任务无关的改动也可能改变 hash。

## 12. 修复阶段的 baseline

一个 phase 的多次 attempt 共享首次 worktree_before；新 engineer_fix_N 有新的 baseline。

所以 implementation retry 可以接续已有代码并只补验证/报告，但新的 fix phase 不能只重新声明上次已有 diff，它需要本阶段真正产生变化并验证。

no_change 分支只看 change_required 等通用条件；该字段允许 verify-only 任务。这是契约条件，不是对所有修复场景无例外强制改代码。

## 13. QA Gate 的结果分类

QA 只取 current_run_id 对应 attempt 的验证，不把其他 attempt 成功当作本次完整验证。

| 类别 | 识别 |
| --- | --- |
| successful | passed/clean 且 hash=最终 diff |
| failed | failed/issues_found/error/timed_out/tool_error |
| blocked_failed | tool_error 或 environment_setup_failed |
| actionable_failed | failed 中排除上述环境/工具阻碍 |

缺记录或只有环境/权限阻碍时，phase Gate 不通过，让 QA retry；不能把没跑起来的测试当代码失败而无限交给 Engineer 改源码。

混合阻碍与 actionable failure 时仍可能进入代码缺陷路由，当前不是独立环境修复 Worker。

## 14. QA pass 的判定与修正

- 模型 verdict 必须 pass/fail。
- 声明 pass 时 QA 不得改变 tracked 交付 diff。
- 必须存在本 attempt 结构化验证。
- 无 actionable failure 时，pass 至少需要成功检查匹配最终 hash。
- report snapshot version/hash 匹配。
- Task contract 满足。

声称 pass 但有 actionable failure 时，Harness 把 effective verdict 降为 fail，保留 reported_verdict、verdict_overridden 等 metadata，返回可完成的 QA phase，再由 Orchestrator 开修复循环。

失败 Task phase 与有效 QA fail 报告不同：前者无法推进；后者可作为缺陷反馈。

当前同 attempt 中曾失败、后来重跑成功，失败记录仍在，可能让 reported pass 被降为 fail。没有用“最后同目标成功覆盖旧失败”的规则。

## 15. 快照不等于严格时序证明

Prompt 要求最终修改 -> 验证 -> 报告。Gate 通过 version/hash 检查证据是否对应同一状态，但没有普遍比较报告 step 必须晚于最后验证 step。

同 diff 的报告即使写作早于验证，hash 本身也不能证明时序。当前保护重点是旧代码状态和旧版本错配。

## 16. PM Acceptance Gate

验收报告要本阶段新建/更新、verdict 合法且匹配权威 QA：pass 要 accepted，fail 要 ready，acceptance Task 已完成。

最终 qa_verdict 不为 pass 时即使验收报告有效，也返回 failed Workflow。报告合法与需求已通过是不同结论。

## 17. shared 模式的保证

shared 跳过 Worktree baseline/hash 和写入快照绑定。仍检查规划交付/Task，Engineer report 与 phase Task，QA verdict/Task，以及 acceptance 一致性。

因此 shared 的 test_report pass 主要是角色声明，没有 worktree 模式同样的机器验证 Gate。该模式适合受控测试或显式选择的工作场景，不能宣称同等隔离/验收强度。

## 18. 最终 patch 与 apply

_finish 调用 _worktree_review -> Manager.diff，收集完整 patch 路径、preview、changed files/hash。成功时 review 失败会降为 failed。

Workflow 不自动 apply。apply 的主 HEAD、git apply --check、人工确认等机制属于 Worktree 工具流程，详见 Security 及后续独立 Worktree 精读。

一个 Workflow 结束后保留 Worktree 供审查，也可能保留失败代码；用户应根据结果和 patch 决定后续操作。

## 19. 真实问题与测试入口

test_serial_workflow 覆盖：

- 报告存在但无 phase-local 代码变化。
- QA pass 没有 verifier event。
- 验证后又修改代码。
- 报告写入对应旧 diff。
- false pass 自动降级并进入修复。
- permission/tool/environment blocker 留在 QA retry。
- fix 产生新 diff，QA 做 regression。
- acceptance 与 QA 冲突。

这些都是 Harness 能判断的可重复事实。它仍不证明验收条件覆盖率或业务质量，必须结合测试质量和人工 review。

## 20. 设计与面试问题

Q：为什么 Gate 在 Orchestrator，而不是靠角色 Prompt？

模型不能可靠自证工具执行和代码状态。Harness 直接持有 Tool events、Worktree 和持久化交付物，可以做确定性检查。

Q：hash 能证明代码正确吗？

不能。hash 只把检查和报告绑定到同一交付状态，正确性仍取决于检查覆盖和需求审查。

Q：QA 为什么不用通用 Bash？

结构化 verifier 提供可靠机器结果，减少任意源码修改和解析终端文本的风险；但允许执行的目标代码本身仍需安全边界。

Q：为什么报告 metadata 可被 Harness 修改？

机器事实应以真实 diff/verification 为准。保留模型声明与规范化字段、warning/snapshot，方便解释冲突。

Q：最重要的限制？

shared 模式较弱；证据 hash 不包含环境；部分 metadata 由模型提供；没有全需求覆盖证明、严格事件时序锁或并发工作区一致性。
