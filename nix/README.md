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
通过 `systemd.packages` 安装到两个 unit 的 drop-in，不在求值时构建其他架构的包。
`codex/tmp/arg0` 供 runtime 创建和读取辅助入口。`codex/skills` 和
`codex/plugins/cache` 允许工具读取技能、插件及其资源，不授予写入权限。
managed deny-read 通过逐层排除这些路径保护其余所有状态文件，包括未来新增文件；
父目录可列出，但敏感内容不可读。
拒绝规则使用 `**/父目录/模式` 保留路径层级；runtime 将模式交给 ripgrep 扫描，
只有文件名的模式会匹配任意深度，误遮蔽技能资源。部署检查覆盖实际 ripgrep 扫描。
服务禁用 shell snapshot，避免从受保护状态目录挂载 shell 环境快照。
启动自检验证 thread 创建、技能和插件资源只读，以及各层相邻状态文件的读取隔离。

## 提示词

默认文件直接来自应用包，升级和回滚会同时切换代码与提示词。
`agentsFile`、`privateAgentsFile`、`groupAgentsFile` 可分别指定覆盖文件的绝对路径。
公共文件同时挂载到 `CODEX_HOME/AGENTS.md`；私聊和群聊文件注入相应 thread。
启动自检要求三份文件可读且非空，登录流程不要求 NapCat token 已准备完成。

首次迁移前比较 Pi 上原有 `/etc/qq-codex-agent/AGENTS*.md` 与本仓库模板。
需要保留的定制应显式配置为覆盖文件；旧文件不删除，但默认不再读取。
默认提示词变更只需更新本仓库和应用 input，无需修改 dotfile 模块。

## 检查与发布

运行仓库维护指南中的 Python 测试和 runtime 检查。使用 dotfile 已锁定的
nixpkgs 执行本机架构的部署检查：

```sh
nix build --impure --no-link --expr '
  let host = builtins.getFlake "/home/antares/Documents/Nix/hosts/rpi5";
  in import ./nix/check.nix { nixpkgs = host.inputs.nixpkgs; }
'
```

检查覆盖默认及自定义提示词挂载、公共提示词的 Codex 路径、可写辅助入口、
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
