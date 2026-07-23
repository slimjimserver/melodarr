"""Bounded cache for fully assembled music-detail HTTP payloads."""

import time
from collections import OrderedDict
from contextlib import contextmanager
from hashlib import sha256
from threading import Lock

from flask import current_app, request

if __package__:
    from .config import (
        DETAIL_PAYLOAD_BROWSER_TTL,
        DETAIL_PAYLOAD_CACHE_MAX_ENTRIES,
        DETAIL_PAYLOAD_CACHE_TTL,
    )
else:
    from config import (
        DETAIL_PAYLOAD_BROWSER_TTL,
        DETAIL_PAYLOAD_CACHE_MAX_ENTRIES,
        DETAIL_PAYLOAD_CACHE_TTL,
    )


_lock = Lock()
_entries = OrderedDict()
_generation = 0
_key_locks_lock = Lock()
_key_locks = {}


def _cache_entry(payload):
    body = current_app.json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return {
        "body": body,
        "etag": sha256(body).hexdigest(),
        "expiresAt": time.monotonic() + DETAIL_PAYLOAD_CACHE_TTL,
    }


def get(key):
    """Return a fresh cached entry and update its LRU position."""
    now = time.monotonic()
    with _lock:
        entry = _entries.get(key)
        if entry is None:
            return None
        if entry["expiresAt"] <= now:
            del _entries[key]
            return None
        _entries.move_to_end(key)
        return entry


def store(key, payload, generation=None):
    """Serialize and retain a payload unless its dependencies changed mid-build."""
    entry = _cache_entry(payload)
    with _lock:
        if generation is None or generation == _generation:
            _entries[key] = entry
            _entries.move_to_end(key)
            while len(_entries) > DETAIL_PAYLOAD_CACHE_MAX_ENTRIES:
                _entries.popitem(last=False)
    return entry


def response(entry):
    """Return cached JSON with browser validation headers."""
    result = current_app.response_class(entry["body"], mimetype="application/json")
    result.headers["Cache-Control"] = (
        f"private, max-age={DETAIL_PAYLOAD_BROWSER_TTL}"
    )
    # The response may subsequently be gzip encoded, so use a weak validator
    # that remains valid for semantically identical encoded representations.
    result.set_etag(entry["etag"], weak=True)
    return result.make_conditional(request)


def cached_response(key):
    entry = get(key)
    return response(entry) if entry is not None else None


def payload_response(key, payload, generation=None):
    return response(store(key, payload, generation))


@contextmanager
def build_lock(key):
    """Coalesce concurrent assembly work for the same detail payload."""
    with _key_locks_lock:
        entry = _key_locks.get(key)
        if entry is None:
            entry = {"lock": Lock(), "users": 0}
            _key_locks[key] = entry
        entry["users"] += 1
        key_lock = entry["lock"]

    key_lock.acquire()
    try:
        with _lock:
            generation = _generation
        yield generation
    finally:
        key_lock.release()
        with _key_locks_lock:
            entry["users"] -= 1
            if entry["users"] == 0 and _key_locks.get(key) is entry:
                del _key_locks[key]


def invalidate_all():
    """Drop assembled payloads after Plex, Lidarr, or metadata changes."""
    global _generation
    with _lock:
        _entries.clear()
        _generation += 1
