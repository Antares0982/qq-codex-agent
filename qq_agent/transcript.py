"""Reconstruct the latest Pi model request."""

import contextlib
import datetime as dt
import json
import os
import re
import sqlite3
import tempfile
import asyncio

from .logging import LOG
import time
from pathlib import Path

ROOT = Path("/var/lib/qq-codex-agent/codex")
UNIT = "qq-codex-agent.service"
SECRET = re.compile(
    r"(?i)\b(access_token|refresh_token|id_token|api_key|token|authorization|cookie|password)\b"
)


def clean(value):
    if isinstance(value, dict):
        return {
            key: "[redacted]" if SECRET.fullmatch(key) else clean(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [clean(item) for item in value]
    if not isinstance(value, str):
        return value
    value = re.sub(
        r"data:[^;,\s]+;base64,[A-Za-z0-9+/=]+|base64://[A-Za-z0-9+/=]+",
        "[binary omitted]",
        value,
    )
    value = re.sub(r"(?i)\bBearer\s+[^\s\"']+", "Bearer [redacted]", value)
    value = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[redacted]", value)
    return re.sub(
        r"(?i)(\b(?:access_token|refresh_token|id_token|api_key|token|authorization|cookie|password)[\"'\s]*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,}]+)",
        r"\1[redacted]",
        value,
    )


@contextlib.contextmanager
def database(path):
    with contextlib.closing(
        sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    ) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        yield db


def log_field(body, name):
    match = re.search(
        r"\b" + re.escape(name) + r'="?([A-Za-z0-9_.-]+)"?(?=\s|}|$)', body
    )
    return match[1] if match else None


def request_record(row):
    body = row["feedback_log_body"] or ""
    compressed = (
        row["target"] == "codex_http_client::request"
        and "Compressed request body" in body
    )
    completed = (
        row["target"] == "codex_http_client::client"
        and "Request completed method=POST" in body
    )
    if not (compressed or completed):
        return None
    endpoint = re.search(
        r'(?:api\.path="?|url="?https?://[^\s/]+(?:/[^\s]*)?)(/responses(?:/compact)?)"?(?=\s|}|$)',
        body,
    )
    if not endpoint:
        return None
    request_id = re.search(r'"x-oai-request-id":\s*"([A-Za-z0-9_-]{1,128})"', body)
    return {
        "request_id": request_id[1] if request_id else None,
        "log_id": row["id"],
        "time_ns": row["ts"] * 1_000_000_000 + row["ts_nanos"],
        "thread": row["thread_id"] or log_field(body, "thread.id"),
        "turn": log_field(body, "turn.id"),
        "model": log_field(body, "model"),
        "process": row["process_uuid"],
        "endpoint": endpoint[1],
        "event": "request" if compressed else "response",
        "status": log_field(body, "status") if completed else None,
    }


def latest_request(db, cutoff, thread=None, turn=None):
    rows = db.execute(
        "SELECT * FROM logs WHERE (ts < ? OR (ts = ? AND ts_nanos <= ?)) "
        "AND ((target='codex_http_client::request' AND feedback_log_body LIKE '%Compressed request body%') "
        "OR (target='codex_http_client::client' AND feedback_log_body LIKE '%Request completed method=POST%')) "
        "ORDER BY id DESC",
        (cutoff // 1_000_000_000, cutoff // 1_000_000_000, cutoff % 1_000_000_000),
    ).fetchall()
    requests = []
    pending = {}
    for row in reversed(rows):
        record = request_record(row)
        if record is None:
            continue
        key = tuple(record[name] for name in ("process", "thread", "turn", "endpoint"))
        if record["event"] == "request":
            requests.append(record)
            pending[key] = record
        else:
            request = pending.pop(key, None)
            if request is None:
                requests.append(record)
            else:
                request["status"] = record["status"]
                request["completion_log_id"] = record["log_id"]
                request["request_id"] = record["request_id"]
    if thread is not None:
        requests = [
            item
            for item in requests
            if item["thread"] == thread and item["turn"] == turn
        ]
    if not requests:
        raise ValueError("No retained Responses request record")
    latest = max(requests, key=lambda item: (item["time_ns"], item["log_id"]))
    if not latest["thread"] or not latest["turn"]:
        raise ValueError(
            "Latest request lacks thread/turn identifiers; refusing another thread"
        )
    return latest


def timestamp(value):
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Rollout timestamp lacks timezone")
    delta = parsed - dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
    return (
        delta.days * 86400 + delta.seconds
    ) * 1_000_000_000 + delta.microseconds * 1000


def load_rollout(path, cutoff, warnings):
    # Full scan; index for long sessions.
    records = []
    with path.open("rb") as stream:
        remaining = os.fstat(stream.fileno()).st_size
        while remaining:
            line = stream.readline(remaining)
            remaining -= len(line)
            if not line.endswith(b"\n"):
                warnings.append("rollout 尾行未写完，已忽略；导出可能不完整。")
                break
            try:
                item = json.loads(line)
                when = timestamp(item["timestamp"])
                if when <= cutoff:
                    records.append((when, item))
            except (ValueError, KeyError, TypeError):
                raise ValueError(
                    "Invalid rollout record; cannot establish request history"
                ) from None
    return records


def safe_item(item):
    if isinstance(item, dict):
        result = {}
        for key, value in item.items():
            if key in {
                "encrypted_content",
                "image_url",
                "image_data",
                "audio",
                "bytes",
            } or (item.get("type") == "image_generation_call" and key == "result"):
                result[key] = "[binary or encrypted content omitted]"
            else:
                result[key] = safe_item(value)
        return clean(result)
    if isinstance(item, list):
        return [safe_item(value) for value in item]
    return clean(item)


def reconstruct(records, request, warnings):
    history, after, uncertain = [], [], []
    system = None
    current_turn = None
    found_turn = False
    boundary = request["time_ns"]
    # Millisecond boundaries require exact capture.
    boundary_ms = boundary // 1_000_000
    for when, record in records:
        payload = record.get("payload", {})
        kind = record.get("type")
        if kind == "session_meta":
            system = payload.get("base_instructions")
            continue
        if kind == "turn_context":
            current_turn = payload.get("turn_id")
            found_turn |= current_turn == request["turn"]
            continue
        if kind == "event_msg" and payload.get("type") == "task_started":
            current_turn = payload.get("turn_id")
            found_turn |= current_turn == request["turn"]
            continue
        if kind not in {"response_item", "compacted"}:
            continue
        if when // 1_000_000 == boundary_ms:
            uncertain.append(payload)
            if kind == "compacted":
                history = [
                    {
                        "type": "unavailable",
                        "message": "请求边界发生压缩，有效历史无法确定",
                    }
                ]
            warnings.append(
                "存在与请求同毫秒的记录，单列为边界不明，未计入确定的请求前历史。"
            )
            continue
        if when > boundary:
            if current_turn == request["turn"]:
                after.append(payload)
            continue
        if kind == "compacted":
            replacement = payload.get("replacement_history")
            if isinstance(replacement, list):
                history = replacement.copy()
            else:
                history = [
                    {
                        "type": "unavailable",
                        "message": "压缩后有效历史无法精确恢复",
                        "summary": payload.get("message"),
                    }
                ]
                warnings.append(
                    "压缩记录缺少 replacement_history，已舍弃压缩前历史，不能保证上下文完整。"
                )
        else:
            history.append(payload)
    if not found_turn:
        raise ValueError(
            "Latest request turn is absent from rollout; retry after runtime flush"
        )
    if system is None:
        warnings.append("缺少 session_meta.base_instructions，无法恢复 system。")
    if request["event"] == "response":
        warnings.append(
            "仅有请求完成记录；开始时间未知，下面的完成前历史可能包含本次响应，不是确定的请求输入。"
        )
    if request["endpoint"] == "/responses/compact":
        warnings.append("最近调用为远程压缩；专用压缩指令与实际输入转换未保存。")
    if after:
        warnings.append(
            "请求后记录按 turn 关联，不保证每项均属于这一次 HTTP 调用；也可能含后续 steering。"
        )
    return system, history, after, uncertain


def block(value):
    text = json.dumps(safe_item(value), ensure_ascii=False, indent=2)
    fence = "`" * max(
        3, max((len(match[0]) + 1 for match in re.finditer(r"`+", text)), default=3)
    )
    return f"{fence}json\n{text}\n{fence}"


def render(request, records, warnings, cutoff):
    system, history, after, uncertain = reconstruct(records, request, warnings)
    warnings[:0] = [
        "这是基于诊断日志和 rollout 的重建，不是原始 HTTP 请求正文。",
        "完整工具定义、内部动态提示词、请求前截断/转换可能未保存；不能保证与模型实际输入逐字相同。",
        "rollout 可能延迟落盘，未压缩且尚未完成的请求可能还没有可定位日志；未记录内容不补造。图片二进制、加密内容已省略，常见凭据已脱敏，聊天正文仍可能敏感。",
    ]
    title = (
        "完成前可用历史（请求起点未知）"
        if request["event"] == "response"
        else "请求前可用上下文"
    )
    output = [
        "# 最近 LLM 请求：重建 transcript",
        "",
        f"服务：`{UNIT}`；时间均为 UTC。",
        "",
        "## 请求定位",
        "",
        block(
            {
                **request,
                "request_time": dt.datetime.fromtimestamp(
                    request["time_ns"] / 1e9, dt.timezone.utc
                ).isoformat(),
                "export_cutoff": dt.datetime.fromtimestamp(
                    cutoff / 1e9, dt.timezone.utc
                ).isoformat(),
            }
        ),
        "",
        "## 完整性说明",
        "",
        *["- " + warning for warning in dict.fromkeys(warnings)],
        "",
        "## System（会话创建时保存）",
        "",
        block(system),
        "",
        "## " + title,
        "",
    ]
    for index, item in enumerate(history, 1):
        output.extend(
            [
                f"### {index}. {item.get('role') or item.get('type', 'unknown')}",
                "",
                block(item),
                "",
            ]
        )
    for heading, items in (
        ("边界不明记录", uncertain),
        ("请求后的同 turn 记录（不属于上述输入）", after),
    ):
        output.extend(["## " + heading, "", block(items), ""])
    return "\n".join(output)


def collect(root=ROOT, thread=None, turn=None, cutoff=None):
    cutoff = cutoff or time.time_ns()
    warnings = []
    request = None
    try:
        with database(root / "logs_2.sqlite") as db:
            request = latest_request(db, cutoff, thread, turn)
    except (OSError, ValueError, sqlite3.Error):
        if thread is None or turn is None:
            raise
        warnings.append(
            "当前 turn 缺少可定位的 HTTP 日志，展示 turn 结束前的可用历史，不能确定最后一次请求的输入边界。"
        )
        request = {
            "thread": thread,
            "turn": turn,
            "time_ns": cutoff,
            "event": "response",
            "endpoint": "unknown",
        }
    with database(root / "state_5.sqlite") as db:
        row = db.execute(
            "SELECT rollout_path FROM threads WHERE id=?", (request["thread"],)
        ).fetchone()
    if row is None:
        raise ValueError("Latest request thread is absent from state database")
    path = Path(row["rollout_path"]).resolve(strict=True)
    if not path.is_relative_to(root.resolve()) or path.suffix != ".jsonl":
        raise ValueError("Rollout path is outside the service runtime directory")
    records = load_rollout(path, cutoff, warnings)
    return render(request, records, warnings, cutoff) + usage_report(
        records, request["turn"]
    )


def usage_report(records, target):
    model, previous, cumulative = None, None, None
    counted = False
    last_row = None
    rows = []
    reset = "首次统计"
    for when, record in records:
        payload = record.get("payload", {})
        kind = record.get("type")
        if kind == "turn_context":
            changed = payload.get("model")
            if changed != model:
                previous, reset = None, "模型切换或未知"
            model = changed
        elif kind == "event_msg" and payload.get("type") == "task_started":
            counted = False
            last_row = None
        elif kind == "event_msg" and payload.get("type") in {
            "task_complete",
            "turn_aborted",
        }:
            if not counted:
                previous, reset = None, "上次请求统计缺失"
        elif kind == "compacted" or (
            kind == "response_item" and payload.get("type") == "compaction"
        ):
            previous, reset = None, "压缩后重置"
            if last_row is not None:
                last_row["input_tokens_delta"] = None
                last_row["baseline"] = "压缩关联，输入转换不可比"
        elif kind == "token_usage_record":
            counted = True
            total = payload.get("thread_token_usage")
            last = payload.get("usage") or {}
            if total is not None and total == cumulative:
                continue
            old_total, cumulative = cumulative, total
            valid = isinstance(last, dict) and all(
                type(last.get(key)) is int and last[key] >= 0
                for key in (
                    "input_tokens",
                    "cached_input_tokens",
                    "output_tokens",
                    "reasoning_output_tokens",
                    "total_tokens",
                )
            )
            if (
                not valid
                or model is None
                or total is None
                or (
                    old_total is not None
                    and total.get("total_tokens", 0) - old_total.get("total_tokens", 0)
                    != last.get("total_tokens")
                )
            ):
                previous, reset = None, "统计缺失或重置"
            if payload.get("turn_id") == target:
                rows.append(
                    {
                        "request": len(rows) + 1,
                        "response_id": payload.get("response_id"),
                        "recorded_at": dt.datetime.fromtimestamp(
                            when / 1e9, dt.timezone.utc
                        ).isoformat(),
                        "model": model,
                        "usage": last if valid else None,
                        "input_tokens_delta": last["input_tokens"] - previous
                        if valid and previous is not None
                        else None,
                        "baseline": "上一有效请求"
                        if valid and previous is not None
                        else reset,
                    }
                )
                last_row = rows[-1]
            else:
                last_row = None
            previous = (
                last["input_tokens"]
                if valid and model is not None and total is not None
                else None
            )
    return "\n## 请求 token 统计（当前 turn）\n\n" + (
        "取自 runtime 的 token_usage_record，按累计 usage 去重；输入净增量是本次 input_tokens 减去上次值，"
        "不是新增文本的精确分词，也不是计费增量。reasoning 已包含在 output 中。"
        "加密块不能逐段计数；未返回 usage 的压缩请求无法计数，不使用 token_count 中的压缩估算。\n\n"
        + block(rows or {"usage": None, "reason": "没有可用 token_usage_record"})
        + "\n"
    )


def save_latest(state, text):
    descriptor, name = tempfile.mkstemp(prefix=".transcript-", dir=state)
    path = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
        path.replace(state / "latest-transcript.md")
    finally:
        path.unlink(missing_ok=True)


class Transcripts:
    def __init__(self, state):
        self.state = state
        self.tail = None

    def completed(self, thread, turn):
        previous = self.tail
        cutoff = time.time_ns()

        async def export():
            if previous:
                await previous
            text = None
            for delay in (0.25, 1, 2):
                await asyncio.sleep(delay)
                try:
                    text = await asyncio.to_thread(
                        collect, self.state / "codex", thread, turn, cutoff
                    )
                    break
                except Exception as error:
                    LOG.warning(
                        "Transcript unavailable thread=%s turn=%s error=%s",
                        thread,
                        turn,
                        type(error).__name__,
                    )
            if text is None:
                text = (
                    "# 最近 LLM 请求：重建 transcript\n\n当前 turn 的诊断或 rollout 不完整，无法重建；usage 未知。\n\n"
                    + block({"thread": thread, "turn": turn})
                )
            try:
                await asyncio.to_thread(save_latest, self.state, text)
            except OSError:
                LOG.exception("Transcript save failed")

        self.tail = asyncio.create_task(export())

    async def close(self):
        if self.tail:
            await self.tail
