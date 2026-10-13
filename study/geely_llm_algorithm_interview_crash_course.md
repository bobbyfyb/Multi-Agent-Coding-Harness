# 吉利汽车集团 - 大模型算法岗一面突击资料

> 适用场景：2027 届秋招，一面约 30 分钟，面试官只能看到 `main_ai.pdf`（用户所称 `resume-agent.pdf`）。  
> 使用方法：先背“0. 今下午只背这些”，再按面试顺序过一遍项目和八股。不要逐字背长答案，要记住每题的关键词和因果链。

## 0. 今下午只背这些

### 0.1 你的唯一主线

> 我既做过基于 PPO 的自适应 RAG 研究，也从零实现过可验证、可恢复的 Agent Runtime；此前还有美团真实业务开发经验。因此我的优势是能把模型训练、Agent/RAG 系统和业务落地连成闭环。

所有回答尽量回到三件事：

1. **算法研究**：ARF-RAG，MDP + LoRA-SFT + PPO，解决何时检索、检索什么以及检索/生成目标错位。
2. **Agent 工程**：Harness，解决工具副作用、长上下文、角色交接、机器验收、恢复和审计。
3. **业务交付**：美团经历，证明你理解需求拆解、性能稳定性、埋点验证、跨团队协作和上线风险。

### 0.2 90 秒自我介绍（主版本）

> 面试官您好，我叫方昱博，目前是筑波大学信息学博士，预计 2027 年毕业。我的研究和项目主要围绕大语言模型、RAG、强化学习和 Agent 系统展开。
>
> 算法研究方面，我做了 ARF-RAG。它针对传统 RAG 固定检索、启发式 query 改写，以及 Retriever 和 Generator 目标错位的问题，把检索决策、query 生成和答案生成统一到一个 LLM policy 中。训练上先通过 LoRA-SFT 建立初始策略，再把多轮 RAG 建模为 MDP，用 PPO 根据答案正确性、检索有效性和检索成本进行联合优化。在 PopQA、HotpotQA 和 2WikiMultiHopQA 上，完整模型都取得了最优的复现实验结果。
>
> 工程方面，我独立实现了一个 Multi-Agent Coding Harness。重点不是简单做多个 Agent，而是围绕真实代码副作用，统一 OpenAI 和 Anthropic 的 Tool Calling，加入权限 Hook、Context/Memory、Trace、Git Worktree 隔离、Checkpoint Resume，以及 PM、Engineer、QA、Acceptance 的结构化工作流。我最看重的是 Evidence Gate：模型负责提出方案，但 Git diff 和测试结果才是验收事实。
>
> 此前我在美团做过业务开发，参与 C/B 端 IM 和频道页主链路，积累了需求拆解、性能优化、跨团队联调、上线和数据验证经验。我希望应聘吉利的大模型算法岗，是因为这个岗位同时强调模型训练优化、Agent/RAG 应用和业务落地，和我的经历比较匹配。我也希望把研究中的自适应检索和工程中的可靠 Agent 能力用于智能座舱、研发知识助手或售后服务等真实场景。

### 0.3 30 秒自我介绍（被打断时）

> 我是筑波大学信息学博士方昱博，方向是 LLM、RAG、强化学习和 Agent。我的两个代表项目分别是基于 LoRA-SFT + PPO 的 ARF-RAG，以及从零实现的 Multi-Agent Coding Harness。前者体现模型训练和评测能力，后者体现 Tool Calling、Workflow、权限、恢复和可观测性的工程能力；此前的美团经历又让我具备真实业务交付经验，所以我和岗位要求的“算法 + 应用落地 + 跨团队协作”比较匹配。

### 0.4 两个项目各用一句话讲清楚

- **ARF-RAG**：把“要不要检索、用什么 query 检索、什么时候回答”统一成一个多步策略，用 SFT 初始化、PPO 根据答案质量、检索有效性和成本共同优化。
- **Harness**：把不稳定的 LLM 放进一个由工具权限、结构化交付物、Git diff、机器验证、Checkpoint 和 Trace 约束的可执行系统。

### 0.5 必背数字

ARF-RAG 完整模型的绝对结果：

| 数据集 | EM | F1 | 任务特征 |
|---|---:|---:|---|
| PopQA | 46.6 | 53.9 | 单跳、事实型 |
| HotpotQA | 34.7 | 46.3 | 多跳 |
| 2WikiMultiHopQA | 36.4 | 43.4 | 多跳 |

训练配置：Qwen3-4B-Instruct；Contriever-MSMARCO；2018-01 Wikipedia；LoRA-SFT 1 epoch、学习率 `2e-5`、batch size 16；PPO 2 epochs、学习率 `2e-5`、batch size 128；最大检索 hop `T=2`；`lambda=0.5`，`alpha=0.2`；2 张 A100 80GB。

检索行为：PopQA 多跳约 10%；HotpotQA 两跳约 52%、一跳约 38%；2Wiki 两跳约 47%、一跳约 41%。一跳 query 的 hit ratio 相比原问题提升：PopQA 13.1%、HotpotQA 30.3%、2Wiki 53.2%。

### 0.6 今天不要主动说的内容

- 不说 Harness “全量测试当前全部通过”；当前全量测试会被 `serpapi` 导入兼容问题阻断。
- 不说 Harness 是生产级沙箱、云平台或已达到成熟 Coding Agent 产品水平。
- 不说“多 Agent 一定优于单 Agent”；正确说法是需要用任务成功率、成本、时延和人工介入量评估。
- 不说 ARF-RAG “训练了 Retriever”；Retriever 是冻结的，优化的是统一 LLM policy 和其生成的 query。
- 不说 RL 奖励在推理时需要 gold context；gold supporting context 只在训练奖励计算中使用。
- 不把 query hit ratio 当作最终答案正确率；它只证明检索 query 更容易命中包含答案的 passage。
- 如果没有真实用 Dify/RAGFlow 上线，不要说生产落地；说“理解核心抽象并能把自研能力映射过去”。

## 1. 30 分钟面试节奏预测

