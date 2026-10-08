"""Supplemental snapshots and explainable identities; canonical metadata stays in MB."""

import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import contextmanager
from datetime import date
from threading import Lock
from urllib.parse import quote, urlsplit
from uuid import UUID, uuid4

import requests

if __package__ == "backend.services":
    from .. import track_search_index
    from ..api_cache import _cache_operation, cache_document_lock, document_cache_key, get_cache_document, set_cache_document
    from . import deezer, musicbrainz, wikipedia
else:
    import track_search_index
    from api_cache import _cache_operation, cache_document_lock, document_cache_key, get_cache_document, set_cache_document
    from services import deezer, musicbrainz, wikipedia

logger = logging.getLogger(__name__)
TOP_TRACKS_TTL = 24 * 60 * 60
BIO_TTL = 30 * 24 * 60 * 60
UNRESOLVED_TTL = 7 * 24 * 60 * 60
RETRY_TTL = 15 * 60
# Freshness is explicit in documents so normal expired-row cleanup cannot delete
# stale successes or long-lived identities. These remain inspectable in api_cache.
RETENTION_TTL = 100 * 365 * 24 * 60 * 60
SNAPSHOT_NAMESPACE = "artist-summary:snapshot-v1"
IDENTITY_NAMESPACE = "artist-summary:identity-v1"
STATE_NAMESPACE = "artist-summary:refresh-v1"
PROGRESS_NAMESPACE = "artist-summary:progress-v1"
PROGRESS_TTL = 30 * 60
# Shared across Summary jobs, rather than a new pool for every artist.
_resolution_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="summary-identity")
# Preserve successful mappings; retry older negative results and snapshots once.
RESOLVER_VERSION = 7


@contextmanager
def _timed(stage, artist_mbid, track_id=None):
    started = time.perf_counter()
    try:
        yield
    finally:
        logger.info("Artist Summary timing stage=%s artist_mbid=%s deezer_track_id=%s duration_ms=%.1f",
                    stage, artist_mbid, track_id, (time.perf_counter() - started) * 1000)


def _id(value):
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return None


def _isrc(track):
    return str(track.get("isrc") or "").replace("-", "").strip().upper()


def _mb_call(path, call, *args, **kwargs):
    """Attach a generated resource path, never a configured URL or headers."""
    try:
        return call(*args, **kwargs)
    except Exception as exc:
        exc.artist_summary_resource = path
        raise


def _log_failure(message, track, exc):
    response = getattr(exc, "response", None)
    code = _isrc(track)
    logger.warning(
        "%s: %s deezer_track_id=%s isrc=%s http_status=%s musicbrainz_path=%s",
        message, type(exc).__name__, int(track["id"]),
        code if re.fullmatch(r"[A-Z]{2}[A-Z0-9]{3}[0-9]{7}", code) else None,
        getattr(response, "status_code", None), getattr(exc, "artist_summary_resource", None),
    )


def _names(credits):
    return {
        track_search_index.normalize_text(credit.get("name") or (credit.get("artist") or {}).get("name"))
        for credit in credits or [] if isinstance(credit, dict)
    } - {""}


def contributor_names(track):
    contributors = [credit for credit in track.get("contributors") or [] if credit.get("role") in {"Main", "Featured"}]
    return _names(contributors or [track.get("artist") or {}])


def _artist_credited(credits, artist_mbid):
    return artist_mbid in {_id((item.get("artist") or {}).get("id"))
                           for item in credits or [] if isinstance(item, dict)}


def _title_matches(track, recording, *, allow_mastering=False):
    """Compare the base title separately from narrowly recognized version evidence."""
    normalize = track_search_index.normalize_text
    title = normalize(recording.get("title"))
    base = normalize(track.get("title_short") or track.get("title"))
    full = normalize(track.get("title"))
    if not base or title not in {base, full}:
        return False
    version = normalize(track.get("title_version"))
    if not version:
        return True
    if _remaster_year(version) is not None:
        # ISRC linkage constrains candidates; mastering can describe releases.
        # Unlinked fallback search retains the stricter full-title requirement.
        return allow_mastering or (title == full and version in normalize(f"{recording.get('title', '')} {recording.get('disambiguation', '')}"))
    versions = {"explicit", "explicit version"} if version in {"explicit", "explicit version"} else {version}
    return (normalize(recording.get("disambiguation")) in versions
            or title in {f"{base} {value}" for value in versions}
            or (title == full and any(title.endswith(f" {value}") for value in versions)))


def _duration_matches(track, recording):
    try:
        seconds, milliseconds = float(track["duration"]), float(recording["length"])
        return seconds > 0 and milliseconds > 0 and abs(seconds * 1000 - milliseconds) <= 5000
    except (KeyError, ValueError, TypeError):
        return False


