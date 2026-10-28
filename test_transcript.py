import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).parent
    / ".agents/skills/export-pi-transcript/scripts/export_transcript.py"
)
spec = importlib.util.spec_from_file_location("export_transcript", SCRIPT)
assert spec is not None and spec.loader is not None
copy_script = importlib.util.module_from_spec(spec)
spec.loader.exec_module(copy_script)

from qq_agent import transcript


def event(kind, payload, second):
    return {
        "timestamp": f"2026-10-25T00:00:{second:06.3f}Z",
        "type": kind,
        "payload": payload,
    }


def message(role, text):
    return {
        "type": "message",
        "role": role,
        "content": [{"type": "input_text", "text": text}],
    }


def setup_records(root, monkeypatch):
    monkeypatch.setattr(
        transcript.time, "time_ns", lambda: transcript.timestamp("2026-10-25T00:00:10Z")
    )
    with sqlite3.connect(root / "logs_2.sqlite") as db:
        db.execute(
            "CREATE TABLE logs (id INTEGER PRIMARY KEY, ts INTEGER, ts_nanos INTEGER, target TEXT, feedback_log_body TEXT, thread_id TEXT, process_uuid TEXT)"
        )
        stamp = transcript.timestamp("2026-10-25T00:00:00Z") // 1_000_000_000
        for identifier, second, thread, turn, compressed in (
            (1, 1, "a", "slow", True),
            (2, 2, "b", "latest", True),
            (3, 3, "b", "latest", False),
            (4, 4, "b", "latest", True),
            (5, 5, "b", "latest", False),
            (6, 6, "a", "slow", False),
        ):
            body = f"turn.id={turn} model=test " + (
                'api.path="/responses" Compressed request body pre_compression_bytes=100'
                if compressed
                else 'Request completed method=POST url=https://example.com/backend-api/codex/responses status=200 {"x-oai-request-id": "req-1"}'
            )
            db.execute(
                "INSERT INTO logs VALUES (?, ?, 0, ?, ?, ?, 'process')",
                (
                    identifier,
                    stamp + second,
                    (
                        "codex_http_client::request"
                        if compressed
                        else "codex_http_client::client"
                    ),
                    body,
                    thread,
                ),
            )
    rollout = root / "b.jsonl"
    with sqlite3.connect(root / "state_5.sqlite") as db:
        db.execute("CREATE TABLE threads (id TEXT, rollout_path TEXT)")
        db.execute("INSERT INTO threads VALUES ('b', ?)", (str(rollout),))
    records = [
        event("session_meta", {"base_instructions": {"text": "system instruction"}}, 0),
        event("turn_context", {"turn_id": "latest"}, 0.1),
        event("response_item", message("developer", "rules"), 0.2),
        event("response_item", message("user", "question"), 1),
        event(
            "response_item",
            {
                "type": "function_call",
                "name": "shell",
                "call_id": "call",
                "arguments": "{}",
            },
            2.1,
        ),
        event(
            "response_item",
            {"type": "function_call_output", "call_id": "call", "output": "answer"},
            3.1,
        ),
        event("response_item", message("user", "same millisecond"), 4),
        event("response_item", message("assistant", "final reply"), 4.5),
        event("turn_context", {"turn_id": "other"}, 5),
        event("response_item", message("user", "other turn"), 5.1),
    ]
    rollout.write_text("".join(json.dumps(record) + "\n" for record in records))
    return rollout


def test_latest_transcript(tmp_path, monkeypatch):
    setup_records(tmp_path, monkeypatch)
    with transcript.database(tmp_path / "logs_2.sqlite") as db:
        request = transcript.latest_request(
            db, transcript.timestamp("2026-10-25T00:00:10Z")
        )
    assert request["thread"] == "b" and request["log_id"] == 4
    assert request["completion_log_id"] == 5
    assert request["request_id"] == "req-1"
    output = transcript.collect(tmp_path)
    before, after = output.split("## 请求后的同 turn 记录", 1)
    assert all(
        value in before
        for value in (
            "system instruction",
            "rules",
            "question",
            "function_call_output",
            "answer",
        )
    )
    assert "final reply" not in before and "final reply" in after
    assert "other turn" not in output
    assert "same millisecond" in before.split("## 边界不明记录", 1)[1]
    assert "same millisecond" not in before.split("## 边界不明记录", 1)[0]
    path = copy_script.save_output(output, tmp_path / "transcript.md")
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        copy_script.save_output("overwrite", path)
    assert path.read_text() == output


def test_compaction_and_redaction(tmp_path, monkeypatch):
    rollout = setup_records(tmp_path, monkeypatch)
    records = [json.loads(line) for line in rollout.read_text().splitlines()]
    compact = event(
        "compacted", {"replacement_history": [message("developer", "replacement")]}, 3.5
    )
    records.insert(6, compact)
    records.insert(
        7,
        event(
            "response_item",
            {"type": "image_generation_call", "result": "SECRET_IMAGE_BASE64"},
            3.6,
        ),
    )
    records.insert(
        8,
        event(
            "response_item",
            message(
                "user",
                "password=secret Bearer abc sk-secret data:image/png;base64,AAAA `code```",
            ),
            3.7,
        ),
    )
    records.insert(
        9,
        event(
            "response_item",
            {
                "type": "reasoning",
                "encrypted_content": "encrypted secret",
                "summary": "visible",
            },
            3.8,
        ),
    )
    rollout.write_text("".join(json.dumps(record) + "\n" for record in records))
    output = transcript.collect(tmp_path)
    assert (
        "replacement" in output and "question" not in output and "rules" not in output
    )
    assert all(
        value not in output
        for value in (
            "SECRET_IMAGE_BASE64",
            "encrypted secret",
            "password=secret",
            "Bearer abc",
            "sk-secret",
            "base64,AAAA",
        )
    )
    assert "visible" in output and "````json" in output
    compact["payload"] = {"message": "saved summary"}
    rollout.write_text("".join(json.dumps(record) + "\n" for record in records))
    output = transcript.collect(tmp_path)
    assert "saved summary" in output and "replacement_history" in output
    assert "question" not in output


