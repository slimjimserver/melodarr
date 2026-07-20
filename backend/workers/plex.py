"""Scheduled Plex music-library cache scans."""

import logging
import time
from threading import Event, Lock

import requests

if __package__ == "backend.workers":
    from ..artwork_cache import plex_artist_artwork_key, remove_stale_plex_artist_artwork
    from ..config import PLEX_FULL_SCAN_INTERVAL, PLEX_RECENT_SCAN_INTERVAL
    from ..services import plex
    from ..storage import get_service
    from . import plex_metadata
else:
    from artwork_cache import plex_artist_artwork_key, remove_stale_plex_artist_artwork
    from config import PLEX_FULL_SCAN_INTERVAL, PLEX_RECENT_SCAN_INTERVAL
    from services import plex
    from storage import get_service
    from workers import plex_metadata


logger = logging.getLogger(__name__)
wake_requested = Event()
request_lock = Lock()
requested_scans = set()
job_state = {
    "recent": {"running": False, "lastCompletedAt": None, "nextExecutionAt": None},
    "full": {"running": False, "lastCompletedAt": None, "nextExecutionAt": None},
}


def request_scan(kind):
    if kind not in job_state:
        raise ValueError(f"Unknown Plex scan kind: {kind}")
    with request_lock:
        requested_scans.add(kind)
    wake_requested.set()


def request_recent_scan():
    request_scan("recent")


def request_full_scan():
    request_scan("full")


def status(kind):
    return dict(job_state[kind])


def _run_scan(kind):
    config = get_service("plex")
    state = job_state[kind]
    state["running"] = True
    try:
        if not config:
            return
        if kind == "full":
            artists = plex.full_library_scan(config)
            server_id = config.get("machineIdentifier") or config.get("url", "")
            valid_artwork = {
                plex_artist_artwork_key(server_id, artist.get("ratingKey"))
                for artist in artists
                if artist.get("ratingKey") and artist.get("thumb")
            }
            remove_stale_plex_artist_artwork(valid_artwork)
        else:
            plex.recently_added_scan(config)
        plex_metadata.request_enrichment()
    except (ValueError, requests.RequestException) as exc:
        logger.warning("Plex %s music-library scan failed: %s", kind, exc)
    except Exception:
        logger.exception("Plex %s music-library scan failed", kind)
    finally:
        completed_at = time.time()
        state["running"] = False
        state["lastCompletedAt"] = completed_at
        state["nextExecutionAt"] = completed_at + (
            PLEX_FULL_SCAN_INTERVAL if kind == "full" else PLEX_RECENT_SCAN_INTERVAL
        )


def run():
    """Run an initial full scan, then service both fixed schedules and requests."""
    job_state["recent"]["nextExecutionAt"] = time.time()
    job_state["full"]["nextExecutionAt"] = time.time()
    while True:
        now = time.time()
        with request_lock:
            requested = set(requested_scans)
            requested_scans.clear()
        full_due = now >= (job_state["full"]["nextExecutionAt"] or now)
        recent_due = now >= (job_state["recent"]["nextExecutionAt"] or now)

        if "full" in requested or full_due:
            _run_scan("full")
            # A full scan includes all recently added artists.
            job_state["recent"]["nextExecutionAt"] = time.time() + PLEX_RECENT_SCAN_INTERVAL
        elif "recent" in requested or recent_due:
            _run_scan("recent")

        next_times = [
            state["nextExecutionAt"]
            for state in job_state.values()
            if state["nextExecutionAt"] is not None
        ]
        timeout = max(0.1, min(next_times) - time.time()) if next_times else 60
        wake_requested.wait(timeout)
        wake_requested.clear()
