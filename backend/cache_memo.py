"""Short-lived in-process memoization for parsed cache documents.

The Plex snapshot and the Lidarr library document are single JSON blobs that
cover the whole library. Detail pages read them several times per request, so
each read previously cost a SQLite round trip plus a full `json.loads` of the
entire library. These helpers keep the parsed form for the duration of a
request, and briefly beyond it for the background workers that have no
application context.
"""

import time
from threading import Lock

from flask import g, has_app_context


DEFAULT_TTL_SECONDS = 30
_REQUEST_STORE = "_melodarr_memoized_documents"
_lock = Lock()
_entries = {}


def memoized_document(key, build, ttl=DEFAULT_TTL_SECONDS):
    """Return `build()`'s result, reusing a recent one for the same key."""
    if has_app_context():
        store = g.setdefault(_REQUEST_STORE, {})
        if key not in store:
            store[key] = build()
        return store[key]

    now = time.monotonic()
    with _lock:
        entry = _entries.get(key)
        if entry and entry[0] > now:
            return entry[1]
    value = build()
    with _lock:
        _entries[key] = (now + ttl, value)
    return value


def invalidate_document(key):
    """Drop a memoized document after the underlying cache is rewritten."""
    with _lock:
        _entries.pop(key, None)
    if has_app_context():
        g.get(_REQUEST_STORE, {}).pop(key, None)