| 时间 | 可能内容 | 你的策略 |
|---|---|---|
| 0-2 分钟 | 自我介绍 | 用 90 秒版本，主动埋下 ARF-RAG、Evidence Gate、美团三个钩子 |
| 2-12 分钟 | 项目深挖 | 算法岗优先讲 ARF-RAG；每题按“问题-设计-证据-局限”回答 |
| 12-19 分钟 | Agent/Harness | 强调 Runtime、Tool Calling、可靠性、评测，不堆模块名 |
| 19-26 分钟 | LLM/RAG/RL 八股 | 先讲公式或数据流，再讲工程意义 |
| 26-28 分钟 | 岗位动机/业务题 | 对齐吉利 Full-Domain AI、座舱/研发/售后场景 |
| 28-30 分钟 | 反问 | 问团队业务、训练/应用边界、评测和新人交付目标 |

如果只够讲一个项目，优先 ARF-RAG，因为岗位名是“大模型算法岗”，且 JD 明确强调后训练、评测、RL；随后用 Harness 补足应用落地。

## 2. ARF-RAG 项目深挖

### 2.1 2 分钟项目介绍

> 传统 RAG 有三个问题：第一，固定检索会在简单问题上增加成本和噪声，在复杂问题上又可能检索不足；第二，直接拿用户问题检索通常冗长或信息需求不明确；第三，Retriever 追求相关性，Generator 追求最终回答正确，两者分别优化会产生目标错位。
>
> ARF-RAG 的核心是把一次多轮 RAG 看成 MDP。状态是原始问题和累计检索上下文；动作只有 `[Search]` 和 `[Answer]`。如果选择 Search，模型同时生成当前信息需求对应的 retriever-friendly query，冻结的 Contriever 返回 top-k 文档并更新状态；如果选择 Answer，模型生成答案并终止。
>
> 训练分两阶段。第一阶段通过两轮自监督标注构造覆盖不检索、单跳和多跳情况的数据，用 LoRA-SFT 建立能正确输出动作和 query 的初始策略。第二阶段用 PPO 联合优化。Answer 动作奖励是 EM + F1；Search 动作奖励同时考虑检索结果与 gold supporting context 的余弦相似度，以及随检索次数增加的成本惩罚；非法格式给 -1。
>
> 实验使用 Qwen3-4B-Instruct、Contriever-MSMARCO 和三个 QA 数据集。完整模型在三个数据集上都取得最优复现实验结果；检索 hop 分布和 reward ablation 说明模型确实学会了按问题复杂度决定检索次数；query hit ratio 又验证了它生成的 query 更适合给定 Retriever。

### 2.2 MDP 五要素

1. **Policy `pi_theta`**：同一个 Qwen policy，同时扮演中央控制、query generator 和 answer generator。
2. **State `S_t`**：`[q, O_t]`，原问题 `q` + 累计观测 `O_t`。
3. **Action `A`**：`{[Search], [Answer]}`；action 后面的 token 序列分别是 query 或 answer。
4. **Transition**：Search 后调用冻结 Retriever，把结果加入上下文；Answer 后终止。
5. **Reward**：回答质量、检索有效性/成本、非法动作惩罚。

状态转移：

```text
S_t = [question, accumulated observations]
  -> policy outputs [Search] query
  -> retriever(query) = top-k passages
  -> S_(t+1) = [question, old observations + passages]
  -> policy chooses Search again or [Answer] answer
```

### 2.3 奖励函数必须会写

回答动作：

```text
r_ans = EM(pred, gold) + F1(pred, gold)
```

检索动作：

```text
r_ret^t = lambda * CosSim(retrieved_passages, gold_supporting_context)
          - (1 - lambda) * alpha * t
```

非法动作：`r_other = -1`。

总目标：

```text
max E[sum_{t=1..T} r_t]
```

口头解释：相似度项鼓励 query 找到真正有用的证据；随 hop 增长的惩罚抑制无意义检索和死循环；答案奖励保证最终目标仍是正确回答。它比只在 episode 末尾给答案奖励更密集，能缓解 Search 动作的信用分配问题。

### 2.4 SFT 数据怎么构造

第一轮先让目标 policy 不检索直接回答：

- `F1 = 1`：说明已有参数知识足够，标注 `[Answer] correct_answer`。
- `0.2 < F1 < 1`：需要补证据，标注 `[Search] optimized_query`。
- `F1 <= 0.2`：先标 Search，并进入第二轮。

Query 弱监督不是直接用原问题：让同一个目标模型在 DSPy BFRS 优化出的 few-shot prompts 下生成多组候选 query；逐个检索并根据 retrieval reward 排序，选最高分 query。

第二轮把第一次检索结果加入状态，再回答：

- `F1 = 1`：标注当前状态下 `[Answer] answer`。
- `F1 < 1`：标注继续 `[Search] next_query`。

这样 SFT 数据同时覆盖 no-retrieval、one-hop 和 multi-hop，先把策略带到合理区域，再做 PPO，降低纯 RL 冷启动和格式崩坏。

### 2.5 PPO 原理

重要性采样比率：

```text
ratio_t(theta) = pi_theta(a_t|s_t) / pi_old(a_t|s_t)
```

PPO clipped objective：

```text
L_clip = E[min(ratio_t * A_t,
               clip(ratio_t, 1-epsilon, 1+epsilon) * A_t)]
```

直觉：优势 `A_t > 0` 时提高该动作概率，`A_t < 0` 时降低；但把概率比裁剪在邻域内，避免一次更新把 policy 推得太远。实际训练还会结合 value loss、entropy bonus，并通过 KL 控制策略不要偏离参考/SFT policy 太快。

在 LLM 中，动作是 token 序列。训练通常基于每个生成 token 的 log probability 计算 ratio 和 advantage，再对有效生成 token 做 mask 和聚合。

### 2.6 为什么用 PPO，而不是 DPO / GRPO

