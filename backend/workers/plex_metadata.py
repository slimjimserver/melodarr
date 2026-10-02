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
queued_track_ids = set()
MAX_FALLBACK_TRACKS_PER_PASS = 100
MAX_DISCOGRAPHY_PAGES_PER_ARTIST = 10
VARIOUS_ARTISTS_ID = musicbrainz.VARIOUS_ARTISTS_ID
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


def request_enrichment(*, artist_ids=None, release_ids=None, track_ids=None):
    """Queue targeted scan deltas, or a full pass when called without targets."""
    global full_enrichment_requested
    full_request = artist_ids is None and release_ids is None and track_ids is None
    artist_ids = set(artist_ids or ())
    release_ids = set(release_ids or ())
    track_ids = set(track_ids or ())
    with queue_lock:
        if full_request:
            full_enrichment_requested = True
        elif artist_ids or release_ids or track_ids:
            queued_artist_ids.update(artist_ids)
            queued_release_ids.update(release_ids)
            queued_track_ids.update(track_ids)
        else:
            return
        job_state["queued"] = (
            1 if full_enrichment_requested
            else len(queued_artist_ids) + len(queued_release_ids) + len(queued_track_ids)
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
    artist_mappings = {}
    release_metadata = {}
    resolved_artist_ids = set()
    for index, release_id in enumerate(sorted(release_ids), start=1):
        try:
            metadata = musicbrainz.release_track_metadata(release_id, priority="background")
            release_metadata[release_id] = metadata
            mappings[release_id] = (metadata.get("release-group") or {}).get("id", "")
            artist_ids = {
                credit.get("artist", {}).get("id")
                for credit in metadata.get("artist-credit", [])
                if isinstance(credit, dict)
                and (credit.get("artist") or {}).get("id")
            }
            if len(artist_ids) == 1:
                artist_mappings[release_id] = next(iter(artist_ids))
                resolved_artist_ids.update(artist_ids)
        except requests.RequestException as exc:
            if _confirmed_missing(exc):
                mappings[release_id] = ""
                release_metadata[release_id] = None
            else:
                logger.warning(
                    "Could not resolve Plex release %s to a MusicBrainz release group: %s",
                    release_id,
                    exc,
                )
        job_state["completed"] = index
        if len(mappings) >= 25:
            plex.apply_release_group_mappings(
                config,
                mappings,
                artist_mappings=artist_mappings,
                release_metadata=release_metadata,
            )
            mappings.clear()
            artist_mappings.clear()
            release_metadata.clear()
    plex.apply_release_group_mappings(
        config,
        mappings,
        artist_mappings=artist_mappings,
        release_metadata=release_metadata,
    )
    return resolved_artist_ids


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
        job_state["phase"] = f"artist discographies · {artist_id}"
        try:
            artist = musicbrainz.get(
                f"/artist/{artist_id}",
                "url-rels+genres",
                priority="background",
            )
            name = artist.get("name") or artist_id
            logger.info("Warming Plex artist discography: %s (%s)", name, artist_id)
            # This special artist represents compilations across the catalogue;
            # its full discography is not useful speculative library enrichment.
            if musicbrainz.is_library_only_artist(artist_id):
                job_state["completed"] = index
                continue
            offset = 0
            seen_ids = set()
            for page_index in range(MAX_DISCOGRAPHY_PAGES_PER_ARTIST):
                job_state["phase"] = f"artist discographies · {name} · page {page_index + 1}"
                page = musicbrainz.get(
                    "/release-group",
                    "",
                    priority="background",
                    artist=artist_id,
                    limit=100,
                    offset=offset,
                )
                batch = page.get("release-groups", [])
                total = int(page.get("release-group-count", offset + len(batch)))
                if page.get("release-group-offset", offset) != offset:
                    raise ValueError("MusicBrainz returned an unexpected discography offset")
                page_ids = {group.get("id") for group in batch if isinstance(group, dict)}
                if batch and (None in page_ids or len(page_ids) != len(batch) or seen_ids & page_ids):
                    raise ValueError("MusicBrainz returned repeated or invalid discography entries")
                seen_ids.update(page_ids)
                if offset + len(batch) >= total or not batch:
                    break
                offset += len(batch)
            else:
                logger.warning(
                    "Stopped optional Plex discography warm-up for %s (%s) after %s pages; "
                    "remaining pages will load when requested",
                    name, artist_id, MAX_DISCOGRAPHY_PAGES_PER_ARTIST,
                )
        except (TypeError, ValueError, requests.RequestException) as exc:
            logger.warning(
                "Could not warm the MusicBrainz discography for Plex artist %s: %s",
                artist_id,
                exc,
            )
        job_state["completed"] = index


def _resolve_tracks(config, track_ids=None):
    targets = plex.fallback_track_ids(config, track_ids)
    selected = targets[:MAX_FALLBACK_TRACKS_PER_PASS]
    job_state.update(phase="track recordings", completed=0, total=len(selected))
    for offset in range(0, len(selected), 25):
        batch = selected[offset:offset + 25]
        try:
            plex.apply_track_recording_mappings(config, musicbrainz.recordings_by_track_ids(batch))
        except (ValueError, requests.RequestException) as exc:
            logger.warning("Could not resolve a Plex track-ID batch: %s", exc)
        job_state["completed"] += len(batch)
    # Large fallback inventories continue in bounded passes, paced by the
    # existing MB scheduler and a one-minute delay between worker passes.
    with queue_lock:
        queued_track_ids.update(targets[MAX_FALLBACK_TRACKS_PER_PASS:])
        job_state["queued"] = len(queued_artist_ids) + len(queued_release_ids) + len(queued_track_ids)


def _run_enrichment(artist_ids=None, release_ids=None, track_ids=None):
    config = get_service("plex")
    if not config:
        return
    job_state["running"] = True
    try:
        # Finish owned release/track mappings before optional discography work.
        inferred_artist_ids = _resolve_release_groups(config, release_ids)
        _resolve_tracks(config, track_ids)
        warm_artist_ids = None if artist_ids is None else set(artist_ids) | inferred_artist_ids
        _warm_artist_discographies(config, warm_artist_ids)
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
        wake_requested.wait(60 if queued_track_ids else None)
        wake_requested.clear()
        with queue_lock:
            full = full_enrichment_requested
            artist_ids = None if full else set(queued_artist_ids)
            release_ids = None if full else set(queued_release_ids)
            track_ids = None if full else set(queued_track_ids)
            full_enrichment_requested = False
            queued_artist_ids.clear()
            queued_release_ids.clear()
            queued_track_ids.clear()
            job_state["queued"] = 0
        _run_enrichment(artist_ids, release_ids, track_ids)
