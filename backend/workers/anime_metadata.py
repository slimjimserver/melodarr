"""Background queue for AnimeThemes song-to-MusicBrainz mappings."""

import logging
import time
from threading import Event, Lock

if __package__ == "backend.workers":
    from ..services import anime_musicbrainz
else:  # Support the existing ``python backend/app.py`` entry point.
    from services import anime_musicbrainz


logger = logging.getLogger(__name__)
wake_requested = Event()
queue_lock = Lock()
queued_themes = {}
active_theme_keys = set()
job_state = {
    "running": False,
    "queued": 0,
    "completed": 0,
    "total": 0,
    "lastCompletedAt": None,
}


def _unique_themes(themes):
    unique = {}
    for theme in themes or []:
        if not isinstance(theme, dict):
            continue
        key = anime_musicbrainz.theme_mapping_key(theme)
        unique.setdefault(key, theme)
    return unique


def _queue_snapshot():
    with queue_lock:
        return set(queued_themes), set(active_theme_keys)


def mappings_for(themes):
    """Return registry/cache documents and transient states keyed by song."""
    unique = _unique_themes(themes)
    queued, active = _queue_snapshot()
    mappings = {}
    for key, theme in unique.items():
        mapping = anime_musicbrainz.stored_mapping(theme)
        if mapping is None:
            is_pending = key in queued or key in active
            if not is_pending:
                # Avoid returning a stale miss when a worker or local edit
                # committed between the first read and its queue snapshot.
                mapping = anime_musicbrainz.stored_mapping(theme)
            if mapping is None:
                mapping = anime_musicbrainz.pending_mapping(
                    theme,
                    queued=is_pending,
                )
        mappings[key] = mapping
    return mappings


def status(anime_slug, themes):
    """Return pollable aggregate state plus each theme mapping document."""
    mappings = mappings_for(themes)
    states = [mapping.get("state") for mapping in mappings.values()]
    pending = sum(state == "pending" for state in states)
    queued = sum(
        mapping.get("state") == "pending" and mapping.get("queued") is True
        for mapping in mappings.values()
    )
    return {
        "animeSlug": str(anime_slug or ""),
        "status": "pending" if pending else "complete",
        "polling": bool(queued),
        "queued": queued,
        "total": len(mappings),
        "completed": len(mappings) - pending,
        "mappings": mappings,
    }


def request_resolution(anime_slug, themes, requested_theme_ids=None):
    """Queue selected missing songs while reporting status for every theme.

    ``None`` preserves the legacy behavior of queueing all unresolved themes.
    An explicit iterable queues only themes whose ``id`` or stable mapping key
    appears in it; an empty iterable queues nothing.
    """
    unique = _unique_themes(themes)
    selected = unique
    if requested_theme_ids is not None:
        if isinstance(requested_theme_ids, (str, int)):
            requested_theme_ids = [requested_theme_ids]
        requested = {str(value) for value in requested_theme_ids}
        selected = {}
        for theme in themes or []:
            if not isinstance(theme, dict):
                continue
            key = anime_musicbrainz.theme_mapping_key(theme)
            theme_id = str(theme.get("id") or "")
            if key in requested or theme_id in requested:
                selected.setdefault(key, theme)
    missing = {
        key: theme
        for key, theme in selected.items()
        if anime_musicbrainz.stored_mapping(theme) is None
    }
    if missing:
        with queue_lock:
            for key, theme in missing.items():
                if key not in active_theme_keys:
                    queued_themes.setdefault(key, theme)
            job_state["queued"] = len(queued_themes)
        wake_requested.set()
    return status(anime_slug, unique.values())


def _drain_queue():
    """Resolve the current queue snapshot; requests arriving later stay queued."""
    with queue_lock:
        batch = list(queued_themes.items())
        queued_themes.clear()
        active_theme_keys.update(key for key, _theme in batch)
        job_state.update(
            running=bool(batch),
            queued=0,
            completed=0,
            total=len(batch),
        )

    for index, (key, theme) in enumerate(batch, start=1):
        persist_to_cache = False
        try:
            mapping = anime_musicbrainz.stored_mapping(theme)
            if mapping is None:
                mapping = anime_musicbrainz.resolve_theme(theme)
                persist_to_cache = mapping.get("mappingSource") not in {
                    "local",
                    "seed",
                }
        except Exception:
            logger.exception("Anime MusicBrainz resolution failed")
            mapping = anime_musicbrainz.failed_mapping(theme)
            persist_to_cache = True
        try:
            if persist_to_cache:
                anime_musicbrainz.cache_mapping(theme, mapping)
        except Exception:
            logger.exception("Could not cache an anime MusicBrainz mapping")
        finally:
            with queue_lock:
                active_theme_keys.discard(key)
                job_state["completed"] = index

    with queue_lock:
        job_state.update(
            running=False,
            queued=len(queued_themes),
            lastCompletedAt=time.time() if batch else job_state["lastCompletedAt"],
        )
    return len(batch)


def run():
    """Resolve queued anime songs at MusicBrainz background priority."""
    while True:
        wake_requested.wait()
        wake_requested.clear()
        _drain_queue()
        with queue_lock:
            more_work = bool(queued_themes)
        if more_work:
            wake_requested.set()
