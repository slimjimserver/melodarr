"""SQLite and JSON-backed persistence for Melodarr."""

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from tempfile import NamedTemporaryFile

if __package__:
    from .config import DATABASE, SETTINGS_FILE
else:  # Support the existing `python backend/app.py` entry point.
    from config import DATABASE, SETTINGS_FILE


DATABASE_BUSY_TIMEOUT_MS = 5000


@contextmanager
def db():
    """Yield a transactional SQLite connection and always close it."""
    os.makedirs(os.path.dirname(os.path.abspath(DATABASE)), exist_ok=True)
    connection = sqlite3.connect(
        DATABASE,
        timeout=DATABASE_BUSY_TIMEOUT_MS / 1000,
    )
    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA busy_timeout = {DATABASE_BUSY_TIMEOUT_MS}")
    connection.execute("PRAGMA synchronous = NORMAL")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def load_settings_file():
    """Read service configuration from its dedicated persistent JSON file."""
    if not os.path.exists(SETTINGS_FILE):
        return None
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as file:
            settings = json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read Melodarr settings file: {exc}") from exc
    if not isinstance(settings, dict):
        raise RuntimeError("Melodarr settings file must contain a JSON object.")
    return settings


def write_settings_file(settings):
    """Atomically replace settings.json so interrupted writes keep the old file."""
    directory = os.path.dirname(os.path.abspath(SETTINGS_FILE))
    os.makedirs(directory, exist_ok=True)
    with NamedTemporaryFile("w", encoding="utf-8", dir=directory, delete=False) as file:
        json.dump(settings, file, indent=2)
        file.write("\n")
        temporary_path = file.name
    try:
        os.replace(temporary_path, SETTINGS_FILE)
        try:
            os.chmod(SETTINGS_FILE, 0o600)
        except OSError:
            pass  # Some host-mounted volumes do not support Unix file modes.
    finally:
        if os.path.exists(temporary_path):
            os.unlink(temporary_path)


def get_service(service):
    """Return a configured external service, if it has object-shaped settings."""
    settings = load_settings_file() or {}
    value = settings.get(service)
    return value if isinstance(value, dict) else None


def save_service(service, values):
    """Persist settings for one external service."""
    settings = load_settings_file() or {}
    settings[service] = values
    write_settings_file(settings)


def get_request_history(user_id, limit=100):
    """Return the most recent private request-history rows for one user."""
    with db() as connection:
        return connection.execute(
            "SELECT kind, mbid, name, artist_name, release_type, release_date, "
            "created_at FROM request_history "
            "WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()


def record_request(
    user_id,
    kind,
    mbid,
    name,
    *,
    artist_name="",
    release_type="",
    release_date="",
):
    """Record an artist or release-group request for one user."""
    with db() as connection:
        connection.execute(
            "INSERT INTO request_history "
            "(user_id, kind, mbid, name, artist_name, release_type, "
            "release_date, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                user_id,
                kind,
                mbid,
                name,
                artist_name or None,
                release_type or None,
                release_date or None,
                time.time(),
            ),
        )


def enqueue_lidarr_search(
    user_id,
    mbid,
    album_id,
    artist_id,
    name,
    *,
    artist_name="",
    release_type="",
    release_date="",
):
    """Persist a refresh-then-search job and its user-visible request atomically."""
    now = time.time()
    with db() as connection:
        cursor = connection.execute(
            "INSERT OR IGNORE INTO pending_lidarr_searches "
            "(user_id, mbid, album_id, artist_id, name, refresh_type, "
            "next_attempt_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            # Briefly hold new jobs so the request transaction is committed
            # before the background worker begins processing them.
            (user_id, mbid, album_id, artist_id, name, "album", now + 1, now),
        )
        if cursor.rowcount:
            connection.execute(
                "INSERT INTO request_history "
                "(user_id, kind, mbid, name, artist_name, release_type, "
                "release_date, created_at) "
                "VALUES (?, 'release-group', ?, ?, ?, ?, ?, ?)",
                (
                    user_id,
                    mbid,
                    name,
                    artist_name or None,
                    release_type or None,
                    release_date or None,
                    now,
                ),
            )
        return bool(cursor.rowcount)