def test_missing_and_partial(tmp_path, monkeypatch):
    rollout = setup_records(tmp_path, monkeypatch)
    with rollout.open("a") as stream:
        stream.write('{"timestamp":')
    assert "尾行未写完" in transcript.collect(tmp_path)
    with sqlite3.connect(tmp_path / "logs_2.sqlite") as db:
        db.execute("UPDATE logs SET thread_id='missing' WHERE id=4")
        db.execute("DELETE FROM logs WHERE id>4")
    with pytest.raises(ValueError, match="absent from state"):
        transcript.collect(tmp_path)
    with sqlite3.connect(tmp_path / "logs_2.sqlite") as db:
        db.execute("DELETE FROM logs")
    with pytest.raises(ValueError, match="No retained"):
        transcript.collect(tmp_path)


def test_completion_only(tmp_path, monkeypatch):
    setup_records(tmp_path, monkeypatch)
    with sqlite3.connect(tmp_path / "logs_2.sqlite") as db:
        db.execute("DELETE FROM logs WHERE id!=5")
    output = transcript.collect(tmp_path)
    assert "请求起点未知" in output and "可能包含本次响应" in output


def test_cutoff_and_turn(tmp_path, monkeypatch):
    rollout = setup_records(tmp_path, monkeypatch)
    with transcript.database(tmp_path / "logs_2.sqlite") as db:
        request = transcript.latest_request(
            db, transcript.timestamp("2026-10-25T00:00:03Z")
        )
    assert request["log_id"] == 2
    rollout.write_text(
        json.dumps(event("session_meta", {"base_instructions": "system"}, 0)) + "\n"
    )
    with pytest.raises(ValueError, match="turn is absent"):
        transcript.collect(tmp_path)


def test_usage_deltas():
    records = []
    total = 0

    def add(kind, payload):
        records.append((0, {"type": kind, "payload": payload}))

    def usage(tokens, turn="current"):
        nonlocal total
        total += tokens + 10
        add(
            "token_usage_record",
            {
                "turn_id": turn,
                "response_id": f"response-{total}",
                "usage": {
                    "input_tokens": tokens,
                    "cached_input_tokens": 20,
                    "output_tokens": 10,
                    "reasoning_output_tokens": 5,
                    "total_tokens": tokens + 10,
                },
                "thread_token_usage": {"total_tokens": total},
            },
        )

    add("turn_context", {"model": "a"})
    usage(100, "previous")
    usage(130)
    records.append(records[-1])
    usage(130)
    usage(140)
    add("compacted", {})
    add(
        "event_msg",
        {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 999999}}},
    )
    usage(50)
    add("turn_context", {"model": "b"})
    usage(60)
    add("event_msg", {"type": "task_started"})
    add("event_msg", {"type": "task_complete"})
    usage(70)
    report = transcript.usage_report(records, "current")
    rows = json.loads(report.split("```json\n")[1].split("\n```")[0])
    assert [row["input_tokens_delta"] for row in rows] == [
        30,
        0,
        None,
        None,
        None,
        None,
    ]
    assert all(row["usage"]["reasoning_output_tokens"] == 5 for row in rows)
    assert "999999" not in report
    total = 0
    usage(20)
    rows = json.loads(
        transcript.usage_report(records, "current")
        .split("```json\n")[1]
        .split("\n```")[0]
    )
    assert rows[-1]["input_tokens_delta"] is None


async def test_latest_atomic(tmp_path, monkeypatch):
    import asyncio

    seen = []

    def collect(root, thread, turn, cutoff):
        seen.append((thread, turn, cutoff))
        return turn

    monkeypatch.setattr(transcript, "collect", collect)
    writer = transcript.Transcripts(tmp_path)
    writer.completed("thread-a", "first")
    writer.completed("thread-b", "second")
    await writer.close()
    path = tmp_path / "latest-transcript.md"
    assert path.read_text() == "second"
    assert path.stat().st_mode & 0o777 == 0o600
    assert [row[1] for row in seen] == ["first", "second"]
    assert not list(tmp_path.glob(".transcript-*"))
    await asyncio.sleep(0)


def test_copy_only(tmp_path, monkeypatch):
    from types import SimpleNamespace

    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(
            returncode=0, stdout="# 最近 LLM 请求：重建 transcript\ncontent"
        )

    monkeypatch.setattr(copy_script.subprocess, "run", run)
    assert copy_script.export("pi").endswith("content")
    assert len(calls) == 1
    assert calls[0][0][-1] == "sudo -n cat /var/lib/qq-codex-agent/latest-transcript.md"
    assert "input" not in calls[0][1]
    with pytest.raises(ValueError):
        copy_script.export("pi; command")
