import asyncio
import json
from collections import deque
from dataclasses import replace
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import pytest

import image_tool
from qq_agent.agent import Agent
from qq_agent.config import GroupSettings, Settings
from qq_agent.group_turns import GroupTurn, parse_reply
from qq_agent.messages import ImageTurn, parse_message
from qq_agent.onebot import OneBot
from qq_agent.storage import open_database
from test_agent import PNG, event, turn_done


@pytest.fixture
async def group_agent(tmp_path):
    settings = Settings(
        set(),
        {"10": GroupSettings(request_message_count=3)},
        "ws://127.0.0.1:3001",
        tmp_path / "token",
        tmp_path / "state",
        tmp_path / "work",
        tmp_path / "AGENTS.md",
    )
    bot = OneBot(settings)
    deliveries = []

    async def call(action, params):
        if action == "send_msg":
            deliveries.append(params)
            return {"message_id": 1000 + len(deliveries)}
        if action == "get_group_member_info":
            return {**params, "card": "成员" + str(params["user_id"]), "role": "member"}
        if action == "get_group_info":
            return {"member_count": 10}
        raise RuntimeError("Unavailable")

    bot.call = AsyncMock(side_effect=call)
    bot.image = AsyncMock(return_value=PNG)
    codex = NS(
        account=AsyncMock(return_value=NS(account=object())),
        _client=NS(
            request=AsyncMock(
                return_value=NS(
                    config=NS(sandbox_workspace_write=None), status="unsubscribed"
                )
            )
        ),
    )
    scripts = deque()
    turns = []

    async def start(inputs, **kwargs):
        script = scripts.popleft() if scripts else ['{"response":null}']

        async def stream():
            for index, value in enumerate(script):
                if callable(value):
                    await value()
                elif isinstance(value, BaseException):
                    raise value
                else:
                    yield NS(
                        method="item/completed",
                        payload=NS(
                            item=NS(
                                root=NS(
                                    type="agentMessage",
                                    id=str(index),
                                    text=value,
                                    phase="commentary",
                                )
                            )
                        ),
                    )
            yield turn_done()

        turn = NS(
            id=f"turn-{len(turns)}",
            stream=stream,
            steer=AsyncMock(),
            interrupt=AsyncMock(),
        )
        turns.append(turn)
        return turn

    thread = NS(id="test-thread", turn=AsyncMock(side_effect=start))
    codex.thread_start = AsyncMock(return_value=thread)
    codex.thread_resume = AsyncMock(return_value=thread)
    agent = Agent(settings, bot, codex)
    yield NS(
        agent=agent,
        bot=bot,
        codex=codex,
        thread=thread,
        scripts=scripts,
        turns=turns,
        deliveries=deliveries,
    )
    for task in [*agent.receivers.values(), *agent.controls, *agent.jobs.values()]:
        task.cancel()
    await asyncio.gather(
        *agent.receivers.values(),
        *agent.controls,
        *agent.jobs.values(),
        return_exceptions=True,
    )
    agent.db.close()


async def receive(env, incoming):
    env.agent.receive(incoming)
    await asyncio.gather(*env.agent.receivers.values())
    await asyncio.gather(*env.agent.controls)


async def execute(env, incoming):
    await receive(env, incoming)
    message = env.agent.queue.get_nowait()
    env.agent.queue.task_done()
    await env.agent.turns.execute(message)
    return message


@pytest.mark.parametrize(
    "payload,required,result",
    [
        ('{"response":null}', False, ("", None)),
        ('{"response":"  "}', False, ("", None)),
        ('{"response":null,"sticker":"平静"}', False, ("", None)),
        ('{"response":null,"sticker":"开心"}', False, ("", PNG)),
        ('{"response":"你好","sticker":"平静"}', True, ("你好", None)),
    ],
)
def test_reply_values(payload, required, result):
    assert parse_reply(payload, {"平静": None, "开心": PNG}, required) == result


