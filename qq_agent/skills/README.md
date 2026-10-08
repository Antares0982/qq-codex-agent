# 内置技能

在此添加 `<技能名>/SKILL.md`，文件须包含 YAML 元数据 `name` 和 `description`。
技能目录内可包含 `scripts/`、`references/`、`assets/` 等资源，随 Python 包一起发布。
此目录不是 Python 包，不需要 `__init__.py`。

服务启动时通过 Codex `skills/extraRoots/set` 注册本目录；所有会话均可发现内置技能。
各会话仍自动发现自己工作区下的 `.agents/skills/<技能名>/SKILL.md`，`/new` 保留这些文件。
使用不同技能名避免歧义；是否调用由任务匹配或用户明确指定决定。

内置技能随应用升级和回滚，Nix 部署中只读；修改后重新部署并重启服务。
工作区 `.agents/skills/` 及其全部资源不参与 14 天文件清理。

`harness-maintenance` 用于维护 agent 自身的 harness，通过已认证的 agent 专用 GitHub
账户 fork 上游仓库、从主分支最新 HEAD 开发并提交 PR；未认证时终止。
