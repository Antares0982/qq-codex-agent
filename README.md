# QQ Codex Agent

部署在树莓派上的个人 QQ AI 助手，通过 NapCat OneBot 11 连接 QQ，使用本地 Codex app-server 和 ChatGPT 登录。

支持自然语言问答、图片理解、图片生成和代码执行；读取运行时 AGENTS.md 定制提示词，使用 Approve for me 自动审批及受限文件系统。

- 私聊与各群独立授权，默认拒绝访问；群内支持指定用户或全员使用，须 @ bot。
- 私聊独立会话，同群共享上下文和模型；群聊仅发送图片与最终文字，指令保留直接回复。
- 支持 `/help`、`/model`、`/status`、`/new`、`/stop`，推理强度固定为 `medium`。
- 通过 NixOS flake 部署，使用 agenix 管理白名单，Pi 无需单独 clone 本仓库。

开发、部署、登录及排障方法见 [AGENTS.md](AGENTS.md)。运行时提示词模板见 [AGENTS.runtime.md](AGENTS.runtime.md)。