@pytest.mark.parametrize(
    "payload,required",
    [
        ('{"response":null}', True),
        ('{"response":null,"sticker":"开心"}', True),
        ('{"response":"hi"}', False),
        ('{"response":1}', False),
        ('{"response":null,"sticker":null}', False),
        ('{"response":null,"sticker":"missing"}', False),
        ('{"response":null,"response":"hi"}', False),
        ('{"response":null,"extra":1}', False),
        ("{}", False),
        ("[]", False),
        ('```json\n{"response":null}\n```', False),
        ('{"response":null}\n{"response":null}', False),
        ("", False),
    ],
)
def test_reply_errors(payload, required):
    with pytest.raises(ValueError):
        parse_reply(payload, {"平静": None, "开心": PNG}, required)


@pytest.mark.parametrize(
    "options",
    [
        {"request_message_count": 0},
        {"request_message_count": True},
        {"request_interval_minutes": 0.5},
        {"request_interval_minutes": -1},
        {"participation_ratio": float("inf")},
        {"participation_ratio": float("nan")},
        {"participation_ratio": 0.9},
        {"participation_ratio": True},
    ],
)
def test_group_config(options):
    with pytest.raises(ValueError):
        GroupSettings(**options)


async def test_history_trigger(group_agent):
    env = group_agent
    await execute(env, event(group=10, user=123, mention=False))
    assert not env.deliveries
    for index in (2, 3):
        unreadable = event(group=10, user=234, identifier=index, mention=False)
        unreadable["message"] = [{"type": "forward", "data": {"id": "forward"}}]
        await receive(env, unreadable)
    assert env.agent.queue.empty()
    await receive(env, event(group=10, user=345, identifier=4, mention=False))
    message = env.agent.queue.get_nowait()
    history = env.agent.groups.input(message)[0].text
    assert "转发的消息" in history and "234" in history and "345" in history
    assert "image" not in history
    assert env.thread.turn.await_count == 1
    await receive(env, event(group=10, user=345, identifier=4, mention=False))
    assert env.agent.queue.empty()
    assert env.agent.db.execute("SELECT total,count FROM group_state").fetchone() == (
        4,
        0,
    )


async def test_time_trigger(group_agent):
    env = group_agent
    await execute(env, event(group=10, mention=False))
    with patch("qq_agent.groups.time.time", return_value=10000):
        with env.agent.db:
            env.agent.db.execute("UPDATE group_state SET last_request=6401")
        await receive(env, event(group=10, identifier=2, mention=False))
        assert env.agent.queue.empty()
        with env.agent.db:
            env.agent.db.execute("UPDATE group_state SET last_request=6400")
        await receive(env, event(group=10, identifier=3, mention=False))
        assert env.agent.queue.qsize() == 1


async def test_delivery_history(group_agent):
    env = group_agent
    env.scripts.append(['{"response":"你好","sticker":"开心"}'])
    message = await execute(env, event(group=10))
    assert [value["message"][0]["type"] for value in env.deliveries] == [
        "image",
        "text",
    ]
    assert env.agent.db.execute(
        "SELECT total,own,count FROM group_state"
    ).fetchone() == (3, 2, 0)
    assert env.agent.queue.empty()
    echo = event(group=10, user=99, identifier=1002, mention=False, text="你好")
    echo.update(post_type="message_sent", time=100)
    await receive(env, echo)
    assert env.agent.db.execute("SELECT total,own FROM group_state").fetchone() == (
        3,
        2,
    )
    await receive(env, event(group=10, identifier=2, mention=False, reply="1002"))
    batch = env.agent.queue.get_nowait()
    assert not batch.mentioned
    history = env.agent.groups.input(batch)[0].text
    assert "你好" in history and '"quote"' in history and "1970-01-01" in history
    assert message.batch != batch.batch


