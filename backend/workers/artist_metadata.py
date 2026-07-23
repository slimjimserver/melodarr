"""Opportunistic MusicBrainz freshness checks for cached artist pages."""

import logging
import sqlite3
import time
from contextlib import contextmanager, nullcontext
from threading import Event, Lock

import requests

if __package__ == "backend.workers":
    from .. import detail_cache
    from ..api_cache import (
        commit_json_responses,
        get_cache_expiry,
        get_cache_document,
        set_cache_document,
    )
    from ..config import (
        MUSICBRAINZ_ARTIST_REVALIDATION_INTERVAL,
        MUSICBRAINZ_ARTIST_REVALIDATION_RETRY_INTERVAL,
        MUSICBRAINZ_METADATA_CACHE_TTL,
    )
    from ..services import musicbrainz
else:
    import detail_cache
    from api_cache import (
        commit_json_responses,
        get_cache_expiry,
        get_cache_document,
        set_cache_document,
    )
    from config import (
        MUSICBRAINZ_ARTIST_REVALIDATION_INTERVAL,
        MUSICBRAINZ_ARTIST_REVALIDATION_RETRY_INTERVAL,
        MUSICBRAINZ_METADATA_CACHE_TTL,
    )
    from services import musicbrainz


logger = logging.getLogger(__name__)
STATE_NAMESPACE = "musicbrainz-artist-revalidation"

wake_requested = Event()
queue_lock = Lock()
queued_artist_ids = set()
active_artist_phases = {}
_refresh_locks_lock = Lock()
_refresh_locks = {}
job_state = {
    "running": False,
    "queued": 0,
    "completed": 0,
    "lastCompletedAt": None,
}


def _cached_discography_page(mbid):
    return musicbrainz.get(
        "/release-group",
        "aliases",
        priority="background",
        cache_only=True,
        artist=mbid,
        limit=100,
        offset=0,
    )


def _discography_cache_expiry(mbid):
    key = musicbrainz.metadata_cache_key(
        "/release-group",
        "aliases",
        artist=mbid,
        limit=100,
        offset=0,
    )
    return get_cache_expiry(key)


def _release_group_count(page):
    try:
        count = int(page["release-group-count"])
    except (KeyError, TypeError, ValueError) as exc:
        raise requests.RequestException(
            "MusicBrainz returned an invalid release-group count."
        ) from exc
    if count < 0:
        raise requests.RequestException(
            "MusicBrainz returned a negative release-group count."
        )
    return count


def _read_state(mbid):
    return get_cache_document(STATE_NAMESPACE, mbid) or {}


def _write_state(mbid, **changes):
    state = {**_read_state(mbid), **changes}
    set_cache_document(
        STATE_NAMESPACE,
        mbid,
        state,
        MUSICBRAINZ_METADATA_CACHE_TTL,
    )
    return state


def _public_status(mbid, state=None):
    state = state or _read_state(mbid)
    with queue_lock:
        phase = active_artist_phases.get(mbid)
        queued = mbid in queued_artist_ids
    status_name = phase or ("queued" if queued else state.get("outcome", "idle"))
    return {
        "status": status_name,
        "polling": bool(phase or queued),
        "nextCheckAt": state.get("nextCheckAt"),
        "lastCheckedAt": state.get("lastCheckedAt"),
        "lastRefreshAt": state.get("lastRefreshAt"),
        "cachedCount": state.get("cachedCount"),
        "observedCount": state.get("observedCount"),
    }


def status(mbid):
    """Return the current or most recently persisted state for one artist."""
    return _public_status(mbid)


def request_revalidation(mbid):
    """Queue an eligible cached artist, coalescing duplicate click requests."""
    mbid = mbid.casefold()
    state = _read_state(mbid)
    now = time.time()
    with queue_lock:
        if mbid in queued_artist_ids or mbid in active_artist_phases:
            return _public_status_without_lock(mbid, state)
    if float(state.get("nextCheckAt") or 0) > now:
        return _public_status(mbid, state)

    # This endpoint must not turn arbitrary submitted MBIDs into upstream work.
    # A complete detail-cache first page proves Melodarr has shown this artist.
    cached_page = _cached_discography_page(mbid)
    if cached_page is None:
        return {
            **_public_status(mbid, state),
            "status": "not-cached",
            "polling": False,
        }
    if not state:
        expires_at = _discography_cache_expiry(mbid)
        cached_at = (
            expires_at - MUSICBRAINZ_METADATA_CACHE_TTL
            if expires_at is not None
            else 0
        )
        if cached_at + MUSICBRAINZ_ARTIST_REVALIDATION_INTERVAL > now:
            cached_count = _release_group_count(cached_page)
            state = _write_state(
                mbid,
                outcome="unchanged",
                lastCheckedAt=cached_at,
                nextCheckAt=(
                    cached_at + MUSICBRAINZ_ARTIST_REVALIDATION_INTERVAL
                ),
                cachedCount=cached_count,
                observedCount=cached_count,
            )
            return _public_status(mbid, state)

    with queue_lock:
        if mbid in queued_artist_ids or mbid in active_artist_phases:
            return _public_status_without_lock(mbid, state)
        queued_artist_ids.add(mbid)
        job_state["queued"] = len(queued_artist_ids)
        result = _public_status_without_lock(mbid, state)
    wake_requested.set()
    return result


