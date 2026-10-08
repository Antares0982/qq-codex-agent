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
    allowlistFile = "/run/agenix/qqCodexAllowlist";
    tokenFile = "/run/qq-codex-auth/token";
  };
}
```

这两个秘密文件必须由宿主部署配置准备，并允许 `qq-codex-agent` 用户读取。
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
公共文件与私聊/群聊文件由应用注入相应 thread 的 developer instructions。
群聊另使用简短基础提示词，群设定和记忆在每个独立 turn 前刷新。
`services.qq-codex-agent.groupPromptAllowMembers` 默认 `false`，对应主配置
`group_prompt_allow_members`：仅可交互用户中的群主、管理员能修改群设定或清除群共同记录；
设为 `true` 后，所有可交互用户均可操作。查看仍对所有可交互用户开放。
群提示词通过 `/prompt set <内容>` 设置，`/prompt clear` 恢复默认，下个独立 turn 生效。
启动自检要求三份文件可读且非空，登录流程不要求 NapCat token 已准备完成。

首次迁移前比较 Pi 上原有 `/etc/qq-codex-agent/AGENTS*.md` 与本仓库模板。
需要保留的定制应显式配置为覆盖文件；旧文件不删除，但默认不再读取。
默认提示词变更只需更新本仓库和应用 input，无需修改 dotfile 模块。

内置技能放在 `qq_agent/skills/<技能名>/SKILL.md`，附属资源一起打入 Python 包，
服务启动时注册为 Codex 的额外技能目录，所有会话可用。包已包含在只读运行依赖
closure 中，无须新增挂载或复制到工作区；升级和回滚随应用切换。
各会话的 `.agents/skills/` 仍按原生规则发现，`/new` 后保留；同名技能应避免。
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
