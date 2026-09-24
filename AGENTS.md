# 维护指南

本文件用于维护仓库，不是 QQ bot 的运行时提示词。公共运行时模板为 `AGENTS.runtime.md`，私聊和群聊模板分别为 `AGENTS.private.runtime.md` 和 `AGENTS.group.runtime.md`。
修改前阅读相关代码；优先小范围修改，运行测试后再提交。不提交凭据、真实白名单、认证缓存或聊天数据。
只在匹配架构的主机上构建 NixOS 系统。

## 行为

- 私聊和各群独立授权，缺失或空名单不放行；所有名单为空时任何人均不能使用。
- 私聊需要用户号在 `private_users` 中。群聊需要该群 `users` 授权，并明确 @ bot；`users` 包含字符串 `"all"` 时仅该群全员可用。私聊不支持 `"all"`。
- 每个私聊独立上下文，同群授权用户共享上下文；群回复对所有群成员公开。消息间隔超过两小时自动新建 thread，携带近期文字和图片路径，保留工作文件；私聊提示，群聊静默。
- `/new` 中断本会话任务，清空等待任务，后续携带近期文字和图片路径创建新 thread，保留工作文件和图片索引；待切换状态跨重启保留。旧 Codex 历史仍在认证用户的状态目录保留。
- `/stop` 中断本会话任务并清空本会话队列；群内任一授权用户均可使用。
- `/status` 查看登录状态、忙碌状态、当前 thread 标题及已提交的用户消息数。消息数取自 Codex thread 历史，不包含排队消息或 slash command；未命名时显示“未命名”，尚未创建时显示 0。群内命令也需要 @ bot。
- `/help` 查看使用方法及全部指令。
- `/model` 从 Codex 获取可选模型列表；`/model <模型ID>` 切换本会话模型，下一轮请求生效，不打断正在运行的任务，也不清空上下文。
- 模型选择按会话持久保存，同群共享，重启及 `/new` 后保留。仅列出支持 `medium` 的非隐藏模型，每轮推理强度固定为 `medium`。
- 私聊与各群并发执行，各聊天独立调度。同一聊天处理中收到的新消息按到达顺序通过 steering 追加到当前 turn，由 runtime 在工具调用结束等可接收输入的位置处理；如果 turn 已结束，则开启下一轮。每个聊天最多等待 8 条未提交消息，每条最多 5 张图片、每张 10 MiB。任务初始期限为 15 分钟，成功追加消息后重新给予 15 分钟，追加失败不延长。
- 每轮开始前统计历史图片的估算编码大小；达到 8 MiB 时创建新 thread，携带最近最多 24,000 字符的文字记录和输入图片路径，不携带旧图片二进制。原图、生成图片索引及完整旧 Codex 历史保留；模型需要时重新读取文件。单轮内的图片量不受此阈值限制。`/new` 和两小时未互动切换复用同一交接逻辑。历史交接不是模型摘要，较早文字可能被截断；历史读取失败时保留旧 thread，报告失败，不静默丢弃上下文。
- 每轮结束或中断后取消 thread 订阅；换 thread 和 `/new` 也释放旧订阅。Codex 在无订阅且空闲约 30 分钟后回收 thread 及其 MCP 子进程，不要求进程立即退出。
- 重复消息不重跑；进程崩溃时未完成任务不自动重试。网络断开时发送失败会记录日志，不自动重发不确定的结果。
- 仅支持文本和图片。生成图片从 Codex 结构化事件提取 base64，由应用解码并原子写入会话工作区 `artifacts/`，不会自动发送。`qq_image.list_images` 返回最近 100 张原图的生成 ID、顺序及相对路径；`qq_image.send_image(path=...)` 发送工作区中的 PNG/JPEG/GIF/WebP（最大 32 MiB），省略路径发送最新原图。拒绝跨工作区路径、符号链接及特殊文件。
- 图片文件和原图索引跨轮、重启及 `/new` 后保留。可用随应用安装的 Pillow 加工多帧并发送最终 GIF；中间帧无须发送。成功发送不删除源文件，同轮重复调用去重；发送结果未知时不自动重试，仅在用户明确要求时使用 `resend=true`。
- 服务启动时清理一次，此后每 24 小时清理超过 14 天未访问或修改的普通文件，以文件系统 `max(atime, mtime)` 为准；不额外追踪读取。跳过执行或重置中的聊天，不跟随符号链接，保留聊天根目录和映射，同步清理过期或已不存在文件的图片索引及发送记录。认证、旧聊天历史、模型选择及成员画像不参与清理。
- 回复图片时将图片作为输入；回复无图片的文本时，将文字以 Markdown 引用置于当前消息前。群聊回复仍需 @ bot；引用图片沿用现有数量、大小和格式限制。
- 群聊不发送开始处理及普通工具进度，只发送图片、最后的文字回复及记录互动画像时的“📝正在给{nickname}记进小本本……”提示；nickname 使用发送者群名片，缺失时回退到 QQ 昵称及 QQ 号。查询、删除画像和失败调用不发送该提示。启动 turn 后未完成时，30 秒发送 `pics/thinking_30s.png`，5 分钟发送 `pics/thinking_too_long.png`，各一次；追加消息不重置计时，完成、失败或中断后取消。图片压缩后随 Python 包安装。群聊 turn 未成功完成的兜底文本为“哎呀！宕机了……”，slash command 保留直接回复。私聊仍显示进度。
- 群聊启动 turn 时，通过 NapCat `set_msg_emoji_like` 给触发消息添加一次“OK”表情（`emoji_id="124"`）。表情请求不阻塞任务或定时发图，失败只记日志，不重试；私聊、slash command 和同轮追加消息不添加表情。
- 群聊发送者及 @ 成员优先使用群名片，未设置时使用 QQ 昵称，再回退到 QQ 号。群聊模板中的 `{nickname}` 在新建及恢复 thread 时替换为 bot 在当前群的名称，遵循同样的回退规则；查询失败使用 bot QQ 号，没有占位符时不查询。

