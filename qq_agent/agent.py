import asyncio
import contextlib
import time
from collections import deque

from .cleanup import Cleanup
from .commands import Commands
from .inputs import Inputs
from .groups import Groups
from .logging import LOG, log_text
from .messages import parse_message
from .replies import Replies
from .runtime import Runtime
from .storage import open_database
from .tool_server import ToolServer
from .turns import Turns

THREAD_IDLE = 2 * 60 * 60


class Agent:
    def __init__(self, settings, bot, codex):
        self.settings = settings
        self.db = open_database(settings)
        self.queue = asyncio.Queue()
        self.jobs = {}
        self.pending = {}
        self.wake = {}
        self.controls = set()
        self.receivers = {}
        self.incoming = {}
        self.scheduled = set()
        self.resetting = set()
        self.replies = Replies(bot)
        self.inputs = Inputs(bot, self.replies.safe_send)
        self.groups = Groups(settings, self.db, bot, self.inputs)
        bot.on_sent = self.groups.sent
        self.runtime = Runtime(settings, self.db, codex, self.inputs.group_name)
        self.turns = Turns(
            settings,
            self.db,
            codex,
            self.runtime,
            self.inputs,
            self.replies,
            self.pending,
            self.wake,
            self.resetting,
            self.mark,
            self.groups,
        )
        self.commands = Commands(
            self.db,
            codex,
            self.runtime,
            self.turns.profiles,
            self.replies.safe_send,
            self.queue,
            self.jobs,
            self.resetting,
            self.mark,
            self.cancel_chat,
            self.flush_group,
        )
        self.tools = ToolServer(
            self.turns.contexts,
            self.turns.images,
            self.turns.profiles,
        )
        self.cleanup = Cleanup(
            settings, self.db, self.jobs, self.resetting, self.turns.contexts
        )

    def receive(self, event):
        message = parse_message(event, self.settings)
        if message is None:
            return
        if "group_id" in message.target:
            if not self.groups.record(message, self.is_command(message)):
                return
            if message.sender_id == message.bot_id:
                return
            LOG.info(
                "Received chat=%s message=%s sender=%s text=%s images=%d reply=%s",
                message.key,
                message.identifier,
                message.sender_id,
                log_text(message.text),
                len(message.images),
                message.reply,
            )
            self.incoming.setdefault(message.key, deque()).append(message)
            if message.key not in self.receivers:
                self.receivers[message.key] = asyncio.create_task(
                    self.receive_group(message.key)
                )
            return
        with self.db:
            inserted = self.db.execute(
                "INSERT OR IGNORE INTO messages VALUES (?, 'received')",
                (message.identifier,),
            ).rowcount
        if not inserted:
            return
        LOG.info(
            "Received chat=%s message=%s sender=%s text=%s images=%d reply=%s",
            message.key,
            message.identifier,
            message.sender_id,
            log_text(message.text),
            len(message.images),
            message.reply,
        )
        if self.is_command(message):
            self.start_control(message)
            return
        reason = None
        if message.key in self.resetting:
            reason = "会话正在重置，请稍后重试。"
        elif message.unsupported:
            reason = "目前仅支持文本和图片。"
        elif len(message.images) > 5:
            reason = "每条消息最多 5 张图片。"
        elif not message.text and not message.images and message.reply is None:
            return
        elif (
            self.db.execute(
                "SELECT count(*) FROM messages WHERE status='queued' AND id LIKE ?",
                (f"{message.bot_id}:{message.key}:%",),
            ).fetchone()[0]
            >= self.settings.queue_limit
        ):
            reason = "任务队列已满，请稍后重试。"
        if reason:
            if len(self.controls) < 8:
                task = asyncio.create_task(self.replies.safe_send(message, reason))
                self.controls.add(task)
                task.add_done_callback(self.controls.discard)
            return
        now = time.time()
        row = self.db.execute(
            "SELECT received, message_gen, thread_gen FROM activity WHERE key=?",
            (message.key,),
        ).fetchone()
        message.generation = row[1] if row else 0
        if row and now - row[0] > THREAD_IDLE:
            message.generation += 1
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO activity VALUES (?, ?, ?, ?)",
                (message.key, now, message.generation, row[2] if row else 0),
            )
        self.queue.put_nowait(message)
        self.mark(message, "queued")

    def is_command(self, message):
        if message.images or message.unsupported:
            return False
        if "group_id" in message.target and not message.mentioned:
            return False
        command = message.text
        return (
            command in {"/new", "/stop", "/status", "/help", "/compact"}
            or ("group_id" in message.target and command == "/new blank")
            or command.split(maxsplit=1)[:1]
            in (["/model"], ["/profile"], ["/prompt"], ["/group-profile"])
        )

    def start_control(self, message):
        if len(self.controls) < 8:
            task = asyncio.create_task(self.commands.control(message))
            self.controls.add(task)
            task.add_done_callback(self.controls.discard)

    async def receive_group(self, key):
        try:
            while self.incoming[key]:
                message = self.incoming[key].popleft()
                command = self.is_command(message)
                if command:
                    self.start_control(message)
                    # Controls establish cancellation before later arrivals.
                    await asyncio.sleep(0)
                    continue
                try:
                    quoted_bot = await self.groups.prepare(message)
                    if self.groups.trigger(message, quoted_bot):
                        self.groups.freeze(message)
                        self.flush_group(key)
                except Exception as error:
                    LOG.error(
                        "Group receive failed chat=%s error=%s", key, log_text(error)
                    )
        finally:
            self.receivers.pop(key, None)
            self.incoming.pop(key, None)

    def flush_group(self, key):
        if key in self.resetting:
            return
        for message in self.groups.pending(key):
            if message.batch in self.scheduled:
                continue
            self.scheduled.add(message.batch)
            self.queue.put_nowait(message)
            self.mark(message, "queued")

    def mark(self, message, status):
        LOG.info(
            "Message status chat=%s message=%s status=%s",
            message.key,
            message.identifier,
            status,
        )
        with self.db:
            self.db.execute(
                "UPDATE messages SET status=? WHERE id=?", (status, message.identifier)
            )
        if message.batch and status not in {"queued", "running"}:
            self.scheduled.discard(message.batch)

    async def chat_work(self, key):
        try:
            while self.pending[key]:
                message = self.pending[key].popleft()
                self.mark(message, "running")
                try:
                    await self.turns.execute(message)
                except TimeoutError:
                    pass
        finally:
            self.clear_chat(key)

    def clear_chat(self, key):
        for message in self.pending.pop(key):
            self.mark(message, "canceled")
        self.wake.pop(key)
        self.jobs.pop(key)

    async def work(self):
        try:
            while True:
                message = await self.queue.get()
                if message.key not in self.jobs:
                    self.pending[message.key] = deque()
                    self.wake[message.key] = asyncio.Event()
                    self.jobs[message.key] = asyncio.create_task(
                        self.chat_work(message.key)
                    )
                self.pending[message.key].append(message)
                self.wake[message.key].set()
                self.queue.task_done()
        finally:
            jobs = list(self.jobs.values())
            for job in jobs:
                job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
            for key in list(self.jobs):
                self.clear_chat(key)

    async def cancel_chat(self, key, identifier=None):
        if key.startswith("group-"):
            self.groups.cancel(key, identifier)
        retained = []
        while not self.queue.empty():
            queued = self.queue.get_nowait()
            self.queue.task_done()
            if queued.key != key:
                retained.append(queued)
            else:
                self.mark(queued, "canceled")
        for queued in retained:
            self.queue.put_nowait(queued)
        job = self.jobs.get(key)
        if job:
            job.cancel()
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await job
            if self.jobs.get(key) is job:
                self.clear_chat(key)
