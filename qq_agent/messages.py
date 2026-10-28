import asyncio
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .config import qq_id
from .faces import face_label


def clean_name(value):
    return " ".join(value.split())[:64] if isinstance(value, str) else ""


def display_name(info, group=False):
    if not isinstance(info, dict):
        return ""
    return (clean_name(info.get("card")) if group else "") or clean_name(
        info.get("nickname")
    )


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
    bot_id: str
    sender_id: str
    generation: int = 0
    timestamp: float = 0
    time_source: str = "received"
    mentioned: bool = False
    batch: str | None = None
    direct: bool = False
    category: str = "normal"
    image_keys: dict[str, str] = field(default_factory=dict)


def media_key(source, unique=None):
    value = unique or source
    match = re.fullmatch(r"(?:.*\.)?([a-fA-F0-9]{32})(?:\.[A-Za-z0-9]+)?", value)
    return "md5:" + match[1].lower() if match else "file:" + value


def image_source(data):
    file = data.get("file")
    if file == "marketface":
        return data.get("file_id") or data.get("url") or file
    return file


def is_command(message):
    if (
        message.images
        or message.unsupported
        or any(kind == "face" for kind, _ in message.parts)
    ):
        return False
    if "group_id" in message.target and not message.mentioned:
        return False
    return (
        message.text in {"/new", "/stop", "/status", "/help", "/compact"}
        or ("group_id" in message.target and message.text == "/new blank")
        or message.text.split(maxsplit=1)[:1]
        in (["/model"], ["/profile"], ["/prompt"], ["/group-profile"])
    )


@dataclass
class ImageTurn:
    message: Message
    folder: Path
    token: str = field(default_factory=lambda: uuid.uuid4().hex)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    tasks: set = field(default_factory=set)
    profiles: list = field(default_factory=list)
    blocked_members: set[str] = field(default_factory=set)
    group_writable: bool = True
    chat_output: bool = False


def parse_parts(segments, group, bot=None):
    parts, images, reply, unsupported = [], [], None, False
    for segment in segments:
        if not isinstance(segment, dict) or not isinstance(segment.get("data"), dict):
            raise ValueError("Invalid message segment")
        data = segment["data"]
        match segment.get("type"):
            case "text":
                if not isinstance(data.get("text"), str):
                    raise ValueError("Invalid message text")
                parts.append(("text", data["text"]))
            case "image":
                file = image_source(data)
                if not isinstance(file, str) or not file or len(file) > 4096:
                    raise ValueError("Invalid message image")
                images.append(file)
                if group:
                    sticker = (
                        data.get("sub_type") in (1, "1")
                        or data.get("file") == "marketface"
                    )
                    parts.append(("sticker" if sticker else "image", file))
            case "reply":
                identifier = data.get("id")
                if (
                    reply is not None
                    or type(identifier) not in (str, int)
                    or not str(identifier)
                    or len(str(identifier)) > 128
                ):
                    raise ValueError("Invalid reply")
                reply = str(identifier)
            case "face" if group:
                identifier = data.get("id")
                if type(identifier) not in (str, int) or not re.fullmatch(
                    r"[0-9]{1,10}", str(identifier)
                ):
                    raise ValueError("Invalid QQ face ID")
                parts.append(("face", str(int(identifier))))
            case "at":
                if group:
                    qq = "all" if data.get("qq") == "all" else qq_id(data.get("qq"))
                    parts.append(("at", qq))
            case _:
                unsupported = True
                if group:
                    label = {
                        "forward": "转发的消息",
                        "json": "卡片",
                        "xml": "卡片",
                    }.get(str(segment.get("type")), "不支持的消息")
                    parts.append(("placeholder", f"[{label}，暂不支持读取]"))
    return parts, images, reply, unsupported


def parse_message(event, settings):
    if not isinstance(event, dict) or event.get("post_type") not in {
        "message",
        "message_sent",
    }:
        return None
    try:
        sender = qq_id(event.get("user_id"))
        bot = qq_id(event.get("self_id"))
        segments = event.get("message")
        if not isinstance(segments, list):
            return None
        if event.get("message_type") == "private":
            if sender == bot or sender not in settings.private_users:
                return None
            key, target = f"private-{sender}", {"user_id": int(sender)}
        elif event.get("message_type") == "group":
            group = qq_id(event.get("group_id"))
            if group not in settings.groups:
                return None
            key, target = f"group-{group}", {"group_id": int(group)}
        else:
            return None
        parts, images, reply, unsupported = parse_parts(
            segments, "group_id" in target, bot
        )
        text = "".join(
            face_label(value)
            if kind == "face"
            else value
            if kind == "text"
            else f"@{value}"
            for kind, value in parts
            if kind in {"text", "face"} or (kind == "at" and value != bot)
        ).strip()
        identifier = event.get("message_id")
        if type(identifier) not in (str, int) or not str(identifier):
            return None
        name = display_name(event.get("sender"), "group_id" in target)
        return Message(
            key,
            f"{bot}:{key}:{identifier}",
            target,
            name or sender,
            text,
            parts,
            images,
            reply,
            unsupported,
            bot,
            sender,
            timestamp=event["time"]
            if type(event.get("time")) in (int, float) and event["time"] > 0
            else time.time(),
            time_source="event"
            if type(event.get("time")) in (int, float) and event["time"] > 0
            else "received",
            mentioned=("at", bot) in parts,
            category="normal"
            if any(
                kind == "image" or (kind == "text" and value.strip())
                for kind, value in parts
            )
            else "low",
            image_keys={
                image_source(segment["data"]): media_key(
                    image_source(segment["data"]),
                    segment["data"].get("file_unique")
                    if isinstance(segment["data"].get("file_unique"), str)
                    and 0 < len(segment["data"]["file_unique"]) <= 4096
                    else None,
                )
                for segment in segments
                if segment.get("type") == "image"
            },
        )
    except (ValueError, TypeError, KeyError, AttributeError):
        return None