def _public_status_without_lock(mbid, state):
    phase = active_artist_phases.get(mbid)
    queued = mbid in queued_artist_ids
    status_name = phase or ("queued" if queued else state.get("outcome", "idle"))
    return {
        "status": status_name,
        "polling": bool(phase or queued),
        "nextCheckAt": state.get("nextCheckAt"),
        "lastCheckedAt": state.get("lastCheckedAt"),
        "lastRefreshAt": state.get("lastRefreshAt"),
        "cachedCount": state.get("cachedCount"),
        "observedCount": state.get("observedCount"),
    }


def _set_phase(mbid, phase):
    with queue_lock:
        active_artist_phases[mbid] = phase


@contextmanager
def _artist_refresh_lock(mbid):
    """Serialize manual and automatic full refreshes for the same artist."""
    with _refresh_locks_lock:
        entry = _refresh_locks.get(mbid)
        if entry is None:
            entry = {"lock": Lock(), "users": 0}
            _refresh_locks[mbid] = entry
        entry["users"] += 1
        refresh_lock = entry["lock"]
    refresh_lock.acquire()
    try:
        yield
    finally:
        refresh_lock.release()
        with _refresh_locks_lock:
            entry["users"] -= 1
            if entry["users"] == 0 and _refresh_locks.get(mbid) is entry:
                del _refresh_locks[mbid]


def _stage_artist_refresh(mbid, priority, old_count):
    records = []
    raw_group_ids = []
    expected_total = None
    offset = 0
    operation = (
        musicbrainz.critical_operation()
        if priority == "critical"
        else nullcontext()
    )
    with operation:
        while True:
            page = musicbrainz.get(
                "/release-group",
                "aliases",
                priority=priority,
                force_refresh=True,
                cache_response=False,
                artist=mbid,
                limit=100,
                offset=offset,
            )
            page_total = _release_group_count(page)
            if expected_total is None:
                expected_total = page_total
            elif page_total != expected_total:
                raise requests.RequestException(
                    "MusicBrainz changed the release-group count during refresh."
                )
            batch = page.get("release-groups")
            if not isinstance(batch, list):
                raise requests.RequestException(
                    "MusicBrainz returned an invalid release-group page."
                )
            records.append(musicbrainz.metadata_cache_record(
                "/release-group",
                "aliases",
                page,
                artist=mbid,
                limit=100,
                offset=offset,
            ))
            raw_group_ids.extend(
                group.get("id") for group in batch if group.get("id")
            )
            if offset + len(batch) >= expected_total:
                break
            if not batch:
                raise requests.RequestException(
                    "MusicBrainz returned an incomplete release-group page."
                )
            offset += len(batch)

        if (
            len(raw_group_ids) != expected_total
            or len(set(raw_group_ids)) != expected_total
        ):
            raise requests.RequestException(
                "MusicBrainz returned an inconsistent release-group collection."
            )

        # Artist metadata is deliberately the final upstream request.
        artist = musicbrainz.get(
            f"/artist/{mbid}",
            "aliases+url-rels+genres",
            priority=priority,
            force_refresh=True,
            cache_response=False,
        )
        if (
            not isinstance(artist, dict)
            or str(artist.get("id") or "").casefold() != mbid
        ):
            raise requests.RequestException(
                "MusicBrainz returned the wrong artist during refresh."
            )
        records.append(musicbrainz.metadata_cache_record(
            f"/artist/{mbid}",
            "aliases+url-rels+genres",
            artist,
        ))

    old_offsets = range(0, max(1, old_count), 100)
    new_offsets = range(0, max(1, expected_total), 100)
    old_keys = {
        musicbrainz.metadata_cache_key(
            "/release-group",
            "aliases",
            artist=mbid,
            limit=100,
            offset=page_offset,
        )
        for page_offset in old_offsets
    }
    new_keys = {
        musicbrainz.metadata_cache_key(
            "/release-group",
            "aliases",
            artist=mbid,
            limit=100,
            offset=page_offset,
        )
        for page_offset in new_offsets
    }
    return records, old_keys - new_keys, expected_total


