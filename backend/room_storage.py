"""Additive, idempotent Rooms migration used by the existing init_db runner."""


def migrate(connection):
    statements = (
        """CREATE TABLE IF NOT EXISTS rooms (
            id TEXT PRIMARY KEY, code TEXT NOT NULL UNIQUE,
            host_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            status TEXT NOT NULL CHECK(status IN ('active','closed')),
            created_at REAL NOT NULL, closed_at REAL,
            server_id TEXT NOT NULL, client_id TEXT NOT NULL, session_key TEXT NOT NULL,
            queue_id TEXT NOT NULL, current_item_id TEXT NOT NULL,
            next_item_id TEXT NOT NULL DEFAULT '', up_next TEXT NOT NULL DEFAULT '{}',
            handoff_item_id TEXT NOT NULL, trim_ids TEXT NOT NULL DEFAULT '[]',
            now_playing TEXT NOT NULL DEFAULT '{}', handoff TEXT NOT NULL DEFAULT '{}',
            playback_state TEXT NOT NULL DEFAULT 'playing',
            warning INTEGER NOT NULL DEFAULT 0, sync_error TEXT,
            dirty INTEGER NOT NULL DEFAULT 1, version INTEGER NOT NULL DEFAULT 1)""",
        "CREATE UNIQUE INDEX IF NOT EXISTS rooms_active_host ON rooms(host_user_id) WHERE status='active'",
        "CREATE UNIQUE INDEX IF NOT EXISTS rooms_active_queue ON rooms(server_id,queue_id) WHERE status='active'",
        """CREATE TABLE IF NOT EXISTS room_guests (
            id TEXT PRIMARY KEY, room_id TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
            token_hash TEXT NOT NULL UNIQUE, csrf_token TEXT NOT NULL, name TEXT NOT NULL,
            created_at REAL NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS room_guests_room ON room_guests(room_id)",
        """CREATE TABLE IF NOT EXISTS room_choices (
            id TEXT PRIMARY KEY, room_id TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
            recording_mbid TEXT NOT NULL, title TEXT NOT NULL, artist TEXT NOT NULL,
            release_group_mbid TEXT, expires_at REAL NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS room_choices_room ON room_choices(room_id,expires_at)",
        """CREATE TABLE IF NOT EXISTS room_entries (
            id TEXT PRIMARY KEY, room_id TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
            position INTEGER NOT NULL, recording_mbid TEXT NOT NULL,
            title TEXT NOT NULL, artist TEXT NOT NULL, release_group_mbid TEXT,
            requester TEXT NOT NULL, guest_id TEXT REFERENCES room_guests(id),
            created_at REAL NOT NULL, state TEXT NOT NULL DEFAULT 'requested',
            playback TEXT NOT NULL DEFAULT 'upcoming' CHECK(playback IN ('upcoming','playing','played')),
            rating_key TEXT, queue_item_id TEXT, add_before TEXT,
            removed INTEGER NOT NULL DEFAULT 0, error TEXT)""",
        "CREATE INDEX IF NOT EXISTS room_entries_order ON room_entries(room_id,position)",
        """CREATE TABLE IF NOT EXISTS room_rate_limits (
            identity TEXT NOT NULL, action TEXT NOT NULL, window INTEGER NOT NULL,
            count INTEGER NOT NULL, PRIMARY KEY(identity,action,window))""",
    )
    for statement in statements:
        connection.execute(statement)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(rooms)")}
    for name, default in (("next_item_id", "''"), ("up_next", "'{}'")):
        if name not in columns:
            connection.execute(
                f"ALTER TABLE rooms ADD COLUMN {name} TEXT NOT NULL DEFAULT {default}"
            )
