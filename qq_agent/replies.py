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
            await self.bot.send(
                message,
                text=text,
                **({"system": True} if "group_id" in message.target else {}),
            )
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

    async def remind_group(self, message, started):
        loop = asyncio.get_running_loop()
        for delay, filename in GROUP_REMINDERS:
            await asyncio.sleep(max(0, started + delay - loop.time()))
            try:
                data = files("pics").joinpath(filename).read_bytes()
                await self.bot.send(message, image=data, sticker=True, system=True)
            except (OSError, RuntimeError, TimeoutError, aiohttp.ClientError):
                LOG.warning("Group reminder delivery failed")
