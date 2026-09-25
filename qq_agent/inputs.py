import uuid

import aiohttp
from openai_codex import LocalImageInput, TextInput

from .messages import display_name
from .onebot import image_suffix


class Inputs:
    def __init__(self, bot, send):
        self.bot = bot
        self.safe_send = send

    async def group_name(self, target, user):
        try:
            info = await self.bot.call(
                "get_group_member_info",
                {"group_id": target["group_id"], "user_id": int(user)},
            )
            return display_name(info, group=True) or user
        except (
            AttributeError,
            ConnectionError,
            RuntimeError,
            TimeoutError,
            aiohttp.ClientError,
        ):
            return user

    async def render_parts(self, parts, target, names):
        content = []
        for kind, value in parts:
            if kind == "text":
                content.append(value)
            elif value == "all":
                content.append("@全体成员")
            else:
                if value not in names:
                    names[value] = await self.group_name(target, value)
                content.append("@" + names[value] + f"（ID: {value}）")
        return "".join(content).strip()

    async def prepare_input(self, message, folder):
        images, quote_parts, quote_images = list(message.images), [], []
        if message.reply is not None:
            quote_parts, quote_images = await self.bot.reply_content(
                message.reply, message.target
            )
            images.extend(quote_images)
            if (
                not images
                and not message.text
                and not any(value.strip() for _, value in quote_parts)
            ):
                await self.safe_send(message, "回复的消息中没有可读取的文本或图片。")
                return None
        if len(images) > 5:
            await self.safe_send(message, "每条消息最多 5 张图片。")
            return None
        names = {}
        content = await self.render_parts(message.parts, message.target, names)
        if quote_parts and not quote_images:
            quote = await self.render_parts(quote_parts, message.target, names)
            if quote:
                quote = "\n".join(f"> {line}" for line in quote.splitlines())
                content = "\n\n".join(part for part in (quote, content) if part)
        sender = message.sender_name
        if "group_id" in message.target:
            sender += f"（ID: {message.sender_id}）"
        inputs = [TextInput(f"QQ 用户 {sender}:\n{content}")]
        for image in images:
            data = await self.bot.image(image)
            path = folder / (uuid.uuid4().hex + image_suffix(data))
            path.write_bytes(data)
            inputs.append(LocalImageInput(str(path)))
        return inputs
