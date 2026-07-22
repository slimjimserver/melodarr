"""Plex HTTP client and response normalization."""

import time
import xml.etree.ElementTree as ET
from threading import RLock
from urllib.parse import quote, urlencode
from uuid import UUID

import requests

if __package__ == "backend.services":
    from ..api_cache import (
        get_cache_document,
        replace_cache_documents,
        set_cache_document,
        upsert_cache_documents,
    )
    from ..config import PLEX_LIBRARY_CACHE_TTL
else:  # Support the existing `python backend/app.py` entry point.
    from api_cache import (
        get_cache_document,
        replace_cache_documents,
        set_cache_document,
        upsert_cache_documents,
    )
    from config import PLEX_LIBRARY_CACHE_TTL


scan_lock = RLock()
SNAPSHOT_VERSION = 2


def _headers(config, accept_json=False):
    headers = {"X-Plex-Token": config["token"]}
    if accept_json:
        headers["Accept"] = "application/json"
    return headers


def machine_identifier(config):
    """Validate a Plex connection and return its server identifier."""
    response = requests.get(
        f"{config['url']}/identity",
        headers=_headers(config),
        timeout=12,
    )
    response.raise_for_status()
    try:
        identity = ET.fromstring(response.content)
    except ET.ParseError as exc:
        raise ValueError("Plex returned an invalid identity response") from exc
    return identity.attrib.get("machineIdentifier", "")


def music_sections(config):
    """Return the selectable music-library sections on a Plex server."""
    base = config["url"]
    headers = _headers(config, accept_json=True)
    sections_response = requests.get(
        f"{base}/library/sections",
        headers=headers,
        timeout=12,
    )
    sections_response.raise_for_status()
    directories = sections_response.json().get("MediaContainer", {}).get("Directory", [])
    return [
        {"id": str(section["key"]), "title": section.get("title") or f"Library {section['key']}"}
        for section in directories
        if section.get("type") == "artist" and section.get("key") is not None
    ]


def selected_music_sections(config, sections=None):
    """Apply the saved section filter, retaining all sections for legacy configs."""
    sections = music_sections(config) if sections is None else sections
    if "librarySectionIds" not in config:
        return sections
    selected = {str(section_id) for section_id in config.get("librarySectionIds", [])}
    return [section for section in sections if section["id"] in selected]


def _plex_url(config, key):
    key = str(key or "")
    if key.endswith("/children"):
        key = key[:-len("/children")]
    machine_identifier_value = config.get("machineIdentifier", "")
    if machine_identifier_value and key:
        return (
            "https://app.plex.tv/desktop/#!/server/"
            f"{machine_identifier_value}/details?key={quote(key, safe='')}"
        )
    return config["url"]


def _plexamp_url(config, key, plex_guid):
    """Build a mobile universal link for a Plex music-library item."""
    key = str(key or "")
    if key.endswith("/children"):
        key = key[:-len("/children")]
    scheme, separator, value = str(plex_guid or "").partition("://")
    media_type, path_separator, item_id = value.partition("/")
    source = str(config.get("machineIdentifier", ""))
    if (
        not separator
        or scheme.casefold() != "plex"
        or not path_separator
        or media_type not in {"artist", "album"}
        or not item_id
        or not source
        or not key
    ):
        return ""
    query = urlencode({"source": source, "key": key})
    return f"https://listen.plex.tv/{media_type}/{quote(item_id, safe='')}?{query}"


def _normalize_snapshot_urls(config, payload):
    """Repair navigational URLs in both new and previously cached snapshots."""
    for collection_name in ("artists", "releaseGroups"):
        for item in payload.get(collection_name, []):
            if item.get("key"):
                item["url"] = _plex_url(config, item["key"])
                item["plexampUrl"] = _plexamp_url(
                    config, item["key"], item.get("plexGuid")
                )
            if collection_name == "artists" and item.get("ratingKey") and item.get("thumb"):
                item["artwork"] = f"/api/artwork/plex-artist/{item['ratingKey']}"
    return payload


def _guids(item):
    values = []
    if item.get("guid"):
        values.append(str(item["guid"]))
    for guid in item.get("Guid", []):
        value = guid.get("id") if isinstance(guid, dict) else guid
        if value:
            values.append(str(value))
    return list(dict.fromkeys(values))


def _musicbrainz_id(guids):
    for guid in guids:
        scheme, separator, value = guid.partition("://")
        if not separator or scheme.casefold() not in {"mbid", "musicbrainz"}:
            continue
        candidate = value.split("?", 1)[0].split("/", 1)[0]
        try:
            return str(UUID(candidate))
        except ValueError:
            continue
    return ""


