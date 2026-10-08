import asyncio
import time
import unicodedata

from openai_codex import AsyncThread
from openai_codex.generated.v2_all import ReasoningEffort

from .logging import LOG, log_text

HELP = """直接发送文字或图片，可提问、执行代码或请求生成图片。
群聊可先发送图片，再回复该图片并 @ bot 提问。
/help 查看用法
/model 列出可选模型及本会话设置
/model <模型ID> 切换本会话模型，下一轮请求生效
/status 查看登录、任务状态、上下文长度及用户消息数
/stop 停止本会话任务并清空队列
/new 开启空白会话，保留工作文件、图片索引、模型选择及聊天偏好
/compact 压缩当前会话上下文，执行任务时请稍后重试
/profile 在当前群公开查看本人聊天偏好
/profile forget 删除本人当前群偏好，后续仍可自动学习
/prompt 查看当前群设定
/prompt set <内容> 替换群设定，最多 4000 字符，下个独立任务生效
/prompt clear 恢复默认群设定
/group-profile 查看本群共同偏好
/group-profile forget 清除本群共同偏好，后续仍可自动学习
群设定修改和群记录清除默认仅授权群主、管理员可用，管理员可配置开放给全部授权成员。
删除记录不清除旧聊天历史及 journal；/new 保留群设定和记录。
推理强度固定为 medium。模型选择在重启后保留。
群聊发送图片、全部模型文字回复、压缩提示及记录聊天偏好时的小本本提示，不发送其他工具进度通知。
私聊与各群权限独立；群聊须获该群授权并 @ bot。同群共享会话和模型设置。
不同聊天独立执行；处理中继续发送消息会追加到当前任务。"""


