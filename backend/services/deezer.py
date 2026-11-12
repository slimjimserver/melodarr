"""Read-only Deezer public API. No account, credentials, or private endpoints."""

import math
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


class ArtistUnavailable(requests.RequestException):
    """The requested public artist profile no longer identifies this artist."""


def artist_relationship_ids(relations):
    """Validate artist authorities, then reuse the existing Deezer URL parser."""
    ids = set()
    for relation in relations or []:
        resource = ((relation.get("url") or {}).get("resource")
                    if isinstance(relation, dict) and isinstance(relation.get("url"), dict) else None)
        if not isinstance(resource, str) or any(character.isspace() or ord(character) < 32 or ord(character) == 127
                                                for character in resource):
            continue
        try:
            url = urlsplit(resource)
            port = url.port
        except ValueError:
            continue
        if (url.username is not None or url.password is not None
                or port not in (None, 443 if url.scheme == "https" else 80)):
            continue
        artist_id = relationship_id([relation])
        if artist_id:
            ids.add(artist_id)
    return sorted(ids)


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
    if not re.fullmatch(r"/(?:artist/[1-9][0-9]*(?:/top)?|track/[1-9][0-9]*)", path):
        raise ValueError("Unsupported public Deezer resource")
    with _lock:
        time.sleep(max(0, _next_request_at - time.monotonic()))
        _next_request_at = time.monotonic() + 0.25
        response = requests.get(
            f"https://api.deezer.com{path}", params=params,
            headers={"User-Agent": USER_AGENT}, timeout=(3.05, 10), allow_redirects=False,
        )
        if response.status_code in {404, 410} and path.startswith("/artist/"):
            raise ArtistUnavailable("Deezer artist profile is unavailable", response=response)
        response.raise_for_status()
        if 300 <= response.status_code < 400:
            raise requests.RequestException("Deezer redirected the public API request")
        value = response.json()
        if (isinstance(value, dict) and isinstance(value.get("error"), dict)
                and value["error"].get("code") == 800 and path.startswith("/artist/")):
            raise ArtistUnavailable("Deezer artist profile is unavailable", response=response)
        if not isinstance(value, dict) or "error" in value:
            raise requests.RequestException("Deezer returned an API error")
        return value


def valid_artist_id(value):
    return ((isinstance(value, int) and not isinstance(value, bool) and value > 0)
            or (isinstance(value, str) and re.fullmatch(r"[1-9][0-9]*", value) is not None))


def validate_artist(value, artist_id):
    """Reject wrong profiles and absent, nonnumeric, or nonfinite fan counts."""
    if not isinstance(value, dict) or not valid_artist_id(value.get("id")) or int(value["id"]) != int(artist_id):
        raise ArtistUnavailable("Deezer returned the wrong artist identity")
    fans = value.get("nb_fan")
    if (isinstance(fans, bool) or not isinstance(fans, (int, float))
            or fans < 0 or (isinstance(fans, float) and not math.isfinite(fans))):
        raise requests.RequestException("Deezer omitted a valid numeric artist fan count")
    return value


def artist(artist_id):
    if not valid_artist_id(artist_id):
        raise ValueError("Invalid public Deezer artist ID")
    return validate_artist(get(f"/artist/{int(artist_id)}"), artist_id)


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
