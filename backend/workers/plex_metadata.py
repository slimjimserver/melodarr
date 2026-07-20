"""Low-priority MusicBrainz enrichment for the cached Plex inventory."""

import logging
import time
from threading import Event

import requests

if __package__ == "backend.workers":
    from ..services import musicbrainz, plex
    from ..storage import get_service
else:
    from services import musicbrainz, plex
    from storage import get_service


logger = logging.getLogger(__name__)
wake_requested = Event()
job_state = {
    "running": False,
    "lastCompletedAt": None,
    "nextExecutionAt": None,
    "queued": 0,
    "completed": 0,
    "total": 0,
    "phase": "idle",
}


def request_enrichment():
    job_state["queued"] = 1
    wake_requested.set()


def status():
    return dict(job_state)


def _confirmed_missing(exc):
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None) == 404


def _resolve_release_groups(config):
    releases = plex.unresolved_musicbrainz_releases(config)
    job_state.update(phase="release groups", completed=0, total=len(releases))
    mappings = {}
    for index, item in enumerate(releases, start=1):
        release_id = item["musicbrainzReleaseId"]
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


def _warm_artist_discographies(config):
    artists = [
        artist for artist in plex.music_library(config)
        if artist.get("musicbrainzId")
    ]
    job_state.update(phase="artist discographies", completed=0, total=len(artists))
    for index, artist in enumerate(artists, start=1):
        artist_id = artist["musicbrainzId"]
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


def _run_enrichment():
    config = get_service("plex")
    if not config:
        return
    job_state.update(running=True, queued=0)
    try:
        # Make Plex artist clicks fast first; exact edition-to-group mapping can
        # then continue behind the already-warmed discographies.
        _warm_artist_discographies(config)
        _resolve_release_groups(config)
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
    while True:
        wake_requested.wait()
        wake_requested.clear()
        _run_enrichment()
