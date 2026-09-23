"""Last.fm API client operations."""

import json
from contextlib import contextmanager
from hashlib import sha256
from threading import Lock

if __package__ == "backend.services":
    from ..api_cache import (
        cached_json_get,
        delete_cache_namespace,
        delete_legacy_cache_namespace,
    )
    from ..config import LASTFM_CACHE_TTL, LASTFM_URL, USER_AGENT
else:  # Support the existing `python backend/app.py` entry point.
    from api_cache import (
        cached_json_get,
        delete_cache_namespace,
        delete_legacy_cache_namespace,
    )
    from config import LASTFM_CACHE_TTL, LASTFM_URL, USER_AGENT


LASTFM_PUBLIC_CACHE_NAMESPACE = "lastfm:public"
_request_locks_lock = Lock()
_request_locks = {}


def user_cache_namespace(username):
    """Return a stable, non-identifying cache scope for one Last.fm user."""
    normalized = str(username or "").strip().casefold()
    if not normalized:
        raise ValueError("Last.fm username is required for a private cache scope.")
    digest = sha256(normalized.encode("utf-8")).hexdigest()
    return f"lastfm:user:{digest}"


def clear_user_cache(username):
    """Delete one linked user's cached Last.fm responses immediately.

    Old releases stored every Last.fm response in one opaque ``lastfm``
    namespace.  Those legacy hashes cannot be attributed to an individual, so
    they are discarded as a one-time privacy migration while modern scoped
    rows for other users and public lookups remain untouched.
    """
    if not str(username or "").strip():
        return 0
    removed = delete_cache_namespace(user_cache_namespace(username))
    removed += delete_legacy_cache_namespace("lastfm")
    return removed


@contextmanager
def _request_lock(namespace, method, api_key, extra):
    """Coalesce same-key cache misses without retaining credentials in memory."""
    payload = json.dumps(
        [namespace, method, str(api_key or ""), extra],
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    key = sha256(payload.encode("utf-8")).hexdigest()
    with _request_locks_lock:
        entry = _request_locks.get(key)
        if entry is None:
            entry = {"lock": Lock(), "users": 0}
            _request_locks[key] = entry
        entry["users"] += 1
        lock = entry["lock"]
    lock.acquire()
    try:
        yield
    finally:
        lock.release()
        with _request_locks_lock:
            entry["users"] -= 1
            if entry["users"] == 0 and _request_locks.get(key) is entry:
                del _request_locks[key]


def _get(
    method, api_key, *, username=None, force_refresh=False, cache_response=True, **extra
):
    """Call one cached Last.fm API method and normalize API-level errors."""
    cache_params = {
        "method": method,
        "format": "json",
        **extra,
    }
    cache_params.pop("api_key", None)
    if username:
        cache_params["user"] = username
    params = {**cache_params, "api_key": api_key}
    namespace = (
        user_cache_namespace(username) if username else LASTFM_PUBLIC_CACHE_NAMESPACE
    )
    with _request_lock(namespace, method, api_key, extra):
        data = cached_json_get(
            LASTFM_URL,
            params=params,
            cache_params=cache_params,
            headers={"User-Agent": USER_AGENT},
            namespace=namespace,
            reject_redirects=True,
            ttl=LASTFM_CACHE_TTL,
            force_refresh=force_refresh,
            cache_response=cache_response,
        )
    if data.get("error"):
        raise ValueError(data.get("message", "Last.fm rejected the request."))
    return data


def get(
    method, username, api_key, *, force_refresh=False, cache_response=True, **extra
):
    """Call a cached Last.fm method associated with one linked username."""
    return _get(
        method, api_key, username=username, force_refresh=force_refresh,
        cache_response=cache_response, **extra
    )


def get_public(method, api_key, **extra):
    """Call a cached Last.fm method that does not require an end-user account."""
    return _get(method, api_key, **extra)
