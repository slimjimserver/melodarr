"""MusicBrainz and Cover Art Archive client operations."""

import logging
import math
import time
import unicodedata
from contextlib import contextmanager
from threading import Lock, local
from urllib.parse import quote, urlsplit, urlunsplit
from uuid import UUID
from xml.etree import ElementTree

import requests
from pykakasi import kakasi

logger = logging.getLogger(__name__)

if __package__ == "backend.services":
    from ..api_cache import cache_key, cached_json_get
    from ..config import (
        COVER_ART_ARCHIVE_URL,
        MUSICBRAINZ_METADATA_CACHE_TTL,
        MUSICBRAINZ_REQUEST_INTERVAL_MS,
        MUSICBRAINZ_SEARCH_CACHE_TTL,
        MUSICBRAINZ_URL,
        USER_AGENT,
    )
    from ..storage import get_service
else:  # Support the existing `python backend/app.py` entry point.
    from api_cache import cache_key, cached_json_get
    from config import (
        COVER_ART_ARCHIVE_URL,
        MUSICBRAINZ_METADATA_CACHE_TTL,
        MUSICBRAINZ_REQUEST_INTERVAL_MS,
        MUSICBRAINZ_SEARCH_CACHE_TTL,
        MUSICBRAINZ_URL,
        USER_AGENT,
    )
    from storage import get_service


TEST_RECORDING_ID = "5f396c8b-ae2e-48de-afbc-904f4f0d66fc"
VARIOUS_ARTISTS_ID = "89ad4ac3-39f7-470e-963a-56509c546377"
RELEASE_TRACK_INCLUDES = "recordings+artist-credits+release-groups+isrcs"
RECORDING_RELEASE_INCLUDES = "recordings+release-groups+artist-credits+media"
SEARCH_UNAVAILABLE_MESSAGE = (
    "MusicBrainz search is unavailable. If this is a self-hosted server, "
    "check that its search/Solr service is running and reachable from the "
    "MusicBrainz web service."
)
_request_lock = Lock()
_next_request_at = 0.0
_critical_waiters = 0
_interactive_waiters = 0
_prefetch_waiters = 0
_critical_streak = 0
_critical_operations = 0
_critical_operation_deadlines = {}
_CRITICAL_OPERATION_PAUSE_SECONDS = 30.0
_CRITICAL_BURST_LIMIT = 2
_BACKGROUND_COOLDOWN_INITIAL_SECONDS = 30.0
_BACKGROUND_COOLDOWN_MAX_SECONDS = 60.0
_background_lock = Lock()
_background_failure_streak = 0
_background_resume_at = 0.0
_session_state = local()
_romanizer = kakasi()


def is_library_only_artist(mbid):
    """Apply the local-library exception to this one special artist identity."""
    return str(mbid or "").casefold() == VARIOUS_ARTISTS_ID


class ConfigurationError(ValueError):
    """Raised when MusicBrainz service settings are invalid."""


class IncompatibleServerError(ValueError):
    """Raised when an endpoint does not behave like MusicBrainz WS2."""


class SearchUnavailableError(IncompatibleServerError):
    """Raised when WS2 lookups work but the search service does not."""


def search_error_message(error):
    """Return a safe, actionable message for a failed search request."""
    response = getattr(error, "response", None)
    if response is not None and response.status_code == 503:
        return SEARCH_UNAVAILABLE_MESSAGE
    return ""