def _recording_selection(track, candidates, artist_mbid, *, fallback=False, release_context=True):
    """Resolve only a unique strongest candidate, never provider/search ordering."""
    candidates = list({_id(item.get("id")): item for item in candidates if _id(item.get("id"))}.values())
    if not candidates:
        return None, "unresolved", []
    if len(candidates) == 1 and not fallback:
        return _id(candidates[0]["id"]), "exact_isrc", []
    names = contributor_names(track)
    exact_credit = [item for item in candidates if names and _names(item.get("artist-credit")) == names
                    and _artist_credited(item.get("artist-credit"), artist_mbid)]
    if fallback:
        valid = [item for item in exact_credit if _title_matches(track, item) and _duration_matches(track, item)]
        return (_id(valid[0]["id"]), "fallback_search", []) if len(valid) == 1 else (None, "unresolved", [])
    if exact_credit:
        candidates = exact_credit
        if len(candidates) == 1:
            return _id(candidates[0]["id"]), "isrc_artist_credit", []
    else:
        return None, "unresolved", []
    titled = [item for item in candidates if _title_matches(track, item, allow_mastering=True)]
    timed = [item for item in titled if _duration_matches(track, item)]
    if len(timed) == 1:
        return _id(timed[0]["id"]), "isrc_title_duration", []
    if not release_context:
        return None, "unresolved", timed
    contextual = [(item, _recording_context_score(track, item, artist_mbid)) for item in timed]
    contextual = [(item, score) for item, score in contextual if score is not None]
    if contextual:
        best = max(score for _, score in contextual)
        winners = [item for item, score in contextual if score == best]
        if len(winners) == 1:
            return _id(winners[0]["id"]), "isrc_album_context", []
    return None, "unresolved", timed


def select_recording(track, candidates, artist_mbid, *, fallback=False):
    return _recording_selection(track, candidates, artist_mbid, fallback=fallback)[:2]


def _hydrate_recording_releases(releases, recording_mbid):
    """Verify missing track identities with the existing bounded release helper."""
    def missing(release):
        if any(_id((item.get("recording") or {}).get("id")) == recording_mbid
               for medium in release.get("media") or [] for item in medium.get("tracks") or []):
            return False
        return not release.get("media") or any(
            not medium.get("tracks") or any(not _id((item.get("recording") or {}).get("id")) for item in medium["tracks"])
            for medium in release["media"]
        )

    if len(releases) > 50 and any(missing(release) for release in releases):
        exc = requests.RequestException("Incomplete release context exceeds hydration budget")
        exc.artist_summary_resource = "/release"
        raise exc
    hydrated = []
    for release in releases:
        if missing(release):
            release_id = _id(release.get("id"))
            path = f"/release/{release_id}"
            release = _mb_call(path, musicbrainz.release_track_metadata, release_id, priority="background")
            if _id(release.get("id")) != release_id or missing(release):
                exc = requests.RequestException("Incomplete recording release track identities")
                exc.artist_summary_resource = path
                raise exc
        hydrated.append(release)
    cached = track_search_index.cached_release_groups((release.get("release-group") or {}).get("id") for release in hydrated)
    return [{**release, "release-group": {**cached.get((release.get("release-group") or {}).get("id"), {}), **(release.get("release-group") or {})}}
            for release in hydrated]


def _album_context_candidates(candidates):
    """Use complete cached release collections before hydrating missing ones."""
    cached = {}
    for candidate in candidates:
        mbid = candidate["id"]
        for include_urls in (True, False):
            releases = _mb_call("/release", musicbrainz.browse_releases_by_recording, mbid,
                                priority="background", include_url_relations=include_urls, cache_only=True)
            if releases is not None:
                cached[mbid] = releases
                break
    hydrated = []
    for candidate in candidates:
        mbid = candidate["id"]
        releases = cached.get(mbid)
        if releases is None:
            releases = _mb_call("/release", musicbrainz.browse_releases_by_recording, mbid,
                                priority="background", include_url_relations=True)
        hydrated.append({**candidate, "releases": _hydrate_recording_releases(releases, mbid)})
    return hydrated