> PPO 更适合这里的原因是任务本身是多步环境交互：每次 Search 都会改变下一状态，而且我们有中间 retrieval reward。PPO 能直接优化 trajectory 并进行逐步信用分配。DPO 更适合固定上下文下完整回答的成对偏好，不自然表达“第几跳检索是否有价值”。GRPO 可以省去独立 critic，并通过同一问题的多组 rollout 做相对优势，未来值得尝试，但会增加每个问题的采样成本，而且仍需要设计稳定的过程奖励。

### 2.7 为什么冻结 Retriever

> 第一是控制变量：验证提升来自 policy 的检索决策和 query 生成，而不是 Retriever 参数变强。第二是工程可迁移性：真实业务经常已有固定检索服务或索引，优化上层 policy 成本更低。第三是稳定性：同时训练 Retriever 和 Generator 会引入非平稳环境，让 PPO 更难收敛。后续可考虑交替优化、蒸馏或 differentiable retriever，但必须处理索引更新成本和训练稳定性。

### 2.8 什么叫 retriever-friendly query

不是简单“把问题改短”，而是：

> Query 要忠实表达 policy 在当前状态下缺失的信息，同时适配给定 Retriever 的表示与匹配偏好，使目标证据在该 Retriever 的排序中更靠前。

多跳例子：原问题问“The Hard Easy 某位客串演员的弟弟是谁”。第一跳先检索客串演员，得到 Brian Doyle-Murray；第二跳 query 改成“younger brother of Brian Doyle-Murray”，最终找到 Bill Murray。第二跳必须基于新 observation，而不是重复原问题。

### 2.9 如何证明不是“只是答案变好了”

三层证据：

1. **最终效果**：三个数据集 EM/F1 最优。
2. **行为分析**：简单 PopQA 大多 0/1 hop，复杂 HotpotQA/2Wiki 大多 1/2 hop，说明检索次数会随复杂度改变。
3. **机制证据**：移除设计的 retrieval reward、改成固定 penalty 后，性能明显下降，策略也倾向于 0/1 hop；生成 query 的 hit ratio 高于原问题。

### 2.10 最容易追问的实验数字

| 模型 | PopQA EM/F1 | HotpotQA EM/F1 | 2Wiki EM/F1 |
|---|---|---|---|
| ARF-RAG SFT | 38.7 / 45.7 | 33.3 / 44.1 | 36.0 / 41.8 |
| ARF-RAG Full | 46.6 / 53.9 | 34.7 / 46.3 | 36.4 / 43.4 |

如果问“为什么 2Wiki 上 RL 增益不大”：

> SFT 数据本身已经覆盖两跳轨迹，初始策略很强；同时 T=2 限制了更长推理，gold supporting context 相似度也只是检索价值的代理指标，RL 的边际增益会变小。这个结果反而说明后续需要更细的过程奖励、更长但受控的轨迹，以及对不同任务自适应的 reward scale。

### 2.11 简历中的相对提升怎么解释

简历的 `+7.6% / +37.2% / +22.8%` 是相对某些强 RL 基线在统一复现实验设置下计算的相对 EM 提升，不要主动展开“最强基线”字眼。最稳妥的回答：

> 简历为了压缩篇幅用了相对提升，正式讨论我更愿意报告绝对值和完整 baseline 表。完整模型在三个数据集的 EM 是 46.6、34.7、36.4，F1 是 53.9、46.3、43.4，并且在我们的统一复现实验对比中均为最佳。

### 2.12 论文局限和改进

主动说 2-3 个即可：

- 单一 policy 联合学习决策、query 和 answer，某一阶段错误会传播，训练对 reward 和超参敏感。
- Retriever 冻结，不能解决 Retriever 表示空间本身的能力上限。
- Retrieval reward 依赖 gold supporting context，适合训练/研究，但真实无标注业务要换成弱监督、规则、reward model 或在线反馈。
- 最大两跳限制了更长链问题。
- Cosine similarity 是代理指标，不保证文档真正支持最终答案，也可能被关键词重合“钻空子”。
- 只在英文 QA 上验证，领域迁移和中文汽车知识仍需实验。

改进路线：curriculum（0/1 hop 到多 hop）、reward normalization、KL/entropy 控制、混合检索 + reranker、LLM judge 与可验证规则组合、多目标/约束 RL、GRPO 对照、Retriever-policy 交替优化、离线日志评测 + 小流量在线 A/B。

## 3. Multi-Agent Coding Harness 深挖

### 3.1 2 分钟项目介绍

> 一个最小 Agent Loop 只需要模型、工具 schema 和循环，但放到真实 Coding 场景会马上遇到副作用、长上下文、失败恢复、角色交接和验收可信度问题。我因此从零实现了一个 Python Multi-Agent Coding Harness。
>
> 底层是 LLMClient 适配层，把 OpenAI/Anthropic 的消息、tool schema、tool call 和 tool result 转成统一内部协议；Agent Runtime 负责 Tool Calling 循环、Hook、Recovery、Context 和 Event；能力层提供文件、命令、测试、Skill、Memory、MCP 等工具；上层用 PM-Engineer-QA-Acceptance 的确定性 Workflow 编排，并用 PRD、TaskSpec、ImplementationReport、TestReport 等 Artifact 做结构化交接。
>
> 项目最关键的设计是把“模型叙述”和“机器事实”分开。Engineer 说改完不能算完成，要看 phase-local Git diff；QA 说 pass 也不能算通过，要看当前阶段结构化测试事件。因此我加入 Evidence Gate、Git Worktree 隔离、Checkpoint Resume 和 Trace。系统目标不是让模型永不犯错，而是让错误能被限制、发现、恢复和审计。

### 3.2 一次 Tool Calling 的完整数据流

```text
User message
  -> ContextManager 预算/压缩
  -> BeforeLLM Hook 注入任务、记忆、Artifact 等运行时上下文
  -> LLMClient 把统一消息和 ToolSpec 转为 provider 格式
  -> LLM 返回文本或 Tool Call(name, arguments, id)
  -> 参数解析与 JSON Schema/权限检查
  -> ToolRegistry 找到执行函数
  -> PreToolUse Hook：工作区边界、危险命令、人工审批
  -> 执行工具，得到结构化 result
  -> PostToolUse Hook + Trace
  -> 按 provider 协议回灌 tool result
  -> 下一轮决策，直到 final 或达到 step budget
```