def _normalize_artist(config, section, item):
    guids = _guids(item)
    rating_key = str(item.get("ratingKey", ""))
    return {
        "name": item.get("title"),
        "thumb": item.get("thumb"),
        "section": section.get("title"),
        "key": item.get("key", ""),
        "ratingKey": rating_key,
        "artwork": f"/api/artwork/plex-artist/{rating_key}" if rating_key and item.get("thumb") else "",
        "plexGuid": str(item.get("guid", "")),
        "guids": guids,
        "musicbrainzId": _musicbrainz_id(guids),
        "url": _plex_url(config, item.get("key", "")),
        "plexampUrl": _plexamp_url(config, item.get("key", ""), item.get("guid", "")),
    }


def _normalize_release_group(config, section, item):
    guids = _guids(item)
    return {
        "name": item.get("title"),
        "artistName": item.get("parentTitle") or item.get("grandparentTitle"),
        "year": item.get("year"),
        "releaseType": item.get("subtype") or "album",
        "thumb": item.get("thumb"),
        "section": section.get("title"),
        "key": item.get("key", ""),
        "ratingKey": str(item.get("ratingKey", "")),
        "plexGuid": str(item.get("guid", "")),
        "guids": guids,
        # Plex album matches use MusicBrainz release IDs, while Melodarr's
        # album entities use release-group IDs. Keep the entity type explicit.
        "musicbrainzReleaseId": _musicbrainz_id(guids),
        "url": _plex_url(config, item.get("key", "")),
        "plexampUrl": _plexamp_url(config, item.get("key", ""), item.get("guid", "")),
    }


def _scan_sections(config, sections, *, recently_added=False):
    base = config["url"]
    headers = _headers(config, accept_json=True)
    result = {"artists": [], "releaseGroups": []}
    for section in sections:
        endpoint = "recentlyAdded" if recently_added else "all"
        for media_type, collection, normalizer in (
            (8, "artists", _normalize_artist),
            (9, "releaseGroups", _normalize_release_group),
        ):
            response = requests.get(
                f"{base}/library/sections/{section['id']}/{endpoint}",
                params={"type": media_type, "includeGuids": 1},
                headers=headers,
                timeout=20,
            )
            response.raise_for_status()
            metadata = response.json().get("MediaContainer", {}).get("Metadata", [])
            result[collection].extend(
                normalizer(config, section, item) for item in metadata
            )
    for collection in result.values():
        collection.sort(key=lambda item: (item.get("name") or "").casefold())
    return result


def _snapshot_id(config):
    return config.get("machineIdentifier") or config["url"]


def _item_identity(item):
    return (
        item.get("ratingKey")
        or item.get("key")
        or item.get("plexGuid")
        or "|".join((
            item.get("section") or "",
            item.get("artistName") or "",
            item.get("name") or "",
        )).casefold()
    )


def _guid_documents(config, inventory):
    server_id = _snapshot_id(config)
    documents = {}
    for media_type, collection in (
        ("artist", inventory["artists"]),
        ("release-group", inventory["releaseGroups"]),
    ):
        for item in collection:
            if not item.get("plexGuid") and not item.get("guids"):
                continue
            identity = _item_identity(item)
            documents[f"{server_id}:{media_type}:{identity}"] = {
                "type": media_type,
                "name": item.get("name"),
                "artistName": item.get("artistName"),
                "plexGuid": item.get("plexGuid"),
                "guids": item.get("guids", []),
                "musicbrainzId": (
                    item.get("musicbrainzId")
                    or item.get("musicbrainzReleaseId", "")
                ),
                "musicbrainzEntity": (
                    "release" if media_type == "release-group" else "artist"
                ),
                "musicbrainzReleaseGroupId": item.get(
                    "musicbrainzReleaseGroupId", ""
                ),
                "releaseGroupResolved": item.get("releaseGroupResolved", False),
            }
    return documents


def _save_snapshot(config, payload, *, replace_guids=False):
    set_cache_document(
        "plex-library", _snapshot_id(config), payload, PLEX_LIBRARY_CACHE_TTL
    )
    documents = _guid_documents(config, payload)
    if replace_guids:
        replace_cache_documents("plex-guid", documents, PLEX_LIBRARY_CACHE_TTL)
    else:
        upsert_cache_documents("plex-guid", documents, PLEX_LIBRARY_CACHE_TTL)


