# 架构与 runtime

模块职责见 [仓库维护指南](../../AGENTS.md#项目结构)，行为契约见 [specs](../README.md#行为契约)。CLI 在 `qq_codex_agent.py`，业务组件位于 `qq_agent/`，显式组合并共用一个 SQLite 连接。调度状态由 `Agent` 管理，活动工具上下文由 `Turns` 管理。

## 状态与生命周期

群历史、候选时效和输入快照由 `groups` 管理，每群定时任务、合并窗口及忙碌调度由 `Agent` 管理，JSON 解析、纠错与 turn 生命周期由 `group_turns` 管理。批次状态为 pending/submitting/submitted/终态。消息发送回执和 bot 自身事件按群及消息 ID 去重。

`member_profiles` 按 `(group_id, user_id)` 保存固定成员字段，`group_profiles` 保存群共同记录，`group_prompts` 保存群设定、修改者及时间。查询其他成员身份后再次校验活动任务和删除状态，防止异步查询期间发生的 forget 被旧任务写回。

`group_handoffs` 保存每群摘要，与 `sessions.renew` 在同一事务提交。新 thread 创建或快照注入失败不会消费待交接状态。总结通过旧 thread 发起，使用当前模型和 medium，不注册 QQ 活动工具上下文；要求不调用工具，但不改变 runtime 工具权限。结构化摘要格式由应用校验。

每轮结束或中断后释放 thread 订阅，换 thread 和 `/new` 同样释放。无订阅且空闲约 30 分钟后 runtime 回收 thread 及 MCP 子进程，不保证立即退出。取消压缩时等待确定终态；无法确定开始或结束状态则退出，由服务重启保护。

上下文估算来自最近一次 token usage，按 thread 持久保存；恢复时可接收更新，缺失时最多等待两秒。自动压缩配置按完整有效上下文计算，不修改模型真实窗口。

## 提示词注入

群新 thread 的 `base_instructions` 写入 Responses 的 system `instructions`；bot QQ 号和昵称由代码独立注入，不依赖 `{nickname}` 占位符。恢复、总结和压缩不覆盖已保存的 system。

直接 @ 的回复规则放在 `GROUP_BASE_INSTRUCTIONS`。背景／新消息语义通过 developer 快照更新，回复义务仍由 `GroupTurn` 汇总、跟踪和校验；不再调用群人数接口或注入低参与度提示。

`group_snapshots` 按 thread 保存上次成功注入的完整 developer 快照。轮前比较内容，变化时通过 `thread/inject_items` 显式注入，成功后保存；压缩完成事件删除去重记录。新 thread 不把动态快照重复放入初始化指令。

不能借用 `collaborationMode.settings.developer_instructions`：模型目录的协作模板会优先覆盖该字段。不依赖 resume 刷新已加载 thread。群 thread 开启 `features.retain_client_developer_messages`，支持远程 v2 手动和自动压缩保留客户端 developer 消息。

0.159.2 runtime 的活动 turn 接受 `thread/inject_items` developer 纠错并继续处理；空闲时先注入，再以空输入启动 turn。developer 消息不能通过 user steering 冒充角色。

私聊沿用新建和恢复 thread 时传入 `developer_instructions` 的机制。更新时机及内容范围以 [提示词契约](../specs/memory-prompts.md#更新时机)为准。

## 图片与清理

生成图片从 Codex 结构化事件提取 base64，在应用内解码并原子写入工作区 `artifacts/`，图片索引及交付记录持久保存。提醒图片通过 `pics` 包资源加载。

清理按文件系统 `max(atime, mtime)` 判断，不额外追踪读取；清理文件时同步清除过期或已不存在文件的图片索引及发送记录。保留范围见 [图片与文件](../specs/images.md)。

## 群聊候选与节奏

`group_messages` 保存接收时间、准备状态、直接互动标记、片段及可恢复的原始 Message。准备后的可读候选与不可读占位分开；异步准备前先持久记录，重启后由外部消息唤醒补准备。bot 和命令只作历史。`consumed` 表示不再待处理，过期的独立原因保存在 `messages.status`，并不表示曾提交给模型。

`group_state` 保存最近接收、片段、普通提交时间、连续沉默次数及退避截止；原累计统计只留作历史。`group_batches` 保存优先级、片段及冻结输入快照。输入记录均占用 batch 归属，不另查历史窗口；快照中没有内部调度信息。旧版冻结快照首次读取时移除 background，并将引用该背景的简写引用恢复为原内容后保存；后续重试复用清理后的快照。已在 thread 中的旧输入不改写。

防抖使用事件循环单调时钟，接收墙钟用于持久时效与恢复；事件时间仅供聊天内容展示。每群最多一个定时任务，新的候选唤醒它；到期重新检查，忙碌则由任务结束或命令收尾重新调度。取消及关闭先取消、等待定时任务，任务清理仅移除自身注册项，避免旧任务删除新任务。

`GroupTurn` 与图片交付共用活动上下文中的成功聊天输出标记，整轮成功结束才反馈一次；提醒和故障发送不设置该标记。失败批次通过外部入站事件解除重试抑制；提交结果未知仍遵循原恢复规则。

参考 MaiBot 提交 [062d45c](https://github.com/Mai-with-u/MaiBot/tree/062d45cf8bd4272ac57d7ec03fcf405e5a458211/src/maisaka/turn_trigger) 的分层调度、沉默退避及短时合并思路；没有复制基于其运行数据拟合的概率权重，也没有新增独立 Planner。参数契约仅在 specs 维护。
