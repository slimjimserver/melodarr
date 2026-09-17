"""MusicBrainz and Cover Art Archive client operations."""

import logging
import math
import time
import unicodedata
from contextlib import contextmanager
from threading import Lock, local
from urllib.parse import quote, urlsplit, urlunsplit

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
_CRITICAL_BURST_LIMIT = 2
_BACKGROUND_COOLDOWN_INITIAL_SECONDS = 30.0
_BACKGROUND_COOLDOWN_MAX_SECONDS = 60.0
_background_lock = Lock()
_background_failure_streak = 0
_background_resume_at = 0.0
_session_state = local()
_romanizer = kakasi()


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
    if priority == "prefetch":
        return bool(_critical_operations or _critical_waiters or _interactive_waiters)
    return bool(
        _critical_operations
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
    """Keep speculative work paused without starving other user actions."""
    global _critical_operations
    with _request_lock:
        _critical_operations += 1
    try:
        yield
    finally:
        with _request_lock:
            _critical_operations -= 1


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
):
    """Search a supported MusicBrainz entity using Melodarr's search names."""
    resources = {
        "artist": "artist",
        "album": "release-group",
        "release-group": "release-group",
        "track": "recording",
        "recording": "recording",
    }
    resource = resources.get(search_type)
    if resource is None:
        raise ValueError(f"Unsupported MusicBrainz search type: {search_type}")
    params = {"query": query, "fmt": "json", "limit": 25}
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

    romanized = "".join(
        item["hepburn"] for item in _romanizer.convert(canonical)
    ).strip().translate(str.maketrans({"、": ",", "。": "."}))
    if not _is_latin_name(romanized) or romanized.casefold() == canonical.casefold():
        return ""
    return romanized[:1].upper() + romanized[1:]
