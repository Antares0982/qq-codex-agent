import asyncio
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .config import qq_id


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


@dataclass
class ImageTurn:
    message: Message
    folder: Path
    token: str = field(default_factory=lambda: uuid.uuid4().hex)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    tasks: set = field(default_factory=set)
    profiles: list = field(default_factory=list)
    profile_writable: bool = True


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
                file = data.get("file")
                if not isinstance(file, str) or not file or len(file) > 4096:
                    raise ValueError("Invalid message image")
                images.append(file)
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
            case "at":
                if group and str(data.get("qq")) != bot:
                    qq = "all" if data.get("qq") == "all" else qq_id(data.get("qq"))
                    parts.append(("at", qq))
            case _:
                unsupported = True
    return parts, images, reply, unsupported


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
        parts, images, reply, unsupported = parse_parts(
            segments, "group_id" in target, bot
        )
        text = "".join(
            value if kind == "text" else f"@{value}" for kind, value in parts
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
        )
    except (ValueError, TypeError, KeyError, AttributeError):
        return None
