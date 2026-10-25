import json
import time
import uuid
from dataclasses import asdict, replace
from datetime import datetime, timezone

from openai_codex import TextInput

from image_assets import save_image
from .logging import LOG, log_text
from .messages import Message, display_name, parse_parts


BACKGROUND_SECONDS = 300
BACKGROUND_COUNT = 40
BACKGROUND_CHARS = 16000


class Groups:
    def __init__(self, settings, db, bot, inputs):
        self.settings = settings
        self.db = db
        self.bot = bot
        self.inputs = inputs

    def record_body(self, message):
        content = []
        for index, (kind, value) in enumerate(message.parts):
            if kind == "image":
                image_id = uuid.uuid5(
                    uuid.NAMESPACE_URL, f"{message.identifier}:{index}"
                ).hex
                self.db.execute(
                    "INSERT OR IGNORE INTO received_images (id,key,source,created) VALUES (?,?,?,?)",
                    (image_id, message.key, value, time.time()),
                )
                content.append({"image_id": image_id})
            elif kind == "at":
                content.append({"at": value})
            else:
                content.append(value)
        timestamp = message.timestamp
        try:
            timestamp = (
                datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
                if timestamp
                else None
            )
        except (ValueError, OverflowError, OSError):
            timestamp = None
        body = {
            "message_id": message.identifier.split(":", 2)[-1],
            "sender_id": message.sender_id,
            "sender_name": message.sender_name,
            "time": timestamp,
            "time_source": message.time_source if timestamp else "unknown",
            "content": content,
        }
        if message.reply:
            body["quote"] = {
                "message_id": message.reply,
                "time": None,
                "time_source": "unknown",
                "content": ["[引用消息尚未读取]"],
            }
        return body

    def record(self, message, command=False):
        own = message.sender_id == message.bot_id
        with self.db:
            old = self.db.execute(
                "SELECT body FROM group_messages WHERE id=?", (message.identifier,)
            ).fetchone()
            if old:
                if message.time_source == "event":
                    body = json.loads(old[0])
                    body.update(
                        {
                            key: value
                            for key, value in self.record_body(message).items()
                            if key in {"time", "time_source"}
                        }
                    )
                    self.db.execute(
                        "UPDATE group_messages SET body=? WHERE id=?",
                        (json.dumps(body, ensure_ascii=False), message.identifier),
                    )
                return False
            inserted = self.db.execute(
                "INSERT OR IGNORE INTO messages VALUES (?,'received')",
                (message.identifier,),
            ).rowcount
            if not inserted:
                return False
            body = self.record_body(message)
            now = time.time()
            self.db.execute(
                "INSERT OR IGNORE INTO group_state (key) VALUES (?)", (message.key,)
            )
            last, segment = self.db.execute(
                "SELECT last_arrival,segment FROM group_state WHERE key=?",
                (message.key,),
            ).fetchone()
            readable = self.readable(message) or message.reply is not None
            if not own and not command and readable:
                config = self.settings.groups[str(message.target["group_id"])]
                if not last or now - last > config.conversation_gap_seconds:
                    segment = now
                    self.db.execute(
                        "UPDATE group_state SET segment=?,silences=0,backoff_until=0 WHERE key=?",
                        (segment, message.key),
                    )
                self.db.execute(
                    "UPDATE group_state SET last_arrival=? WHERE key=?",
                    (now, message.key),
                )
            self.db.execute(
                "INSERT INTO group_messages (id,key,body,own,created,consumed,message,ready,priority,segment) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    message.identifier,
                    message.key,
                    json.dumps(body, ensure_ascii=False),
                    own,
                    now,
                    own or command,
                    json.dumps(asdict(message)),
                    -1 if own or command else 0,
                    int(
                        not own
                        and not command
                        and (message.mentioned or message.reply is not None)
                    ),
                    segment,
                ),
            )
            self.db.execute(
                "UPDATE group_state SET total=total+1, own=own+?, count=count+? WHERE key=?",
                (own, not own and not command, message.key),
            )
        return True

    async def resolve_body(self, body, target):
        for index, part in enumerate(body["content"]):
            if isinstance(part, dict) and "at" in part:
                user = part["at"]
                name = (
                    "全体成员"
                    if user == "all"
                    else await self.inputs.group_name(target, user)
                )
                body["content"][index] = f"@({user}){name}"
        return body

    async def prepare(self, message):
        row = self.db.execute(
            "SELECT body FROM group_messages WHERE id=?", (message.identifier,)
        ).fetchone()
        body = await self.resolve_body(json.loads(row[0]), message.target)
        quoted_bot = False
        if message.reply:
            identifier = f"{message.bot_id}:{message.key}:{message.reply}"
            row = self.db.execute(
                "SELECT body FROM group_messages WHERE id=? AND key=?",
                (identifier, message.key),
            ).fetchone()
            try:
                if row:
                    quote = json.loads(row[0])
                    quote.pop("quote", None)
                else:
                    result = await self.bot.reply_record(message.reply, message.target)
                    parts, images, _, unsupported = parse_parts(result["message"], True)
                    sender = result["sender"]
                    timestamp = result.get("time")
                    quoted = replace(
                        message,
                        identifier=identifier,
                        sender_id=str(sender["user_id"]),
                        sender_name=display_name(sender, True)
                        or str(sender["user_id"]),
                        parts=parts,
                        images=images,
                        unsupported=unsupported,
                        timestamp=timestamp
                        if type(timestamp) in (int, float) and timestamp > 0
                        else 0,
                        time_source="event",
                        reply=None,
                    )
                    with self.db:
                        quote = self.record_body(quoted)
                body["quote"] = await self.resolve_body(quote, message.target)
                quoted_bot = quote["sender_id"] == message.bot_id
            except Exception as error:
                LOG.warning(
                    "Group quote unavailable chat=%s error=%s",
                    message.key,
                    log_text(error),
                )
                body["quote"] = {
                    "message_id": message.reply,
                    "time": None,
                    "time_source": "unknown",
                    "content": ["[引用消息不可读取]"],
                }
        with self.db:
            self.db.execute(
                "UPDATE group_messages SET body=? WHERE id=?",
                (json.dumps(body, ensure_ascii=False), message.identifier),
            )
        return quoted_bot

    @staticmethod
    def readable(message):
        return any(
            kind in {"at", "image"} or (kind == "text" and value.strip())
            for kind, value in message.parts
        )

    def observe(self, message, quoted_bot):
        message.direct = message.mentioned or quoted_bot
        readable = self.readable(message) or quoted_bot
        with self.db:
            self.db.execute(
                "UPDATE group_messages SET ready=?,priority=?,message=?,consumed=CASE WHEN ? THEN consumed ELSE 1 END WHERE id=?",
                (
                    1 if readable else -1,
                    int(message.direct),
                    json.dumps(asdict(message)),
                    readable,
                    message.identifier,
                ),
            )
        return readable

    def unprepared(self, key):
        return [
            Message(**json.loads(row[0]))
            for row in self.db.execute(
                "SELECT message FROM group_messages WHERE key=? AND ready=0 AND consumed=0 AND message IS NOT NULL ORDER BY sequence",
                (key,),
            )
        ]

    def expire(self, key):
        config = self.settings.groups[key[6:]]
        cutoff = time.time() - config.message_ttl_seconds
        with self.db:
            expired_batches = [
                row[0]
                for row in self.db.execute(
                    "SELECT b.id FROM group_batches b WHERE b.key=? AND b.status='pending' AND b.priority=0 AND EXISTS (SELECT 1 FROM group_messages m WHERE m.batch=b.id AND (m.created<=? OR m.segment<(SELECT segment FROM group_state WHERE key=?)))",
                    (key, cutoff, key),
                )
            ]
            rows = self.db.execute(
                "SELECT id FROM group_messages WHERE key=? AND own=0 AND consumed=0 AND priority=0 AND batch IS NULL AND (created<=? OR segment<(SELECT segment FROM group_state WHERE key=?))",
                (key, cutoff, key),
            ).fetchall()
            for batch in expired_batches:
                rows.extend(
                    self.db.execute(
                        "SELECT id FROM group_messages WHERE batch=?", (batch,)
                    ).fetchall()
                )
                self.db.execute(
                    "UPDATE group_batches SET status='expired' WHERE id=?", (batch,)
                )
                self.db.execute(
                    "UPDATE messages SET status='expired' WHERE id=(SELECT json_extract(message,'$.identifier') FROM group_batches WHERE id=?)",
                    (batch,),
                )
            for (identifier,) in rows:
                self.db.execute(
                    "UPDATE group_messages SET consumed=1 WHERE id=?", (identifier,)
                )
                self.db.execute(
                    "UPDATE messages SET status='expired' WHERE id=?", (identifier,)
                )
        if rows:
            LOG.info("Group candidates expired chat=%s count=%d", key, len(rows))

    def candidates(self, key):
        self.expire(key)
        return [
            Message(**json.loads(row[0]))
            for row in self.db.execute(
                "SELECT message FROM group_messages WHERE key=? AND own=0 AND consumed=0 AND ready=1 AND batch IS NULL AND message IS NOT NULL ORDER BY sequence",
                (key,),
            )
        ]

    def retry(self, key):
        with self.db:
            self.db.execute(
                "UPDATE messages SET status='received' WHERE status='failed' AND id IN (SELECT json_extract(message,'$.identifier') FROM group_batches WHERE key=? AND status='pending')",
                (key,),
            )

    def timing(self, message):
        row = self.db.execute(
            "SELECT created,consumed FROM group_messages WHERE id=?",
            (message.identifier,),
        ).fetchone()
        previous = self.db.execute(
            "SELECT created,json_extract(message,'$.sender_id') FROM group_messages WHERE key=? AND own=0 AND ready=1 AND message IS NOT NULL AND sequence<(SELECT sequence FROM group_messages WHERE id=?) ORDER BY sequence DESC LIMIT 1",
            (message.key, message.identifier),
        ).fetchone()
        return row, previous

    def eligible(self, message):
        self.expire(message.key)
        return (
            self.db.execute(
                "SELECT 1 FROM group_batches WHERE id=? AND status='pending'",
                (message.batch,),
            ).fetchone()
            is not None
        )

    def delay(self, key):
        last, until = self.db.execute(
            "SELECT last_observation,backoff_until FROM group_state WHERE key=?", (key,)
        ).fetchone()
        interval = self.settings.groups[key[6:]].observation_interval_seconds
        return max(0, last + interval - time.time(), until - time.time())

    def result(self, message, status, visible):
        if status != "completed":
            return
        segment = self.db.execute(
            "SELECT segment FROM group_batches WHERE id=?", (message.batch,)
        ).fetchone()
        row = self.db.execute(
            "SELECT segment,silences FROM group_state WHERE key=?", (message.key,)
        ).fetchone()
        if not segment or not row or segment[0] != row[0]:
            return
        if not visible and message.direct:
            return
        count = 0 if visible else row[1] + 1
        delay = min(300, 60 * 2 ** min(3, count - 2)) if count >= 2 else 0
        with self.db:
            self.db.execute(
                "UPDATE group_state SET silences=?,backoff_until=? WHERE key=?",
                (count, time.time() + delay if delay else 0, message.key),
            )
        LOG.info(
            "Group observation ended chat=%s visible=%s silences=%d backoff=%s",
            message.key,
            visible,
            count,
            delay,
        )

    def freeze(self, message, direct_only=False):
        self.expire(message.key)
        now = time.time()
        row = self.db.execute(
            "SELECT received,message_gen,thread_gen FROM activity WHERE key=?",
            (message.key,),
        ).fetchone()
        generation = (row[1] + int(now - row[0] > 7200)) if row else 0
        rows = self.db.execute(
            "SELECT id,body,message,priority,segment FROM group_messages WHERE key=? AND own=0 AND batch IS NULL AND consumed=0 AND ready=1 AND (?=0 OR priority=1) AND sequence <= (SELECT sequence FROM group_messages WHERE id=?) ORDER BY sequence",
            (message.key, direct_only, message.identifier),
        ).fetchall()
        if not rows:
            raise ValueError("No eligible group messages")
        required = any(json.loads(row[2]).get("mentioned", False) for row in rows)
        direct = any(row[3] for row in rows)
        batch = replace(
            message,
            batch=uuid.uuid4().hex,
            generation=generation,
            mentioned=required,
            direct=direct,
        )
        segment = rows[-1][4]
        identifiers = {row[0] for row in rows}
        background, size = [], 0
        for identifier, body in self.db.execute(
            "SELECT id,body FROM group_messages WHERE key=? AND ready!=0 AND segment=? AND created>=? AND sequence <= (SELECT sequence FROM group_messages WHERE id=?) ORDER BY sequence DESC LIMIT ?",
            (
                message.key,
                segment,
                now - BACKGROUND_SECONDS,
                message.identifier,
                BACKGROUND_COUNT + len(rows),
            ),
        ):
            if identifier in identifiers:
                continue
            if (
                len(background) >= BACKGROUND_COUNT
                or size + len(body) > BACKGROUND_CHARS
            ):
                break
            background.append(json.loads(body))
            size += len(body)
        payload = json.dumps(
            {
                "background": list(reversed(background)),
                "messages": [json.loads(row[1]) for row in rows],
            },
            ensure_ascii=False,
        )
        batch.identifier = f"{message.bot_id}:{message.key}:batch-{batch.batch}"
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO activity VALUES (?,?,?,?)",
                (message.key, now, generation, row[2] if row else 0),
            )
            self.db.execute(
                "INSERT INTO group_batches (id,key,message,status,created,payload,priority,segment) VALUES (?,?,?,'pending',?,?,?,?)",
                (
                    batch.batch,
                    batch.key,
                    json.dumps(asdict(batch)),
                    now,
                    payload,
                    int(direct),
                    segment,
                ),
            )
            self.db.executemany(
                "UPDATE group_messages SET batch=? WHERE id=?",
                [(batch.batch, identifier) for identifier in identifiers],
            )
            self.db.execute(
                "UPDATE group_state SET count=(SELECT count(*) FROM group_messages WHERE key=group_state.key AND own=0 AND consumed=0 AND batch IS NULL),last_request=? WHERE key=?",
                (time.time(), batch.key),
            )
            self.db.execute(
                "INSERT INTO messages VALUES (?,'received')", (batch.identifier,)
            )
        return batch

    def pending(self, key, retry=True):
        self.expire(key)
        return [
            Message(**json.loads(row[0]))
            for row in self.db.execute(
                "SELECT message FROM group_batches WHERE key=? AND status='pending' AND (? OR coalesce((SELECT status FROM messages WHERE id=json_extract(group_batches.message,'$.identifier')),'received')!='failed') ORDER BY created,rowid",
                (key, retry),
            )
        ]

    def input(self, message):
        row = self.db.execute(
            "SELECT payload FROM group_batches WHERE id=?", (message.batch,)
        ).fetchone()
        payload = row[0] if row else None
        if payload is None:
            rows = self.db.execute(
                "SELECT body FROM group_messages WHERE batch=? ORDER BY sequence",
                (message.batch,),
            )
            payload = json.dumps(
                {"background": [], "messages": [json.loads(row[0]) for row in rows]},
                ensure_ascii=False,
            )
            with self.db:
                self.db.execute(
                    "UPDATE group_batches SET payload=? WHERE id=?",
                    (payload, message.batch),
                )
        return [TextInput("群聊消息记录：\n" + payload)]

    def status(self, message, status):
        with self.db:
            if status == "submitted" and not message.direct:
                self.db.execute(
                    "UPDATE group_state SET last_observation=? WHERE key=?",
                    (time.time(), message.key),
                )
            self.db.execute(
                "UPDATE messages SET status=? WHERE id IN (SELECT id FROM group_messages WHERE batch=?)",
                (
                    "running" if status in {"submitting", "submitted"} else status,
                    message.batch,
                ),
            )
            self.db.execute(
                "UPDATE group_batches SET status=? WHERE id=?", (status, message.batch)
            )
            if status not in {"pending", "submitting"}:
                self.db.execute(
                    "UPDATE group_messages SET consumed=1 WHERE batch=?",
                    (message.batch,),
                )

    def cancel(self, key, identifier=None):
        cutoff = self.db.execute(
            "SELECT sequence FROM group_messages WHERE id=?", (identifier,)
        ).fetchone()
        sequence = (
            cutoff[0]
            if cutoff
            else self.db.execute(
                "SELECT coalesce(max(sequence),0) FROM group_messages WHERE key=?",
                (key,),
            ).fetchone()[0]
        )
        with self.db:
            self.db.execute(
                "UPDATE messages SET status='canceled' WHERE id IN (SELECT id FROM group_messages WHERE key=? AND consumed=0 AND sequence<=?)",
                (key, sequence),
            )
            self.db.execute(
                "UPDATE group_messages SET consumed=1 WHERE key=? AND consumed=0 AND sequence<=?",
                (key, sequence),
            )
            self.db.execute(
                "UPDATE group_batches SET status='canceled' WHERE key=? AND status IN ('pending','submitting','submitted') AND id IN (SELECT batch FROM group_messages WHERE key=? AND sequence<=?)",
                (key, key, sequence),
            )
            self.db.execute(
                "UPDATE group_state SET count=(SELECT count(*) FROM group_messages WHERE key=group_state.key AND own=0 AND consumed=0 AND batch IS NULL),silences=0,backoff_until=0,last_request=0 WHERE key=?",
                (key,),
            )

    def sent(self, message, receipt, parts, image=None):
        if "group_id" not in message.target:
            return
        identifier = receipt.get("message_id") if isinstance(receipt, dict) else None
        if type(identifier) not in (int, str) or not str(identifier):
            raise RuntimeError("Missing QQ message receipt")
        sent = replace(
            message,
            identifier=f"{message.bot_id}:{message.key}:{identifier}",
            sender_id=message.bot_id,
            sender_name=message.bot_id,
            parts=parts,
            timestamp=time.time(),
            time_source="sent",
            reply=None,
        )
        self.record(sent)
        if image is not None:
            row = self.db.execute(
                "SELECT folder FROM sessions WHERE key=?", (message.key,)
            ).fetchone()
            folder_name = row[0] if row else uuid.uuid4().hex
            folder = self.settings.workspace_dir / folder_name
            folder.mkdir(mode=0o700, exist_ok=True)
            path = save_image(folder, image)
            image_id = uuid.uuid5(uuid.NAMESPACE_URL, f"{sent.identifier}:0").hex
            with self.db:
                self.db.execute(
                    "INSERT OR IGNORE INTO sessions (key,folder) VALUES (?,?)",
                    (message.key, folder_name),
                )
                self.db.execute(
                    "UPDATE received_images SET source=NULL,folder=?,path=? WHERE id=?",
                    (folder_name, path, image_id),
                )