def _normalized_base_url(value):
    if not isinstance(value, str):
        raise ConfigurationError("MusicBrainz WS2 base URL must be text.")
    base_url = value.strip()
    if not base_url:
        raise ConfigurationError("Enter a MusicBrainz WS2 base URL.")
    try:
        parsed = urlsplit(base_url)
        parsed_port = parsed.port
    except ValueError as exc:
        raise ConfigurationError("Enter a valid MusicBrainz WS2 base URL.") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigurationError(
            "MusicBrainz WS2 base URL must use HTTP or HTTPS."
        )
    if parsed.username or parsed.password:
        raise ConfigurationError(
            "MusicBrainz WS2 base URL must not include credentials."
        )
    if parsed.query or parsed.fragment:
        raise ConfigurationError(
            "MusicBrainz WS2 base URL must not include a query or fragment."
        )
    if not parsed.hostname or (
        parsed_port is not None and not 1 <= parsed_port <= 65535
    ):
        raise ConfigurationError("Enter a valid MusicBrainz WS2 base URL.")
    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc,
            parsed.path.rstrip("/"),
            "",
            "",
        )
    )


def _request_interval_ms(value):
    if isinstance(value, bool):
        raise ConfigurationError(
            "MusicBrainz request interval must be a whole number from 0 to 60000."
        )
    try:
        interval = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(
            "MusicBrainz request interval must be a whole number from 0 to 60000."
        ) from exc
    if (
        not math.isfinite(interval)
        or not interval.is_integer()
        or not 0 <= interval <= 60000
    ):
        raise ConfigurationError(
            "MusicBrainz request interval must be a whole number from 0 to 60000."
        )
    return int(interval)


def configuration(values=None):
    """Return validated persisted settings or validate a proposed configuration."""
    source = (get_service("musicbrainz") or {}) if values is None else values
    if not isinstance(source, dict):
        raise ConfigurationError("MusicBrainz settings must be a JSON object.")
    user_agent_value = source.get("userAgent", USER_AGENT)
    if not isinstance(user_agent_value, str):
        raise ConfigurationError("MusicBrainz user agent must be text.")
    user_agent = user_agent_value.strip()
    if not user_agent:
        raise ConfigurationError("Enter a MusicBrainz user agent.")
    if len(user_agent) > 512:
        raise ConfigurationError(
            "MusicBrainz user agent must be 512 characters or fewer."
        )
    return {
        "baseUrl": _normalized_base_url(source.get("baseUrl", MUSICBRAINZ_URL)),
        "userAgent": user_agent,
        "requestIntervalMs": _request_interval_ms(
            source.get("requestIntervalMs", MUSICBRAINZ_REQUEST_INTERVAL_MS)
        ),
    }


def reset_request_pacing():
    """Apply a changed interval immediately in the process that saved it."""
    global _next_request_at
    with _request_lock:
        _next_request_at = 0.0


def _http_get(*args, **kwargs):
    """Reuse TLS connections within each request/background worker thread."""
    session = getattr(_session_state, "session", None)
    if session is None:
        session = requests.Session()
        _session_state.session = session
    return session.get(*args, **kwargs)


def _wait_for_background_circuit():
    """Pause speculative work while leaving user-initiated calls unaffected."""
    while True:
        with _background_lock:
            delay = _background_resume_at - time.monotonic()
        if delay <= 0:
            return
        time.sleep(delay)


def _record_background_failure(exc):
    global _background_failure_streak, _background_resume_at
    with _background_lock:
        _background_failure_streak += 1
        delay = min(
            _BACKGROUND_COOLDOWN_MAX_SECONDS,
            _BACKGROUND_COOLDOWN_INITIAL_SECONDS
            * (2 ** (_background_failure_streak - 1)),
        )
        _background_resume_at = max(
            _background_resume_at,
            time.monotonic() + delay,
        )
    logger.warning(
        "MusicBrainz background requests paused for %.0f seconds after "
        "a transport failure: %s",
        delay,
        exc,
    )


def _record_background_success():
    global _background_failure_streak, _background_resume_at
    with _background_lock:
        _background_failure_streak = 0
        _background_resume_at = 0.0


