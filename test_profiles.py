import asyncio
import json
import sqlite3
from datetime import date, datetime, timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from openai_codex.errors import JsonRpcError
from openai_codex.generated.v2_all import MessagePhase

from qq_agent import agent as agent_module
from qq_agent.agent import Agent
from qq_agent.config import GroupSettings, Settings
from qq_agent.messages import parse_message
from qq_agent.profiles import ProfileSummary, parse_profile
from qq_agent.runtime import PROFILE_SCHEMA
from qq_agent.storage import open_database
from test_agent import event, turn_done
from test_groups import group_agent  # noqa: F401


@pytest.mark.parametrize(
    "value",
    [
        "",
        "[]",
        "{}",
        '{"valid":[],"new":[],"extra":0}',
        '{"valid":[],"valid":[],"new":["a","b","c"]}',
        *[
            json.dumps({"valid": indexes, "new": ["a", "b", "c"]})
            for indexes in ([0], [2], [True], [1, 1], {}, [[1]])
        ],
        *[
            json.dumps({"valid": [], "new": items})
            for items in (
                None,
                "abc",
                [1, "b", "c"],
                ["", "b", "c"],
                ["a", "a ", "b"],
                ["a\x00", "b", "c"],
                ["a" * 41, "b", "c"],
            )
        ],
    ],
)
def test_profile_validation(value):
    with pytest.raises(ValueError):
        parse_profile(value, ["old"], 40)


def test_profile_limits():
    assert parse_profile(
        json.dumps({"valid": [1], "new": [" 文 ", "字" * 40, "c", "c"]}), ["old"], 40
    ) == {
        "valid": [1],
        "new": sorted(["文", "字" * 40, "c"]),
    }
    for options in (
        {"profile_max_entries": True},
        {"profile_max_chars": 0},
        {"profile_max_entries": -1},
        {"profile_max_chars": 1.5},
        {"profile_summary_enabled": 1},
        {"profile_summary_time": 5},
        {"profile_summary_time": "5:00"},
        {"profile_summary_time": "24:00"},
        {"profile_summary_time": "05:60"},
        {"profile_summary_time": "05:00:00"},
    ):
        with pytest.raises(ValueError):
            GroupSettings(**json.loads(json.dumps(options)))
    now = datetime(2026, 1, 1, 5)
    assert Agent.next_profile(now, GroupSettings()) == now + timedelta(days=1)
    assert Agent.next_profile(now - timedelta(seconds=1), GroupSettings()) == now
    assert Agent.next_profile(
        now, GroupSettings(profile_summary_time="06:15")
    ) == now.replace(hour=6, minute=15)


def test_profile_config(tmp_path, caplog):
    path = tmp_path / "config.toml"
    base = 'napcat_url="ws://127.0.0.1:3001"\ntoken_file="/token"\nstate_dir="/state"\nworkspace_dir="/work"\n'
    path.write_text(
        base
        + 'agents_file="/etc/qq-codex-agent/AGENTS.md"\n[groups."10"]\nprofile_max_entries=5\nprofile_max_chars=20\nprofile_summary_time="06:30"\nprofile_summary_enabled=false\n'
    )
    settings = Settings.load(path)
    assert settings.groups["10"] == GroupSettings(
        profile_max_entries=5,
        profile_max_chars=20,
        profile_summary_time="06:30",
        profile_summary_enabled=False,
    )
    assert "Deprecated agents_file" in caplog.text
    path.write_text(base + 'group_agents_file="/custom.md"\n')
    with pytest.raises(ValueError, match="Migrate custom prompts"):
        Settings.load(path)


async def test_profile_storage(group_agent):
    agent = group_agent.agent
    profiles = agent.turns.profiles
    agent.settings.groups["10"].profile_max_entries = 4
    with agent.db:
        agent.db.executemany(
            "INSERT INTO group_profiles VALUES ('10',?,?)",
            [
                ("retained", "2026-01-01"),
                ("old", "2025-01-01"),
                ("refreshed", "2024-01-01"),
            ],
        )
        agent.db.execute(
            "INSERT INTO group_profiles VALUES ('11','elsewhere','2024-01-01')"
        )
    entries = profiles.entries("10")
    profiles.apply(
        "10",
        entries,
        parse_profile('{"valid":[2],"new":["a","b","refreshed"]}', entries, 40),
    )
    assert profiles.entries("10") == ["a", "b", "refreshed", "retained"]
    dates = dict(
        agent.db.execute("SELECT text,date FROM group_profiles WHERE group_id='10'")
    )
    assert dates["retained"] == "2026-01-01"
    assert dates["refreshed"] == dates["a"] == date.today().isoformat()
    assert profiles.entries("11") == ["elsewhere"]
    agent.settings.groups["10"].profile_max_entries = 2
    profiles.apply("10", profiles.entries("10"), {"valid": [], "new": ["a", "b", "z"]})
    assert profiles.entries("10") == ["refreshed", "z"]
    await agent.runtime.transcripts.close()


