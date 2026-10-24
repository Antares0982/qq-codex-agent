# 维护指南

本文件用于维护仓库，不是 QQ bot 的运行时提示词。公共运行时模板为 `AGENTS.runtime.md`，私聊和群聊模板分别为 `AGENTS.private.runtime.md` 和 `AGENTS.group.runtime.md`。
修改前阅读相关代码；优先小范围修改，运行测试后再提交。不提交凭据、真实白名单、认证缓存或聊天数据。
只在匹配架构的主机上构建 NixOS 系统。

## 代码结构

`qq_codex_agent.py` 保留命令行入口及自检脚本使用的配置导入，业务实现在 `qq_agent/`；主动群聊历史和调度规则由 `groups` 管理，JSON 回复、纠错及群 turn 生命周期由 `group_turns` 管理：

| 模块 | 职责 |
| --- | --- |
| `config`、`messages` | 配置校验；消息类型、解析及授权过滤 |
| `onebot`、`inputs`、`replies` | NapCat 通信；模型输入组装；回复、表情及群提醒 |
| `agent`、`commands` | 聊天调度、队列与取消；slash command |
| `runtime`、`turns` | thread 配置、恢复、压缩及订阅；单轮执行与 steering |
| `profiles`、`images`、`tool_server` | 成员聊天偏好；生成图片及交付；本地工具请求分发 |
| `storage`、`cleanup`、`logging`、`cli` | 数据库初始化；文件清理；日志脱敏；进程启动退出 |

组件显式组合，共用一个 SQLite 连接；调度状态由 `Agent` 管理，活动工具上下文由 `Turns` 管理。
业务 SQL、常量及提示词放在各自功能模块。测试直接导入所属模块，并在实际使用位置 mock。
`image_assets.py` 和 `image_tool.py` 分别保留图片文件安全操作与 MCP stdio 入口；提醒图片通过 `pics` 包资源加载。
`stickers/` 随包提供表情资源，`qq_agent.stickers` 在启动时加载并校验配置及资源。添加表情时加入图片并修改 `stickers/sticker_config.json`，运行 `uv run --frozen python compress_stickers.py` 后重新打包部署；无需改代码，不支持热重载。压缩脚本原地处理 PNG/JPEG/GIF/WebP，先将面积限制到 640,000 像素，再优化编码、减色或进一步缩小到 200,000 字节以内；保留透明度和动画时序，不额外备份。

## 行为

