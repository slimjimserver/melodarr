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


def _user_statistics(username, resource, count=100, range_name="all_time"):
    """Load bounded public listening statistics for personalization/novelty."""
    response = requests.get(
        f"{LISTENBRAINZ_URL}/stats/user/"
        f"{quote(username, safe='')}/{quote(resource, safe='')}",
        params={"count": count, "range": range_name},
        headers={"User-Agent": USER_AGENT},
        timeout=15,
    )
    if response.status_code == 204:
        return []
    response.raise_for_status()
    payload = response.json().get("payload") or {}
    values = payload.get(resource.replace("-", "_")) or []
    return values if isinstance(values, list) else []


def top_artists(username, count=100, *, range_name="all_time"):
    """Return a user's public top artists for a supported statistics range."""
    return _user_statistics(
        username,
        "artists",
        count=count,
        range_name=range_name,
    )


def top_release_groups(username, count=100, *, range_name="all_time"):
    """Return a user's public top release groups for a statistics range."""
    return _user_statistics(
        username,
        "release-groups",
        count=count,
        range_name=range_name,
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
