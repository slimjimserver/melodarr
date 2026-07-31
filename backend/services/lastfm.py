"""Last.fm API client operations."""

from hashlib import sha256

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


def _get(method, api_key, *, username=None, **extra):
    """Call one cached Last.fm API method and normalize API-level errors."""
    params = {
        "method": method,
        "api_key": api_key,
        "format": "json",
        **extra,
    }
    if username:
        params["user"] = username
    namespace = (
        user_cache_namespace(username)
        if username
        else LASTFM_PUBLIC_CACHE_NAMESPACE
    )
    data = cached_json_get(
        LASTFM_URL,
        params=params,
        headers={"User-Agent": USER_AGENT},
        namespace=namespace,
        ttl=LASTFM_CACHE_TTL,
    )
    if data.get("error"):
        raise ValueError(data.get("message", "Last.fm rejected the request."))
    return data


def get(method, username, api_key, **extra):
    """Call a cached Last.fm method associated with one linked username."""
    return _get(method, api_key, username=username, **extra)


def get_public(method, api_key, **extra):
    """Call a cached Last.fm method that does not require an end-user account."""
    return _get(method, api_key, **extra)
