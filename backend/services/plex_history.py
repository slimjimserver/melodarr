"""Plex music-listening history client and response normalization."""

from collections.abc import Iterator
from urllib.parse import urlsplit

import requests

if __package__ == "backend.services":
    from . import plex
else:  # Support the existing `python backend/app.py` entry point.
    from services import plex


HISTORY_ENDPOINT = "/status/sessions/history/all"
DEFAULT_PAGE_SIZE = 200
TRACK_TYPES = frozenset({"track", "10"})


def _headers(config, *, start=None, size=None):
    headers = {
        "Accept": "application/json",
        "X-Plex-Token": config["token"],
    }
    if start is not None:
        headers["X-Plex-Container-Start"] = str(start)
    if size is not None:
        headers["X-Plex-Container-Size"] = str(size)
    return headers


def _as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _container(response):
    try:
        payload = response.json()
    except (TypeError, ValueError) as exc:
        raise ValueError("Plex returned an invalid JSON response") from exc
    if not isinstance(payload, dict):
        raise ValueError("Plex returned an invalid JSON document")
    container = payload.get("MediaContainer", payload)
    if not isinstance(container, dict):
        raise ValueError("Plex returned an invalid MediaContainer")
    return container


def _integer(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _timestamp(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _metadata(container):
    """Return history records across the JSON collection names Plex has used."""
    items = []
    seen = set()
    for collection in ("Metadata", "Track", "Audio", "Video", "_children"):
        values = _as_list(container.get(collection))
        for item in values:
            if not isinstance(item, dict) or id(item) in seen:
                continue
            seen.add(id(item))
            items.append(item)
    return items


def selected_music_section_ids(config):
    """Return configured section IDs after confirming they are music libraries."""
    return [
        str(section["id"])
        for section in plex.selected_music_sections(config)
        if section.get("id") is not None
    ]


def accounts(config):
    """Return server-local Plex account IDs and their username-like aliases."""
    response = requests.get(
        f"{config['url']}/accounts",
        headers=_headers(config),
        timeout=12,
    )
    response.raise_for_status()
    container = _container(response)
    records = []
    for item in (
        _as_list(container.get("Account"))
        + _as_list(container.get("User"))
    ):
        if not isinstance(item, dict):
            continue
        account_id = item.get("id")
        if account_id is None:
            account_id = item.get("accountID", item.get("accountId"))
        if account_id is None:
            continue
        aliases = []
        for key in ("name", "username", "title"):
            value = str(item.get(key) or "").strip()
            if value and value.casefold() not in {
                alias.casefold() for alias in aliases
            }:
                aliases.append(value)
        records.append({
            "account_id": str(account_id),
            "aliases": tuple(aliases),
        })
    return records


def _rating_key(value):
    """Extract a rating key from either a scalar or `/library/metadata/...`."""
    text = str(value or "").strip()
    if not text:
        return ""
    path = urlsplit(text).path.rstrip("/")
    parts = [part for part in path.split("/") if part]
    try:
        metadata_index = parts.index("metadata")
    except ValueError:
        return parts[-1] if parts else ""
    return parts[metadata_index + 1] if len(parts) > metadata_index + 1 else ""


def _media_type(item):
    return str(
        item.get("type")
        or item.get("metadataType")
        or item.get("_elementType")
        or ""
    ).casefold()


def _history_event(item):
    if _media_type(item) not in TRACK_TYPES:
        return None

    history_key = str(
        item.get("historyKey")
        or item.get("history_key")
        or item.get("historyID")
        or ""
    ).strip()
    # Unlike regular library metadata, Plex history commonly supplies only the
    # key paths. Their metadata IDs are the same server-local rating keys used
    # by the cached library snapshot.
    artist_rating_key = _rating_key(
        item.get("grandparentRatingKey") or item.get("grandparentKey")
    )
    played_at = _timestamp(item.get("viewedAt"))
    if not history_key or not artist_rating_key or played_at is None:
        return None

    account_id = item.get("accountID", item.get("accountId"))
    if account_id is None:
        children = (
            _as_list(item.get("User"))
            + _as_list(item.get("children"))
            + _as_list(item.get("_children"))
        )
        for child in children:
            if not isinstance(child, dict):
                continue
            child_type = str(
                child.get("_elementType")
                or child.get("type")
                or "user"
            ).casefold()
            if child_type == "user":
                account_id = child.get("id", child.get("accountID"))
                if account_id is not None:
                    break
    if account_id is None:
        return None

    album_rating_key = _rating_key(
        item.get("parentRatingKey") or item.get("parentKey")
    )
    return {
        "history_key": history_key,
        "account_id": str(account_id),
        "artist_rating_key": artist_rating_key,
        "album_rating_key": album_rating_key or None,
        "played_at": played_at,
    }


def _server_history(
    config,
    *,
    since,
    until,
    section_ids,
    page_size,
    artist_rating_keys,
    album_rating_keys,
    diagnostics,
) -> Iterator[dict]:
    selected_sections = {str(value) for value in section_ids}
    selected_artists = {str(value) for value in artist_rating_keys}
    selected_albums = {str(value) for value in album_rating_keys}
    start = 0
    previous_page_identity = None
    while True:
        # Plex conventionally accepts these two X-Plex-Container values as
        # query arguments. Send them as HTTP headers as well for compatibility
        # with server versions and proxies that implement the documented form.
        pagination = {
            "X-Plex-Container-Start": start,
            "X-Plex-Container-Size": page_size,
        }
        params = {
            # Server-side filters on this legacy endpoint vary between Plex
            # versions. Page the canonical global history feed and enforce
            # section, media type, and time boundaries locally.
            "sort": "viewedAt:desc",
            **pagination,
        }
        response = requests.get(
            f"{config['url']}{HISTORY_ENDPOINT}",
            params=params,
            headers=_headers(config, start=start, size=page_size),
            timeout=30,
        )
        response.raise_for_status()
        container = _container(response)
        items = _metadata(container)
        diagnostics["pages"] += 1
        diagnostics["scanned"] += len(items)
        page_identity = tuple(
            str(item.get("historyKey") or item.get("historyID") or "")
            for item in items
        )
        if start and items and page_identity == previous_page_identity:
            raise ValueError("Plex history pagination did not advance")
        previous_page_identity = page_identity

        for item in items:
            if _media_type(item) in TRACK_TYPES:
                diagnostics["tracks"] += 1
            event = _history_event(item)
            if event is None:
                continue
            diagnostics["normalized"] += 1
            # Older history payload descriptions invert the parent and
            # grandparent labels for music. Resolve that ambiguity using the
            # selected library snapshot's typed rating-key indexes.
            if (
                event["artist_rating_key"] not in selected_artists
                and event["album_rating_key"] in selected_artists
                and (
                    not selected_albums
                    or event["artist_rating_key"] in selected_albums
                )
            ):
                event["artist_rating_key"], event["album_rating_key"] = (
                    event["album_rating_key"],
                    event["artist_rating_key"],
                )
            section_id = str(item.get("librarySectionID") or "")
            if section_id:
                section_matches = section_id in selected_sections
            else:
                # Some history variants omit the section. The current cached
                # Plex inventory is an equally strong selected-library check.
                section_matches = (
                    event["artist_rating_key"] in selected_artists
                )
            if not section_matches:
                continue
            diagnostics["selected"] += 1
            if float(since) <= event["played_at"] <= float(until):
                yield event

        returned = len(items)
        total = _integer(container.get("totalSize"))
        response_offset = _integer(container.get("offset"), start)
        response_size = _integer(container.get("size"), returned)
        next_start = response_offset + returned
        timestamps = [
            timestamp
            for item in items
            if (timestamp := _timestamp(item.get("viewedAt"))) is not None
        ]
        crossed_retention_boundary = bool(
            timestamps and min(timestamps) < float(since)
        )
        if (
            returned == 0
            or crossed_retention_boundary
            or (total is not None and next_start >= total)
            or (total is None and returned < page_size)
            or (response_size is not None and response_size == 0)
        ):
            break
        if next_start <= start:
            raise ValueError("Plex history pagination returned an invalid offset")
        start = next_start


def iter_history(
    config,
    *,
    since,
    until,
    section_ids=None,
    page_size=DEFAULT_PAGE_SIZE,
    diagnostics=None,
) -> Iterator[dict]:
    """Yield normalized track plays from selected music sections.

    The yielded dictionaries intentionally omit track identity and descriptive
    metadata. Artist/album names, tags, and MusicBrainz IDs remain owned by the
    enriched Plex library snapshot and are resolved by rating key later.
    """
    if page_size <= 0:
        raise ValueError("Plex history page size must be positive")
    diagnostics = diagnostics if diagnostics is not None else {}
    for key in ("pages", "scanned", "tracks", "normalized", "selected"):
        diagnostics[key] = 0
    section_ids = (
        selected_music_section_ids(config)
        if section_ids is None
        else [str(value) for value in section_ids]
    )
    diagnostics["sections"] = len(set(section_ids))
    if not section_ids:
        return
    library_index = plex.cached_library_index(config)
    artist_rating_keys = library_index.get("artistsByRatingKey", {})
    album_rating_keys = library_index.get("releaseGroupsByRatingKey", {})
    diagnostics["cachedArtists"] = len(artist_rating_keys)
    diagnostics["cachedAlbums"] = len(album_rating_keys)
    seen_history_keys = set()
    for event in _server_history(
        config,
        since=since,
        until=until,
        section_ids=dict.fromkeys(section_ids),
        page_size=page_size,
        artist_rating_keys=artist_rating_keys,
        album_rating_keys=album_rating_keys,
        diagnostics=diagnostics,
    ):
        if event["history_key"] in seen_history_keys:
            continue
        seen_history_keys.add(event["history_key"])
        yield event
