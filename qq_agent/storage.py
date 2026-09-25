import sqlite3


def open_database(settings):
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    settings.workspace_dir.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(settings.state_dir / "sessions.sqlite")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (key TEXT PRIMARY KEY, thread TEXT, folder TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS messages (id TEXT PRIMARY KEY, status TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS models (key TEXT PRIMARY KEY, model TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS activity (key TEXT PRIMARY KEY, received REAL NOT NULL, message_gen INTEGER NOT NULL, thread_gen INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS context_usage (thread TEXT PRIMARY KEY, tokens INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS member_profiles (group_id TEXT NOT NULL, user_id TEXT NOT NULL, display_name TEXT NOT NULL, profile TEXT NOT NULL, updated_at REAL NOT NULL, PRIMARY KEY (group_id, user_id));
        CREATE TABLE IF NOT EXISTS generated_images (sequence INTEGER PRIMARY KEY, folder TEXT NOT NULL, item_id TEXT NOT NULL, path TEXT NOT NULL, size INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS image_deliveries (folder TEXT NOT NULL, path TEXT NOT NULL, digest TEXT NOT NULL, turn TEXT NOT NULL, status TEXT NOT NULL, PRIMARY KEY (folder, path, digest));
        UPDATE messages SET status='interrupted' WHERE status IN ('queued', 'running');
    """)
    if "renew" not in {row[1] for row in db.execute("PRAGMA table_info(sessions)")}:
        with db:
            db.execute(
                "ALTER TABLE sessions ADD COLUMN renew INTEGER NOT NULL DEFAULT 0"
            )
    return db
