"""Lidarr configuration and HTTP client operations."""

import math
import os
import re
from datetime import datetime, timezone
from numbers import Real
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

if __package__ == "backend.services":
    from ..api_cache import cached_json_get, get_cache_document, set_cache_document
    from ..cache_memo import invalidate_document, memoized_document
    from ..config import (
        LIDARR_LIBRARY_CACHE_TTL,
        LIDARR_DOWNLOAD_CACHE_TTL,
        LIDARR_METADATA_CACHE_TTL,
        LIDARR_METADATA_URL,
        LIDARR_OPTIONS_CACHE_TTL,
        USER_AGENT,
    )
    from ..detail_cache import invalidate_all as invalidate_detail_payloads
    from ..storage import get_service
else:  # Support the existing `python backend/app.py` entry point.
    from api_cache import cached_json_get, get_cache_document, set_cache_document
    from cache_memo import invalidate_document, memoized_document
    from config import (
        LIDARR_LIBRARY_CACHE_TTL,
        LIDARR_DOWNLOAD_CACHE_TTL,
        LIDARR_METADATA_CACHE_TTL,
        LIDARR_METADATA_URL,
        LIDARR_OPTIONS_CACHE_TTL,
        USER_AGENT,
    )
    from detail_cache import invalidate_all as invalidate_detail_payloads
    from storage import get_service


LIBRARY_INDEX_KEY = "lidarr-library-index"
DOWNLOAD_SNAPSHOT_NAMESPACE = "lidarr-downloads"
DOWNLOAD_SNAPSHOT_KEY = "snapshot"
PUBLIC_DOWNLOAD_FIELDS = (
    "progress", "status", "trackedDownloadStatus", "trackedDownloadState",
    "timeLeft", "estimatedCompletionTime",
)
TIME_LEFT_PATTERN = re.compile(
    r"^(?:(?P<days>\d+)\.)?(?P<hours>\d+):"
    r"(?P<minutes>[0-5]\d):(?P<seconds>[0-5]\d)(?:\.\d+)?$"
)


def connection(values, old=None):
    """Normalize Lidarr connection form values into stored configuration."""
    hostname = str(values.get("hostname", values.get("url", ""))).strip().rstrip("/")
    if hostname and not hostname.startswith(("http://", "https://")):
        hostname = f"{'https' if values.get('useSsl') else 'http'}://{hostname}"
    port = str(values.get("port", "")).strip()
    if port and hostname.rsplit(":", 1)[-1] != port:
        hostname = f"{hostname}:{port}"
    return {
        "url": hostname,
        "apiKey": str(values.get("apiKey", "")).strip() or (old or {}).get("apiKey", ""),
    }


def headers(config=None):
    """Build authenticated headers for the configured Lidarr instance."""
    config = config or get_service("lidarr")
    if not config or not config.get("apiKey"):
        raise ValueError("Lidarr is not configured.")
    return {"X-Api-Key": config["apiKey"]}


def url(path, config=None):
    """Build a Lidarr v1 API URL."""
    config = config or get_service("lidarr")
    if not config or not config.get("url"):
        raise ValueError("Lidarr is not configured.")
    return f"{config['url'].rstrip('/')}/api/v1{path}"


def _request(method, path, *, config=None, timeout=15, **kwargs):
    config = config or get_service("lidarr")
    return requests.request(
        method,
        url(path, config),
        headers=headers(config),
        timeout=timeout,
        **kwargs,
    )


def system_status(config=None):
    return _request("GET", "/system/status", config=config, timeout=12)


def options(config=None):
    """Load the selectable root folders, profiles, and tags from Lidarr."""
    config = config or get_service("lidarr")
    request_headers = headers(config)

    def get(path):
        return cached_json_get(
            url(path, config),
            headers=request_headers,
            namespace="lidarr-options",
            ttl=LIDARR_OPTIONS_CACHE_TTL,
        )

    return {
        "rootFolders": get("/rootfolder"),
        "qualityProfiles": get("/qualityprofile"),
        "metadataProfiles": get("/metadataprofile"),
        "tags": get("/tag"),
    }


def lookup_artist(mbid, config=None):
    return _request("GET", "/artist/lookup", config=config, params={"term": f"mbid:{mbid}"})


def add_artist(artist, config=None):
    return _request("POST", "/artist", config=config, json=artist, timeout=20)