- 私聊通过 `private_users` 授权；群表存在即开放该群全员参与，未配置群不处理。空私聊名单与空群配置分别禁用对应入口。
- 群聊接收全员消息，无须 @；直接 @ 要求文本回复，引用 bot、时间和数量阈值也可触发。群 slash command 仍须 @，管理操作保留群角色检查。逐群配置及完整协议见 `group-design.md`。
- 每个私聊独立上下文，同群授权用户共享上下文；群回复对所有群成员公开。消息间隔超过两小时且当前上下文估算超过 100,000 tokens 时，在原 thread 先压缩再提交新消息；私聊和群聊均提示。
- `/new` 中断本会话任务并清空等待任务。私聊后续创建空白 thread；群聊在旧 thread 额外执行总结 turn，使用当前模型及 medium，生成非空、最多 2000 字符的结构化摘要，成功后下条消息携摘要创建新 thread。仅归纳群成员表达及上次交接背景，不归纳 assistant 回复、工具输出、system/developer 指令和群设定，不推断任务完成；这只是对当前可见上下文的提示词约束，不保证绝对角色隔离或完整原始历史覆盖。总结不注册 QQ 活动工具上下文，不发送模型正文或生成图片，只提示状态；要求不调用工具，但不改变 runtime 的工具权限。总结最长 15 分钟（若配置的任务期限更短则取较短值）。总结期间普通消息继续积累，结束后调度已触发批次，`/status` 显示忙碌，`/stop` 可取消。群聊 `/new blank` 可取消总结并直接空白重置、清除交接摘要；私聊不新增此命令。总结失败、超时、取消或输出无效时保留旧 thread，不自动重试，已提交的总结请求不回滚。`group_handoffs` 按群保存摘要，与 `sessions.renew` 在同一事务提交，待切换状态跨重启保留；未完成总结不自动恢复。已经待切换时重复 `/new` 不再次总结。摘要作为参考数据参与新群 thread 的完整提示词快照及压缩后补注入，不授权执行旧请求。工作文件、图片索引、模型、群设定及聊天偏好均保留；旧 Codex 历史仍保留。
- `/stop` 中断本会话任务并清空本会话队列；群内任一授权用户均可使用。
- `/status` 查看登录状态、忙碌状态、上下文长度及已提交的用户消息数。上下文长度取当前 thread 最近上报并持久保存的 token 估算，不是累计消耗；已有 thread 但缺少估算时显示“暂未知”。消息数取自 Codex thread 历史，不包含排队消息；普通 slash command 不提交历史，但群聊总结会增加一条内部总结请求，失败或取消后可能仍计入旧 thread；读取失败不影响上下文估算显示。尚未创建或 `/new` 待切换时两者显示 0。群内命令也需要 @ bot。
- `/help` 查看使用方法及全部指令。
- `/model` 从 Codex 获取可选模型列表；`/model <模型ID>` 切换本会话模型，下一轮请求生效，不打断正在运行的任务，也不清空上下文。
- 模型选择按会话持久保存，同群共享，重启及 `/new` 后保留。仅列出支持 `medium` 的非隐藏模型，每轮推理强度固定为 `medium`。
- 私聊与各群并发执行，各聊天独立调度。同一聊天处理中收到的新消息按到达顺序通过 steering 追加到当前 turn，由 runtime 在工具调用结束等可接收输入的位置处理；如果 turn 已结束，则开启下一轮。私聊最多等待 8 条未提交消息，每条最多 5 张图片、每张 10 MiB；群聊使用持久化历史和批次，不沿用单消息队列限制。任务初始期限为 15 分钟，成功追加消息后重新给予 15 分钟，追加失败不延长。
- 自动压缩阈值固定为 200,000 tokens，按完整有效上下文统计，不修改模型真实窗口大小。取消历史迁移、文字截断及 8 MiB 图片换 thread 逻辑；原图、图片索引和完整聊天历史保留，压缩不保证移除所有图片或降低总网络流量。上下文大小取 runtime 最近一次 token usage 的近似值，按 thread 持久保存，恢复时接收更新；缺失时最多等待两秒，仍未知则跳过空闲压缩。
- `/compact` 手动压缩原会话，不提交用户消息；会话忙碌时提示稍后重试，无 thread 时不调用模型。私聊的手动、空闲和自动压缩，以及群聊显式 /compact 开始时发送“正在压缩上下文……”；同一压缩事件不重复提示，同轮再次压缩仍提示。`/compact` 保留完成或失败回复。压缩期间新消息排队，受原有 15 分钟期限及 `/stop`、`/new` 控制，结束后释放订阅。空闲压缩明确失败且已结束时继续提交新消息，状态未知时停止并沿用 runtime 重启保护。
- 每轮结束或中断后取消 thread 订阅；换 thread 和 `/new` 也释放旧订阅。Codex 在无订阅且空闲约 30 分钟后回收 thread 及其 MCP 子进程，不要求进程立即退出。
- 重复消息不重跑；进程崩溃时未完成任务不自动重试。网络断开时发送失败会记录日志，不自动重发不确定的结果。
- 仅支持文本和图片。生成图片从 Codex 结构化事件提取 base64，由应用解码并原子写入会话工作区 `artifacts/`，不会自动发送。`qq_image.list_images` 返回最近 100 张原图的生成 ID、顺序及相对路径；`qq_image.send_image(path=...)` 发送工作区中的 PNG/JPEG/GIF/WebP（最大 32 MiB），省略路径发送最新原图。拒绝跨工作区路径、符号链接及特殊文件。
- 图片文件和原图索引跨轮、重启及 `/new` 后保留。可用随应用安装的 Pillow 加工多帧并发送最终 GIF；中间帧无须发送。成功发送不删除源文件，同轮重复调用去重；发送结果未知时不自动重试，仅在用户明确要求时使用 `resend=true`。
- 群回复采用 JSON 的 response/sticker 协议，不再提供 send_sticker 工具和每 turn 一次额度。sticker 必须为配置键，配置值可为 null；先发表情再发文本。普通图片使用 sub_type=0，表情和提醒使用 sub_type=1。
- 服务启动时清理一次，此后每 24 小时清理超过 14 天未访问或修改的普通文件，以文件系统 `max(atime, mtime)` 为准；不额外追踪读取。跳过执行或重置中的聊天，不跟随符号链接，保留聊天根目录和映射，同步清理过期或已不存在文件的图片索引及发送记录。工作区内各级 `.agents/skills/` 及其全部资源、内置技能、认证、旧聊天历史、模型选择及成员聊天偏好不参与清理。
- 私聊引用保留原来的文本/图片合并及 5 张图片限制。群聊引用、文本、@、图片索引和不可读占位保持原顺序；引用带原发送者和时间，原时间缺失明确标记未知，不递归展开引用。图片通过 qq_image.view_image(image_id) 按需查看和下载，每张下载最多 10 MiB。
- 群聊首次成功发出 JSON 对应文本或表情后开始 30 秒/5 分钟提醒；每条完整 assistant 消息重置，turn 结束取消。合法沉默无兜底回复。连续三次解析错误发送一次熔断提示；普通自主任务尚未发言时的错误只记日志，待回复 @ 或已经发言时保留失败反馈。私聊继续显示原有进度。
- 删除群聊 OK 表情、记忆写入及自动压缩通知；显式命令保留操作结果。自身消息记录历史和参与率，不触发请求、不增加触发计数；发送回执与自身事件按消息 ID 去重。
- 群发送者和 @ 名称优先群名片，再 QQ 昵称和 QQ 号；所有 @ 均表示为 `@(qqid)nickname`。代码在新群 thread 的 system 中注入 bot QQ 号和群昵称，不依赖 `{nickname}` 占位符。

