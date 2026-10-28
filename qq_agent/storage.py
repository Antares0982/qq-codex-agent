import json
import sqlite3


def open_database(settings):
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    settings.workspace_dir.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(settings.state_dir / "sessions.sqlite")
    legacy = db.execute("SELECT 1 FROM sqlite_master WHERE name='sessions'").fetchone()
    upgraded = db.execute("PRAGMA user_version").fetchone()[0] >= 2
    if legacy and not upgraded:
        with sqlite3.connect(
            settings.state_dir / "sessions.pre-group-v2.sqlite"
        ) as backup:
            db.backup(backup)
    cadence_upgrade = db.execute("PRAGMA user_version").fetchone()[0] < 3
    if (
        legacy
        and cadence_upgrade
        and not (settings.state_dir / "sessions.pre-cadence-v3.sqlite").exists()
    ):
        with sqlite3.connect(
            settings.state_dir / "sessions.pre-cadence-v3.sqlite"
        ) as backup:
            db.backup(backup)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (key TEXT PRIMARY KEY, thread TEXT, folder TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS messages (id TEXT PRIMARY KEY, status TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS models (key TEXT PRIMARY KEY, model TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS activity (key TEXT PRIMARY KEY, received REAL NOT NULL, message_gen INTEGER NOT NULL, thread_gen INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS context_usage (thread TEXT PRIMARY KEY, tokens INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS group_snapshots (thread TEXT PRIMARY KEY, instructions TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS group_handoffs (group_id TEXT PRIMARY KEY, summary TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS member_profiles (group_id TEXT NOT NULL, user_id TEXT NOT NULL, display_name TEXT NOT NULL, profile TEXT NOT NULL, updated_at REAL NOT NULL, PRIMARY KEY (group_id, user_id));
        CREATE TABLE IF NOT EXISTS group_prompts (group_id TEXT PRIMARY KEY, prompt TEXT NOT NULL, updated_by TEXT NOT NULL, updated_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS group_profiles (group_id TEXT PRIMARY KEY, profile TEXT NOT NULL, updated_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS generated_images (sequence INTEGER PRIMARY KEY, folder TEXT NOT NULL, item_id TEXT NOT NULL, path TEXT NOT NULL, size INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS image_deliveries (folder TEXT NOT NULL, path TEXT NOT NULL, digest TEXT NOT NULL, turn TEXT NOT NULL, status TEXT NOT NULL, PRIMARY KEY (folder, path, digest));
        CREATE TABLE IF NOT EXISTS group_state (key TEXT PRIMARY KEY, total INTEGER NOT NULL DEFAULT 0, own INTEGER NOT NULL DEFAULT 0, last_request REAL NOT NULL DEFAULT 0, count INTEGER NOT NULL DEFAULT 0, members INTEGER);
        CREATE TABLE IF NOT EXISTS group_messages (sequence INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL, key TEXT NOT NULL, body TEXT NOT NULL, own INTEGER NOT NULL, created REAL NOT NULL, batch TEXT, consumed INTEGER NOT NULL DEFAULT 0);
        CREATE INDEX IF NOT EXISTS group_pending ON group_messages(key, consumed, batch);
        CREATE TABLE IF NOT EXISTS group_batches (id TEXT PRIMARY KEY, key TEXT NOT NULL, message TEXT NOT NULL, status TEXT NOT NULL, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS received_images (id TEXT PRIMARY KEY, key TEXT NOT NULL, source TEXT, folder TEXT, path TEXT, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS system_messages (id TEXT PRIMARY KEY, key TEXT NOT NULL, own INTEGER NOT NULL, cutoff INTEGER NOT NULL, created REAL NOT NULL, event_time REAL);
        UPDATE group_batches SET status='unknown' WHERE status='submitting';
        UPDATE group_batches SET status='interrupted' WHERE status='submitted';
        UPDATE group_messages SET consumed=1 WHERE batch IN (SELECT id FROM group_batches WHERE status IN ('unknown','interrupted'));
        UPDATE messages SET status='interrupted' WHERE status IN ('queued', 'running');
    """)
    if "renew" not in {row[1] for row in db.execute("PRAGMA table_info(sessions)")}:
        with db:
            db.execute(
                "ALTER TABLE sessions ADD COLUMN renew INTEGER NOT NULL DEFAULT 0"
            )
    if cadence_upgrade:
        with db:
            db.execute("BEGIN")
            for table, columns in {
                "group_state": {
                    "last_arrival": "REAL NOT NULL DEFAULT 0",
                    "segment": "REAL NOT NULL DEFAULT 0",
                    "last_observation": "REAL NOT NULL DEFAULT 0",
                    "silences": "INTEGER NOT NULL DEFAULT 0",
                    "backoff_until": "REAL NOT NULL DEFAULT 0",
                },
                "group_messages": {
                    "message": "TEXT",
                    "ready": "INTEGER NOT NULL DEFAULT 1",
                    "priority": "INTEGER NOT NULL DEFAULT 1",
                    "segment": "REAL NOT NULL DEFAULT 0",
                },
                "group_batches": {
                    "payload": "TEXT",
                    "priority": "INTEGER NOT NULL DEFAULT 1",
                    "segment": "REAL NOT NULL DEFAULT 0",
                },
            }.items():
                for name, declaration in columns.items():
                    db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
            # Preserve uncertain legacy interactions.
            for identifier, key, body in db.execute(
                "SELECT id,key,body FROM group_messages WHERE own=0 AND consumed=0 AND batch IS NULL"
            ).fetchall():
                value = json.loads(body)
                bot = identifier.split(":", 1)[0]
                mentioned = any(
                    part == {"at": bot}
                    or (isinstance(part, str) and part.startswith(f"@({bot})"))
                    for part in value["content"]
                )
                message = dict(
                    key=key,
                    identifier=identifier,
                    target={"group_id": int(key[6:])},
                    sender_name=value["sender_name"],
                    sender_id=value["sender_id"],
                    text="",
                    parts=[],
                    images=[],
                    reply=None,
                    unsupported=False,
                    bot_id=bot,
                    mentioned=mentioned,
                    direct=mentioned or "quote" in value,
                )
                db.execute(
                    "UPDATE group_messages SET message=?,priority=? WHERE id=?",
                    (json.dumps(message), int(message["direct"]), identifier),
                )
            for identifier, raw in db.execute(
                "SELECT id,message FROM group_batches"
            ).fetchall():
                message = json.loads(raw)
                message["direct"] = bool(
                    message.get("mentioned") or message.get("reply")
                )
                db.execute(
                    "UPDATE group_batches SET message=?,priority=? WHERE id=?",
                    (json.dumps(message), int(message["direct"]), identifier),
                )
            db.execute(
                "UPDATE group_state SET last_arrival=coalesce((SELECT max(created) FROM group_messages WHERE key=group_state.key AND own=0),0)"
            )
            db.execute("UPDATE group_state SET segment=last_arrival")
            db.execute(
                "UPDATE group_messages SET segment=coalesce((SELECT segment FROM group_state WHERE key=group_messages.key),0)"
            )
            db.execute(
                "UPDATE group_batches SET segment=coalesce((SELECT segment FROM group_state WHERE key=group_batches.key),0)"
            )
            db.execute("UPDATE group_messages SET consumed=1 WHERE own=1")
            if legacy and not upgraded:
                db.execute("UPDATE sessions SET renew=1 WHERE key LIKE 'group-%'")
                db.execute("DELETE FROM group_handoffs")
            db.execute("PRAGMA user_version=3")
    if "category" not in {
        row[1] for row in db.execute("PRAGMA table_info(group_messages)")
    }:
        with db:
            db.execute(
                "ALTER TABLE group_messages ADD COLUMN category TEXT NOT NULL DEFAULT 'normal'"
            )
            db.execute("UPDATE group_messages SET category='low' WHERE own=1")
            db.execute("PRAGMA user_version=4")
    return db