def _priority_is_blocked(priority):
    """Apply burst fairness while keeping speculative work at the back."""
    if priority == "critical":
        return bool(
            _interactive_waiters and _critical_streak >= _CRITICAL_BURST_LIMIT
        )
    if priority == "interactive":
        return bool(
            _critical_waiters and _critical_streak < _CRITICAL_BURST_LIMIT
        )
    # A very large catalogue (especially Various Artists) can take hours to
    # assemble. Reserve the first few pages, then let enrichment make progress
    # while the operation continues to use critical priority for live slots.
    critical_operation_paused = bool(_critical_operations) and any(
        deadline > time.monotonic()
        for deadline in tuple(_critical_operation_deadlines.values())
    )
    if priority == "prefetch":
        return bool(critical_operation_paused or _critical_waiters or _interactive_waiters)
    return bool(
        critical_operation_paused
        or _critical_waiters
        or _interactive_waiters
        or _prefetch_waiters
    )


def _wait_for_request_slot(priority="interactive", request_interval_seconds=None):
    """Pace live calls in discography, click, prefetch, background order."""
    global _critical_waiters, _interactive_waiters, _prefetch_waiters
    global _critical_streak, _next_request_at
    if request_interval_seconds is None:
        request_interval_seconds = configuration()["requestIntervalMs"] / 1000
    if priority not in {"critical", "interactive", "prefetch", "background"}:
        priority = "interactive"
    if priority in {"critical", "interactive", "prefetch"}:
        with _request_lock:
            if priority == "critical":
                _critical_waiters += 1
            elif priority == "interactive":
                _interactive_waiters += 1
            else:
                _prefetch_waiters += 1
    try:
        while True:
            with _request_lock:
                blocked = _priority_is_blocked(priority)
                if blocked:
                    delay = 0.05
                else:
                    now = time.monotonic()
                    delay = max(0.0, _next_request_at - now)
                    if not delay:
                        _next_request_at = now + request_interval_seconds
                        if priority == "critical":
                            _critical_streak += 1
                        else:
                            _critical_streak = 0
                        return
            time.sleep(delay)
    finally:
        if priority in {"critical", "interactive", "prefetch"}:
            with _request_lock:
                if priority == "critical":
                    _critical_waiters -= 1
                elif priority == "interactive":
                    _interactive_waiters -= 1
                else:
                    _prefetch_waiters -= 1


@contextmanager
def critical_operation():
    """Briefly reserve artist-page slots without indefinitely pausing workers."""
    global _critical_operations
    token = object()
    with _request_lock:
        _critical_operations += 1
        _critical_operation_deadlines[token] = (
            time.monotonic() + _CRITICAL_OPERATION_PAUSE_SECONDS
        )
    try:
        yield
    finally:
        with _request_lock:
            _critical_operations -= 1
            del _critical_operation_deadlines[token]


def _cached_get(
    url,
    priority="interactive",
    request_interval_seconds=None,
    **kwargs,
):
    """Apply MusicBrainz pacing and bounded transient-error retries."""
    max_attempts = 5 if priority == "critical" else 3

    def before_request():
        if priority == "background":
            _wait_for_background_circuit()
        _wait_for_request_slot(priority, request_interval_seconds)

    def after_response(_response):
        if priority == "background":
            _record_background_success()

    try:
        return cached_json_get(
            url,
            before_request=before_request,
            retry_statuses={429, 500, 502, 503, 504},
            retry_exceptions=(requests.Timeout, requests.ConnectionError),
            max_attempts=max_attempts,
            retry_backoff=1.0,
            request_timeout=20 if priority == "critical" else 15,
            request_get=_http_get,
            after_response=after_response,
            # A critical operation pauses speculative requests. Sharing their
            # miss lock would deadlock if a paused owner held this cache key.
            # The response cache remains shared, and is rechecked after pacing.
            coalescing_scope=priority,
            **kwargs,
        )
    except (requests.Timeout, requests.ConnectionError) as exc:
        if priority == "background":
            _record_background_failure(exc)
        raise