async def test_profile_migration(group_agent):
    env = group_agent
    settings = env.agent.settings
    env.agent.db.close()
    with sqlite3.connect(settings.state_dir / "sessions.sqlite") as db:
        db.executescript("""
            DROP TABLE group_profiles;
            CREATE TABLE group_profiles (group_id TEXT PRIMARY KEY,profile TEXT,updated_at REAL);
            INSERT INTO group_profiles VALUES ('10','old group',0);
            CREATE TABLE member_profiles (group_id TEXT,user_id TEXT,profile TEXT);
            INSERT INTO member_profiles VALUES ('10','1','old member');
            INSERT INTO group_prompts VALUES ('10','keep','1',0);
            INSERT INTO group_handoffs VALUES ('10','keep');
            PRAGMA user_version=4;
        """)
    env.agent.db = db = open_database(settings)
    assert db.execute("PRAGMA user_version").fetchone() == (5,)
    assert db.execute("SELECT * FROM group_profiles").fetchall() == []
    assert not db.execute(
        "SELECT name FROM sqlite_master WHERE name='member_profiles'"
    ).fetchone()
    assert db.execute("SELECT prompt FROM group_prompts").fetchone() == ("keep",)
    assert db.execute("SELECT summary FROM group_handoffs").fetchone() == ("keep",)
    backup = settings.state_dir / "sessions.pre-profiles-v5.sqlite"
    with sqlite3.connect(backup) as old:
        assert old.execute("SELECT profile FROM member_profiles").fetchone() == (
            "old member",
        )
    with db:
        db.execute("INSERT INTO group_profiles VALUES ('10','new','2026-01-01')")
    db.close()
    env.agent.db = open_database(settings)
    assert env.agent.db.execute("SELECT text FROM group_profiles").fetchone() == (
        "new",
    )
    with sqlite3.connect(backup) as old:
        assert old.execute("SELECT profile FROM group_profiles").fetchone() == (
            "old group",
        )


def summary_turn(text, action=None):
    async def stream():
        if action:
            await action()
        if text is not None:
            yield NS(
                method="item/completed",
                payload=NS(
                    item=NS(
                        root=NS(
                            type="agentMessage",
                            phase=MessagePhase.final_answer,
                            text=text,
                        )
                    )
                ),
            )
        yield turn_done()

    return NS(id="summary", stream=stream, interrupt=AsyncMock())


def summary_session(env):
    (env.agent.settings.workspace_dir / "folder").mkdir()
    with env.agent.db:
        env.agent.db.execute(
            "INSERT INTO sessions VALUES ('group-10','test-thread','folder',0)"
        )
        env.agent.db.execute(
            "INSERT INTO group_profiles VALUES ('10','old','2020-02-03')"
        )
    return ProfileSummary("group-10", datetime(2026, 1, 1, 5), datetime(2026, 1, 2, 5))


@pytest.mark.parametrize(
    "answers,success",
    [
        (['{"valid":[1],"new":["a","b","c"]}'], True),
        ([None, '{"valid":[],"new":[]}', '{"valid":[1],"new":["a","b","c"]}'], True),
        (["{}", "bad", '{"valid":[],"new":["a","b"]}'], False),
    ],
)
async def test_profile_summary(group_agent, answers, success):
    env = group_agent
    job = summary_session(env)
    env.thread.turn.side_effect = [summary_turn(answer) for answer in answers]
    await env.agent.summarize_profile(job)
    assert env.thread.turn.await_count == len(answers)
    assert env.codex.thread_start.await_count == 0
    env.codex.thread_resume.assert_awaited_once()
    assert env.thread.turn.call_args.kwargs["output_schema"] == PROFILE_SCHEMA
    initial = env.thread.turn.call_args_list[0].args[0]
    assert '"1": "old"' in initial
    assert "2020-02-03" not in initial
    assert "50" not in initial and "日期戳" not in initial and "淘汰" not in initial
    assert not env.agent.turns.contexts and not env.deliveries
    assert env.agent.turns.profiles.entries("10") == (
        ["a", "b", "c", "old"] if success else ["old"]
    )
    corrections = [
        call
        for call in env.codex._client.request.call_args_list
        if call.args[0] == "thread/inject_items" and "结果无效" in str(call.args[1])
    ]
    assert len(corrections) == len(answers) - 1


