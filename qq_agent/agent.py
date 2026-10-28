import asyncio
import contextlib
import time
from collections import deque
from dataclasses import dataclass, field

from .cleanup import Cleanup
from .commands import Commands
from .inputs import Inputs
from .groups import Groups
from .logging import LOG, log_text
from .messages import is_command, parse_message
from .replies import Replies
from .runtime import Runtime
from .storage import open_database
from .tool_server import ToolServer
from .turns import Turns

THREAD_IDLE = 2 * 60 * 60


@dataclass
class GroupWindow:
    first: float
    deadline: float
    urgent: float | None = None
    changed: asyncio.Event = field(default_factory=asyncio.Event)

    def observe(self, message, arrival, previous, now):
        if message.direct:
            self.urgent = min(self.urgent or arrival + 1, arrival + 1)
        if self.deadline > now:
            if previous:
                wait = 2 if previous[1] == message.sender_id else 0.5
            else:
                wait = 2
            self.deadline = min(arrival + wait, self.first + 8)
        if self.urgent is not None:
            self.deadline = min(self.deadline, self.urgent)
        self.changed.set()


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
        self.group_windows = {}
        self.group_timers = {}
        self.closing = False
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
        return is_command(message)

    def start_control(self, message):
        if len(self.controls) < 8:
            task = asyncio.create_task(self.commands.control(message))
            self.controls.add(task)
            task.add_done_callback(self.controls.discard)

    async def receive_group(self, key):
        queued = {message.identifier for message in self.incoming[key]}
        recovered = set()
        for message in reversed(self.groups.unprepared(key)):
            if message.identifier not in queued:
                self.incoming[key].appendleft(message)
                recovered.add(message.identifier)
        observed = False
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
                    if (
                        self.groups.observe(message, quoted_bot)
                        and message.identifier not in recovered
                    ):
                        observed = True
                        self.groups.retry(key)
                        self.observe_group(message)
                except Exception as error:
                    LOG.error(
                        "Group receive failed chat=%s error=%s", key, log_text(error)
                    )
        finally:
            self.receivers.pop(key, None)
            self.incoming.pop(key, None)
            if observed or key in self.group_windows:
                self.flush_group(key)

    def observe_group(self, message):
        row, previous = self.groups.timing(message)
        if not row or row[1]:
            return
        now = asyncio.get_running_loop().time()
        arrival = now - max(0, time.time() - row[0])
        window = self.group_windows.get(message.key)
        if window is None:
            wait = 5 if previous and row[0] - previous[0] <= 30 else 2
            if message.images and not message.text:
                wait = max(wait, 4)
            deadline = arrival + wait
            if message.direct:
                deadline = min(deadline, arrival + 1)
            window = GroupWindow(
                arrival, deadline, arrival + 1 if message.direct else None
            )
            self.group_windows[message.key] = window
        else:
            window.observe(message, arrival, previous, now)

    async def schedule_group(self, key):
        try:
            while not self.closing:
                window = self.group_windows.get(key)
                if window:
                    window.changed.clear()
                candidates = self.groups.candidates(key)
                batches = self.groups.pending(key, retry=False)
                if not candidates and not batches:
                    self.group_windows.pop(key, None)
                    return
                if key in self.resetting or key in self.receivers:
                    return
                direct = any(message.direct for message in candidates) or any(
                    message.direct and message.batch not in self.scheduled
                    for message in batches
                )
                busy = key in self.jobs or any(
                    message.batch in self.scheduled for message in batches
                )
                if busy and not direct:
                    LOG.debug(
                        "Group observation waiting chat=%s reason=busy candidates=%d",
                        key,
                        len(candidates),
                    )
                    return
                now = asyncio.get_running_loop().time()
                delay = max(0, window.deadline - now) if window and candidates else 0
                if not direct:
                    delay = max(delay, self.groups.delay(key))
                if delay > 0:
                    LOG.debug(
                        "Group observation waiting chat=%s candidates=%d direct=%s seconds=%.3f",
                        key,
                        len(candidates),
                        direct,
                        delay,
                    )
                    if window is None:
                        window = GroupWindow(now, now)
                        self.group_windows[key] = window
                    try:
                        await asyncio.wait_for(window.changed.wait(), delay)
                    except TimeoutError:
                        pass
                    continue
                freeze = bool(candidates) and (
                    any(message.direct for message in candidates)
                    or (not direct and not batches)
                )
                if freeze:
                    LOG.info(
                        "Group observation admitted chat=%s candidates=%d direct=%s",
                        key,
                        len(candidates),
                        direct,
                    )
                    selected = (
                        [message for message in candidates if message.direct]
                        if busy
                        else candidates
                    )
                    self.groups.freeze(selected[-1], direct_only=busy)
                    if len(selected) == len(candidates):
                        self.group_windows.pop(key, None)
                for message in self.groups.pending(key, retry=False):
                    if message.batch in self.scheduled:
                        continue
                    if direct and not message.direct:
                        continue
                    self.scheduled.add(message.batch)
                    self.queue.put_nowait(message)
                    self.mark(message, "queued")
                    if not direct:
                        break
                return
        except Exception as error:
            LOG.error("Group scheduling failed chat=%s error=%s", key, log_text(error))
        finally:
            if self.group_timers.get(key) is asyncio.current_task():
                self.group_timers.pop(key, None)

    def flush_group(self, key):
        if self.closing or not key.startswith("group-") or key in self.resetting:
            return
        if key in self.group_timers:
            window = self.group_windows.get(key)
            if window:
                window.changed.set()
            return
        self.group_timers[key] = asyncio.create_task(self.schedule_group(key))

    async def close_timers(self):
        self.closing = True
        timers = list(self.group_timers.values())
        for timer in timers:
            timer.cancel()
        await asyncio.gather(*timers, return_exceptions=True)
        self.group_windows.clear()

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
            if key.startswith("group-"):
                self.flush_group(key)

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
            await self.close_timers()
            jobs = list(self.jobs.values())
            for job in jobs:
                job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
            for key in list(self.jobs):
                self.clear_chat(key)

    async def cancel_chat(self, key, identifier=None):
        if key.startswith("group-"):
            timer = self.group_timers.pop(key, None)
            if timer:
                timer.cancel()
                await asyncio.gather(timer, return_exceptions=True)
            self.group_windows.pop(key, None)
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
