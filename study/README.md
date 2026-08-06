# LLM Agent 学习与面试文档索引

这个目录记录项目的设计依据、实现细节、评测复盘和秋招面试准备。文档以当前代码为准，不把“计划实现”写成“已经实现”。

## 推荐阅读顺序

1. [`llm_agent_project_review.md`](llm_agent_project_review.md)：项目总览与 3 分钟主讲稿。
2. [`modules/01_runtime_and_provider.md`](modules/01_runtime_and_provider.md)：LLM 适配、Agent Loop、Hook、Recovery 与后台任务。
3. [`modules/02_context_memory_and_skills.md`](modules/02_context_memory_and_skills.md)：上下文压缩、三层状态、长期记忆与 Skill。
4. [`modules/03_tools_security_and_observability.md`](modules/03_tools_security_and_observability.md)：Coding Tools、安全、验证、Trace 与 MCP。
5. [`modules/04_collaboration_and_workflow.md`](modules/04_collaboration_and_workflow.md)：Task、Artifact、Subagent、Worktree 与串行 Workflow。
6. [`modules/05_evaluation_and_incidents.md`](modules/05_evaluation_and_incidents.md)：真实评测中暴露的问题、修复过程与剩余风险。
7. [`modules/06_interview_question_bank.md`](modules/06_interview_question_bank.md)：按追问深度整理的面试题与回答要点。

专题文档：

- [`task_system_design.md`](task_system_design.md)：Task System 的数据模型、分层和面试问题。
- [`development_todo.md`](development_todo.md)：项目目标、阶段进度和后续收尾事项。

## 文档口径

- **已实现**：代码中已经存在，并有对应调用链。
- **已测试**：单元或集成测试覆盖了主要行为。
- **评测验证**：在自托管 coding workflow 中真实触发过。
- **仍有限制**：项目明确保留的边界，面试中不夸大。

阅读源码时建议从 `main.py` 的依赖装配开始，再沿下面两条主链路展开：

```text
交互链路：main.py -> Agent -> LLMClient -> ToolRegistry -> Hook/Trace
工作流链路：SerialCodingWorkflow -> Role Agent -> Artifact/Task -> Worktree -> Evidence Gate
```
