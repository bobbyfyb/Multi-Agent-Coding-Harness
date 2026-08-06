# 自托管评测与故障复盘

## 1. 为什么要做自托管评测

单元测试只能证明 Harness 组件在预设输入下符合预期，不能回答：

- 模型会不会真的修改正确文件；
- 长任务是否会耗尽 step/context budget；
- 角色之间是否重复探索；
- QA 会不会忽略真实失败；
- resume 后能否沿用 Worktree 和进展；
- 最终候选能否通过仓库外的黑盒验收。

因此项目选择一个“让 Agent 给自己的 Harness 开发 API Server”的任务作为 self-hosting benchmark。它同时覆盖：

- 仓库理解和架构复用；
- Python/FastAPI 开发；
- 后台线程、SSE、approval 等并发语义；
- 依赖与 lockfile；
- 单元测试、lint 和外部 HTTP contract；
- PM/Engineer/QA/Acceptance 完整协作。

评测目录：

- `evaluation/self_hosting/README.md`
- `evaluation/self_hosting/tasks/01_api_server.md`
- `evaluation/self_hosting/acceptance/backend.md`
- `evaluation/self_hosting/results/`

## 2. 评测方法

### 2.1 可复现输入

每轮应固定：

- baseline commit；
- task 文本；
- Workflow 配置；
- provider/model 和真实 context window；
- required/optional Skills；
- Memory 初始状态和 MCP 开关；
- Harness commit；
- 外部 checker 版本。

每轮使用全新 workspace，避免上一轮 Worktree、Artifact、Memory 和 dependency cache 污染结论。

### 2.2 三层验收

```text
Harness Gate
  -> Artifact、Git diff、阶段内 verification evidence
Target Regression
  -> 目标仓库自己的 pytest / lint
External Contract
  -> 仓库外 checker 启动服务并检查 HTTP/SSE/approval 行为
```

模型写出的测试不能成为唯一验收。否则它可以让实现和测试同时偏离需求，形成自洽但错误的候选。

### 2.3 观测指标

至少记录：

- Workflow/phase/attempt 状态；
- wall-clock duration；
- LLM calls、input/output tokens；
- tool calls 和结果；
- context compact 次数；
- handoff 保存和注入次数；
- Artifact 数量；
- changed files/patch hash；
- test/lint/external checker 结果；
- Memory recall/reflection；
- 最终失败归因。

成功率只是一个结果，成本、重复工作和失败是否被正确拦截同样重要。

## 3. API Run 演进时间线

### 3.1 Run 01：能写代码，但没有形成可验收交付

结果：PM 完成规划，Engineer 在 Worktree 写入 3 个文件、533 行，但三次执行都没有生成 `ImplementationReport`，Workflow 在 Engineer 阶段失败，QA 没有启动。

同时发现候选代码本身不可运行：依赖未声明、导入不存在类、核心路径有 placeholder、lint 大量失败。

验证到的 Harness 能力：

- Worktree 保持目标仓库干净；
- provider/context 错误可以恢复；
- resume 复用同一个 Worktree；
- 失败状态能持久化。

暴露的问题：

- Step budget 只在最后截断，缺少收敛提醒；
- retry 没有足够的 working context；
- Role prompt 对工作区、交付 Artifact 和验证要求不够明确；
- 长轮次消耗大量 token，却没有完成协议要求。

对应改进：

- 在剩余 step 较少时注入收敛提示；
- 强化 Engineer 的 Artifact 和 verification 契约；
- phase 异常时持久化失败；
- 限制 shell `cd/pushd` 越界；
- resume 和 retry 使用更清楚的 corrective reason。

正式复盘见 `evaluation/self_hosting/results/api-run-01.md`。

### 3.2 Run 02：重复失败暴露“代码进展”和“流程进展”不同步

Engineer 已经修改 `main.py`、runtime 和 server，但第二次 36-step attempt 仍主要重复阅读已有代码，最终缺少 `ImplementationReport`。

关键认识：

> Worktree 保存了文件状态，不等于新 Agent 知道上一 attempt 为什么这样改、还差什么。

这推动了后续的 attempt working state：不仅保存 changed files，还保存最近动作、失败、门禁问题和下一步。

### 3.3 Run 03：第一次真正证明 Evidence Gate 有必要

