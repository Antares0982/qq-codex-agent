import asyncio
import base64
import io
import json
from types import SimpleNamespace as NS
from unittest.mock import patch

import pytest
from aiohttp import web
from PIL import Image

from image_assets import save_image
from qq_agent.config import Settings
from qq_agent.images import image_preview
from qq_agent.logging import LOG
from qq_agent.messages import ImageTurn, parse_message
from qq_agent.moderation import Moderation
from qq_agent.storage import open_database
from test_agent import PNG, event
from test_groups import execute, group_agent, receive


def picture(color, size=(32, 32)):
    stream = io.BytesIO()
    Image.new("RGB", size, color).save(stream, format="PNG")
    return stream.getvalue()


def incoming(*sources, identifier=1, subtype=0):
    value = event(group=10, text="caption", identifier=identifier, mention=False)
    value["message"] += [
        {"type": "image", "data": {"file": source, "sub_type": subtype}}
        for source in sources
    ]
    return value


@pytest.fixture
async def moderated(group_agent):
    env = group_agent
    env.requests = []
    env.denied = set()
    env.status = 200
    env.response = None
    env.entered = asyncio.Event()
    env.release = None

    async def handler(request):
        body = await request.json()
        assert set(body) == {"image"}
        assert int(request.headers["Content-Length"]) > 0
        env.requests.append(body["image"])
        env.entered.set()
        if env.release is not None:
            await env.release.wait()
        if env.response is not None:
            return web.Response(body=env.response, status=env.status)
        flagged = body["image"].split(",", 1)[1] in env.denied
        return web.json_response(
            {
                "flagged": flagged,
                "categories": {
                    "self-harm": False,
                    "sexual": flagged,
                    "violence": False,
                },
            },
            status=env.status,
        )

    app = web.Application()
    app.router.add_post("/{path}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    url = f"http://127.0.0.1:{runner.addresses[0][1]}/moderate"
    env.agent.settings.moderation_url = url
    env.agent.groups.moderation.url = url
    try:
        yield env
    finally:
        if env.release is not None:
            env.release.set()
        await env.agent.close_timers()
        await runner.cleanup()


def indexed(env, value):
    message = parse_message(value, env.agent.settings)
    assert message is not None
    with env.agent.db:
        body = env.agent.groups.record_body(message)
    ids = [
        part["image_id"]
        for part in body["content"]
        if isinstance(part, dict) and "image_id" in part
    ]
    return message, ids


async def test_short_circuit(moderated, caplog):
    caplog.set_level("INFO", logger=LOG.name)
    env = moderated
    data = {
        name: picture(color) for name, color in zip("abc", ("green", "red", "blue"))
    }
    env.bot.image.side_effect = data.__getitem__
    env.denied.add(image_preview(data["b"])[0]["data"])
    message, ids = indexed(env, incoming("a", "b", "c"))
    folder = env.agent.settings.workspace_dir / "old"
    folder.mkdir()
    paths = [save_image(folder, data[name]) for name in "abc"]
    with env.agent.db:
        env.agent.db.executemany(
            "UPDATE received_images SET folder='old',path=? WHERE id=?", zip(paths, ids)
        )
    await receive(env, incoming("a", "b", "c"))
    assert len(env.requests) == 2
    env.bot.image.assert_not_awaited()
    assert not list(folder.rglob("*.png"))
    assert all(env.agent.groups.moderation.blocked(image_id) for image_id in ids)
    assert not env.agent.db.execute("SELECT * FROM group_messages").fetchall()
    assert env.agent.queue.empty()
    assert env.agent.db.execute(
        "SELECT total,count,last_arrival FROM group_state"
    ).fetchone() == (0, 0, 0)
    await receive(env, incoming("c", identifier=2))
    await receive(env, incoming("c", identifier=2))
    assert len(env.requests) == 2
    reactions = [
        call.args[1]
        for call in env.bot.call.call_args_list
        if call.args[0] == "set_msg_emoji_like"
    ]
    assert reactions == [
        {"message_id": "1", "emoji_id": "479", "set": True},
        {"message_id": "2", "emoji_id": "479", "set": True},
    ]
    await receive(env, event(group=10, identifier=3, reply="1", mention=False))
    batch = env.agent.queue.get_nowait()
    payload = json.loads(env.agent.groups.input(batch)[0].text.split("\n", 1)[1])
    quote = payload["messages"][0]["quote"]
    assert set(quote) == {"message_id", "content"}
    assert not env.agent.groups.can_quote(message.key, "1")
    digest = env.agent.db.execute(
        "SELECT digest FROM moderation_results WHERE flagged=1"
    ).fetchone()[0]
    assert [
        record.message
        for record in caplog.records
        if record.message.startswith("Moderation flagged ")
    ] == [f"Moderation flagged digest={digest} categories=sexual"]


async def test_previews_and_cache(moderated, caplog):
    caplog.set_level("INFO", logger=LOG.name)
    env = moderated
    data = picture("purple", (1500, 1000))
    env.bot.image.return_value = data
    await execute(env, incoming("a", "b"))
    assert len(env.requests) == 1
    preview = image_preview(data)[0]
    assert env.requests[0] == f"data:{preview['mimeType']};base64,{preview['data']}"
    encoded = base64.b64decode(preview["data"])
    assert len(encoded) <= 200000 and encoded != data
    with Image.open(io.BytesIO(encoded)) as image:
        assert image.width * image.height <= 640000
    image_id = env.agent.db.execute(
        "SELECT id FROM received_images LIMIT 1"
    ).fetchone()[0]
    db = open_database(env.agent.settings)
    try:
        reopened = Moderation(env.agent.settings, db, env.bot)
        assert await reopened.inspect(image_id)
        assert len(env.requests) == 1
        reopened.url = reopened.url.replace("/moderate", "/changed")
        assert await reopened.inspect(image_id)
        assert len(env.requests) == 2
    finally:
        db.close()
    assert "Moderation flagged" not in caplog.text
    assert "Received chat=" not in caplog.text
    assert "caption" not in caplog.text


@pytest.mark.parametrize(
    "status,response",
    [
        (502, None),
        (504, None),
        (200, b"{}"),
        (200, b"bad json"),
        (
            200,
            b'{"flagged":false,"categories":{"sexual":true,"self-harm":false,"violence":false}}',
        ),
    ],
)
async def test_failures(moderated, status, response):
    env = moderated
    env.status, env.response = status, response
    await receive(env, incoming("a"))
    assert env.agent.queue.empty()
    assert not env.agent.db.execute("SELECT * FROM group_messages").fetchall()
    assert not env.agent.db.execute("SELECT * FROM moderation_results").fetchall()
    assert not env.agent.db.execute(
        "SELECT * FROM moderation_images WHERE blocked=1"
    ).fetchall()
    assert not any(
        call.args[0] == "set_msg_emoji_like" for call in env.bot.call.call_args_list
    )
    assert not list(env.agent.settings.workspace_dir.rglob("artifacts/*"))
    await asyncio.sleep(0)
    assert len(env.requests) == 1
    env.status, env.response = 200, None
    await execute(env, incoming("a", identifier=2))
    assert len(env.requests) == 2


async def test_frozen_taint(moderated):
    env = moderated
    data = {"a": picture("green"), "b": picture("red")}
    env.bot.image.side_effect = data.__getitem__
    env.denied.add(image_preview(data["b"])[0]["data"])
    await receive(env, incoming("a"))
    batch = env.agent.queue.get_nowait()
    await receive(env, incoming("a", "b", identifier=2))
    await env.agent.turns.execute(batch)
    env.thread.turn.assert_not_awaited()
    assert not env.agent.db.execute("SELECT * FROM group_messages").fetchall()
    payload = env.agent.db.execute("SELECT payload FROM group_batches").fetchone()[0]
    assert not json.loads(payload)["messages"]


async def test_exemptions(moderated):
    env = moderated
    env.denied.add(image_preview(PNG)[0]["data"])
    await receive(env, incoming("shared"))
    await execute(env, incoming("shared", identifier=2, subtype=1))
    image_id = env.agent.db.execute(
        "SELECT id FROM received_images WHERE kind='sticker'"
    ).fetchone()[0]
    message = parse_message(event(group=10), env.agent.settings)
    folder = (
        env.agent.settings.workspace_dir
        / env.agent.db.execute(
            "SELECT folder FROM sessions WHERE key='group-10'"
        ).fetchone()[0]
    )
    context = ImageTurn(message, folder)
    env.agent.turns.contexts[message.key] = context
    result = await env.agent.turns.images.view_image(
        context, {"action": "view_image", "image_id": image_id, "token": context.token}
    )
    assert result["ok"]
    env.agent.turns.images.save_generated(
        folder, NS(id="generated", result=base64.b64encode(PNG).decode())
    )
    await env.agent.turns.images.deliver_image(context, {"action": "send_image"})
    outgoing = env.agent.db.execute(
        "SELECT id FROM received_images WHERE kind='outgoing'"
    ).fetchone()[0]
    result = await env.agent.turns.images.view_image(
        context, {"action": "view_image", "image_id": outgoing, "token": context.token}
    )
    assert result["ok"] and len(env.requests) == 1


async def test_shared_request(moderated):
    env = moderated
    env.release = asyncio.Event()
    _, ids = indexed(env, incoming("a", "b"))
    first, second = [
        asyncio.create_task(env.agent.groups.moderation.inspect(image_id))
        for image_id in ids
    ]
    await env.entered.wait()
    first.cancel()
    await asyncio.gather(first, return_exceptions=True)
    env.release.set()
    assert await second
    assert len(env.requests) == 1
    assert not list(env.agent.settings.workspace_dir.rglob("artifacts/*"))


async def test_cold_rejection(moderated):
    env = moderated
    env.denied.add(image_preview(PNG)[0]["data"])
    await receive(env, incoming("a", "b"))
    env.bot.image.assert_awaited_once_with("a")
    assert len(env.requests) == 1 and env.agent.queue.empty()
    assert not list(env.agent.settings.workspace_dir.rglob("artifacts/*"))
    ids = [row[0] for row in env.agent.db.execute("SELECT id FROM received_images")]
    db = open_database(env.agent.settings)
    try:
        reopened = Moderation(env.agent.settings, db, env.bot)
        for image_id in ids:
            assert not await reopened.inspect(image_id)
        assert len(env.requests) == 1
    finally:
        db.close()


async def test_old_copies(moderated):
    env = moderated
    original = picture("red", (1500, 1000))
    preview = image_preview(original)[0]
    folder = env.agent.settings.workspace_dir / "other-group"
    folder.mkdir()
    paths = [
        save_image(folder, content)
        for content in (original, base64.b64decode(preview["data"]))
    ]
    with env.agent.db:
        env.agent.db.executemany(
            "INSERT INTO received_images (id,key,folder,path,created,kind) VALUES (?,'group-11','other-group',?,0,'image')",
            zip(("old-original", "old-preview"), paths),
        )
    env.bot.image.return_value = original
    env.denied.add(preview["data"])
    await receive(env, incoming("new-id"))
    assert not any((folder / path).exists() for path in paths)
    assert env.agent.groups.moderation.blocked("old-original")
    assert env.agent.groups.moderation.blocked("old-preview")


async def test_old_tool_rejection(moderated):
    env = moderated
    message = parse_message(incoming("a", "b"), env.agent.settings)
    assert message is not None
    env.agent.groups.record(message)
    folder = env.agent.settings.workspace_dir / "old"
    folder.mkdir()
    ids = [row[0] for row in env.agent.db.execute("SELECT id FROM received_images")]
    paths = [save_image(folder, PNG) for _ in ids]
    with env.agent.db:
        env.agent.db.executemany(
            "UPDATE received_images SET folder='old',path=? WHERE id=?", zip(paths, ids)
        )
    context = ImageTurn(message, folder)
    env.agent.turns.contexts[message.key] = context
    env.denied.add(image_preview(PNG)[0]["data"])
    with pytest.raises(ValueError):
        await env.agent.turns.images.read_received(context, ids[0], context.token)
    assert not list(folder.rglob("*.png"))
    assert not env.agent.db.execute("SELECT * FROM group_messages").fetchall()
    assert not env.agent.db.execute(
        "SELECT path FROM received_images WHERE path IS NOT NULL"
    ).fetchall()


async def test_finished_tool(moderated):
    env = moderated
    message, ids = indexed(env, incoming("a"))
    folder = env.agent.settings.workspace_dir / "current"
    folder.mkdir()
    context = ImageTurn(message, folder)
    env.agent.turns.contexts[message.key] = context
    env.release = asyncio.Event()
    task = asyncio.create_task(
        env.agent.turns.images.read_received(context, ids[0], context.token)
    )
    await env.entered.wait()
    context.token = "new-token"
    env.release.set()
    with pytest.raises(ValueError):
        await task
    assert not list(folder.rglob("artifacts/*"))


async def test_failed_quote_retry(moderated):
    env = moderated
    with patch.object(
        env.agent.groups.moderation, "classify", side_effect=TimeoutError
    ):
        await receive(env, incoming("a"))
    original = env.bot.call.side_effect

    async def call(action, params):
        if action == "get_msg":
            return {**incoming("a"), "sender": {"user_id": 1, "nickname": "friend"}}
        return await original(action, params)

    env.bot.call.side_effect = call
    await execute(env, event(group=10, identifier=2, reply="1", mention=False))
    assert len(env.requests) == 1
    assert env.agent.groups.can_quote("group-10", "1")
    assert not env.agent.db.execute(
        "SELECT * FROM moderation_messages WHERE flagged=0"
    ).fetchall()


async def test_disabled_moderation(moderated):
    env = moderated
    env.agent.settings.moderation_url = None
    env.agent.groups.moderation.url = None
    env.denied.add(image_preview(PNG)[0]["data"])
    await execute(env, incoming("a"))
    assert not env.requests
    assert list(env.agent.settings.workspace_dir.rglob("artifacts/*.png"))


@pytest.mark.parametrize(
    "url",
    [
        "",
        "ftp://example.com",
        "http://user:pass@localhost/moderate",
        "http://localhost:bad/moderate",
        "http://localhost/moderate?q=1",
        "http://localhost/moderate#fragment",
        "http://localhost/\x00",
        123,
    ],
)
def test_endpoint_config(tmp_path, url):
    path = tmp_path / "config.toml"
    path.write_text(
        f'napcat_url="ws://127.0.0.1:3001"\ntoken_file="/tmp/token"\nstate_dir="/tmp/state"\nworkspace_dir="/tmp/work"\nmoderation_url={json.dumps(url)}\n'
    )
    with pytest.raises(ValueError):
        Settings.load(path)