def search(
    query,
    search_type,
    include_cache_status=False,
    priority="interactive",
    plain_search=False,
    limit=None,
):
    """Search a supported MusicBrainz entity using Melodarr's search names."""
    resources = {
        "artist": "artist",
        "album": "release-group",
        "release-group": "release-group",
        "release": "release",
        "track": "recording",
        "recording": "recording",
    }
    resource = resources.get(search_type)
    if resource is None:
        raise ValueError(f"Unsupported MusicBrainz search type: {search_type}")
    if limit is None:
        limit = 100 if resource == "release-group" else 25
    params = {"query": query, "fmt": "json", "limit": limit}
    if plain_search:
        params["dismax"] = "true"
    config = configuration()
    return _cached_get(
        f"{config['baseUrl']}/{resource}/",
        params=params,
        headers={"User-Agent": config["userAgent"]},
        namespace="musicbrainz-search",
        ttl=MUSICBRAINZ_SEARCH_CACHE_TTL,
        include_cache_status=include_cache_status,
        priority=priority,
        request_interval_seconds=config["requestIntervalMs"] / 1000,
    )


def lookup_urls(resources, *, priority="background", include_cache_status=False):
    """Look up up to 40 exact URL resources in one paced, cached request."""
    resources = list(resources)
    if not 1 <= len(resources) <= 40:
        raise ValueError("URL lookup requires between 1 and 40 resources")
    config = configuration()
    params = [("resource", resource) for resource in resources]
    params.extend((("inc", "release-rels+release-group-rels"), ("fmt", "json")))
    return _cached_get(
        f"{config['baseUrl']}/url", params=params,
        headers={"User-Agent": config["userAgent"]},
        namespace="musicbrainz-url", ttl=MUSICBRAINZ_SEARCH_CACHE_TTL,
        include_cache_status=include_cache_status, priority=priority,
        request_interval_seconds=config["requestIntervalMs"] / 1000,
    )


def get(
    path,
    inc,
    include_cache_status=False,
    priority="interactive",
    force_refresh=False,
    cache_only=False,
    cache_response=True,
    cache_ttl=None,
    **extra,
):
    """Load one metadata resource or collection from MusicBrainz."""
    if is_library_only_artist(extra.get("artist")) and path.rstrip("/") in {
        "/release-group", "/release", "/recording",
    }:
        if cache_only:
            return (None, False) if include_cache_status else None
        raise requests.RequestException("Various Artists uses only the local library catalogue.")
    params = {"fmt": "json", **extra}
    if inc:
        params["inc"] = inc
    config = configuration()
    return _cached_get(
        f"{config['baseUrl']}{path}",
        params=params,
        headers={"User-Agent": config["userAgent"]},
        namespace="musicbrainz-metadata",
        ttl=MUSICBRAINZ_METADATA_CACHE_TTL if cache_ttl is None else cache_ttl,
        include_cache_status=include_cache_status,
        priority=priority,
        request_interval_seconds=config["requestIntervalMs"] / 1000,
        force_refresh=force_refresh,
        cache_only=cache_only,
        cache_response=cache_response,
    )


def browse_releases_by_recording(
    recording_id, *, priority="interactive", cache_ttl=None, force_refresh=False,
    include_url_relations=False, cache_only=False,
):
    """Read every directly linked release, rejecting incomplete page sets.

    MusicBrainz can return fewer than limit releases because of its track
    budget. Only the actual number returned advances the offset.
    Cache-only reads return None unless every page is already available.
    """
    recording_id = str(UUID(str(recording_id)))
    releases, seen, offset, expected_total = [], set(), 0, None
    while True:
        page = get(
            "/release", RECORDING_RELEASE_INCLUDES + ("+url-rels" if include_url_relations else ""), priority=priority,
            recording=recording_id, limit=100, offset=offset,
            cache_ttl=cache_ttl, force_refresh=force_refresh,
            **({"cache_only": True} if cache_only else {}),
        )
        if page is None and cache_only:
            return None
        try:
            total = page["release-count"]
            actual_offset = page.get("release-offset", offset)
            batch = page["releases"]
            if (
                isinstance(total, bool) or not isinstance(total, int) or total < 0
                or isinstance(actual_offset, bool) or not isinstance(actual_offset, int)
                or actual_offset != offset
                or not isinstance(batch, list) or offset + len(batch) > total
                or (not batch and offset < total)
                or (expected_total is not None and total != expected_total)
            ):
                raise ValueError
            ids = [str(UUID(str(release["id"]))) for release in batch]
            if len(set(ids)) != len(ids) or seen.intersection(ids):
                raise ValueError
        except (TypeError, KeyError, ValueError, AttributeError) as exc:
            raise requests.RequestException(
                "MusicBrainz returned an incomplete recording release collection."
            ) from exc
        expected_total = total
        releases.extend(batch)
        seen.update(ids)
        offset += len(batch)
        if offset == total:
            return releases