async def test_lazy_images(group_agent):
    env = group_agent
    incoming = event(group=10, text="看看", mention=False)
    incoming["message"] += [
        {"type": "image", "data": {"file": "remote"}},
        {"type": "at", "data": {"qq": "99"}},
        {"type": "json", "data": {}},
    ]
    await receive(env, incoming)
    batch = env.agent.queue.get_nowait()
    text = env.agent.groups.input(batch)[0].text
    assert "@(99)成员99" in text and "卡片" in text and '"image_id"' in text
    env.bot.image.assert_not_awaited()
    folder = env.agent.settings.workspace_dir / "images"
    folder.mkdir()
    context = ImageTurn(batch, folder)
    env.agent.turns.contexts[batch.key] = context
    index = env.agent.db.execute("SELECT id FROM received_images").fetchone()[0]
    request = {"action": "view_image", "image_id": index, "token": context.token}
    result = await env.agent.turns.images.view_image(context, request)
    assert (folder / result["path"]).read_bytes() == PNG
    assert result["image"]["type"] == "image"
    await env.agent.turns.images.view_image(context, request)
    env.bot.image.assert_awaited_once_with("remote")
    for request in ({**request, "token": "old"}, {**request, "image_id": "unknown"}):
        with pytest.raises(ValueError):
            await env.agent.turns.images.view_image(context, request)
    other = replace(batch, key="group-11", target={"group_id": 11})
    foreign = ImageTurn(other, folder)
    env.agent.turns.contexts[other.key] = foreign
    with pytest.raises(ValueError):
        await env.agent.turns.images.view_image(
            foreign, {"action": "view_image", "image_id": index, "token": foreign.token}
        )


async def test_image_socket(group_agent):
    env = group_agent
    message = parse_message(event(group=10), env.agent.settings)
    folder = env.agent.settings.workspace_dir / "socket"
    folder.mkdir()
    context = ImageTurn(message, folder)
    env.agent.turns.contexts[message.key] = context
    with env.agent.db:
        env.agent.db.execute(
            "INSERT INTO received_images (id,key,source,created) VALUES ('image','group-10','remote',strftime('%s','now'))"
        )
    socket_path = env.agent.settings.state_dir / "test.sock"
    server = await asyncio.start_unix_server(
        env.agent.tools.send_image, path=socket_path
    )
    try:
        result = await asyncio.to_thread(
            image_tool.call,
            str(socket_path),
            folder.name,
            "view_image",
            {"image_id": "image"},
            context.token,
        )
        assert result["content"][1]["type"] == "image"
        assert "image" not in result["structuredContent"]
        assert "data" not in result["content"][0]["text"]
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize("low", [False, True])
@pytest.mark.parametrize("mentioned", [False, True])
async def test_group_instructions(group_agent, low, mentioned):
    env = group_agent
    env.agent.settings.groups["10"].request_message_count = 1
    for identifier in (1, 2):
        await receive(env, event(group=10, identifier=identifier, mention=mentioned))
        message = env.agent.queue.get_nowait()
        env.agent.queue.task_done()
        with env.agent.db:
            env.agent.db.execute(
                "UPDATE group_state SET total=100,own=?", (0 if low else 50,)
            )
        env.scripts.append(['{"response":"收到","sticker":"平静"}'])
        env.codex._client.request.reset_mock()
        await env.agent.turns.execute(message)
        texts = [
            call.args[1]["items"][0]["content"][0]["text"]
            for call in env.codex._client.request.call_args_list
            if call.args[0] == "thread/inject_items"
        ]
        assert all("当前待回复要求" not in text for text in texts)
        if identifier == 1:
            assert texts.pop(0).startswith("以下是本轮最新应用提示词与记忆")
        assert texts == (
            ["你在群聊中的参与度偏低，可以在合适时自然参与。"] if low else []
        )
    system = env.codex.thread_start.call_args.kwargs["base_instructions"]
    assert "收到群成员直接 @ 你的新消息时" in system
    assert "response 必须提供非空文本" in system
    assert "base_instructions" not in env.codex.thread_resume.call_args.kwargs


async def test_required_repair(group_agent):
    env = group_agent
    env.scripts.extend(
        [
            ['{"response":null}'],
            ['{"response":"收到","sticker":"平静"}', '{"response":null}'],
        ]
    )
    message = await execute(env, event(group=10))
    assert [value["message"][0]["data"]["text"] for value in env.deliveries] == ["收到"]
    assert env.thread.turn.call_args_list[1].args[0] == []
    corrections = [
        call
        for call in env.codex._client.request.call_args_list
        if "上一条回复解析失败" in str(call)
    ]
    assert len(corrections) == 1
    assert corrections[0].args[1]["items"][0]["role"] == "developer"
    assert env.agent.db.execute(
        "SELECT status FROM group_batches WHERE id=?", (message.batch,)
    ).fetchone() == ("completed",)


