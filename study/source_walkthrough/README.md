# 源码精读索引

本目录用于逐模块梳理当前项目的真实源码。它与 `study/modules/` 的分工不同：

- `study/modules/` 从系统架构和面试表达出发，回答“系统有什么能力”。
- `study/source_walkthrough/` 从类、函数和调用链出发，回答“代码具体如何工作，以及为什么这样写”。

文档以当前仓库源码和测试为准。已经实现、测试覆盖、评测验证和仍有限制的内容会分别说明，不把计划能力写成现有能力。

## 阅读方法

每个模块尽量沿同一顺序展开：

1. 模块目标与职责边界。
2. 源文件和依赖地图。
3. 数据结构、类、函数与关键变量。
4. 一条完整运行链路。
5. 与其他模块的协作方式。
6. 异常、恢复和边界条件。
7. 测试覆盖与阅读入口。
8. 上层设计、取舍和可替代方案。
9. 真实问题与修复过程。
10. 当前限制、演进方向和面试问题。

阅读时建议一边打开对应源码，一边跟随文档中的流程图和伪代码。源码行号会随改动变化，因此文档主要使用稳定的符号名定位。

## 模块进度

1. [`01_llm_client.md`](01_llm_client.md)：OpenAI/Anthropic SDK 适配、Tool Calling 协议转换、请求恢复与 Trace。
2. [`02_agent_loop.md`](02_agent_loop.md)：Agent 的 ReAct 状态机、Context/Hook/Tool 协作、恢复和事件输出。
3. [`03_context_manager.md`](03_context_manager.md)：System Prompt、请求上下文装配、工具结果治理、历史摘要和响应式压缩。
4. [`04_tool_system.md`](04_tool_system.md)：ToolRegistry、可靠 Coding Tools、结构化验证、能力裁剪与 MCP 动态接入。
5. [`05_hook_system.md`](05_hook_system.md)：Hook 生命周期、结果合并与短路、权限确认、运行时上下文注入及 Trace 协作。
6. [`06_security_system.md`](06_security_system.md)：路径与敏感文件、Shell 策略、子进程环境、角色能力和 Worktree/MCP 安全边界。
7. [`07_recovery_system.md`](07_recovery_system.md)：错误分类、传输重试、模型 fallback、上下文/输出恢复与 Workflow 恢复边界。

后续文档将继续覆盖 Memory、Task/Artifact、Subagent/Skill、Workflow/Worktree 与 Evaluation。