async def test_profile_snapshot(group_agent):
    env = group_agent
    job = summary_session(env)
    await env.agent.runtime.update_group(env.thread, job, env.agent.turns.profiles)
    initial = env.codex._client.request.await_count
    with env.agent.db:
        env.agent.db.execute("UPDATE group_profiles SET date='2026-01-01'")
    await env.agent.runtime.update_group(env.thread, job, env.agent.turns.profiles)
    assert env.codex._client.request.await_count == initial
    with env.agent.db:
        env.agent.db.executemany(
            "INSERT INTO group_profiles VALUES ('10',?,'2025-01-01')", [("z",), ("a",)]
        )
    await env.agent.runtime.update_group(env.thread, job, env.agent.turns.profiles)
    assert env.codex._client.request.await_count == initial + 1
    text = env.codex._client.request.call_args.args[1]["items"][0]["content"][0]["text"]
    assert json.loads(text.split("\n", 1)[1])["群共同记录"] == "a\nold\nz"


async def test_profile_queue(group_agent):
    env = group_agent
    job = summary_session(env)
    entered = asyncio.Event()
    release = asyncio.Event()
    order = []

    async def chat(message):
        order.append(message.text)
        if message.text == "first":
            entered.set()
            await release.wait()

    async def summary(*args):
        order.append("summary")
        assert order == ["first", "summary"]

    env.agent.turns.execute = AsyncMock(side_effect=chat)
    env.agent.runtime.summarize = AsyncMock(side_effect=summary)
    worker = asyncio.create_task(env.agent.work())
    first = parse_message(event(group=10, text="first"), env.agent.settings)
    last = parse_message(event(group=10, text="last", identifier=2), env.agent.settings)
    try:
        env.agent.queue.put_nowait(first)
        await asyncio.wait_for(entered.wait(), 2)
        assert env.agent.enqueue_profile("10", job.end)
        waiter = env.agent.profile_waiters[job.key]
        env.agent.queue.put_nowait(last)
        await env.agent.queue.join()
        assert order == ["first"]
        release.set()
        await asyncio.wait_for(waiter.wait(), 2)
        await asyncio.sleep(0)
        assert order == ["first", "summary", "last"]
        assert env.agent.enqueue_profile("10", job.end)
        assert env.agent.queue.empty()
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


async def test_profile_controls(group_agent):
    env = group_agent
    job = summary_session(env)
    env.agent.settings.group_prompt_allow_members = True
    entered = asyncio.Event()
    release = asyncio.Event()

    async def pause():
        entered.set()
        await release.wait()

    turn = summary_turn('{"valid":[1],"new":["a","b","c"]}', pause)
    env.thread.turn.side_effect = [turn]
    worker = asyncio.create_task(env.agent.work())
    try:
        env.agent.enqueue_profile("10", job.end)
        await asyncio.wait_for(entered.wait(), 2)
        command = parse_message(
            event(group=10, text="/group-profile forget"), env.agent.settings
        )
        control = asyncio.create_task(env.agent.control(command))
        await asyncio.sleep(0)
        assert not control.done()
        assert env.agent.turns.profiles.entries("10") == ["old"]
        release.set()
        await asyncio.wait_for(control, 2)
        assert env.agent.turns.profiles.entries("10") == []
        turn.interrupt.assert_not_awaited()
    finally:
        release.set()
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


async def test_profile_stop(group_agent):
    env = group_agent
    job = summary_session(env)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def pause():
        entered.set()
        await release.wait()

    turn = summary_turn(None, pause)
    turn.interrupt.side_effect = lambda: release.set()
    env.thread.turn.side_effect = [turn]
    worker = asyncio.create_task(env.agent.work())
    try:
        env.agent.enqueue_profile("10", job.end)
        await asyncio.wait_for(entered.wait(), 2)
        await env.agent.control(
            parse_message(event(group=10, text="/stop"), env.agent.settings)
        )
        turn.interrupt.assert_awaited_once()
        assert env.agent.turns.profiles.entries("10") == ["old"]
        assert not env.agent.profile_waiters and not env.agent.jobs
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