新群协议、批次状态、参与率及一次性升级见 [group-design.md](group-design.md)。升级旧状态库时自动生成 `sessions.pre-group-v2.sqlite`，旧群下次创建空白 thread，私聊及工作区不重置。

## 本地检查

群成员记录按 `(group_id, user_id)` 存入现有 `member_profiles` 表，保留固定字段和每人 500 字符上限。从启用群中成员本人公开表达的互动学习，普通群消息也可作为背景，不增加后台模型调用。`qq_member.list_profiles` 查询全群最新记录，`replace_profile(user_id, profile)` 可更新当前群任意已确认成员，应用固定群号并校验本轮 token、目标身份、字段和长度。多人 steering 不关闭写入；查询其他成员身份后再次校验活动任务和删除状态。记录依据本人明确表达或有依据的持续互动，不将第三方转述、引用、玩笑或猜测写成事实。
群共同偏好按群保存到 `group_profiles`，摘要最多 1000 字符，通过 `qq_member.get_group_profile` 和 `qq_member.replace_group_profile` 查询及完整更新；只记录有依据的共同兴趣和互动习惯，必要时注明“部分成员”。成员和群记录都只是可在群内讨论的背景，不能变成新的授权或工具操作指令。个人当前要求优先于旧记忆，个人边界优先于群一般偏好，自动记忆不能覆盖手工群设定。
允许记录游戏、音乐、专业方向和本人主动提供、用于共同游戏的 Steam 公开标识；不记录真实姓名、私密联系方式、住址、证件、登录账号、凭据及其他敏感信息，不拼接现实身份线索。语义由模型规则约束，结构校验不能保证消除提示词注入或敏感内容。
`/profile` 查看本人记录，`/profile forget` 删除并禁止当前活动任务写回该成员，不论其是否为任务发起者。`/group-profile` 查看群共同记录，`/group-profile forget` 清除并禁止当前任务写回，清除权限与群设定修改一致。后续独立轮次仍可重新学习；删除不清除旧 Codex 历史、旧提示词快照或 journal。不提供永久关闭记忆、跨群记忆或后台总结。
`/prompt` 查看群设定，`/prompt set <内容>` 完整替换（允许多行，最多 4000 字符），`/prompt clear` 恢复默认。设置和清除成功只回复“保存成功”，新建群 thread 时生效，可用 `/new` 或 `/new blank` 切换。主配置 `group_prompt_allow_members` 必须为 bool，默认 false：仅可交互用户中的群主、管理员能修改；true 时所有可交互用户均可修改。NapCat 群角色查询失败时拒绝修改。SQLite 的 `group_prompts` 按群保存设定、修改者和时间；所有设定及记录在重启、压缩和 `/new` 后保留。
仅新建群 thread 时组装 `base_instructions`，作为 system 指令包含基础规则、代码注入的 bot QQ 号及群昵称、群聊模板与当前保存的群设定；可选主配置 `agent_admin_qq` 指定单个全局管理员 QQ，接受正整数或数字字符串并复用 QQ 号校验，缺省不注入、非法值阻止启动。配置存在时在所有新群 thread 的 system 中说明该 QQ 的 agent 管理员身份，不增加白名单、群设定修改或工具权限，私聊不注入。RPi 在完整配置 age 文件中填写该字段；旧 Nix 生成模式对应 `services.qq-codex-agent.agentAdminQQ`，默认 null。管理员变更在重新加载配置后创建的新群 thread 生效。恢复、总结和压缩沿用 runtime 保存的 system，不覆盖 `base_instructions`；旧 thread 不补写 system、不清理历史。基础规则保留文件读取、修改、创建、执行、网页搜索及 QQ 工具使用说明，runtime 继续提供工具定义、技能和权限信息。群聊在每个独立 turn 开始前重新读取公共提示词文件，并组装完整提示词快照：应用规则、QQ 图片交付说明、Pillow 路径、表情规则与全部可用名称、公共模板、最新全群记忆和交接摘要。与 SQLite `group_snapshots` 按 thread 保存的上次成功注入内容比较；相同则跳过，不同才通过 `thread/inject_items` 显式注入 developer 消息，成功后保存完整快照。表情 JSON 仍仅在进程启动时加载一次，其名称列表参与轮前快照比较。旧群 thread 的应用规则与记忆也按内容差异补注入，不需要 `/new`，群模板、群设定及管理员 system 需新建 thread 生效。私聊保留原有新建/恢复时传入提示词的机制，不参与本次轮前快照逻辑，也不提供表情功能。最新应用规则与记忆快照替代旧快照，不包含群模板、群设定和管理员身份。去重跨重启保留，新 thread（包括 `/new`）首次必注入；手动、空闲及自动压缩完成后使去重记录失效，下次独立 turn 补注入，轮前空闲压缩则在本轮启动前补注入。不能借用 `collaborationMode.settings.developer_instructions`：模型目录中的协作模板会优先覆盖该字段。群 thread 开启 `features.retain_client_developer_messages`，使远程 v2 手动、轮前及轮中自动压缩保留客户端 developer 消息。不依赖 resume 刷新已加载 thread，不把动态快照重复放入初始化指令。更新失败不提交新 turn。当前 turn 的 steering 沿用当前规则；应用规则与记忆变更下个独立 turn 生效，群模板及群设定变更新建 thread 生效，历史保留。普通消息及 steering 不附加偏好文本。

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
request_interval_minutes = 60
request_message_count = 20
participation_ratio = 5.0

