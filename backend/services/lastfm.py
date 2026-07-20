"""Last.fm API client operations."""

if __package__ == "backend.services":
    from ..api_cache import cached_json_get
    from ..config import LASTFM_CACHE_TTL, LASTFM_URL, USER_AGENT
else:  # Support the existing `python backend/app.py` entry point.
    from api_cache import cached_json_get
    from config import LASTFM_CACHE_TTL, LASTFM_URL, USER_AGENT


def get(method, username, api_key, **extra):
    """Call one cached Last.fm API method and normalize API-level errors."""
    data = cached_json_get(
        LASTFM_URL,
        params={
            "method": method,
            "user": username,
            "api_key": api_key,
            "format": "json",
            **extra,
        },
        headers={"User-Agent": USER_AGENT},
        namespace="lastfm",
        ttl=LASTFM_CACHE_TTL,
    )
    if data.get("error"):
        raise ValueError(data.get("message", "Last.fm rejected the request."))
    return data