def release_track_metadata(release_id, *, priority="background"):
    """Reuse release documents in the shared cache, upgrading old includes once."""
    if __package__ == "backend.services":
        from .. import track_search_index
    else:
        import track_search_index
    cached = track_search_index.cached_musicbrainz_release(release_id)
    if cached is not None:
        return cached
    path = f"/release/{quote(release_id)}"
    metadata = get(path, RELEASE_TRACK_INCLUDES, priority=priority)
    track_search_index.index_release(metadata, metadata_cache_key(path, RELEASE_TRACK_INCLUDES))
    return metadata


def recordings_by_track_ids(track_ids, *, priority="background"):
    """Batch exact tid searches; reject ambiguous or truncated results."""
    track_ids = sorted(set(track_ids))
    if not 1 <= len(track_ids) <= 25:
        raise ValueError("Track-ID search requires between 1 and 25 IDs")
    query = " OR ".join(f"tid:{track_id}" for track_id in track_ids)
    response = search(query, "recording", priority=priority, limit=100)
    recordings = response.get("recordings", [])
    if int(response.get("recording-count", len(recordings))) > len(recordings):
        return {}
    candidates = {track_id: {} for track_id in track_ids}
    for recording in recordings:
        recording_id = recording.get("id")
        if not recording_id:
            continue
        matched_ids = {
            track.get("id") for release in recording.get("releases", [])
            for medium in release.get("media", [])
            for track in (medium.get("tracks") or medium.get("track") or [])
        }
        # A single exact tid search can uniquely identify a recording even
        # when the search server omits the nested release track list.
        if len(track_ids) == 1 and len(recordings) == 1:
            matched_ids.add(track_ids[0])
        for track_id in matched_ids & candidates.keys():
            candidates[track_id][recording_id] = recording
    mappings = {}
    recording_metadata = {}
    if len(track_ids) > 1 and recordings:
        # Some search servers omit nested track IDs from recording results.
        # Exact single-ID queries disambiguate only those unattributed IDs;
        # the worker still bounds and paces the entire fallback pass.
        for track_id, matches in candidates.items():
            if not matches:
                resolved = recordings_by_track_ids([track_id], priority=priority)
                if track_id in resolved:
                    recording = resolved[track_id]
                    matches[recording["id"]] = recording
    for track_id, matches in candidates.items():
        if len(matches) != 1:
            continue
        recording_id, recording = next(iter(matches.items()))
        if "isrcs" not in recording:
            if recording_id not in recording_metadata:
                recording_metadata[recording_id] = get(
                    f"/recording/{quote(recording_id)}", "isrcs", priority=priority,
                )
            recording = {**recording, **recording_metadata[recording_id]}
        mappings[track_id] = recording
    return mappings