def refresh_artist_metadata(mbid, priority, *, cached_count=None):
    """Stage and atomically commit a complete artist and discography refresh."""
    mbid = mbid.casefold()
    with _artist_refresh_lock(mbid):
        if cached_count is None:
            cached_page = _cached_discography_page(mbid)
            cached_count = (
                _release_group_count(cached_page)
                if cached_page is not None
                else 0
            )
        records, obsolete_keys, total = _stage_artist_refresh(
            mbid,
            priority,
            cached_count,
        )
        cache_key = ("artist", mbid)
        with detail_cache.build_lock(cache_key):
            try:
                commit_json_responses(records, delete_keys=obsolete_keys)
            except sqlite3.Error as exc:
                raise requests.RequestException(
                    "Melodarr could not commit the refreshed metadata cache."
                ) from exc
            detail_cache.invalidate(cache_key)
        refreshed_at = time.time()
        _write_state(
            mbid,
            outcome="refreshed",
            lastAttemptAt=refreshed_at,
            lastCheckedAt=refreshed_at,
            nextCheckAt=(
                refreshed_at + MUSICBRAINZ_ARTIST_REVALIDATION_INTERVAL
            ),
            cachedCount=total,
            observedCount=total,
            lastRefreshAt=refreshed_at,
        )
        return {"count": total, "refreshedAt": refreshed_at}


def _record_failure(mbid):
    attempted_at = time.time()
    _write_state(
        mbid,
        outcome="failed",
        lastAttemptAt=attempted_at,
        nextCheckAt=(
            attempted_at + MUSICBRAINZ_ARTIST_REVALIDATION_RETRY_INTERVAL
        ),
    )


def _process_artist(mbid):
    state = _read_state(mbid)
    if float(state.get("nextCheckAt") or 0) > time.time():
        return
    cached_page = _cached_discography_page(mbid)
    if cached_page is None:
        return
    cached_count = _release_group_count(cached_page)
    attempted_at = time.time()
    _write_state(
        mbid,
        outcome="checking",
        lastAttemptAt=attempted_at,
        nextCheckAt=(
            attempted_at + MUSICBRAINZ_ARTIST_REVALIDATION_RETRY_INTERVAL
        ),
        cachedCount=cached_count,
    )
    live_page = musicbrainz.get(
        "/release-group",
        "",
        priority="background",
        force_refresh=True,
        artist=mbid,
        limit=1,
        offset=0,
    )
    observed_count = _release_group_count(live_page)
    if observed_count == cached_count:
        checked_at = time.time()
        _write_state(
            mbid,
            outcome="unchanged",
            lastCheckedAt=checked_at,
            nextCheckAt=(
                checked_at + MUSICBRAINZ_ARTIST_REVALIDATION_INTERVAL
            ),
            cachedCount=cached_count,
            observedCount=observed_count,
        )
        return

    _write_state(
        mbid,
        outcome="refreshing",
        cachedCount=cached_count,
        observedCount=observed_count,
    )
    _set_phase(mbid, "refreshing")
    refresh_artist_metadata(
        mbid,
        "background",
        cached_count=cached_count,
    )


def run():
    """Service queued artist checks without delaying interactive MB requests."""
    while True:
        wake_requested.wait()
        while True:
            with queue_lock:
                if not queued_artist_ids:
                    wake_requested.clear()
                    job_state["queued"] = 0
                    job_state["running"] = False
                    break
                mbid = queued_artist_ids.pop()
                active_artist_phases[mbid] = "checking"
                job_state["queued"] = len(queued_artist_ids)
                job_state["running"] = True
            try:
                _process_artist(mbid)
            except requests.RequestException as exc:
                _record_failure(mbid)
                logger.warning(
                    "Could not revalidate MusicBrainz artist %s: %s",
                    mbid,
                    exc,
                )
            except Exception:
                _record_failure(mbid)
                logger.exception(
                    "Unexpected MusicBrainz artist revalidation failure for %s",
                    mbid,
                )
            finally:
                with queue_lock:
                    active_artist_phases.pop(mbid, None)
                    job_state["completed"] += 1
                    job_state["lastCompletedAt"] = time.time()
