# 部署与迁移

所有命令未特别说明时从相应仓库根目录执行。应用配置约束见 [配置与权限](../specs/configuration.md)。

## 独立运行

需要 Python 3.13、uv、Node.js、可用的本地 NapCat OneBot 11 服务及 Codex runtime 支持的沙箱环境。执行 `uv sync --frozen`，复制 [配置示例](../../config.example.toml)到不提交的本地配置文件，填写绝对目录、三份模板路径、token 文件及授权入口。独立登录时不设置 `auth_socket`。

```sh
uv run --frozen qq-codex-agent --config /absolute/path/config.toml --login
uv run --frozen qq-codex-agent --config /absolute/path/config.toml
```

配置路径默认 `/etc/qq-codex-agent/config.toml`。NapCat token 文件必须存在，可为空。不要将配置、认证和聊天数据提交到仓库。

## Nix 模块接入

`package.nix` 使用调用方锁定的 `nixpkgs`、`uv2nix`、`pyproject-nix`、
`pyproject-build-systems`，从本仓库的 `uv.lock` 打包应用及三份提示词。
`module.nix` 负责配置生成、服务与登录 unit、运行依赖挂载和启动自检。
源码 input 继续使用 `flake = false`，不需要另建 flake。

调用方提供源码 input 和包：

```nix
{
  imports = [ (inputs.qq-codex-agent + "/nix/module.nix") ];
  services.qq-codex-agent = {
    enable = true;
    package = import (inputs.qq-codex-agent + "/nix/package.nix") {
      inherit inputs system;
    };
    configFile = "/run/agenix/qqCodexAllowlist";
    tokenFile = "/run/qq-codex-auth/token";
  };
}
```

`configFile` 指向运行时完整应用 TOML（通常为 `config.age.secrets.qqCodexAllowlist.path`），
以字符串路径传入；不在 Nix 求值时读取内容，也不合并生成配置。
它只读挂载为 `/etc/qq-codex-private/config.toml`，启动命令显式使用该路径，
目录级 managed deny-read 阻止模型工具读取。`tokenFile` 继续指向单独的 NapCat token。
这两个文件必须由宿主准备，并允许 `qq-codex-agent` 用户读取。

RPi 沿用 `secrets/qq-codex-allowlist.age` 文件名，但内容已升级为完整配置，
可同时填写 `private_users`、`groups`、`agent_admin_qq`、`model` 等字段。
完整示例见下文；[config.example.toml](../../config.example.toml) 是独立运行示例，Nix 部署还须启用其中
`auth_socket`、`resources_dir` 两行。顶层字段放在 `[groups."群号"]` 等表声明之前。
必需字段缺失、格式错误或文件不可读会阻止启动；空名单仍不放行任何用户。

设置 `configFile` 后，`allowlistFile`、`napcatUrl`、`groupPromptAllowMembers`、
`agentAdminQQ`、`authSocket` 不再生成应用字段；也不再挂载单独白名单。
提示词资源、`codexSettings`、token、共享认证和其他服务挂载仍由 Nix 管理。
完整配置内的目录和路径必须匹配现有挂载，填写新路径不会增加宿主访问权限。
不设置 `configFile` 时保留原生成模式，此时必须设置 `allowlistFile`；
生成配置也使用新的受保护路径，应用 CLI 默认路径仍保留供独立运行兼容使用。
旧生成模式中，`groupPromptAllowMembers` 对应 `group_prompt_allow_members`（默认 false），`agentAdminQQ` 对应 `agent_admin_qq`（默认 null 时省略），`authSocket` 默认 `/run/codex-auth/auth.sock`。完整配置模式直接在 TOML 中填写对应字段。