def artist_entity_counts(mbid, priority="background"):
    """Fetch release-group, release and recording totals in one paced lookup.

    MusicBrainz omits these linked-list totals from its JSON artist lookup, so
    this narrow probe uses the XML representation instead.
    """
    config = configuration()
    url = f"{config['baseUrl']}/artist/{mbid}"
    retry_statuses = {429, 500, 502, 503, 504}
    attempts = 5 if priority == "critical" else 3
    for attempt in range(attempts):
        if priority == "background":
            _wait_for_background_circuit()
        _wait_for_request_slot(priority, config["requestIntervalMs"] / 1000)
        try:
            response = _http_get(
                url,
                params={
                    "inc": "release-groups+releases+recordings",
                    "fmt": "xml",
                },
                headers={"User-Agent": config["userAgent"]},
                timeout=20 if priority == "critical" else 15,
            )
            if response.status_code in retry_statuses and attempt + 1 < attempts:
                retry_after = response.headers.get("Retry-After", "")
                try:
                    delay = max(0.0, float(retry_after))
                except (TypeError, ValueError):
                    delay = 2 ** attempt
                time.sleep(delay)
                continue
            response.raise_for_status()
            root = ElementTree.fromstring(response.content)
            artist = root.find("{*}artist")
            if artist is None or artist.get("id", "").casefold() != mbid.casefold():
                raise requests.RequestException("MusicBrainz returned the wrong artist.")
            counts = {}
            for name, key in (
                ("release-group-list", "releaseGroupCount"),
                ("release-list", "releaseCount"),
                ("recording-list", "recordingCount"),
            ):
                linked = artist.find(f"{{*}}{name}")
                try:
                    count = int(linked.get("count")) if linked is not None else -1
                except (TypeError, ValueError) as exc:
                    raise requests.RequestException(
                        f"MusicBrainz returned an invalid {name} count."
                    ) from exc
                if count < 0:
                    raise requests.RequestException(
                        f"MusicBrainz omitted the {name} count."
                    )
                counts[key] = count
            if priority == "background":
                _record_background_success()
            return counts
        except (requests.Timeout, requests.ConnectionError) as exc:
            if attempt + 1 >= attempts:
                if priority == "background":
                    _record_background_failure(exc)
                raise
            time.sleep(2 ** attempt)
        except ElementTree.ParseError as exc:
            raise requests.RequestException(
                "MusicBrainz returned invalid artist count XML."
            ) from exc


def metadata_cache_record(path, inc, value, **extra):
    """Describe one MusicBrainz response for an atomic staged cache commit."""
    params = {"fmt": "json", **extra}
    if inc:
        params["inc"] = inc
    config = configuration()
    return {
        "namespace": "musicbrainz-metadata",
        "url": f"{config['baseUrl']}{path}",
        "params": params,
        "value": value,
        "ttl": MUSICBRAINZ_METADATA_CACHE_TTL,
    }


def metadata_cache_key(path, inc, **extra):
    """Return the persistent key used by a MusicBrainz metadata request."""
    record = metadata_cache_record(path, inc, None, **extra)
    return cache_key(record["namespace"], record["url"], record["params"])


