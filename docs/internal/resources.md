# 技能与资源维护

## 提示词资源

所有应用提示词在 [qq_agent/prompts.py](../../qq_agent/prompts.py) 维护，随 Python 包升级和回滚；集中维护规则见 [架构说明](architecture.md#提示词注入)。不再发布、挂载或读取三份 runtime Markdown，也不再提供 Nix 模板路径覆盖选项。旧默认路径配置仅用于迁移兼容并发出弃用提示，自定义路径必须先将定制合入源码。

应用同时注入 QQ 文件交付规则和 Pillow Python 路径。工作区内的 AGENTS.md 保持原生层级加载，会话目录显式标记为可信项目；技能发现文件继续留在技能目录。生效时机见 [提示词契约](../specs/memory-prompts.md#更新时机)。

## 内置与维护者技能

在 `qq_agent/skills/` 添加 `<技能名>/SKILL.md`，文件须包含 YAML 元数据 `name` 和 `description`。
技能目录内可包含 `scripts/`、`references/`、`assets/` 等资源，随 Python 包一起发布。
此目录不是 Python 包，不需要 `__init__.py`。

服务启动时通过 Codex `skills/extraRoots/set` 注册本目录；所有会话均可发现内置技能。
各会话仍自动发现自己工作区下的 `.agents/skills/<技能名>/SKILL.md`，`/new` 保留这些文件。
使用不同技能名避免歧义；是否调用由任务匹配或用户明确指定决定。

内置技能随应用升级和回滚，Nix 部署中只读；修改后重新部署并重启服务。
工作区 `.agents/skills/` 及其全部资源不参与 14 天文件清理。

`harness-maintenance` 用于维护 agent 自身的 harness，通过已认证的 agent 专用 GitHub
账户 fork 上游仓库、从主分支最新 HEAD 开发并提交 PR；未认证时终止。

内置技能已包含在只读运行依赖 closure 中，无须新增挂载或复制到工作区。
创建和恢复 thread 前安全创建该目录、拒绝符号链接，并仅将当前会话的
`.agents/skills/` 追加到有效配置的可写目录，保留宿主配置的缓存等可写路径。
`.agents` 其余内容以及 `.git`、`.codex` 仍受 runtime 默认只读保护。

仓库 `.agents/skills/` 保存维护者使用的技能，不由应用注册或打包。`export-pi-transcript` 从维护者本机读取 Pi 日志，入口和用法见 [SKILL.md](../../.agents/skills/export-pi-transcript/SKILL.md)。技能说明必须留在发现目录，普通维护说明集中在本文件。

## 表情与压缩

QQ 内置表情映射 `qq_agent/faces.json` 提取自 [NapCat v4.18.38 的 face_config.json](https://github.com/NapNeko/NapCatQQ/blob/v4.18.38/packages/napcat-core/external/face_config.json)：使用 `sysface` 中 `QSid` 到去掉首个 `/` 的 `QDes`，保留数字 ID 字符串，不使用 IQLid/AQLid 或猜测拼音缩写。此版本与 Pi 的 NapCat 部署一致。升级 NapCat 时核对该映射，新增 ID 须在目标版本实际支持后才加入；运行时不联网拉取映射。资源随 Python 包发布，行为见 [群聊协议](../specs/groups.md#图片和提醒)。

`stickers/` 随包发布资源，`qq_agent.stickers` 启动时加载并校验配置和图片，不支持热重载。新增表情时加入图片，更新 `stickers/sticker_config.json`，压缩后重新打包部署，无需修改代码。

developer 提示词仅序列化配置的名称列表，不包含文件路径或是否有图的标记；同一组名称的图片映射变化不改变提示词快照。文件映射由应用解析回复及发送时使用，协议见 [群聊 JSON 回复](../specs/groups.md#json-回复与纠错)。

```sh
uv run --frozen python compress_stickers.py stickers/example.png
uv run --frozen python compress_stickers.py stickers/
uv run --frozen python compress_stickers.py
```

单文件参数只处理该文件；目录参数递归处理 PNG/JPEG/GIF/WebP，无参数默认处理 `stickers/`。目录模式忽略其他扩展名；单文件通过实际图片格式校验。符号链接、特殊文件、非法图片和不存在的路径被拒绝，失败返回非零状态；目录模式继续处理其他图片并汇总失败状态。

压缩先等比缩到不超过 640,000 像素，不放大原图；先比较无损编码，再搜索符合 200,000 字节限制的 WebP 质量。只有编码无法达标才继续缩小，以二分细化可行尺寸；优先尺寸而非最低画质保证。已有合规文件不重新编码。

透明度、动画帧数、循环、时序及 APNG 默认图必须经解码校验；格式转换无法保留时回退原格式，仍不能达标则报错。编码逻辑由随应用发布的 `qq_agent.compression` 共用。

转换成功时使用实际格式后缀，输出新旧路径、尺寸和字节数；新路径已存在时拒绝覆盖。处理仓库 `stickers/` 时同步更新 `sticker_config.json` 中所有对应引用，其他目录不猜测配置。先验证并写入新图，再原子更新配置，最后移除原图；失败时保留可用原图，配置不会指向缺失文件，配置更新失败可能留下新旧两份图片。原格式输出仍采用原子替换。

表情加载先解析安装包目录的真实路径，兼容 Nix 虚拟环境的目录符号链接；包内图片仍拒绝符号链接和越界路径。打包阶段从隔离的实际安装环境加载表情，避免源码目录掩盖安装布局问题，无需额外挂载表情目录。
