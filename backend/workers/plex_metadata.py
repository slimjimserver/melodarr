"""Low-priority MusicBrainz enrichment for the cached Plex inventory."""

import logging
import time
from threading import Event, Lock

import requests

if __package__ == "backend.workers":
    from ..services import musicbrainz, plex
    from ..storage import get_service
else:
    from services import musicbrainz, plex
    from storage import get_service


logger = logging.getLogger(__name__)
wake_requested = Event()
queue_lock = Lock()
queued_artist_ids = set()
queued_release_ids = set()
full_enrichment_requested = False
job_state = {
    "running": False,
    "lastCompletedAt": None,
    "nextExecutionAt": None,
    "queued": 0,
    "completed": 0,
    "total": 0,
    "phase": "idle",
}


def request_enrichment(*, artist_ids=None, release_ids=None):
    """Queue targeted scan deltas, or a full pass when called without targets."""
    global full_enrichment_requested
    full_request = artist_ids is None and release_ids is None
    artist_ids = set(artist_ids or ())
    release_ids = set(release_ids or ())
    with queue_lock:
        if full_request:
            full_enrichment_requested = True
        elif artist_ids or release_ids:
            queued_artist_ids.update(artist_ids)
            queued_release_ids.update(release_ids)
        else:
            return
        job_state["queued"] = (
            1 if full_enrichment_requested
            else len(queued_artist_ids) + len(queued_release_ids)
        )
    wake_requested.set()


def status():
    return dict(job_state)


def _confirmed_missing(exc):
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None) == 404


def _resolve_release_groups(config, release_ids=None):
    if release_ids is None:
        release_ids = {
            item["musicbrainzReleaseId"]
            for item in plex.unresolved_musicbrainz_releases(config)
        }
    else:
        release_ids = set(release_ids)
    job_state.update(phase="release groups", completed=0, total=len(release_ids))
    mappings = {}
    for index, release_id in enumerate(sorted(release_ids), start=1):
        try:
            metadata = musicbrainz.get(
                f"/release/{release_id}",
                "release-groups",
                priority="background",
            )
            mappings[release_id] = (metadata.get("release-group") or {}).get("id", "")
        except requests.RequestException as exc:
            if _confirmed_missing(exc):
                mappings[release_id] = ""
            else:
                logger.warning(
                    "Could not resolve Plex release %s to a MusicBrainz release group: %s",
                    release_id,
                    exc,
                )
        job_state["completed"] = index
        if len(mappings) >= 25:
            plex.apply_release_group_mappings(config, mappings)
            mappings.clear()
    plex.apply_release_group_mappings(config, mappings)


def _warm_artist_discographies(config, artist_ids=None):
    if artist_ids is None:
        artist_ids = {
            artist["musicbrainzId"]
            for artist in plex.music_library(config)
            if artist.get("musicbrainzId")
        }
    else:
        artist_ids = set(artist_ids)
    job_state.update(phase="artist discographies", completed=0, total=len(artist_ids))
    for index, artist_id in enumerate(sorted(artist_ids), start=1):
        try:
            musicbrainz.get(
                f"/artist/{artist_id}",
                "url-rels+genres",
                priority="background",
            )
            offset = 0
            while True:
                page = musicbrainz.get(
                    "/release-group",
                    "",
                    priority="background",
                    artist=artist_id,
                    limit=100,
                    offset=offset,
                )
                batch = page.get("release-groups", [])
                total = page.get("release-group-count", offset + len(batch))
                if offset + len(batch) >= total or not batch:
                    break
                offset += len(batch)
        except requests.RequestException as exc:
            logger.warning(
                "Could not warm the MusicBrainz discography for Plex artist %s: %s",
                artist_id,
                exc,
            )
        job_state["completed"] = index


def _run_enrichment(artist_ids=None, release_ids=None):
    config = get_service("plex")
    if not config:
        return
    job_state["running"] = True
    try:
        # Make Plex artist clicks fast first; exact edition-to-group mapping can
        # then continue behind the already-warmed discographies.
        _warm_artist_discographies(config, artist_ids)
        _resolve_release_groups(config, release_ids)
    except (ValueError, requests.RequestException) as exc:
        logger.warning("Plex MusicBrainz enrichment failed: %s", exc)
    except Exception:
        logger.exception("Plex MusicBrainz enrichment failed")
    finally:
        job_state.update(
            running=False,
            lastCompletedAt=time.time(),
            phase="idle",
            completed=0,
            total=0,
        )


def run():
    """Enrich after scans or manual requests, yielding to interactive MB work."""
    global full_enrichment_requested
    while True:
        wake_requested.wait()
        wake_requested.clear()
        with queue_lock:
            full = full_enrichment_requested
            artist_ids = None if full else set(queued_artist_ids)
            release_ids = None if full else set(queued_release_ids)
            full_enrichment_requested = False
            queued_artist_ids.clear()
            queued_release_ids.clear()
            job_state["queued"] = 0
        _run_enrichment(artist_ids, release_ids)
