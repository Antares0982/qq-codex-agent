import argparse
import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import os
import signal
import time
from pathlib import Path

from openai_codex import AsyncCodex, CodexConfig

LOG = logging.getLogger(__name__)


def token_claims(token):
    body = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))


class Broker:
    def __init__(self, codex, home):
        self.codex = codex
        self.home = home
        self.lock = asyncio.Lock()
        self.refreshing = None

    def read(self):
        data = json.loads((self.home / "auth.json").read_text())
        tokens = data["tokens"]
        access = tokens["access_token"]
        claims = token_claims(access)
        account = tokens.get("account_id")
        if not isinstance(account, str) or not account:
            raise ValueError("Missing account identity")
        auth = claims.get("https://api.openai.com/auth", {})
        result = {
            "accessToken": access,
            "chatgptAccountId": account,
            "generation": hashlib.sha256(access.encode()).hexdigest(),
        }
        plan = auth.get("chatgpt_plan_type")
        if plan:
            result["chatgptPlanType"] = plan
        return result, float(claims["exp"])

    async def get(self, request):
        async with self.lock:
            tokens, expiry = self.read()
            account = request.get("account")
            if account and account != tokens["chatgptAccountId"]:
                raise ValueError("Account mismatch")
            refresh = request.get("refresh", False)
            generation = request.get("generation", "")
            if expiry < time.time() + 300 or (
                refresh and generation == tokens["generation"]
            ):
                if self.refreshing is None or self.refreshing.done():
                    self.refreshing = asyncio.create_task(
                        self.codex.account(refresh_token=True)
                    )
                await asyncio.shield(self.refreshing)
                updated, expiry = self.read()
                if tokens["chatgptAccountId"] != updated["chatgptAccountId"]:
                    raise ValueError("Account changed during refresh")
                tokens = updated
            if expiry <= time.time():
                raise ValueError("Expired access token")
            return tokens

    async def serve(self, reader, writer):
        task = None
        try:
            raw = await asyncio.wait_for(reader.readline(), 3)
            request = json.loads(raw)
            if not isinstance(request, dict) or request.keys() - {
                "refresh",
                "generation",
                "account",
            }:
                raise ValueError("Invalid request")
            if type(request.get("refresh", False)) is not bool:
                raise ValueError("Invalid refresh flag")
            for key in ("generation", "account"):
                value = request.get(key)
                if value is not None and (
                    not isinstance(value, str) or len(value) > 256
                ):
                    raise ValueError("Invalid identity")
            task = asyncio.create_task(self.get(request))
            response = await asyncio.wait_for(asyncio.shield(task), 7)
        except Exception:
            LOG.warning("Credential request unavailable")
            response = {"error": "auth_unavailable"}
            if task is not None:
                task.add_done_callback(consume_result)
        try:
            writer.write(json.dumps(response).encode() + b"\n")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def maintain(self):
        while True:
            try:
                await self.get({})
            except Exception:
                LOG.warning("Credential maintenance unavailable")
            await asyncio.sleep(60)


def consume_result(task):
    with contextlib.suppress(Exception, asyncio.CancelledError):
        task.result()


async def run(args):
    os.umask(0o077)
    args.home.mkdir(parents=True, exist_ok=True, mode=0o700)
    config = CodexConfig(
        env={"CODEX_HOME": str(args.home)},
        config_overrides=(
            'forced_login_method="chatgpt"',
            'cli_auth_credentials_store="file"',
        ),
    )
    async with AsyncCodex(config=config) as codex:
        if args.login:
            login = await codex.login_chatgpt_device_code()
            print(login.verification_url, login.user_code, flush=True)
            if not (await login.wait()).success:
                raise RuntimeError("统一登录失败")
            print("统一登录成功。")
            return
        broker = Broker(codex, args.home)
        args.socket.unlink(missing_ok=True)
        server = await asyncio.start_unix_server(
            broker.serve, path=args.socket, limit=4096
        )
        args.socket.chmod(0o660)
        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            asyncio.get_running_loop().add_signal_handler(sig, stop.set)
        maintenance = asyncio.create_task(broker.maintain())
        try:
            await stop.wait()
        finally:
            server.close()
            await server.wait_closed()
            maintenance.cancel()
            await asyncio.gather(maintenance, return_exceptions=True)
            args.socket.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=Path, default=Path("/var/lib/codex-auth/codex"))
    parser.add_argument(
        "--socket", type=Path, default=Path("/run/codex-auth/auth.sock")
    )
    parser.add_argument("--login", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