def pending_lidarr_search(mbid):
    with db() as connection:
        return connection.execute(
            "SELECT * FROM pending_lidarr_searches WHERE mbid = ?", (mbid,)
        ).fetchone()


def due_lidarr_searches(limit=20):
    with db() as connection:
        return connection.execute(
            "SELECT * FROM pending_lidarr_searches WHERE next_attempt_at <= ? "
            "ORDER BY created_at LIMIT ?",
            (time.time(), limit),
        ).fetchall()


def set_lidarr_refresh_command(job_ids, command_id):
    """Attach one metadata-refresh command to an exact batch of jobs."""
    job_ids = list(job_ids)
    if not job_ids:
        return
    placeholders = ", ".join("?" for _ in job_ids)
    with db() as connection:
        connection.execute(
            "UPDATE pending_lidarr_searches SET refresh_command_id = ?, "
            f"attempts = 0, last_error = NULL, next_attempt_at = ? "
            f"WHERE id IN ({placeholders})",
            (command_id, time.time(), *job_ids),
        )


def set_lidarr_search_command(job_id, command_id):
    with db() as connection:
        connection.execute(
            "UPDATE pending_lidarr_searches SET search_command_id = ?, "
            "last_error = NULL, next_attempt_at = ? WHERE id = ?",
            (command_id, time.time(), job_id),
        )


def defer_lidarr_search(job_id, error, reset_refresh=False):
    """Retry transient Lidarr work with bounded exponential backoff."""
    with db() as connection:
        row = connection.execute(
            "SELECT attempts, refresh_command_id "
            "FROM pending_lidarr_searches WHERE id = ?",
            (job_id,),
        ).fetchone()
        if not row:
            return
        attempts = row["attempts"] + 1
        delay = min(5 * (2 ** min(attempts - 1, 6)), 300)
        refresh_command_id = None if reset_refresh else row["refresh_command_id"]
        connection.execute(
            "UPDATE pending_lidarr_searches SET refresh_command_id = ?, attempts = ?, "
            "last_error = ?, next_attempt_at = ? WHERE id = ?",
            (refresh_command_id, attempts, str(error)[:500], time.time() + delay, job_id),
        )


def schedule_lidarr_search_poll(job_id, delay=2):
    with db() as connection:
        connection.execute(
            "UPDATE pending_lidarr_searches SET next_attempt_at = ? WHERE id = ?",
            (time.time() + delay, job_id),
        )


def complete_lidarr_search(job_id):
    with db() as connection:
        connection.execute("DELETE FROM pending_lidarr_searches WHERE id = ?", (job_id,))


def recommendation_users():
    """Return the user fields needed to assemble recommendation caches."""
    with db() as connection:
        return connection.execute(
            "SELECT id, username, listenbrainz_username, lastfm_username, lastfm_api_key FROM users"
        ).fetchall()


def get_recommendation_cache(user_id):
    """Return one user's current recommendation payload and refresh time."""
    with db() as connection:
        return connection.execute(
            "SELECT value, refreshed_at FROM recommendation_cache WHERE user_id = ?",
            (user_id,),
        ).fetchone()


def save_recommendation_cache(user_id, value):
    """Replace one user's assembled recommendation cache."""
    with db() as connection:
        connection.execute(
            "INSERT OR REPLACE INTO recommendation_cache (user_id, value, refreshed_at) "
            "VALUES (?, ?, ?)",
            (
                user_id,
                json.dumps(value, ensure_ascii=False, separators=(",", ":")),
                time.time(),
            ),
        )


def delete_recommendation_cache(user_id):
    """Invalidate one user's assembled cache after their recommendation inputs change."""
    with db() as connection:
        connection.execute(
            "DELETE FROM recommendation_cache WHERE user_id = ?",
            (user_id,),
        )


def recommendation_cache_stats():
    """Summarize assembled per-user recommendation payloads."""
    with db() as connection:
        row = connection.execute(
            "SELECT COUNT(*) AS entries, "
            "COALESCE(SUM(LENGTH(CAST(value AS BLOB))), 0) AS value_bytes, "
            "MIN(refreshed_at) AS oldest_refresh, MAX(refreshed_at) AS newest_refresh "
            "FROM recommendation_cache"
        ).fetchone()
    return dict(row)