def update_artists(values, config=None):
    return _request("PUT", "/artist/editor", config=config, json=values)


def lookup_album(mbid, config=None):
    return _request("GET", "/album/lookup", config=config, params={"term": f"mbid:{mbid}"})


def add_album(album, config=None):
    return _request("POST", "/album", config=config, json=album, timeout=20)


def albums_by_release_group(mbid, config=None):
    return _request("GET", "/album", config=config, params={"foreignAlbumId": mbid})


def library_artists(config=None):
    """Return every artist already tracked by Lidarr."""
    response = _request("GET", "/artist", config=config, timeout=20)
    response.raise_for_status()
    data = response.json()
    if isinstance(data, list):
        return data
    return data.get("records", []) if isinstance(data, dict) else []


def library_albums(config=None):
    """Return every album already tracked by Lidarr."""
    response = _request("GET", "/album", config=config, timeout=20)
    response.raise_for_status()
    data = response.json()
    if isinstance(data, list):
        return data
    return data.get("records", []) if isinstance(data, dict) else []


def queue_records(config=None):
    """Read all Lidarr queue pages, including the embedded album when present."""
    records = []
    page, page_size = 1, 1000
    expected_total = None
    while True:
        response = _request(
            "GET", "/queue", config=config, timeout=20,
            params={"includeAlbum": "true", "page": page, "pageSize": page_size},
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
            raise ValueError("Lidarr queue response did not contain records.")
        batch = payload["records"]
        records.extend(batch)
        total = payload.get("totalRecords")
        if isinstance(total, int):
            if total < 0 or len(records) > total:
                raise ValueError("Lidarr queue totalRecords was inconsistent.")
            expected_total = total
        if expected_total is not None and len(records) >= expected_total:
            break
        if not batch:
            if expected_total is not None:
                raise ValueError("Lidarr queue pagination ended before totalRecords.")
            break
        if expected_total is None and len(batch) < page_size:
            break
        page += 1
        if page > 1000:
            raise ValueError("Lidarr queue pagination did not terminate.")
    return records


def _safe_queue_text(value):
    """Keep queue strings client-safe and bounded; queue identifiers stay private."""
    return value.strip()[:120] if isinstance(value, str) else ""


def _safe_progress(value):
    """Normalize only finite numeric queue progress into a client-safe integer."""
    if isinstance(value, bool) or not isinstance(value, Real):
        return None
    try:
        numeric = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    if not math.isfinite(numeric):
        return None
    return max(0, min(100, int(round(numeric))))


def _format_time_left(value):
    """Format Lidarr's TimeSpan value as total hours, minutes, and seconds."""
    text = _safe_queue_text(value)
    match = TIME_LEFT_PATTERN.fullmatch(text)
    if not match:
        return ""
    total_hours = (
        int(match.group("days") or 0) * 24
        + int(match.group("hours"))
    )
    return (
        f"{total_hours:02d}:{int(match.group('minutes')):02d}:"
        f"{int(match.group('seconds')):02d}"
    )


def _container_timezone():
    """Resolve the configured container timezone, with a system-local fallback."""
    timezone_name = os.environ.get("TZ", "").strip().lstrip(":")
    if timezone_name:
        try:
            return ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError:
            pass
    return datetime.now().astimezone().tzinfo or timezone.utc


def _format_completion_time(value):
    """Render Lidarr's completion timestamp in the container's local timezone."""
    text = _safe_queue_text(value)
    if not text:
        return ""
    normalized = f"{text[:-1]}+00:00" if text[-1:].casefold() == "z" else text
    try:
        completion = datetime.fromisoformat(normalized)
    except ValueError:
        return ""
    if completion.tzinfo is None:
        completion = completion.replace(tzinfo=timezone.utc)
    return completion.astimezone(_container_timezone()).strftime(
        "%m/%d/%Y %I:%M %p"
    )


def _representative_texts(record):
    """Normalize every emitted text field for stable representative selection."""
    return (
        _safe_queue_text(record.get("status")).casefold(),
        _safe_queue_text(record.get("trackedDownloadStatus")).casefold(),
        _safe_queue_text(record.get("trackedDownloadState")).casefold(),
        _safe_queue_text(record.get("timeleft")).casefold(),
        _safe_queue_text(record.get("estimatedCompletionTime")).casefold(),
    )


def _queue_release_group_id(record, album_ids):
    album = record.get("album")
    if isinstance(album, dict) and album.get("foreignAlbumId"):
        return str(album["foreignAlbumId"]).casefold()
    try:
        album_id = int(record.get("albumId"))
    except (TypeError, ValueError):
        return None
    candidates = album_ids.get(album_id, ())
    return candidates[0] if len(candidates) == 1 else None


def normalize_download_snapshot(records, library_index=None):
    """Aggregate queue records by release group without retaining queue identities.

    Duplicate records use byte-weighted progress.  The representative state is the
    item with the largest remaining fraction, then a stable safe-field tie-breaker;
    that makes status and ETA deterministic while favoring the least-complete item.
    """
    album_ids = {}
    for mbid, album in ((library_index or {}).get("albums") or {}).items():
        try:
            album_id = int((album or {}).get("id"))
        except (TypeError, ValueError):
            continue
        album_ids.setdefault(album_id, []).append(str(mbid).casefold())
    grouped = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        mbid = _queue_release_group_id(record, album_ids)
        if not mbid:
            continue
        try:
            size = int(record.get("size") or 0)
            size_left = int(record.get("sizeleft") or 0)
        except (TypeError, ValueError):
            size, size_left = 0, 0
        if size > 0:
            size_left = max(0, min(size, size_left))
        else:
            size_left = 0
        grouped.setdefault(mbid, []).append((record, size, size_left))
    albums = {}
    for mbid, items in grouped.items():
        total = sum(size for _, size, _ in items if size > 0)
        remaining = sum(left for _, size, left in items if size > 0)
        progress = int(round((total - remaining) * 100 / total)) if total else 0
        progress = max(0, min(100, progress))
        representative = max(
            items,
            key=lambda item: (
                (item[2] / item[1]) if item[1] else 1.0,
                *_representative_texts(item[0]),
            ),
        )[0]
        status, tracked_status, tracked_state, time_left, completion_time = (
            _representative_texts(representative)
        )
        albums[mbid] = {
            "progress": progress,
            "status": status,
            "trackedDownloadStatus": tracked_status,
            "trackedDownloadState": tracked_state,
            "timeLeft": time_left,
            "estimatedCompletionTime": completion_time,
        }
    return {"albums": albums}


def refresh_download_snapshot(config=None):
    """Replace the shared live snapshot only after a complete successful poll."""
    snapshot = normalize_download_snapshot(queue_records(config), cached_library_index())
    # A successful empty queue deliberately clears stale entries.  Exceptions before
    # this write leave the last-known-good document in place until its short expiry.
    set_cache_document(
        DOWNLOAD_SNAPSHOT_NAMESPACE, DOWNLOAD_SNAPSHOT_KEY,
        snapshot, LIDARR_DOWNLOAD_CACHE_TTL,
    )
    return snapshot


def cached_download_availability():
    """Read the worker-written snapshot; never issue a web-request queue call."""
    snapshot = get_cache_document(DOWNLOAD_SNAPSHOT_NAMESPACE, DOWNLOAD_SNAPSHOT_KEY)
    if not isinstance(snapshot, dict):
        return {}
    albums = snapshot.get("albums")
    return albums if isinstance(albums, dict) else {}


def public_download_status(value):
    """Return the fixed client-safe subset even if a cache row is malformed."""
    if not isinstance(value, dict):
        return None
    sanitized = {}
    progress = _safe_progress(value.get("progress"))
    if progress is not None:
        sanitized["progress"] = progress
    for field in PUBLIC_DOWNLOAD_FIELDS[1:4]:
        text = _safe_queue_text(value.get(field))
        if text:
            sanitized[field] = text.casefold() if field == "status" else text
    time_left = _format_time_left(value.get("timeLeft"))
    if time_left:
        sanitized["timeLeft"] = time_left
    completion_time = _format_completion_time(value.get("estimatedCompletionTime"))
    if completion_time:
        sanitized["estimatedCompletionTime"] = completion_time
    return sanitized


def album_availability(album):
    """Normalize Lidarr's track statistics into release-group availability."""
    statistics = album.get("statistics") or {}
    total = int(statistics.get("totalTrackCount") or album.get("trackCount") or 0)
    downloaded = int(statistics.get("trackFileCount") or 0)
    return {
        "id": album.get("id"),
        "title": album.get("title") or "",
        "totalTrackCount": total,
        "trackFileCount": downloaded,
        "fullyAvailable": bool(total and downloaded >= total),
        "monitored": bool(album.get("monitored")),
        "artistMbid": str(album.get("foreignArtistId") or (album.get("artist") or {}).get("foreignArtistId") or ""),
        "artistName": str(album.get("artistName") or (album.get("artist") or {}).get("artistName") or (album.get("artist") or {}).get("name") or ""),
    }


def scan_library_availability(config=None):
    """Refresh cached artist tracking and release-group completion from Lidarr."""
    artists = {}
    for artist in library_artists(config):
        artist_id = artist.get("foreignArtistId")
        if artist_id:
            artists[artist_id] = {
                "id": artist.get("id"),
                "name": artist.get("artistName") or artist.get("name") or "",
                "monitored": bool(artist.get("monitored")),
            }
    albums = {}
    for album in library_albums(config):
        release_group_id = album.get("foreignAlbumId")
        if release_group_id:
            normalized = album_availability(album)
            artist = artists.get(normalized["artistMbid"])
            if artist and not normalized["artistName"]:
                normalized["artistName"] = artist["name"]
            albums[str(release_group_id).casefold()] = normalized
    payload = {"artists": artists, "albums": albums}
    set_cache_document("lidarr-library", "albums", payload, LIDARR_LIBRARY_CACHE_TTL)
    invalidate_document(LIBRARY_INDEX_KEY)
    invalidate_detail_payloads()
    # This receives only a complete successful scan; absent records remain unknown.
    try:
        from ..notifications import observe_availability
    except ImportError:
        from notifications import observe_availability
    observe_availability(albums)
    return payload


def cached_library_index():
    """Return the cached Lidarr library document, parsed at most once."""
    return memoized_document(
        LIBRARY_INDEX_KEY,
        lambda: get_cache_document("lidarr-library", "albums", allow_expired=True) or {},
    )


def cached_library_availability():
    """Read Lidarr status without making an HTTP request on an artist page."""
    return cached_library_index().get("albums", {})


def cached_artist_availability():
    """Read tracked Lidarr artists without making an HTTP request."""
    return cached_library_index().get("artists", {})


def tracked_artist(mbid, config=None):
    """Return a locally tracked artist without invoking Lidarr metadata lookup."""
    return next((
        artist
        for artist in library_artists(config)
        if artist.get("foreignArtistId") == mbid
    ), None)


def albums_by_artist(artist_id, config=None):
    """Return Lidarr's locally stored release groups for one tracked artist."""
    response = _request(
        "GET",
        "/album",
        config=config,
        params={"artistId": artist_id},
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()
    if isinstance(data, list):
        return data
    return data.get("records", []) if isinstance(data, dict) else []


def start_command(values, config=None):
    return _request("POST", "/command", config=config, json=values, timeout=20)


def command(command_id, config=None):
    return _request("GET", f"/command/{command_id}", config=config, timeout=12)


def _artist_image(images):
    """Select the best portrait-like image across Lidarr response formats."""
    for cover_type in ("poster", "headshot", "fanart"):
        image = next((
            item
            for item in images or []
            if str(item.get("coverType") or item.get("CoverType") or "").lower()
            == cover_type
        ), None)
        if image:
            return (
                image.get("remoteUrl")
                or image.get("RemoteUrl")
                or image.get("url")
                or image.get("Url")
            )
    return None


def _metadata_artist(mbid):
    """Load public Lidarr metadata without requiring a user's Lidarr server."""
    return cached_json_get(
        f"{LIDARR_METADATA_URL.rstrip('/')}/artist/{quote(mbid)}",
        headers={"User-Agent": USER_AGENT},
        namespace="lidarr-artist-metadata",
        ttl=LIDARR_METADATA_CACHE_TTL,
        request_timeout=15,
    )


def artist_image_url(mbid, config=None):
    """Return artist art even when a local Lidarr server is not configured."""
    try:
        response = lookup_artist(mbid, config)
        response.raise_for_status()
        artists = response.json()
        artist = next((item for item in artists if item.get("foreignArtistId") == mbid), None)
        image_url = _artist_image((artist or {}).get("images"))
        if image_url:
            return image_url
    except (ValueError, requests.RequestException):
        pass

    try:
        metadata = _metadata_artist(mbid)
        return _artist_image((metadata or {}).get("images"))
    except requests.RequestException:
        return None
