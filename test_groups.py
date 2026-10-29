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
from qq_agent.faces import FACES, face_label, parse_faces, text_chunks
from qq_agent.messages import ImageTurn, parse_message
from qq_agent.onebot import OneBot
from qq_agent.storage import open_database
from test_agent import PNG, event, turn_done


@pytest.fixture
async def group_agent(tmp_path):
    settings = Settings(
        set(),
        {"10": GroupSettings()},
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
    await agent.close_timers()
    for task in [*agent.receivers.values(), *agent.controls, *agent.jobs.values()]:
        task.cancel()
    await asyncio.gather(
        *agent.receivers.values(),
        *agent.controls,
        *agent.jobs.values(),
        return_exceptions=True,
    )
    agent.db.close()


async def drain(env):
    window = env.agent.group_windows.get("group-10")
    if window:
        window.deadline = 0
        window.changed.set()
    with patch.object(env.agent.groups, "delay", return_value=0):
        await asyncio.gather(*env.agent.group_timers.values())


async def receive(env, incoming, dispatch=True):
    env.agent.receive(incoming)
    await asyncio.gather(*env.agent.receivers.values())
    await asyncio.gather(*env.agent.controls)
    if dispatch:
        await drain(env)


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
    assert parse_reply(payload, {"平静": None, "开心": PNG}, required) == (
        *result,
        None,
    )


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
        {"observation_interval_seconds": 0},
        {"observation_interval_seconds": True},
        {"observation_interval_seconds": 0.5},
        {"message_ttl_seconds": -1},
        {"conversation_gap_seconds": float("inf")},
        {"conversation_gap_seconds": float("nan")},
        {"message_ttl_seconds": 30},
        {"conversation_gap_seconds": 60},
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
            env.agent.db.execute("UPDATE group_state SET last_observation=9970")
        assert env.agent.groups.delay("group-10") == 30
        with env.agent.db:
            env.agent.db.execute("UPDATE group_state SET last_observation=9940")
        assert env.agent.groups.delay("group-10") == 0


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
        assert texts == []
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
    await receive(env, event(group=10, identifier=2, mention=False), dispatch=False)
    await env.agent.close_timers()
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
        2,
        1,
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
    assert texts == []


async def test_history_cleanup(group_agent):
    env = group_agent
    await execute(env, event(group=10, mention=False))
    await receive(env, event(group=10, identifier=2, mention=False), dispatch=False)
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
    await drain(env)
    first = env.agent.queue.get_nowait()
    records = json.loads(env.agent.groups.input(first)[0].text.split("\n", 1)[1])[
        "messages"
    ]
    assert [row["message_id"] for row in records] == ["1", "2", "3"]
    assert env.agent.queue.empty()
    assert env.agent.db.execute("SELECT count FROM group_state").fetchone() == (0,)
    env.agent.groups.status(first, "completed")
    env.agent.mark(first, "completed")
    await receive(env, event(group=10, identifier=4, mention=False))
    second = env.agent.queue.get_nowait()
    records = json.loads(env.agent.groups.input(second)[0].text.split("\n", 1)[1])[
        "messages"
    ]
    assert [row["message_id"] for row in records] == ["4"]


async def test_stop_arrival_boundary(group_agent):
    env = group_agent
    env.agent.receive(event(group=10, identifier=1))
    env.agent.receive(event(group=10, identifier=2, text="/stop"))
    env.agent.receive(event(group=10, identifier=3))
    await asyncio.gather(*env.agent.receivers.values())
    await asyncio.gather(*env.agent.controls)
    await drain(env)
    batches = env.agent.groups.pending("group-10")
    assert len(batches) == 1
    records = json.loads(env.agent.groups.input(batches[0])[0].text.split("\n", 1)[1])[
        "messages"
    ]
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


def test_debounce_deadlines():
    from qq_agent.agent import GroupWindow

    message = NS(direct=False, sender_id="1")
    window = GroupWindow(0, 5)
    window.observe(message, 1, (0, "1"), 1)
    assert window.deadline == 3
    window.observe(message, 2, (1, "2"), 2)
    assert window.deadline == 2.5
    window = GroupWindow(0, 5)
    for arrival in (1, 2, 3, 4, 5, 6, 7):
        window.observe(message, arrival, (arrival - 1, "1"), arrival)
    assert window.deadline == 8
    window.observe(message, 9, (7, "1"), 9)
    assert window.deadline == 8
    window = GroupWindow(0, 5)
    message.direct = True
    window.observe(message, 1, (0, "1"), 1)
    window.observe(message, 1.5, (1, "1"), 1.5)
    assert window.deadline == 2


async def test_idle_boundary(group_agent):
    env = group_agent
    with patch("qq_agent.groups.time.time", return_value=10000):
        await receive(
            env,
            event(group=10, identifier=1, mention=False, text="旧讨论"),
            dispatch=False,
        )
    with patch("qq_agent.groups.time.time", return_value=13600):
        await receive(env, event(group=10, identifier=2, mention=False, text="新话题"))
    batch = env.agent.queue.get_nowait()
    payload = env.agent.groups.input(batch)[0].text
    assert "新话题" in payload and "旧讨论" not in payload
    assert env.agent.db.execute(
        "SELECT status FROM messages WHERE id='99:group-10:1'"
    ).fetchone() == ("expired",)
    assert env.agent.db.execute("SELECT count(*) FROM group_messages").fetchone() == (
        2,
    )


async def test_timer_without_arrival(group_agent):
    env = group_agent
    await receive(env, event(group=10, mention=False), dispatch=False)
    assert env.agent.queue.empty()
    window = env.agent.group_windows["group-10"]
    window.deadline = 0
    window.changed.set()
    await asyncio.gather(*env.agent.group_timers.values())
    assert env.agent.queue.qsize() == 1
    batch = env.agent.queue.get_nowait()
    await env.agent.turns.execute(batch)
    env.agent.flush_group("group-10")
    await asyncio.gather(*env.agent.group_timers.values())
    assert env.agent.queue.empty()


async def test_middle_mention(group_agent):
    env = group_agent
    for identifier, mention in ((1, False), (2, True), (3, False)):
        await receive(
            env, event(group=10, identifier=identifier, mention=mention), dispatch=False
        )
    await drain(env)
    batch = env.agent.queue.get_nowait()
    assert batch.mentioned and batch.direct
    payload = json.loads(env.agent.groups.input(batch)[0].text.split("\n", 1)[1])
    assert [row["message_id"] for row in payload["messages"]] == ["1", "2", "3"]
    assert set(payload) == {"background", "messages"}
    env.scripts.append(['{"response":"收到","sticker":"平静"}'])
    await env.agent.turns.execute(batch)
    await receive(env, event(group=10, identifier=4, mention=False))
    batch = env.agent.queue.get_nowait()
    assert not batch.mentioned and not batch.direct
    payload = json.loads(env.agent.groups.input(batch)[0].text.split("\n", 1)[1])
    assert payload["background"] == []
    assert not {"1", "2", "3"} & {row["message_id"] for row in payload["messages"]}


async def test_backoff_results(group_agent):
    env = group_agent
    await receive(env, event(group=10, mention=False))
    batch = env.agent.queue.get_nowait()
    with patch("qq_agent.groups.time.time", return_value=10000):
        for count, delay in enumerate((0, 60, 120, 240, 300, 300), 1):
            env.agent.groups.result(batch, "completed", False)
            actual = env.agent.db.execute(
                "SELECT silences,backoff_until FROM group_state"
            ).fetchone()
            assert actual == (count, 10000 + delay if delay else 0)
        env.agent.groups.result(batch, "failed", False)
        assert env.agent.db.execute("SELECT silences FROM group_state").fetchone() == (
            6,
        )
        env.agent.groups.result(replace(batch, direct=True), "completed", False)
        assert env.agent.db.execute("SELECT silences FROM group_state").fetchone() == (
            6,
        )
        env.agent.groups.result(batch, "completed", True)
        assert env.agent.db.execute(
            "SELECT silences,backoff_until FROM group_state"
        ).fetchone() == (0, 0)


async def test_direct_bypasses_backoff(group_agent):
    env = group_agent
    await execute(env, event(group=10, mention=False))
    with env.agent.db:
        env.agent.db.execute("UPDATE group_state SET backoff_until=9999999999")
    await receive(env, event(group=10, identifier=2), dispatch=False)
    window = env.agent.group_windows["group-10"]
    window.deadline = 0
    window.changed.set()
    await asyncio.gather(*env.agent.group_timers.values())
    assert env.agent.queue.get_nowait().mentioned


async def test_candidate_expiry(group_agent):
    env = group_agent
    with patch("qq_agent.groups.time.time", return_value=10000):
        await receive(env, event(group=10, identifier=1, mention=False), dispatch=False)
        await receive(env, event(group=10, identifier=2), dispatch=False)
    with patch("qq_agent.groups.time.time", return_value=10120):
        candidates = env.agent.groups.candidates("group-10")
        assert [message.identifier for message in candidates] == ["99:group-10:2"]
        assert candidates[0].mentioned
    await drain(env)
    batch = env.agent.queue.get_nowait()
    assert batch.mentioned


@pytest.mark.parametrize("status", ["submitted", "completed", "unknown"])
async def test_history_once(group_agent, status):
    env = group_agent
    await receive(env, event(group=10, mention=False))
    first = env.agent.queue.get_nowait()
    original = env.agent.groups.input(first)[0].text
    env.agent.groups.status(first, status)
    await receive(env, event(group=10, identifier=2))
    batch = env.agent.queue.get_nowait()
    payload = json.loads(env.agent.groups.input(batch)[0].text.split("\n", 1)[1])
    assert payload["background"] == []
    assert [row["message_id"] for row in payload["messages"]] == ["2"]
    with env.agent.db:
        env.agent.db.execute("UPDATE group_messages SET body='{}'")
    assert env.agent.groups.input(first)[0].text == original


async def test_pending_expiry(group_agent):
    env = group_agent
    with patch("qq_agent.groups.time.time", return_value=10000):
        await receive(env, event(group=10, mention=False))
    batch = env.agent.queue.get_nowait()
    with patch("qq_agent.groups.time.time", return_value=10120):
        await env.agent.turns.execute(batch)
    env.thread.turn.assert_not_awaited()
    assert env.agent.db.execute("SELECT status FROM group_batches").fetchone() == (
        "expired",
    )


@pytest.mark.parametrize("frozen", [False, True])
async def test_low_after_expiry(group_agent, frozen):
    env = group_agent
    with patch("qq_agent.groups.time.time", return_value=10000):
        incoming = event(group=10, identifier=1, mention=False, text="")
        incoming["message"] = [{"type": "face", "data": {"id": "178"}}]
        await receive(env, incoming)
        await receive(
            env, event(group=10, identifier=2, mention=False), dispatch=frozen
        )
    with patch("qq_agent.groups.time.time", return_value=10120):
        if frozen:
            await env.agent.turns.execute(env.agent.queue.get_nowait())
        await drain(env)
        env.agent.flush_group("group-10")
        await drain(env)
        assert env.agent.queue.empty() and not env.agent.group_windows
        env.thread.turn.assert_not_awaited()
        assert env.agent.db.execute(
            "SELECT id FROM group_messages WHERE consumed=0 AND batch IS NULL"
        ).fetchall() == [("99:group-10:1",)]
        await receive(env, event(group=10, identifier=3, mention=False))
        batch = env.agent.queue.get_nowait()
        payload = json.loads(env.agent.groups.input(batch)[0].text.split("\n", 1)[1])
        assert payload["background"] == []
        assert [row["message_id"] for row in payload["messages"]] == ["1", "3"]


async def test_legacy_background(group_agent):
    env = group_agent
    await receive(env, event(group=10, mention=False))
    first = env.agent.queue.get_nowait()
    old = json.loads(env.agent.groups.input(first)[0].text.split("\n", 1)[1])
    env.agent.groups.status(first, "completed")
    await receive(env, event(group=10, identifier=2, reply="1", mention=False))
    batch = env.agent.queue.get_nowait()
    payload = json.loads(env.agent.groups.input(batch)[0].text.split("\n", 1)[1])
    quote = payload["messages"][0]["quote"]
    payload["messages"][0]["quote"] = {"message_id": "1"}
    payload["background"] = old["messages"]
    with env.agent.db:
        env.agent.db.execute(
            "UPDATE group_batches SET payload=? WHERE id=?",
            (json.dumps(payload), batch.batch),
        )
    actual = env.agent.groups.input(batch)[0].text
    payload = json.loads(actual.split("\n", 1)[1])
    assert payload["background"] == []
    assert [row["message_id"] for row in payload["messages"]] == ["2"]
    assert payload["messages"][0]["quote"] == quote
    assert env.agent.groups.input(batch)[0].text == actual
    assert env.agent.db.execute(
        "SELECT payload FROM group_batches WHERE id=?", (batch.batch,)
    ).fetchone() == (actual.split("\n", 1)[1],)


async def test_failed_no_retry(group_agent):
    from openai_codex.errors import JsonRpcError

    env = group_agent
    env.thread.turn.side_effect = JsonRpcError(-32602, "rejected")
    await execute(env, event(group=10, mention=False))
    env.agent.flush_group("group-10")
    await asyncio.gather(*env.agent.group_timers.values())
    assert env.agent.queue.empty()


async def test_restart_unprepared(group_agent):
    env = group_agent
    message = parse_message(event(group=10), env.agent.settings)
    env.agent.groups.record(message)
    env.agent.db.close()
    env.agent.db = open_database(env.agent.settings)
    env.agent.groups.db = env.agent.db
    await receive(env, event(group=10, identifier=2, mention=False))
    batch = env.agent.queue.get_nowait()
    assert batch.mentioned
    assert (
        len(
            json.loads(env.agent.groups.input(batch)[0].text.split("\n", 1)[1])[
                "messages"
            ]
        )
        == 2
    )


async def test_busy_candidates(group_agent):
    env = group_agent
    key = "group-10"
    busy = asyncio.create_task(asyncio.Event().wait())
    env.agent.jobs[key] = busy
    try:
        await receive(env, event(group=10, identifier=1, mention=False))
        assert env.agent.queue.empty()
        assert len(env.agent.groups.candidates(key)) == 1
        await receive(env, event(group=10, identifier=2))
        batch = env.agent.queue.get_nowait()
        assert batch.mentioned
        payload = json.loads(env.agent.groups.input(batch)[0].text.split("\n", 1)[1])
        assert payload["background"] == []
        assert [row["message_id"] for row in payload["messages"]] == ["2"]
        assert len(env.agent.groups.candidates(key)) == 1
    finally:
        busy.cancel()
        await asyncio.gather(busy, return_exceptions=True)
        env.agent.jobs.pop(key)


async def test_slow_preparation(group_agent):
    env = group_agent
    with patch("qq_agent.groups.time.time", return_value=10000):
        message = parse_message(event(group=10, mention=False), env.agent.settings)
        env.agent.groups.record(message)
    with patch("qq_agent.groups.time.time", return_value=10010):
        quoted = await env.agent.groups.prepare(message)
        env.agent.groups.observe(message, quoted)
        env.agent.observe_group(message)
        assert (
            env.agent.group_windows[message.key].deadline
            < asyncio.get_running_loop().time()
        )


async def test_cadence_upgrade(group_agent):
    import sqlite3

    env = group_agent
    with patch("qq_agent.groups.time.time", return_value=10000):
        await receive(env, event(group=10, identifier=1, mention=False))
        ordinary = env.agent.queue.get_nowait()
        await receive(env, event(group=10, identifier=2))
        direct = env.agent.queue.get_nowait()
        message = parse_message(event(group=10, identifier=3), env.agent.settings)
        env.agent.groups.record(message)
    await env.agent.close_timers()
    with env.agent.db:
        for table, columns in {
            "group_state": [
                "last_arrival",
                "segment",
                "last_observation",
                "silences",
                "backoff_until",
            ],
            "group_messages": ["message", "ready", "priority", "segment"],
            "group_batches": ["payload", "priority", "segment"],
        }.items():
            for name in columns:
                env.agent.db.execute(f"ALTER TABLE {table} DROP COLUMN {name}")
        env.agent.db.execute("PRAGMA user_version=2")
    env.agent.db.close()
    env.agent.db = open_database(env.agent.settings)
    env.agent.groups.db = env.agent.db
    with patch("qq_agent.groups.time.time", return_value=10010):
        assert {batch.batch for batch in env.agent.groups.pending("group-10")} == {
            ordinary.batch,
            direct.batch,
        }
    with patch("qq_agent.groups.time.time", return_value=10120):
        batches = env.agent.groups.pending("group-10")
        assert len(batches) == 1 and batches[0].batch == direct.batch
        assert batches[0].direct and batches[0].mentioned
        candidates = env.agent.groups.candidates("group-10")
        assert len(candidates) == 1 and candidates[0].mentioned
    assert env.agent.db.execute("PRAGMA user_version").fetchone() == (3,)
    with sqlite3.connect(
        env.agent.settings.state_dir / "sessions.pre-cadence-v3.sqlite"
    ) as backup:
        assert backup.execute("PRAGMA user_version").fetchone() == (2,)
        assert backup.execute("SELECT count(*) FROM group_messages").fetchone() == (3,)
    env.agent.db.close()
    env.agent.db = open_database(env.agent.settings)
    env.agent.groups.db = env.agent.db
    assert env.agent.db.execute("PRAGMA user_version").fetchone() == (3,)


@pytest.mark.parametrize(
    "field",
    ["request_message_count", "request_interval_minutes", "participation_ratio"],
)
def test_legacy_config(field, tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(f'[groups."10"]\n{field} = 60\n')
    with pytest.raises(ValueError, match="Migrate group settings"):
        Settings.load(path)


async def test_retry_admission(group_agent):
    from openai_codex.errors import JsonRpcError

    env = group_agent
    env.thread.turn.side_effect = JsonRpcError(-32602, "rejected")
    first = await execute(env, event(group=10, mention=False))
    await receive(env, event(group=10, identifier=2, mention=False))
    assert env.agent.queue.qsize() == 1
    assert env.agent.queue.get_nowait().batch == first.batch
    assert len(env.agent.groups.candidates("group-10")) == 1


async def test_priority_admission(group_agent):
    from openai_codex.errors import JsonRpcError

    env = group_agent
    env.thread.turn.side_effect = JsonRpcError(-32602, "rejected")
    first = await execute(env, event(group=10, mention=False))
    await receive(env, event(group=10, identifier=2))
    assert env.agent.queue.qsize() == 1
    assert env.agent.queue.get_nowait().mentioned
    assert any(
        batch.batch == first.batch for batch in env.agent.groups.pending("group-10")
    )


async def test_low_backlog(group_agent):
    env = group_agent
    for index in range(110):
        incoming = event(group=10, identifier=index, text="", mention=False)
        incoming["message"] = (
            [{"type": "image", "data": {"file": "same", "sub_type": 1}}]
            if index < 100
            else [{"type": "forward", "data": {"id": "x"}}]
        )
        await receive(env, incoming)
    assert env.agent.queue.empty() and not env.agent.group_windows
    assert env.agent.db.execute(
        "SELECT last_arrival,count FROM group_state"
    ).fetchone() == (0, 0)
    with env.agent.db:
        env.agent.db.execute("UPDATE group_messages SET created=created-7200")
    await receive(env, event(group=10, identifier=111, mention=False))
    batch = env.agent.queue.get_nowait()
    payload = json.loads(env.agent.groups.input(batch)[0].text.split("\n", 1)[1])
    assert len(payload["messages"]) == 111
    assert (
        len({row["content"][0]["sticker_id"] for row in payload["messages"][:100]}) == 1
    )
    env.bot.image.assert_not_called()
    await env.agent.turns.execute(batch)
    env.agent.flush_group("group-10")
    await drain(env)
    assert env.agent.queue.empty()
    await receive(env, event(group=10, identifier=112, mention=False))
    batch = env.agent.queue.get_nowait()
    payload = json.loads(env.agent.groups.input(batch)[0].text.split("\n", 1)[1])
    assert payload["background"] == []
    assert [row["message_id"] for row in payload["messages"]] == ["112"]


async def test_face_history(group_agent):
    env = group_agent
    for identifier, face in enumerate((178, "999999"), 1):
        incoming = event(group=10, identifier=identifier, text="", mention=False)
        incoming["message"] = [{"type": "face", "data": {"id": face}}]
        parsed = parse_message(incoming, env.agent.settings)
        assert parsed and not parsed.unsupported and parsed.category == "low"
        await receive(env, incoming)
    assert env.agent.queue.empty() and not env.agent.group_windows
    incoming = event(group=10, identifier=3, text="看看", reply="1", mention=False)
    incoming["message"].append({"type": "face", "data": {"id": "178"}})
    parsed = parse_message(incoming, env.agent.settings)
    assert parsed and parsed.category == "normal"
    await receive(env, incoming)
    batch = env.agent.queue.get_nowait()
    payload = json.loads(env.agent.groups.input(batch)[0].text.split("\n", 1)[1])
    assert payload["messages"][0]["content"] == ["[qqface:178:斜眼笑]"]
    assert payload["messages"][1]["content"] == ["[qqface:999999:未知表情]"]
    assert payload["messages"][2]["content"] == ["看看", "[qqface:178:斜眼笑]"]
    assert payload["messages"][2]["quote"] == {"message_id": "1"}
    assert env.agent.db.execute("SELECT count(*) FROM received_images").fetchone() == (
        0,
    )
    env.agent.groups.status(batch, "completed")
    incoming = event(group=10, identifier=4, text="/status")
    incoming["message"].append({"type": "face", "data": {"id": "178"}})
    parsed = parse_message(incoming, env.agent.settings)
    assert parsed and not env.agent.is_command(parsed)
    for invalid in (True, -1, "", "1.0", "1x", "1" * 11, None):
        incoming["message"][-1]["data"]["id"] = invalid
        assert parse_message(incoming, env.agent.settings) is None


async def test_face_delivery(group_agent):
    env = group_agent
    env.agent.runtime.stickers = {"静": None, "图": PNG}
    marker = "[qqface:178:斜眼笑]"
    for value in FACES:
        assert parse_faces(face_label(value)) == [("face", value)]
    for invalid in (
        "[qqface:999999:未知表情]",
        "[qqface:178:笑]",
        "[qqface:178]",
        "hello[qqface:178:斜眼笑",
        "[qqface:0178:斜眼笑]",
        "[qqface:x:笑]",
    ):
        with pytest.raises(ValueError):
            parse_reply(json.dumps({"response": invalid, "sticker": "图"}), {"图": PNG})
    with pytest.raises(ValueError, match="非空文本"):
        parse_reply(
            json.dumps({"response": marker, "sticker": "静"}), {"静": None}, True
        )
    text = "a" * 1499 + marker * 2 + "xyx /xyx [斜眼笑]"
    assert text_chunks(text) == [[("text", text[:1500])], [("text", text[1500:])]]
    env.scripts.append(
        [
            json.dumps({"response": "[qqface:178:错误]", "sticker": "图"}),
            json.dumps({"response": text, "sticker": "静", "quote": "1"}),
        ]
    )
    await execute(env, event(group=10))
    assert len(env.deliveries) == 2
    assert env.deliveries[0]["message"] == [
        {"type": "reply", "data": {"id": "1"}},
        {"type": "text", "data": {"text": "a" * 1499}},
        {"type": "face", "data": {"id": "178"}},
    ]
    assert env.deliveries[1]["message"] == [
        {"type": "face", "data": {"id": "178"}},
        {"type": "text", "data": {"text": "xyx /xyx [斜眼笑]"}},
    ]
    rows = env.agent.db.execute(
        "SELECT body,category FROM group_messages WHERE own=1 ORDER BY sequence"
    ).fetchall()
    assert all(category == "low" for _, category in rows)
    assert json.loads(rows[0][0])["content"] == ["a" * 1499, marker]
    assert json.loads(rows[1][0])["content"] == [marker, "xyx /xyx [斜眼笑]"]
    env.scripts.append(
        [json.dumps({"response": marker, "sticker": "静", "quote": "1"})]
    )
    await execute(env, event(group=10, identifier=2, mention=False))
    assert env.deliveries[-1]["message"] == [
        {"type": "reply", "data": {"id": "1"}},
        {"type": "face", "data": {"id": "178"}},
    ]
    message = parse_message(event(group=10), env.agent.settings)
    await env.bot.send(message, text=marker, system=True)
    assert env.deliveries[-1]["message"] == [{"type": "text", "data": {"text": marker}}]
    before = len(env.deliveries)
    with pytest.raises(ValueError):
        await env.bot.send(message, text="[qqface:178:错误]", image=PNG)
    assert len(env.deliveries) == before


async def test_media_identity(group_agent, monkeypatch):
    import time
    from qq_agent import groups

    env = group_agent
    monkeypatch.setenv("TZ", "UTC-8")
    time.tzset()
    try:
        ids = []
        parsed = None
        for index, subtype in enumerate((0, 1, 0)):
            incoming = event(group=10, identifier=index, mention=False)
            incoming["time"] = 100.123456
            incoming["message"] = [
                {
                    "type": "image",
                    "data": {
                        "file": f"source-{index}",
                        "file_unique": "A" * 32,
                        "sub_type": subtype,
                    },
                }
            ]
            parsed = parse_message(incoming, env.agent.settings)
            assert parsed is not None
            env.agent.groups.record(parsed)
            body = json.loads(
                env.agent.db.execute(
                    "SELECT body FROM group_messages WHERE id=?", (parsed.identifier,)
                ).fetchone()[0]
            )
            assert body["time"] == "1970-01-01T08:01:40+08:00"
            ids.append(body["content"][0]["sticker_id" if subtype else "image_id"])
        assert len(set(ids)) == 1
        assert env.agent.db.execute(
            "SELECT source FROM received_images"
        ).fetchone() == ("source-2",)
        assert parsed is not None
        parsed.key = "group-11"
        assert env.agent.groups.record_body(parsed)["content"][0]["image_id"] != ids[0]
        payload = {
            "background": [],
            "messages": [
                {
                    "message_id": "old",
                    "time": "2026-01-01T00:00:00.123+00:00",
                    "time_source": "event",
                    "quote": {
                        "message_id": "quoted",
                        "time": "2025-12-31T23:00:00+00:00",
                        "time_source": "event",
                    },
                },
                {"message_id": "unknown", "time": None, "time_source": "unknown"},
            ],
        }
        formatted = groups.Groups.format_history(payload)
        assert formatted["messages"][0]["time"] == "2026-01-01T08:00:00"
        assert formatted["messages"][0]["quote"]["time"] == "2026-01-01T07:00:00"
        assert formatted["messages"][1]["time"] is None
        assert "time_source" not in json.dumps(formatted)
        rules = await env.agent.runtime.developer_instructions(parsed)
        assert "群聊记录的 time 均为系统本地时间（UTC +0800），省略时区后缀。" in rules
    finally:
        monkeypatch.undo()
        time.tzset()


async def test_system_history(group_agent):
    env = group_agent
    await receive(env, event(group=10, text="/status"))
    assert env.agent.db.execute("SELECT count(*) FROM group_messages").fetchone() == (
        0,
    )
    assert env.agent.db.execute("SELECT count(*) FROM system_messages").fetchone() == (
        2,
    )
    assert env.agent.queue.empty()
    await receive(env, event(group=10, identifier=2, reply="1001", mention=False))
    batch = env.agent.queue.get_nowait()
    payload = json.loads(env.agent.groups.input(batch)[0].text.split("\n", 1)[1])
    assert payload["messages"][0]["quote"] == {
        "message_id": "1001",
        "content": ["<redacted system message>"],
    }
    assert batch.direct and not env.agent.groups.can_quote("group-10", "1001")
    assert not env.agent.groups.can_quote("group-11", "2")
    assert not env.agent.groups.can_quote("group-10", "%")


async def test_system_echo(group_agent):
    env = group_agent
    trigger = parse_message(event(group=10), env.agent.settings)
    assert trigger is not None
    for identifier, system in ((20, True), (21, False)):
        echo = event(
            group=10, user=99, identifier=identifier, text="echo", mention=False
        )
        echo["time"] = 100
        await receive(env, echo)
        assert env.agent.db.execute(
            "SELECT count(*) FROM group_messages"
        ).fetchone() == (0,)
        env.agent.groups.sent(
            trigger, {"message_id": identifier}, [("text", "reply")], system=system
        )
    rows = env.agent.db.execute("SELECT body,category FROM group_messages").fetchall()
    assert len(rows) == 1 and rows[0][1] == "low"
    assert json.loads(rows[0][0])["time_source"] == "event"
    assert env.agent.queue.empty()


async def test_quote_delivery(group_agent):
    env = group_agent
    env.agent.runtime.stickers = {"静": None, "图": PNG}
    env.scripts.append(
        [json.dumps({"response": "a" * 1600, "sticker": "图", "quote": "1"})]
    )
    await execute(env, event(group=10))
    assert [part["type"] for part in env.deliveries[0]["message"]] == ["image"]
    assert env.deliveries[1]["message"][0] == {"type": "reply", "data": {"id": "1"}}
    assert [part["type"] for part in env.deliveries[2]["message"]] == ["text"]
    env.scripts.append(['{"response":null,"sticker":"图","quote":"1"}'])
    await execute(env, event(group=10, identifier=2, mention=False))
    assert env.deliveries[-1]["message"][0] == {"type": "reply", "data": {"id": "1"}}
    before = len(env.deliveries)
    env.scripts.append(
        ['{"response":"bad","sticker":"图","quote":"missing"}', '{"response":null}']
    )
    await execute(env, event(group=10, identifier=3, mention=False))
    assert len(env.deliveries) == before


@pytest.mark.parametrize("sticker", [None, PNG])
async def test_quote_sticker_intent(group_agent, sticker):
    env = group_agent
    env.agent.runtime.stickers = {"表情": sticker}
    reply = '{"response":null,"sticker":"表情","quote":"1"}'
    assert parse_reply(reply, env.agent.runtime.stickers) == ("", sticker, "1")
    env.scripts.append([reply])
    await execute(env, event(group=10, mention=False))
    assert len(env.deliveries) == int(sticker is not None)
    assert env.thread.turn.call_count == 1
    if sticker is not None:
        assert env.deliveries[0]["message"][0] == {"type": "reply", "data": {"id": "1"}}


async def test_quote_batch(group_agent):
    env = group_agent
    await receive(env, event(group=10, mention=False), dispatch=False)
    await receive(env, event(group=10, identifier=2, reply="1", mention=False))
    batch = env.agent.queue.get_nowait()
    payload = json.loads(env.agent.groups.input(batch)[0].text.split("\n", 1)[1])
    assert payload["messages"][-1]["quote"] == {"message_id": "1"}
    stored = env.agent.db.execute(
        "SELECT body FROM group_messages WHERE id='99:group-10:2'"
    ).fetchone()[0]
    assert json.loads(stored)["quote"]["sender_id"] == "1"
    env.agent.groups.status(batch, "completed")
    with env.agent.db:
        env.agent.db.execute("UPDATE group_messages SET created=created-3600")
    await receive(env, event(group=10, identifier=3, reply="1", mention=False))
    batch = env.agent.queue.get_nowait()
    payload = json.loads(env.agent.groups.input(batch)[0].text.split("\n", 1)[1])
    assert payload["messages"][0]["quote"]["sender_id"] == "1"


async def test_low_retry(group_agent):
    from openai_codex.errors import JsonRpcError

    env = group_agent
    env.thread.turn.side_effect = JsonRpcError(-32602, "rejected")
    await execute(env, event(group=10, mention=False))
    incoming = event(group=10, identifier=2, mention=False, text="")
    incoming["message"].append(
        {"type": "image", "data": {"file": "same", "sub_type": 1}}
    )
    await receive(env, incoming)
    assert env.agent.queue.empty() and not env.agent.group_windows
    assert env.agent.db.execute(
        "SELECT status FROM messages WHERE id LIKE '%batch-%'"
    ).fetchone() == ("failed",)
    incoming["message_id"] = 3
    incoming["message"].append({"type": "at", "data": {"qq": "99"}})
    await receive(env, incoming)
    assert not env.agent.queue.empty()


@pytest.mark.parametrize("quote", [1, "", " ", [], {}, "a" * 129])
def test_quote_invalid(quote):
    with pytest.raises(ValueError):
        parse_reply(
            json.dumps({"response": "hi", "sticker": "静", "quote": quote}),
            {"静": None},
        )
    with pytest.raises(ValueError):
        parse_reply('{"response":null,"quote":"1"}', {})


async def test_low_during_window(group_agent):
    env = group_agent
    await receive(env, event(group=10, mention=False), dispatch=False)
    entered, release = asyncio.Event(), asyncio.Event()
    prepare = env.agent.groups.prepare

    async def blocked(message):
        entered.set()
        await release.wait()
        return await prepare(message)

    incoming = event(group=10, identifier=2, mention=False, text="")
    incoming["message"] = [{"type": "forward", "data": {"id": "x"}}]
    with patch.object(env.agent.groups, "prepare", side_effect=blocked):
        env.agent.receive(incoming)
        await entered.wait()
        await drain(env)
        assert env.agent.queue.empty()
        release.set()
        await asyncio.gather(*env.agent.receivers.values())
    await drain(env)
    batch = env.agent.queue.get_nowait()
    history = json.loads(env.agent.groups.input(batch)[0].text.split("\n", 1)[1])
    assert [row["message_id"] for row in history["messages"]] == ["1", "2"]
