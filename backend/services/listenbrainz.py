"""ListenBrainz account and recommendation client operations."""

from urllib.parse import quote

import requests

if __package__ == "backend.services":
    from ..api_cache import cached_json_get
    from ..config import (
        LISTENBRAINZ_METADATA_CACHE_TTL,
        LISTENBRAINZ_URL,
        USER_AGENT,
    )
else:  # Support the existing `python backend/app.py` entry point.
    from api_cache import cached_json_get
    from config import LISTENBRAINZ_METADATA_CACHE_TTL, LISTENBRAINZ_URL, USER_AGENT


def user_listen_count(username):
    """Return the ListenBrainz response used to validate a linked username."""
    return requests.get(
        f"{LISTENBRAINZ_URL}/user/{quote(username, safe='')}/listen-count",
        headers={"User-Agent": USER_AGENT},
        timeout=12,
    )


def recording_recommendations(username, count=50):
    """Load collaborative-filtering recording recommendations for a user."""
    response = requests.get(
        f"{LISTENBRAINZ_URL}/cf/recommendation/user/{quote(username, safe='')}/recording",
        params={"count": count},
        headers={"User-Agent": USER_AGENT},
        timeout=15,
    )
    if response.status_code == 204:
        return []
    response.raise_for_status()
    return response.json().get("payload", {}).get("mbids", [])


def recording_metadata(recording_mbids):
    """Resolve recording IDs to cached artist and release metadata."""
    return cached_json_get(
        f"{LISTENBRAINZ_URL}/metadata/recording/",
        params={"recording_mbids": ",".join(recording_mbids), "inc": "artist release"},
        headers={"User-Agent": USER_AGENT},
        namespace="listenbrainz-metadata",
        ttl=LISTENBRAINZ_METADATA_CACHE_TTL,
    )
