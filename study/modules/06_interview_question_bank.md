# 秋招面试问题库

## 1. 使用方法

不要背整段答案。每题按四步回答：

```text
场景问题 -> 当前设计 -> 为什么这样取舍 -> 证据与局限
```

回答中优先使用真实例子：

- QA 自报 pass 被失败 test event 推翻；
- ImplementationReport 声称 changed，但 phase-local diff 未变化；
- retry 重复探索后引入 attempt handoff；
- 目标环境漏依赖，Verifier 与共享环境隔离；
- Run 05 输入 token 过高，暴露 runtime context 重复注入。

## 2. 项目定位与架构

### Q1：这个项目解决了什么问题？

普通 Agent Demo 只演示模型调用函数。本项目解决 coding 场景中的副作用控制、长任务上下文、跨进程恢复、角色交接、机器验收和可观测性，目标是把不稳定 LLM 放进一个可验证的执行系统。

### Q2：为什么叫 Harness，不叫 Agent Framework？

Framework 强调开发抽象，Harness 更强调模型周围的执行约束：工具、权限、隔离、证据、恢复、追踪和评测。项目当然有 Agent Runtime，但亮点在于 Harness 对真实副作用和结果的管理。

### Q3：整体架构如何分层？

从下到上讲：Provider Adapter；Tool/Security/Worktree；Agent Runtime；Context/Memory/Task/Artifact；Subagent/Workflow Orchestration；CLI 与 Trace/Evaluation。指出依赖方向，Provider 和存储细节不应反向污染 Orchestrator。

### Q4：你个人最关键的设计贡献是什么？

选择两个讲深：三层状态模型，以及 Evidence Gate。前者解决 context、retry 和长期知识混用；后者把模型叙述与 Git/test 事实分开。

### Q5：项目与 Claude Code/Codex 有什么差距？

成熟产品拥有更强沙箱、模型专用推理、流式交互、并行调度、远程执行、代码索引、缓存和大规模 eval。本项目聚焦可解释的串行闭环，优势是源码和设计边界可完整说明，不应声称能力对等。

### Q6：为什么没有直接使用 LangChain/LangGraph？

为了理解并控制 provider tool protocol、消息原子性、恢复和证据边界，核心 Runtime 自己实现。若未来 Workflow 拓扑复杂，能评估图框架，但 Worktree、安全和 Evidence 仍需自定义，框架不会自动解决。

## 3. LLM 与 Tool Calling

### Q7：OpenAI 和 Anthropic Tool Calling 最大区别是什么？

讲 system 位置、tool schema、tool call block 和 tool result 回灌。重点是 Anthropic 同一轮多个结果要作为一条 user message 中的多个 `tool_result` block。

### Q8：为什么不直接用 HTTP？

官方 SDK 保留连接、错误、超时和协议演进；项目只做必要适配。手写 HTTP 会重复 SDK 工作，也更容易在兼容 endpoint 上漏字段。

### Q9：为什么还保留 `raw` response？

统一模型只覆盖上层依赖语义。供应商新字段、compatible API 异常和序列化问题需要 raw 排障；但业务不能依赖 raw，否则适配层失效。

### Q10：Tool arguments 不是 dict 怎么办？

在 provider 边界解析与校验。合法 JSON string 可转 dict；list/非法 JSON 生成结构化失败回灌。绝不能让参数校验失败的 write/bash 调用继续执行。

### Q11：多个 Tool Call 是并行执行吗？

当前 Agent 按确定顺序执行，再批量回灌。这样副作用顺序和 Trace 更容易解释。未来只对声明为只读、无依赖的工具并行，不能盲目并行 edit/bash。

### Q12：怎样防止 Tool Call 无限循环？

角色 max_steps、接近预算的 progress instruction、Task/phase 目标、错误结果回灌和 Workflow retry/fix budget共同约束。单一 max_steps 只能止损，不能替代计划。

### Q13：这算 ReAct 吗？

算工程化 ReAct 变体：模型观察上下文，生成行动 Tool Call，观察结果，再继续决策。系统展示 progress summary，不等于泄露隐藏 chain-of-thought。

## 4. Agent Runtime、Hook 与 Recovery

### Q14：一次 Agent step 的完整顺序？

Background notifications -> Context prepare -> BeforeLLM runtime injection -> preflight budget -> LLM -> assistant message -> PreToolUse -> tool -> PostToolUse -> provider-specific result messages -> next step/final。