async def test_reply_breaker(group_agent):
    env = group_agent
    env.scripts.extend([["invalid"], ["invalid"], ["invalid"]])
    await execute(env, event(group=10, mention=False))
    assert env.thread.turn.await_count == 3
    assert (
        len(env.deliveries) == 1
        and env.deliveries[0]["message"][0]["data"]["text"] == "agent 回复解析失败"
    )
    env.turns[-1].interrupt.assert_awaited_once()
    env.scripts.append(['{"response":"恢复","sticker":"平静"}'])
    await execute(env, event(group=10, identifier=2))
    assert env.deliveries[-1]["message"][0]["data"]["text"] == "恢复"


async def test_reply_reset(group_agent):
    env = group_agent
    env.scripts.append(["bad", '{"response":null}', "bad", '{"response":null}'])
    await execute(env, event(group=10, mention=False))
    assert env.thread.turn.await_count == 1
    assert not env.deliveries


async def test_empty_required(group_agent):
    env = group_agent
    env.scripts.extend([[], [], []])
    await execute(env, event(group=10))
    assert env.thread.turn.await_count == 3
    assert len(env.deliveries) == 1


async def test_partial_delivery(group_agent):
    env = group_agent
    original = env.bot.call.side_effect

    async def call(action, params):
        if action == "send_msg" and params["message"][0]["type"] == "text":
            raise TimeoutError
        return await original(action, params)

    env.bot.call.side_effect = call
    env.scripts.append(['{"response":"你好","sticker":"开心"}'])
    await execute(env, event(group=10))
    assert len(env.deliveries) == 1
    assert env.thread.turn.await_count == 1
    assert not any(
        "上一条回复解析失败" in str(call)
        for call in env.codex._client.request.call_args_list
    )


async def test_group_notice(group_agent):
    env = group_agent
    message = parse_message(event(group=10), env.agent.settings)
    run = GroupTurn(env.agent.turns, message)
    folder = env.agent.settings.workspace_dir / "notices"
    folder.mkdir()
    run.context = ImageTurn(message, folder)
    run.thread = env.thread
    calls = []
    waiting = asyncio.Event()

    async def remind(message, started):
        calls.append(started)
        await waiting.wait()

    with patch.object(env.agent.replies, "remind_group", side_effect=remind):
        await run.deliver(NS(type="agentMessage", text='{"response":null}', phase=None))
        assert run.reminder is None
        await run.deliver(
            NS(
                type="agentMessage",
                text='{"response":"hi","sticker":"平静"}',
                phase=None,
            )
        )
        await asyncio.sleep(0)
        first = run.reminder
        await run.deliver(NS(type="agentMessage", text='{"response":null}', phase=None))
        await asyncio.sleep(0)
        assert first is not None
        assert first.cancelled() and len(calls) == 2
        run.finished.set()
        await run.notice()
        assert run.reminder is None


async def test_restart_batches(group_agent):
    env = group_agent
    await receive(env, event(group=10, mention=False))
    message = env.agent.queue.get_nowait()
    env.agent.groups.status(message, "submitting")
    await receive(env, event(group=10, identifier=2, mention=False))
    env.agent.db.close()
    env.agent.db = open_database(env.agent.settings)
    env.agent.groups.db = env.agent.db
    assert env.agent.db.execute("SELECT status FROM group_batches").fetchone() == (
        "unknown",
    )
    assert env.agent.db.execute(
        "SELECT count(*) FROM group_messages WHERE consumed=0 AND batch IS NULL"
    ).fetchone() == (1,)
    assert env.agent.db.execute("SELECT total FROM group_state").fetchone() == (2,)
    env.agent.groups.cancel("group-10")
    assert not env.agent.db.execute(
        "SELECT 1 FROM group_messages WHERE consumed=0"
    ).fetchone()
    assert env.agent.db.execute("SELECT total FROM group_state").fetchone() == (2,)