要点：Tool schema 是“可以怎么调用”的契约；Tool Call 是模型生成的具体实例；Runtime/Registry 才是实际执行者。模型从不直接执行函数。

### 3.3 OpenAI 与 Anthropic Tool Calling 如何统一

适配层统一成：

```text
LLMResponse(content, tool_calls, stop_reason, usage, raw)
LLMToolCall(id, name, arguments)
```

主要差异：system message 放置、工具 schema 包装、tool call block、stop reason、usage，以及 tool result 回灌格式。Anthropic 同一轮多个 tool result 需要组合在一条 user content 中；OpenAI 通常是按 `tool_call_id` 关联的 tool message。Provider 分支只留在 LLMClient，Agent Loop 不感知供应商。

### 3.4 为什么用官方 SDK，不手写 HTTP

> 官方 SDK 已处理连接、超时、错误类型和协议演进。我只在边界做最小归一化，既保留 provider 的错误语义和 raw response 便于排障，又避免 SDK 对象污染上层。手写 HTTP 会重复维护协议细节，兼容 endpoint 也容易漏字段。

### 3.5 Agent、Workflow、Multi-Agent 的区别

- **Agent**：LLM 在循环中根据状态动态决定下一步行动，路径不完全预定义。
- **Workflow**：控制流主要由代码确定，节点、边和失败分支可预测。
- **Multi-Agent**：多个拥有不同上下文/角色/工具的 Agent 协作；它不是目的，只有当任务分解、并行探索或权限隔离的收益超过通信成本时才值得。

Harness 是“阶段外确定性、阶段内 Agent 化”：Orchestrator 固定 PM→Engineer→QA→Acceptance；每个角色内部用 Agent Loop 解决开放任务。

### 3.6 为什么串行，不并行

> 当前核心问题是证据一致性和交付可靠性，而不是吞吐。并行会引入任务抢占、共享状态竞争、代码冲突、取消传播和成本放大。串行已经能验证角色隔离、Artifact 协议、测试反馈闭环和恢复。未来只有在评测证明并行探索确实提高成功率或时延时，才为每个分支增加独立 Worktree 和 merge gate。

### 3.7 Task 和 Artifact 为什么分开

- **Task 是控制面**：谁做什么、状态、依赖、优先级。
- **Artifact 是数据面**：PRD、TaskSpec、实现报告、测试报告、验收报告，表达可持久化和可校验的交付物。

只用消息会导致恢复后缺约束；只用 Task 又不能表达阶段产物。结构化 Artifact 让 gate 能检查“该阶段是否真的交付了规定内容”。

### 3.8 Evidence Gate 是什么

> Evidence Gate 是在 Orchestrator 中对模型声明和外部事实做一致性校验。实现阶段以 phase-local Git diff 为权威；QA 阶段以结构化 `run_tests`/`run_lint` 事件为权威；验收阶段不能覆盖 QA 的最终状态。

典型事故：QA 的 TestReport 写 pass，但同阶段测试事件失败。系统把它降级为 fail。另一次 Engineer 写 `outcome=changed`，但本轮 diff 没有变化，也会被拒绝。

这体现的通用原则：**LLM 负责提出和解释，Harness 负责验证和裁决。**

### 3.9 为什么用 Git Worktree

- 主工作区不被未验收修改污染。
- 同一 Workflow 的 Engineer、QA 和修复轮共享连续文件状态。
- 可比较 phase-local diff、生成 patch，并在最终由用户显式 apply。
- 比仅靠 prompt 说“不要改主分支”更可靠。

边界：Worktree 是代码目录隔离，不是不可信代码的容器沙箱；命令执行仍需要权限、路径保护和环境隔离。

### 3.10 Context、Checkpoint、Memory 的区别

| 层 | 保存什么 | 生命周期 |
|---|---|---|
| Context | 当前推理需要的近期对话、工具结果摘要 | 单次 run/会话 |
| Checkpoint / workflow state | 当前 phase、attempt、Artifact id、Worktree、失败和下一步 | 单个 workflow，可跨进程 |
| Long-term Memory | 稳定偏好、项目约定、验证过的经验 | 跨任务/跨会话 |

不要把失败尝试全部写长期 Memory，否则会把未验证结论变成未来“事实”。短期 retry 由 bounded attempt handoff 解决。

### 3.11 Context 压缩如何保证 Tool Call 完整

Assistant Tool Call 和对应 Tool Result 是原子消息组，压缩或裁剪不能拆开；否则 provider 会拒绝消息，模型也失去动作-观测因果链。优先压缩长工具输出为结构化摘要，保留最近用户目标、失败原因、文件路径、exit code 和 next step；再必要时由 LLM 总结更早历史。

### 3.12 Permission Hook 如何设计

权限不是只看工具名，而要看工具 + 参数 + 作用域：

- read/search 通常低风险；
- write/edit 限制在工作区；
- bash 解析工作目录、命令类别和危险模式；
- 删除、覆盖、外部写操作需要审批；
- QA 不给源码写权限；
- MCP 工具也经过同一 Hook 和 Trace。

安全原则：默认拒绝越界路径，参数校验失败不执行，敏感内容 Trace 脱敏，副作用操作幂等或可恢复。

### 3.13 MCP 如何解释

MCP 是 Host/Client 与外部 Server 之间的工具和资源协议，不是模型直接连接 Server：

```text
MCP Server exposes tools/list + tools/call
  -> Harness 内的 MCP Client 建立连接并发现 tool schema
  -> 转换为内部 ToolDefinition
  -> LLM 看到 schema，生成普通 Tool Call
  -> Harness 经权限检查后调用 MCP Server
  -> 结果回灌 LLM
```

MCP 解决标准化接入，不自动解决权限、业务语义、可靠性或沙箱。远程工具仍要走 Registry、Hook、Trace 和超时/重试。

### 3.14 如何评测 Agent

