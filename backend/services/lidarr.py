"""Lidarr configuration and HTTP client operations."""

from urllib.parse import quote

import requests

if __package__ == "backend.services":
    from ..api_cache import cached_json_get, get_cache_document, set_cache_document
    from ..cache_memo import invalidate_document, memoized_document
    from ..config import (
        LIDARR_LIBRARY_CACHE_TTL,
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
        LIDARR_METADATA_CACHE_TTL,
        LIDARR_METADATA_URL,
        LIDARR_OPTIONS_CACHE_TTL,
        USER_AGENT,
    )
    from detail_cache import invalidate_all as invalidate_detail_payloads
    from storage import get_service


LIBRARY_INDEX_KEY = "lidarr-library-index"


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
            albums[release_group_id] = album_availability(album)
    payload = {"artists": artists, "albums": albums}
    set_cache_document("lidarr-library", "albums", payload, LIDARR_LIBRARY_CACHE_TTL)
    invalidate_document(LIBRARY_INDEX_KEY)
    invalidate_detail_payloads()
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