def resolve_recording(track, artist_mbid):
    code = _isrc(track)
    candidates = []
    if re.fullmatch(r"[A-Z]{2}[A-Z0-9]{3}[0-9]{7}", code):
        candidates, complete = track_search_index.cached_recordings_by_isrc(code)
        if not complete:
            path, inc = f"/isrc/{code}", "artist-credits"
            try:
                value = _mb_call(path, musicbrainz.get, path, inc, priority="background")
            except requests.HTTPError as exc:
                if exc.response is None or exc.response.status_code != 404:
                    raise
                value = {"isrc": code, "recordings": []}
            candidates = value.get("recordings") if isinstance(value, dict) else None
            if (not isinstance(candidates, list)
                    or value.get("recording-count", len(candidates)) != len(candidates)
                    or any(not isinstance(item, dict) or not _id(item.get("id")) for item in candidates)):
                exc = requests.RequestException("Incomplete ISRC candidate set")
                exc.artist_summary_resource = path
                raise exc
            track_search_index.index_recording_document(value, musicbrainz.metadata_cache_key(path, inc))
        if candidates:
            # Embedded releases can be partial; compare complete browsed/cached
            # collections only after credit/title/duration checks remain tied.
            mbid, method, remaining = _recording_selection(track, candidates, artist_mbid, release_context=False)
            if mbid or len(remaining) < 2 or not track_search_index.normalize_text((track.get("album") or {}).get("title")):
                return mbid, method
            return select_recording(track, _album_context_candidates(remaining), artist_mbid)
    # Search only when an ISRC genuinely has no results (or is absent), never
    # after ambiguity or a provider outage. Local title evidence is tried first.
    title = track.get("title") or ""
    local = track_search_index.exact_track_matches(artist_mbid, title)
    local_recordings = []
    for match in local:
        mbid = match.get("recording_mbid")
        if not _id(mbid):
            continue
        value = track_search_index.cached_recording_metadata(mbid) or musicbrainz.get(
            f"/recording/{mbid}", "artist-credits+isrcs", priority="background", cache_only=True,
        )
        if value:
            local_recordings.append(value)
    result = select_recording(track, local_recordings, artist_mbid, fallback=True)
    if result[0]:
        return result
    if not title or not track.get("duration") or not contributor_names(track):
        return None, "unresolved"
    escaped = re.sub(r'([+\-!(){}\[\]^"~*?:\\/])', r"\\\1", str(title))
    value = _mb_call("/recording", musicbrainz.search, f'arid:{artist_mbid} AND recording:"{escaped}"', "recording", priority="background", limit=100)
    candidates = value.get("recordings") or []
    if int(value.get("count", value.get("recording-count", len(candidates)))) > len(candidates):
        exc = requests.RequestException("Incomplete recording search results")
        exc.artist_summary_resource = "/recording"
        raise exc
    return select_recording(track, candidates, artist_mbid, fallback=True)


def _remaster_year(value, *, allow_channels=False):
    """Recognize only remaster(ed) and one optional year, in either order.

    An empty string means a recognized remaster without a year; None means
    unrelated edition text. Token order does not change the edition meaning.
    Mono/stereo wording is allowed only when reading MusicBrainz comments.
    """
    tokens = track_search_index.normalize_text(value).split()
    markers = [token for token in tokens if token in {"remaster", "remastered"}]
    years = [token for token in tokens if re.fullmatch(r"[0-9]{4}", token)]
    channels = [token for token in tokens if allow_channels and token in {"mono", "stereo"}]
    if (len(markers) != 1 or len(years) > 1 or len(channels) > 1
            or len(tokens) != len(markers) + len(years) + len(channels)):
        return None
    return years[0] if years else ""


def _album_edition_title(value):
    """Recognize only a trailing remaster or deluxe album qualifier."""
    match = re.fullmatch(r"(.+?)\s*\(([^()]*)\)", str(value or "").strip())
    if match:
        year = _remaster_year(match[2])
        if year is not None:
            return track_search_index.normalize_text(match[1]), "remaster", year
        if track_search_index.normalize_text(match[2]) in {"deluxe", "deluxe edition", "deluxe version"}:
            return track_search_index.normalize_text(match[1]), "deluxe", None
    return track_search_index.normalize_text(value), None, None


def _remaster_album_title(value):
    """Keep remaster-only evidence separate from other album editions."""
    base, edition, year = _album_edition_title(value)
    return (base, year) if edition == "remaster" else (track_search_index.normalize_text(value), None)


def _album_number_spacing(value):
    """Album-only key: join ASCII letters/digits across whitespace, nothing else."""
    normalized = track_search_index.normalize_text(value)
    return re.sub(r"(?<=[a-z]) (?=[0-9])|(?<=[0-9]) (?=[a-z])", "", normalized)


