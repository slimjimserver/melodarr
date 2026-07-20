"""MusicBrainz and Cover Art Archive client operations."""

import logging
import time
from contextlib import contextmanager
from threading import Lock, local
from urllib.parse import quote

import requests


logger = logging.getLogger(__name__)

if __package__ == "backend.services":
    from ..api_cache import cached_json_get
    from ..config import (
        COVER_ART_ARCHIVE_URL,
        MUSICBRAINZ_METADATA_CACHE_TTL,
        MUSICBRAINZ_SEARCH_CACHE_TTL,
        MUSICBRAINZ_URL,
        USER_AGENT,
    )
else:  # Support the existing `python backend/app.py` entry point.
    from api_cache import cached_json_get
    from config import (
        COVER_ART_ARCHIVE_URL,
        MUSICBRAINZ_METADATA_CACHE_TTL,
        MUSICBRAINZ_SEARCH_CACHE_TTL,
        MUSICBRAINZ_URL,
        USER_AGENT,
    )


_REQUEST_INTERVAL_SECONDS = 1.1
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


def _wait_for_request_slot(priority="interactive"):
    """Pace live calls in discography, click, prefetch, background order."""
    global _critical_waiters, _interactive_waiters, _prefetch_waiters
    global _critical_streak, _next_request_at
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
                        _next_request_at = now + _REQUEST_INTERVAL_SECONDS
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


def _cached_get(url, priority="interactive", **kwargs):
    """Apply MusicBrainz pacing and bounded transient-error retries."""
    max_attempts = 5 if priority == "critical" else 3

    def before_request():
        if priority == "background":
            _wait_for_background_circuit()
        _wait_for_request_slot(priority)

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


def search(query, search_type, include_cache_status=False, priority="interactive"):
    """Search MusicBrainz for artists or release groups."""
    resource = "artist" if search_type == "artist" else "release-group"
    return _cached_get(
        f"{MUSICBRAINZ_URL}/{resource}/",
        params={"query": query, "fmt": "json", "limit": 25},
        headers={"User-Agent": USER_AGENT},
        namespace="musicbrainz-search",
        ttl=MUSICBRAINZ_SEARCH_CACHE_TTL,
        include_cache_status=include_cache_status,
        priority=priority,
    )


def get(
    path,
    inc,
    include_cache_status=False,
    priority="interactive",
    force_refresh=False,
    cache_only=False,
    **extra,
):
    """Load one metadata resource or collection from MusicBrainz."""
    params = {"fmt": "json", **extra}
    if inc:
        params["inc"] = inc
    return _cached_get(
        f"{MUSICBRAINZ_URL}{path}",
        params=params,
        headers={"User-Agent": USER_AGENT},
        namespace="musicbrainz-metadata",
        ttl=MUSICBRAINZ_METADATA_CACHE_TTL,
        include_cache_status=include_cache_status,
        priority=priority,
        force_refresh=force_refresh,
        cache_only=cache_only,
    )


def cover_art_url(mbid, size=250):
    """Return the Cover Art Archive front-cover URL for a release group."""
    return f"{COVER_ART_ARCHIVE_URL}/release-group/{quote(mbid)}/front-{size}"
