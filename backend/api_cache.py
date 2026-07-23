"""Persistent JSON response caching for external HTTP APIs."""

import json
import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from hashlib import sha256
from threading import Lock

import requests

if __package__:
    from .config import API_CACHE_CLEANUP_INTERVAL, CACHE_DATABASE, DATABASE
else:  # Support the existing `python backend/app.py` entry point.
    from config import API_CACHE_CLEANUP_INTERVAL, CACHE_DATABASE, DATABASE


logger = logging.getLogger(__name__)
CACHE_BUSY_TIMEOUT_MS = 5000
CACHE_LOCK_RETRY_DELAYS = (0.05, 0.15)
_RAISE_ON_LOCK = object()
_cleanup_lock = Lock()
_last_cleanup_at = None


@contextmanager
def cache_db():
    """Yield a transactional connection to the disposable metadata cache."""
    os.makedirs(os.path.dirname(os.path.abspath(CACHE_DATABASE)), exist_ok=True)
    connection = sqlite3.connect(
        CACHE_DATABASE,
        timeout=CACHE_BUSY_TIMEOUT_MS / 1000,
    )
    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA busy_timeout = {CACHE_BUSY_TIMEOUT_MS}")
    connection.execute("PRAGMA synchronous = NORMAL")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _is_lock_error(exc):
    message = str(exc).casefold()
    return "locked" in message or "busy" in message


def _cache_operation(operation, *, locked_default=_RAISE_ON_LOCK, description="access cache"):
    """Run a short cache transaction, retrying transient SQLite contention."""
    delays = (*CACHE_LOCK_RETRY_DELAYS, None)
    for delay in delays:
        try:
            with cache_db() as connection:
                return operation(connection)
        except sqlite3.OperationalError as exc:
            if not _is_lock_error(exc):
                raise
            if delay is None:
                if locked_default is _RAISE_ON_LOCK:
                    raise
                logger.warning(
                    "Metadata cache remained busy while trying to %s; "
                    "continuing without that cache operation",
                    description,
                )
                return locked_default
            time.sleep(delay)