def _album_edition_matches(title, release, compare):
    """Apply the same controlled edition/year rules with a given base-title key."""
    titles = [release.get("title"), (release.get("release-group") or {}).get("title")]
    base, edition, year = _album_edition_title(title)
    if not base or edition is None:
        return False
    base = compare(base)
    if edition == "remaster":
        # Preserve remaster-only parsing, including other meaningful base text.
        matching = [part for part in map(_remaster_album_title, titles) if compare(part[0]) == base]
        if not matching:
            return False
        # Missing comments are normal; recognized year conflicts are contrary
        # evidence. Unrelated disambiguation text is not a title suffix.
        evidence_years = [part[1] for part in matching] + [_remaster_year(release.get("disambiguation"), allow_channels=True)]
        if year and any(candidate_year and candidate_year != year for candidate_year in evidence_years):
            return False
    elif not any(compare(part[0]) == base and part[1] in {None, edition} for part in map(_album_edition_title, titles)):
        return False
    return True


def _album_title_match(title, release):
    """Rank exact (2), controlled edition (1), then number-spacing fallback (0.5)."""
    normalize = track_search_index.normalize_text
    titles = [release.get("title"), (release.get("release-group") or {}).get("title")]
    normalized_title = normalize(title)
    if normalized_title and normalized_title in {normalize(value) for value in titles}:
        return 2
    if _album_edition_matches(title, release, normalize):
        return 1
    base, edition, _ = _album_edition_title(title)
    if not base:
        return 0
    if edition is None:
        # Unrecognized qualifiers stay in the full title; never normalize the
        # qualifier into a recognized edition or strip it to obtain a match.
        spaced = _album_number_spacing(base)
        if any(part[1] is None and _album_number_spacing(part[0]) == spaced
               for part in map(_album_edition_title, titles)):
            return 0.5
    elif _album_edition_matches(title, release, _album_number_spacing):
        return 0.5
    return 0


def _release_date_match(album, release):
    """Prefer exact original group dates, then edition dates; never expand partials."""
    provider_date = str(album.get("release_date") or "")
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", provider_date):
        return 0
    try:
        date.fromisoformat(provider_date)
    except ValueError:
        return 0
    # Remastered provider albums may still report the original album date.
    if provider_date == (release.get("release-group") or {}).get("first-release-date"):
        return 3
    return 2 if provider_date == release.get("date") else 0


def _release_candidate(recording_mbid, track, release, artist_mbid):
    """Shared exact-containment/artist/status guards and existing group scoring."""
    album = track.get("album") or {}
    if not any(_id((item.get("recording") or {}).get("id")) == recording_mbid
               for medium in release.get("media") or [] for item in medium.get("tracks") or []):
        return None
    group = release.get("release-group") or {}
    mbid = _id(group.get("id"))
    if not mbid or str(release.get("status") or "").casefold() in {"bootleg", "pseudo-release", "withdrawn", "cancelled"}:
        return None
    direct = album.get("id") and any(deezer.relationship_id(entity.get("relations"), "album") == int(album["id"]) for entity in (release, group))
    title_match = _album_title_match(album.get("title"), release)
    if not direct and not title_match:
        return None
    artist_match = _artist_credited(group.get("artist-credit") or release.get("artist-credit"), artist_mbid)
    if not direct and not artist_match:
        return None
    secondary = {str(item).casefold() for item in group.get("secondary-types") or []}
    if not direct and secondary.intersection({"compilation", "dj-mix", "mixtape/street"}):
        return None
    year = str(album.get("release_date") or "")[:4]
    year_match = bool(year and year == str(release.get("date") or group.get("first-release-date") or "")[:4])
    date_match = _release_date_match(album, release) or int(year_match)
    score = (bool(direct), title_match, artist_match, date_match, not bool(secondary))
    if title_match == 2:
        method = "recording_album_title"
    elif title_match == 0.5:
        method = "recording_album_number_spacing"
    else:
        method = "recording_album_remaster" if _album_edition_title(album.get("title"))[1] == "remaster" else "recording_album_edition"
    return mbid, score, "recording_deezer_album" if direct else method


def _recording_context_score(track, recording, artist_mbid):
    """Compare each recording's strongest compatible release, never release count."""
    requested_year = _remaster_year(track.get("title_version"))
    if requested_year is None:
        requested_year = _remaster_album_title((track.get("album") or {}).get("title"))[1]
    scores = []
    for release in recording.get("releases") or []:
        candidate = _release_candidate(_id(recording["id"]), track, release, artist_mbid)
        if candidate is None or not candidate[1][2]:
            continue
        remaster = 0
        if requested_year is not None:
            group = release.get("release-group") or {}
            years = [_remaster_album_title(value)[1] for value in (release.get("title"), group.get("title"))]
            years.append(_remaster_year(release.get("disambiguation"), allow_channels=True))
            if requested_year and any(year and year != requested_year for year in years):
                continue
            remaster = 2 if requested_year and requested_year in years else int(any(year is not None for year in years))
        score = candidate[1]
        scores.append((*score[:3], remaster, *score[3:]))
    return max(scores) if scores else None


