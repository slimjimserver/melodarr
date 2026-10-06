"""Bounded, on-demand supplemental work; never runs during ordinary browsing."""

import json
import logging
import time
from queue import Queue
from threading import Lock, Thread

if __package__ == "backend.workers":
    from ..api_cache import _cache_operation, document_cache_key, get_cache_document, set_cache_document
    from ..services import artist_summary as service
else:
    from api_cache import _cache_operation, document_cache_key, get_cache_document, set_cache_document
    from services import artist_summary as service

logger = logging.getLogger(__name__)
jobs = Queue(maxsize=32)
_start_lock = Lock()
_started = False
LEASE_TTL = 30 * 60


def _state(artist_mbid, source):
    return get_cache_document(service.STATE_NAMESPACE, service.refresh_state_key(artist_mbid, source)) or {}


def _claim(artist_mbid, source):
    """SQLite compare-and-set prevents duplicate work across web processes."""
    now = time.time()
    state = {"status": "pending", "pending_until": now + LEASE_TTL}
    key = document_cache_key(service.STATE_NAMESPACE, service.refresh_state_key(artist_mbid, source))

    def claim(connection):
        return connection.execute(
            "INSERT INTO api_cache (cache_key, value, expires_at) VALUES (?, ?, ?) "
            "ON CONFLICT(cache_key) DO UPDATE SET value=excluded.value, expires_at=excluded.expires_at "
            "WHERE api_cache.expires_at <= ?",
            (key, json.dumps(state), now + LEASE_TTL, now),
        ).rowcount == 1

    return _cache_operation(claim, locked_default=False, description="claim supplemental refresh")


def request_summary(artist_mbid):
    global _started
    values, pending = {}, {}
    for source in ("bio", "top_tracks"):
        values[source] = service.snapshot(artist_mbid, source)
        if not service.fresh(values[source], source):
            with _start_lock:
                if not jobs.full() and _claim(artist_mbid, source):
                    jobs.put_nowait((artist_mbid, source))
                    if not _started:
                        # Each provider can complete while the other's chain runs.
                        for index in range(2):
                            Thread(target=run, name=f"artist-summary-{index}", daemon=True).start()
                        _started = True
        state = _state(artist_mbid, source)
        pending[source] = state.get("status") == "pending" and state.get("pending_until", 0) > time.time()
        if not pending[source]:
            values[source] = service.snapshot(artist_mbid, source)
    return {
        "bio": (values["bio"] or {}).get("bio"),
        "topTracks": (values["top_tracks"] or {}).get("entries") or [],
        "pending": any(pending.values()), "sources": {
            source: {"pending": pending[source], "stale": bool(values[source] and not service.fresh(values[source], source)),
                     "fetchedAt": (values[source] or {}).get("fetched_at")}
            for source in values
        },
    }


def process_job(artist_mbid, source):
    try:
        if not service.fresh(service.snapshot(artist_mbid, source), source):
            (service.refresh_bio if source == "bio" else service.refresh_top_tracks)(artist_mbid)
        state, ttl = {"status": "complete"}, service.RETRY_TTL
    except Exception as exc:
        # Preserve the previous successful document and avoid logging provider
        # payloads, arbitrary URLs, or credentials from the configured MB mirror.
        logger.warning("Artist Summary %s refresh failed: %s", source, type(exc).__name__)
        state, ttl = {"status": "failed"}, service.RETRY_TTL
    try:
        set_cache_document(service.STATE_NAMESPACE, service.refresh_state_key(artist_mbid, source), state, ttl)
    except Exception as exc:
        logger.warning("Artist Summary refresh state failed: %s", type(exc).__name__)


def run():
    while True:
        artist_mbid, source = jobs.get()
        try:
            process_job(artist_mbid, source)
        finally:
            jobs.task_done()
