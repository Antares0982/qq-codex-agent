import json
import time
import uuid
from dataclasses import asdict, replace
from datetime import datetime, timezone

from openai_codex import TextInput

from image_assets import save_image
from .logging import LOG, log_text
from .messages import Message, display_name, parse_parts


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
            self.db.execute(
                "INSERT INTO group_messages (id,key,body,own,created,consumed) VALUES (?,?,?,?,?,?)",
                (
                    message.identifier,
                    message.key,
                    json.dumps(body, ensure_ascii=False),
                    own,
                    time.time(),
                    command,
                ),
            )
            self.db.execute(
                "INSERT OR IGNORE INTO group_state (key) VALUES (?)", (message.key,)
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

    def trigger(self, message, quoted_bot):
        readable = any(
            kind in {"at", "image"} or (kind == "text" and value.strip())
            for kind, value in message.parts
        )
        if not readable and not quoted_bot:
            return False
        config = self.settings.groups[str(message.target["group_id"])]
        last = self.db.execute(
            "SELECT last_request FROM group_state WHERE key=?", (message.key,)
        ).fetchone()[0]
        count = self.db.execute(
            "SELECT count(*) FROM group_messages WHERE key=? AND own=0 AND consumed=0 AND batch IS NULL AND sequence <= (SELECT sequence FROM group_messages WHERE id=?)",
            (message.key, message.identifier),
        ).fetchone()[0]
        available = self.db.execute(
            "SELECT consumed=0 AND batch IS NULL FROM group_messages WHERE id=?",
            (message.identifier,),
        ).fetchone()
        if not available or not available[0]:
            return False
        return (
            message.mentioned
            or quoted_bot
            or time.time() - last >= config.request_interval_minutes * 60
            or count >= config.request_message_count
        )

    def freeze(self, message):
        now = time.time()
        row = self.db.execute(
            "SELECT received,message_gen,thread_gen FROM activity WHERE key=?",
            (message.key,),
        ).fetchone()
        generation = (row[1] + int(now - row[0] > 7200)) if row else 0
        batch = replace(message, batch=uuid.uuid4().hex, generation=generation)
        batch.identifier = f"{message.bot_id}:{message.key}:batch-{batch.batch}"
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO activity VALUES (?,?,?,?)",
                (message.key, now, generation, row[2] if row else 0),
            )
            self.db.execute(
                "INSERT INTO group_batches VALUES (?,?,?,'pending',?)",
                (batch.batch, batch.key, json.dumps(asdict(batch)), time.time()),
            )
            self.db.execute(
                "UPDATE group_messages SET batch=? WHERE key=? AND batch IS NULL AND consumed=0 AND sequence <= (SELECT sequence FROM group_messages WHERE id=?)",
                (batch.batch, batch.key, message.identifier),
            )
            self.db.execute(
                "UPDATE group_state SET count=(SELECT count(*) FROM group_messages WHERE key=group_state.key AND own=0 AND consumed=0 AND batch IS NULL),last_request=? WHERE key=?",
                (time.time(), batch.key),
            )
            self.db.execute(
                "INSERT INTO messages VALUES (?,'received')", (batch.identifier,)
            )
        return batch

    def pending(self, key):
        return [
            Message(**json.loads(row[0]))
            for row in self.db.execute(
                "SELECT message FROM group_batches WHERE key=? AND status='pending' ORDER BY created,rowid",
                (key,),
            )
        ]

    def input(self, message):
        rows = self.db.execute(
            "SELECT body FROM group_messages WHERE batch=? ORDER BY sequence",
            (message.batch,),
        )
        return [
            TextInput(
                "群聊消息记录：\n"
                + json.dumps([json.loads(row[0]) for row in rows], ensure_ascii=False)
            )
        ]

    def status(self, message, status):
        with self.db:
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
                "UPDATE group_state SET count=(SELECT count(*) FROM group_messages WHERE key=group_state.key AND own=0 AND consumed=0 AND batch IS NULL),last_request=0 WHERE key=?",
                (key,),
            )

    async def instructions(self, message):
        try:
            info = await self.bot.call(
                "get_group_info", {"group_id": message.target["group_id"]}
            )
            members = info.get("member_count")
            if type(members) is int and members > 0:
                with self.db:
                    self.db.execute(
                        "UPDATE group_state SET members=? WHERE key=?",
                        (members, message.key),
                    )
        except Exception as error:
            LOG.warning(
                "Group count unavailable chat=%s error=%s", message.key, log_text(error)
            )
        row = self.db.execute(
            "SELECT total,own,members FROM group_state WHERE key=?", (message.key,)
        ).fetchone()
        ratio = self.settings.groups[
            str(message.target["group_id"])
        ].participation_ratio
        if row and row[0] and row[2] and row[1] / row[0] < 1 / (row[2] * ratio):
            return "你在群聊中的参与度偏低，可以在合适时自然参与。"
        return ""

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