def select_release_group(recording_mbid, track, releases, artist_mbid):
    """Titles compare only inside releases proven to contain this exact recording."""
    candidates = {}
    for release in releases:
        candidate = _release_candidate(recording_mbid, track, release, artist_mbid)
        if candidate is None:
            continue
        mbid, score, method = candidate
        if mbid not in candidates or score > candidates[mbid][0]:
            candidates[mbid] = (score, method)
    if not candidates:
        return None, "unresolved"
    best = max(item[0] for item in candidates.values())
    winners = [(mbid, item[1]) for mbid, item in candidates.items() if item[0] == best]
    return winners[0] if len(winners) == 1 else (None, "unresolved")


def _identity(key):
    value = get_cache_document(IDENTITY_NAMESPACE, key)
    if value and not value.get("complete") and key.startswith(("track:", "group:")) and value.get("resolver_version") != RESOLVER_VERSION:
        return None
    if value and (value.get("complete") or value.get("retry_at", 0) > time.time()):
        return value
    return None


def _save_identity(key, value, complete):
    value.update(complete=bool(complete), resolver_version=RESOLVER_VERSION,
                 resolved_at=time.time(), retry_at=0 if complete else time.time() + UNRESOLVED_TTL)
    set_cache_document(IDENTITY_NAMESPACE, key, value, RETENTION_TTL)
    return value


def _provider_failure(value):
    return {**value, "complete": False, "provider_failure": True,
            "resolver_version": RESOLVER_VERSION, "retry_at": time.time() + RETRY_TTL}


def _save_provider_failure(key, value):
    value = _provider_failure(value)
    # Expire the backoff itself; failures never enter the seven-day negative cache.
    set_cache_document(IDENTITY_NAMESPACE, key, value, RETRY_TTL)
    return value


def artist_relations(artist_mbid):
    # Reuse the exact include set used by the normal artist page first.
    value = musicbrainz.get(f"/artist/{quote(artist_mbid)}", "aliases+url-rels+genres", priority="background")
    if _id(value.get("id")) != artist_mbid:
        raise requests.RequestException("MusicBrainz returned the wrong artist identity")
    return value


def _cached_recording_identity(track):
    track_id = int(track["id"])
    recording = _identity(f"track:{track_id}")
    code = _isrc(track)
    if recording and re.fullmatch(r"[A-Z]{2}[A-Z0-9]{3}[0-9]{7}", code) and recording.get("isrc") and code != str(recording["isrc"]).replace("-", "").upper():
        # A changed explicit ISRC is contrary identity evidence, not an ordinary
        # ordering refresh. Re-resolve this track instead of perpetuating it.
        recording = None
    return recording


def _recording_identity(track, artist_mbid):
    track_id = int(track["id"])
    recording = _cached_recording_identity(track)
    code = _isrc(track)
    if recording is None:
        try:
            mbid, method = resolve_recording(track, artist_mbid)
        except Exception:
            _save_provider_failure(f"track:{track_id}", {
                "source": "deezer", "deezer_track_id": track_id, "isrc": code or None,
                "recording_mbid": None, "recording_resolution_method": "unresolved",
            })
            raise
        recording = _save_identity(f"track:{track_id}", {
            "source": "deezer", "deezer_track_id": track_id, "isrc": code or None,
            "recording_mbid": mbid, "recording_resolution_method": method,
        }, mbid)
    return recording


def _group_identity_key(track, recording):
    album = track.get("album") or {}
    return f"group:{recording['recording_mbid']}:{album.get('id')}:{track_search_index.normalize_text(album.get('title'))}"


def _cached_summary_mapping(track):
    """Publish reusable identities without waiting behind uncached resolutions."""
    recording = _cached_recording_identity(track)
    if recording is None:
        return None
    result = {**recording, "release_group_mbid": None, "release_group_resolution_method": "unresolved"}
    if not recording.get("recording_mbid"):
        return result
    mapping = _identity(_group_identity_key(track, recording))
    return {**result, **mapping} if mapping is not None else None


def resolve_track(track, artist_mbid, *, on_recording=None):
    track_id = int(track["id"])
    with _timed("recording_resolution", artist_mbid, track_id), \
            cache_document_lock(IDENTITY_NAMESPACE, f"track:{track_id}"):
        recording = _recording_identity(track, artist_mbid)
    result = {**recording, "release_group_mbid": None, "release_group_resolution_method": "unresolved"}
    if not recording.get("recording_mbid"):
        return result
    if on_recording:
        on_recording({**result, "release_group_resolution_method": "pending"})
    key = _group_identity_key(track, recording)
    with _timed("release_group_resolution", artist_mbid, track_id), cache_document_lock(IDENTITY_NAMESPACE, key):
        return _release_group_identity(track, artist_mbid, recording, result, key)