def clear_recommendation_cache():
    """Invalidate assembled recommendations for every user."""
    with db() as connection:
        cursor = connection.execute("DELETE FROM recommendation_cache")
        return cursor.rowcount


def pending_lidarr_search_stats():
    """Summarize durable Lidarr follow-up work for the admin jobs page."""
    with db() as connection:
        row = connection.execute(
            "SELECT COUNT(*) AS queued, MIN(next_attempt_at) AS next_attempt, "
            "COALESCE(SUM(CASE WHEN last_error IS NOT NULL THEN 1 ELSE 0 END), 0) "
            "AS retrying FROM pending_lidarr_searches"
        ).fetchone()
    return dict(row)


def init_db():
    """Create current tables and migrate legacy service settings to JSON."""
    legacy_settings = {}
    with db() as connection:
        # WAL lets request threads read account and queue state while a
        # background worker commits unrelated updates.
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('admin', 'user')),
                listenbrainz_username TEXT,
                lastfm_username TEXT,
                lastfm_api_key TEXT,
                created_at REAL NOT NULL
            )
        """)
        user_columns = {row["name"] for row in connection.execute("PRAGMA table_info(users)")}
        if "listenbrainz_username" not in user_columns:
            connection.execute("ALTER TABLE users ADD COLUMN listenbrainz_username TEXT")
        if "lastfm_username" not in user_columns:
            connection.execute("ALTER TABLE users ADD COLUMN lastfm_username TEXT")
        if "lastfm_api_key" not in user_columns:
            connection.execute("ALTER TABLE users ADD COLUMN lastfm_api_key TEXT")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS request_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id),
                kind TEXT NOT NULL CHECK(kind IN ('artist', 'release-group')),
                mbid TEXT NOT NULL,
                name TEXT NOT NULL,
                artist_name TEXT,
                release_type TEXT,
                release_date TEXT,
                created_at REAL NOT NULL
            )
        """)
        request_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(request_history)")
        }
        for column in ("artist_name", "release_type", "release_date"):
            if column not in request_columns:
                connection.execute(
                    f"ALTER TABLE request_history ADD COLUMN {column} TEXT"
                )
        connection.execute("""
            CREATE TABLE IF NOT EXISTS recommendation_cache (
                user_id INTEGER PRIMARY KEY REFERENCES users(id),
                value TEXT NOT NULL,
                refreshed_at REAL NOT NULL
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS account_invitations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token_hash TEXT NOT NULL UNIQUE,
                created_by INTEGER NOT NULL REFERENCES users(id),
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                used_at REAL
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS pending_lidarr_searches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id),
                mbid TEXT NOT NULL UNIQUE,
                album_id INTEGER NOT NULL,
                artist_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                refresh_type TEXT NOT NULL DEFAULT 'album'
                    CHECK(refresh_type IN ('artist', 'album')),
                refresh_command_id INTEGER,
                search_command_id INTEGER,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL NOT NULL,
                last_error TEXT,
                created_at REAL NOT NULL
            )
        """)
        search_columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(pending_lidarr_searches)"
            )
        }
        if "refresh_type" not in search_columns:
            connection.execute(
                "ALTER TABLE pending_lidarr_searches ADD COLUMN "
                "refresh_type TEXT NOT NULL DEFAULT 'album'"
            )
        # Release-group requests always use RefreshAlbum. Convert work queued
        # by versions that conditionally selected RefreshArtist as well.
        connection.execute(
            "UPDATE pending_lidarr_searches SET refresh_type = 'album' "
            "WHERE refresh_type != 'album'"
        )
        has_legacy_settings = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'settings'"
        ).fetchone()
        if has_legacy_settings:
            legacy_settings = {
                row["service"]: json.loads(row["value"])
                for row in connection.execute("SELECT service, value FROM settings")
            }

    settings = load_settings_file()
    if settings is None:
        write_settings_file(legacy_settings)

    # The JSON file is safely written before removing the old table, so an
    # upgrade retains existing configurations without leaving credentials in
    # the database.
    if has_legacy_settings:
        with db() as connection:
            connection.execute("DROP TABLE settings")
