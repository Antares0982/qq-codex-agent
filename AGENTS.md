# 维护指南

本文件用于维护仓库，不是 QQ bot 的运行时提示词。运行时模板为 `AGENTS.runtime.md`。
修改前阅读相关代码；优先小范围修改，运行测试后再提交。不提交凭据、真实白名单、认证缓存或聊天数据。
GitHub 推送由用户操作，不调用 gh CLI。只在匹配架构的主机上构建 NixOS 系统。

## 行为

- 私聊和各群独立授权，缺失或空名单不放行；所有名单为空时任何人均不能使用。
- 私聊需要用户号在 `private_users` 中。群聊需要该群 `users` 授权，并明确 @ bot；`users` 包含字符串 `"all"` 时仅该群全员可用。私聊不支持 `"all"`。
- 每个私聊独立上下文，同群授权用户共享上下文；群回复对所有群成员公开。消息间隔超过两小时自动新建 thread，保留工作文件；私聊提示，群聊静默。
- `/new` 中断本会话任务，清空等待任务并删除该会话工作文件，后续创建新 thread。旧 Codex 历史仍在认证用户的状态目录保留。
- `/stop` 中断本会话任务并清空本会话队列；群内任一授权用户均可使用。
- `/status` 查看登录状态、忙碌状态、当前 thread 标题及已提交的用户消息数。消息数取自 Codex thread 历史，不包含排队消息或 slash command；未命名时显示“未命名”，尚未创建时显示 0。群内命令也需要 @ bot。
- `/help` 查看使用方法及全部指令。
- `/model` 从 Codex 获取可选模型列表；`/model <模型ID>` 切换本会话模型，下一轮请求生效，不打断正在运行的任务，也不清空上下文。
- 模型选择按会话持久保存，同群共享，重启及 `/new` 后保留。仅列出支持 `medium` 的非隐藏模型，每轮推理强度固定为 `medium`。
- 默认全局串行执行，最多等待 8 个任务，每次最多 5 张图片、每张 10 MiB，每任务最长 15 分钟。
- 重复消息不重跑；进程崩溃时未完成任务不自动重试。网络断开时发送失败会记录日志，不自动重发不确定的结果。
- 仅支持文本和图片。生成图片从 Codex 结构化事件中提取 base64，仅在 agent 调用 `qq_image.send_image` 后通过 NapCat 回传。
- 回复图片时将图片作为输入；回复无图片的文本时，将文字以 Markdown 引用置于当前消息前。群聊回复仍需 @ bot；引用图片沿用现有数量、大小和格式限制。
- 群聊不发送开始处理、工具进度等中间通知，只发送图片及最后的文字回复；失败时保留一次终态提示，slash command 保留直接回复。私聊仍显示进度。

## 本地检查

需要 Python 3.13 和 uv。提交 `uv.lock`，使用配套锁定的官方 SDK 和 runtime。

```sh
uv sync --frozen
uv run --frozen python -m unittest -v
uv run --frozen python check_runtime.py
uv run --frozen python check_runtime.py --sandbox
```

`check_runtime.py` 使用临时、未登录的 Codex 状态目录，只验证启动和认证状态接口，不读取个人登录缓存，也不请求模型。
`--sandbox` 额外使用 Linux bubblewrap 验证 managed deny-read 及嵌套沙箱，需要 PATH 中有 bwrap。
测试模拟 OneBot、模型事件和图片响应。实机 `check_sandbox.py` 由服务启动前执行，失败将阻止 agent 启动。

## NixOS 部署

服务模块位于 Nix 仓库 `rpi/qq-codex-agent.nix`，打包逻辑在 `rpi/qq-codex-package.nix`。
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

第一次切换时会创建专用用户和目录，并在 AGENTS.md 不存在时复制默认模板。
认证复用 tri-lug relay 的 `qqRelayEnv`：启动准备服务从中提取 `NAPCAT_WS_TOKEN`，
写入仅服务用户可读的 `/run/qq-codex-auth/token`，再只读挂载到 agent 内。
无需手工创建 token 文件，RabbitMQ 凭据不会传入 agent。
有 token 时通过 `access_token` 查询参数认证；未设置或为空时不携带认证，与 relay 一致。
独立运行时，`token_file` 仍须存在，但可以为空；非空时只放 token 本身。
NapCat 的 forward WebSocket 使用 `ws://127.0.0.1:3001`，必须支持多客户端，不能停掉现有 relay。
应用与依赖均由 Nix 管理，运行用户不可修改。应用的完整运行依赖加入私有根目录的只读挂载，
不开放整个宿主 Nix store。已有 AGENTS.md 和登录状态会保留。

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

编辑 `/etc/qq-codex-agent/AGENTS.md` 后重启 agent，再在 QQ `/new`。
文件只读挂载为专用 `CODEX_HOME/AGENTS.md`，由 Codex 原生加载，可控制语气、语言和工具使用；app 不重写 Codex 基础提示词。
会话工作目录会被显式标记为可信项目。工作目录下新建的 AGENTS.md 遵循 Codex 自身规则，不能改变应用 allowlist 或宿主挂载。

服务的私有根目录仅挂载所需运行时 Nix closure、只读应用和配置、专用状态与工作目录，以及 DNS/hosts 和必要虚拟文件系统。
不挂载宿主完整 `/nix/store`、`/home`、`/etc`、`/run` 或 `/var`。
工作区为 `/var/lib/qq-codex-work`，不同会话使用不同子目录；这提供会话组织，不是授权用户之间的强多租户隔离。
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
3. 发送图片要求描述，再请求生成图片并继续编辑；图片必须实际回传 QQ。
4. 请求在当前目录计算并保存结果，验证代码运行；设置 AGENTS.md 中可观察的语言规则，验证 `/new` 后生效。
5. 请求读取 `/home/antares`、`/home/napcat`、`/run/agenix` 以及状态目录，确认拒绝或不可见；不得用真实秘密内容作为测试输入。
6. 请求修改受保护配置，验证不能执行；自动审批拒绝后不应出现自建审批菜单或自动切换 full-access。
7. 执行较长任务后 `/stop`，确认执行停止；重启服务后续聊，确认登录及 thread 恢复。
8. 重启 NapCat 后验证重连；发送超大、无法下载的图片，确认明确失败且服务仍可用。

图片生成是否可用取决于账号权限和额度，必须实测；代码与模拟测试通过不等于账号侧图片能力已验证。

## 排障

在 Pi 查看 `sudo journalctl -u qq-codex-agent -n 100 --no-pager`。
启动前检查失败会阻止服务运行，优先查看 traceback；不要通过删除沙箱检查或放宽目录权限绕过错误。
遇到 `Unknown allowlist fields`，核对加密文件格式和实际运行的源码版本；重启服务不会更新 flake 锁定的旧代码。
登录失败检查登录 unit 日志和本地代理；不要输出或分享 token、设备码及认证文件。
推送源码后必须更新 Nix input、同步锁文件并在 Pi 切换系统；只更新应用仓库不会更新运行中的服务。
