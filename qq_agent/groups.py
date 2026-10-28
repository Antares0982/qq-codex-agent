import hashlib
import json
import time
import uuid
from dataclasses import asdict, replace
from datetime import datetime

from openai_codex import TextInput

from image_assets import save_image
from .logging import LOG, log_text
from .messages import Message, is_command, media_key, parse_message


BACKGROUND_SECONDS = 300
BACKGROUND_COUNT = 40
BACKGROUND_CHARS = 16000
HISTORY_SECONDS = 14 * 86400
SYSTEM_PLACEHOLDER = "<redacted system message>"


class Groups:
    def __init__(self, settings, db, bot, inputs):
        self.settings = settings
        self.db = db
        self.bot = bot
        self.inputs = inputs

    def record_body(self, message):
        content = []
        for index, (kind, value) in enumerate(message.parts):
            if kind in {"image", "sticker"}:
                identity = message.image_keys.get(value, media_key(value))
                if value == "marketface" and identity == "file:marketface":
                    identity = f"{message.identifier}:{index}"
                image_id = uuid.uuid5(
                    uuid.NAMESPACE_URL, f"{message.key}:{identity}"
                ).hex
                self.db.execute(
                    "INSERT INTO received_images (id,key,source,created) VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET source=excluded.source,created=excluded.created",
                    (image_id, message.key, value, time.time()),
                )
                content.append(
                    {"sticker_id" if kind == "sticker" else "image_id": image_id}
                )
            elif kind == "at":
                content.append({"at": value})
            else:
                content.append(value)
        timestamp = message.timestamp
        try:
            timestamp = (
                datetime.fromtimestamp(timestamp)
                .astimezone()
                .isoformat(timespec="seconds")
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

    def system(self, message):
        with self.db:
            inserted = self.db.execute(
                "INSERT OR IGNORE INTO messages VALUES (?,'received')",
                (message.identifier,),
            ).rowcount
            self.db.execute(
                "INSERT OR IGNORE INTO system_messages VALUES (?,?,?,?,?,?)",
                (
                    message.identifier,
                    message.key,
                    message.sender_id == message.bot_id,
                    self.db.execute(
                        "SELECT coalesce(max(sequence),0) FROM group_messages WHERE key=?",
                        (message.key,),
                    ).fetchone()[0],
                    time.time(),
                    message.timestamp if message.time_source == "event" else None,
                ),
            )
        return bool(inserted)

    def record(self, message, command=False, *, sent=False):
        own = message.sender_id == message.bot_id
        if command or (
            own
            and not sent
            and not self.db.execute(
                "SELECT 1 FROM group_messages WHERE id=?", (message.identifier,)
            ).fetchone()
        ):
            return self.system(message)
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
                            for key, value in self.record_body(
                                replace(message, parts=[])
                            ).items()
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
            recovered = (
                sent
                and self.db.execute(
                    "DELETE FROM system_messages WHERE id=?", (message.identifier,)
                ).rowcount
            )
            if not inserted and not recovered:
                return False
            body = self.record_body(message)
            now = time.time()
            self.db.execute(
                "INSERT OR IGNORE INTO group_state (key) VALUES (?)", (message.key,)
            )
            segment = self.db.execute(
                "SELECT segment FROM group_state WHERE key=?", (message.key,)
            ).fetchone()[0]
            self.db.execute(
                "INSERT INTO group_messages (id,key,body,own,created,consumed,message,ready,priority,segment,category) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    message.identifier,
                    message.key,
                    json.dumps(body, ensure_ascii=False),
                    own,
                    now,
                    False,
                    json.dumps(asdict(message)),
                    -1 if own else 0,
                    int(not own and (message.mentioned or message.reply is not None)),
                    segment,
                    "low" if own else message.category,
                ),
            )
            self.db.execute(
                "UPDATE group_state SET total=total+1, own=own+?, count=count+? WHERE key=?",
                (own, not own and message.category == "normal", message.key),
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
            system = self.db.execute(
                "SELECT own FROM system_messages WHERE id=? AND key=?",
                (identifier, message.key),
            ).fetchone()
            row = self.db.execute(
                "SELECT body FROM group_messages WHERE id=? AND key=?",
                (identifier, message.key),
            ).fetchone()
            try:
                if system:
                    quote = {
                        "message_id": message.reply,
                        "content": [SYSTEM_PLACEHOLDER],
                    }
                    quoted_bot = bool(system[0])
                elif row:
                    quote = json.loads(row[0])
                    quote.pop("quote", None)
                    quoted_bot = quote["sender_id"] == message.bot_id
                else:
                    result = await self.bot.reply_record(message.reply, message.target)
                    quoted = parse_message(
                        {
                            **result,
                            "post_type": "message",
                            "self_id": message.bot_id,
                            "user_id": result["sender"]["user_id"],
                            "message_id": message.reply,
                            **message.target,
                        },
                        self.settings,
                    )
                    if quoted is None:
                        raise ValueError("Invalid quoted message")
                    quoted_bot = quoted.sender_id == message.bot_id
                    if is_command(quoted) or quoted_bot:
                        self.system(quoted)
                        quote = {
                            "message_id": message.reply,
                            "content": [SYSTEM_PLACEHOLDER],
                        }
                    else:
                        if quoted.time_source != "event":
                            quoted.timestamp = 0
                            quoted.time_source = "unknown"
                        quoted.reply = None
                        with self.db:
                            quote = self.record_body(quoted)
                body["quote"] = await self.resolve_body(quote, message.target)
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

    def observe(self, message, quoted_bot):
        message.direct = message.mentioned or quoted_bot
        readable = message.category == "normal" or message.direct
        with self.db:
            if readable:
                created = self.db.execute(
                    "SELECT created FROM group_messages WHERE id=?",
                    (message.identifier,),
                ).fetchone()[0]
                last, segment = self.db.execute(
                    "SELECT last_arrival,segment FROM group_state WHERE key=?",
                    (message.key,),
                ).fetchone()
                if (
                    not last
                    or created - last
                    > self.settings.groups[message.key[6:]].conversation_gap_seconds
                ):
                    segment = created
                    self.db.execute(
                        "UPDATE group_state SET segment=?,silences=0,backoff_until=0 WHERE key=?",
                        (segment, message.key),
                    )
                self.db.execute(
                    "UPDATE group_state SET last_arrival=? WHERE key=?",
                    (created, message.key),
                )
                self.db.execute(
                    "UPDATE group_messages SET segment=? WHERE id=?",
                    (segment, message.identifier),
                )
            self.db.execute(
                "UPDATE group_messages SET ready=?,priority=?,message=? WHERE id=?",
                (
                    1 if readable else -1,
                    int(message.direct),
                    json.dumps(asdict(message)),
                    message.identifier,
                ),
            )
        return readable

    def can_quote(self, key, identifier):
        if self.db.execute(
            "SELECT 1 FROM system_messages WHERE key=? AND substr(id,instr(id,':')+1)=?",
            (key, f"{key}:{identifier}"),
        ).fetchone():
            return False
        return (
            self.db.execute(
                "SELECT 1 FROM group_messages WHERE key=? AND (json_extract(body,'$.message_id')=? OR (json_extract(body,'$.quote.message_id')=? AND json_extract(body,'$.quote.sender_id') IS NOT NULL AND coalesce(json_extract(body,'$.quote.content[0]'),'')!=?)) LIMIT 1",
                (key, identifier, identifier, SYSTEM_PLACEHOLDER),
            ).fetchone()
            is not None
        )

    @staticmethod
    def format_history(payload):
        bodies = [*payload["background"], *payload["messages"]]
        identifiers = {body["message_id"] for body in bodies}
        for body in bodies:
            quote = body.get("quote")
            for item in (body, quote):
                if item and item.get("time"):
                    item["time"] = (
                        datetime.fromisoformat(item["time"])
                        .astimezone()
                        .isoformat(timespec="seconds")
                    )
            if (
                quote
                and quote["message_id"] in identifiers
                and quote.get("content") != [SYSTEM_PLACEHOLDER]
            ):
                body["quote"] = {"message_id": quote["message_id"]}
        return payload

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
                    "SELECT b.id FROM group_batches b WHERE b.key=? AND b.status='pending' AND b.priority=0 AND EXISTS (SELECT 1 FROM group_messages m WHERE m.batch=b.id AND ((m.category='normal' AND m.created<=?) OR (m.category='normal' AND m.segment<(SELECT segment FROM group_state WHERE key=?))))",
                    (key, cutoff, key),
                )
            ]
            rows = self.db.execute(
                "SELECT id FROM group_messages WHERE key=? AND own=0 AND category='normal' AND consumed=0 AND priority=0 AND batch IS NULL AND (created<=? OR segment<(SELECT segment FROM group_state WHERE key=?))",
                (key, cutoff, key),
            ).fetchall()
            for batch in expired_batches:
                self.db.execute(
                    "UPDATE group_messages SET batch=NULL WHERE batch=? AND category='low' AND priority=0",
                    (batch,),
                )
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
            "SELECT id,body,message,priority,segment,sequence FROM group_messages WHERE key=? AND own=0 AND batch IS NULL AND consumed=0 AND ready=1 AND (?=0 OR priority=1) AND sequence <= (SELECT sequence FROM group_messages WHERE id=?) ORDER BY sequence",
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
        low = self.db.execute(
            "SELECT id,body,message,priority,segment,sequence FROM group_messages WHERE key=? AND category='low' AND consumed=0 AND batch IS NULL AND ready=-1 AND created>=? ORDER BY sequence",
            (message.key, now - HISTORY_SECONDS),
        ).fetchall()
        rows = sorted([*rows, *low], key=lambda row: row[5])
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
            self.format_history(
                {
                    "background": list(reversed(background)),
                    "messages": [json.loads(row[1]) for row in rows],
                }
            ),
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
                "UPDATE group_state SET count=(SELECT count(*) FROM group_messages WHERE key=group_state.key AND own=0 AND ready=1 AND consumed=0 AND batch IS NULL),last_request=? WHERE key=?",
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
            "SELECT cutoff FROM system_messages WHERE id=? UNION ALL SELECT sequence FROM group_messages WHERE id=? LIMIT 1",
            (identifier, identifier),
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
                "UPDATE group_state SET count=(SELECT count(*) FROM group_messages WHERE key=group_state.key AND own=0 AND ready=1 AND consumed=0 AND batch IS NULL),silences=0,backoff_until=0,last_request=0 WHERE key=?",
                (key,),
            )

    def sent(self, message, receipt, parts, image=None, *, system=False, quote=None):
        if "group_id" not in message.target:
            return
        identifier = receipt.get("message_id") if isinstance(receipt, dict) else None
        if type(identifier) not in (int, str) or not str(identifier):
            raise RuntimeError("Missing QQ message receipt")
        echoed = self.db.execute(
            "SELECT event_time FROM system_messages WHERE id=?",
            (f"{message.bot_id}:{message.key}:{identifier}",),
        ).fetchone()
        sent = replace(
            message,
            identifier=f"{message.bot_id}:{message.key}:{identifier}",
            sender_id=message.bot_id,
            sender_name=message.bot_id,
            parts=parts,
            timestamp=echoed[0] if echoed and echoed[0] else time.time(),
            time_source="event" if echoed and echoed[0] else "sent",
            reply=quote,
            text="",
            images=[],
            mentioned=False,
            direct=False,
            unsupported=False,
            category="low",
            image_keys={
                "outgoing": "md5:"
                + hashlib.md5(image, usedforsecurity=False).hexdigest()
            }
            if image is not None
            else {},
        )
        if system:
            self.system(sent)
            return
        self.record(sent, sent=True)
        if quote:
            row = self.db.execute(
                "SELECT body FROM group_messages WHERE key=? AND json_extract(body,'$.message_id')=?",
                (message.key, quote),
            ).fetchone()
            quoted = json.loads(row[0]) if row else None
            if quoted is None:
                row = self.db.execute(
                    "SELECT json_extract(body,'$.quote') FROM group_messages WHERE key=? AND json_extract(body,'$.quote.message_id')=? AND json_extract(body,'$.quote.sender_id') IS NOT NULL LIMIT 1",
                    (message.key, quote),
                ).fetchone()
                quoted = json.loads(row[0]) if row else None
            if quoted:
                body = json.loads(
                    self.db.execute(
                        "SELECT body FROM group_messages WHERE id=?", (sent.identifier,)
                    ).fetchone()[0]
                )
                body["quote"] = quoted
                body["quote"].pop("quote", None)
                with self.db:
                    self.db.execute(
                        "UPDATE group_messages SET body=? WHERE id=?",
                        (json.dumps(body, ensure_ascii=False), sent.identifier),
                    )
        if image is not None:
            row = self.db.execute(
                "SELECT folder FROM sessions WHERE key=?", (message.key,)
            ).fetchone()
            folder_name = row[0] if row else uuid.uuid4().hex
            folder = self.settings.workspace_dir / folder_name
            folder.mkdir(mode=0o700, exist_ok=True)
            image_id = uuid.uuid5(
                uuid.NAMESPACE_URL, f"{sent.key}:{sent.image_keys['outgoing']}"
            ).hex
            cached = self.db.execute(
                "SELECT folder,path FROM received_images WHERE id=?", (image_id,)
            ).fetchone()
            path = (
                cached[1]
                if cached
                and cached[0] == folder_name
                and cached[1]
                and (folder / cached[1]).is_file()
                else save_image(folder, image)
            )
            with self.db:
                self.db.execute(
                    "INSERT OR IGNORE INTO sessions (key,folder) VALUES (?,?)",
                    (message.key, folder_name),
                )
                self.db.execute(
                    "UPDATE received_images SET source=NULL,folder=?,path=? WHERE id=?",
                    (folder_name, path, image_id),
                )
