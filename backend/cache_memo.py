"""Short-lived in-process memoization for parsed cache documents.

The Plex snapshot and the Lidarr library document are single JSON blobs that
cover the whole library. These helpers keep the parsed form for the duration
of a request and briefly share it across requests in the same process.
"""

import time
from threading import Lock

from flask import g, has_app_context


DEFAULT_TTL_SECONDS = 30
_REQUEST_STORE = "_melodarr_memoized_documents"
_MISSING = object()
_lock = Lock()
_entries = {}
_generations = {}


def memoized_document(key, build, ttl=DEFAULT_TTL_SECONDS):
    """Return `build()`'s result, reusing a recent one for the same key."""
    request_store = None
    if has_app_context():
        request_store = g.setdefault(_REQUEST_STORE, {})
        if key in request_store:
            return request_store[key]

    now = time.monotonic()
    with _lock:
        generation = _generations.get(key, 0)
        entry = _entries.get(key)
        if entry and entry[0] > now:
            value = entry[1]
        else:
            value = _MISSING
    if value is _MISSING:
        value = build()
        expires_at = time.monotonic() + ttl
        with _lock:
            # A scan may have invalidated this key while the document was
            # being read and parsed. Do not republish that older value.
            if _generations.get(key, 0) == generation:
                entry = _entries.get(key)
                if entry and entry[0] > time.monotonic():
                    value = entry[1]
                else:
                    _entries[key] = (expires_at, value)
    if request_store is not None:
        request_store[key] = value
    return value


def invalidate_document(key):
    """Drop a memoized document after the underlying cache is rewritten."""
    with _lock:
        _entries.pop(key, None)
        _generations[key] = _generations.get(key, 0) + 1
    if has_app_context():
        g.get(_REQUEST_STORE, {}).pop(key, None)
