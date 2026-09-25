# QQ Codex Agent

部署在树莓派上的个人 QQ AI 助手，通过 NapCat OneBot 11 连接 QQ，使用本地 Codex app-server 和 ChatGPT 登录。

支持自然语言问答、图片理解、图片生成和代码执行；读取运行时 AGENTS.md 定制提示词，使用 Approve for me 自动审批及受限文件系统。

- 私聊与各群独立授权，默认拒绝访问；群内支持指定用户或全员使用，须 @ bot。
- 各私聊、各群独立并发，处理中继续发送的消息通过 steering 追加到当前任务，在工具调用结束等可接收输入的位置处理。
- 私聊独立会话，同群共享上下文和模型；消息间隔超过两小时且上下文估算超过 50,000 tokens 时，在原 thread 压缩后再提交新消息；仅私聊显示压缩进度。`/new` 创建不携带历史的空白会话，保留工作文件。
- 自动压缩阈值为 100,000 tokens，`/compact` 可手动压缩空闲会话，保留原图和完整历史，不保证清除图片或降低总网络流量；成功追加消息后重置 15 分钟期限。空闲 thread 取消订阅后由 Codex 延迟回收。
- 群聊仅在 agent 调用发送工具时发送生成图片，群聊和私聊均在每条模型文字完成后按顺序发送，包括过程说明和最终回复；记录互动画像时提示“📝正在给{nickname}记进小本本……”，使用群名片，缺失时回退到昵称或 QQ 号；指令保留直接回复。
- 生成原图自动保存在会话工作区；`qq_image.list_images` 查询路径，使用 Pillow 加工多帧后通过 `qq_image.send_image(path=...)` 发送最终 GIF。每个聊天固定使用独立目录，图片跨轮及 `/new` 保留。服务启动及此后每 24 小时清理超过 14 天未访问或修改的文件，跳过忙碌聊天。
- 支持 `/help`、`/model`、`/status`、`/new`、`/stop`、`/compact`，推理强度固定为 `medium`。
- 群聊会从本人明确偏好和反复出现的交流习惯学习简短互动画像，按群隔离，仅更新当前发送者。每轮召回本人及被 @ 成员，重启、换 thread 和 `/new` 后保留；不监听未 @ bot 的聊天，不增加后台模型调用。
- `/profile` 在当前群公开显示本人画像；`/profile forget` 删除本人当前群画像，旧聊天历史仍保留，后续可能重新形成画像。两个命令均须获该群授权并 @ bot。
- 本仓库维护 Nix 包、服务模块及默认提示词；dotfile 维护宿主配置和版本锁，使用 agenix 管理白名单，Pi 无需单独 clone 本仓库。

两仓库接入、提示词覆盖及部署检查见 [nix/README.md](nix/README.md)。

开发、部署、登录及排障方法见 [AGENTS.md](AGENTS.md)。公共运行时模板见 [AGENTS.runtime.md](AGENTS.runtime.md)，私聊和群聊模板分别见 [AGENTS.private.runtime.md](AGENTS.private.runtime.md) 与 [AGENTS.group.runtime.md](AGENTS.group.runtime.md)。

消息收发、任务进度及真实错误可直接查看 `sudo journalctl -u qq-codex-agent -f -o cat`。日志包含聊天正文，避免公开分享；图片二进制不记录，常见凭据字段脱敏。