async def test_profile_clock(group_agent, monkeypatch):
    env = group_agent
    now = datetime(2026, 1, 1, 6)

    class Clock:
        @staticmethod
        def now():
            return now

    monkeypatch.setattr(agent_module, "datetime", Clock)
    # Keep the standard date constructor.
    monkeypatch.setattr(Clock, "combine", datetime.combine, raising=False)
    original_sleep = asyncio.sleep
    sleeping = asyncio.Event()
    resume = asyncio.Event()

    async def sleep(delay):
        assert 0 < delay <= 30
        sleeping.set()
        await resume.wait()
        resume.clear()

    monkeypatch.setattr(agent_module.asyncio, "sleep", sleep)
    timer = asyncio.create_task(env.agent.schedule_profiles())
    try:
        await asyncio.wait_for(sleeping.wait(), 2)
        assert env.agent.queue.empty()
        now = datetime(2026, 1, 2, 5)
        sleeping.clear()
        resume.set()
        await original_sleep(0)
        await asyncio.wait_for(sleeping.wait(), 2)
        job = env.agent.queue.get_nowait()
        env.agent.queue.task_done()
        assert job.start == datetime(2026, 1, 1, 5)
        assert job.end == now
        env.agent.mark(job, "completed")
    finally:
        timer.cancel()
        await asyncio.gather(timer, return_exceptions=True)


@pytest.mark.parametrize(
    "mode", ["no-thread", "renew", "logged-out", "timeout", "failed"]
)
async def test_profile_failures(group_agent, mode):
    env = group_agent
    job = summary_session(env)
    if mode == "no-thread":
        with env.agent.db:
            env.agent.db.execute("DELETE FROM sessions")
    elif mode == "renew":
        with env.agent.db:
            env.agent.db.execute("UPDATE sessions SET renew=1")
    elif mode == "logged-out":
        env.codex.account.return_value = NS(account=None)
    elif mode == "timeout":
        env.agent.settings.task_timeout = 0.02
        stop = asyncio.Event()
        turn = summary_turn(None, stop.wait)
        turn.interrupt.side_effect = lambda: stop.set()
        env.thread.turn.side_effect = [turn]
    else:
        from openai_codex.generated.v2_all import TurnStatus

        async def stream():
            yield turn_done(TurnStatus.failed)

        env.thread.turn.side_effect = [NS(id="failed", stream=stream)]
    await env.agent.summarize_profile(job)
    assert env.agent.turns.profiles.entries("10") == ["old"]
    assert env.thread.turn.await_count == int(mode in {"timeout", "failed"})
    assert not env.deliveries


async def test_profile_disabled(group_agent, monkeypatch):
    env = group_agent
    env.agent.settings.groups["10"].profile_summary_enabled = False

    async def stop(delay):
        env.agent.closing = True

    monkeypatch.setattr(agent_module.asyncio, "sleep", stop)
    await env.agent.schedule_profiles()
    assert env.agent.queue.empty()
    assert not env.agent.db.execute("SELECT * FROM group_profile_runs").fetchall()


async def test_profile_backup_failure(group_agent):
    env = group_agent
    settings = env.agent.settings
    with env.agent.db:
        env.agent.db.execute("PRAGMA user_version=4")
        env.agent.db.execute(
            "INSERT INTO group_profiles VALUES ('10','keep','2026-01-01')"
        )
    (settings.state_dir / "sessions.pre-profiles-v5.sqlite").write_bytes(b"corrupt")
    with pytest.raises(sqlite3.DatabaseError):
        open_database(settings)
    assert env.agent.turns.profiles.entries("10") == ["keep"]
    assert env.agent.db.execute("PRAGMA user_version").fetchone() == (4,)


async def test_profile_retry_rejected(group_agent):
    env = group_agent
    job = summary_session(env)
    previous = summary_turn("{}")
    env.thread.turn.side_effect = [previous, JsonRpcError(-32602, "rejected")]
    await env.agent.summarize_profile(job)
    assert env.thread.turn.await_count == 2
    previous.interrupt.assert_not_awaited()
    assert env.agent.turns.profiles.entries("10") == ["old"]
    assert not env.deliveries