class Commands:
    def __init__(
        self,
        db,
        codex,
        runtime,
        profiles,
        send,
        queue,
        jobs,
        resetting,
        mark,
        cancel_chat,
    ):
        self.db = db
        self.codex = codex
        self.runtime = runtime
        self.profiles = profiles
        self.safe_send = send
        self.queue = queue
        self.jobs = jobs
        self.resetting = resetting
        self.mark = mark
        self.cancel_chat = cancel_chat

    async def group_control(self, message):
        if "group_id" not in message.target:
            await self.safe_send(message, "此指令仅群聊可用，请在群内 @ bot 使用。")
            return
        group = str(message.target["group_id"])
        parts = message.text.split(maxsplit=2)
        prompt = parts[0] == "/prompt"
        if len(parts) == 1:
            if prompt:
                row = self.db.execute(
                    "SELECT prompt FROM group_prompts WHERE group_id=?", (group,)
                ).fetchone()
                text = row[0] if row else "暂无自定义群设定。"
            else:
                text = self.profiles.group_profile(group) or "暂无群共同偏好。"
            await self.safe_send(message, text)
            return
        if prompt and len(parts) == 3 and parts[1] == "set":
            text = parts[2].strip()
            if (
                not text
                or len(text) > 4000
                or any(
                    unicodedata.category(char).startswith("C") and char not in "\n\r\t"
                    for char in text
                )
            ):
                await self.safe_send(
                    message, "群设定须为 1 至 4000 字符的文本，可换行。"
                )
                return
        elif parts == ["/prompt", "clear"] or parts == ["/group-profile", "forget"]:
            text = ""
        else:
            await self.safe_send(
                message,
                "用法：/prompt、/prompt set <内容>、/prompt clear。"
                if prompt
                else "用法：/group-profile 或 /group-profile forget。",
            )
            return
        if not self.runtime.settings.group_prompt_allow_members:
            try:
                info = await self.profiles.member_info(group, message.sender_id)
            except Exception as error:
                LOG.warning(
                    "Group role failed chat=%s error=%s", message.key, log_text(error)
                )
                await self.safe_send(message, "无法确认群管理权限，请稍后重试。")
                return
            if info.get("role") not in {"owner", "admin"}:
                await self.safe_send(
                    message, "仅可交互用户中的群主、管理员可执行此操作。"
                )
                return
        with self.db:
            if prompt:
                if text:
                    self.db.execute(
                        "INSERT OR REPLACE INTO group_prompts VALUES (?, ?, ?, ?)",
                        (group, text, message.sender_id, time.time()),
                    )
                else:
                    self.db.execute(
                        "DELETE FROM group_prompts WHERE group_id=?", (group,)
                    )
            else:
                self.db.execute("DELETE FROM group_profiles WHERE group_id=?", (group,))
                context = self.profiles.contexts.get(message.key)
                if context:
                    context.group_writable = False
        await self.safe_send(
            message,
            "保存成功"
            if prompt
            else "已删除本群共同偏好。旧聊天历史和 journal 仍保留，后续独立轮次可能重新形成偏好。",
        )

    async def select_model(self, message):
        async with asyncio.timeout(20):
            response = await self.codex.models()
        models = {
            model.model: model
            for model in response.data
            if not model.hidden
            and any(
                option.reasoning_effort == ReasoningEffort.medium
                for option in model.supported_reasoning_efforts
            )
        }
        parts = message.text.split()
        if len(parts) == 1:
            selected = (
                self.runtime.selected_model(message.key) or "Codex 默认（未固定）"
            )
            listing = "\n".join(models) or "暂无支持 medium 的可选模型"
            await self.safe_send(
                message,
                f"本会话模型设置：{selected}\n推理强度：medium\n{listing}\n"
                "切换：/model <模型ID>；群内设置共享。",
            )
        elif len(parts) == 2 and parts[1] in models:
            with self.db:
                self.db.execute(
                    "INSERT OR REPLACE INTO models VALUES (?, ?)",
                    (message.key, parts[1]),
                )
            await self.safe_send(
                message, f"已选择 {parts[1]}，medium；下一轮请求生效，保留会话。"
            )
        else:
            await self.safe_send(
                message, "用法：/model <模型ID>。请用 /model 查看可选模型。"
            )

    async def control(self, message):
        try:
            if message.text.split(maxsplit=1)[:1] in (["/prompt"], ["/group-profile"]):
                await self.group_control(message)
                return
            if message.text == "/compact":
                busy = (
                    message.key in self.jobs
                    or message.key in self.resetting
                    or self.db.execute(
                        "SELECT 1 FROM messages WHERE status='queued' AND id LIKE ?",
                        (f"{message.bot_id}:{message.key}:%",),
                    ).fetchone()
                )
                if busy:
                    await self.safe_send(
                        message, "会话正在处理任务，请结束后再使用 /compact。"
                    )
                    return
                self.mark(message, "queued")
                self.queue.put_nowait(message)
                return
            if message.text.split(maxsplit=1)[:1] == ["/profile"]:
                await self.profiles.profile_control(message)
                return
            if message.text == "/help":
                await self.safe_send(message, HELP)
                return
            if message.text.split(maxsplit=1)[:1] == ["/model"]:
                await self.select_model(message)
                return
            if message.text == "/status":
                account = await self.codex.account()
                text = (
                    "已登录"
                    if account.account is not None
                    else "未登录，请管理员完成设备码登录"
                )
                busy = message.key in self.jobs
                row = self.db.execute(
                    "SELECT thread FROM sessions WHERE key=? AND renew=0",
                    (message.key,),
                ).fetchone()
                context = "0 tokens"
                messages = "0 条"
                if row and row[0]:
                    usage = self.db.execute(
                        "SELECT tokens FROM context_usage WHERE thread=?", (row[0],)
                    ).fetchone()
                    context = f"约 {usage[0]:,} tokens" if usage else "暂未知"
                    try:
                        async with asyncio.timeout(20):
                            result = await AsyncThread(self.codex, row[0]).read(
                                include_turns=True
                            )
                        count = sum(
                            item.root.type == "userMessage"
                            for turn in result.thread.turns
                            for item in turn.items
                        )
                        messages = f"{count} 条"
                    except Exception as error:
                        LOG.warning(
                            "Thread status failed chat=%s error=%s",
                            message.key,
                            log_text(error),
                        )
                        messages = "暂时无法读取"
                await self.safe_send(
                    message,
                    f"{text}；本会话{'执行中' if busy else '空闲'}。\n"
                    f"上下文：{context}\n用户消息：{messages}",
                )
                return
            if message.key in self.resetting:
                return
            self.resetting.add(message.key)
            try:
                await self.cancel_chat(message.key)
                if message.text == "/new":
                    row = self.db.execute(
                        "SELECT folder, thread FROM sessions WHERE key=?",
                        (message.key,),
                    ).fetchone()
                    if row and row[1]:
                        await self.runtime.release_thread(row[1])
                    with self.db:
                        self.db.execute(
                            "UPDATE sessions SET renew=1 WHERE key=?", (message.key,)
                        )
                await self.safe_send(
                    message,
                    "已停止当前任务，下条消息将开启空白会话，工作文件和图片索引保留。"
                    if message.text == "/new"
                    else "已停止本会话任务并清空队列。",
                )
            finally:
                self.resetting.discard(message.key)
        except Exception as error:
            LOG.warning("Control failed chat=%s error=%s", message.key, log_text(error))
            await self.safe_send(message, "操作失败，请查看服务日志中的错误类型。")
