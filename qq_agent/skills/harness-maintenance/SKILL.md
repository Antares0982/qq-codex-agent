---
name: harness-maintenance
description: 当用户要求维护、修复或扩展本 QQ agent 自身的 harness（qq-codex-agent 的调度、工具、配置、运行时提示词或内置技能）时使用；通过 agent 专用 GitHub 账户 fork 上游主分支、开发并提交 PR。普通聊天任务或其他项目维护不触发。
---

# Harness 维护

上游仓库固定为 `Antares0982/qq-codex-agent`，不是当前聊天工作区中的任意仓库。
用户要求维护自身 harness 时，完成从 fork、开发、测试到向上游提交 PR 的流程。
使用现有已认证的 `gh` CLI，当前认证账户是 agent 专用账户。

## 认证与工作副本

1. 首先执行 `gh auth status --hostname github.com --active`。`gh` 不可用、未认证或认证失败时立即终止流程，简要说明原因；不要发起登录、索取 token、切换账户或改用其他身份。
2. 用 `gh api user --jq .login` 获取专用账户名，用 `gh repo view Antares0982/qq-codex-agent --json defaultBranchRef --jq .defaultBranchRef.name` 查询上游主分支。任一步失败均停止，不猜测账户或分支。
3. 执行 `gh repo fork Antares0982/qq-codex-agent --default-branch-only --clone=false --remote=false`，在当前认证账户下创建或复用 fork。验证目标 fork 的 owner 是该账户、parent 是上游仓库；不覆盖无关同名仓库。
4. 在当前聊天工作区内选择新的目录，用 `gh repo clone <账户>/qq-codex-agent <目录>` 克隆 fork。确认 `origin` 指向专用账户的 fork、`upstream` 指向上游；缺少 `upstream` 时添加。不在安装目录、Nix store 或已有脏工作副本中开发。
5. `git fetch upstream <主分支>` 后，以刚获取的 `FETCH_HEAD` 创建唯一任务分支（`git switch -c <任务分支> FETCH_HEAD`），记录 `git rev-parse HEAD`。这里的 HEAD 指上游主分支最新提交，不使用可能过期的 fork 主分支或运行中安装版本作为基线。

## 开发与提交

阅读克隆仓库内的 `AGENTS.md` 和相关代码，按用户请求做最小修改，并运行仓库要求的检查。
凭据、认证缓存、真实白名单和聊天数据不得进入 Git 或 PR；测试遵守仓库的临时未登录状态与架构限制。
如需设置提交者信息，仅在当前克隆配置，使用专用账户身份。

检查 diff，只提交本任务文件；测试通过后，将任务分支推送到 fork 的 `origin`，不直接推送上游主分支、不强推。
Git 的 HTTPS 操作复用 `gh` 的认证；需要时在单次命令中使用
`git -c credential.helper= -c 'credential.helper=!gh auth git-credential' push -u origin <任务分支>`，不提取或输出 token。

将 PR 正文写入临时文件，说明具体问题、修改行为及验证结果，再执行：

```sh
gh pr create --repo Antares0982/qq-codex-agent \
  --base <上游主分支> --head <账户>:<任务分支> \
  --title '<简明标题>' --body-file <正文文件>
```

创建结果不确定时，先查询该 head 是否已有 PR，避免重复创建。认证、权限或检查失败时停止后续发布，保留已有工作并报告失败步骤。
最终返回 PR 链接和检查结果；此流程止于开 PR，不自动合并、部署或重启正在服务的 agent。

命令参考：[认证状态](https://cli.github.com/manual/gh_auth_status)、[fork](https://cli.github.com/manual/gh_repo_fork)、[创建 PR](https://cli.github.com/manual/gh_pr_create)。