def _release_group_identity(track, artist_mbid, recording, result, key):
    album = track.get("album") or {}
    mapping = _identity(key)
    if mapping is None:
        try:
            releases = _mb_call("/release", musicbrainz.browse_releases_by_recording, recording["recording_mbid"], priority="background", include_url_relations=True)
        except Exception as exc:
            _log_failure("Artist Summary recording releases failed", track, exc)
            return _save_provider_failure(key, result)
        # Some mirrors omit media/track identities on browse; hydrate only
        # bounded candidates; incomplete oversized sets use provider backoff.
        if len(releases) > 50 and any(not release.get("media") for release in releases):
            exc = requests.RequestException("Incomplete release context exceeds hydration budget")
            exc.artist_summary_resource = "/release"
            _log_failure("Artist Summary recording releases failed", track, exc)
            return _save_provider_failure(key, result)
        cached_groups = track_search_index.cached_release_groups(
            (release.get("release-group") or {}).get("id") for release in releases
        )
        hydrated = []
        for release in releases:
            if not release.get("media") or any("recording" not in item for medium in release.get("media") or [] for item in medium.get("tracks") or []):
                try:
                    release = _mb_call(f"/release/{_id(release['id'])}", musicbrainz.release_track_metadata, release["id"], priority="background")
                except Exception as exc:
                    _log_failure("Artist Summary release detail failed", track, exc)
                    return _save_provider_failure(key, result)
            group = release.get("release-group") or {}
            release = {**release, "release-group": {**cached_groups.get(group.get("id"), {}), **group}}
            hydrated.append(release)
        mbid, method = select_release_group(recording["recording_mbid"], track, hydrated, artist_mbid)
        mapping = _save_identity(key, {
            "recording_mbid": recording["recording_mbid"], "deezer_album_id": album.get("id"),
            "release_group_mbid": mbid, "release_group_resolution_method": method,
        }, mbid)
        for release in hydrated:
            group = release.get("release-group") or {}
            if group.get("id") == mbid:
                track_search_index.index_release_groups([group])
    return {**result, **mapping}


def _ordering_fresh(value):
    return bool(value and not value.get("pending") and value.get("fetched_at", 0) + TOP_TRACKS_TTL > time.time())


def _identity_retry_due(value, entry):
    if entry.get("provider_failure"):
        return entry.get("retry_at", 0) <= time.time()
    return value.get("resolver_version") != RESOLVER_VERSION and (
        not entry.get("recording_mbid") or not entry.get("release_group_mbid")
    )


def refresh_state_key(artist_mbid, source):
    # Old top-track leases/backoffs must not block the one-time resolver repair.
    prefix = f"{source}:resolver-v{RESOLVER_VERSION}" if source == "top_tracks" else source
    return f"{prefix}:{artist_mbid}"


def _summary_mapping(details, artist_mbid, on_recording=None):
    try:
        if on_recording is not None:
            return resolve_track(details, artist_mbid, on_recording=on_recording)
        return resolve_track(details, artist_mbid)
    except Exception as exc:
        _log_failure("Artist Summary identity resolution failed", details, exc)
        return _provider_failure({"recording_mbid": None, "release_group_mbid": None,
                                  "recording_resolution_method": "unresolved", "release_group_resolution_method": "unresolved"})


def _provider_cover(details):
    """Allow only public Deezer cover CDN metadata for informational rows."""
    album = details.get("album") or {}
    for source in (album.get("cover_small"), album.get("cover")):
        try:
            url = urlsplit(str(source or ""))
            if (url.scheme == "https" and url.hostname == "cdn-images.dzcdn.net" and not url.port
                    and not url.username and not url.password and not url.query and not url.fragment
                    and url.path.startswith("/images/cover/")):
                return str(source)
        except ValueError:
            continue
    return None


def _track_entry(details, mapping, track_id, position, artist_id, *, pending=False, details_pending=False):
    album = details.get("album") or {}
    return {
        **mapping, "position": position, "source": "deezer", "deezer_artist_id": artist_id,
        "pending": pending, "details_pending": details_pending, "coverArt": _provider_cover(details),
        "deezer_track_id": track_id, "deezer_album_id": album.get("id"), "isrc": details.get("isrc"),
        **{key: details.get(key) for key in ("title", "title_short", "title_version", "artist", "contributors", "duration", "rank")},
        "album": {key: album.get(key) for key in ("id", "title", "release_date")},
    }


def progress_snapshot(artist_mbid, refresh_id):
    if not refresh_id:
        return None
    return get_cache_document(PROGRESS_NAMESPACE, f"top_tracks:{artist_mbid}:{refresh_id}")


