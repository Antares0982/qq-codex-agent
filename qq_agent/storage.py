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
    with db:
        if legacy and not upgraded:
            db.execute("UPDATE sessions SET renew=1 WHERE key LIKE 'group-%'")
            db.execute("DELETE FROM group_handoffs")
        db.execute("PRAGMA user_version=2")
    return db
