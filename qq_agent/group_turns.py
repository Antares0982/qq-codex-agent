import asyncio
import json
import time

from openai_codex import ApprovalMode
from openai_codex.errors import JsonRpcError
from openai_codex.generated.v2_all import ReasoningEffort, TurnStatus

from .logging import LOG, log_text
from .faces import parse_faces
from .messages import ImageTurn
from .stickers import unique_names


def parse_reply(text, stickers, required=False):
    value = json.loads(text, object_pairs_hook=unique_names)
    if (
        not isinstance(value, dict)
        or "response" not in value
        or value.keys() - {"response", "sticker", "quote"}
    ):
        raise ValueError("只允许 response、sticker 和 quote，response 必填")
    response = value["response"]
    if response is not None and not isinstance(response, str):
        raise ValueError("response 必须是字符串或 null")
    response = response.strip() if response else ""
    parts = parse_faces(response)
    if required and not any(kind == "text" and value.strip() for kind, value in parts):
        raise ValueError("当前必须回复非空文本，不能沉默或仅发表情")
    if response and "sticker" not in value:
        raise ValueError("有文本时 sticker 必填")
    name = value.get("sticker")
    if "sticker" in value and (not isinstance(name, str) or name not in stickers):
        raise ValueError("sticker 必须是配置中的名称字符串")
    sticker = stickers[name] if name is not None else None
    quote = value.get("quote")
    if quote is not None and (
        not isinstance(quote, str) or not 1 <= len(quote) <= 128 or not quote.strip()
    ):
        raise ValueError("quote 必须是消息 ID 字符串或 null")
    if quote is not None and not response and sticker is None:
        raise ValueError("沉默时不能指定 quote")
    return response, sticker, quote