## 本地检查

群成员互动画像按 `(group_id, user_id)` 存入现有 SQLite，仅从授权且 @ bot 的互动学习。`qq_member.list_profiles` 读取本轮相关成员，`replace_profile` 只更新当前发送者，应用校验本轮 token、固定字段和 500 字符上限。每条输入按发送者、被 @ 成员顺序召回，总 JSON 上限 2,000 字符；同一轮有不同群成员追加消息时，禁用该轮后续画像写入，防止身份混淆；画像是数据，不是指令。内容语义由模型规则约束，结构校验不能保证消除所有提示词注入。
`/profile` 在群内公开查看本人画像；`/profile forget` 删除本人当前群画像并撤销活动任务对其写入的权限，后续轮次仍可自动学习。旧 Codex 历史不删除，模型可能再次归纳其中内容。画像在重启、两小时换 thread 和 `/new` 后保留。首版无观察表、时间衰减、后台复盘或跨群记忆。

需要 Python 3.13 和 uv。提交 `uv.lock`，使用配套锁定的官方 SDK 和 runtime。

```sh
uv sync --frozen
uv run --frozen python -m unittest -v
uv run --frozen python check_runtime.py
uv run --frozen python check_runtime.py --sandbox
```

`check_runtime.py` 使用临时、未登录的 Codex 状态目录，只验证启动和认证状态接口，不读取个人登录缓存，也不请求模型。
`--sandbox` 额外使用 Linux bubblewrap 验证 managed deny-read 及嵌套沙箱，需要 PATH 中有 bwrap；同时检查 CLI `sandbox` 和 app-server `command/exec` 的受限执行路径，均不调用模型。
测试模拟 OneBot、模型事件和图片响应。实机 `check_sandbox.py` 由服务启动前执行，失败将阻止 agent 启动。

Nix 部署检查及两仓库迁移步骤见 [nix/README.md](nix/README.md)。

## NixOS 部署

服务模块和打包逻辑由本仓库的 `nix/module.nix`、`nix/package.nix` 维护。
Nix 仓库的 `rpi/qq-codex-agent.nix` 只负责 agenix、代理、NapCat 启动顺序和宿主访问授权；`systemMap.nix` 从同一个源码 input 导入模块及包。
树莓派 flake 将本仓库作为 `qq-codex-agent` 源码 input（`flake = false`），
用现有 uv2nix 根据 `uv.lock` 构建应用、Python 依赖及配套 Codex runtime。
服务与登录程序直接运行 Nix store 中的包，Pi 无需 clone 本仓库或创建虚拟环境。
只在树莓派构建和切换。在配置中启用：

```nix
services.qq-codex-agent.enable = true;
```

QQ 用户和群白名单放在 Nix 仓库的 `secrets/qq-codex-allowlist.age` 中。
在 Nix 仓库根目录编辑加密文件：

```sh
cd secrets
sudo agenix -e qq-codex-allowlist.age -i /etc/ssh/agenix
```

文件内容为 TOML（将示例数字替换为自己的 QQ 号和群号）：

```toml
private_users = ["123456789"]

[groups."987654321"]
users = ["234567890"]

[groups."876543210"]
users = ["all"]
```