def test_connection(values):
    """Verify that an endpoint supports MusicBrainz WS2 lookup and search."""
    config = configuration(values)
    started_at = time.monotonic()
    response = _http_get(
        f"{config['baseUrl']}/recording/{TEST_RECORDING_ID}",
        params={"fmt": "json"},
        headers={
            "Accept": "application/json",
            "User-Agent": config["userAgent"],
        },
        timeout=15,
        allow_redirects=False,
    )
    if 300 <= response.status_code < 400:
        raise IncompatibleServerError(
            "MusicBrainz redirected the WS2 test request. "
            "Use the final WS2 base URL."
        )
    response.raise_for_status()
    try:
        payload = response.json()
    except ValueError as exc:
        raise IncompatibleServerError(
            "The server did not return valid MusicBrainz WS2 JSON."
        ) from exc
    if (
        not isinstance(payload, dict)
        or payload.get("id") != TEST_RECORDING_ID
        or not isinstance(payload.get("title"), str)
    ):
        raise IncompatibleServerError(
            "The server responded, but it did not return a compatible "
            "MusicBrainz WS2 recording."
        )
    request_interval_seconds = config["requestIntervalMs"] / 1000
    if request_interval_seconds:
        time.sleep(request_interval_seconds)
    search_response = _http_get(
        f"{config['baseUrl']}/artist/",
        params={
            "query": "3 Doors Down",
            "fmt": "json",
            "limit": 1,
            "dismax": "true",
        },
        headers={
            "Accept": "application/json",
            "User-Agent": config["userAgent"],
        },
        timeout=15,
        allow_redirects=False,
    )
    if 300 <= search_response.status_code < 400:
        raise IncompatibleServerError(
            "MusicBrainz redirected the WS2 search test request. "
            "Use the final WS2 base URL."
        )
    if search_response.status_code >= 500:
        raise SearchUnavailableError(
            "Direct MusicBrainz lookups work, but search is unavailable. "
            "If this is a self-hosted server, check that its search/Solr "
            "service is running and reachable from the MusicBrainz web service."
        )
    search_response.raise_for_status()
    try:
        search_payload = search_response.json()
    except ValueError as exc:
        raise IncompatibleServerError(
            "The server did not return valid MusicBrainz WS2 search JSON."
        ) from exc
    if not isinstance(search_payload, dict) or not isinstance(
        search_payload.get("artists"), list
    ):
        raise IncompatibleServerError(
            "The server responded, but it did not return a compatible "
            "MusicBrainz WS2 artist search."
        )
    return {
        "message": (
            "Connected to MusicBrainz WS2; lookup and search are working "
            f"(resolved {payload['title']})."
        ),
        "baseUrl": config["baseUrl"],
        "latencyMs": max(0, round((time.monotonic() - started_at) * 1000)),
        "recording": {
            "id": payload["id"],
            "title": payload["title"],
        },
    }


def cover_art_url(mbid, size=250):
    """Return the Cover Art Archive front-cover URL for a release group."""
    return f"{COVER_ART_ARCHIVE_URL}/release-group/{quote(mbid)}/front-{size}"


def _is_latin_name(value):
    """Return whether every letter in a non-empty name uses Latin script."""
    letters = [character for character in str(value or "") if character.isalpha()]
    return bool(letters) and all(
        "LATIN" in unicodedata.name(character, "") for character in letters
    )


def _latin_alias(entity, canonical):
    """Choose the best English or Latin-script alias for an entity."""
    aliases = [
        alias
        for alias in entity.get("aliases") or []
        if _is_latin_name(alias.get("name"))
        and str(alias.get("name") or "").strip().casefold() != canonical.casefold()
    ]
    priorities = (
        lambda alias: alias.get("locale") == "en" and alias.get("primary") is True,
        lambda alias: alias.get("locale") == "en",
        lambda alias: alias.get("primary") is True,
        lambda _alias: True,
    )
    for matches in priorities:
        alias = next((item for item in aliases if matches(item)), None)
        if alias:
            return str(alias["name"]).strip()
    return ""


def romanized_artist_name(artist):
    """Choose a distinct English or Latin-script name for an artist."""
    canonical = str(artist.get("name") or "").strip()
    if not canonical or _is_latin_name(canonical):
        return ""

    alias = _latin_alias(artist, canonical)
    if alias:
        return alias

    sort_name = str(
        artist.get("sort-name") or artist.get("sortName") or ""
    ).strip()
    return sort_name if _is_latin_name(sort_name) else ""


def romanized_release_group_title(group):
    """Return an English alias or locally romanized Japanese release title."""
    canonical = str(group.get("title") or "").strip()
    if not canonical or _is_latin_name(canonical):
        return ""

    alias = _latin_alias(group, canonical)
    if alias:
        return alias

    return romanized_track_title(canonical)


def romanized_track_title(title):
    """Romanize a track title locally without requesting or choosing aliases."""
    canonical = str(title or "").strip()
    if not canonical or _is_latin_name(canonical):
        return ""

    romanized = "".join(
        item["hepburn"] for item in _romanizer.convert(canonical)
    ).strip().translate(str.maketrans({"、": ",", "。": "."}))
    if not _is_latin_name(romanized) or romanized.casefold() == canonical.casefold():
        return ""
    return romanized[:1].upper() + romanized[1:]
