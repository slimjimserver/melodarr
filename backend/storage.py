"""SQLite and JSON-backed persistence for Melodarr."""

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from copy import deepcopy
from tempfile import NamedTemporaryFile
from threading import Lock

if __package__:
    from .config import DATABASE, SETTINGS_FILE
else:  # Support the existing `python backend/app.py` entry point.
    from config import DATABASE, SETTINGS_FILE


DATABASE_BUSY_TIMEOUT_MS = 5000
_settings_lock = Lock()
_settings_cache_lock = Lock()
_settings_cache_signature = None
_settings_cache_value = None


def _settings_file_signature():
    try:
        metadata = os.stat(SETTINGS_FILE)
    except FileNotFoundError:
        return None
    return (metadata.st_mtime_ns, metadata.st_size, metadata.st_ino)


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
    global _settings_cache_signature, _settings_cache_value
    signature = _settings_file_signature()
    with _settings_cache_lock:
        if signature == _settings_cache_signature:
            return deepcopy(_settings_cache_value)
        if signature is None:
            _settings_cache_signature = None
            _settings_cache_value = None
            return None
        try:
            with open(SETTINGS_FILE, encoding="utf-8") as file:
                settings = json.load(file)
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Could not read Melodarr settings file: {exc}") from exc
        if not isinstance(settings, dict):
            raise RuntimeError(  # noqa: TRY004 - preserve configuration error contract
                "Melodarr settings file must contain a JSON object."
            )
        _settings_cache_signature = _settings_file_signature()
        _settings_cache_value = settings
        return deepcopy(settings)


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
    global _settings_cache_signature, _settings_cache_value
    with _settings_cache_lock:
        _settings_cache_signature = _settings_file_signature()
        _settings_cache_value = deepcopy(settings)


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


def update_service(service, updater):
    """Atomically transform one settings section and return its new value."""
    with _settings_lock:
        settings = load_settings_file() or {}
        current = settings.get(service)
        current = deepcopy(current) if isinstance(current, dict) else {}
        updated = updater(current)
        if not isinstance(updated, dict):
            raise TypeError("Service settings updates must return an object.")
        if settings.get(service) != updated:
            settings[service] = updated
            write_settings_file(settings)
        return deepcopy(updated)


def get_request_history(user_id, limit=100, offset=0):
    """Return the most recent private request-history rows for one user."""
    with db() as connection:
        return connection.execute(
            "SELECT id, use_for_recommendations, kind, mbid, name, artist_name, release_type, release_date, "
            "anime_slug, anime_name, theme_id, theme_label, song_id, song_title, "
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


def _wake_recommendations():
    # Import lazily: the worker itself depends on storage.
    if __package__:
        from .workers.recommendations import request_refresh
    else:
        from workers.recommendations import request_refresh
    request_refresh()


def record_request(
    user_id,
    kind,
    mbid,
    name,
    *,
    artist_name="",
    release_type="",
    release_date="",
    anime_slug="",
    anime_name="",
    theme_id=None,
    theme_label="",
    song_id=None,
    song_title="",
):
    """Record an artist or release-group request for one user."""
    with db() as connection:
        connection.execute(
            "INSERT INTO request_history "
            "(user_id, kind, mbid, name, artist_name, release_type, "
            "release_date, anime_slug, anime_name, theme_id, theme_label, "
            "song_id, song_title, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                user_id,
                kind,
                mbid,
                name,
                artist_name or None,
                release_type or None,
                release_date or None,
                anime_slug or None,
                anime_name or None,
                theme_id,
                theme_label or None,
                song_id,
                song_title or None,
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

    _wake_recommendations()


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
    anime_slug="",
    anime_name="",
    theme_id=None,
    theme_label="",
    song_id=None,
    song_title="",
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
            "release_date, anime_slug, anime_name, theme_id, theme_label, "
            "song_id, song_title, created_at) "
            "VALUES (?, 'release-group', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                user_id,
                mbid,
                name,
                artist_name or None,
                release_type or None,
                release_date or None,
                anime_slug or None,
                anime_name or None,
                theme_id,
                theme_label or None,
                song_id,
                song_title or None,
                now,
            ),
        )
        inserted = bool(cursor.rowcount)
    _wake_recommendations()
    return inserted


def pending_lidarr_search(mbid):
    with db() as connection:
        return connection.execute(
            "SELECT * FROM pending_lidarr_searches WHERE mbid = ?", (mbid,)
        ).fetchone()