def init_cache_db():
    """Create the standalone external-API cache database."""

    def initialize(connection):
        # WAL allows API requests to read cached metadata while a background
        # worker is replacing or enriching a different cache namespace.
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS api_cache (
                cache_key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                expires_at REAL NOT NULL
            )
        """)
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_api_cache_expires_at "
            "ON api_cache(expires_at)"
        )

    _cache_operation(initialize, description="initialize it")


def migrate_legacy_cache():
    """Move cache rows out of the durable application database once."""
    if os.path.abspath(DATABASE) == os.path.abspath(CACHE_DATABASE):
        return
    os.makedirs(os.path.dirname(os.path.abspath(DATABASE)), exist_ok=True)
    source = sqlite3.connect(DATABASE)
    source.row_factory = sqlite3.Row
    try:
        exists = source.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'api_cache'"
        ).fetchone()
        if not exists:
            return
        rows = source.execute(
            "SELECT cache_key, value, expires_at FROM api_cache"
        )
        migrated = 0
        with cache_db() as destination:
            while batch := rows.fetchmany(500):
                destination.executemany(
                    "INSERT OR REPLACE INTO api_cache (cache_key, value, expires_at) "
                    "VALUES (?, ?, ?)",
                    [
                        (row["cache_key"], row["value"], row["expires_at"])
                        for row in batch
                    ],
                )
                migrated += len(batch)
        source.execute("DROP TABLE api_cache")
        source.commit()
        logger.info("Moved %s API cache entries to %s", migrated, CACHE_DATABASE)
    finally:
        source.close()


def _fresh_cache_value(key):
    now = time.time()

    def read(connection):
        return connection.execute(
            "SELECT value FROM api_cache WHERE cache_key = ? AND expires_at > ?",
            (key, now),
        ).fetchone()
    row = _cache_operation(
        read,
        locked_default=None,
        description="read an HTTP response",
    )
    return json.loads(row["value"]) if row else None


def get_cache_expiry(key):
    """Return the expiry of one fresh cache row without reading its value."""
    now = time.time()

    def read(connection):
        return connection.execute(
            "SELECT expires_at FROM api_cache "
            "WHERE cache_key = ? AND expires_at > ?",
            (key, now),
        ).fetchone()

    row = _cache_operation(
        read,
        locked_default=None,
        description="read an HTTP response expiry",
    )
    return row["expires_at"] if row else None


def cache_key(namespace, url, params=None):
    """Create a stable key without storing API keys or other credentials."""
    payload = json.dumps(
        {"namespace": namespace, "url": url, "params": params or {}},
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"{namespace}:{sha256(payload.encode()).hexdigest()}"


def document_cache_key(namespace, document_id):
    """Create a stable key for a locally assembled cache document."""
    digest = sha256(str(document_id).encode()).hexdigest()
    return f"{namespace}:{digest}"


def get_cache_document(namespace, document_id, *, allow_expired=False):
    """Read a non-HTTP cache document used by a background scan."""
    key = document_cache_key(namespace, document_id)

    def read(connection):
        if allow_expired:
            return connection.execute(
                "SELECT value FROM api_cache WHERE cache_key = ?", (key,)
            ).fetchone()
        return connection.execute(
            "SELECT value FROM api_cache WHERE cache_key = ? AND expires_at > ?",
            (key, time.time()),
        ).fetchone()
    row = _cache_operation(
        read,
        locked_default=None,
        description=f"read the {namespace} document",
    )
    return json.loads(row["value"]) if row else None


def set_cache_document(namespace, document_id, value, ttl):
    """Persist a locally assembled cache document."""
    upsert_cache_documents(namespace, {document_id: value}, ttl)


def upsert_cache_documents(namespace, documents, ttl):
    """Persist multiple documents in one short transaction."""
    if not documents:
        return
    expires_at = time.time() + ttl
    rows = [
        (document_cache_key(namespace, document_id), json.dumps(value), expires_at)
        for document_id, value in documents.items()
    ]

    def write(connection):
        connection.executemany(
            "INSERT OR REPLACE INTO api_cache (cache_key, value, expires_at) "
            "VALUES (?, ?, ?)",
            rows,
        )

    _cache_operation(
        write,
        locked_default=None,
        description=f"write {namespace} documents",
    )


def replace_cache_documents(namespace, documents, ttl):
    """Atomically replace every document in a locally assembled namespace."""
    expires_at = time.time() + ttl
    rows = [
        (
            document_cache_key(namespace, document_id),
            json.dumps(value),
            expires_at,
        )
        for document_id, value in documents.items()
    ]

    def replace(connection):
        connection.execute(
            "DELETE FROM api_cache WHERE cache_key LIKE ?", (f"{namespace}:%",)
        )
        connection.executemany(
            "INSERT INTO api_cache (cache_key, value, expires_at) VALUES (?, ?, ?)",
            rows,
        )

    _cache_operation(
        replace,
        locked_default=None,
        description=f"replace the {namespace} namespace",
    )


def commit_json_responses(responses, *, delete_keys=()):
    """Atomically replace a staged set of external-API responses."""
    now = time.time()
    rows = [
        (
            cache_key(
                response["namespace"],
                response["url"],
                response.get("params"),
            ),
            json.dumps(response["value"]),
            now + response["ttl"],
        )
        for response in responses
    ]
    delete_keys = tuple(set(delete_keys))

    def commit(connection):
        if delete_keys:
            connection.executemany(
                "DELETE FROM api_cache WHERE cache_key = ?",
                ((key,) for key in delete_keys),
            )
        connection.executemany(
            "INSERT OR REPLACE INTO api_cache "
            "(cache_key, value, expires_at) VALUES (?, ?, ?)",
            rows,
        )

    _cache_operation(
        commit,
        description="commit staged external API responses",
    )


def cleanup_expired_cache():
    """Remove expired rows no more than once per configured interval."""
    global _last_cleanup_at
    checked_at = time.monotonic()
    if (
        _last_cleanup_at is not None
        and checked_at - _last_cleanup_at < API_CACHE_CLEANUP_INTERVAL
    ):
        return 0
    if not _cleanup_lock.acquire(blocking=False):
        return 0
    try:
        checked_at = time.monotonic()
        if (
            _last_cleanup_at is not None
            and checked_at - _last_cleanup_at < API_CACHE_CLEANUP_INTERVAL
        ):
            return 0
        now = time.time()

        def cleanup(connection):
            cursor = connection.execute(
                "DELETE FROM api_cache WHERE expires_at <= ?", (now,)
            )
            return cursor.rowcount

        try:
            removed = _cache_operation(
                cleanup,
                locked_default=-1,
                description="remove expired metadata cache entries",
            )
        except sqlite3.OperationalError as exc:
            logger.warning(
                "Could not clean expired metadata cache entries at %s: %s",
                CACHE_DATABASE,
                exc,
            )
            return 0
        if removed < 0:
            return 0
        _last_cleanup_at = checked_at
        return removed
    finally:
        _cleanup_lock.release()


def cached_json_get(
    url,
    *,
    headers=None,
    params=None,
    namespace,
    ttl,
    include_cache_status=False,
    before_request=None,
    retry_statuses=(),
    retry_exceptions=(),
    max_attempts=1,
    retry_backoff=1.0,
    request_timeout=15,
    force_refresh=False,
    cache_only=False,
    cache_response=True,
    request_get=None,
    after_response=None,
):
    """Fetch JSON, optionally replacing rather than reading a fresh cached value."""
    key = cache_key(namespace, url, params)
    if not force_refresh:
        value = _fresh_cache_value(key)
        if value is not None:
            return (value, True) if include_cache_status else value
    if cache_only:
        return (None, False) if include_cache_status else None

    attempts = max(1, int(max_attempts))
    retry_statuses = set(retry_statuses)
    request_get = request_get or requests.get
    for attempt in range(attempts):
        if before_request:
            before_request()
            # A higher-priority request may have populated this key while the
            # current request yielded its external-service slot.
            if not force_refresh:
                value = _fresh_cache_value(key)
                if value is not None:
                    return (value, True) if include_cache_status else value
        try:
            response = request_get(
                url,
                params=params,
                headers=headers,
                timeout=request_timeout,
            )
        except tuple(retry_exceptions) as exc:
            if attempt + 1 >= attempts:
                raise
            delay = retry_backoff * (2 ** attempt)
            logger.warning(
                "External API request failed for %s: %s; retrying in %.1f seconds",
                url,
                exc,
                delay,
            )
            time.sleep(delay)
            continue
        if response.status_code in retry_statuses and attempt + 1 < attempts:
            retry_after = response.headers.get("Retry-After", "")
            try:
                delay = max(0.0, float(retry_after))
            except (TypeError, ValueError):
                delay = retry_backoff * (2 ** attempt)
            logger.warning(
                "External API returned HTTP %s for %s; retrying in %.1f seconds",
                response.status_code,
                url,
                delay,
            )
            time.sleep(delay)
            continue
        response.raise_for_status()
        if after_response:
            after_response(response)
        break
    value = response.json()
    if cache_response:
        now = time.time()

        def write_response(connection):
            connection.execute(
                "INSERT OR REPLACE INTO api_cache "
                "(cache_key, value, expires_at) VALUES (?, ?, ?)",
                (key, json.dumps(value), now + ttl),
            )

        try:
            _cache_operation(
                write_response,
                locked_default=None,
                description="write an HTTP response",
            )
        except sqlite3.OperationalError as exc:
            # A non-locking filesystem error should remain visible, but caching
            # must never turn a successful upstream request into an API failure.
            logger.warning(
                "Could not write metadata cache at %s: %s",
                CACHE_DATABASE,
                exc,
            )
    cleanup_expired_cache()
    return (value, False) if include_cache_status else value


def clear_cache(namespace):
    """Remove all cached responses belonging to one external API namespace."""

    def clear(connection):
        connection.execute(
            "DELETE FROM api_cache WHERE cache_key LIKE ?", (f"{namespace}:%",)
        )

    try:
        _cache_operation(
            clear,
            locked_default=None,
            description=f"clear the {namespace} namespace",
        )
    except sqlite3.OperationalError as exc:
        logger.warning("Could not clear metadata cache at %s: %s", CACHE_DATABASE, exc)


def cache_stats():
    """Return storage and expiry statistics grouped by API namespace."""
    now = time.time()

    def read_stats(connection):
        return connection.execute(
            "SELECT substr(cache_key, 1, instr(cache_key, ':') - 1) AS namespace, "
            "COUNT(*) AS entries, "
            "SUM(CASE WHEN expires_at <= ? THEN 1 ELSE 0 END) AS expired, "
            "COALESCE(SUM(LENGTH(CAST(value AS BLOB))), 0) AS value_bytes, "
            "MIN(expires_at) AS earliest_expiry, MAX(expires_at) AS latest_expiry "
            "FROM api_cache GROUP BY namespace ORDER BY namespace",
            (now,),
        ).fetchall()
    rows = _cache_operation(
        read_stats,
        locked_default=[],
        description="calculate cache statistics",
    )
    database_bytes = 0
    for suffix in ("", "-wal", "-shm"):
        try:
            database_bytes += os.path.getsize(f"{CACHE_DATABASE}{suffix}")
        except OSError:
            pass
    return {
        "databaseBytes": database_bytes,
        "namespaces": [dict(row) for row in rows],
    }
