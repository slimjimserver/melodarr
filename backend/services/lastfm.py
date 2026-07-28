"""Last.fm API client operations."""

if __package__ == "backend.services":
    from ..api_cache import cached_json_get
    from ..config import LASTFM_CACHE_TTL, LASTFM_URL, USER_AGENT
else:  # Support the existing `python backend/app.py` entry point.
    from api_cache import cached_json_get
    from config import LASTFM_CACHE_TTL, LASTFM_URL, USER_AGENT


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
    data = cached_json_get(
        LASTFM_URL,
        params=params,
        headers={"User-Agent": USER_AGENT},
        namespace="lastfm",
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
