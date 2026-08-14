"""Low-priority MusicBrainz resolution for Last.fm similar artists."""

import logging
import time
from threading import Event, Lock
from uuid import UUID

import requests

if __package__ == "backend.workers":
    from ..api_cache import get_cache_document, set_cache_document
    from ..config import MUSICBRAINZ_METADATA_CACHE_TTL
    from ..services import musicbrainz
else:  # Support `python backend/worker.py` for local development.
    from api_cache import get_cache_document, set_cache_document
    from config import MUSICBRAINZ_METADATA_CACHE_TTL
    from services import musicbrainz


logger = logging.getLogger(__name__)
RESOLUTION_NAMESPACE = "lastfm-similar-artist-resolution"
MISSING_RESOLUTION_TTL = 24 * 60 * 60
MAX_QUEUED_CANDIDATES = 250

wake_requested = Event()
queue_lock = Lock()
queued_candidates = {}
active_candidate_keys = set()
job_state = {
    "running": False,
    "queued": 0,
    "completed": 0,
    "lastCompletedAt": None,
}


def _normalized_name(name):
    return " ".join(str(name or "").strip().split()).casefold()


def _candidate_name(candidate):
    value = candidate.get("name") if isinstance(candidate, dict) else candidate
    return " ".join(str(value or "").strip().split())[:300]


def _candidate_key(candidate):
    name = _candidate_name(candidate)
    url = (
        str(candidate.get("url") or "").strip().casefold()[:500]
        if isinstance(candidate, dict)
        else ""
    )
    return f"{url}\0{_normalized_name(name)}" if name else ""


def cached_resolution(candidate):
    """Return a successful or confirmed-missing cached name resolution."""
    key = _candidate_key(candidate)
    return get_cache_document(RESOLUTION_NAMESPACE, key) if key else None


def request_resolutions(candidates):
    """Queue uncached candidate names, coalescing and bounding upstream work."""
    pending = {}
    for candidate in candidates:
        name = _candidate_name(candidate)
        key = _candidate_key(candidate)
        if key and cached_resolution(candidate) is None:
            pending.setdefault(key, name)
    if not pending:
        return 0
    with queue_lock:
        remaining = max(
            0,
            MAX_QUEUED_CANDIDATES - len(queued_candidates) - len(active_candidate_keys),
        )
        for key, name in pending.items():
            if not remaining:
                break
            if key in active_candidate_keys or key in queued_candidates:
                continue
            queued_candidates[key] = name
            remaining -= 1
        job_state["queued"] = len(queued_candidates)
        queued = job_state["queued"]
    if queued:
        wake_requested.set()
    return queued


def _valid_uuid(value):
    try:
        return str(UUID(str(value or "")))
    except (TypeError, ValueError, AttributeError):
        return ""


def _matching_artist(name):
    query_name = name.replace('"', "").strip()
    if not query_name:
        return {"id": "", "name": name}
    response = musicbrainz.search(
        f'artist:"{query_name}"',
        "artist",
        priority="background",
    )
    artists = response.get("artists", [])
    normalized = _normalized_name(name)
    exact = {}
    for artist in artists:
        artist_id = _valid_uuid(artist.get("id"))
        names = {
            _normalized_name(artist.get("name")),
            _normalized_name(artist.get("sort-name")),
            *(
                _normalized_name(alias.get("name"))
                for alias in artist.get("aliases") or []
                if isinstance(alias, dict)
            ),
        }
        if artist_id and normalized in names:
            exact.setdefault(artist_id, artist)
    if len(exact) != 1:
        return {"id": "", "name": name}
    match = next(iter(exact.values()))
    return {
        "id": _valid_uuid(match.get("id")),
        "name": str(match.get("name") or name).strip() or name,
    }


def process_one():
    """Resolve one queued name without delaying interactive MusicBrainz work."""
    with queue_lock:
        if not queued_candidates:
            job_state["running"] = False
            job_state["queued"] = 0
            return False
        key = next(iter(queued_candidates))
        name = queued_candidates.pop(key)
        active_candidate_keys.add(key)
        job_state["running"] = True
        job_state["queued"] = len(queued_candidates)
    try:
        resolution = _matching_artist(name)
        ttl = (
            MUSICBRAINZ_METADATA_CACHE_TTL
            if resolution["id"]
            else MISSING_RESOLUTION_TTL
        )
        set_cache_document(RESOLUTION_NAMESPACE, key, resolution, ttl)
    except (ValueError, requests.RequestException):
        logger.warning("Could not resolve a Last.fm similar-artist candidate")
    except Exception:
        logger.exception("Unexpected similar-artist resolution failure")
    finally:
        with queue_lock:
            active_candidate_keys.discard(key)
            job_state["completed"] += 1
            job_state["lastCompletedAt"] = time.time()
            job_state["running"] = bool(active_candidate_keys)
    return True


def run():
    """Drain queued candidates silently at MusicBrainz background priority."""
    while True:
        wake_requested.wait()
        while process_one():
            pass
        with queue_lock:
            if not queued_candidates:
                wake_requested.clear()