[groups."876543210"]
request_interval_minutes = 60
request_message_count = 20
participation_ratio = 5.0
```

解密文件位于 `/run/agenix/qqCodexAllowlist`，仅专用服务用户可读。
Nix 设置 `services.qq-codex-agent.configFile = config.age.secrets.qqCodexAllowlist.path`，
直接只读挂载为 `/etc/qq-codex-private/config.toml`，启动命令显式传入该路径；
整个目录禁止 Codex 工具读取。完整配置不合并 Nix 默认值、不设置 `allowlist_file`，
不将 QQ 号或配置明文写入公开 Nix 配置、Git 或 Nix store。
`configFile` 模式忽略用于生成应用字段的 `allowlistFile`、`napcatUrl`、`groupPromptAllowMembers`、`agentAdminQQ`、`authSocket`。
提示词资源、Codex runtime 配置、token、共享认证和服务挂载仍由 Nix 管理；配置中的路径须匹配挂载。
空名单不放行任何用户；文件缺失或格式错误会阻止启动。修改后重新部署，服务自动重启读取。
独立运行仍可使用 `config.example.toml`；CLI 默认配置路径保持兼容。Nix 不设置 `configFile` 时保留原生成方式，要求 `allowlistFile`，主配置也挂载到新的受保护路径；应用设置 `allowlist_file` 时仍仅接受外部白名单字段。
上述示例开放两个配置群的全员参与，不开放其他群或额外私聊用户。旧 groups.users 配置会阻止启动，需改为新逐群配置。
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

## 共享 ChatGPT 登录

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

## 提示词和目录边界

默认修改本仓库的三份 `AGENTS*.runtime.md`，随应用重新部署。需要本机定制时，在 Nix 配置中设置 `services.qq-codex-agent.agentsFile`、`privateAgentsFile`、`groupAgentsFile` 为自定义文件的绝对路径；不要让秘密内容进入 Nix store。群聊每个独立 turn 前重读公共提示词文件并按完整快照去重，公共模板更新下个独立 turn 生效。群聊模板仅在新建 thread 时读入 system，更新后需 `/new` 或 `/new blank`；恢复、总结和压缩不重读群模板或替换昵称。Nix store 提示词或挂载目标变化仍需重新部署；私聊沿用重启 agent 后 `/new` 的更新方式。首次迁移前比较 Pi 上原有三份 `/etc/qq-codex-agent/AGENTS*.md`，将需要保留的手工修改显式配置为覆盖文件；迁移不删除旧文件。
公共文件与私聊模板沿用新建和恢复 thread 的 `developer_instructions`；群聊的公共文件通过上述轮前 developer 快照显式注入，群聊模板仅在新建 thread 时写入 system。`agents_file`、`private_agents_file`、`group_agents_file`
由 Nix 模块统一生成并只读挂载；启动校验三份文件可读且非空。应用同时注入 QQ 文件交付
规则和 Pillow Python 路径。工作区内 AGENTS.md 保持原生层级加载。
会话工作目录会被显式标记为可信项目。工作目录下新建的 AGENTS.md 遵循 Codex 自身规则，不能改变应用 allowlist 或宿主挂载。

服务的私有根目录仅挂载所需运行时 Nix closure、只读应用和配置、专用状态与工作目录，以及 DNS/hosts 和必要虚拟文件系统。
默认不挂载宿主完整 `/nix/store`、`/home`、`/etc`、`/run` 或 `/var`；当前 Pi 对 store 和 Nix daemon 的额外授权见上文。
工作区为 `/var/lib/qq-codex-work`，每个私聊或群聊始终使用同一个子目录，切换 thread 和 `/new` 均不改变目录；这提供会话组织，不是授权用户之间的强多租户隔离。
会话状态位于 `/var/lib/qq-codex-agent`，共享认证位于独立用户目录。不可修改的 Codex managed requirements 禁止工具读取该目录和 NapCat token；外层文件系统仍约束自动审批后的访问范围。
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
2. 验证私聊白名单与启用群独立，群内所有成员均可参与，命令仍需 @。验证直接 @ 必须有文本、引用 bot 可沉默、时间和数量触发、自身消息不递归；不同群上下文独立。
3. 发送图片要求描述，再请求生成图片并继续编辑；图片必须实际回传 QQ。生成多帧后使用 list_images 获取路径，通过 Pillow 拼接 GIF，只发送最终 GIF；下一轮及重启后可补发原图，群聊 /new 后文件和索引保留，新 thread 仅带入交接摘要及最新规则，不复制旧历史；/new blank 与私聊 /new 不带入摘要。验证 `/compact` 仍使用同一 thread，压缩过程中收到的新消息随后执行。
4. 请求在当前目录计算并保存结果，验证代码运行；设置 AGENTS.md 中可观察的语言规则，验证 `/new` 后生效。
5. 请求读取 `/home/antares`、`/home/napcat`、`/run/agenix` 以及状态目录，确认拒绝或不可见；不得用真实秘密内容作为测试输入。
6. 请求修改受保护配置，验证不能执行；自动审批拒绝后不应出现自建审批菜单或自动切换 full-access。
7. 执行较长任务后 `/stop`，确认执行停止；重启服务后续聊，确认登录及 thread 恢复。
8. 重启 NapCat 后验证重连；发送超大、无法下载的图片，确认明确失败且服务仍可用。

图片生成是否可用取决于账号权限和额度，必须实测；代码与模拟测试通过不等于账号侧图片能力已验证。

## 排障

在 Pi 查看 `sudo journalctl -u qq-codex-agent -n 100 --no-pager`。
INFO 日志包含授权消息的发送者、正文、图片数量、引用消息 ID、回复正文、图片交付路径和大小，以及 thread/turn、工具状态、压缩结果和任务耗时。错误事件及终态错误直接进入 journal，超时与主动取消分别标注；不记录工具的图片 Base64 或认证头。常见 token、API key、密码字段会脱敏，文本换行转义；任意自然语言秘密无法保证自动识别。journal 含私聊与群聊内容，仅向受信任管理员开放，按宿主 journald 策略保留。
每轮开始记录所选模型（未指定时为 `default`）、推理强度和请求前上下文 token 估算，结束记录 turn ID。后台每 5 秒只读增量提取锁定 runtime 的 `codex/logs_2.sqlite`，将 Responses 请求的压缩前后字节数、压缩耗时、HTTP 状态码及可用的 request/thread/turn/model 标识写入 `Codex request diagnostic`；时间戳为原始日志的 Unix 秒，可用于比较成功与失败请求。仅输出白名单字段，不复制请求正文、URL 或响应头；启动时跳过已有记录，数据库不可用时警告并继续聊天。该提取器依赖 runtime 日志格式，升级时由本地模拟端点测试验证；未压缩请求没有压缩大小记录。
启动前检查失败会阻止服务运行，优先查看 traceback；不要通过删除沙箱检查或放宽目录权限绕过错误。
Codex 0.159.2 的多个文件级 deny-read 会触发 bubblewrap 文件描述符复用错误。
部署改用整个目录隔离，技能与插件 cache 放在 `/var/lib/qq-codex-resources`；
`CODEX_HOME/tmp/arg0` 只读，让官方 runtime 使用自身可执行文件回退入口。
PATH aliases 的只读警告是预期现象；必须以实际 thread、沙箱和资源读取自检为准。
不要开放私有状态或关闭沙箱。详情见 `nix/README.md`。
遇到 `Unknown allowlist fields`，确认完整配置已通过新模块的 `configFile` 接入而非旧白名单入口，并核对实际运行的源码版本；重启服务不会更新 flake 锁定的旧代码。
登录失败检查登录 unit 日志和本地代理；不要输出或分享 token、设备码及认证文件。
推送源码后必须更新 Nix input、同步锁文件并在 Pi 切换系统；只更新应用仓库不会更新运行中的服务。
