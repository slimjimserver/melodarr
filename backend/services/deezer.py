"""Read-only Deezer public API. No account, credentials, or private endpoints."""

import re
import time
from threading import Lock
from urllib.parse import urlsplit

import requests

if __package__ == "backend.services":
    from ..config import USER_AGENT
else:
    from config import USER_AGENT

_lock = Lock()
_next_request_at = 0.0


def relationship_id(relations, resource="artist"):
    """Accept one exact provider identity; conflicting relationships stay unknown."""
    ids = set()
    for relation in relations or []:
        if not isinstance(relation, dict) or not isinstance(relation.get("url"), dict):
            continue
        try:
            url = urlsplit(str(relation["url"].get("resource") or ""))
        except ValueError:
            continue
        if url.scheme not in {"http", "https"} or url.hostname not in {"deezer.com", "www.deezer.com"}:
            continue
        match = re.fullmatch(rf"/(?:[a-z]{{2}}/)?{resource}/([1-9][0-9]*)/?", url.path)
        if match:
            ids.add(int(match[1]))
    return next(iter(ids)) if len(ids) == 1 else None


def get(path, **params):
    """Bound and pace all requests, including HTTP-200 provider error objects."""
    global _next_request_at
    if not re.fullmatch(r"/(?:artist/[1-9][0-9]*/top|track/[1-9][0-9]*)", path):
        raise ValueError("Unsupported public Deezer resource")
    with _lock:
        time.sleep(max(0, _next_request_at - time.monotonic()))
        _next_request_at = time.monotonic() + 0.25
        response = requests.get(
            f"https://api.deezer.com{path}", params=params,
            headers={"User-Agent": USER_AGENT}, timeout=(3.05, 10), allow_redirects=False,
        )
        response.raise_for_status()
        if 300 <= response.status_code < 400:
            raise requests.RequestException("Deezer redirected the public API request")
        value = response.json()
        if not isinstance(value, dict) or "error" in value:
            raise requests.RequestException("Deezer returned an API error")
        return value


def top_tracks(artist_id):
    value = get(f"/artist/{int(artist_id)}/top", limit=10)
    if not isinstance(value.get("data"), list):
        raise requests.RequestException("Deezer omitted the top tracks")
    return value["data"][:10]


def track(track_id):
    value = get(f"/track/{int(track_id)}")
    if str(value.get("id")) != str(track_id):
        raise requests.RequestException("Deezer returned the wrong track")
    return value
