import asyncio
import contextlib
import json
import logging
import re
import sqlite3

LOG = logging.getLogger("qq-codex-agent")


def read_diagnostics(path, cursor):
    with contextlib.closing(
        sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.2)
    ) as db:
        latest = db.execute("SELECT coalesce(max(id), 0) FROM logs").fetchone()[0]
        if cursor is None:
            return latest, []
        if latest < cursor:
            cursor = 0
        rows = db.execute(
            "SELECT id, ts, thread_id, target, feedback_log_body FROM logs "
            "WHERE id>? AND id<=? AND target IN (?, ?) ORDER BY id LIMIT 500",
            (cursor, latest, "codex_http_client::request", "codex_http_client::client"),
        ).fetchall()
    records = []
    for identifier, timestamp, thread, target, body in rows:
        body = body or ""
        if target == "codex_http_client::request" and "Compressed request body" in body:
            fields = (
                "pre_compression_bytes",
                "post_compression_bytes",
                "compression_duration_ms",
            )
            kind = "compression"
        elif "Request completed method=POST" in body and re.search(
            r"url=https?://[^\s]+/responses\s", body
        ):
            fields = ("status",)
            kind = "response"
        else:
            continue
        record = {"event": kind, "log_id": identifier, "timestamp": timestamp}
        for field in fields:
            match = re.search(r"\b" + field + r"=([0-9]{1,20})\b", body)
            if match:
                record[field] = int(match[1])
        if not all(field in record for field in fields):
            continue
        if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", thread or ""):
            record["thread"] = thread
        for field, pattern in (
            ("turn", r"\bturn\.id=([A-Za-z0-9_-]{1,128})(?=\s|})"),
            ("model", r"\bmodel=([A-Za-z0-9_.-]{1,128})(?=\s|})"),
            ("request_id", r'"x-oai-request-id":\s*"([A-Za-z0-9_-]{1,128})"'),
        ):
            match = re.search(pattern, body)
            if match:
                record[field] = match[1]
        records.append(record)
    return rows[-1][0] if rows else latest, records


async def watch_diagnostics(state_dir):
    path = (state_dir / "codex/logs_2.sqlite").resolve()
    cursor, warned = None, False
    while True:
        try:
            cursor, records = await asyncio.to_thread(read_diagnostics, path, cursor)
            for record in records:
                LOG.info("Codex request diagnostic=%s", json.dumps(record))
            warned = False
        except sqlite3.Error as error:
            if not warned:
                LOG.warning("Codex diagnostics unavailable error=%s", log_text(error))
                warned = True
        await asyncio.sleep(5)


def log_text(value):
    text = (
        f"{type(value).__name__}: {value}"
        if isinstance(value, BaseException)
        else str(value)
    )
    text = re.sub(
        r"(?:data:image/[^;,\s]+;base64,|base64://)[A-Za-z0-9+/=]+", "[image]", text
    )
    text = re.sub(r"(?i)\bBearer\s+[^\s\"']+", "Bearer [redacted]", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[redacted]", text)
    text = re.sub(
        r"(?i)(\b(?:access_token|refresh_token|id_token|api_key|token|authorization|cookie|password)[\"'\s]*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,}]+)",
        r"\1[redacted]",
        text,
    )
    return json.dumps(text, ensure_ascii=False)
