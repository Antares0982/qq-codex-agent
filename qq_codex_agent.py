from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import ipaddress
import json
import logging
import os
import shutil
import signal
import socket
import sqlite3
import sys
import time
import tomllib
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
from openai_codex.generated.v2_all import MessagePhase, ReasoningEffort, TurnStatus
from openai_codex import (
    ApprovalMode,
    AsyncCodex,
    AsyncThread,
    CodexConfig,
    LocalImageInput,
    Sandbox,
    TextInput,
)

LOG = logging.getLogger("qq-codex-agent")
IMAGE_LIMIT = 10 * 1024 * 1024
OUTPUT_LIMIT = 32 * 1024 * 1024
THREAD_IDLE = 2 * 60 * 60
HELP = """直接发送文字或图片，可提问、执行代码或请求生成图片。
群聊可先发送图片，再回复该图片并 @ bot 提问。
/help 查看用法
/model 列出可选模型及本会话设置
/model <模型ID> 切换本会话模型，下一轮请求生效
/status 查看登录、任务状态、thread 标题及用户消息数
/stop 停止本会话任务并清空队列
/new 重置会话并删除工作文件，保留模型选择
推理强度固定为 medium。模型选择在重启后保留。
群聊只发送图片和最终文字，不发送开始及工具进度通知。
私聊与各群权限独立；群聊须获该群授权并 @ bot。同群共享会话和模型设置。"""