def full_library_scan(config):
    """Replace the cached artist, release-group, and GUID inventory."""
    with scan_lock:
        previous = get_cache_document(
            "plex-library", _snapshot_id(config), allow_expired=True
        ) or {}
        previous_mappings = {
            item.get("musicbrainzReleaseId"): {
                "musicbrainzReleaseGroupId": item.get(
                    "musicbrainzReleaseGroupId", ""
                ),
                "releaseGroupResolved": item.get("releaseGroupResolved", False),
            }
            for item in previous.get("releaseGroups", [])
            if item.get("musicbrainzReleaseId")
        }
        sections = selected_music_sections(config)
        inventory = _scan_sections(config, sections)
        for release_group in inventory["releaseGroups"]:
            mapping = previous_mappings.get(
                release_group.get("musicbrainzReleaseId")
            )
            if mapping:
                release_group.update(mapping)
        payload = {
            "snapshotVersion": SNAPSHOT_VERSION,
            **inventory,
            "sectionIds": [section["id"] for section in sections],
            "scannedAt": time.time(),
        }
        _save_snapshot(config, payload, replace_guids=True)
        return payload["artists"]


def recently_added_scan(config):
    """Merge recently added artists and release groups into the full snapshot."""
    with scan_lock:
        sections = selected_music_sections(config)
        section_ids = [section["id"] for section in sections]
        cached = get_cache_document(
            "plex-library", _snapshot_id(config), allow_expired=True
        )
        if (
            not cached
            or cached.get("snapshotVersion") != SNAPSHOT_VERSION
            or cached.get("sectionIds") != section_ids
        ):
            return full_library_scan(config)
        recent = _scan_sections(config, sections, recently_added=True)
        merged_inventory = {}
        for collection_name in ("artists", "releaseGroups"):
            merged = {
                _item_identity(item): item
                for item in cached.get(collection_name, [])
                if item.get("name")
            }
            for item in recent[collection_name]:
                if item.get("name"):
                    identity = _item_identity(item)
                    previous_item = merged.get(identity, {})
                    merged[identity] = {**previous_item, **item}
                    if previous_item.get("releaseGroupResolved"):
                        merged[identity].update({
                            "musicbrainzReleaseGroupId": previous_item.get(
                                "musicbrainzReleaseGroupId", ""
                            ),
                            "releaseGroupResolved": True,
                        })
            merged_inventory[collection_name] = sorted(
                merged.values(), key=lambda item: (item.get("name") or "").casefold()
            )
        payload = {
            "snapshotVersion": SNAPSHOT_VERSION,
            **merged_inventory,
            "sectionIds": section_ids,
            "scannedAt": time.time(),
        }
        _save_snapshot(config, payload)
        return payload["artists"]


def library_snapshot(config):
    """Return the complete cached inventory, scanning when absent or outdated."""
    cached = get_cache_document("plex-library", _snapshot_id(config))
    configured_ids = config.get("librarySectionIds")
    valid_sections = (
        configured_ids is None
        or set(cached.get("sectionIds", [])) == {str(value) for value in configured_ids}
    ) if cached else False
    if not cached or cached.get("snapshotVersion") != SNAPSHOT_VERSION or not valid_sections:
        full_library_scan(config)
        cached = get_cache_document("plex-library", _snapshot_id(config))
    return _normalize_snapshot_urls(
        config,
        cached or {"artists": [], "releaseGroups": []},
    )


def cached_library_snapshot(config, *, allow_expired=True):
    """Read availability metadata without triggering a synchronous Plex scan."""
    payload = get_cache_document(
        "plex-library", _snapshot_id(config), allow_expired=allow_expired
    ) or {"artists": [], "releaseGroups": []}
    return _normalize_snapshot_urls(config, payload)


def music_library(config):
    """Return cached artists from the selected Plex music libraries."""
    return library_snapshot(config).get("artists", [])


def library_release_groups(config):
    """Return cached albums, EPs, singles, and other album-level Plex items."""
    return library_snapshot(config).get("releaseGroups", [])


def unresolved_musicbrainz_releases(config):
    """Return Plex albums whose release MBID still needs a group lookup."""
    return [
        item
        for item in library_release_groups(config)
        if item.get("musicbrainzReleaseId")
        and not item.get("releaseGroupResolved")
    ]


def apply_release_group_mappings(config, mappings):
    """Persist resolved release-to-release-group relationships in the snapshot."""
    if not mappings:
        return 0
    with scan_lock:
        payload = get_cache_document(
            "plex-library", _snapshot_id(config), allow_expired=True
        )
        if not payload:
            return 0
        changed = []
        for item in payload.get("releaseGroups", []):
            release_id = item.get("musicbrainzReleaseId")
            if release_id not in mappings:
                continue
            item["musicbrainzReleaseGroupId"] = mappings[release_id] or ""
            item["releaseGroupResolved"] = True
            changed.append(item)
        if not changed:
            return 0
        set_cache_document(
            "plex-library", _snapshot_id(config), payload, PLEX_LIBRARY_CACHE_TTL
        )
        documents = _guid_documents(config, {
            "artists": [],
            "releaseGroups": changed,
        })
        upsert_cache_documents("plex-guid", documents, PLEX_LIBRARY_CACHE_TTL)
        return len(changed)
