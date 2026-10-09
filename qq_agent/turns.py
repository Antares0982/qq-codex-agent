import asyncio
import time
import uuid

from openai_codex import ApprovalMode
from openai_codex.errors import JsonRpcError
from openai_codex.generated.v2_all import ReasoningEffort, TurnStatus

from .images import Images
from .logging import LOG, log_text
from .messages import ImageTurn
from .profiles import Profiles

IDLE_COMPACT_LIMIT = 100000


class Turns:
    def __init__(
        self,
        settings,
        db,
        codex,
        runtime,
        inputs,
        replies,
        pending,
        wake,
        resetting,
        mark,
    ):
        self.settings = settings
        self.db = db
        self.codex = codex
        self.runtime = runtime
        self.inputs = inputs
        self.replies = replies
        self.pending = pending
        self.wake = wake
        self.resetting = resetting
        self.mark = mark
        self.contexts = {}
        self.profiles = Profiles(db, self.contexts, replies.safe_send, replies.bot)
        self.images = Images(db, replies.bot, self.contexts)

    async def execute(self, message):
        turn = context = steering = reminder = reaction = None
        thread = None
        started = time.monotonic()
        finished = asyncio.Event()
        submitted = []
        notices = []
        starting_turn = False
        manual = (
            message.text == "/compact"
            and not message.images
            and not message.unsupported
        )
        try:
            async with asyncio.timeout(self.settings.task_timeout) as deadline:
                account = await self.codex.account()
                if account.account is None:
                    await self.replies.safe_send(
                        message, "尚未登录，请管理员完成设备码登录。"
                    )
                    self.mark(message, "failed")
                    return
                if "group_id" in message.target and not manual:
                    reaction = asyncio.create_task(self.replies.react_message(message))
                    await asyncio.sleep(0)
                session = await self.prepare_session(message, manual)
                if session is None:
                    return
                thread_id, folder, inputs, manual, renewing = session
                options, model = await self.runtime.thread_options(message, folder)
                context = ImageTurn(message, folder)
                self.contexts[message.key] = context
                if "group_id" in message.target:
                    context.profiles = self.profiles.recall_profiles(message)
                thread = await self.runtime.prepare_thread(
                    message, thread_id, context, options
                )
                if not thread_id:
                    self.save_session(message, thread, folder)
                if manual:
                    await self.compact_manual(message, thread)
                    return
                if renewing:
                    await self.compact_idle(message, thread)
                if "group_id" in message.target:
                    await self.runtime.update_group(thread, message, self.profiles)
                with self.db:
                    self.db.execute(
                        "UPDATE activity SET thread_gen=? WHERE key=?",
                        (message.generation, message.key),
                    )
                reminder = await self.start_notices(context)
                starting_turn = True
                turn = await thread.turn(
                    inputs,
                    approval_mode=ApprovalMode.auto_review,
                    model=model,
                    effort=ReasoningEffort.medium,
                )
                starting_turn = False
                LOG.info(
                    "Turn started chat=%s message=%s thread=%s turn=%s",
                    message.key,
                    message.identifier,
                    thread.id,
                    getattr(turn, "id", "unknown"),
                )
                if message.key in self.pending:
                    steering = asyncio.create_task(
                        self.steer_messages(
                            turn, context, finished, submitted, deadline
                        )
                    )
                status, has_text = await self.stream_turn(
                    turn,
                    context,
                    thread,
                    finished,
                    steering,
                    reminder,
                    notices,
                    started,
                )
                if reminder:
                    reminder.cancel()
                    await asyncio.gather(reminder, return_exceptions=True)
                await self.finish_message(message, status, has_text, notices)
        except (asyncio.CancelledError, TimeoutError) as error:
            if reminder:
                reminder.cancel()
            await self.report_interruption(message, context, error, started)
            raise
        except Exception as error:
            if reminder:
                reminder.cancel()
            LOG.error(
                "Task failed chat=%s message=%s type=%s error=%s",
                message.key,
                message.identifier,
                type(error).__name__,
                log_text(error),
            )
            self.mark(message, "failed")
            await self.replies.safe_send(
                message, "任务失败，未自动重试。请检查登录状态、图片格式或服务日志。"
            )
        finally:
            if reaction:
                reaction.cancel()
                await asyncio.gather(reaction, return_exceptions=True)
            if reminder:
                reminder.cancel()
                await asyncio.gather(reminder, return_exceptions=True)
            if steering:
                steering.cancel()
                await asyncio.gather(steering, return_exceptions=True)
            await self.cleanup_context(message, context, submitted, notices)
            if starting_turn:
                LOG.error("Turn start outcome unknown; runtime must restart")
                raise SystemExit(1)
            if turn is not None and not finished.is_set():
                try:
                    await asyncio.wait_for(turn.interrupt(), 10)
                except Exception:
                    LOG.error("Interrupt failed; runtime must restart")
                    raise SystemExit(1)
            if thread is not None:
                await self.runtime.release_thread(thread.id)

    async def prepare_session(self, message, manual):
        row = self.db.execute(
            "SELECT thread, folder, renew FROM sessions WHERE key=?",
            (message.key,),
        ).fetchone()
        thread_id, folder_name, requested = row if row else (None, uuid.uuid4().hex, 0)
        if manual and (not thread_id or requested):
            await self.replies.safe_send(message, "暂无可压缩的会话。")
            self.mark(message, "completed")
            return
        if row is None:
            with self.db:
                self.db.execute(
                    "INSERT INTO sessions (key, thread, folder) VALUES (?, ?, ?)",
                    (message.key, None, folder_name),
                )
        row = self.db.execute(
            "SELECT thread_gen FROM activity WHERE key=?",
            (message.key,),
        ).fetchone()
        renewing = thread_id is not None and message.generation > (row[0] if row else 0)
        folder = self.settings.workspace_dir / folder_name
        folder.mkdir(mode=0o700, exist_ok=True)
        if requested and thread_id:
            await self.runtime.release_thread(thread_id)
            thread_id = None
        inputs = [] if manual else await self.inputs.prepare_input(message, folder)
        if inputs is None:
            self.mark(message, "failed")
            return
        return thread_id, folder, inputs, manual, renewing and not requested

    def save_session(self, message, thread, folder):
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO sessions (key, thread, folder) VALUES (?, ?, ?)",
                (message.key, thread.id, folder.name),
            )
            self.db.execute(
                "UPDATE activity SET thread_gen=? WHERE key=?",
                (message.generation, message.key),
            )

    async def compact_manual(self, message, thread):
        await self.replies.safe_send(message, "正在压缩上下文……")
        success = await self.runtime.compact(thread)
        await self.replies.safe_send(
            message,
            "上下文压缩完成。" if success else "上下文压缩失败，原会话保留。",
        )
        self.mark(message, "completed" if success else "failed")

    async def compact_idle(self, message, thread):
        tokens = await self.runtime.context_tokens(thread.id)
        if tokens is not None and tokens > IDLE_COMPACT_LIMIT:
            await self.replies.safe_send(message, "正在压缩上下文……")
            await self.runtime.compact(thread)

    async def start_notices(self, context):
        message = context.message
        await self.replies.safe_send(message, "开始处理……", intermediate=True)
        if "group_id" in message.target:
            return asyncio.create_task(
                self.replies.remind_group(message, asyncio.get_running_loop().time())
            )

    async def stream_turn(
        self, turn, context, thread, finished, steering, reminder, notices, started
    ):
        message = context.message
        delivered, has_text, status = set(), False, None
        shown_progress = set()
        async for event in turn.stream():
            if event.method == "error":
                LOG.error(
                    "Codex error chat=%s thread=%s turn=%s error=%s",
                    message.key,
                    thread.id,
                    getattr(turn, "id", "unknown"),
                    log_text(event.payload),
                )
            if event.method == "item/started":
                await self.show_progress(
                    message, event.payload.item.root, shown_progress, notices
                )
            elif event.method == "item/completed":
                item = event.payload.item.root
                if item.id in delivered:
                    continue
                delivered.add(item.id)
                if await self.deliver_item(context, item):
                    has_text = True
            elif event.method == "turn/completed":
                status = event.payload.turn.status
                LOG.info(
                    "Turn completed chat=%s thread=%s status=%s seconds=%.1f error=%s",
                    message.key,
                    thread.id,
                    status,
                    time.monotonic() - started,
                    log_text(getattr(event.payload.turn, "error", None)),
                )
                if reminder:
                    reminder.cancel()
                finished.set()
                if steering:
                    self.wake[message.key].set()
                    await steering
        return status, has_text

    async def show_progress(self, message, item, shown_progress, notices):
        LOG.info(
            "Item started chat=%s type=%s item=%s",
            message.key,
            item.type,
            item.id,
        )
        if item.type == "contextCompaction":
            key = (item.type, item.id)
            if key not in shown_progress:
                shown_progress.add(key)
                await self.replies.safe_send(message, "正在压缩上下文……")
            return
        progress = {
            "commandExecution": "正在执行代码……",
            "imageGeneration": "正在生成图片……",
            "webSearch": "正在检索资料……",
        }.get(item.type)
        if progress and progress not in shown_progress:
            shown_progress.add(progress)
            notices.append(
                asyncio.create_task(
                    self.replies.safe_send(message, progress, intermediate=True)
                )
            )

    async def deliver_item(self, context, item):
        message = context.message
        LOG.info(
            "Item completed chat=%s type=%s item=%s status=%s",
            message.key,
            item.type,
            item.id,
            getattr(item, "status", ""),
        )
        detail = getattr(item, "error", None) or getattr(item, "failure", None)
        if detail:
            LOG.error(
                "Tool error chat=%s item=%s error=%s",
                message.key,
                item.id,
                log_text(detail),
            )
        if item.type == "agentMessage":
            LOG.info(
                "Assistant chat=%s phase=%s text=%s",
                message.key,
                item.phase,
                log_text(item.text),
            )
            if item.text:
                await self.replies.safe_send(message, item.text)
                return True
        elif item.type == "imageGeneration" and item.status == "completed":
            self.images.save_generated(context.folder, item)
        return False

    async def finish_message(self, message, status, has_text, notices):
        if status == TurnStatus.completed:
            await asyncio.gather(*notices)
            if not has_text:
                await self.replies.safe_send(message, "任务完成。")
            self.mark(message, "completed")
        else:
            await self.replies.safe_send(
                message,
                "哎呀！宕机了……"
                if "group_id" in message.target
                else "任务未完成，详细错误已记录到服务日志。",
            )
            self.mark(message, "failed")

    async def report_interruption(self, message, context, error, started):
        LOG.warning(
            "Task interrupted chat=%s message=%s reason=%s seconds=%.1f",
            message.key,
            message.identifier,
            type(error).__name__,
            time.monotonic() - started,
        )
        self.mark(message, "interrupted")
        await self.replies.safe_send(
            message,
            (
                "图片已发送，但后续处理已中断或超时，不会自动重跑。"
                if context
                and self.db.execute(
                    "SELECT 1 FROM image_deliveries WHERE turn=? AND status='sent'",
                    (context.token,),
                ).fetchone()
                else "任务已中断或超时，不会自动重跑。"
            ),
            intermediate=message.key in self.resetting,
        )

    async def cleanup_context(self, message, context, submitted, notices):
        row = self.db.execute(
            "SELECT status FROM messages WHERE id=?", (message.identifier,)
        ).fetchone()
        for extra in submitted:
            self.mark(extra, row[0] if row else "interrupted")
        if context:
            self.contexts.pop(message.key, None)
        for notice in notices:
            notice.cancel()
        await asyncio.gather(*notices, return_exceptions=True)
        if context:
            tasks = list(context.tasks)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def steer_messages(self, turn, context, finished, submitted, deadline):
        key = context.message.key
        pending, wake = self.pending[key], self.wake[key]
        while not finished.is_set():
            if not pending:
                wake.clear()
                await wake.wait()
                continue
            message = pending[0]
            if message.generation != context.message.generation:
                return
            try:
                inputs = await self.inputs.prepare_input(message, context.folder)
            except Exception as error:
                LOG.warning(
                    "Steering input failed chat=%s message=%s error=%s",
                    key,
                    message.identifier,
                    log_text(error),
                )
                inputs = None
                await self.replies.safe_send(
                    message, "追加消息准备失败，请检查图片或引用消息。"
                )
            if inputs is None:
                pending.popleft()
                self.mark(message, "failed")
                continue
            if finished.is_set():
                return
            pending.popleft()
            submitted.append(message)
            self.mark(message, "running")
            try:
                await turn.steer(inputs)
                deadline.reschedule(
                    asyncio.get_running_loop().time() + self.settings.task_timeout
                )
                LOG.info(
                    "Message steered chat=%s message=%s timeout_reset=%s",
                    key,
                    message.identifier,
                    self.settings.task_timeout,
                )
            except JsonRpcError as error:
                submitted.remove(message)
                if error.code == -32600 and "no active turn" in error.message.lower():
                    pending.appendleft(message)
                    self.mark(message, "queued")
                    return
                self.mark(message, "failed")
                await self.replies.safe_send(message, "追加消息被拒绝，未自动重试。")
            except Exception as error:
                submitted.remove(message)
                self.mark(message, "failed")
                LOG.warning(
                    "Steering failed chat=%s message=%s error=%s",
                    key,
                    message.identifier,
                    log_text(error),
                )
                await self.replies.safe_send(
                    message, "追加消息送达结果未知，未自动重试。"
                )