迁移时通过 `agenix -e` 在原密文上补齐字段，保留原白名单和已有管理员配置，
不修改加密接收者。先发布本仓库的模块与应用，再更新 RPi 的源码 input 和部署接线；
不可只迁移密文便部署旧版模块。共享登录凭据和 NapCat token 不迁入该配置。
模块不依赖 agenix；宿主负责秘密文件更新时的重启触发和准备服务的依赖关系。
当前 Pi 的 dotfile 还负责代理、GitHub 凭据、NapCat 启动顺序、禁用 WebSocket
的 provider 配置，以及 Nix daemon 和完整 store 的额外访问授权。
通用模块仅挂载应用和运行工具的 Nix closure；closure 挂载清单在构建时生成，
通过 `systemd.packages` 安装到 agent unit 的 drop-in，不在求值时构建其他架构的包。
状态目录 `/var/lib/qq-codex-agent`、秘密配置目录 `/etc/qq-codex-private` 和认证
socket 目录 `/run/codex-auth` 整体禁止工具读取。只使用目录规则，不使用逐文件 glob。
技能与插件 cache 迁到 `/var/lib/qq-codex-resources`，原位置保留链接；运行时生成的
skill 路径会指向资源目录，工具可读但不可写。工作区中的自建技能仍可按工作区权限使用。
首次启动搬迁已有资源，目标冲突时停止，不覆盖。备份状态时同时备份这个资源目录。

`CODEX_HOME/tmp/arg0` 只读挂载为空目录，让官方 runtime 使用自身可执行文件作为沙箱
入口，避免必读辅助路径与整个私有状态目录的拒读规则冲突。启动时 PATH aliases 的
只读目录警告是这个布局的预期现象；不可据此开放私有目录。启动检查实测 thread 创建、
CLI/app-server 沙箱、只读资源、私有状态拒读和认证 socket 连接拒绝。
服务禁用 shell snapshot。附加 deny-read 路径只允许目录。

## RPi 部署

服务模块和打包逻辑由本仓库的 `nix/module.nix`、`nix/package.nix` 维护。
Nix 仓库的 `rpi/qq-codex-agent.nix` 只负责 agenix、代理、NapCat 启动顺序和宿主访问授权；`systemMap.nix` 从同一个源码 input 导入模块及包。
树莓派 flake 将本仓库作为 `qq-codex-agent` 源码 input（`flake = false`），
用现有 uv2nix 根据 `uv.lock` 构建应用、Python 依赖及配套 Codex runtime。
服务与登录程序直接运行 Nix store 中的包，Pi 无需 clone 本仓库或创建虚拟环境。
只在树莓派构建和切换。在配置中启用：

```nix
services.qq-codex-agent.enable = true;
```

完整应用配置放在 Nix 仓库的 `secrets/qq-codex-allowlist.age` 中；保留历史文件名，内容已不限于白名单。
在 Nix 仓库根目录编辑加密文件：

```sh
cd secrets
sudo agenix -e qq-codex-allowlist.age -i /etc/ssh/agenix
```

文件内容为完整 TOML（将示例数字替换为自己的 QQ 号和群号，顶层字段放在群表之前）：

```toml
napcat_url = "ws://127.0.0.1:3001"
token_file = "/etc/qq-codex-private/napcat-token"
state_dir = "/var/lib/qq-codex-agent"
workspace_dir = "/var/lib/qq-codex-work"
resources_dir = "/var/lib/qq-codex-resources"
auth_socket = "/run/codex-auth/auth.sock"
agents_file = "/etc/qq-codex-agent/AGENTS.md"
private_agents_file = "/etc/qq-codex-agent/AGENTS.private.md"
group_agents_file = "/etc/qq-codex-agent/AGENTS.group.md"
queue_limit = 8
task_timeout = 3600
group_prompt_allow_members = false
# agent_admin_qq = "123456789"
private_users = ["123456789"]

[groups."987654321"]
observation_interval_seconds = 60
message_ttl_seconds = 120
conversation_gap_seconds = 300

[groups."876543210"]
observation_interval_seconds = 60
message_ttl_seconds = 120
conversation_gap_seconds = 300
```