不要只看“最后回答像不像成功”。至少四层：

1. **任务结果**：任务完成率、外部 contract、pytest/ruff、代码 diff 正确性。
2. **过程可靠性**：工具失败恢复率、resume 成功率、假 pass 拦截率、人工审批次数。
3. **效率**：wall time、LLM calls、input/output tokens、工具调用数、压缩次数、成本。
4. **安全与体验**：越权拦截、敏感信息、p95 时延、用户中断/恢复、可解释证据。

评测数据集应包含 bugfix、refactor、feature 和不需要改代码的任务；外部 checker 要冻结，不能让模型自己写测试再自证。

### 3.15 当前项目完成度怎么说

推荐话术：

> 核心 Runtime、Tool Calling、Hook、Context/Memory、Trace、Workflow、Artifact、Worktree 和 Checkpoint 都已经有代码和模块测试；我也做了 self-hosting 端到端评测。当前仍有两类工作：一类是依赖和环境隔离，例如最近全量测试会被 `serpapi` SDK 导入兼容问题阻断；另一类是复杂端到端任务的成功率和 token 效率。我的重点不是掩盖失败，而是让 Harness 能准确识别失败，并用 Trace 定位是模型、环境、Harness 还是 benchmark 的问题。

这比“全部完成、全部通过”更可信，也能自然引出你的工程判断。

## 4. Agent 高频知识

### 4.1 常见 Agent 范式

| 范式 | 核心 | 适合场景 | 风险 |
|---|---|---|---|
| ReAct | 思考/决策-行动-观察循环 | 工具使用、开放探索 | 循环、成本、错误累积 |
| Plan-and-Execute | 先计划再逐步执行 | 长任务、依赖明确 | 计划过时，需动态重规划 |
| Router | 先分类再路由到专用链路 | 多意图、客服 | 路由错误传导 |
| Orchestrator-Worker | 中央拆分并调度 Worker | 多模块、多角色 | 通信与聚合成本 |
| Evaluator-Optimizer | 生成-评价-修订 | 质量可打分的内容/代码 | 自评偏差、无限修订 |
| Parallelization | 多候选并行再聚合 | 搜索、投票、方案对比 | 成本和冲突 |

### 4.2 Agent 生产化的八个关键点

1. Tool schema 清晰、参数强校验。
2. 状态显式，不把所有东西塞 messages。
3. 幂等、超时、重试、熔断和降级。
4. 高风险动作 Human-in-the-loop。
5. 结构化输出和机器 Evidence。
6. Trace、指标、回放与离线评测。
7. Context budget、缓存、摘要和检索式 Memory。
8. 权限最小化、租户隔离、Prompt Injection 防护。

### 4.3 LangGraph 怎么回答

> LangGraph 是低层的有状态 Agent/Workflow 编排 Runtime。核心抽象是 State、Node 和 Edge；节点读取状态并返回增量更新，条件边负责动态路由，Checkpointer 保存每一步状态，Interrupt 支持 Human-in-the-loop 和恢复。它适合长运行、需要持久化、显式控制流和可恢复的任务。

你的 Harness 映射：`SerialCodingWorkflow ≈ graph topology`；`Workflow checkpoint ≈ checkpointer`；`Role Agent ≈ node/subgraph`；`Permission approval ≈ interrupt`；`Trace ≈ observability`。区别是你重点自定义了 coding 工具、Worktree、Artifact 和 Evidence Gate。

如果问为何没直接用：

> 我希望先把 provider 协议、Tool Call 原子性、副作用和 Evidence 边界吃透，所以核心 Runtime 自研。未来拓扑复杂时可以用 LangGraph 承担编排和 checkpoint，但 Worktree、安全和机器验收仍然是业务自定义层，框架不会自动解决。

### 4.4 Dify、RAGFlow 怎么区分

- **Dify**：偏 LLM 应用平台和低代码交付，适合快速搭建 Chatflow/Workflow、Agent、知识库、模型和工具集成，并通过 API 发布。优势是业务团队迭代快；深度定制、复杂状态和细粒度代码控制相对弱。
- **RAGFlow**：重点是复杂文档的深度解析、切分、检索和带引用回答，同时也提供图式 Agent/Workflow。适合 PDF、Word、表格、图片、PPT 等知识库场景。
- **LangGraph**：代码优先、低层编排，适合工程团队实现复杂有状态流程。

回答经验边界：

> 我的深度实战主要在自研 Harness 和 ARF-RAG，但它们的核心抽象可以直接迁移到 LangGraph/RAGFlow/Dify。入职后如果团队已有平台，我会优先复用，而不是为了展示自研重复造轮子。

## 5. RAG 高频知识

### 5.1 标准 RAG 数据流

```text
文档接入
 -> 解析/OCR/清洗/去重
 -> 结构感知切分 + metadata
 -> embedding + 向量索引，必要时 BM25 倒排索引
 -> query rewrite/decompose/filter
 -> dense + sparse hybrid recall
 -> metadata filter
 -> cross-encoder/LLM rerank
 -> 去重、压缩、上下文组装
 -> LLM 生成 + 引用
 -> 日志、评测、反馈回流
```

### 5.2 BM25、Dense、Hybrid、Reranker

- **BM25**：词项精确匹配强，适合型号、零件号、专有名词；依赖词面，语义泛化弱。
- **Dense Retrieval**：双塔把 query/doc 编码到向量空间，语义召回强；可能错过精确 token，且 domain shift 明显。
- **Hybrid**：并行 BM25 + dense，使用加权或 RRF 合并，提高召回鲁棒性。
- **Reranker**：Cross-encoder 联合编码 query-doc，精度高但昂贵，只对召回的较小候选集重排。

RRF：`score(d) = sum_i 1 / (k + rank_i(d))`，不要求不同检索器分数可比。

### 5.3 Chunk 怎么定

不是越大越好：

- 太小：语义和上下文断裂，多跳关系丢失。
- 太大：向量表达稀释，召回不精确，占用上下文和推理成本。
- 真实方案：按标题/段落/表格/代码结构切分，适度 overlap；父子 chunk；保留文档、章节、版本、权限、车型等 metadata；通过 Recall@k 和端到端答案指标调参。