### Q15：Hook 和 Event 为什么分离？

Hook 能改变行为，Event 只通知 UI，Trace 负责持久化。分离后权限可单测、CLI 可替换、日志失败不影响流程。

### Q16：`effective_result` 是什么？

多个 Hook 顺序执行后的有效组合结果。deny 立即短路；allow data 合并；replace 覆盖工具结果。它避免 HookManager 只返回最后一个 callback 而丢失前面决策。

### Q17：为什么使用 ContextVar？

Trace/Recovery 是与当前 run 绑定的横切上下文，层层显式传参会污染大量 API。ContextVar 支持嵌套和并发上下文隔离；必须通过 contextmanager 在 finally reset，不能当普通全局变量。

### Q18：`with trace_scope(...)` 的原理？

进入时 `ContextVar.set`，`yield` 把作用域交给 with body，退出时 reset token。异常也会执行 finally，因此不会把子 run context 泄漏给后续请求。

### Q19：哪些错误应该 retry？

timeout、连接失败、429、过载和部分 5xx 可有界重试；认证、权限、非法参数通常直接失败；context overflow 触发 compact；output truncation 提高输出预算/续写。未知错误不能无限重试。

### Q20：Retry 会不会重复执行工具？

LLM retry 在 Tool Call 被接受和执行前发生，不重放工具。项目也不对任意 Tool 透明重试，因为副作用工具未必幂等。

### Q21：为什么 Background Job 只支持 Bash？

它是当前最明确的长耗时动作。扩大到任意工具前需要幂等性、取消、资源 ownership 和结果时序语义；先做显式 Bash 能控制复杂度。

## 5. Context 与 Memory

### Q22：Context 压缩具体怎么做？

大工具结果落盘、旧 Tool Result micro-compact、早期消息 LLM summary、保留近期原子消息组、最后 hard trim；provider 仍报超限时 reactive recover。完整 transcript 保留用于审计。

### Q23：为什么 Tool Call/Result 要原子分组？

供应商协议和模型因果链要求调用与结果对应。拆开会导致 API 拒绝，或模型误以为动作未执行而重复副作用。

### Q24：为什么 token estimate 不直接等于实际 token？

不同 provider/model tokenizer 不同，tool schema 和隐藏包装也有成本。当前用启发式 preflight，再用 context error reactive recovery兜底。生产版本应接模型 tokenizer/capability registry。

### Q25：短期记忆如何实现？

分两部分：Agent run 内由 Context summary 管理；跨 retry/resume 的任务进展由 Workflow checkpoint + attempt handoff 管理。不是把所有临时信息写进长期 Memory。

### Q26：长期 Memory 存什么？

稳定用户偏好、明确反馈、项目约定、reference 和成功工作流中有证据支持的经验。临时失败和未验证代码不自动写入。

### Q27：记忆如何召回？

active filter + lexical score + pinned/type/confidence/recency 等信号，可选 LLM side-query 选 id，最后按预算注入。模型只选择已有条目，不凭空生成 memory 内容。

### Q28：反思、遗忘、过期如何做？

成功 Workflow reflection 提炼经验；stale 表示可能过期且默认不召回；archive 保留审计；forget 物理删除；新鲜 Git/验证事实优先于 memory。

### Q29：为什么不用向量数据库？

当前规模小，文件可读可审计，瓶颈是记忆质量和生命周期。向量库提升大规模语义检索，但不解决冲突、过期和错误写入；Store 接口允许未来增加索引。

### Q30：Run 失败后 Memory 为空，是不是没生效？

不是。失败进展由 Workflow working memory 承担；长期 Memory 拒绝失败反思正是防污染策略。应检查 handoff 是否注入，而不是期待 memory 文件增长。

## 6. Skill 与 MCP

### Q31：Skill 和 Tool 有什么区别？

Skill 是方法/知识，Tool 是可执行能力。Skill 不能因为包含脚本说明就绕过角色 allowlist 和 Permission Hook。

### Q32：Required 和 Optional Skill 如何选择？

Orchestrator 对关键流程使用 required 确定性注入；Agent 对可能有用的能力从 catalog 自主 `skill_load`。前者保证关键规范，后者节省 token。

### Q33：复杂目录 Skill 支持吗？

每个独立 Skill 以自己的 `SKILL.md` 为发现单元；Skill 内可有多层 reference。资源通过受路径和大小限制的工具按需读取，不全量注入。

