import argparse
import asyncio
import logging
import os
import signal

from openai_codex import AsyncCodex

from .agent import Agent
from .config import Settings
from .onebot import OneBot
from .runtime import codex_config


async def run(settings, login):
    os.umask(0o077)
    os.environ.pop("NAPCAT_WS_TOKEN", None)
    if not login:
        settings.check_prompts()
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
        server = await asyncio.start_unix_server(
            agent.tools.send_image, path=socket_path
        )
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        watcher = agent.runtime.watch_codex()
        watcher.__enter__()
        workers = [
            asyncio.create_task(bot.listen(agent.receive)),
            asyncio.create_task(agent.work()),
            asyncio.create_task(agent.cleanup.cleanup_files()),
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
            watcher.__exit__(None, None, None)
            agent.db.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="/etc/qq-codex-agent/config.toml")
    parser.add_argument("--login", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    asyncio.run(run(Settings.load(args.config), args.login))
