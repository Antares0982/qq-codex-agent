# 文档

## 行为契约

- [配置与权限](specs/configuration.md)：入口授权、配置约束和安全边界。
- [会话与命令](specs/sessions.md)：任务、指令、上下文、取消与恢复。
- [群聊协议](specs/groups.md)：消息节奏、时效、回复和故障语义。
- [图片与文件](specs/images.md)：图片工具、交付、资源保留。
- [记忆与提示词](specs/memory-prompts.md)：成员记录、群设定和更新时机。

## 内部说明

- [架构与 runtime](internal/architecture.md)：组件、持久状态和提示词注入。
- [开发与检查](internal/development.md)：环境、测试及打包验收。
- [部署与迁移](internal/deployment.md)：独立运行、NixOS、共享登录及回滚。
- [运维与排障](internal/operations.md)：日志、故障定位和 transcript 导出。
- [技能与资源维护](internal/resources.md)：技能发现、提示词资源和表情压缩。

仓库维护约定见 [AGENTS.md](../AGENTS.md)。运行时模板为 [公共](../AGENTS.runtime.md)、[私聊](../AGENTS.private.runtime.md)和[群聊](../AGENTS.group.runtime.md)，不属于上述维护文档。
