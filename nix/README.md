# Nix 部署

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
完整示例见仓库维护指南；`config.example.toml` 是独立运行示例，Nix 部署还须启用其中
`auth_socket`、`resources_dir` 两行。顶层字段放在 `[groups."群号"]` 等表声明之前。
必需字段缺失、格式错误或文件不可读会阻止启动；空名单仍不放行任何用户。

设置 `configFile` 后，`allowlistFile`、`napcatUrl`、`groupPromptAllowMembers`、
`agentAdminQQ`、`authSocket` 不再生成应用字段；也不再挂载单独白名单。
提示词资源、`codexSettings`、token、共享认证和其他服务挂载仍由 Nix 管理。
完整配置内的目录和路径必须匹配现有挂载，填写新路径不会增加宿主访问权限。
不设置 `configFile` 时保留原生成模式，此时必须设置 `allowlistFile`；
生成配置也使用新的受保护路径，应用 CLI 默认路径仍保留供独立运行兼容使用。

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

## 共享认证

模块要求宿主提供 `codex-auth.service`、`codex-login.service` 和 `codex-auth-clients` 组。
`authSocket` 默认为 `/run/codex-auth/auth.sock`；QQ 用户加入该组，socket 只挂载给宿主
应用，managed deny-read 禁止模型工具读取。认证 broker 随本包的 `codex-auth` 命令安装。
RPi 的共享服务和一次性 `codex-auth-import` 由 Nix 仓库维护；令牌只由该服务刷新。
升级前停止消费者、备份旧状态并迁移登录，旧 QQ CODEX_HOME 继续保留会话历史。

## 提示词

默认文件直接来自应用包，升级和回滚会同时切换代码与提示词。
`agentsFile`、`privateAgentsFile`、`groupAgentsFile` 可分别指定覆盖文件的绝对路径。
私聊继续在新建/恢复 thread 时传入公共及私聊模板，不提供表情工具。
私聊和群聊关闭 runtime 的 `multi_agent_role` 与 `collaboration_mode` 提示词注入，其他 developer 提示词保留。同时关闭 `recommended_plugins` 和 `tool_suggest`，停用插件推荐上下文与安装推荐工具，保留已安装插件。已有历史中的旧提示词不会被删除，部署后可用 `/new` 创建不含这些提示词的新上下文。
群聊在每个独立 turn 前重读公共及群聊模板，将应用规则、表情名称与记忆纳入完整快照；与上次成功注入内容相同则跳过，否则显式注入并持久保存。应用规则更新无需 `/new`，压缩后下一轮补注入；表情 JSON 仍仅启动时加载。
`services.qq-codex-agent.groupPromptAllowMembers` 默认 `false`，对应主配置
`group_prompt_allow_members`：仅可交互用户中的群主、管理员能修改群设定或清除群共同记录；
设为 `true` 后，所有可交互用户均可操作。查看仍对所有可交互用户开放。
群提示词通过 `/prompt set <内容>` 设置，`/prompt clear` 恢复默认，仅在新建群 thread 时通过 `base_instructions` 写入 system；可用 `/new` 或 `/new blank` 切换。恢复、总结和压缩不覆盖已保存的 system，旧 thread 不自动迁移。
完整配置模式直接在 age 文件内设置 `agent_admin_qq`。旧生成模式的可选 `services.qq-codex-agent.agentAdminQQ = "123456789"` 对应主配置 `agent_admin_qq`，默认 `null` 时省略该字段。配置的 QQ 仅作为 agent 管理员身份写入所有新群 thread 的 system，不增加应用权限、不影响私聊；变更配置后重新部署并新建群 thread 生效。
启动自检要求三份文件可读且非空，登录流程不要求 NapCat token 已准备完成。

首次迁移前比较 Pi 上原有 `/etc/qq-codex-agent/AGENTS*.md` 与本仓库模板。
需要保留的定制应显式配置为覆盖文件；旧文件不删除，但默认不再读取。
默认提示词变更只需更新本仓库和应用 input，无需修改 dotfile 模块。

内置技能放在 `qq_agent/skills/<技能名>/SKILL.md`，附属资源一起打入 Python 包，
服务启动时注册为 Codex 的额外技能目录，所有会话可用。包已包含在只读运行依赖
closure 中，无须新增挂载或复制到工作区；升级和回滚随应用切换。
各会话的 `.agents/skills/` 仍按原生规则发现，`/new` 后保留；同名技能应避免。
创建和恢复 thread 前安全创建该目录、拒绝符号链接，并仅将当前会话的
`.agents/skills/` 追加到有效配置的可写目录，保留宿主配置的缓存等可写路径。
`.agents` 其余内容以及 `.git`、`.codex` 仍受 runtime 默认只读保护。
工作区 `.agents/skills/` 及其全部资源、内置技能均不参与 14 天文件清理。

## 检查与发布

运行仓库维护指南中的 Python 测试和 runtime 检查。使用 dotfile 已锁定的
nixpkgs 执行本机架构的部署检查：

```sh
nix build --impure --no-link --expr '
  let host = builtins.getFlake "/home/antares/Documents/Nix/hosts/rpi5";
  in import ./nix/check.nix { nixpkgs = host.inputs.nixpkgs; }
'
```

检查覆盖默认及自定义提示词挂载、只读辅助目录、独立公共资源路径、
token 挂载和实际生成的 TOML。打包阶段检查三份模板存在且非空，并执行 Python 测试。
表情加载先解析安装包目录的真实路径，兼容 Nix 虚拟环境的目录符号链接；包内图片仍拒绝符号链接和越界路径。打包阶段另用隔离模式从实际安装环境加载表情，避免源码目录掩盖安装布局问题；无需额外挂载表情目录。
本机可用本地源码覆盖 input 求值完整 Pi 服务，不改锁文件：

```sh
nix eval --no-write-lock-file \
  --override-input qq-codex-agent "path:$PWD" \
  /home/antares/Documents/Nix/hosts/rpi5#nixosConfigurations.rpi5.config.systemd.services.qq-codex-agent.serviceConfig \
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
