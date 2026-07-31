"""SQLite and JSON-backed persistence for Melodarr."""

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from tempfile import NamedTemporaryFile
from threading import Lock

if __package__:
    from .config import DATABASE, SETTINGS_FILE
else:  # Support the existing `python backend/app.py` entry point.
    from config import DATABASE, SETTINGS_FILE


DATABASE_BUSY_TIMEOUT_MS = 5000
_settings_lock = Lock()


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
    connection.execute("PRAGMA foreign_keys = ON")
    if not connection.execute("PRAGMA foreign_keys").fetchone()[0]:
        connection.close()
        raise RuntimeError("SQLite foreign-key enforcement could not be enabled.")
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


def get_lastfm_api_key():
    """Return the shared administrator-managed Last.fm API key."""
    config = get_service("lastfm") or {}
    return str(config.get("apiKey") or "").strip()


def save_service(service, values):
    """Persist settings for one external service."""
    # The production server handles requests on multiple threads. Serialize
    # the read-modify-write sequence so two unrelated service updates cannot
    # each replace the file with a snapshot that omits the other update.
    with _settings_lock:
        settings = load_settings_file() or {}
        settings[service] = values
        write_settings_file(settings)


def get_request_history(user_id, limit=100, offset=0):
    """Return the most recent private request-history rows for one user."""
    with db() as connection:
        return connection.execute(
            "SELECT kind, mbid, name, artist_name, release_type, release_date, "
            "created_at FROM request_history "
            "WHERE user_id = ? ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            (user_id, limit, offset),
        ).fetchall()


def count_request_history(user_id):
    """Return the number of private request-history rows for one user."""
    with db() as connection:
        return connection.execute(
            "SELECT COUNT(*) AS total FROM request_history WHERE user_id = ?",
            (user_id,),
        ).fetchone()["total"]


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
        if kind == "release-group":
            connection.execute(
                "INSERT OR IGNORE INTO pending_lidarr_search_requesters "
                "(job_id, user_id) "
                "SELECT id, ? FROM pending_lidarr_searches WHERE mbid = ?",
                (user_id, mbid),
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
            "(mbid, album_id, artist_id, name, refresh_type, "
            "next_attempt_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            # Briefly hold new jobs so the request transaction is committed
            # before the background worker begins processing them.
            (mbid, album_id, artist_id, name, "album", now + 1, now),
        )
        job_id = connection.execute(
            "SELECT id FROM pending_lidarr_searches WHERE mbid = ?",
            (mbid,),
        ).fetchone()["id"]
        connection.execute(
            "INSERT OR IGNORE INTO pending_lidarr_search_requesters "
            "(job_id, user_id) VALUES (?, ?)",
            (job_id, user_id),
        )
        # The queue is shared across users by release-group MBID, while
        # request history is private per user. Even when another request
        # already created the shared job, retain this user's action.
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
            "SELECT id, username, listenbrainz_username, lastfm_username, plex_id "
            "FROM users"
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
        cursor = connection.execute(
            "INSERT OR REPLACE INTO recommendation_cache "
            "(user_id, value, refreshed_at) "
            "SELECT ?, ?, ? WHERE EXISTS "
            "(SELECT 1 FROM users WHERE id = ?)",
            (
                user_id,
                json.dumps(value, ensure_ascii=False, separators=(",", ":")),
                time.time(),
                user_id,
            ),
        )
        return bool(cursor.rowcount)


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


def listening_profile_users():
    """Return only the fields needed by the private profile refresh worker."""
    with db() as connection:
        return connection.execute(
            "SELECT id, listenbrainz_username, lastfm_username, plex_id "
            "FROM users ORDER BY id"
        ).fetchall()


def get_listening_profile(user_id):
    """Return one user's durable listening profile without exposing another."""
    with db() as connection:
        return connection.execute(
            "SELECT value, refreshed_at, last_attempted_at, last_error "
            "FROM listening_profiles WHERE user_id = ?",
            (user_id,),
        ).fetchone()


def save_listening_profile(
    user_id,
    value,
    *,
    refreshed_at=None,
    last_attempted_at=None,
    last_error=None,
):
    """Atomically replace one user's profile after a complete build pass."""
    refreshed_at = time.time() if refreshed_at is None else float(refreshed_at)
    last_attempted_at = (
        refreshed_at if last_attempted_at is None else float(last_attempted_at)
    )
    with db() as connection:
        cursor = connection.execute(
            "INSERT OR REPLACE INTO listening_profiles "
            "(user_id, value, refreshed_at, last_attempted_at, last_error) "
            "SELECT ?, ?, ?, ?, ? WHERE EXISTS "
            "(SELECT 1 FROM users WHERE id = ?)",
            (
                user_id,
                json.dumps(value, ensure_ascii=False, separators=(",", ":")),
                refreshed_at,
                last_attempted_at,
                str(last_error)[:500] if last_error else None,
                user_id,
            ),
        )
        return bool(cursor.rowcount)