def publish_for_refresh(namespace, document_id, value, ttl, artist_mbid, source, refresh_id):
    """Fence final/state writes atomically against a replacement refresh lease."""
    if not refresh_id:
        set_cache_document(namespace, document_id, value, ttl)
        return True
    now = time.time()
    state_key = document_cache_key(STATE_NAMESPACE, refresh_state_key(artist_mbid, source))

    def publish(connection):
        return connection.execute(
            "INSERT INTO api_cache (cache_key, value, expires_at) "
            "SELECT ?, ?, ? WHERE EXISTS (SELECT 1 FROM api_cache WHERE cache_key = ? "
            "AND expires_at > ? AND json_extract(value, '$.refresh_id') = ?) "
            "ON CONFLICT(cache_key) DO UPDATE SET value=excluded.value, expires_at=excluded.expires_at",
            (document_cache_key(namespace, document_id), json.dumps(value), now + ttl, state_key, now, refresh_id),
        ).rowcount == 1

    return _cache_operation(publish, locked_default=False, description="publish supplemental refresh")


class _TopTracksProgress:
    """Serialize row updates without replacing the retained completed snapshot."""

    def __init__(self, artist_mbid, previous, entries, started):
        self.artist_mbid, self.previous, self.started = artist_mbid, previous, started
        state = get_cache_document(STATE_NAMESPACE, refresh_state_key(artist_mbid, "top_tracks")) or {}
        self.lease_id = state.get("refresh_id") if state.get("status") == "pending" else None
        self.refresh_id = self.lease_id or uuid4().hex
        self.entries, self.lock, self.revision = list(entries), Lock(), 0
        self.publish()

    def publish(self, index=None, entry=None):
        with self.lock:
            if index is not None:
                self.entries[index] = entry
            self.revision += 1
            set_cache_document(PROGRESS_NAMESPACE, f"top_tracks:{self.artist_mbid}:{self.refresh_id}", {
                "entries": self.entries, "pending": True, "refresh_id": self.refresh_id,
                "revision": self.revision,
            }, PROGRESS_TTL)
            if self.revision == 1:
                logger.info("Artist Summary timing stage=first_top_tracks_snapshot artist_mbid=%s refresh_id=%s duration_ms=%.1f rows=%s",
                            self.artist_mbid, self.refresh_id, (time.perf_counter() - self.started) * 1000, len(self.entries))

    def resolve(self, index, details, track_id, position, artist_id):
        def recording_ready(mapping):
            self.publish(index, _track_entry(details, mapping, track_id, position, artist_id, pending=True))
        mapping = _summary_mapping(details, self.artist_mbid, recording_ready)
        self.publish(index, _track_entry(details, mapping, track_id, position, artist_id))

    def reuse(self, index, details, track_id, position, artist_id):
        with _timed("identity_cache_lookup", self.artist_mbid, track_id):
            mapping = _cached_summary_mapping(details)
        if mapping is None:
            return False
        self.publish(index, _track_entry(details, mapping, track_id, position, artist_id))
        return True

    def finish(self, **metadata):
        value = {**self.previous, **metadata, "entries": self.entries, "resolver_version": RESOLVER_VERSION}
        # An expired/reclaimed lease must not publish an old refresh's final data.
        if not publish_for_refresh(SNAPSHOT_NAMESPACE, f"top_tracks:{self.artist_mbid}", value, RETENTION_TTL,
                                   self.artist_mbid, "top_tracks", self.lease_id):
            return value
        logger.info("Artist Summary timing stage=final_top_tracks_snapshot artist_mbid=%s refresh_id=%s duration_ms=%.1f rows=%s",
                    self.artist_mbid, self.refresh_id, (time.perf_counter() - self.started) * 1000, len(self.entries))
        return value


def _pending_mapping():
    return {"recording_mbid": None, "release_group_mbid": None,
            "recording_resolution_method": "pending", "release_group_resolution_method": "pending"}


def _repair_top_tracks(artist_mbid, previous, started):
    """Repair identities without extending/refetching the daily provider ordering."""
    entries = previous.get("entries") or []
    progress = _TopTracksProgress(artist_mbid, previous, [
        {**entry, "pending": True} if _identity_retry_due(previous, entry) else entry for entry in entries
    ], started)
    futures = []
    try:
        for index, entry in enumerate(entries):
            if not _identity_retry_due(previous, entry):
                continue
            track_id = int(entry["deezer_track_id"])
            details = {**entry, "id": track_id}
            if entry.get("details_missing") or not entry.get("duration") or not (entry.get("contributors") or entry.get("artist")):
                try:
                    with _timed("deezer_track_detail", artist_mbid, track_id):
                        details = deezer.track(track_id)
                except Exception as exc:
                    _log_failure("Artist Summary track detail failed", details, exc)
                    progress.publish(index, _provider_failure({**entry, "details_missing": True, "pending": False}))
                    continue
            if not progress.reuse(index, details, track_id, entry["position"], entry.get("deezer_artist_id")):
                futures.append(_resolution_executor.submit(progress.resolve, index, details, track_id, entry["position"], entry.get("deezer_artist_id")))
    finally:
        wait(futures)
    for future in futures:
        future.result()
    return progress.finish(identity_refreshed_at=time.time())