async def test_required_version(group_agent):
    env = group_agent
    message = parse_message(event(group=10), env.agent.settings)
    run = GroupTurn(env.agent.turns, message)
    folder = env.agent.settings.workspace_dir / "revision"
    folder.mkdir()
    run.context = ImageTurn(message, folder)
    run.thread = env.thread
    run.required = True
    run.required_version = 2
    item = NS(
        type="agentMessage",
        text='{"response":"较早的回复","sticker":"平静"}',
        phase=None,
    )
    try:
        await run.deliver(item, version=1)
        assert run.required
        await run.deliver(item, version=2)
        assert not run.required
    finally:
        await run.notice()


async def test_reminder_timing(group_agent):
    from qq_agent import replies

    env = group_agent

    async def silent_wait():
        await asyncio.sleep(0.01)
        assert not env.deliveries

    async def first_wait():
        await asyncio.sleep(0.01)
        assert len(env.deliveries) == 3

    async def second_wait():
        await asyncio.sleep(0.01)
        assert len(env.deliveries) == 5

    env.scripts.append(
        [
            silent_wait,
            '{"response":"hello","sticker":"平静"}',
            first_wait,
            '{"response":null}',
            second_wait,
        ]
    )
    with patch.object(
        replies,
        "GROUP_REMINDERS",
        ((0, "thinking_30s.png"), (0, "thinking_too_long.png")),
    ):
        await execute(env, event(group=10))
    assert env.agent.db.execute("SELECT total,own FROM group_state").fetchone() == (
        6,
        5,
    )


@pytest.mark.parametrize("low", [False, True])
async def test_steer_rejection(group_agent, low):
    from openai_codex.errors import JsonRpcError

    env = group_agent
    await receive(env, event(group=10, identifier=1, mention=False))
    first = env.agent.queue.get_nowait()
    await receive(env, event(group=10, identifier=2))
    second = env.agent.queue.get_nowait()
    with env.agent.db:
        env.agent.db.execute(
            "UPDATE group_state SET total=100,own=?", (0 if low else 50,)
        )
    env.agent.pending[first.key] = deque([second])
    env.agent.wake[first.key] = asyncio.Event()
    folder = env.agent.settings.workspace_dir / "steer"
    folder.mkdir()
    run = GroupTurn(env.agent.turns, first)
    run.context = ImageTurn(first, folder)
    run.thread = env.thread
    run.turn = NS(steer=AsyncMock(side_effect=JsonRpcError(-32600, "no active turn")))
    async with asyncio.timeout(1) as deadline:
        before = deadline.when()
        await run.steer(deadline)
        assert before == deadline.when()
    assert env.agent.pending[first.key][0].batch == second.batch
    assert env.agent.db.execute(
        "SELECT status FROM group_batches WHERE id=?", (second.batch,)
    ).fetchone() == ("pending",)
    assert not run.required
    run.turn.steer.side_effect = TimeoutError()
    async with asyncio.timeout(1) as deadline:
        with pytest.raises(TimeoutError):
            await run.steer(deadline)
    assert not env.agent.pending[first.key]
    assert env.agent.db.execute(
        "SELECT status FROM group_batches WHERE id=?", (second.batch,)
    ).fetchone() == ("unknown",)
    assert second.batch not in [
        message.batch for message in env.agent.groups.pending(first.key)
    ]
    texts = [
        call.args[1]["items"][0]["content"][0]["text"]
        for call in env.codex._client.request.call_args_list
        if call.args[0] == "thread/inject_items"
    ]
    assert texts == ["你在群聊中的参与度偏低，可以在合适时自然参与。"] * (
        2 if low else 0
    )


async def test_history_cleanup(group_agent):
    env = group_agent
    await execute(env, event(group=10, mention=False))
    await receive(env, event(group=10, identifier=2, mention=False))
    with env.agent.db:
        env.agent.db.execute("UPDATE group_messages SET created=0")
        env.agent.db.execute("UPDATE group_batches SET created=0")
    env.agent.resetting.add("group-10")
    env.agent.cleanup.clean_files()
    assert env.agent.db.execute("SELECT count(*) FROM group_messages").fetchone() == (
        2,
    )
    env.agent.resetting.clear()
    env.agent.cleanup.clean_files()
    assert env.agent.db.execute("SELECT count(*) FROM group_messages").fetchone() == (
        1,
    )
    await receive(env, event(group=10, mention=False))
    assert env.agent.db.execute("SELECT total,count FROM group_state").fetchone() == (
        2,
        1,
    )


