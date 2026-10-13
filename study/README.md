# LLM Agent 学习与面试文档索引

这个目录记录项目的设计依据、实现细节、评测复盘和秋招面试准备。文档以当前代码为准，不把“计划实现”写成“已经实现”。

## 推荐阅读顺序

1. [`llm_agent_project_review.md`](llm_agent_project_review.md)：项目总览与 3 分钟主讲稿。
2. [`source_walkthrough/README.md`](source_walkthrough/README.md)：逐模块源码精读索引，从类、函数和真实调用链理解实现。
3. [`modules/01_runtime_and_provider.md`](modules/01_runtime_and_provider.md)：LLM 适配、Agent Loop、Hook、Recovery 与后台任务。
4. [`modules/02_context_memory_and_skills.md`](modules/02_context_memory_and_skills.md)：上下文压缩、三层状态、长期记忆与 Skill。
5. [`modules/03_tools_security_and_observability.md`](modules/03_tools_security_and_observability.md)：Coding Tools、安全、验证、Trace 与 MCP。
6. [`modules/04_collaboration_and_workflow.md`](modules/04_collaboration_and_workflow.md)：Task、Artifact、Subagent、Worktree 与串行 Workflow。
7. [`modules/05_evaluation_and_incidents.md`](modules/05_evaluation_and_incidents.md)：真实评测中暴露的问题、修复过程与剩余风险。
8. [`modules/06_interview_question_bank.md`](modules/06_interview_question_bank.md)：按追问深度整理的面试题与回答要点。

专题文档：

- [`source_walkthrough/01_llm_client.md`](source_walkthrough/01_llm_client.md)：`LLMClient` 的 SDK 适配、Tool Calling、恢复与 Trace 源码精读。
- [`source_walkthrough/02_agent_loop.md`](source_walkthrough/02_agent_loop.md)：`Agent` 的 ReAct Loop、上下文准备、工具执行、恢复和事件系统源码精读。
- [`source_walkthrough/03_context_manager.md`](source_walkthrough/03_context_manager.md)：`ContextManager` 的 Prompt 组装、token 预算、工具结果压缩与上下文恢复源码精读。
- [`source_walkthrough/04_tool_system.md`](source_walkthrough/04_tool_system.md)：Tool Registry、可靠 Coding Tools、结构化验证、能力裁剪与 MCP 接入源码精读。
- [`source_walkthrough/05_hook_system.md`](source_walkthrough/05_hook_system.md)：Hook 生命周期、权限确认、运行时上下文注入、结果替换及 Trace 协作源码精读。
- [`source_walkthrough/06_security_system.md`](source_walkthrough/06_security_system.md)：路径、Shell、环境变量、能力裁剪、Worktree 与 MCP 安全边界源码精读。
- [`source_walkthrough/07_recovery_system.md`](source_walkthrough/07_recovery_system.md)：错误分类、传输重试、fallback、上下文/输出恢复与 Workflow 恢复边界源码精读。
- [`source_walkthrough/08_memory_system.md`](source_walkthrough/08_memory_system.md)：三层记忆、长期存储与召回、反思、遗忘、Hook 注入及 Workflow 工作 checkpoint 源码精读。
- [`source_walkthrough/09_task_system.md`](source_walkthrough/09_task_system.md)：Task 数据模型、状态转换、依赖、持久化、规划提醒和 Workflow 任务契约源码精读。
- [`source_walkthrough/10_artifact_system.md`](source_walkthrough/10_artifact_system.md)：Artifact 存储、版本记录、工具与上下文、角色交接及报告证据校验源码精读。
- [`source_walkthrough/11_skill_system.md`](source_walkthrough/11_skill_system.md)：Skill 注册、缓存、目录与资源、角色注入及上下文/权限协作源码精读。
- [`source_walkthrough/12_serial_workflow.md`](source_walkthrough/12_serial_workflow.md)：角色装配、阶段推进、attempt 重试及缺陷修复闭环源码精读。
- [`source_walkthrough/13_workflow_evidence.md`](source_walkthrough/13_workflow_evidence.md)：Worktree、验证/报告快照、权威 QA verdict 和阶段 Evidence Gate 源码精读。
- [`source_walkthrough/14_workflow_persistence_and_resume.md`](source_walkthrough/14_workflow_persistence_and_resume.md)：Workflow 配置、持久化、工作 checkpoint 和 resume 源码精读。
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