def qq_id(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("QQ IDs must be positive decimal numbers")
    value = str(value)
    if not value.isascii() or not value.isdecimal() or int(value) <= 0:
        raise ValueError("QQ IDs must be positive decimal numbers")
    return str(int(value))


def clean_name(value):
    return " ".join(value.split())[:64] if isinstance(value, str) else ""


@dataclass
class Settings:
    private_users: set[str]
    groups: dict[str, set[str]]
    napcat_url: str
    token_file: Path
    state_dir: Path
    workspace_dir: Path
    agents_file: Path
    queue_limit: int = 8
    task_timeout: int = 900
    model: str | None = None

    @classmethod
    def load(cls, path):
        raw = tomllib.loads(Path(path).read_text())
        if raw.keys() & {"allowed_users", "allowed_groups"}:
            raise ValueError(
                "Migrate allowed_users/allowed_groups to private_users/groups"
            )
        allowlist = raw.pop("allowlist_file", None)
        if allowlist is not None:
            if not isinstance(allowlist, str) or not Path(allowlist).is_absolute():
                raise ValueError("allowlist_file must be absolute")
            entries = tomllib.loads(Path(allowlist).read_text())
            if entries.keys() - {"private_users", "groups"}:
                raise ValueError("Unknown allowlist fields")
            raw["private_users"] = entries.get("private_users", [])
            raw["groups"] = entries.get("groups", {})
        private_users = raw.get("private_users", [])
        if not isinstance(private_users, list):
            raise ValueError("private_users must be an array")
        raw["private_users"] = {qq_id(value) for value in private_users}
        groups = raw.get("groups", {})
        if not isinstance(groups, dict):
            raise ValueError("groups must be a table")
        raw["groups"] = {}
        for group, entry in groups.items():
            group = qq_id(group)
            if group in raw["groups"]:
                raise ValueError("Duplicate normalized group ID")
            if not isinstance(entry, dict) or entry.keys() - {"users"}:
                raise ValueError("Group entries must contain only users")
            users = entry.get("users", [])
            if not isinstance(users, list):
                raise ValueError("Group users must be an array")
            raw["groups"][group] = {
                "all" if value == "all" else qq_id(value) for value in users
            }
        for key in ("token_file", "state_dir", "workspace_dir", "agents_file"):
            raw[key] = Path(raw[key])
            if not raw[key].is_absolute():
                raise ValueError(f"{key} must be absolute")
        settings = cls(**raw)
        url = urlsplit(settings.napcat_url)
        if (
            url.scheme != "ws"
            or url.hostname not in {"127.0.0.1", "::1"}
            or url.query
            or url.username
        ):
            raise ValueError("napcat_url must be a loopback ws URL without credentials")
        for key in ("queue_limit", "task_timeout"):
            value = getattr(settings, key)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{key} must be positive")
        return settings


@dataclass
class Message:
    key: str
    identifier: str
    target: dict
    sender_name: str
    text: str
    parts: list[tuple[str, str]]
    images: list[str]
    reply: str | None
    unsupported: bool
    generation: int = 0


def parse_message(event, settings):
    if not isinstance(event, dict) or event.get("post_type") != "message":
        return None
    try:
        sender = qq_id(event.get("user_id"))
        bot = qq_id(event.get("self_id"))
        if sender == bot:
            return None
        segments = event.get("message")
        if not isinstance(segments, list):
            return None
        if event.get("message_type") == "private":
            if sender not in settings.private_users:
                return None
            key, target = f"private-{sender}", {"user_id": int(sender)}
        elif event.get("message_type") == "group":
            group = qq_id(event.get("group_id"))
            users = settings.groups.get(group, set())
            if not (sender in users or "all" in users) or not any(
                segment.get("type") == "at"
                and str(segment.get("data", {}).get("qq")) == bot
                for segment in segments
                if isinstance(segment, dict)
            ):
                return None
            key, target = f"group-{group}", {"group_id": int(group)}
        else:
            return None
        text, parts, images, reply, unsupported = [], [], [], None, False
        for segment in segments:
            data = segment["data"]
            match segment["type"]:
                case "text":
                    if not isinstance(data.get("text"), str):
                        return None
                    text.append(data["text"])
                    parts.append(("text", data["text"]))
                case "image":
                    file = data.get("file")
                    if not isinstance(file, str) or not file or len(file) > 4096:
                        return None
                    images.append(file)
                case "reply":
                    identifier = data.get("id")
                    if (
                        reply is not None
                        or type(identifier) not in (str, int)
                        or not str(identifier)
                        or len(str(identifier)) > 128
                    ):
                        return None
                    reply = str(identifier)
                case "at":
                    if "group_id" in target and str(data.get("qq")) != bot:
                        qq = (
                            "all" if data.get("qq") == "all" else qq_id(data.get("qq"))
                        )
                        text.append(f"@{qq}")
                        parts.append(("at", qq))
                case _:
                    unsupported = True
        identifier = event.get("message_id")
        if type(identifier) not in (str, int) or not str(identifier):
            return None
        sender_info = event.get("sender")
        name = clean_name(sender_info.get("nickname")) if isinstance(sender_info, dict) else ""
        return Message(
            key,
            f"{bot}:{key}:{identifier}",
            target,
            name or sender,
            "".join(text).strip(),
            parts,
            images,
            reply,
            unsupported,
        )
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


class PublicResolver(aiohttp.abc.AbstractResolver):
    async def resolve(self, host, port=0, family=socket.AF_INET):
        records = await asyncio.get_running_loop().getaddrinfo(
            host, port, family=family, type=socket.SOCK_STREAM
        )
        if any(not ipaddress.ip_address(record[4][0]).is_global for record in records):
            raise ValueError("Image URL resolves to a non-public address")
        return [
            {
                "hostname": host,
                "host": record[4][0],
                "port": port,
                "family": record[0],
                "proto": record[2],
                "flags": socket.AI_NUMERICHOST,
            }
            for record in records
        ]

    async def close(self):
        pass


def image_suffix(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    raise ValueError("Unsupported image encoding")


class OneBot:
    def __init__(self, settings):
        self.settings = settings
        self.ws = None
        self.pending = {}

    async def call(self, action, params):
        ws = self.ws
        if ws is None or ws.closed:
            raise ConnectionError("NapCat disconnected")
        echo = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[echo] = future
        try:
            await ws.send_json({"action": action, "params": params, "echo": echo})
            response = await asyncio.wait_for(future, 30)
            if response.get("status") != "ok" or response.get("retcode") != 0:
                raise RuntimeError("NapCat action failed")
            return response.get("data") or {}
        finally:
            self.pending.pop(echo, None)

    async def send(self, message, text=None, image=None):
        if image is not None:
            segments = [
                {
                    "type": "image",
                    "data": {"file": "base64://" + base64.b64encode(image).decode()},
                }
            ]
            await self.call("send_msg", {**message.target, "message": segments})
        if text:
            for offset in range(0, len(text), 1500):
                await self.call(
                    "send_msg",
                    {
                        **message.target,
                        "message": [
                            {
                                "type": "text",
                                "data": {"text": text[offset : offset + 1500]},
                            }
                        ],
                    },
                )

    async def image(self, file):
        result = await self.call("get_image", {"file": file})
        encoded = result.get("base64")
        if encoded:
            if (
                not isinstance(encoded, str)
                or len(encoded) > (IMAGE_LIMIT + 2) // 3 * 4
            ):
                raise ValueError("Image exceeds 10 MiB")
            data = base64.b64decode(encoded, validate=True)
        else:
            url = result.get("url", "")
            parts = urlsplit(url)
            if (
                parts.scheme != "https"
                or not parts.hostname
                or parts.username
                or parts.port not in (None, 443)
            ):
                raise ValueError("NapCat did not provide downloadable image bytes")
            try:
                address = ipaddress.ip_address(parts.hostname)
            except ValueError:
                address = None
            if address is not None and not address.is_global:
                raise ValueError("Non-public image URL")
            connector = aiohttp.TCPConnector(resolver=PublicResolver())
            async with aiohttp.ClientSession(
                connector=connector,
                trust_env=False,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as client:
                async with client.get(url, allow_redirects=False) as response:
                    if response.status != 200:
                        raise ValueError("Image download failed")
                    data = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        data.extend(chunk)
                        if len(data) > IMAGE_LIMIT:
                            raise ValueError("Image exceeds 10 MiB")
                    data = bytes(data)
        if not data or len(data) > IMAGE_LIMIT:
            raise ValueError("Image exceeds 10 MiB or is empty")
        image_suffix(data)
        return data

    async def reply_images(self, identifier, target):
        result = await self.call("get_msg", {"message_id": identifier})
        expected = "group" if "group_id" in target else "private"
        if result.get("message_type") != expected:
            raise ValueError("Reply belongs to another conversation")
        if (
            expected == "group"
            and result.get("group_id") is not None
            and qq_id(result["group_id"]) != str(target["group_id"])
        ):
            raise ValueError("Reply belongs to another group")
        segments = result.get("message")
        if not isinstance(segments, list):
            raise ValueError("Invalid replied message")
        images = []
        for segment in segments:
            if not isinstance(segment, dict) or not isinstance(
                segment.get("data"), dict
            ):
                raise ValueError("Invalid replied message")
            if segment.get("type") != "image":
                continue
            file = segment["data"].get("file")
            if not isinstance(file, str) or not file or len(file) > 4096:
                raise ValueError("Invalid replied image")
            images.append(file)
        return images

    async def listen(self, receive):
        token = self.settings.token_file.read_text().strip()
        async with aiohttp.ClientSession(trust_env=False) as client:
            while True:
                try:
                    async with client.ws_connect(
                        self.settings.napcat_url,
                        params={"access_token": token} if token else {},
                        heartbeat=30,
                        max_msg_size=16 * 1024 * 1024,
                    ) as ws:
                        self.ws = ws
                        LOG.info("NapCat connected")
                        async for frame in ws:
                            if frame.type != aiohttp.WSMsgType.TEXT:
                                continue
                            try:
                                event = json.loads(frame.data)
                            except ValueError:
                                continue
                            if not isinstance(event, dict):
                                continue
                            echo = event.get("echo")
                            if isinstance(echo, str) and echo in self.pending:
                                future = self.pending[echo]
                                if not future.done():
                                    future.set_result(event)
                            else:
                                receive(event)
                except (aiohttp.ClientError, OSError, TimeoutError):
                    LOG.warning("NapCat connection lost")
                finally:
                    self.ws = None
                    for future in list(self.pending.values()):
                        if not future.done():
                            future.set_exception(ConnectionError("NapCat disconnected"))
                await asyncio.sleep(3)


class Agent:
    def __init__(self, settings, bot, codex):
        self.settings, self.bot, self.codex = settings, bot, codex
        settings.state_dir.mkdir(parents=True, exist_ok=True)
        settings.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(settings.state_dir / "sessions.sqlite")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (key TEXT PRIMARY KEY, thread TEXT, folder TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS messages (id TEXT PRIMARY KEY, status TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS models (key TEXT PRIMARY KEY, model TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS activity (key TEXT PRIMARY KEY, received REAL NOT NULL, message_gen INTEGER NOT NULL, thread_gen INTEGER NOT NULL);
            UPDATE messages SET status='interrupted' WHERE status IN ('queued', 'running');
        """)
        self.queue = asyncio.Queue(settings.queue_limit)
        self.active = None
        self.job = None
        self.controls = set()
        self.resetting = set()
        self.generated = []
        self.image_message = None

    async def send_image(self, reader, writer):
        try:
            request = json.loads(await reader.readline())
            if request != {"action": "send_image"} or self.image_message is None:
                raise ValueError("No active image turn")
            if not self.generated:
                raise ValueError("No generated image available")
            await self.bot.send(self.image_message, image=self.generated[-1])
            self.generated.pop()
            response = {"ok": True}
        except (ValueError, ConnectionError, RuntimeError, TimeoutError) as error:
            response = {"error": str(error)}
        except Exception as error:
            LOG.warning("Image delivery failed: %s", type(error).__name__)
            response = {"error": "Image delivery failed"}
        writer.write((json.dumps(response) + "\n").encode())
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    def receive(self, event):
        message = parse_message(event, self.settings)
        if message is None:
            return
        with self.db:
            inserted = self.db.execute(
                "INSERT OR IGNORE INTO messages VALUES (?, 'received')",
                (message.identifier,),
            ).rowcount
        if not inserted:
            return
        command = message.text if not message.images and not message.unsupported else ""
        if command in {"/new", "/stop", "/status", "/help"} or (
            command.split(maxsplit=1)[:1] == ["/model"]
        ):
            if len(self.controls) < 8:
                task = asyncio.create_task(self.control(message))
                self.controls.add(task)
                task.add_done_callback(self.controls.discard)
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
        elif self.queue.full():
            reason = "任务队列已满，请稍后重试。"
        if reason:
            if len(self.controls) < 8:
                task = asyncio.create_task(self.safe_send(message, reason))
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

    def mark(self, message, status):
        with self.db:
            self.db.execute(
                "UPDATE messages SET status=? WHERE id=?", (status, message.identifier)
            )

    async def safe_send(self, message, text, *, intermediate=False):
        if intermediate and "group_id" in message.target:
            return
        try:
            await self.bot.send(message, text=text)
        except (ConnectionError, RuntimeError, TimeoutError, aiohttp.ClientError):
            LOG.warning("Reply delivery failed")

    def selected_model(self, key):
        row = self.db.execute("SELECT model FROM models WHERE key=?", (key,)).fetchone()
        return row[0] if row else self.settings.model

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
            selected = self.selected_model(message.key) or "Codex 默认（未固定）"
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
                busy = self.active is not None and self.active.key == message.key
                row = self.db.execute(
                    "SELECT thread FROM sessions WHERE key=?", (message.key,)
                ).fetchone()
                details = "Thread 标题：尚未创建\n用户消息：0 条"
                if row and row[0]:
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
                        title = result.thread.name or "未命名"
                        details = f"Thread 标题：{title}\n用户消息：{count} 条"
                    except Exception as error:
                        LOG.warning("Thread status failed: %s", type(error).__name__)
                        details = "Thread 标题及用户消息数：暂时无法读取"
                await self.safe_send(
                    message,
                    f"{text}；本会话{'执行中' if busy else '空闲'}。\n{details}",
                )
                return
            if message.key in self.resetting:
                return
            self.resetting.add(message.key)
            try:
                retained = []
                while not self.queue.empty():
                    queued = self.queue.get_nowait()
                    self.queue.task_done()
                    if queued.key != message.key:
                        retained.append(queued)
                    else:
                        self.mark(queued, "canceled")
                for queued in retained:
                    self.queue.put_nowait(queued)
                if self.active and self.active.key == message.key and self.job:
                    self.job.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await self.job
                if message.text == "/new":
                    row = self.db.execute(
                        "SELECT folder FROM sessions WHERE key=?", (message.key,)
                    ).fetchone()
                    if row:
                        folder = self.settings.workspace_dir / row[0]
                        if (
                            folder.parent != self.settings.workspace_dir
                            or folder.is_symlink()
                        ):
                            raise ValueError("Invalid workspace")
                        if folder.exists():
                            shutil.rmtree(folder)
                    with self.db:
                        self.db.execute(
                            "DELETE FROM sessions WHERE key=?", (message.key,)
                        )
                        self.db.execute(
                            "DELETE FROM activity WHERE key=?", (message.key,)
                        )
                await self.safe_send(
                    message,
                    "已重置会话及工作文件。"
                    if message.text == "/new"
                    else "已停止本会话任务并清空队列。",
                )
            finally:
                self.resetting.discard(message.key)
        except Exception as error:
            LOG.warning("Control failed: %s", type(error).__name__)
            await self.safe_send(message, "操作失败，请查看服务日志中的错误类型。")

    async def execute(self, message):
        turn = None
        terminal = False
        starting_turn = False
        try:
            async with asyncio.timeout(self.settings.task_timeout):
                account = await self.codex.account()
                if account.account is None:
                    await self.safe_send(message, "尚未登录，请管理员完成设备码登录。")
                    self.mark(message, "failed")
                    return
                images = list(message.images)
                if message.reply is not None:
                    images.extend(
                        await self.bot.reply_images(message.reply, message.target)
                    )
                    if not images and not message.text:
                        await self.safe_send(message, "回复的消息中没有可读取的图片。")
                        self.mark(message, "failed")
                        return
                if len(images) > 5:
                    await self.safe_send(message, "每条消息最多 5 张图片。")
                    self.mark(message, "failed")
                    return
                row = self.db.execute(
                    "SELECT thread, folder FROM sessions WHERE key=?", (message.key,)
                ).fetchone()
                thread_id, folder_name = row if row else (None, uuid.uuid4().hex)
                row = self.db.execute(
                    "SELECT thread_gen FROM activity WHERE key=?",
                    (message.key,),
                ).fetchone()
                renewing = thread_id is not None and message.generation > (
                    row[0] if row else 0
                )
                if renewing:
                    thread_id = None
                folder = self.settings.workspace_dir / folder_name
                folder.mkdir(mode=0o700, exist_ok=True)
                options = {
                    "cwd": str(folder),
                    "sandbox": Sandbox.workspace_write,
                    "approval_mode": ApprovalMode.auto_review,
                }
                model = self.selected_model(message.key)
                if model:
                    options["model"] = model
                if thread_id:
                    thread = await self.codex.thread_resume(thread_id, **options)
                else:
                    thread = await self.codex.thread_start(
                        **options,
                        config={"projects": {str(folder): {"trust_level": "trusted"}}},
                    )
                    with self.db:
                        self.db.execute(
                            "INSERT OR REPLACE INTO sessions VALUES (?, ?, ?)",
                            (message.key, thread.id, folder_name),
                        )
                        self.db.execute(
                            "UPDATE activity SET thread_gen=? WHERE key=?",
                            (message.generation, message.key),
                        )
                if renewing:
                    await self.safe_send(
                        message,
                        "距上一条消息已超过两小时，已新建一个 thread。",
                        intermediate=True,
                    )
                await self.safe_send(message, "开始处理……", intermediate=True)
                content, names = [], {}
                for kind, value in message.parts:
                    if kind == "text":
                        content.append(value)
                    elif value == "all":
                        content.append("@全体成员")
                    else:
                        if value not in names:
                            try:
                                info = await self.bot.call(
                                    "get_group_member_info",
                                    {
                                        "group_id": message.target["group_id"],
                                        "user_id": int(value),
                                    },
                                )
                                names[value] = clean_name(info.get("nickname"))
                            except (
                                AttributeError,
                                ConnectionError,
                                RuntimeError,
                                TimeoutError,
                                aiohttp.ClientError,
                            ):
                                names[value] = ""
                        content.append("@" + (names[value] or value))
                inputs = [
                    TextInput(f"QQ 用户 {message.sender_name}:\n{''.join(content).strip()}")
                ]
                for image in images:
                    data = await self.bot.image(image)
                    path = folder / (uuid.uuid4().hex + image_suffix(data))
                    path.write_bytes(data)
                    inputs.append(LocalImageInput(str(path)))
                starting_turn = True
                turn = await thread.turn(
                    inputs,
                    approval_mode=ApprovalMode.auto_review,
                    model=model,
                    effort=ReasoningEffort.medium,
                )
                starting_turn = False
                delivered, final, status = set(), None, None
                self.image_message = message
                self.generated = []
                shown_progress = set()
                async for event in turn.stream():
                    if event.method == "item/started":
                        item = event.payload.item.root
                        progress = {
                            "commandExecution": "正在执行代码……",
                            "imageGeneration": "正在生成图片……",
                            "webSearch": "正在检索资料……",
                        }.get(item.type)
                        if progress and progress not in shown_progress:
                            await self.safe_send(message, progress, intermediate=True)
                            shown_progress.add(progress)
                    elif event.method == "item/completed":
                        item = event.payload.item.root
                        if item.id in delivered:
                            continue
                        delivered.add(item.id)
                        if item.type == "agentMessage" and item.phase in (
                            None,
                            MessagePhase.final_answer,
                        ):
                            final = item.text
                        elif (
                            item.type == "imageGeneration"
                            and item.status == "completed"
                        ):
                            if len(item.result) > (OUTPUT_LIMIT + 2) // 3 * 4:
                                raise ValueError("Generated image too large")
                            data = base64.b64decode(item.result, validate=True)
                            if not data or len(data) > OUTPUT_LIMIT:
                                raise ValueError("Invalid generated image")
                            image_suffix(data)
                            self.generated.append(data)
                    elif event.method == "turn/completed":
                        status = event.payload.turn.status
                        terminal = True
                if status == TurnStatus.completed:
                    await self.safe_send(message, final or "任务完成。")
                    self.mark(message, "completed")
                else:
                    await self.safe_send(
                        message,
                        "任务未完成，可能是额度、认证或自动审批限制；请检查 /status 后重试。",
                    )
                    self.mark(message, "failed")
        except (asyncio.CancelledError, TimeoutError):
            self.mark(message, "interrupted")
            await self.safe_send(
                message,
                "任务已中断或超时，不会自动重跑。",
                intermediate=message.key in self.resetting,
            )
            raise
        except Exception as error:
            LOG.warning("Task failed: %s", type(error).__name__)
            self.mark(message, "failed")
            await self.safe_send(
                message, "任务失败，未自动重试。请检查登录状态、图片格式或服务日志。"
            )
        finally:
            self.image_message = None
            self.generated = []
            if starting_turn:
                LOG.error("Turn start outcome unknown; runtime must restart")
                raise SystemExit(1)
            if turn is not None and not terminal:
                try:
                    await asyncio.wait_for(turn.interrupt(), 10)
                except Exception:
                    LOG.error("Interrupt failed; runtime must restart")
                    raise SystemExit(1)

    async def work(self):
        while True:
            message = await self.queue.get()
            self.active = message
            self.mark(message, "running")
            self.job = asyncio.create_task(self.execute(message))
            try:
                await self.job
            except (asyncio.CancelledError, TimeoutError):
                if asyncio.current_task().cancelling():
                    raise
            finally:
                self.active = self.job = None
                self.queue.task_done()


def codex_config(settings):
    overrides = (
        'forced_login_method="chatgpt"',
        'cli_auth_credentials_store="file"',
        'approval_policy="on-request"',
        'approvals_reviewer="auto_review"',
        'model_reasoning_effort="medium"',
        f'projects.{json.dumps(str(settings.workspace_dir))}.trust_level="trusted"',
        "project_root_markers=[]",
        f'mcp_servers.qq_image.command={json.dumps(sys.executable)}',
        f'mcp_servers.qq_image.args={json.dumps([str(Path(__file__).with_name("image_tool.py")), str(settings.state_dir / "image.sock")])}',
        'mcp_servers.qq_image.required=true',
        'mcp_servers.qq_image.default_tools_approval_mode="approve"',
    )
    return CodexConfig(
        config_overrides=overrides,
        env={"CODEX_HOME": str(settings.state_dir / "codex")},
    )


async def run(settings, login):
    os.umask(0o077)
    os.environ.pop("NAPCAT_WS_TOKEN", None)
    async with AsyncCodex(config=codex_config(settings)) as codex:
        if login:
            handle = await codex.login_chatgpt_device_code()
            print(handle.verification_url, handle.user_code, flush=True)
            completed = await handle.wait()
            if not completed.success:
                raise RuntimeError("Device login failed")
            print("设备码登录成功。")
            return
        bot = OneBot(settings)
        agent = Agent(settings, bot, codex)
        socket_path = settings.state_dir / "image.sock"
        socket_path.unlink(missing_ok=True)
        server = await asyncio.start_unix_server(agent.send_image, path=socket_path)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        workers = [
            asyncio.create_task(bot.listen(agent.receive)),
            asyncio.create_task(agent.work()),
            asyncio.create_task(stop.wait()),
        ]
        try:
            done, _ = await asyncio.wait(workers, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in [*workers, *agent.controls]:
                task.cancel()
            await asyncio.gather(*workers, *agent.controls, return_exceptions=True)
            server.close()
            await server.wait_closed()
            socket_path.unlink(missing_ok=True)
            agent.db.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="/etc/qq-codex-agent/config.toml")
    parser.add_argument("--login", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    asyncio.run(run(Settings.load(args.config), args.login))


if __name__ == "__main__":
    main()