def pending_lidarr_search_mbids(mbids):
    """Return pending release groups in one bounded query for history pages."""
    normalized = {str(mbid).casefold() for mbid in mbids if mbid}
    if not normalized:
        return set()
    placeholders = ", ".join("?" for _ in normalized)
    with db() as connection:
        rows = connection.execute(
            "SELECT mbid FROM pending_lidarr_searches "
            f"WHERE lower(mbid) IN ({placeholders})",
            tuple(normalized),
        ).fetchall()
    return {str(row["mbid"]).casefold() for row in rows}


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


def _migrate_anime_song_mapping_schema(connection):
    """Upgrade pre-release registry tables without losing local corrections."""
    parent = connection.execute(
        "SELECT sql FROM sqlite_master "
        "WHERE type = 'table' AND name = 'anime_song_mappings'"
    ).fetchone()
    if parent is None:
        return
    target = connection.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type = 'table' AND name = 'anime_song_mapping_targets'"
    ).fetchone()
    target_columns = {
        row["name"]
        for row in connection.execute(
            "PRAGMA table_info(anime_song_mapping_targets)"
        )
    } if target else set()
    if "'rejected'" in (parent["sql"] or "") and (
        not target or "mapping_scope" in target_columns
    ):
        return

    connection.execute("DROP INDEX IF EXISTS anime_song_mapping_one_preferred")
    connection.execute("""
        CREATE TABLE anime_song_mappings_v2 (
            song_id INTEGER PRIMARY KEY CHECK(song_id > 0),
            title_snapshot TEXT NOT NULL,
            artists_json TEXT NOT NULL,
            status TEXT NOT NULL
                CHECK(status IN ('proposed', 'confirmed', 'rejected')),
            provenance TEXT NOT NULL,
            mapping_scope TEXT NOT NULL,
            schema_version INTEGER NOT NULL CHECK(schema_version > 0),
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
    """)
    connection.execute(
        "INSERT INTO anime_song_mappings_v2 "
        "(song_id, title_snapshot, artists_json, status, provenance, "
        "mapping_scope, schema_version, created_at, updated_at) "
        "SELECT song_id, title_snapshot, artists_json, status, provenance, "
        "mapping_scope, schema_version, created_at, updated_at "
        "FROM anime_song_mappings"
    )
    connection.execute("""
        CREATE TABLE anime_song_mapping_targets_v2 (
            song_id INTEGER NOT NULL
                REFERENCES anime_song_mappings_v2(song_id) ON DELETE CASCADE,
            release_group_mbid TEXT NOT NULL,
            recording_mbids_json TEXT NOT NULL DEFAULT '[]',
            artist_mbids_json TEXT NOT NULL DEFAULT '[]',
            release_group_title TEXT NOT NULL,
            artist_name TEXT NOT NULL,
            primary_type TEXT NOT NULL,
            first_release_date TEXT NOT NULL,
            mapping_scope TEXT NOT NULL,
            is_preferred INTEGER NOT NULL DEFAULT 0
                CHECK(is_preferred IN (0, 1)),
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            PRIMARY KEY(song_id, release_group_mbid)
        )
    """)
    if target:
        target_scope = (
            "COALESCE(target.mapping_scope, parent.mapping_scope, 'unknown')"
            if "mapping_scope" in target_columns
            else "COALESCE(parent.mapping_scope, 'unknown')"
        )
        connection.execute(
            "INSERT INTO anime_song_mapping_targets_v2 "
            "(song_id, release_group_mbid, recording_mbids_json, "
            "artist_mbids_json, release_group_title, artist_name, "
            "primary_type, first_release_date, mapping_scope, is_preferred, "
            "created_at, updated_at) "
            "SELECT target.song_id, target.release_group_mbid, "
            "target.recording_mbids_json, target.artist_mbids_json, "
            "target.release_group_title, target.artist_name, "
            f"target.primary_type, target.first_release_date, {target_scope}, "
            "target.is_preferred, target.created_at, target.updated_at "
            "FROM anime_song_mapping_targets AS target "
            "JOIN anime_song_mappings AS parent "
            "ON parent.song_id = target.song_id"
        )
        connection.execute("DROP TABLE anime_song_mapping_targets")
    connection.execute("DROP TABLE anime_song_mappings")
    connection.execute(
        "ALTER TABLE anime_song_mappings_v2 RENAME TO anime_song_mappings"
    )
    connection.execute(
        "ALTER TABLE anime_song_mapping_targets_v2 "
        "RENAME TO anime_song_mapping_targets"
    )