### 5.4 RAG 怎么评测

检索层：Recall@k、Precision@k、Hit Rate、MRR、nDCG、支持证据覆盖率。  
生成层：EM/F1、answer relevance、faithfulness/groundedness、citation correctness/completeness。  
系统层：端到端成功率、p95 latency、token/检索成本、空召回率、拒答准确率、线上投诉或任务完成率。

必须把 retrieval 和 generation 分层，否则答错时无法知道是“没召回、重排错、上下文污染，还是模型没利用证据”。

### 5.5 常见问题与解决

| 问题 | 诊断 | 解决 |
|---|---|---|
| 召回不到 | gold passage 不在 top-k | 清洗/切分、query rewrite、hybrid、domain embedding、扩大候选 |
| 召回有但排序低 | recall 有、MRR/nDCG 低 | reranker、metadata、hard negative 训练 |
| 上下文有答案仍答错 | evidence utilization 低 | prompt、位置重排、context compression、SFT、引用约束 |
| 多轮问题歧义 | 当前 query 缺上下文 | history-aware rewrite，但避免把全历史直接拼入 |
| 幻觉 | 空召回仍强答、冲突文档 | score/证据阈值、拒答、引用验证、冲突检测 |
| 知识过期 | 旧版本被召回 | 版本 metadata、增量索引、TTL、变更日志 |
| 权限泄漏 | 越权文档进入候选 | 检索前权限过滤，不只生成后屏蔽 |
| Prompt Injection | 文档含恶意指令 | 数据/指令分离、内容标记、工具权限、输出验证 |

### 5.6 汽车业务 RAG 的特殊点

- 车型、年款、地区、配置、软件版本必须做 metadata filter。
- 维修手册/法规/故障码常含表格、图片和层级结构，需要版面解析和多模态。
- 安全问题要证据优先、版本可追溯、低置信度拒答或转人工。
- 座舱需要低时延、弱网/离线降级、语音噪声鲁棒性和驾驶安全约束。
- 售后知识频繁更新，要增量索引、回滚和生效时间管理。

## 6. LLM 基础与后训练

### 6.1 Transformer Decoder 数据流

```text
token ids -> embedding + position
 -> [Norm -> causal self-attention -> residual
     -> Norm -> FFN/SwiGLU -> residual] x N
 -> final norm -> LM head -> logits -> softmax
```

Attention：

```text
Q = XWq, K = XWk, V = XWv
Attention(Q,K,V) = softmax(QK^T / sqrt(d_k) + causal_mask) V
```

除以 `sqrt(d_k)` 是为了控制点积方差，避免维度大时 softmax 饱和、梯度变小。多头让不同子空间学习不同关系。Causal mask 保证当前位置看不到未来 token。

### 6.2 RoPE、RMSNorm、SwiGLU、GQA

- **RoPE**：对 Q/K 施加随位置变化的旋转，使点积包含相对位置信息；长上下文外推仍需 scaling/训练配合。
- **RMSNorm**：按均方根归一化，不减均值，计算更简单。
- **SwiGLU**：门控 FFN，通常比普通 ReLU/GELU FFN 表达更强。
- **GQA**：多个 query heads 共享较少的 K/V heads，降低 KV cache 和推理带宽，介于 MHA 与 MQA 之间。

### 6.3 预训练、SFT、对齐

- **Pretraining**：海量语料 next-token prediction，学通用知识和语言能力。
- **Continued pretraining**：在领域无标注语料继续 next-token 训练，注入领域分布。
- **SFT**：用高质量指令-回答训练行为格式和任务能力。
- **Preference/RL alignment**：用人类/规则/模型偏好优化有用性、安全性和推理策略，例如 PPO、DPO、GRPO。

SFT 学“模仿什么”，RL/Preference 学“多个可能输出中偏好什么”。

### 6.4 LoRA

冻结原权重 `W`，只训练低秩增量：

```text
W' = W + (alpha / r) * B A
A in R^(r x d_in), B in R^(d_out x r), r << d
```

优点：训练参数、显存和 checkpoint 小；可为不同任务挂载多个 adapter。局限：rank/目标层选择影响上限；多 adapter 管理复杂；它减少可训练参数，但激活和部分优化开销仍在，不等于训练显存按参数比例线性下降。

### 6.5 DPO 与 GRPO

DPO 直接从偏好对 `(y_w, y_l)` 优化 policy 相对 reference 的 log-prob 差，不需要显式 reward model 和在线 rollout；稳定简单，但依赖偏好数据质量，且主要适合完整输出偏好。

GRPO 对同一 prompt 采样一组输出，用组内 reward 的相对标准化值作 advantage，省去 value model；适合有可验证 reward 的推理任务，但 group rollout 成本高，reward 方差和模式坍塌仍需控制。

### 6.6 训练稳定性问题

- 学习率过大、梯度爆炸：gradient clipping、warmup、scheduler、监控 grad norm。
- 长序列 OOM：gradient checkpointing、FlashAttention、sequence packing、ZeRO/FSDP、降低 batch/length。
- Reward hacking：多维 reward、规则校验、held-out evaluator、对抗样本、人工审计。
- Policy drift / KL 爆炸：KL penalty、clip range、early stop、reference policy。
- 灾难性遗忘：混合通用数据、较小 LR、LoRA、回归评测。
- 数据污染/泄漏：去重、时间切分、训练/测试实体检查。

## 7. 推理与 vLLM

### 7.1 Prefill 与 Decode

- **Prefill**：一次处理全部输入 token，计算量大、并行度高，主要影响 TTFT。
- **Decode**：每步生成一个 token，复用 KV cache，通常受显存带宽和调度影响，主要影响 TPOT/ITL。

KV cache 保存每层历史 token 的 K/V，避免每次 decode 重算；上下文长、并发高时它会成为显存瓶颈。

### 7.2 PagedAttention 与 Continuous Batching