### Q34：MCP 如何接入现有工具系统？

MCPManager discovery 得到 binding，适配为 namespaced ToolDefinition，注册到 ToolRegistry；后续走同一 Agent Loop、Permission Hook 和 Trace。

### Q35：为什么不把本地工具改成 MCP Server？

本地工具与 Worktree、路径、安全环境、完整日志和 Evidence 深度集成。MCP 提供互操作，不自动提供这些语义；改造会增加进程/部署复杂度。

### Q36：为什么当前没有 OAuth？

OAuth 需要浏览器授权、callback、code exchange、token 加密持久化、refresh 和撤销。当前 CLI 没有 credential lifecycle；GitHub MCP 更适合本地 Docker/stdio + 显式 token。

## 7. Security 与可靠工具

### Q37：怎样防止 Agent 读取 `.env`？

角色权限、PermissionHook 和 Tool 内 sensitive path validation 分层阻止；子进程环境也移除 secret。不能只靠 system prompt。

### Q38：怎样防止 `../` 或 symlink 越界？

对候选路径 resolve 后检查是否 `is_relative_to(resolved workspace)`，不是字符串前缀比较。最终 I/O 前在 Tool 内再次检查。

### Q39：命令黑名单足够安全吗？

不够。Shell 语义太复杂，当前规则只是防误操作；配合 allowlist、确认、环境隔离和 Worktree 降低风险。生产高风险场景必须增加 OS/container sandbox。

### Q40：为什么阻止 `pip install`？

防止修改 Harness 共享环境、泄漏凭据和破坏可复现性。正确做法是修改目标项目 dependency manifest/lock，或在 workspace 内建隔离环境。

### Q41：Worktree 提供了什么，不提供什么？

提供 Git 修改隔离、真实 diff、patch 和显式 apply；不提供进程、网络、系统文件和资源的 OS 级隔离。

### Q42：为什么 QA 不允许普通 bash？

QA 应只读验证。普通 bash 可以改源码或伪造结果；专用 verifier 让命令形态、环境和证据结构由 Harness 控制。

### Q43：怎样保证验证使用目标项目环境？

识别 pyproject/lock，使用 `uv run --locked --no-env-file`，清理 inherited `VIRTUAL_ENV`，再结构化分类 missing dependency、lock outdated 和 test failure。

## 8. Task、Artifact 与 Workflow

### Q44：为什么 TaskManager 和 TaskTool 分层？

Manager 承担可复用领域规则，Tool 只承担 LLM schema/参数适配。Workflow、Hook、测试可直接调用 Manager，不必伪造 Tool Call。

### Q45：为什么不用 TodoWrite？

整表重写容易覆盖、漏项和跨 Agent 不一致。统一 Task 以 id 增量更新，session scope 覆盖 Todo 用途，project scope 支持跨会话协作。

### Q46：Task 和 Artifact 的区别？

Task 是控制面状态，Artifact 是数据面交付。一个回答谁做什么和进度，另一个保存 PRD、报告和证据说明。

### Q47：Artifact 为什么要 version？

避免角色基于旧版本静默覆盖；history 也保留交接修订。即使当前串行，模型多次 read/update 仍可能使用过期状态。

### Q48：Subagent 和 Agent Team 的区别？

Subagent 是父 Agent 对一个子任务的隔离委派，当前同步返回；Agent Team 是多个自治成员共享任务、并发运行、相互通信，需要调度和冲突解决。

### Q49：Workflow 算 Orchestrator-Worker 吗？

算。Orchestrator 决定阶段、角色、输入、预算和 Gate；Worker 在阶段内使用 LLM+Tools 完成开放任务。

### Q50：为什么按 Workflow 建 Worktree？

串行角色必须共享连续代码状态；每角色 Worktree 会引入 merge。未来并行方案分支才需要各自 Worktree。

### Q51：Resume 怎么保证不重做？

WorkflowStore 保存 completed phase、当前 checkpoint、Artifact/Worktree id 和 fix cycle。resume 跳过完成阶段，恢复同一 Worktree；中断 attempt 转为 handoff。

### Q52：Evidence Gate 检查什么？

规划 Artifact/change_required；实现 outcome 与 phase-local diff；报告 changed files 与 Git 事实；QA verdict 与当前 verifier event；Acceptance verdict/status 与权威 QA 状态。

### Q53：为什么不完全相信模型生成的报告？

