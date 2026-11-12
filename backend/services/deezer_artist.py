"""Finite Deezer selections derived from the same MB artist metadata as Spotify."""

import time
from urllib.parse import quote

import requests

if __package__ == "backend.services":
    from .. import detail_cache
    from ..api_cache import cache_document_lock, get_cache_document, set_cache_document
    from ..config import MUSICBRAINZ_METADATA_CACHE_TTL
    from . import deezer, musicbrainz
else:
    import detail_cache
    from api_cache import cache_document_lock, get_cache_document, set_cache_document
    from config import MUSICBRAINZ_METADATA_CACHE_TTL
    from services import deezer, musicbrainz

SELECTION_VERSION = 1
SELECTION_TTL = MUSICBRAINZ_METADATA_CACHE_TTL
FAN_TTL = 7 * 24 * 60 * 60
RETRY_TTL = 15 * 60
SELECTION_NAMESPACE = "deezer-artist:selection-v1"
FAN_NAMESPACE = "deezer-artist:fans-v1"
ARTIST_INCLUDES = "aliases+url-rels+genres"


def cached_selection(artist_mbid):
    value = get_cache_document(SELECTION_NAMESPACE, str(artist_mbid).casefold(), allow_expired=True)
    if not isinstance(value, dict):
        return {}
    selected = value.get("deezer_artist_id")
    if selected is not None and not deezer.valid_artist_id(selected):
        return {}
    return value


def artist_metadata(artist_mbid, *, cache_only=False):
    value = (musicbrainz.cached_artist_metadata(artist_mbid) if cache_only else
             musicbrainz.get(f"/artist/{quote(artist_mbid)}", ARTIST_INCLUDES, priority="background"))
    if not isinstance(value, dict) or str(value.get("id") or "").casefold() != artist_mbid.casefold():
        if cache_only:
            return None
        raise requests.RequestException("MusicBrainz returned the wrong artist identity")
    return value


def _due(selection):
    if selection.get("selection_version") != SELECTION_VERSION:
        return True
    deadline = selection.get("expires_at", 0) if selection.get("complete") else selection.get("retry_at", 0)
    return deadline <= time.time()


def _save(artist_mbid, value, previous, ttl):
    set_cache_document(SELECTION_NAMESPACE, artist_mbid, value, ttl)
    if previous.get("deezer_artist_id") != value.get("deezer_artist_id"):
        detail_cache.invalidate(("artist", artist_mbid))
    return value


def _fans(artist_id, *, force=False):
    # Share candidate counts between artists and coalesce concurrent detail/Summary reads.
    with cache_document_lock(FAN_NAMESPACE, artist_id):
        value = None if force else get_cache_document(FAN_NAMESPACE, artist_id)
        if value is not None:
            try:
                return deezer.validate_artist(value, artist_id)["nb_fan"]
            except requests.RequestException:
                pass
        value = deezer.validate_artist(deezer.artist(artist_id), artist_id)
        document = {"id": artist_id, "nb_fan": value["nb_fan"], "fetched_at": time.time()}
        set_cache_document(FAN_NAMESPACE, artist_id, document, FAN_TTL)
        return document["nb_fan"]