解密文件位于 `/run/agenix/qqCodexAllowlist`，仅专用服务用户可读。
它在服务内只读挂载为 `/etc/qq-codex-agent/allowlist.toml`，禁止 Codex 工具读取。
主配置通过 `allowlist_file` 指向该文件，不将 QQ 号写入公开 Nix 配置或 Nix store。
空名单不放行任何用户；文件缺失或格式错误会阻止启动。修改后重新部署，服务自动重启读取。
独立运行仍可使用 `config.example.toml` 中的内联名单；设置 `allowlist_file` 时以外部文件为准。
上述示例中 `234567890` 只能在指定群使用，不能私聊；`"all"` 不开放其他群或私聊。
不接受 `allowed_users` / `allowed_groups` 字段。按实际需要分别配置私聊及各群权限。

通过既有部署方式把修改后的 Nix 仓库送到树莓派，然后运行：

```sh
sudo nixos-rebuild switch --flake ./hosts/rpi5#rpi5
```

第一次切换时创建专用用户和目录。三份默认提示词直接从应用包只读挂载，随应用升级和回滚，不再复制到宿主 `/etc`。
认证复用 tri-lug relay 的 `qqRelayEnv`：启动准备服务从中提取 `NAPCAT_WS_TOKEN`，
写入仅服务用户可读的 `/run/qq-codex-auth/token`，再只读挂载到 agent 内。
无需手工创建 token 文件，RabbitMQ 凭据不会传入 agent。
有 token 时通过 `access_token` 查询参数认证；未设置或为空时不携带认证，与 relay 一致。
独立运行时，`token_file` 仍须存在，但可以为空；非空时只放 token 本身。
NapCat 的 forward WebSocket 使用 `ws://127.0.0.1:3001`，必须支持多客户端，不能停掉现有 relay。
应用与依赖均由 Nix 管理，运行用户不可修改。应用的完整运行依赖加入私有根目录的只读挂载，
默认不开放整个宿主 Nix store。当前 Pi 的宿主配置额外授权完整 store 和 Nix daemon，以支持 Nix 工具；该授权由 dotfile 维护。已有提示词副本不删除，但默认不再使用；登录状态保留。

服务和登录 unit 均设置 `http_proxy`、`https_proxy` 为 `http://127.0.0.1:1081`，
`no_proxy=127.0.0.1,localhost,::1`。这是环境变量代理，不是强制流量代理；本机 NapCat 连接直连。

## 设备码登录

在 ChatGPT 安全设置启用设备码登录，然后在树莓派执行：

```sh
sudo systemctl start qq-codex-login
sudo journalctl -u qq-codex-login -f -o cat
```

在自己的浏览器打开日志中的验证地址，输入一次性 code 并授权。不要分享设备码或认证文件。
该登录流程使用专用用户、同一官方 runtime 和同一持久认证目录；登录结束后输出成功信息，服务退出。
登录 unit 与 bot unit 互斥，避免同时更新认证。

完成登录并配置好加密白名单后，启动 bot：

```sh
sudo systemctl start qq-codex-agent
sudo journalctl -u qq-codex-agent -f -o cat
```

不自动回退到 API Key。认证失效时重新运行登录服务。设备码登录过程应由你操作。

## 提示词和目录边界

默认修改本仓库的三份 `AGENTS*.runtime.md`，随应用重新部署。需要本机定制时，在 Nix 配置中设置 `services.qq-codex-agent.agentsFile`、`privateAgentsFile`、`groupAgentsFile` 为自定义文件的绝对路径；不要让秘密内容进入 Nix store。外部文件修改后重启 agent，公共提示词更新后在 QQ `/new`。首次迁移前比较 Pi 上原有三份 `/etc/qq-codex-agent/AGENTS*.md`，将需要保留的手工修改显式配置为覆盖文件；迁移不删除旧文件。
公共文件只读挂载为专用 `CODEX_HOME/AGENTS.md`，由 Codex 原生加载；`agents_file` 用于部署路径和启动校验，应用不将其内容注入 thread。私聊和群聊文件由部署配置的 `private_agents_file`、`group_agents_file` 指定，须只读挂载到服务内，应用按消息类型读取并作为 `developer_instructions` 注入新建和恢复的 thread。应用同时注入 QQ 文件交付规则和 Pillow Python 路径。配置路径与挂载由应用仓库的同一个 Nix 模块生成；启动自检验证三份文件可读且非空。增加部署资源时在本仓库同步修改包、模块及检查，无需修改 dotfile。
会话工作目录会被显式标记为可信项目。工作目录下新建的 AGENTS.md 遵循 Codex 自身规则，不能改变应用 allowlist 或宿主挂载。