真实评测出现过 QA 声称 pass 但测试失败、Engineer 声称 changed 但 diff 未变。模型适合解释证据，Harness 必须裁决机器事实。

### Q54：Gate 太严格导致误伤怎么办？

区分可机械修正的表示误差和语义冲突。changed_files 漏项可按 Git 自动 reconciliation；`outcome=no_change` 但 diff 变化属于冲突，应失败。

### Q55：Phase retry 和 fix cycle 区别？

Retry 是同一角色未满足阶段协议后的纠正机会；fix cycle 是 QA 给出有效 fail 后进入 Engineer 修复和 QA 回归的业务循环。预算和统计应分开。

## 9. Trace 与评测

### Q56：Trace 和日志有什么区别？

普通日志面向人阅读；Trace 有稳定 schema、run/parent/correlation、phase、duration、usage，可聚合和复盘。CLI 日志只是 Event 的一种展示。

### Q57：为什么 JSONL？

追加写简单，中途崩溃已有记录仍可读，适合流式处理。Markdown 是派生报告，WorkflowStore 才是恢复状态。

### Q58：如何防止 Trace 泄密？

敏感 key/value 递归脱敏，内容有 capture 开关和长度预算，完整命令输出落到受控路径。仍需避免让 secret 先进入 Tool Result。

### Q59：项目怎么评测？

固定 baseline/task/config/model，使用全新 workspace，收集 Trace/Artifact/Patch，先 Harness Gate，再目标回归，最后仓库外黑盒 contract。

### Q60：最新评测为什么失败？

候选实现存在入口、API 构造、SSE、approval、状态码和测试覆盖问题。Harness 正确拒绝了 QA 假 pass 和 PM 错误 acceptance。失败同时暴露 4.46M input token 的效率问题。

### Q61：失败评测对简历有价值吗？

有，前提是能展示可复现输入、Trace 归因、修复和回归。不要声称任务成功；强调系统能识别失败，以及评测如何推动 Evidence、Memory 和验证环境设计。

### Q62：如何区分模型问题和 Harness 问题？

看责任边界：输入完整却调用不存在 API偏模型；正确测试被错误解释偏 Harness；baseline 漏依赖偏 benchmark。报告分别列 Candidate defects、Harness findings、Benchmark issues。

## 10. 系统设计演进题

### Q63：如果支持并行 Agent Team，先改什么？

先定义 task claim/lease、每分支 Worktree、Artifact 并发版本、冲突合并、取消传播和预算调度，再实现 scheduler。不能只把角色调用改成并发。

### Q64：如果部署为服务，需要补什么？

持久数据库、Workflow lease/lock、队列、worker heartbeat、幂等 API、credential store、容器 sandbox、租户隔离、审计保留策略、指标告警和流式事件协议。

### Q65：如果 Memory 增长到百万条？

Store 后增加 embedding/BM25 hybrid index、namespace/ACL、时间衰减、去重聚类和离线 consolidation；原文与 source 仍保留，向量库不成为唯一事实。

### Q66：如果 Tool 需要自动重试？

为 ToolDefinition 增加 side-effect/idempotency metadata，只对读操作或带 idempotency key 的调用自动 retry；写操作由工具显式实现事务/补偿。

### Q67：如何降低 token 成本？

先从 Trace 找主要来源。当前优先减少每步 Artifact 重复注入、按 phase 做相关性选择、缓存不变 system/tool schema、减少重复 read、缩短 fix prompt，而不是盲目提高 compact 次数。

### Q68：如何衡量项目最终完成？

至少 2-3 个固定 task 在冻结基准上多次运行；报告成功率、成本、时长和失败分类；外部 contract 通过；README 有架构和复现步骤；已知限制清楚。单次成功录屏不够。

## 11. 反问自己的问题

面试前应能脱离文档回答：

1. 画出一次 Anthropic 多工具调用的完整消息序列。
2. 指出 Hook、Event、Trace 各自改变或不改变什么。
3. 解释为什么 Context summary、attempt handoff、Memory 不能合并。
4. 用 Run 03 说明 Evidence Gate 的必要性。
5. 用 Run 05 解释依赖环境如何选择和为什么阻止 `pip install`。
6. 说明 Worktree 为什么不是 sandbox。
7. 说明从串行 Workflow 到并行 Team 需要新增哪些一致性机制。
8. 坦诚给出当前评测结果、最大瓶颈和下一步优先级。