def select(artist_mbid, artist=None, *, loader=None):
    """One MB relationship is immediate; multiple candidates require every fan count."""
    artist_mbid = str(artist_mbid).casefold()
    with cache_document_lock(SELECTION_NAMESPACE, artist_mbid):
        previous = cached_selection(artist_mbid)
        source = artist if artist is not None else artist_metadata(artist_mbid, cache_only=True)
        if source is None and not _due(previous):
            return previous
        if source is None:
            source = (loader or artist_metadata)(artist_mbid)
        candidates = deezer.artist_relationship_ids(source.get("relations"))
        if previous.get("candidate_ids") == candidates and not _due(previous):
            return previous
        now = time.time()
        invalid = set(previous.get("invalid_ids") or [])
        counts, failed = {}, False
        if len(candidates) == 1:
            if candidates[0] in invalid:
                failed = True
        elif len(candidates) > 1:
            for artist_id in candidates:
                try:
                    counts[artist_id] = _fans(artist_id, force=artist_id in invalid)
                    invalid.discard(artist_id)
                except requests.RequestException as exc:
                    failed = True
                    if isinstance(exc, deezer.ArtistUnavailable):
                        invalid.add(artist_id)
                        set_cache_document(FAN_NAMESPACE, artist_id, {}, 0)
        common = {"artist_mbid": artist_mbid, "selection_version": SELECTION_VERSION,
                  "candidate_ids": candidates, "checked_at": now}
        if failed:
            fallback_id = previous.get("deezer_artist_id")
            fallback = bool(previous.get("verified") and fallback_id in candidates and fallback_id not in invalid)
            value = {**common, "deezer_artist_id": fallback_id if fallback else None,
                     "verified": fallback, "complete": False, "provider_failure": True,
                     "method": previous.get("method") if fallback else "unresolved",
                     "verified_at": previous.get("verified_at") if fallback else None,
                     "expires_at": previous.get("expires_at", 0) if fallback else 0,
                     "retry_at": now + RETRY_TTL, "invalid_ids": sorted(invalid),
                     "invalidated_at": previous.get("invalidated_at", now)}
            return _save(artist_mbid, value, previous, SELECTION_TTL if fallback or invalid else RETRY_TTL)
        winners = candidates if len(candidates) <= 1 else [key for key, value in counts.items() if value == max(counts.values())]
        selected = winners[0] if len(winners) == 1 else None
        value = {**common, "deezer_artist_id": selected, "verified": bool(selected),
                 "complete": bool(selected), "provider_failure": False,
                 "method": "musicbrainz_relationship" if len(candidates) == 1 else "deezer_fan_count" if selected else "unresolved",
                 "verified_at": now if selected else None, "expires_at": now + SELECTION_TTL if selected else 0,
                 "retry_at": 0 if selected else now + FAN_TTL}
        return _save(artist_mbid, value, previous, SELECTION_TTL if selected else FAN_TTL)


def invalidate(artist_mbid, artist_id):
    """Known missing/wrong profiles cannot survive as outage fallback selections."""
    artist_mbid = str(artist_mbid).casefold()
    with cache_document_lock(SELECTION_NAMESPACE, artist_mbid):
        previous = cached_selection(artist_mbid)
        invalid = set(previous.get("invalid_ids") or []) | {int(artist_id)}
        value = {**previous, "deezer_artist_id": None, "verified": False, "complete": False,
                 "provider_failure": True, "expires_at": 0, "retry_at": 0,
                 "invalid_ids": sorted(invalid), "invalidated_at": time.time()}
        set_cache_document(FAN_NAMESPACE, artist_id, {}, 0)
        _save(artist_mbid, value, previous, SELECTION_TTL)


def confirm_single_profile(artist_mbid, artist_id):
    """Restore a sole relationship after its normal Top Tracks request succeeds."""
    artist_mbid = str(artist_mbid).casefold()
    with cache_document_lock(SELECTION_NAMESPACE, artist_mbid):
        previous = cached_selection(artist_mbid)
        if previous.get("candidate_ids") != [artist_id]:
            return previous
        now = time.time()
        value = {**previous, "deezer_artist_id": artist_id, "complete": True, "verified": True,
                 "provider_failure": False, "method": "musicbrainz_relationship", "invalid_ids": [],
                 "verified_at": now, "expires_at": now + SELECTION_TTL, "retry_at": 0}
        return _save(artist_mbid, value, previous, SELECTION_TTL)


def snapshot_artist_id(snapshot):
    row_ids = {row.get("deezer_artist_id") for row in snapshot.get("entries") or [] if row.get("deezer_artist_id")}
    return snapshot.get("deezer_artist_id") or (next(iter(row_ids)) if len(row_ids) == 1 else None)


def refresh_needed(artist_mbid, snapshot, *, legacy=None):
    """Read-only freshness dependency; never make provider requests while polling."""
    selection = cached_selection(artist_mbid)
    source = artist_metadata(artist_mbid, cache_only=True)
    snapshot_id = snapshot_artist_id(snapshot)
    if source is not None:
        candidates = deezer.artist_relationship_ids(source.get("relations"))
        if selection and selection.get("candidate_ids") != candidates:
            return True
        if not selection and (len(candidates) > 1 or (snapshot_id and candidates != [snapshot_id])):
            return True
    if selection:
        if _due(selection):
            return True
        # An outage with no valid fallback retains old Top Tracks until retry.
        if selection.get("provider_failure") and not selection.get("deezer_artist_id"):
            return False
        return snapshot_id != selection.get("deezer_artist_id")
    if snapshot.get("deezer_selection_version") == SELECTION_VERSION:
        deadline = snapshot.get("deezer_selection_expires_at") or snapshot.get("deezer_selection_retry_at", 0)
        return deadline <= time.time()
    # Old permanent ambiguous mappings are deliberately never imported.
    return bool(legacy and not legacy.get("complete"))
