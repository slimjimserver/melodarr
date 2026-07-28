"""Plex music-listening history client and response normalization."""

from collections.abc import Iterator

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
    for collection in ("Metadata", "Track"):
        values = _as_list(container.get(collection))
        items.extend(item for item in values if isinstance(item, dict))
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


def _history_event(item):
    media_type = str(item.get("type") or item.get("metadataType") or "").casefold()
    if media_type not in TRACK_TYPES:
        return None

    history_key = str(
        item.get("historyKey")
        or item.get("history_key")
        or item.get("historyID")
        or ""
    ).strip()
    artist_rating_key = str(item.get("grandparentRatingKey") or "").strip()
    played_at = _timestamp(item.get("viewedAt"))
    if not history_key or not artist_rating_key or played_at is None:
        return None

    account_id = item.get("accountID", item.get("accountId"))
    if account_id is None:
        users = _as_list(item.get("User"))
        user = users[0] if users and isinstance(users[0], dict) else {}
        account_id = user.get("id", user.get("accountID"))
    if account_id is None:
        return None

    album_rating_key = str(item.get("parentRatingKey") or "").strip()
    return {
        "history_key": history_key,
        "account_id": str(account_id),
        "artist_rating_key": artist_rating_key,
        "album_rating_key": album_rating_key or None,
        "played_at": played_at,
    }


def _section_history(
    config,
    section_id,
    *,
    since,
    until,
    page_size,
) -> Iterator[dict]:
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
            "librarySectionID": str(section_id),
            "type": 10,
            "viewedAt>=": int(since),
            "viewedAt<=": int(until),
            "sort": "viewedAt:asc",
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
        page_identity = tuple(
            str(item.get("historyKey") or item.get("historyID") or "")
            for item in items
        )
        if start and items and page_identity == previous_page_identity:
            raise ValueError("Plex history pagination did not advance")
        previous_page_identity = page_identity

        for item in items:
            event = _history_event(item)
            if event is not None:
                yield event

        returned = len(items)
        total = _integer(container.get("totalSize"))
        response_offset = _integer(container.get("offset"), start)
        response_size = _integer(container.get("size"), returned)
        next_start = response_offset + returned
        if (
            returned == 0
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
) -> Iterator[dict]:
    """Yield normalized track plays from selected music sections.

    The yielded dictionaries intentionally omit track identity and descriptive
    metadata. Artist/album names, tags, and MusicBrainz IDs remain owned by the
    enriched Plex library snapshot and are resolved by rating key later.
    """
    if page_size <= 0:
        raise ValueError("Plex history page size must be positive")
    section_ids = (
        selected_music_section_ids(config)
        if section_ids is None
        else [str(value) for value in section_ids]
    )
    seen_history_keys = set()
    for section_id in dict.fromkeys(section_ids):
        for event in _section_history(
            config,
            section_id,
            since=since,
            until=until,
            page_size=page_size,
        ):
            if event["history_key"] in seen_history_keys:
                continue
            seen_history_keys.add(event["history_key"])
            yield event
