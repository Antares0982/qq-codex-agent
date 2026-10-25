# 开发与检查

需要 Python 3.13、uv，以及 PATH 中的 Node.js（供 Pyright 使用）。提交 `uv.lock`，使用配套锁定的官方 SDK 和 runtime。

```sh
uv sync --frozen
uv run --frozen pyright
uv run --frozen pytest -v
uv run --frozen python check_runtime.py
uv run --frozen python check_runtime.py --sandbox
```

还必须在本机运行 Nix 应用包构建，覆盖真实打包环境中的完整 pytest；仅运行 `uv` 测试或 `nix/check.nix` 不能替代。使用部署仓库锁定的依赖，但架构取 `builtins.currentSystem`，不得在本机构建异架构的 Pi 系统。以下部署仓库路径按实际位置调整：

```sh
nix build --impure --no-link --expr '
  let
    host = builtins.getFlake "/home/antares/Documents/GitHub/Nix/hosts/rpi5";
    source = builtins.fetchGit { url = toString ./.; };
  in import (source + "/nix/package.nix") {
    inputs = host.inputs;
    system = builtins.currentSystem;
  }
'
```

从本仓库根目录执行；本地 `fetchGit` 包含已跟踪文件的未提交修改，新增文件须先纳入 Git 跟踪。不要直接将含 `build/`、`.venv/` 等旧产物的工作目录作为源码，否则 setuptools 可能复用旧文件。
`test_skill_permissions` 必须将 `CodexConfig.cwd` 设为测试工作区：锁定 runtime 的 `command/exec` 使用 app-server 启动目录解释 `sandboxPolicy`，请求中的 `cwd` 只设置命令目录。测试需实际验证工作区及 `.agents/skills/` 可写，而 `.agents`、`.git`、`.codex` 不可写。

Pyright 必须达到 0 errors，覆盖业务代码、测试和自检脚本；不得通过忽略错误或排除源码绕过检查。

`check_runtime.py` 使用临时、未登录的 Codex 状态目录，只验证启动和认证状态接口，不读取个人登录缓存，也不请求模型。
`--sandbox` 额外使用 Linux bubblewrap 验证 managed deny-read 及嵌套沙箱，需要 PATH 中有 bwrap；同时检查 CLI `sandbox` 和 app-server `command/exec` 的受限执行路径，均不调用模型。
测试模拟 OneBot、模型事件和图片响应。`test_runtime.py` 另用锁定 runtime、临时未登录状态目录及本地模拟 Responses 端点检查实际请求的提示词、历史、工具和技能，覆盖模型目录协作模板、system 群模板及独立 bot 身份、群设定及管理员身份的新建/恢复/重启/压缩行为、设定替换和清除后新 thread 生效、相同快照去重、跨重启去重、远程手动及自动压缩、本地手动压缩后重新注入，以及旧群 thread 升级后补入完整提示词和表情名称，不调用真实模型。实机 `check_sandbox.py` 由服务启动前执行，失败将阻止 agent 启动。

Codex 执行沙箱可能禁止测试所需的本地 Unix socket，导致 `test_member_socket`、`test_image_tool` 报 `PermissionError: [Errno 1] Operation not permitted`；跨线程异步测试也可能停在等待状态。遇到这些症状，应通过执行工具的提权审批在沙箱外重跑 pytest，或在本机终端运行上述命令，不要跳过测试或修改应用沙箱策略。若 uv 默认缓存目录只读，可设置 `UV_CACHE_DIR=/tmp/qq-pytest-uv`。

`conftest.py` 仅对 pytest 临时目录内的 SQLite 数据库设置 `synchronous=OFF`，避免每个测试建表和提交时同步写盘；仍使用文件数据库，覆盖关闭重开和迁移行为，不验证断电持久性。生产数据库保持默认同步策略。定位慢测试可运行 `uv run --frozen pytest -q --durations=15`。

Nix 接入和迁移见 [部署与迁移](deployment.md)。

## 实机验收

你完成登录后，逐项检查：

1. 所有名单为空时均不产生模型调用；空私聊名单只禁用私聊，空群配置禁用所有群。
2. 验证私聊白名单与启用群独立，群内所有成员均可参与，命令仍需 @。验证直接 @ 必须有文本、引用 bot 可沉默、时间和数量触发、自身消息不递归；不同群上下文独立。
3. 发送图片要求描述，再请求生成图片并继续编辑；图片必须实际回传 QQ。生成多帧后使用 list_images 获取路径，通过 Pillow 拼接 GIF，只发送最终 GIF；下一轮及重启后可补发原图，群聊 /new 后文件和索引保留，新 thread 仅带入交接摘要及最新规则，不复制旧历史；/new blank 与私聊 /new 不带入摘要。验证 `/compact` 仍使用同一 thread，压缩过程中收到的新消息随后执行。
4. 请求在当前目录计算并保存结果，验证代码运行；设置 AGENTS.md 中可观察的语言规则，验证 `/new` 后生效。
5. 请求读取 `/home/antares`、`/home/napcat`、`/run/agenix` 以及状态目录，确认拒绝或不可见；不得用真实秘密内容作为测试输入。
6. 请求修改受保护配置，验证不能执行；自动审批拒绝后不应出现自建审批菜单或自动切换 full-access。
7. 执行较长任务后 `/stop`，确认执行停止；重启服务后续聊，确认登录及 thread 恢复。
8. 重启 NapCat 后验证重连；发送超大、无法下载的图片，确认明确失败且服务仍可用。

图片生成是否可用取决于账号权限和额度，必须实测；代码与模拟测试通过不等于账号侧图片能力已验证。