async def test_state_upgrade(group_agent):
    import sqlite3

    env = group_agent
    settings = replace(
        env.agent.settings, state_dir=env.agent.settings.state_dir / "legacy"
    )
    settings.state_dir.mkdir()
    path = settings.state_dir / "sessions.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript(
            "CREATE TABLE sessions (key TEXT PRIMARY KEY,thread TEXT,folder TEXT NOT NULL); INSERT INTO sessions VALUES ('group-10','group-old','group-folder'),('private-1','private-old','private-folder'); CREATE TABLE group_handoffs (group_id TEXT PRIMARY KEY,summary TEXT NOT NULL); INSERT INTO group_handoffs VALUES ('10','old summary');"
        )
    db = open_database(settings)
    try:
        assert db.execute("SELECT renew FROM sessions ORDER BY key").fetchall() == [
            (1,),
            (0,),
        ]
        assert not db.execute("SELECT * FROM group_handoffs").fetchall()
        with db:
            db.execute("UPDATE sessions SET renew=0")
    finally:
        db.close()
    db = open_database(settings)
    try:
        assert db.execute("SELECT renew FROM sessions ORDER BY key").fetchall() == [
            (0,),
            (0,),
        ]
    finally:
        db.close()
    with sqlite3.connect(settings.state_dir / "sessions.pre-group-v2.sqlite") as backup:
        assert backup.execute("SELECT summary FROM group_handoffs").fetchone() == (
            "old summary",
        )


async def test_arrival_persistence(group_agent):
    env = group_agent
    entered, release = asyncio.Event(), asyncio.Event()
    original = env.agent.groups.prepare

    async def prepare(message):
        entered.set()
        await release.wait()
        return await original(message)

    env.agent.groups.prepare = prepare
    env.agent.receive(event(group=10, identifier=1, mention=False))
    await entered.wait()
    env.agent.receive(event(group=10, identifier=2, mention=False))
    env.agent.receive(event(group=10, identifier=3, mention=False))
    assert env.agent.db.execute("SELECT count(*) FROM group_messages").fetchone() == (
        3,
    )
    release.set()
    await asyncio.gather(*env.agent.receivers.values())
    first = env.agent.queue.get_nowait()
    records = json.loads(env.agent.groups.input(first)[0].text.split("\n", 1)[1])
    assert [row["message_id"] for row in records] == ["1"]
    assert env.agent.queue.empty()
    assert env.agent.db.execute("SELECT count FROM group_state").fetchone() == (2,)
    await receive(env, event(group=10, identifier=4, mention=False))
    second = env.agent.queue.get_nowait()
    records = json.loads(env.agent.groups.input(second)[0].text.split("\n", 1)[1])
    assert [row["message_id"] for row in records] == ["2", "3", "4"]


async def test_stop_arrival_boundary(group_agent):
    env = group_agent
    env.agent.receive(event(group=10, identifier=1))
    env.agent.receive(event(group=10, identifier=2, text="/stop"))
    env.agent.receive(event(group=10, identifier=3))
    await asyncio.gather(*env.agent.receivers.values())
    await asyncio.gather(*env.agent.controls)
    batches = env.agent.groups.pending("group-10")
    assert len(batches) == 1
    records = json.loads(env.agent.groups.input(batches[0])[0].text.split("\n", 1)[1])
    assert [row["message_id"] for row in records] == ["3"]


async def test_start_rejection(group_agent):
    from openai_codex.errors import JsonRpcError

    env = group_agent
    env.thread.turn.side_effect = JsonRpcError(-32602, "rejected input")
    message = await execute(env, event(group=10, mention=False))
    assert [batch.batch for batch in env.agent.groups.pending(message.key)] == [
        message.batch
    ]
    assert not env.deliveries