- **PagedAttention**：把每个请求的 KV cache 拆成固定大小 block，通过类似虚拟内存分页的方式映射，减少连续大块预留、内外碎片和冗余复制，并支持 block 共享。
- **Continuous batching**：不是等一整个静态 batch 全部结束，而是在迭代级别动态加入新请求、移除已完成请求，提高 GPU 利用率和吞吐。

vLLM 还常用 prefix caching、chunked prefill、量化、tensor/pipeline/data parallel、speculative decoding，并提供 OpenAI-compatible server。

### 7.3 部署指标

TTFT（首 token 时延）、TPOT/ITL（每 token/相邻 token 时延）、tokens/s、requests/s、并发数、p50/p95/p99、GPU utilization、KV cache usage、OOM/超时率、prefix cache hit rate、单位请求成本。

优化是多目标权衡：提高 batch 常提升吞吐，但可能损害单请求时延；量化节省显存和提高吞吐，但要回归精度和不同硬件 kernel 支持。

## 8. 业务落地题

### 8.1 如果让你做车主手册/售后助手

回答框架：

1. 先定义场景和风险等级：普通功能问答、故障提示、维修建议分级。
2. 数据治理：车型/年款/地区/版本/生效日期/权限 metadata，解析 PDF 表格和图片。
3. 检索：历史感知 query rewrite，BM25 + dense hybrid，metadata filter，rerank。
4. 生成：引用证据、冲突文档处理、低置信度拒答/转人工；安全类答案使用模板和规则兜底。
5. 评测：gold QA + evidence，Recall@k、faithfulness、拒答准确率、延迟；按车型和问题类别切片。
6. 上线：shadow traffic、小流量 A/B、日志脱敏、反馈闭环、索引版本和回滚。

### 8.2 如何提升用户体验

不要只说“换更大模型”。按漏斗诊断：

```text
用户问题 -> 意图理解 -> 检索 -> 重排 -> 上下文利用
        -> 生成 -> 工具执行 -> 端到端时延/满意度
```

建立失败 taxonomy：答非所问、缺知识、知识过期、幻觉、工具失败、时延、拒答不当。结合离线集、Trace 和线上埋点，找到最大损失环节；每次只改变少数变量，通过 A/B 或回放评测确认收益。

### 8.3 LLM 输出不稳定怎么办

- 温度/采样参数和 seed 用于可控实验，但不能保证所有 GPU/batch 完全确定。
- 结构化输出 + schema validation + constrained decoding。
- 对关键业务使用规则、状态机和 deterministic workflow。
- 工具幂等、超时、重试、fallback；高风险动作审批。
- 输出事实校验、引用和拒答；重复请求缓存。
- 建立回归集，不用单个 demo 判断稳定性。

### 8.4 如何处理跨团队协作

用美团经历回答：

> 我会先把业务目标翻译成可验收指标和接口边界，例如首屏时延、转化漏斗或稳定性指标；技术方案评审时明确客户端、服务端、算法、产品的 owner、依赖和灰度计划；开发中通过 mock/联调环境提前暴露协议问题；上线前准备监控、回滚和数据看板；上线后按埋点验证，而不是只以“代码合并”作为完成。

映射到大模型项目：算法离线指标、平台服务 SLA、业务成功率和安全指标必须共同验收。

## 9. 为什么吉利、为什么这个岗位

### 9.1 推荐回答

> 我关注到吉利正在从模块化智能向 Full-Domain AI 的统一架构推进，覆盖智能座舱、底盘、安全、驾驶以及研发和售后等产品周期。这个方向对大模型岗位提出的不只是训练一个模型，而是让模型在安全、时延、隐私和业务指标约束下真正落地。
>
> 我的匹配点有三层：ARF-RAG 对应模型后训练、自适应检索和评测；Agent Harness 对应工具编排、状态恢复、权限和可观测性；美团经历对应跨团队和线上业务。我希望参与的是既有算法深度、又能被真实用户使用的项目，这也是我选择汽车大模型场景的原因。

### 9.2 可提出的落地方向

- 智能座舱：多轮语音助手、车控 Tool Calling、个性化但受安全约束的 Agent。
- 售后/客服：按车型和版本过滤的可信 RAG、故障知识问答、转人工。
- 研发/制造：规范、故障案例、代码和测试知识助手；多 Agent 但以机器 Evidence 验收。
- 多模态：结合语音、图像、车辆状态和手册证据；边云协同与隐私保护。

不要猜团队一定做哪一个；用反问确认。

## 10. 行为面试答案

### 10.1 最困难的问题

首选 Harness Evidence Gate：

> 困难不是把 Agent 跑起来，而是判断它是否真的完成。早期 QA 报告 pass，但底层测试事件失败；Engineer 也可能报告 changed，但本轮没有 diff。我把 Artifact 从“事实”降为“声明”，引入 phase-local Git diff 和结构化 verifier 作为权威证据，再让 Orchestrator 做一致性 gate。这个问题让我认识到 LLM 系统的关键不是要求模型永不出错，而是建立可证伪的闭环。

### 10.2 做过的一个取舍

> Harness 当前选择串行 Workflow 而不是并行 Agent Team。并行看起来更先进，但在 coding 场景会引入代码冲突、状态竞争和成本放大。当时评测的主要瓶颈是证据一致性和上下文效率，所以我先用串行 PM-Engineer-QA 打通可靠闭环。我的原则是先围绕主要风险设计，再用数据决定是否增加复杂度。

### 10.3 失败经历

> Self-hosting 评测中，Agent 为自己的 Harness 开发 API 服务，候选代码最终没有通过完整外部 contract。复盘发现三类问题：模型调用了不存在的接口，Harness 的上下文重复注入导致 token 成本过高，benchmark 本身也有依赖缺陷。我没有把失败统一归因给模型，而是拆成 candidate、Harness 和 benchmark 三类，并据此加强 Evidence Gate、attempt handoff 和环境隔离。失败的价值是让系统更会识别和定位失败。

### 10.4 你的不足

