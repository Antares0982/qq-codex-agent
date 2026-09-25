import asyncio
from importlib.resources import files

import aiohttp

from .logging import LOG, log_text

GROUP_REMINDERS = ((30, "thinking_30s.png"), (300, "thinking_too_long.png"))


class Replies:
    def __init__(self, bot):
        self.bot = bot

    async def safe_send(self, message, text, *, intermediate=False):
        if intermediate and "group_id" in message.target:
            return
        try:
            await self.bot.send(message, text=text)
            LOG.info(
                "Sent chat=%s message=%s text=%s",
                message.key,
                message.identifier,
                log_text(text),
            )
        except (
            ConnectionError,
            RuntimeError,
            TimeoutError,
            aiohttp.ClientError,
        ) as error:
            LOG.warning(
                "Reply delivery failed chat=%s message=%s error=%s",
                message.key,
                message.identifier,
                log_text(error),
            )

    async def react_message(self, message):
        try:
            await self.bot.call(
                "set_msg_emoji_like",
                {
                    "message_id": message.identifier.split(":", 2)[2],
                    "emoji_id": "124",
                    "set": True,
                },
            )
        except (ConnectionError, RuntimeError, TimeoutError, aiohttp.ClientError):
            LOG.warning("Message reaction failed")

    async def remind_group(self, message, started):
        loop = asyncio.get_running_loop()
        for delay, filename in GROUP_REMINDERS:
            await asyncio.sleep(max(0, started + delay - loop.time()))
            try:
                data = files("pics").joinpath(filename).read_bytes()
                await self.bot.send(message, image=data)
            except (OSError, RuntimeError, TimeoutError, aiohttp.ClientError):
                LOG.warning("Group reminder delivery failed")
