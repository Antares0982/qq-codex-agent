# QQ Codex Agent

部署在树莓派上的个人 QQ AI 助手，通过 NapCat OneBot 11 连接 QQ，使用本地 Codex app-server 和 ChatGPT 登录。

支持自然语言问答、图片理解、图片生成和代码执行；读取运行时 AGENTS.md 定制提示词，使用 Approve for me 自动审批及受限文件系统。

- 私聊与各群独立授权，默认拒绝访问；群内支持指定用户或全员使用，须 @ bot。
- 私聊独立会话，同群共享上下文和模型；消息间隔超过两小时自动新建 thread，私聊提示、群聊静默。
- 群聊仅在 agent 调用发送工具时发送生成图片，另发送最终文字；指令保留直接回复。
- 生成原图自动保存在会话工作区；`qq_image.list_images` 查询路径，使用 Pillow 加工多帧后通过 `qq_image.send_image(path=...)` 发送最终 GIF。图片跨轮保留，`/new` 清理。
- 支持 `/help`、`/model`、`/status`、`/new`、`/stop`，推理强度固定为 `medium`。
- 本仓库维护 Nix 包、服务模块及默认提示词；dotfile 维护宿主配置和版本锁，使用 agenix 管理白名单，Pi 无需单独 clone 本仓库。

两仓库接入、提示词覆盖及部署检查见 [nix/README.md](nix/README.md)。

开发、部署、登录及排障方法见 [AGENTS.md](AGENTS.md)。公共运行时模板见 [AGENTS.runtime.md](AGENTS.runtime.md)，私聊和群聊模板分别见 [AGENTS.private.runtime.md](AGENTS.private.runtime.md) 与 [AGENTS.group.runtime.md](AGENTS.group.runtime.md)。
