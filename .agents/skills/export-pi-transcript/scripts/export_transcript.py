"""Reconstruct the latest Pi model request."""

import argparse
import contextlib
import datetime as dt
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
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


def latest_request(db, cutoff):
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
    # ponytail: millisecond timestamps; exact capture needed.
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


def collect(root=ROOT):
    cutoff = time.time_ns()
    warnings = []
    with database(root / "logs_2.sqlite") as db:
        request = latest_request(db, cutoff)
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
    return render(request, records, warnings, cutoff)


def export(host):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]*", host):
        raise ValueError("Expected an SSH host alias")
    ssh = ["ssh"]
    config = Path.home() / ".ssh/config"
    if config.is_file():
        ssh.extend(["-F", str(config)])
    ssh.extend(["-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host])
    # Journal bodies may contain private chat.
    probe = subprocess.run(
        [
            *ssh,
            f"sudo -n journalctl -u {UNIT} -n 1 -o json --output-fields=_SYSTEMD_UNIT --no-pager",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if probe.returncode or UNIT not in probe.stdout:
        raise ValueError(
            "Service journal unavailable; check SSH and non-interactive sudo"
        )
    result = subprocess.run(
        [*ssh, "sudo -n python3 - --collect"],
        input=Path(__file__).read_text(),
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode:
        raise ValueError("Remote export failed: " + str(clean(result.stderr.strip())))
    if not result.stdout.startswith("# 最近 LLM 请求：重建 transcript\n"):
        raise ValueError("Unexpected remote output; no transcript written")
    return result.stdout


def save_output(text, path):
    if path is None:
        descriptor, name = tempfile.mkstemp(
            prefix="qq-transcript-", suffix=".md", dir="/tmp"
        )
        path = Path(name)
    else:
        path = path.expanduser().absolute()
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="pi")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--collect", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if args.collect:
            sys.stdout.write(collect())
        else:
            print(save_output(export(args.host), args.output))
    except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError) as error:
        print(clean(str(error)), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