class GroupTurn:
    def __init__(self, turns, message):
        self.owner = turns
        self.message = message
        self.groups = turns.groups
        self.runtime = turns.runtime
        self.replies = turns.replies
        self.context = None
        self.thread = None
        self.turn = None
        self.finished = asyncio.Event()
        self.lock = asyncio.Lock()
        self.required = False
        self.required_version = 0
        self.visible = False
        self.errors = 0
        self.repair = False
        self.tripped = False
        self.reminder = None
        self.steering = None
        self.submitted = []
        self.starting = False

    async def notice(self, start=False):
        if self.reminder:
            self.reminder.cancel()
            await asyncio.gather(self.reminder, return_exceptions=True)
            self.reminder = None
        if start and not self.finished.is_set():
            self.reminder = asyncio.create_task(
                self.replies.remind_group(
                    self.message, asyncio.get_running_loop().time()
                )
            )

    async def correct(self, reason):
        self.errors += 1
        self.repair = True
        LOG.warning(
            "Group reply invalid chat=%s count=%d reason=%s",
            self.message.key,
            self.errors,
            log_text(reason),
        )
        if self.errors >= 3:
            self.tripped = True
            self.repair = False
            await self.replies.safe_send(self.message, "agent 回复解析失败")
            return
        await self.runtime.inject(
            self.thread,
            "上一条回复解析失败："
            + str(reason)
            + "。请仅修正该回复，不重复已经成功发送的内容或工具操作。"
            + (
                "必须提供非空 response 文本。"
                if self.required
                else "可以按规则选择沉默。"
            ),
        )

    async def deliver(self, item, version=None):
        assert self.context is not None
        if item.type == "imageGeneration" and item.status == "completed":
            self.owner.images.save_generated(self.context.folder, item)
            return
        if item.type != "agentMessage":
            return
        async with self.lock:
            await self.notice(self.visible)
            LOG.info(
                "Assistant chat=%s phase=%s text=%s",
                self.message.key,
                item.phase,
                log_text(item.text),
            )
            try:
                text, sticker, quote = parse_reply(
                    item.text,
                    self.runtime.stickers,
                    self.required
                    and (version is None or version == self.required_version),
                )
                if quote is not None and not self.groups.can_quote(
                    self.message.key, quote
                ):
                    raise ValueError(
                        "quote 必须引用本群可见历史中的消息，不能引用系统消息"
                    )
            except (ValueError, TypeError, RecursionError) as error:
                await self.correct(error)
                return
            self.errors = 0
            self.repair = False
            if sticker is not None:
                await self.replies.bot.send(
                    self.message,
                    image=sticker,
                    sticker=True,
                    **({"quote": quote} if quote is not None and not text else {}),
                )
                self.context.chat_output = True
                if not self.visible:
                    self.visible = True
                    await self.notice(True)
            if text:
                await self.replies.bot.send(
                    self.message,
                    text=text,
                    **({"quote": quote} if quote is not None else {}),
                )
                if version is None or version == self.required_version:
                    self.required = False
                self.context.chat_output = True
                if not self.visible:
                    self.visible = True
                    await self.notice(True)

    async def steer(self, deadline):
        assert self.turn is not None
        key = self.message.key
        pending, wake = self.owner.pending[key], self.owner.wake[key]
        while not self.finished.is_set() and not self.tripped:
            if not pending:
                wake.clear()
                await wake.wait()
                continue
            message = pending[0]
            if message.batch is None or not message.direct:
                return
            async with self.lock:
                if self.finished.is_set() or self.tripped:
                    return
                required = self.required or message.mentioned
                pending.popleft()
                self.groups.status(message, "submitting")
                try:
                    await self.turn.steer(self.groups.input(message))
                except JsonRpcError as error:
                    if (
                        error.code == -32600
                        and "no active turn" in error.message.lower()
                    ):
                        self.groups.status(message, "pending")
                        pending.appendleft(message)
                        return
                    self.groups.status(message, "pending")
                    self.owner.mark(message, "failed")
                    raise
                except BaseException:
                    self.groups.status(message, "unknown")
                    self.owner.mark(message, "interrupted")
                    raise
                self.required = required
                if message.mentioned:
                    self.required_version += 1
                self.submitted.append(message)
                self.groups.status(message, "submitted")
                self.owner.mark(message, "running")
                deadline.reschedule(
                    asyncio.get_running_loop().time() + self.owner.settings.task_timeout
                )

    async def stream(self):
        assert self.turn is not None
        delivered, versions = set(), {}
        saw_reply = False
        async for event in self.turn.stream():
            if event.method == "error":
                LOG.error(
                    "Codex group error chat=%s error=%s",
                    self.message.key,
                    log_text(event.payload),
                )
            elif event.method == "item/started":
                item = event.payload.item.root
                if item.type == "agentMessage":
                    versions[item.id] = self.required_version
            elif event.method == "item/completed":
                item = event.payload.item.root
                if item.id in delivered:
                    continue
                delivered.add(item.id)
                if item.type == "agentMessage":
                    saw_reply = True
                await self.deliver(item, versions.get(item.id))
                if self.tripped:
                    return None, saw_reply
            elif event.method == "turn/completed":
                self.finished.set()
                return event.payload.turn.status, saw_reply
        return None, saw_reply

    async def run(self):
        message = self.message
        if not self.groups.eligible(message):
            self.owner.mark(message, "expired")
            return
        status = "failed"
        started = time.monotonic()
        deadline = asyncio.timeout(self.owner.settings.task_timeout)
        try:
            async with deadline:
                account = await self.owner.codex.account()
                if account.account is None:
                    if message.mentioned:
                        await self.replies.safe_send(
                            message, "尚未登录，请管理员完成设备码登录。"
                        )
                    return
                session = await self.owner.prepare_session(message, False)
                if session is None:
                    return
                thread_id, folder, inputs, _ = session
                self.context = ImageTurn(message, folder)
                self.owner.contexts[message.key] = self.context
                self.context.profiles = self.owner.profiles.recall_profiles(message)
                options, model = await self.runtime.thread_options(message, folder)
                self.thread = await self.runtime.prepare_thread(
                    message, thread_id, self.context, options
                )
                if not thread_id:
                    self.owner.save_session(message, self.thread, folder)
                await self.runtime.update_group(
                    self.thread, message, self.owner.profiles
                )
                self.required = message.mentioned
                first = True
                while True:
                    self.finished.clear()
                    correcting = self.repair
                    self.repair = False
                    saw_reply = False
                    if first:
                        if not self.groups.eligible(message):
                            status = "expired"
                            return
                        self.groups.status(message, "submitting")
                    usage = self.owner.db.execute(
                        "SELECT tokens FROM context_usage WHERE thread=?",
                        (self.thread.id,),
                    ).fetchone()
                    self.starting = True
                    try:
                        self.turn = await self.thread.turn(
                            inputs if first else [],
                            approval_mode=ApprovalMode.auto_review,
                            model=model,
                            effort=ReasoningEffort.medium,
                        )
                    except JsonRpcError:
                        self.starting = False
                        if first:
                            self.groups.status(message, "pending")
                        raise
                    self.starting = False
                    if first:
                        self.groups.status(message, "submitted")
                        self.submitted.append(message)
                    first = False
                    LOG.info(
                        "Group turn started chat=%s thread=%s turn=%s model=%s effort=medium context_tokens=%s",
                        message.key,
                        self.thread.id,
                        getattr(self.turn, "id", "unknown"),
                        model or "default",
                        usage[0] if usage else "unknown",
                    )
                    self.visible = False
                    async with asyncio.TaskGroup() as tasks:
                        if message.key in self.owner.pending:
                            self.steering = tasks.create_task(self.steer(deadline))
                        streaming = tasks.create_task(self.stream())
                        turn_status, saw_reply = await streaming
                        if self.steering:
                            self.steering.cancel()
                    self.steering = None
                    await self.notice()
                    if self.tripped:
                        break
                    if turn_status != TurnStatus.completed:
                        raise RuntimeError(f"Group turn ended: {turn_status}")
                    if (
                        self.required or (correcting and not saw_reply)
                    ) and not self.repair:
                        await self.correct("turn 结束但仍未提供有效回复")
                    if self.tripped:
                        break
                    if not self.repair:
                        status = "completed"
                        break
        except (asyncio.CancelledError, TimeoutError) as error:
            status = "interrupted"
            if (
                self.required or self.visible
            ) and message.key not in self.owner.resetting:
                await self.replies.safe_send(
                    message, "任务已中断或超时，不会自动重跑。"
                )
            if isinstance(error, asyncio.CancelledError) or deadline.expired():
                raise
        except Exception as error:
            LOG.error(
                "Group task failed chat=%s error=%s", message.key, log_text(error)
            )
            if self.required or self.visible:
                await self.replies.safe_send(message, "哎呀！宕机了……")
        finally:
            await self.notice()
            if self.steering:
                self.steering.cancel()
                await asyncio.gather(self.steering, return_exceptions=True)
            if self.starting:
                self.groups.status(message, "unknown")
            for submitted in self.submitted:
                self.groups.status(submitted, status)
                self.owner.mark(submitted, status)
            self.owner.mark(message, status)
            self.groups.result(
                message, status, bool(self.context and self.context.chat_output)
            )
            await self.owner.cleanup_context(message, self.context, [], [])
            if self.turn is not None and not self.finished.is_set():
                try:
                    await asyncio.wait_for(self.turn.interrupt(), 10)
                except Exception:
                    raise SystemExit("Interrupt failed; runtime must restart") from None
            if self.thread is not None:
                await self.runtime.release_thread(self.thread.id)
            LOG.info(
                "Group task ended chat=%s status=%s seconds=%.1f",
                message.key,
                status,
                time.monotonic() - started,
            )
            if self.starting:
                raise SystemExit("Turn start outcome unknown; runtime must restart")