QA 最终文本声称：

- 所有接口实现；
- 多组测试全部通过；
- verdict=pass。

但同一 QA phase 的 `run_tests` 事件包含失败。Workflow 没有相信 final summary，而是拒绝 pass：

```text
test_report declares verdict=pass,
but a verification tool reported failure, issues, timeout, or error
```

resume 后 Engineer 又创建 `outcome=changed` 的 ImplementationReport，但本 phase 的 Worktree diff 没有变化，第二个 Gate 再次拒绝。

这一轮确认了两条核心规则：

1. 角色 final/Artifact 是叙述，不是权威事实；
2. Evidence 必须是 phase-local，不能用上一阶段遗留改动冒充本轮动作。

### 3.4 Run 04：门禁正确，但过严契约和续接效率仍有问题

初次 Engineer 实现产生 5 个 changed files，但报告中的列表不完全一致。早期 Gate 直接失败，导致已经完成的大量工作因为 metadata 误差被丢弃。

修复方向：

- Worktree changed files 是权威事实；
- 报告字段不一致时由 Harness 自动 reconciliation 并记录 warning；
- 只有语义冲突才失败，例如 `outcome=no_change` 但真实 diff 改变。

resume 后 Workflow 经历多个 Engineer/QA fix cycle 并到达 PM Acceptance。QA 权威 verdict 仍为 fail，但 PM 生成了“ACCEPTED”报告。顶层 Workflow 最终因 QA 未通过而失败，随后又补强 Acceptance Gate：Artifact status/verdict 也必须一致，不能只在 workflow 最终阶段兜底。

这一阶段还暴露了短期记忆误解：长期 Memory 目录为空并不说明系统失效；失败 retry 的续接问题应该由 Workflow handoff 解决，而不是把未验证代码进展写入长期 Memory。

### 3.5 Run 05：Harness 回归通过，候选实现仍失败

Run 05 是目前最完整的可复现诊断结果。最终：

```text
workflow status = failed
qa verdict = fail
fix cycles = 2
candidate external contract = failed
```

关键指标：

| 指标 | 结果 |
|---|---:|
| 总时长 | 64 分 35.8 秒 |
| Trace records | 4,133 |
| 完成 LLM calls | 251 |
| Input tokens | 4,455,644 |
| Output tokens | 44,504 |
| Tool calls/results | 236/236 |
| Role executions | 10 |
| Context compactions | 143 |
| Artifacts | 9 |
| Candidate patch | 7 files / 709 insertions |

已经验证：

- QA 自报 pass 被同阶段失败证据降级为 effective fail；
- 两个 fix cycle 的 role run id 唯一；
- phase-local `agent_status` 不再引用旧 attempt；
- AcceptanceReport 的 `verdict=fail + status=accepted` 被拒绝；
- target Worktree 环境检测出缺少 `serpapi`；
- 共享环境 `pip install` 被权限策略阻止；
- Engineer 改 dependency manifest/lock 后，后续不再报告该缺失；
- failed workflow 没有触发长期 Memory reflection；
- 目标仓库主工作区保持干净。

候选仍然失败：

- API 入口、对象构造和 shutdown 使用错误接口；
- SSE、approval、workflow resume 等关键行为只是 placeholder 或语义错误；
- HTTP status/response contract 不匹配；
- 自写测试覆盖了 placeholder 行为，没有验证真实契约；
- Ruff 和外部 checker 未通过。

这说明 Harness 的“拒绝错误成功”能力在增强，但模型自主实现能力和评测效率还未达到稳定 demo 标准。

完整记录见 `evaluation/self_hosting/results/api-run-05.md`。

## 4. 关键事故模式

### 4.1 自证循环

```text
模型实现代码
  -> 模型编写低质量测试
  -> 自己的测试通过
  -> 模型宣布完成
```

解决：引入 frozen external checker；Harness 机器证据高于角色叙述；QA 与 Engineer 权限分离。

### 4.2 Artifact 与真实状态漂移

表现：changed_files 漏项、verdict 与 status 冲突、报告 changed 但本轮无 diff。

解决：

- 能从机器事实推导的字段由 Harness reconciliation；
- 语义冲突由 Gate fail-closed；
- Artifact 使用 version/history 保存修正轨迹。

