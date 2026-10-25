# 配置与权限

应用配置为 TOML，完整字段示例见 [config.example.toml](../../config.example.toml)；启动与部署方式见 [部署文档](../internal/deployment.md)。

## 入口与校验

- `private_users` 为 QQ 号数组，仅允许其中用户私聊；`groups` 中存在的群允许全员参与，未配置群不处理。两个入口分别由空名单和空群配置禁用。
- QQ 号接受正整数或 ASCII 数字字符串，规范化为十进制字符串；不接受 bool、零和负数。规范化后重复的群号无效。
- 每群配置 `request_interval_minutes`、`request_message_count` 必须为正整数，默认分别为 60、20；`participation_ratio` 必须为有限数值且至少为 1，默认 5.0。不接受 bool、未知群字段或旧 `groups.users`。
- 不接受旧 `allowed_users`、`allowed_groups`。可选 `allowlist_file` 必须为绝对路径，其文件仅接受 `private_users` 和 `groups`，替换主配置中的这两项；不能将完整应用配置作为白名单文件。
- `napcat_url` 必须为不含查询参数、用户名的回环 `ws://` 地址。`token_file` 必须存在，内容可为空；非空时用于连接认证。
- `token_file`、`state_dir`、`workspace_dir`、`agents_file` 及可选私聊/群聊模板、认证 socket、资源目录均使用绝对路径。配置中提供的模板须可读且非空；Nix 服务要求三份模板均有效。
- `queue_limit` 和 `task_timeout` 必须为正整数，默认分别为 8 条和 3600 秒。模型可由主配置指定，也可按会话选择。
- `group_prompt_allow_members` 必须为 bool，默认 false。群设定修改和群共同记忆清除默认限群主、管理员；true 时开放给群内所有可交互用户。角色查询失败时拒绝操作。
- 可选 `agent_admin_qq` 接受单个有效 QQ 号，缺省不提示管理员身份。它只影响新群会话中的身份说明，不增加入口、群管理或工具权限，不影响私聊。
- 配置缺失、格式错误或非法值阻止启动；配置变更重新加载后生效，管理员身份还需新建群会话。

## 安全边界

群回复与成员记录对群成员公开。同群共享上下文，各私聊及不同群独立；工作区用于组织会话，不提供授权用户之间的强多租户隔离。仅应授权信任的用户。

工具可操作专用工作区，但不得读取私有服务状态、NapCat token 或共享认证。工作区中的 AGENTS.md 和技能不能改变应用授权与宿主访问范围。群设定和记忆不能作为新的工具授权。

默认不开放额外宿主业务目录。登录凭据必须持久且受保护，不进入 Git、Nix store 或公开附件。具体挂载和共享认证实现见 [部署文档](../internal/deployment.md)。