解密文件位于 `/run/agenix/qqCodexAllowlist`，仅专用服务用户可读。
设置 `services.qq-codex-agent.configFile = config.age.secrets.qqCodexAllowlist.path`；挂载和旧配置兼容方式见上文[Nix 模块接入](#nix-模块接入)。不将 QQ 号或配置明文写入公开 Nix 配置、Git 或 Nix store。修改加密配置后重新部署，服务自动重启读取。

上述示例仅开放列出的私聊和两个群；字段限制见 [配置与权限](../specs/configuration.md)。

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
默认不开放整个宿主 Nix store。当前 Pi 的宿主配置额外授权完整 store 和 Nix daemon，以支持 Nix 工具；该授权由 dotfile 维护。已有提示词副本不删除，但默认不再使用；登录状态保留。宿主 `/etc/localtime` 存在时只读挂载到服务私有根目录，使群历史采用系统时区。

服务和登录 unit 均设置 `http_proxy`、`https_proxy` 为 `http://127.0.0.1:1081`，
`no_proxy=127.0.0.1,localhost,::1`。这是环境变量代理，不是强制流量代理；本机 NapCat 连接直连。

## 共享 ChatGPT 登录

模块要求宿主提供 `codex-auth.service`、`codex-login.service` 和 `codex-auth-clients` 组，QQ 用户加入该组。认证 broker 随本包的 `codex-auth` 命令安装。登录流程不要求 NapCat token 已准备完成。

RPi 使用独立 `codex-auth.service` 作为唯一刷新者，凭据位于
`/var/lib/codex-auth/codex`，由 Nix 仓库的 `rpi/codex-auth.nix` 管理。
QQ 与 Antares 通过 `/run/codex-auth/auth.sock` 获取短期 access token；各自 CODEX_HOME
只保管会话，认证使用内存存储，不读取旧 auth.json，也不拥有 refresh token。

首次迁移先停止消费者，再运行宿主提供的 `codex-auth-import`，复用现有 QQ 登录。
已有统一认证时不要再次导入。需要重新验证时：

```sh
sudo systemctl start codex-login
sudo journalctl -u codex-login -f -o cat
sudo systemctl start codex-auth qq-codex-agent antares-agent
```

登录 unit 与认证服务互斥；成功后需重新启动消费者。旧 `qq-codex-login` 仅转调新入口。
不分享设备码或认证文件，不将认证放进 Nix store/Git，不同时运行第二个刷新者。
本地不设置 `auth_socket` 时仍支持独立登录，但 RPi 配置始终使用共享认证。
完整停服、备份和回退流程见 antares-agent 的 `docs/design/06-codex-migration.md`。

## 隔离与目录

服务的私有根目录仅挂载所需运行时 Nix closure、只读应用和配置、专用状态与工作目录，以及 DNS/hosts 和必要虚拟文件系统。
默认不挂载宿主完整 `/nix/store`、`/home`、`/etc`、`/run` 或 `/var`；当前 Pi 对 store 和 Nix daemon 的额外授权见上文。
工作区为 `/var/lib/qq-codex-work`，每个私聊或群聊始终使用同一个子目录，切换 thread 和 `/new` 均不改变目录；这提供会话组织，不是授权用户之间的强多租户隔离。
会话状态位于 `/var/lib/qq-codex-agent`，共享认证位于独立用户目录。不可修改的 Codex managed requirements 禁止工具读取该目录和 NapCat token；外层文件系统仍约束自动审批后的访问范围。
所有授权用户都能够操作专用工作区内的数据，因此只加入你信任的 QQ 号。
默认不开放额外业务目录；需要添加时修改管理员维护的 Nix bind mounts 并重新部署。

登录 token 会刷新，认证目录必须持久可写；不能放进 Nix store、Git 或公开附件中。系统回滚恢复应用与依赖，不回滚可写认证状态或工作文件。

## 检查与发布

运行 [开发检查](development.md)中的 Python 测试、runtime 自检和必需的本机 Nix 应用包构建。下述模块检查不能替代应用包构建。使用 dotfile 已锁定的
nixpkgs 执行本机架构的部署检查：

```sh
nix build --impure --no-link --expr '
  let host = builtins.getFlake "/home/antares/Documents/GitHub/Nix/hosts/rpi5";
  in import ./nix/check.nix { nixpkgs = host.inputs.nixpkgs; }
'
```

检查覆盖默认及自定义提示词挂载、只读辅助目录、独立公共资源路径、
token 挂载和实际生成的 TOML。打包阶段检查三份模板存在且非空，并执行 Python 测试。
表情的安装环境校验见 [资源维护](resources.md#表情与压缩)。
本机可用本地源码覆盖 input 求值完整 Pi 服务，不改锁文件：

```sh
nix eval --no-write-lock-file \
  --override-input qq-codex-agent "path:$PWD" \
  /home/antares/Documents/GitHub/Nix/hosts/rpi5#nixosConfigurations.rpi5.config.systemd.services.qq-codex-agent.serviceConfig \
  --json
```

首次发布顺序：先推送包含 `nix/` 的应用版本，再在 dotfile 中更新
`qq-codex-agent` input，连同 dotfile 重构提交和锁文件一起部署。
旧的 input 不包含新模块，不能只部署 dotfile 修改。
不要将测试用的本地 `path:` 覆盖写进正式锁文件。

```sh
nix flake update qq-codex-agent --flake ./hosts/rpi5
```

后续更新同样只需推送应用、更新这一个 input，并在 Pi 上构建和切换。
在 Pi 验证服务启动、私聊和群聊提示词、GitHub/Nix 工具、自检及设备码登录；
本地测试不代替实机验收。

## 主动群聊版本升级

早期主动群聊升级曾移除每群 `users` 并引入时间／数量触发及累计参与率；当前版本不再接受这些中间版本字段。升级当前版本按下节操作。群表存在即开放全群，不再有群成员白名单；私聊 `private_users` 保持不变。
从更早的 schema v1 升级时，仍先保留 `sessions.pre-group-v2.sqlite` 并让旧群下次开启空白 thread、清除旧交接摘要；工作目录、模型、群设定和成员记忆保留。已经使用 schema v2 的群不执行这一步，按下节保留原 thread。
应用按成功发送回执记录自身消息，不依赖 NapCat 自身上报；若需要捕获同账号其他客户端发出的消息，开启对应 WebSocket 的 `reportSelfMessage`。任何断线漏报均不补抓。


## 群聊节奏 v3 迁移

先停止服务并备份状态目录、旧程序版本及旧加密配置。每群删除 `request_interval_minutes`、`request_message_count`、`participation_ratio`，改为 [配置与权限](../specs/configuration.md) 的三个秒级字段；不按旧值换算。主配置及可选 allowlist 文件都要检查；旧字段会阻止启动。

数据库自动备份到 `sessions.pre-cadence-v3.sqlite` 后事务升级，保留 thread、工作目录、图片索引、成员／群记忆及交接摘要，不主动清空会话。旧可识别的普通积压接受时效检查，旧直接 @ 保留义务，无法确认的旧引用保守按直接互动保留；进行中或提交结果未知的批次不重放。重启等待外部消息唤醒，不自动补跑。

RPi 的 `qq-codex-allowlist.age` 实为完整应用 TOML。使用 Nix 仓库的 agenix 编辑流程，只迁移群节奏字段，验证其他配置语义一致；明文不得写入 Git、Nix store 或日志。保留接收者、服务权限及挂载路径。先发布应用、更新 RPi 应用 source input，再将新程序和新密文配置同代部署。配置检查不解密，因此还须在受限临时环境中用新 Settings 解析明文，只输出成功／失败。

回滚需停服并同时恢复旧程序、对应旧配置及迁移前数据库；不能只回滚密文或二进制。Pi 系统只在 Pi 构建和切换，本机只做配置评估与本机架构应用包构建。

群消息分类升级通过 SQLite 增量增加 category 及系统消息元数据。保留旧 thread、历史内容、图片 ID 和冻结批次，不回写或清空已经发给模型的旧上下文；新消息才使用系统过滤、稳定媒体 ID 和 sticker_id。
