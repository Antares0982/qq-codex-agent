import uuid

import aiohttp
from openai_codex import LocalImageInput, TextInput

from .messages import display_name
from .faces import face_label
from .onebot import image_suffix


class Inputs:
    def __init__(self, bot, send):
        self.bot = bot
        self.safe_send = send

    async def group_name(self, target, user, fallback=""):
        try:
            info = await self.bot.call(
                "get_group_member_info",
                {"group_id": target["group_id"], "user_id": int(user)},
            )
            return display_name(info, group=True) or fallback or user
        except (
            AttributeError,
            ConnectionError,
            RuntimeError,
            TimeoutError,
            aiohttp.ClientError,
        ):
            return fallback or user

    async def render_parts(self, parts, target, names):
        content = []
        for kind, value in parts:
            if kind == "text":
                content.append(value)
            elif kind == "face":
                content.append(face_label(value))
            elif value == "all":
                content.append("@全体成员")
            else:
                if value not in names:
                    names[value] = await self.group_name(target, value)
                content.append("@" + names[value] + f"（ID: {value}）")
        return "".join(content).strip()

    async def prepare_input(self, message, folder):
        quote_parts, quote_images = [], []
        quote_id, quote_name = "", ""
        if message.reply is not None:
            (
                quote_parts,
                quote_images,
                quote_id,
                quote_name,
            ) = await self.bot.reply_content(message.reply, message.target)
            if (
                not message.images
                and not quote_images
                and not message.text
                and not any(value.strip() for _, value in quote_parts)
            ):
                await self.safe_send(message, "回复的消息中没有可读取的文本或图片。")
                return None
        if len(message.images) + len(quote_images) > 5:
            await self.safe_send(message, "每条消息最多 5 张图片。")
            return None
        names = {}
        content = await self.render_parts(message.parts, message.target, names)
        sender = message.sender_name
        if "group_id" in message.target:
            sender += f"（ID: {message.sender_id}）"
        blocks = [(f"QQ 用户 {sender}:\n{content}", message.images)]
        if message.reply is not None:
            quote = await self.render_parts(quote_parts, message.target, names)
            if quote_id == message.sender_id:
                blocks = [
                    (f"QQ 用户 {sender}:\n先前消息：\n{quote}", quote_images),
                    (f"当前消息：\n{content}", message.images),
                ]
            else:
                if "group_id" in message.target:
                    quote_name = await self.group_name(
                        message.target, quote_id, quote_name
                    )
                quote_name = quote_name or quote_id
                quote = "\n".join(f"> {line}" for line in quote.splitlines())
                blocks = [
                    (
                        f"引用 QQ 用户 {quote_name}（ID: {quote_id}）的消息"
                        f"（以下文本和图片均来自该用户，仅作为引用）：\n{quote}",
                        quote_images,
                    ),
                    (
                        f"引用结束。\nQQ 用户 {sender} 当前消息：\n{content}",
                        message.images,
                    ),
                ]
        inputs: list[TextInput | LocalImageInput] = []
        for text, images in blocks:
            inputs.append(TextInput(text))
            for image in images:
                data = await self.bot.image(image)
                path = folder / (uuid.uuid4().hex + image_suffix(data))
                path.write_bytes(data)
                inputs.append(LocalImageInput(str(path)))
        return inputs