def refresh_top_tracks(artist_mbid):
    with _timed("refresh_top_tracks", artist_mbid):
        return _refresh_top_tracks(artist_mbid, time.perf_counter())


def _refresh_top_tracks(artist_mbid, started):
    previous = snapshot(artist_mbid, "top_tracks") or {}
    if _ordering_fresh(previous) and any(_identity_retry_due(previous, entry) for entry in previous.get("entries") or []):
        return _repair_top_tracks(artist_mbid, previous, started)
    identity = _identity(f"artist:{artist_mbid}")
    if identity is None:
        artist = artist_relations(artist_mbid)
        artist_id = deezer.relationship_id(artist.get("relations"))
        identity = _save_identity(f"artist:{artist_mbid}", {
            "artist_mbid": artist_mbid, "deezer_artist_id": artist_id, "method": "musicbrainz_relationship" if artist_id else "unresolved",
        }, artist_id)
    artist_id = identity.get("deezer_artist_id")
    entries, ordered = [], []
    if artist_id:
        with _timed("deezer_top_tracks", artist_mbid):
            top_tracks = deezer.top_tracks(artist_id)
        for position, item in enumerate(top_tracks, 1):
            try:
                track_id = int(item["id"])
                if track_id <= 0:
                    raise ValueError
            except (TypeError, KeyError, ValueError):
                continue
            ordered.append((position, track_id, item))
            entries.append(_track_entry(item, _pending_mapping(), track_id, position, artist_id, pending=True, details_pending=True))
    progress = _TopTracksProgress(artist_mbid, previous, entries, started)
    futures = []
    try:
        previous_entries = {str(item.get("deezer_track_id")): item for item in previous.get("entries") or []}
        for index, (position, track_id, item) in enumerate(ordered):
            try:
                with _timed("deezer_track_detail", artist_mbid, track_id):
                    details = deezer.track(track_id)
            except Exception as exc:
                _log_failure("Artist Summary track detail failed", {**item, "id": track_id}, exc)
                if str(track_id) in previous_entries:
                    progress.publish(index, _provider_failure({**previous_entries[str(track_id)], "position": position,
                                                               "details_missing": True, "pending": False}))
                    continue
                # Never guess identities from the reduced top-list object.
                mapping = _provider_failure({"recording_mbid": None, "release_group_mbid": None,
                                             "recording_resolution_method": "unresolved", "release_group_resolution_method": "unresolved",
                                             "details_missing": True})
                progress.publish(index, _track_entry(item, mapping, track_id, position, artist_id))
                continue
            progress.publish(index, _track_entry(details, _pending_mapping(), track_id, position, artist_id, pending=True))
            if not progress.reuse(index, details, track_id, position, artist_id):
                futures.append(_resolution_executor.submit(progress.resolve, index, details, track_id, position, artist_id))
    finally:
        wait(futures)
    for future in futures:
        future.result()
    return progress.finish(fetched_at=time.time(), provider="deezer")


def refresh_bio(artist_mbid):
    with _timed("refresh_bio", artist_mbid):
        return _refresh_bio(artist_mbid)


def _refresh_bio(artist_mbid):
    value = wikipedia.bio(artist_relations(artist_mbid).get("relations"))
    previous = snapshot(artist_mbid, "bio")
    if not value and previous and previous.get("bio"):
        raise requests.RequestException("Wikipedia no longer supplied usable lead text")
    document = {"fetched_at": time.time(), "bio": value}
    set_cache_document(SNAPSHOT_NAMESPACE, f"bio:{artist_mbid}", document, RETENTION_TTL)
    return document


def snapshot(artist_mbid, source):
    return get_cache_document(SNAPSHOT_NAMESPACE, f"{source}:{artist_mbid}")


def fresh(value, source):
    if not value:
        return False
    if source == "top_tracks":
        return _ordering_fresh(value) and not any(_identity_retry_due(value, entry) for entry in value.get("entries") or [])
    ttl = BIO_TTL if value.get("bio") else UNRESOLVED_TTL
    return value.get("fetched_at", 0) + ttl > time.time()