> 我目前最深的实践是 Python、RAG/RL 和 Agent Runtime，大规模在线推理集群和车端部署经验还不如长期做基础设施的同学。我的弥补方式是把问题拆成可验证的系统指标，例如 TTFT、TPOT、吞吐、KV cache、降级和回滚，并结合 vLLM 等现有技术快速补齐。另一方面，我的优势是既能理解训练目标，也能落到工程和业务验收。

## 11. 高频快问快答

### 11.1 为什么 RAG 仍会幻觉

检索不到、检索错、上下文冲突、模型没利用证据，或在证据不足时仍强答。RAG 只是提供外部证据，不保证 grounding；需要检索评测、引用、拒答、冲突检测和生成校验。

### 11.2 Temperature 的本质

对 logits 除以 `T` 后 softmax。`T<1` 放大差异、更确定；`T>1` 拉平分布、更多样；`T->0` 接近 greedy。它改变采样分布，不改变模型参数，也不是“创造力旋钮”的完整解释。

### 11.3 Top-k 与 Top-p

Top-k 只保留概率最高的 k 个 token；Top-p 保留累计概率达到 p 的最小集合，候选数会随分布变化。二者都在重归一化后采样。

### 11.4 Embedding 怎么训练

通常用对比学习，让正 query-doc 相似度高、负样本低；InfoNCE/softmax 对 batch 内负样本优化。Hard negatives 对排序边界重要，但伪负样本会伤害训练。

### 11.5 为什么 Retriever 指标高不等于 RAG 高

Retriever 的“相关”不一定是 Generator 最需要的证据；文档可能冗余、冲突或位置不友好；模型也可能不会利用。需要以端到端答案和 faithfulness 共同优化，这正是 ARF-RAG 的动机。

### 11.6 长上下文能否替代 RAG

不能完全替代。长上下文减少切分/召回遗漏，但输入成本、时延、位置效应、权限和知识更新仍存在。实际常用 RAG 先筛选，再用足够长的上下文综合证据。

### 11.7 Tool Calling 与普通 JSON 输出差别

Tool Calling 是 provider 支持的结构化协议，模型输出工具名和参数，Host 通过 call id 回灌结果；普通 JSON 只是文本格式约束，没有工具生命周期、执行和结果关联语义。

### 11.8 Prompt Injection 怎么防

把外部文档视为不可信数据；system/tool policy 高于文档；不让检索内容直接扩权；工具最小权限；敏感操作审批；对外部内容做来源标记和检测；输出/参数再校验。只写一句“忽略恶意指令”不够。

### 11.9 Agent Memory 怎么做

短期 memory 是当前状态和对话；长期 memory 要结构化抽取、打分检索、置信度/来源、更新与遗忘。不能无选择地把全部聊天写入向量库，否则噪声和错误会累积。

### 11.10 怎么选模型

先按任务需要确定能力下限，再比较准确率、工具调用、上下文、时延、吞吐、成本、部署和合规。复杂路由可用小模型分类/抽取，大模型处理困难 case；不是所有请求都上最大模型。

## 12. 反问环节

优先问 2 个，不要全问。

1. **业务与指标**：这个岗位目前最核心的落地场景更偏智能座舱、研发/售后知识系统，还是模型后训练平台？团队判断项目成功最看重哪些离线和线上指标？
2. **工作边界**：算法同学在一个项目中通常负责到哪一层——数据和后训练、RAG/Agent 编排、推理部署，还是也会参与产品灰度和线上分析？
3. **技术挑战**：当前团队在大模型落地中最大的瓶颈更偏模型效果、数据闭环、推理成本，还是车端安全与时延？
4. **新人预期**：如果我加入，前三到六个月最希望我独立交付什么类型的结果？
5. **评测体系**：团队现在如何把模型离线指标和真实用户体验连接起来？是否有固定回归集、Trace、A/B 或人工审核机制？

不建议第一轮就问薪资、加班和晋升细节；除非面试官主动进入这些话题。

## 13. 面试前 45 分钟冲刺顺序

### 前 15 分钟

朗读 90 秒自我介绍两遍；闭眼复述：两个项目一句话、ARF-RAG MDP、奖励公式、三个绝对结果。

### 中间 15 分钟

复述四题：为什么 PPO、为什么冻结 Retriever、Evidence Gate、Context/Checkpoint/Memory 区别。

### 后 10 分钟

过一遍 Transformer attention、LoRA、PPO clip、RAG pipeline、PagedAttention/continuous batching。

### 最后 5 分钟

确认设备、网络、简历；准备纸笔，只写五个提示词：

```text
研究 + 工程 + 业务
问题 -> 设计 -> 证据 -> 局限
绝对值：46.6 / 34.7 / 36.4
LLM 提议，机器证据裁决
反问：场景、指标、前三个月交付
```

## 14. 官方资料速查

- LangGraph：State/Node/Edge、durable execution、persistence、Human-in-the-loop：<https://docs.langchain.com/oss/python/langgraph/overview>
- Dify Knowledge Retrieval：<https://github.com/langgenius/dify-docs/blob/main/en/cloud/use-dify/nodes/knowledge-retrieval.mdx>
- RAGFlow Quickstart 与 Agents：<https://ragflow.net/docs>、<https://ragflow.net/docs/category/agents>
- vLLM：PagedAttention、continuous batching、OpenAI-compatible serving：<https://docs.vllm.ai/en/stable/index.html>
- 吉利 Full-Domain AI 2.0：<https://global.geely.com/en/news/2026/geely-ces-2026-full-domain-ai>

## 15. 最后的表达原则

1. 先给结论，再给机制；一句话能答清，不先讲五分钟背景。
2. 任何项目题都按“问题 -> 设计 -> 为什么 -> 证据 -> 局限”回答。
3. 不熟的框架不要装熟：讲清核心抽象、与自己项目的映射、快速迁移路径。
4. 数字只报记得准的绝对值；相对提升被追问时再解释口径。
5. 遇到没做过的问题：先说明边界，再给工程化解决思路和验证指标。
6. 把“我用了很多模块”改成“我解决了什么风险，并用什么证据证明”。