### 4.3 重试重复工作

表现：新 Agent 从仓库入口重新读起，不知道上一 attempt 已经修改什么。

解决：

- Worktree 保存物理代码；
- checkpoint 保存流程位置；
- bounded handoff 保存认知进展；
- retry prompt 要求从 existing workspace continuation；
- 不用长期 Memory 保存临时状态。

### 4.4 验证环境错误

表现：裸 `python -m pytest` 选中系统/Harness 解释器，产生误导性 dependency error。

解决：结构化 verifier 识别目标项目，使用 `uv run --locked --no-env-file`，清除父 `VIRTUAL_ENV`；只有 verifier 事件进入 Evidence Gate。

### 4.5 上下文配置与模型实际窗口不一致

Run 05 配置 100,000 tokens，但模型实际窗口 32,768。一次请求触发 reactive context recovery；其余大量 micro-compaction 也说明输入成本很高。

解决方向：

- 评测配置绑定真实模型 capability；
- runtime context 做 delta/去重；
- Artifact 按 phase 与相关性选择，不每步全量注入；
- 分别统计 tool micro-compaction 和 LLM summary，不能只看“压缩次数”。

### 4.6 基准本身不完整

冻结 baseline 导入 `serpapi` 却未声明依赖，导致 Agent 花费步骤修复与任务无关的问题。

解决：评测前先保证 baseline 在其锁定环境中通过 smoke test；基准缺陷应单独修复，而不是算作 Agent 任务难度。

## 5. 如何区分 Harness 缺陷与模型失败

可以使用以下判断框架：

| 现象 | 更可能属于 Harness | 更可能属于模型/候选 |
|---|---|---|
| 正确 Tool Call 被错误拒绝 | 是 | 否 |
| 测试失败却被标成 pass | Evidence Gate 缺失时是 | QA 叙述本身也是 |
| retry 看不到持久化 changed files | 是 | 否 |
| 有完整输入仍调用不存在 API | 否 | 是 |
| verifier 选错解释器 | 是 | 否 |
| 外部契约未满足 | 未运行/误判时是 | 实现错误时是 |
| token 成本异常高 | 上下文装配可能是 | 模型反复探索也可能是 |

真实问题经常两者都有。评测报告应分别写：

1. Candidate defects；
2. Harness findings；
3. Benchmark quality issues。

## 6. 下一轮评测应如何改进

### P0：先修评测可信度

- 修复 Trace Markdown root status/duration 聚合；
- baseline 在 locked env 中先通过自身 smoke test；
- external checker 清除继承 `VIRTUAL_ENV`；
- Workflow context window 与模型真实限制一致；
- 保存完整 Harness commit、dirty diff hash、target commit 和 patch hash。

### P1：降低成本

- Artifact context 只注入当前 phase 必需项；
- 对未变化 runtime section 做 cache/delta；
- Engineer prompt 强制先读既有接口再写代码；
- 设置分阶段 token/step budget，不只设置总 step；
- 统计 repeated file reads 和无效 tool calls。

### P2：提高成功率

- 给 API/SSE/approval 任务提供更小的 milestone TaskSpec；
- 每完成一个 milestone 立即运行 focused verifier；
- Engineer fix prompt 只提供失败契约与相关文件，而不是重新给完整需求；
- Acceptance fail 时由 Harness 确定性规范化 Artifact 状态，而不是消耗多步让 PM 猜合法枚举。

## 7. 面试中如何讲失败

不要回避失败，也不要把失败包装成成功。推荐表述：

> 我做了多轮 self-hosting evaluation，最终候选还没有通过完整外部契约，但评测推动了 Harness 的关键设计。最典型的是 QA 明明有失败的测试事件，却在报告里声称 pass；我因此把阶段内结构化验证和 Git diff 设为权威证据。后续评测确认系统能拒绝假 pass、错误 acceptance 和无真实 diff 的实现报告。与此同时，Trace 也暴露了 4.46M input token 的上下文效率问题，这成为下一轮优化重点。我的目标不是制造一个看起来成功的 Demo，而是让系统能够诚实地区分成功和失败。

这种回答体现：

- 有真实实验；
- 能从 Trace 归因；
- 能区分模型、Harness 和 benchmark 问题；
- 修改有回归证据；
- 对当前边界诚实。