def record_listening_profile_failure(user_id, error, *, attempted_at=None):
    """Record a failed pass while retaining the last known-good profile value."""
    with db() as connection:
        connection.execute(
            "UPDATE listening_profiles SET last_attempted_at = ?, last_error = ? "
            "WHERE user_id = ?",
            (
                time.time() if attempted_at is None else float(attempted_at),
                str(error)[:500],
                user_id,
            ),
        )


def delete_listening_profile(user_id):
    """Remove profile data when a user is removed or explicitly invalidated."""
    with db() as connection:
        cursor = connection.execute(
            "DELETE FROM listening_profiles WHERE user_id = ?",
            (user_id,),
        )
        return cursor.rowcount


def clear_listening_profiles():
    """Remove all profiles after a deliberate shared-provider reconfiguration."""
    with db() as connection:
        cursor = connection.execute("DELETE FROM listening_profiles")
        return cursor.rowcount


def listening_profile_stats():
    """Summarize durable profiles without returning any private taste data."""
    with db() as connection:
        row = connection.execute(
            "SELECT COUNT(*) AS entries, "
            "COALESCE(SUM(LENGTH(CAST(value AS BLOB))), 0) AS value_bytes, "
            "MIN(refreshed_at) AS oldest_refresh, "
            "MAX(refreshed_at) AS newest_refresh, "
            "COALESCE(SUM(CASE WHEN last_error IS NOT NULL THEN 1 ELSE 0 END), 0) "
            "AS errors FROM listening_profiles"
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


def insert_plex_listens(listens):
    """Insert normalized Plex play events, ignoring previously imported history."""
    values = [
        (
            listen["server_id"],
            listen["history_key"],
            listen["user_id"],
            listen["artist_rating_key"],
            listen.get("album_rating_key"),
            listen["played_at"],
            listen["user_id"],
        )
        for listen in listens
    ]
    if not values:
        return 0
    with db() as connection:
        cursor = connection.executemany(
            "INSERT OR IGNORE INTO plex_listens "
            "(server_id, history_key, user_id, artist_rating_key, "
            "album_rating_key, played_at) "
            "SELECT ?, ?, ?, ?, ?, ? WHERE EXISTS "
            "(SELECT 1 FROM users WHERE id = ?)",
            values,
        )
        return cursor.rowcount


def get_plex_listens(user_id, since, *, server_id=None):
    """Return one user's resolvable Plex play keys within a rolling window."""
    query = (
        "SELECT server_id, history_key, user_id, artist_rating_key, "
        "album_rating_key, played_at FROM plex_listens "
        "WHERE user_id = ? AND played_at >= ?"
    )
    parameters = [user_id, since]
    if server_id is not None:
        query += " AND server_id = ?"
        parameters.append(server_id)
    query += " ORDER BY played_at DESC, id DESC"
    with db() as connection:
        return connection.execute(query, parameters).fetchall()


def prune_plex_listens(before):
    """Delete Plex play events older than the rolling retention cutoff."""
    with db() as connection:
        cursor = connection.execute(
            "DELETE FROM plex_listens WHERE played_at < ?",
            (before,),
        )
        return cursor.rowcount


def plex_listen_stats(*, user_id=None, server_id=None):
    """Summarize stored Plex play events for jobs and diagnostics."""
    conditions = []
    parameters = []
    if user_id is not None:
        conditions.append("user_id = ?")
        parameters.append(user_id)
    if server_id is not None:
        conditions.append("server_id = ?")
        parameters.append(server_id)
    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    with db() as connection:
        row = connection.execute(
            "SELECT COUNT(*) AS count, COUNT(DISTINCT user_id) AS users, "
            "MIN(played_at) AS oldest_played_at, "
            "MAX(played_at) AS newest_played_at "
            f"FROM plex_listens{where}",
            parameters,
        ).fetchone()
    return dict(row)


def delete_plex_listens(user_id):
    """Delete all imported Plex listening history owned by one user."""
    with db() as connection:
        cursor = connection.execute(
            "DELETE FROM plex_listens WHERE user_id = ?",
            (user_id,),
        )
        return cursor.rowcount


def _create_pending_lidarr_searches_table(connection):
    connection.execute("""
        CREATE TABLE IF NOT EXISTS pending_lidarr_searches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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


def _migrate_pending_lidarr_searches(connection):
    """Separate shared queue work from the users who requested it."""
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type = 'table' AND name = 'pending_lidarr_searches'"
    ).fetchone()
    legacy_table = None
    if exists:
        columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(pending_lidarr_searches)"
            )
        }
        if "refresh_type" not in columns:
            connection.execute(
                "ALTER TABLE pending_lidarr_searches ADD COLUMN "
                "refresh_type TEXT NOT NULL DEFAULT 'album'"
            )
            columns.add("refresh_type")
        if "user_id" in columns:
            legacy_table = "pending_lidarr_searches_legacy"
            connection.execute(
                "ALTER TABLE pending_lidarr_searches "
                f"RENAME TO {legacy_table}"
            )
            _create_pending_lidarr_searches_table(connection)
            connection.execute(
                "INSERT INTO pending_lidarr_searches "
                "(id, mbid, album_id, artist_id, name, refresh_type, "
                "refresh_command_id, search_command_id, attempts, "
                "next_attempt_at, last_error, created_at) "
                "SELECT id, mbid, album_id, artist_id, name, refresh_type, "
                "refresh_command_id, search_command_id, attempts, "
                f"next_attempt_at, last_error, created_at FROM {legacy_table}"
            )
    else:
        _create_pending_lidarr_searches_table(connection)

    connection.execute("""
        CREATE TABLE IF NOT EXISTS pending_lidarr_search_requesters (
            job_id INTEGER NOT NULL
                REFERENCES pending_lidarr_searches(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL
                REFERENCES users(id) ON DELETE CASCADE,
            PRIMARY KEY(job_id, user_id)
        )
    """)
    if legacy_table:
        connection.execute(
            "INSERT OR IGNORE INTO pending_lidarr_search_requesters "
            "(job_id, user_id) "
            f"SELECT legacy.id, legacy.user_id FROM {legacy_table} AS legacy "
            "JOIN users ON users.id = legacy.user_id"
        )
    # Older releases retained every requester's private history even though the
    # queue row named only its first requester. Recover all of those associations.
    connection.execute(
        "INSERT OR IGNORE INTO pending_lidarr_search_requesters "
        "(job_id, user_id) "
        "SELECT jobs.id, history.user_id "
        "FROM pending_lidarr_searches AS jobs "
        "JOIN request_history AS history "
        "ON history.kind = 'release-group' AND history.mbid = jobs.mbid "
        "JOIN users ON users.id = history.user_id"
    )
    connection.execute(
        "DELETE FROM pending_lidarr_searches "
        "WHERE NOT EXISTS ("
        "SELECT 1 FROM pending_lidarr_search_requesters AS requesters "
        "WHERE requesters.job_id = pending_lidarr_searches.id)"
    )
    if legacy_table:
        connection.execute(f"DROP TABLE {legacy_table}")


def _delete_legacy_orphans(connection):
    """Remove rows written before every connection enforced foreign keys."""
    for table, column in (
        ("plex_auth_flows", "user_id"),
        ("request_history", "user_id"),
        ("recommendation_cache", "user_id"),
        ("listening_profiles", "user_id"),
        ("plex_listens", "user_id"),
        ("account_invitations", "created_by"),
    ):
        connection.execute(
            f"DELETE FROM {table} WHERE {column} IS NOT NULL "
            f"AND NOT EXISTS (SELECT 1 FROM users WHERE id = {table}.{column})"
        )
    connection.execute(
        "DELETE FROM pending_lidarr_search_requesters "
        "WHERE NOT EXISTS (SELECT 1 FROM users "
        "WHERE users.id = pending_lidarr_search_requesters.user_id) "
        "OR NOT EXISTS (SELECT 1 FROM pending_lidarr_searches "
        "WHERE pending_lidarr_searches.id = "
        "pending_lidarr_search_requesters.job_id)"
    )
    connection.execute(
        "DELETE FROM pending_lidarr_searches "
        "WHERE NOT EXISTS (SELECT 1 FROM pending_lidarr_search_requesters "
        "WHERE pending_lidarr_search_requesters.job_id = "
        "pending_lidarr_searches.id)"
    )
    violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise RuntimeError(
            "The Melodarr database contains unresolved foreign-key violations."
        )


def init_db():
    """Create current tables and migrate legacy service settings to JSON."""
    legacy_settings = {}
    legacy_lastfm_api_key = ""
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
                plex_id TEXT,
                plex_username TEXT,
                plex_email TEXT,
                plex_avatar TEXT,
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
        legacy_lastfm_row = connection.execute(
            """
            SELECT lastfm_api_key
            FROM users
            WHERE NULLIF(TRIM(lastfm_api_key), '') IS NOT NULL
            ORDER BY CASE WHEN role = 'admin' THEN 0 ELSE 1 END, created_at, id
            LIMIT 1
            """
        ).fetchone()
        if legacy_lastfm_row:
            legacy_lastfm_api_key = str(
                legacy_lastfm_row["lastfm_api_key"] or ""
            ).strip()
        for column in ("plex_id", "plex_username", "plex_email", "plex_avatar"):
            if column not in user_columns:
                connection.execute(f"ALTER TABLE users ADD COLUMN {column} TEXT")
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS users_plex_id_unique "
            "ON users(plex_id) WHERE plex_id IS NOT NULL"
        )
        plex_flow_schema = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'plex_auth_flows'"
        ).fetchone()
        if plex_flow_schema and "'link'" not in (plex_flow_schema["sql"] or ""):
            # PIN authorizations last at most fifteen minutes and are safe to
            # invalidate while widening the purpose constraint on upgrade.
            connection.execute("DROP TABLE plex_auth_flows")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS plex_auth_flows (
                flow_hash TEXT PRIMARY KEY,
                pin_id INTEGER NOT NULL,
                client_identifier TEXT NOT NULL,
                purpose TEXT NOT NULL CHECK(purpose IN ('login', 'server', 'link')),
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                auth_token TEXT,
                account_json TEXT,
                resources_json TEXT,
                selection_json TEXT,
                libraries_json TEXT
            )
        """)
        connection.execute(
            "DELETE FROM plex_auth_flows WHERE expires_at <= ?", (time.time(),)
        )
        connection.execute("""
            CREATE TABLE IF NOT EXISTS request_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
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
                user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                value TEXT NOT NULL,
                refreshed_at REAL NOT NULL
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS listening_profiles (
                user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                value TEXT NOT NULL,
                refreshed_at REAL NOT NULL,
                last_attempted_at REAL NOT NULL,
                last_error TEXT
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS plex_listens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                server_id TEXT NOT NULL,
                history_key TEXT NOT NULL,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                artist_rating_key TEXT NOT NULL,
                album_rating_key TEXT,
                played_at REAL NOT NULL,
                UNIQUE(server_id, history_key)
            )
        """)
        connection.execute(
            "CREATE INDEX IF NOT EXISTS plex_listens_user_played "
            "ON plex_listens(user_id, played_at)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS plex_listens_server_played "
            "ON plex_listens(server_id, played_at)"
        )
        connection.execute("""
            CREATE TABLE IF NOT EXISTS account_invitations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token_hash TEXT NOT NULL UNIQUE,
                created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                used_at REAL
            )
        """)
        _migrate_pending_lidarr_searches(connection)
        # Release-group requests always use RefreshAlbum. Convert work queued
        # by versions that conditionally selected RefreshArtist as well.
        connection.execute(
            "UPDATE pending_lidarr_searches SET refresh_type = 'album' "
            "WHERE refresh_type != 'album'"
        )
        _delete_legacy_orphans(connection)
        has_legacy_settings = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'settings'"
        ).fetchone()
        if has_legacy_settings:
            legacy_settings = {
                row["service"]: json.loads(row["value"])
                for row in connection.execute("SELECT service, value FROM settings")
            }

    settings = load_settings_file()
    settings_changed = settings is None
    if settings is None:
        settings = legacy_settings
    lastfm_config = settings.get("lastfm")
    if (
        legacy_lastfm_api_key
        and (
            not isinstance(lastfm_config, dict)
            or not str(lastfm_config.get("apiKey") or "").strip()
        )
    ):
        settings["lastfm"] = {"apiKey": legacy_lastfm_api_key}
        settings_changed = True
    if settings_changed:
        write_settings_file(settings)

    # Legacy releases stored an application API key on every user. Once a
    # shared copy has safely reached settings.json, scrub those duplicates.
    if get_lastfm_api_key():
        with db() as connection:
            connection.execute(
                "UPDATE users SET lastfm_api_key = NULL "
                "WHERE lastfm_api_key IS NOT NULL"
            )

    # The JSON file is safely written before removing the old table, so an
    # upgrade retains existing configurations without leaving credentials in
    # the database.
    if has_legacy_settings:
        with db() as connection:
            connection.execute("DROP TABLE settings")
