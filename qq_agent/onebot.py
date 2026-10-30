import asyncio
import base64
import ipaddress
import json
import socket
import uuid
from collections.abc import Awaitable, Callable
from typing import cast
from urllib.parse import urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver, ResolveResult

from .config import qq_id
from .faces import text_chunks
from .logging import LOG
from .messages import display_name, parse_parts

IMAGE_LIMIT = 10 * 1024 * 1024


class PublicResolver(AbstractResolver):
    async def resolve(self, host, port=0, family=socket.AF_INET) -> list[ResolveResult]:
        records = await asyncio.get_running_loop().getaddrinfo(
            host, port, family=family, type=socket.SOCK_STREAM
        )
        if any(not ipaddress.ip_address(record[4][0]).is_global for record in records):
            raise ValueError("Image URL resolves to a non-public address")
        return [
            {
                "hostname": host,
                "host": cast(str, record[4][0]),
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
        self.on_sent: Callable[..., Awaitable[None]] | None = None

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

    async def send(
        self, message, text=None, image=None, *, sticker=False, system=False, quote=None
    ):
        chunks = text_chunks(
            text or "", faces="group_id" in message.target and not system
        )
        receipts = []
        if image is not None:
            segments = [
                {
                    "type": "image",
                    "data": {
                        "file": "base64://" + base64.b64encode(image).decode(),
                        "sub_type": 1 if sticker else 0,
                    },
                }
            ]
            if quote is not None and not text:
                segments.insert(0, {"type": "reply", "data": {"id": quote}})
            receipt = await self.call(
                "send_msg", {**message.target, "message": segments}
            )
            receipts.append(receipt)
            if self.on_sent:
                await self.on_sent(
                    message,
                    receipt,
                    [("sticker" if sticker else "image", "outgoing")],
                    image,
                    system=system,
                    quote=quote if not text else None,
                )
        if text:
            for index, parts in enumerate(chunks):
                receipt = await self.call(
                    "send_msg",
                    {
                        **message.target,
                        "message": (
                            [{"type": "reply", "data": {"id": quote}}]
                            if quote is not None and index == 0
                            else []
                        )
                        + [
                            {
                                "type": kind,
                                "data": {"id" if kind == "face" else "text": value},
                            }
                            for kind, value in parts
                        ],
                    },
                )
                receipts.append(receipt)
                if self.on_sent:
                    await self.on_sent(
                        message,
                        receipt,
                        parts,
                        system=system,
                        quote=quote if index == 0 else None,
                    )
        return receipts

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

    async def reply_record(self, identifier, target):
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
        sender = result.get("sender")
        if not isinstance(sender, dict):
            raise ValueError("Invalid replied sender")
        sender["user_id"] = qq_id(sender.get("user_id"))
        return result

    async def reply_content(self, identifier, target):
        result = await self.reply_record(identifier, target)
        group = "group_id" in target
        parts, images, _, _ = parse_parts(result["message"], group)
        sender = result["sender"]
        return parts, images, sender["user_id"], display_name(sender, group)

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
