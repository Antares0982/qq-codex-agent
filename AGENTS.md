# 维护指南

本文件用于维护仓库，不是 QQ bot 的运行时提示词。

## 代码维护

- 修改前阅读相关代码和调用方，优先小范围修改；业务 SQL、常量及提示词放在所属功能模块。
- 组件显式组合，共用 SQLite 连接；调度状态由 `Agent` 管理，活动工具上下文由 `Turns` 管理。测试直接导入所属模块，在实际使用位置 mock。
- 使用 Python 3.13、uv、PATH 中的 Node.js 和锁定的 SDK/runtime；提交 `uv.lock`。
- 提交前完成 [开发检查](docs/internal/development.md)：Pyright 必须 0 errors，完整 pytest、两种 runtime 自检及本机架构 Nix 应用包构建均必需。不得通过忽略错误、排除源码或跳过测试绕过检查。
- 只在匹配架构的主机上构建 NixOS 系统；Pi 系统仅在 Pi 构建和切换。
- 不提交凭据、真实白名单、认证缓存、聊天数据或导出的 transcript；不通过放宽沙箱权限修复自检。

## 项目结构

| 路径或模块 | 职责 |
| --- | --- |
| `qq_codex_agent.py` | CLI 入口及自检使用的配置导入 |
| `qq_agent/config`、`messages` | 配置校验、消息解析与授权 |
| `qq_agent/onebot`、`inputs`、`replies` | NapCat 通信、输入组装、回复和提醒 |
| `qq_agent/agent`、`commands` | 调度、队列、取消和 slash command |
| `qq_agent/groups`、`group_turns` | 群历史与批次、JSON 回复与纠错 |
| `qq_agent/runtime`、`turns` | thread 配置、恢复、压缩、单轮执行和 steering |
| `qq_agent/profiles`、`images`、`tool_server` | 聊天偏好、图片交付、本地工具分发 |
| `qq_agent/storage`、`cleanup`、`logging`、`cli` | 数据库、清理、日志、进程生命周期 |
| `qq_agent/auth`、`auth_client` | 共享认证 broker 与客户端 |
| `image_assets.py`、`image_tool.py` | 图片文件安全操作、MCP stdio 入口 |
| `pics/`、`stickers/`、`compress_stickers.py` | 提醒和表情资源、图片压缩 |
| `qq_agent/skills/` | 随 bot 发布的内置技能 |
| `.agents/skills/` | 仓库维护者技能，不随 bot 发布 |
| `nix/` | 服务模块、应用打包及部署检查 |
| `test_*.py`、`check_*.py` | 自动测试和 runtime/沙箱自检 |
| `AGENTS*.runtime.md` | 公共、私聊、群聊运行时模板 |
| `docs/` | 文档入口、行为契约及内部说明 |

## 文档维护

- [README](README.md) 只介绍用途、基本用法及部署形态，仅引用 [docs/](docs/README.md) 作为文档入口。
- `docs/specs/` 是行为契约：配置与接口、可见行为、约束、失败语义；不写表结构、模块调用、runtime 接线和运维步骤。
- `docs/internal/` 保存实现、开发检查、部署迁移、排障及资源维护；行为规则链接到 specs，不复制维护。
- 每项规则只保留一个权威说明位置；修改行为时同步对应 spec，实现或操作方式变化时更新 internal，并维护文档入口及相对链接。
- 迁移文档时核对代码和测试，保留限制、失败语义及恢复步骤，修正文档冲突；不能借整理文档改变应用行为。
- `SKILL.md` 必须留在技能发现目录；普通技能维护说明放入 internal。运行时模板独立维护，不能把仓库维护指南注入 bot。
