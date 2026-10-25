# 运维与排障

在 Pi 查看 `sudo journalctl -u qq-codex-agent -n 100 --no-pager`。
INFO 日志包含授权消息的发送者、正文、图片数量、引用消息 ID、回复正文、图片交付路径和大小，以及 thread/turn、工具状态、压缩结果和任务耗时。错误事件及终态错误直接进入 journal，超时与主动取消分别标注；不记录工具的图片 Base64 或认证头。常见 token、API key、密码字段会脱敏，文本换行转义；任意自然语言秘密无法保证自动识别。journal 含私聊与群聊内容，仅向受信任管理员开放，按宿主 journald 策略保留。
每轮开始记录所选模型（未指定时为 `default`）、推理强度和请求前上下文 token 估算，结束记录 turn ID。后台每 5 秒只读增量提取锁定 runtime 的 `codex/logs_2.sqlite`，将 Responses 请求的压缩前后字节数、压缩耗时、HTTP 状态码及可用的 request/thread/turn/model 标识写入 `Codex request diagnostic`；时间戳为原始日志的 Unix 秒，可用于比较成功与失败请求。仅输出白名单字段，不复制请求正文、URL 或响应头；启动时跳过已有记录，数据库不可用时警告并继续聊天。该提取器依赖 runtime 日志格式，升级时由本地模拟端点测试验证；未压缩请求没有压缩大小记录。
启动前检查失败会阻止服务运行，优先查看 traceback；不要通过删除沙箱检查或放宽目录权限绕过错误。
Codex 0.159.2 的多个文件级 deny-read 会触发 bubblewrap 文件描述符复用错误。
部署改用整个目录隔离，技能与插件 cache 放在 `/var/lib/qq-codex-resources`；
`CODEX_HOME/tmp/arg0` 只读，让官方 runtime 使用自身可执行文件回退入口。
PATH aliases 的只读警告是预期现象；必须以实际 thread、沙箱和资源读取自检为准。
不要开放私有状态或关闭沙箱。详情见 [部署文档](deployment.md)。
遇到 `Unknown allowlist fields`，确认完整配置已通过新模块的 `configFile` 接入而非旧白名单入口，并核对实际运行的源码版本；重启服务不会更新 flake 锁定的旧代码。
登录失败检查登录 unit 日志和本地代理；不要输出或分享 token、设备码及认证文件。
推送源码后必须更新 Nix input、同步锁文件并在 Pi 切换系统；只更新应用仓库不会更新运行中的服务。

## 导出最近请求

仓库维护技能 [export-pi-transcript](../../.agents/skills/export-pi-transcript/SKILL.md)通过 SSH 只读访问 Pi，生成一份带完整性说明的 Markdown 重建 transcript。它不属于 QQ bot 的内置技能，不能通过开放 bot 私有状态权限来运行。

runtime 目前记录请求诊断和 rollout 历史，没有保存完整 HTTP 请求正文。导出按请求时间恢复可用上下文，不能保证工具定义、压缩状态、内部动态提示词及并发消息边界与真实请求逐字一致。产物含聊天内容，只保存于仓库外；不自动发布或发送。

群聊节奏排查先检查应用日志中的候选过期、调度等待／提交及观察结束事件，结合候选数量、等待秒数、直接互动标记、可见输出和连续沉默次数判断原因。节奏状态只进入应用日志，不发到 QQ、不注入模型；配置与行为以 [群聊协议](../specs/groups.md) 为准。模型调用失败不视为沉默，明确未提交的失败等待下一条外部消息再检查，避免定时重试风暴。