服务的私有根目录仅挂载所需运行时 Nix closure、只读应用和配置、专用状态与工作目录，以及 DNS/hosts 和必要虚拟文件系统。
默认不挂载宿主完整 `/nix/store`、`/home`、`/etc`、`/run` 或 `/var`；当前 Pi 对 store 和 Nix daemon 的额外授权见上文。
工作区为 `/var/lib/qq-codex-work`，每个私聊或群聊始终使用同一个子目录，切换 thread 和 `/new` 均不改变目录；这提供会话组织，不是授权用户之间的强多租户隔离。
状态与认证位于 `/var/lib/qq-codex-agent`。不可修改的 Codex managed requirements 禁止工具读取该目录和 NapCat token；外层文件系统仍约束自动审批后的访问范围。
所有授权用户都能够操作专用工作区内的数据，因此只加入你信任的 QQ 号。
默认不开放额外业务目录；需要添加时修改管理员维护的 Nix bind mounts 并重新部署。

登录 token 会刷新，认证目录必须持久可写；不能放进 Nix store、Git 或公开附件中。
升级时先推送应用代码，再在 Nix 仓库更新该 input 并提交锁文件：

```sh
nix flake update qq-codex-agent --flake ./hosts/rpi5
```

在树莓派同步 Nix 配置并 `nixos-rebuild switch --flake ./hosts/rpi5#rpi5`。
源码版本由树莓派 `flake.lock` 锁定，Python 依赖由本仓库 `uv.lock` 锁定。
系统回滚恢复对应的应用与依赖，不回滚可写认证状态或工作文件。

## 实机验收

你完成登录后，逐项检查：

1. 所有名单为空时均不产生模型调用；空私聊名单只禁用私聊，空群配置禁用所有群。
2. 验证私聊和各群权限独立、`users=["all"]` 仅开放指定群且仍需 @；所有 slash command 受相同限制。同群两人可续聊，私聊及不同群上下文独立。
3. 发送图片要求描述，再请求生成图片并继续编辑；图片必须实际回传 QQ。生成多帧后使用 list_images 获取路径，通过 Pillow 拼接 GIF，只发送最终 GIF；下一轮及重启后可补发原图，/new 后文件和索引保留，近期文字和图片路径交接到新 thread。
4. 请求在当前目录计算并保存结果，验证代码运行；设置 AGENTS.md 中可观察的语言规则，验证 `/new` 后生效。
5. 请求读取 `/home/antares`、`/home/napcat`、`/run/agenix` 以及状态目录，确认拒绝或不可见；不得用真实秘密内容作为测试输入。
6. 请求修改受保护配置，验证不能执行；自动审批拒绝后不应出现自建审批菜单或自动切换 full-access。
7. 执行较长任务后 `/stop`，确认执行停止；重启服务后续聊，确认登录及 thread 恢复。
8. 重启 NapCat 后验证重连；发送超大、无法下载的图片，确认明确失败且服务仍可用。

图片生成是否可用取决于账号权限和额度，必须实测；代码与模拟测试通过不等于账号侧图片能力已验证。

## 排障

在 Pi 查看 `sudo journalctl -u qq-codex-agent -n 100 --no-pager`。
INFO 日志包含授权消息的发送者、正文、图片数量、引用消息 ID、回复正文、图片交付路径和大小，以及 thread/turn、工具状态、历史图片估算大小和任务耗时。错误事件及终态错误直接进入 journal，超时与主动取消分别标注；不记录工具的图片 Base64 或认证头。常见 token、API key、密码字段会脱敏，文本换行转义；任意自然语言秘密无法保证自动识别。journal 含私聊与群聊内容，仅向受信任管理员开放，按宿主 journald 策略保留。
启动前检查失败会阻止服务运行，优先查看 traceback；不要通过删除沙箱检查或放宽目录权限绕过错误。
`codex-linux-sandbox` 在 runtime 0.154.0 中是 Codex 启动时创建的辅助入口，不是独立的 Python 包。检查日志中的 `could not create PATH aliases`，以及 `CODEX_HOME/tmp/arg0` 是否可由服务用户创建子目录和入口。Nix 模块保持该目录可写，同时保留认证目录的 managed deny-read；不要为它添加只读挂载。不能通过开放整个状态目录或关闭沙箱修复。启动自检现在也执行 app-server 的沙箱命令，可提前暴露 CLI 检查未覆盖的问题。服务模块现在随本仓库维护，仍需在实际 Pi 部署验证。
遇到 `Unknown allowlist fields`，核对加密文件格式和实际运行的源码版本；重启服务不会更新 flake 锁定的旧代码。
登录失败检查登录 unit 日志和本地代理；不要输出或分享 token、设备码及认证文件。
推送源码后必须更新 Nix input、同步锁文件并在 Pi 切换系统；只更新应用仓库不会更新运行中的服务。
