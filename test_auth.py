import asyncio
import base64
import json
import time
from pathlib import Path

import pytest

from qq_agent.auth import Broker
from qq_agent.auth_client import TokenClient


def write_token(home, number, expiry=None):
    body = {
        "exp": expiry or time.time() + 3600,
        "nonce": number,
        "https://api.openai.com/auth": {"chatgpt_plan_type": "plus"},
    }
    encoded = base64.urlsafe_b64encode(json.dumps(body).encode()).decode().rstrip("=")
    (home / "auth.json").write_text(
        json.dumps(
            {
                "tokens": {
                    "access_token": "fixture." + encoded + ".fixture",
                    "account_id": "account",
                    "refresh_token": "must-never-leave-broker",
                }
            }
        )
    )


class FakeCodex:
    def __init__(self, home):
        self.home = home
        self.calls = 0

    async def account(self, refresh_token=False):
        assert refresh_token
        self.calls += 1
        await asyncio.sleep(0.01)
        write_token(self.home, self.calls)


async def test_shared_refresh(tmp_path: Path):
    write_token(tmp_path, 0)
    codex = FakeCodex(tmp_path)
    broker = Broker(codex, tmp_path)
    first = await broker.get({})
    request = {"refresh": True, "generation": first["generation"], "account": "account"}
    results = await asyncio.gather(*(broker.get(request) for _ in range(8)))
    assert codex.calls == 1
    assert len({r["generation"] for r in results}) == 1
    assert "refresh_token" not in json.dumps(results)
    with pytest.raises(ValueError, match="mismatch"):
        await broker.get({"account": "other"})


async def test_socket_and_denial(tmp_path: Path):
    write_token(tmp_path, 0)
    broker = Broker(FakeCodex(tmp_path), tmp_path)
    path = tmp_path / "auth.sock"
    server = await asyncio.start_unix_server(broker.serve, path=path, limit=4096)
    try:
        client = TokenClient(path)
        first = await asyncio.to_thread(client.fetch)
        assert first["chatgptAccountId"] == "account"
        updated = await asyncio.to_thread(
            client.handle,
            "account/chatgptAuthTokens/refresh",
            {"previousAccountId": "account"},
        )
        assert first["accessToken"] != updated["accessToken"]
        assert client.handle("item/commandExecution/requestApproval", {}) == {
            "decision": "decline"
        }
        with pytest.raises(RuntimeError):
            await asyncio.to_thread(client.fetch, True, "other")
    finally:
        server.close()
        await server.wait_closed()


async def test_cancelled_refresh(tmp_path: Path):
    write_token(tmp_path, 0, time.time() - 1)
    codex = FakeCodex(tmp_path)
    broker = Broker(codex, tmp_path)
    task = asyncio.create_task(broker.get({}))
    await asyncio.sleep(0.001)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    result = await broker.get({})
    assert result["accessToken"]
    assert codex.calls == 1


def test_resource_move(tmp_path: Path):
    from types import SimpleNamespace
    from qq_agent.runtime import prepare_home

    state = tmp_path / "state"
    source = state / "codex/skills/custom/SKILL.md"
    source.parent.mkdir(parents=True)
    source.write_text("skill")
    settings = SimpleNamespace(state_dir=state, resources_dir=tmp_path / "resources")
    prepare_home(settings)
    prepare_home(settings)
    assert (state / "codex/skills").is_symlink()
    assert (settings.resources_dir / "skills/custom/SKILL.md").read_text() == "skill"