def init_db():
    """Create current tables and migrate legacy service settings to JSON."""
    legacy_settings = {}
    legacy_lastfm_api_key = ""
    with db() as connection:
        # WAL lets request threads read account and queue state while a
        # background worker commits unrelated updates.
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("""CREATE TABLE IF NOT EXISTS anime_automatic_matches (
            mapping_key TEXT PRIMARY KEY, payload TEXT NOT NULL,
            retry_at REAL, updated_at REAL NOT NULL)""")
        connection.execute("""CREATE TABLE IF NOT EXISTS anime_recording_catalogs (
            artist_mbid TEXT PRIMARY KEY, recordings_json TEXT NOT NULL DEFAULT '{}',
            next_offset INTEGER NOT NULL DEFAULT 0, complete INTEGER NOT NULL DEFAULT 0,
            generation REAL NOT NULL, updated_at REAL NOT NULL)""")
        connection.execute("""CREATE TABLE IF NOT EXISTS anime_artist_refresh_jobs (
            artist_id INTEGER PRIMARY KEY, artist_mbid TEXT NOT NULL, slug TEXT NOT NULL,
            due_at REAL NOT NULL DEFAULT 0, lease_until REAL NOT NULL DEFAULT 0,
            snapshot TEXT, updated_at REAL, failures INTEGER NOT NULL DEFAULT 0)""")
        connection.execute("""CREATE TABLE IF NOT EXISTS anime_performance_jobs (
            anime_slug TEXT NOT NULL, theme_id INTEGER NOT NULL, song_id INTEGER NOT NULL,
            due_at REAL NOT NULL DEFAULT 0, lease_until REAL NOT NULL DEFAULT 0,
            failures INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(anime_slug, theme_id))""")

        # Retry negatives produced by the removed hard catalog cap. Reviewed
        # mappings and successful automatic evidence are not touched.
        for row in connection.execute(
            "SELECT mapping_key, payload FROM anime_automatic_matches "
            "WHERE payload LIKE '%artist-recording-browse-limit%'"
        ).fetchall():
            result = json.loads(row["payload"])
            if result.get("state") != "unmatched" or result.get("reason") != "artist-recording-browse-limit":
                continue
            song_id = str(result.get("sourceSongId") or "")
            if song_id.isdigit():
                connection.execute("UPDATE anime_performance_jobs SET due_at=0 WHERE song_id=?", (int(song_id),))
            connection.execute("DELETE FROM anime_automatic_matches WHERE mapping_key=?", (row["mapping_key"],))

        # AI recommendations were removed. This intentionally deletes only the
        # obsolete derived profiles; every account, request, and library table
        # remains untouched.
        connection.execute("DROP TABLE IF EXISTS listening_profiles")
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
                anime_slug TEXT,
                anime_name TEXT,
                theme_id INTEGER,
                theme_label TEXT,
                song_id INTEGER,
                song_title TEXT,
                created_at REAL NOT NULL
            )
        """)
        connection.execute(
            "CREATE INDEX IF NOT EXISTS request_history_user_recent "
            "ON request_history(user_id, created_at DESC, id DESC)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS request_history_recent "
            "ON request_history(created_at DESC, id DESC)"
        )
        connection.execute("""
            CREATE TABLE IF NOT EXISTS recommendation_preferences (
                user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                mode TEXT NOT NULL DEFAULT 'balanced' CHECK(mode IN ('familiar', 'balanced', 'discovery')),
                artists_json TEXT NOT NULL DEFAULT '[]',
                revision INTEGER NOT NULL DEFAULT 0
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS recommendation_feedback (
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                kind TEXT NOT NULL CHECK(kind IN ('artist', 'release-group')),
                mbid TEXT NOT NULL,
                action TEXT NOT NULL CHECK(action IN ('dismiss', 'more')),
                item_json TEXT NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY(user_id, kind, mbid)
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS recommendation_exposures (
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                kind TEXT NOT NULL CHECK(kind IN ('artist', 'release-group')),
                mbid TEXT NOT NULL,
                day INTEGER NOT NULL,
                item_json TEXT NOT NULL,
                shown_at REAL NOT NULL,
                opened INTEGER NOT NULL DEFAULT 0,
                listened INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(user_id, kind, mbid, day)
            )
        """)
        # Notification rows deliberately retain event and target snapshots.  This
        # makes scans idempotent and keeps provider I/O out of the scan transaction.
        connection.execute("""
            CREATE TABLE IF NOT EXISTS user_notification_preferences (
                user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                notification_email TEXT,
                enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0, 1)),
                email_enabled INTEGER NOT NULL DEFAULT 0 CHECK(email_enabled IN (0, 1)),
                web_push_enabled INTEGER NOT NULL DEFAULT 0 CHECK(web_push_enabled IN (0, 1)),
                requested_available INTEGER NOT NULL DEFAULT 1 CHECK(requested_available IN (0, 1)),
                all_new_music INTEGER NOT NULL DEFAULT 0 CHECK(all_new_music IN (0, 1)),
                admin_request_notifications INTEGER NOT NULL DEFAULT 1
                    CHECK(admin_request_notifications IN (0, 1)),
                updated_at REAL NOT NULL
            )
        """)
        preference_columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(user_notification_preferences)"
            )
        }
        if "admin_request_notifications" not in preference_columns:
            connection.execute(
                "ALTER TABLE user_notification_preferences ADD COLUMN "
                "admin_request_notifications INTEGER NOT NULL DEFAULT 1 "
                "CHECK(admin_request_notifications IN (0, 1))"
            )
        connection.execute("""
            CREATE TABLE IF NOT EXISTS web_push_subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                endpoint TEXT NOT NULL UNIQUE,
                p256dh TEXT NOT NULL,
                auth TEXT NOT NULL,
                device_name TEXT,
                operating_system TEXT,
                browser TEXT,
                engine TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        # Keep installations from before device details were introduced usable.
        # These are deliberately nullable: old subscriptions never contained a
        # user agent-derived label and must remain valid until the user replaces
        # them.
        subscription_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(web_push_subscriptions)")
        }
        for column in ("device_name", "operating_system", "browser", "engine"):
            if column not in subscription_columns:
                connection.execute(f"ALTER TABLE web_push_subscriptions ADD COLUMN {column} TEXT")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS notification_mutes (
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                kind TEXT NOT NULL CHECK(kind IN ('artist', 'release-group')),
                mbid TEXT NOT NULL,
                created_at REAL NOT NULL,
                PRIMARY KEY(user_id, kind, mbid)
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS release_availability_state (
                release_mbid TEXT PRIMARY KEY,
                fully_available INTEGER NOT NULL CHECK(fully_available IN (0, 1)),
                generation INTEGER NOT NULL DEFAULT 0 CHECK(generation >= 0),
                artist_mbid TEXT NOT NULL DEFAULT '',
                artist_name TEXT NOT NULL DEFAULT '',
                release_title TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS notification_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                release_mbid TEXT NOT NULL,
                generation INTEGER NOT NULL,
                artist_mbid TEXT NOT NULL,
                artist_name TEXT NOT NULL,
                release_title TEXT NOT NULL,
                event_type TEXT NOT NULL DEFAULT 'availability',
                requester_username TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                UNIQUE(release_mbid, generation)
            )
        """)
        event_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(notification_events)")
        }
        if "event_type" not in event_columns:
            connection.execute(
                "ALTER TABLE notification_events ADD COLUMN "
                "event_type TEXT NOT NULL DEFAULT 'availability'"
            )
        if "requester_username" not in event_columns:
            connection.execute(
                "ALTER TABLE notification_events ADD COLUMN "
                "requester_username TEXT NOT NULL DEFAULT ''"
            )
        connection.execute("""
            CREATE TABLE IF NOT EXISTS notification_deliveries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL REFERENCES notification_events(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                channel TEXT NOT NULL CHECK(channel IN ('email', 'web-push')),
                email_target TEXT,
                subscription_id INTEGER REFERENCES web_push_subscriptions(id) ON DELETE SET NULL,
                push_endpoint TEXT,
                push_p256dh TEXT,
                push_auth TEXT,
                status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'leased', 'sent', 'dead')),
                attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
                next_attempt_at REAL NOT NULL,
                lease_token TEXT,
                lease_until REAL,
                last_error TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                CHECK((channel = 'email' AND email_target IS NOT NULL AND subscription_id IS NULL
                       AND push_endpoint IS NULL AND push_p256dh IS NULL AND push_auth IS NULL)
                   OR (channel = 'web-push' AND email_target IS NULL AND push_endpoint IS NOT NULL
                       AND push_p256dh IS NOT NULL AND push_auth IS NOT NULL))
            )
        """)
        delivery_columns = {row["name"] for row in connection.execute("PRAGMA table_info(notification_deliveries)")}
        if "push_endpoint" not in delivery_columns:
            # The first notification release joined live subscriptions at send time.
            # Rebuild before enabling snapshots: a browser endpoint may subsequently
            # transfer to another user, and historical work must never follow it.
            connection.execute("""CREATE TABLE notification_deliveries_v2 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL REFERENCES notification_events(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                channel TEXT NOT NULL CHECK(channel IN ('email', 'web-push')),
                email_target TEXT, subscription_id INTEGER REFERENCES web_push_subscriptions(id) ON DELETE SET NULL,
                push_endpoint TEXT, push_p256dh TEXT, push_auth TEXT,
                status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'leased', 'sent', 'dead')),
                attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0), next_attempt_at REAL NOT NULL,
                lease_token TEXT, lease_until REAL, last_error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                CHECK((channel='email' AND email_target IS NOT NULL AND subscription_id IS NULL AND push_endpoint IS NULL AND push_p256dh IS NULL AND push_auth IS NULL)
                   OR (channel='web-push' AND email_target IS NULL AND push_endpoint IS NOT NULL AND push_p256dh IS NOT NULL AND push_auth IS NOT NULL))
            )""")
            connection.execute("""INSERT INTO notification_deliveries_v2
                (id,event_id,user_id,channel,email_target,subscription_id,push_endpoint,push_p256dh,push_auth,status,attempts,next_attempt_at,lease_token,lease_until,last_error,created_at,updated_at)
                SELECT d.id,d.event_id,d.user_id,d.channel,d.email_target,d.subscription_id,s.endpoint,s.p256dh,s.auth,d.status,d.attempts,d.next_attempt_at,d.lease_token,d.lease_until,d.last_error,d.created_at,d.updated_at
                FROM notification_deliveries d LEFT JOIN web_push_subscriptions s ON s.id=d.subscription_id
                WHERE d.channel='email' OR s.id IS NOT NULL""")
            connection.execute("DROP TABLE notification_deliveries")
            connection.execute("ALTER TABLE notification_deliveries_v2 RENAME TO notification_deliveries")
        connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS notification_delivery_email_unique "
                           "ON notification_deliveries(event_id, user_id, email_target) WHERE channel = 'email'")
        connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS notification_delivery_push_unique "
            "ON notification_deliveries(event_id, user_id, push_endpoint) WHERE channel = 'web-push'")
        connection.execute("CREATE INDEX IF NOT EXISTS notification_deliveries_due "
                           "ON notification_deliveries(status, next_attempt_at)")
        request_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(request_history)")
        }
        request_optional_columns = {
            "use_for_recommendations": "INTEGER NOT NULL DEFAULT 1 CHECK(use_for_recommendations IN (0, 1))",
            "artist_name": "TEXT",
            "release_type": "TEXT",
            "release_date": "TEXT",
            "anime_slug": "TEXT",
            "anime_name": "TEXT",
            "theme_id": "INTEGER",
            "theme_label": "TEXT",
            "song_id": "INTEGER",
            "song_title": "TEXT",
        }
        for column, column_type in request_optional_columns.items():
            if column not in request_columns:
                connection.execute(
                    f"ALTER TABLE request_history ADD COLUMN {column} {column_type}"
                )
        connection.execute("""
            CREATE TABLE IF NOT EXISTS recommendation_cache (
                user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
                value TEXT NOT NULL,
                refreshed_at REAL NOT NULL
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
        _migrate_anime_song_mapping_schema(connection)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS anime_song_mappings (
                song_id INTEGER PRIMARY KEY CHECK(song_id > 0),
                title_snapshot TEXT NOT NULL,
                artists_json TEXT NOT NULL,
                status TEXT NOT NULL
                    CHECK(status IN ('proposed', 'confirmed', 'rejected')),
                provenance TEXT NOT NULL,
                mapping_scope TEXT NOT NULL,
                schema_version INTEGER NOT NULL CHECK(schema_version > 0),
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS anime_song_mapping_targets (
                song_id INTEGER NOT NULL
                    REFERENCES anime_song_mappings(song_id) ON DELETE CASCADE,
                release_group_mbid TEXT NOT NULL,
                recording_mbids_json TEXT NOT NULL DEFAULT '[]',
                artist_mbids_json TEXT NOT NULL DEFAULT '[]',
                release_group_title TEXT NOT NULL,
                artist_name TEXT NOT NULL,
                primary_type TEXT NOT NULL,
                first_release_date TEXT NOT NULL,
                mapping_scope TEXT NOT NULL,
                is_preferred INTEGER NOT NULL DEFAULT 0
                    CHECK(is_preferred IN (0, 1)),
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY(song_id, release_group_mbid)
            )
        """)
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "anime_song_mapping_one_preferred "
            "ON anime_song_mapping_targets(song_id) WHERE is_preferred = 1"
        )
        connection.execute("""
            CREATE TABLE IF NOT EXISTS anime_mapping_proposals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                submitter_user_id INTEGER NOT NULL
                    REFERENCES users(id) ON DELETE CASCADE,
                anime_slug TEXT NOT NULL,
                anime_name TEXT NOT NULL,
                theme_id INTEGER NOT NULL CHECK(theme_id > 0),
                theme_label TEXT NOT NULL,
                song_id INTEGER NOT NULL CHECK(song_id > 0),
                song_title TEXT NOT NULL,
                artists_json TEXT NOT NULL,
                target_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending', 'approved', 'rejected')),
                reviewed_by_user_id INTEGER
                    REFERENCES users(id) ON DELETE SET NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                reviewed_at REAL
            )
        """)
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "anime_mapping_proposal_one_pending_per_theme "
            "ON anime_mapping_proposals"
            "(submitter_user_id, anime_slug, theme_id) "
            "WHERE status = 'pending'"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS anime_mapping_proposals_review_queue "
            "ON anime_mapping_proposals(status, updated_at DESC)"
        )
        connection.execute("""
            CREATE TABLE IF NOT EXISTS anime_theme_release_group_links (
                anime_slug TEXT NOT NULL,
                anime_id INTEGER,
                anime_name TEXT NOT NULL,
                anime_series_json TEXT NOT NULL DEFAULT '[]',
                theme_id INTEGER NOT NULL CHECK(theme_id > 0),
                theme_label TEXT NOT NULL,
                theme_type TEXT NOT NULL,
                sequence INTEGER,
                song_id INTEGER CHECK(song_id IS NULL OR song_id > 0),
                song_title TEXT NOT NULL,
                release_group_mbid TEXT NOT NULL,
                recording_mbids_json TEXT NOT NULL DEFAULT '[]',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY(anime_slug, theme_id, release_group_mbid)
            )
        """)
        theme_link_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(anime_theme_release_group_links)")
        }
        if "anime_id" not in theme_link_columns:
            connection.execute(
                "ALTER TABLE anime_theme_release_group_links ADD COLUMN anime_id INTEGER"
            )
        if "anime_series_json" not in theme_link_columns:
            connection.execute(
                "ALTER TABLE anime_theme_release_group_links ADD COLUMN "
                "anime_series_json TEXT NOT NULL DEFAULT '[]'"
            )
        if "recording_mbids_json" not in theme_link_columns:
            connection.execute(
                "ALTER TABLE anime_theme_release_group_links ADD COLUMN "
                "recording_mbids_json TEXT NOT NULL DEFAULT '[]'"
            )
        if "is_preferred" not in theme_link_columns:
            connection.execute("ALTER TABLE anime_theme_release_group_links "
                               "ADD COLUMN is_preferred INTEGER NOT NULL DEFAULT 0")
        if "release_group_title" not in theme_link_columns:
            connection.execute(
                "ALTER TABLE anime_theme_release_group_links ADD COLUMN "
                "release_group_title TEXT NOT NULL DEFAULT ''"
            )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS anime_theme_links_release_group "
            "ON anime_theme_release_group_links"
            "(release_group_mbid, anime_name, theme_id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS anime_theme_links_theme "
            "ON anime_theme_release_group_links(anime_slug, theme_id)"
        )
        connection.execute("""
            CREATE TABLE IF NOT EXISTS anime_artist_links (
                anime_slug TEXT NOT NULL,
                theme_id INTEGER NOT NULL,
                artist_mbid TEXT NOT NULL,
                animethemes_artist_id INTEGER NOT NULL,
                artist_slug TEXT NOT NULL,
                artist_name TEXT NOT NULL,
                credited_as TEXT NOT NULL,
                verified INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(anime_slug, theme_id, artist_mbid, animethemes_artist_id)
            )
        """)
        connection.execute(
            "CREATE INDEX IF NOT EXISTS anime_artist_links_mbid "
            "ON anime_artist_links(artist_mbid)"
        )
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
    # Remove only the retired AI provider configuration and credentials.
    if "ai" in settings:
        del settings["ai"]
        settings_changed = True
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
