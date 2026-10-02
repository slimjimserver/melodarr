"""Plex HTTP client and response normalization."""

import time
import xml.etree.ElementTree as ET
from threading import RLock
from urllib.parse import quote, urlencode
from uuid import UUID

import requests

if __package__ == "backend.services":
    from .. import track_search_index
    from ..api_cache import (
        get_cache_document,
        replace_cache_documents,
        set_cache_document,
        upsert_cache_documents,
    )
    from ..cache_memo import invalidate_document, memoized_document
    from ..config import PLEX_LIBRARY_CACHE_TTL
    from ..detail_cache import invalidate_all as invalidate_detail_payloads
    from ..http_security import request_without_redirects
    from ..media_urls import plex_artist_artwork
else:  # Support the existing `python backend/app.py` entry point.
    import track_search_index
    from api_cache import (
        get_cache_document,
        replace_cache_documents,
        set_cache_document,
        upsert_cache_documents,
    )
    from cache_memo import invalidate_document, memoized_document
    from config import PLEX_LIBRARY_CACHE_TTL
    from detail_cache import invalidate_all as invalidate_detail_payloads
    from http_security import request_without_redirects
    from media_urls import plex_artist_artwork


scan_lock = RLock()
SNAPSHOT_VERSION = 7
TRACK_MAPPING_FIELDS = (
    "musicbrainzRecordingId", "musicbrainzReleaseGroupId", "isrcs",
    "mappingSource", "mappingConfidence",
)
METADATA_TAG_FIELDS = {
    "genres": "Genre",
    "styles": "Style",
    "moods": "Mood",
}


def _index_key(snapshot_id):
    return f"plex-library-index:{snapshot_id}"


def _headers(config, accept_json=False):
    headers = {"X-Plex-Token": config["token"]}
    if accept_json:
        headers["Accept"] = "application/json"
    return headers


def machine_identifier(config):
    """Validate a Plex connection and return its server identifier."""
    response = request_without_redirects(
        requests.get,
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
    sections_response = request_without_redirects(
        requests.get,
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
        media_type = "artist" if collection_name == "artists" else "album"
        for item in payload.get(collection_name, []):
            if item.get("key"):
                plex_guid = _plex_metadata_guid(
                    [item.get("plexGuid"), *(item.get("guids") or [])], media_type
                )
                item["plexGuid"] = plex_guid
                item["url"] = _plex_url(config, item["key"])
                item["plexampUrl"] = _plexamp_url(
                    config, item["key"], plex_guid
                )
            if collection_name == "artists" and item.get("ratingKey") and item.get("thumb"):
                item["artwork"] = plex_artist_artwork(
                    item["ratingKey"], item["thumb"]
                )
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


def _plex_metadata_guid(guids, media_type):
    prefix = f"plex://{media_type}/"
    return next((
        str(guid) for guid in guids
        if str(guid).casefold().startswith(prefix)
    ), "")


def _metadata_tags(item, child_name):
    """Normalize Plex metadata tags while preserving their display spelling."""
    children = item.get(child_name, [])
    if isinstance(children, (dict, str)):
        children = [children]
    elif not isinstance(children, (list, tuple)):
        return []

    tags = []
    seen = set()
    for child in children:
        value = child.get("tag") if isinstance(child, dict) else child
        if not isinstance(value, str):
            continue
        value = " ".join(value.split())
        identity = value.casefold()
        if not value or identity in seen:
            continue
        seen.add(identity)
        tags.append(value)
    return tags


def _metadata_tag_fields(item):
    return {
        field_name: _metadata_tags(item, child_name)
        for field_name, child_name in METADATA_TAG_FIELDS.items()
    }


def _normalize_artist(config, section, item):
    guids = _guids(item)
    rating_key = str(item.get("ratingKey", ""))
    plex_guid = _plex_metadata_guid(guids, "artist")
    return {
        "name": item.get("title"),
        "sortName": item.get("titleSort") or "",
        "thumb": item.get("thumb"),
        "section": section.get("title"),
        "key": item.get("key", ""),
        "ratingKey": rating_key,
        "artwork": plex_artist_artwork(rating_key, item.get("thumb")),
        "plexGuid": plex_guid,
        "guids": guids,
        "musicbrainzId": _musicbrainz_id(guids),
        **_metadata_tag_fields(item),
        "url": _plex_url(config, item.get("key", "")),
        "plexampUrl": _plexamp_url(config, item.get("key", ""), plex_guid),
    }


def _normalize_release_group(config, section, item):
    guids = _guids(item)
    plex_guid = _plex_metadata_guid(guids, "album")
    artist_rating_key = str(
        item.get("parentRatingKey") or item.get("grandparentRatingKey") or ""
    )
    artist_key = (
        item.get("parentKey")
        or item.get("grandparentKey")
        or (
            f"/library/metadata/{artist_rating_key}"
            if artist_rating_key
            else ""
        )
    )
    return {
        "name": item.get("title"),
        "artistName": item.get("parentTitle") or item.get("grandparentTitle"),
        "artistKey": artist_key,
        "artistRatingKey": artist_rating_key,
        "artistPlexGuid": _plex_metadata_guid(
            [
                item.get("parentGuid", ""),
                item.get("grandparentGuid", ""),
            ],
            "artist",
        ),
        "year": item.get("year"),
        "releaseType": item.get("subtype") or "album",
        "thumb": item.get("thumb"),
        "section": section.get("title"),
        "key": item.get("key", ""),
        "ratingKey": str(item.get("ratingKey", "")),
        "plexGuid": plex_guid,
        "guids": guids,
        # Plex album matches use MusicBrainz release IDs, while Melodarr's
        # album entities use release-group IDs. Keep the entity type explicit.
        "musicbrainzReleaseId": _musicbrainz_id(guids),
        **_metadata_tag_fields(item),
        "url": _plex_url(config, item.get("key", "")),
        "plexampUrl": _plexamp_url(config, item.get("key", ""), plex_guid),
    }


def _integer(value):
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _normalize_track(config, section, item):
    """A Plex Track GUID identifies a release track, never a recording."""
    guids = _guids(item)
    rating_key = str(item.get("ratingKey") or "")
    return {
        "ratingKey": rating_key,
        "key": item.get("key") or (f"/library/metadata/{rating_key}" if rating_key else ""),
        "plexGuid": _plex_metadata_guid(guids, "track"),
        "guids": guids,
        "title": item.get("title") or "",
        "trackArtist": item.get("originalTitle") or item.get("grandparentTitle") or "",
        "albumArtist": item.get("grandparentTitle") or "",
        "albumTitle": item.get("parentTitle") or "",
        "durationMs": _integer(item.get("duration")),
        "trackNumber": _integer(item.get("index")),
        "discNumber": _integer(item.get("parentIndex")),
        "year": _integer(item.get("parentYear") or item.get("year")),
        "librarySectionId": str(section["id"]),
        "librarySectionTitle": section.get("title") or "",
        "artistRatingKey": str(item.get("grandparentRatingKey") or ""),
        "albumRatingKey": str(item.get("parentRatingKey") or ""),
        "musicbrainzTrackId": _musicbrainz_id(guids),
        "musicbrainzRecordingId": "",
        "musicbrainzReleaseId": "",
        "musicbrainzReleaseGroupId": "",
        "isrcs": [],
        "mappingSource": "",
        "mappingConfidence": "",
    }


def _metadata_pages(config, path, headers, **params):
    """Bound response sizes while retaining all items in large libraries."""
    start = 0
    while True:
        response = request_without_redirects(
            requests.get, f"{config['url']}{path}",
            params={**params, "includeGuids": 1,
                    "X-Plex-Container-Start": start, "X-Plex-Container-Size": 500},
            headers=headers, timeout=20,
        )
        response.raise_for_status()
        container = response.json().get("MediaContainer", {})
        items = container.get("Metadata", [])
        yield from items
        start += len(items)
        total = _integer(container.get("totalSize"))
        if not items or total is None or start >= total:
            break


def _parent_artist(config, section, release_group, headers):
    """Load or minimally reconstruct the artist owning a recent Plex album."""
    key = release_group.get("artistKey") or ""
    if key:
        try:
            response = request_without_redirects(
                requests.get,
                f"{config['url']}{key.removesuffix('/children')}",
                params={"includeGuids": 1},
                headers=headers,
                timeout=20,
            )
            response.raise_for_status()
            metadata = response.json().get("MediaContainer", {}).get("Metadata", [])
            if metadata:
                return _normalize_artist(config, section, metadata[0])
        except requests.RequestException:
            # The album is already useful inventory even if Plex is still
            # materializing its parent metadata. Keep a minimal artist entry
            # and let the next scan replace it with the complete record.
            pass

    rating_key = release_group.get("artistRatingKey") or ""
    plex_guid = release_group.get("artistPlexGuid") or ""
    return {
        "name": release_group.get("artistName"),
        "sortName": "",
        "thumb": None,
        "section": section.get("title"),
        "key": key,
        "ratingKey": rating_key,
        "artwork": "",
        "plexGuid": plex_guid,
        "guids": [plex_guid] if plex_guid else [],
        "musicbrainzId": "",
        "genres": [],
        "styles": [],
        "moods": [],
        "url": _plex_url(config, key),
        "plexampUrl": _plexamp_url(config, key, plex_guid),
    }


def _scan_sections(config, sections, *, recently_added=False, known_album_ids=()):
    headers = _headers(config, accept_json=True)
    result = {"artists": [], "releaseGroups": [], "tracks": []}
    for section in sections:
        endpoint = "recentlyAdded" if recently_added else "all"
        section_releases = []
        for media_type, plex_type, collection, normalizer in (
            (8, "artist", "artists", _normalize_artist),
            (9, "album", "releaseGroups", _normalize_release_group),
            (10, "track", "tracks", _normalize_track),
        ):
            metadata = _metadata_pages(
                config, f"/library/sections/{section['id']}/{endpoint}",
                headers, type=media_type,
            )
            normalized = [
                normalizer(config, section, item)
                for item in metadata
                # Plex's recentlyAdded endpoint can ignore the requested type
                # and return albums for an artist query. Never merge an
                # explicitly different entity type into the artist snapshot.
                if not item.get("type")
                or str(item["type"]).casefold() == plex_type
            ]
            result[collection].extend(normalized)
            if collection == "releaseGroups":
                section_releases = normalized
        if recently_added:
            known = set(known_album_ids) | {item.get("ratingKey") for item in section_releases}
            parent_ids = {
                item.get("albumRatingKey") for item in result["tracks"]
                if item.get("librarySectionId") == section["id"] and item.get("albumRatingKey")
            }
            for rating_key in sorted(parent_ids - known):
                try:
                    items = list(_metadata_pages(config, f"/library/metadata/{rating_key}", headers))
                    for item in items:
                        if not item.get("type") or item["type"] == "album":
                            album = _normalize_release_group(config, section, item)
                            section_releases.append(album)
                            result["releaseGroups"].append(album)
                except requests.RequestException:
                    # Keep the tracks when their parent is not yet available.
                    pass
            # recentlyAdded may ignore type=10 or list only some songs. Read
            # each recent album's children to cover the complete new album.
            for release_group in section_releases:
                rating_key = release_group.get("ratingKey")
                if not rating_key:
                    continue
                for item in _metadata_pages(
                    config, f"/library/metadata/{rating_key}/children", headers,
                ):
                    if not item.get("type") or item["type"] == "track":
                        result["tracks"].append(_normalize_track(config, section, item))
            known_artist_ids = {
                artist.get("ratingKey")
                for artist in result["artists"]
                if artist.get("ratingKey")
            }
            parent_releases = {}
            for release_group in section_releases:
                rating_key = release_group.get("artistRatingKey")
                identity = rating_key or "|".join((
                    release_group.get("section") or "",
                    release_group.get("artistName") or "",
                )).casefold()
                if (
                    identity
                    and rating_key not in known_artist_ids
                ):
                    parent_releases.setdefault(identity, release_group)
            for release_group in parent_releases.values():
                artist = _parent_artist(config, section, release_group, headers)
                if artist.get("name"):
                    result["artists"].append(artist)
                    if artist.get("ratingKey"):
                        known_artist_ids.add(artist["ratingKey"])
    result["tracks"] = list({
        _item_identity(item): item for item in result["tracks"]
    }.values())
    for collection in result.values():
        collection.sort(key=_sort_key)
    return result


def _sort_key(item):
    return ((item.get("name") or item.get("title") or "").casefold(),
            item.get("ratingKey") or "")


def _attach_track_releases(inventory, previous_tracks=()):
    albums = {item.get("ratingKey"): item for item in inventory.get("releaseGroups", [])}
    previous = {_item_identity(item): item for item in previous_tracks}
    for track in inventory.get("tracks", []):
        album = albums.get(track.get("albumRatingKey"), {})
        track["musicbrainzReleaseId"] = album.get("musicbrainzReleaseId") or ""
        old = previous.get(_item_identity(track), {})
        if (old.get("musicbrainzTrackId") == track.get("musicbrainzTrackId")
                and old.get("musicbrainzReleaseId") == track["musicbrainzReleaseId"]):
            for field in TRACK_MAPPING_FIELDS:
                if field in old:
                    track[field] = old[field]


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


def _save_snapshot(
    config,
    payload,
    *,
    replace_guids=False,
    guid_inventory=None,
    track_inventory=None,
    invalidate_details=True,
):
    payload["serverId"] = _snapshot_id(config)
    set_cache_document(
        "plex-library", _snapshot_id(config), payload, PLEX_LIBRARY_CACHE_TTL
    )
    track_search_index.index_plex_library(
        payload, server_id=_snapshot_id(config), track_inventory=track_inventory,
    )
    invalidate_document(_index_key(_snapshot_id(config)))
    if invalidate_details:
        invalidate_detail_payloads()
    documents = _guid_documents(config, guid_inventory or payload)
    if replace_guids:
        replace_cache_documents("plex-guid", documents, PLEX_LIBRARY_CACHE_TTL)
    else:
        upsert_cache_documents("plex-guid", documents, PLEX_LIBRARY_CACHE_TTL)


def _scan_result(inventory, *, artist_items=(), release_items=(), changed):
    """Return the inventory plus the exact MusicBrainz work caused by a scan."""
    return {
        "artists": inventory.get("artists", []),
        "tracks": inventory.get("tracks", []),
        "artistMbids": sorted({
            item["musicbrainzId"]
            for item in artist_items
            if item.get("musicbrainzId")
        }),
        "releaseMbids": sorted({
            item["musicbrainzReleaseId"]
            for item in release_items
            if item.get("musicbrainzReleaseId")
            and not item.get("releaseGroupResolved")
        } | {
            item["musicbrainzReleaseId"]
            for item in inventory.get("tracks", [])
            if item.get("musicbrainzReleaseId") and not item.get("musicbrainzRecordingId")
        }),
        "trackMbids": sorted({
            item["musicbrainzTrackId"] for item in inventory.get("tracks", [])
            if item.get("musicbrainzTrackId") and not item.get("musicbrainzRecordingId")
        }),
        "changed": changed,
    }


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
                "trackMetadataResolved": item.get("trackMetadataResolved", False),
                "releaseMetadataMissing": item.get("releaseMetadataMissing", False),
            }
            for item in previous.get("releaseGroups", [])
            if item.get("musicbrainzReleaseId")
        }
        sections = selected_music_sections(config)
        inventory = _scan_sections(config, sections)
        inventory.setdefault("tracks", [])
        for release_group in inventory["releaseGroups"]:
            mapping = previous_mappings.get(
                release_group.get("musicbrainzReleaseId")
            )
            if mapping:
                release_group.update(mapping)
        _attach_track_releases(inventory, previous.get("tracks", []))
        payload = {
            "snapshotVersion": SNAPSHOT_VERSION,
            **inventory,
            "sectionIds": [section["id"] for section in sections],
            "scannedAt": time.time(),
        }
        previous_artists = {
            _item_identity(item): item for item in previous.get("artists", [])
        }
        changed_artists = [
            item for item in inventory["artists"]
            if previous_artists.get(_item_identity(item)) != item
        ]
        changed = bool(
            previous.get("snapshotVersion") != SNAPSHOT_VERSION
            or previous.get("sectionIds") != payload["sectionIds"]
            or previous.get("artists") != inventory["artists"]
            or previous.get("releaseGroups") != inventory["releaseGroups"]
            or previous.get("tracks", []) != inventory["tracks"]
        )
        _save_snapshot(config, payload, replace_guids=True)
        return _scan_result(
            inventory,
            artist_items=changed_artists,
            release_items=inventory["releaseGroups"],
            changed=changed,
        )


def recently_added_scan(config):
    """Merge recent inventory and upsert only changed tracks in SQLite."""
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
        recent = _scan_sections(
            config, sections, recently_added=True,
            known_album_ids={item.get("ratingKey") for item in cached.get("releaseGroups", [])},
        )
        recent.setdefault("tracks", [])
        merged_inventory = {}
        changed_inventory = {"artists": [], "releaseGroups": []}
        recent_inventory = {"artists": [], "releaseGroups": [], "tracks": []}
        for collection_name in ("artists", "releaseGroups", "tracks"):
            if collection_name == "tracks":
                _attach_track_releases(
                    {"releaseGroups": merged_inventory["releaseGroups"], "tracks": recent["tracks"]},
                    cached.get("tracks", []),
                )
            merged = {
                _item_identity(item): item
                for item in cached.get(collection_name, [])
                if collection_name == "tracks" or item.get("name")
            }
            for item in recent[collection_name]:
                if collection_name == "tracks" or item.get("name"):
                    identity = _item_identity(item)
                    previous_item = merged.get(identity, {})
                    updated_item = {**previous_item, **item}
                    # Recently-added responses can omit metadata children that
                    # were present in the full scan. Only a full scan should
                    # clear previously known tags.
                    for field_name in METADATA_TAG_FIELDS:
                        if not item.get(field_name) and previous_item.get(field_name):
                            updated_item[field_name] = previous_item[field_name]
                    if (previous_item.get("releaseGroupResolved")
                            and previous_item.get("musicbrainzReleaseId") == item.get("musicbrainzReleaseId")):
                        updated_item.update({
                            "musicbrainzReleaseGroupId": previous_item.get(
                                "musicbrainzReleaseGroupId", ""
                            ),
                            "releaseGroupResolved": True,
                            "trackMetadataResolved": previous_item.get("trackMetadataResolved", False),
                            "releaseMetadataMissing": previous_item.get("releaseMetadataMissing", False),
                        })
                    recent_inventory[collection_name].append(updated_item)
                    if updated_item != previous_item:
                        changed_inventory.setdefault(collection_name, []).append(updated_item)
                        merged[identity] = updated_item
            merged_inventory[collection_name] = sorted(
                merged.values(), key=_sort_key
            )
        changed = any(changed_inventory.values())
        if not changed:
            return _scan_result(
                recent_inventory,
                release_items=recent_inventory["releaseGroups"],
                changed=False,
            )
        payload = {
            "snapshotVersion": SNAPSHOT_VERSION,
            **merged_inventory,
            "sectionIds": section_ids,
            "scannedAt": time.time(),
        }
        _save_snapshot(
            config, payload, guid_inventory=changed_inventory,
            track_inventory=changed_inventory.get("tracks", []),
            invalidate_details=bool(changed_inventory["artists"] or changed_inventory["releaseGroups"]),
        )
        return _scan_result(
            recent_inventory,
            artist_items=changed_inventory["artists"],
            release_items=recent_inventory["releaseGroups"],
            changed=True,
        )


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


def _build_library_index(config):
    snapshot = cached_library_snapshot(config)
    release_groups = {}
    for item in snapshot.get("releaseGroups", []):
        release_group_id = item.get("musicbrainzReleaseGroupId")
        if release_group_id:
            release_groups.setdefault(release_group_id, []).append(item)
    return {
        "snapshot": snapshot,
        "artistsByMbid": {
            artist["musicbrainzId"]: artist
            for artist in snapshot.get("artists", [])
            if artist.get("musicbrainzId")
        },
        "artistsByRatingKey": {
            artist["ratingKey"]: artist
            for artist in snapshot.get("artists", [])
            if artist.get("ratingKey")
        },
        "releaseGroupsByRatingKey": {
            release_group["ratingKey"]: release_group
            for release_group in snapshot.get("releaseGroups", [])
            if release_group.get("ratingKey")
        },
        "releaseGroupsByMbid": release_groups,
    }


def cached_library_index(config):
    """Return lookup tables over the Plex snapshot, parsed at most once.

    Detail pages previously deserialized the whole snapshot several times per
    request and then scanned it linearly. The request-local memo is backed by a
    short process TTL, so nearby HTTP requests and background work share it.
    """
    return memoized_document(
        _index_key(_snapshot_id(config)),
        lambda: _build_library_index(config),
    )


def music_library(config):
    """Return cached artists from the selected Plex music libraries."""
    return library_snapshot(config).get("artists", [])


def library_release_groups(config):
    """Return cached albums, EPs, singles, and other album-level Plex items."""
    return library_snapshot(config).get("releaseGroups", [])


def unresolved_musicbrainz_releases(config):
    """Return Plex albums whose release MBID still needs a group lookup."""
    snapshot = cached_library_snapshot(config)
    track_releases = {
        item.get("musicbrainzReleaseId") for item in snapshot.get("tracks", [])
        if not item.get("musicbrainzRecordingId")
    }
    return [
        item
        for item in snapshot.get("releaseGroups", [])
        if item.get("musicbrainzReleaseId")
        and (not item.get("releaseGroupResolved")
             or item.get("musicbrainzReleaseId") in track_releases)
    ]


def apply_release_group_mappings(config, mappings, *, artist_mappings=None, release_metadata=None):
    """Persist release-group mappings and inferred parent-artist MBIDs."""
    if not mappings:
        return 0
    artist_mappings = artist_mappings or {}
    with scan_lock:
        payload = get_cache_document(
            "plex-library", _snapshot_id(config), allow_expired=True
        )
        if not payload:
            return 0
        changed_releases = []
        artists_by_rating_key = {
            artist.get("ratingKey"): artist
            for artist in payload.get("artists", [])
            if artist.get("ratingKey")
        }
        artists_by_name = {}
        for artist in payload.get("artists", []):
            name = (artist.get("name") or "").casefold()
            if name:
                artists_by_name.setdefault(name, []).append(artist)
        changed_artists = []
        for item in payload.get("releaseGroups", []):
            release_id = item.get("musicbrainzReleaseId")
            if release_id not in mappings:
                continue
            update = {
                "musicbrainzReleaseGroupId": mappings[release_id] or "",
                "releaseGroupResolved": True,
            }
            if release_metadata is not None and release_id in release_metadata:
                metadata = release_metadata[release_id] or {}
                usable = any(medium.get("tracks") for medium in metadata.get("media", []))
                update.update(trackMetadataResolved=usable, releaseMetadataMissing=not usable)
            if any(item.get(field) != value for field, value in update.items()):
                item.update(update)
                changed_releases.append(item)

            artist_id = artist_mappings.get(release_id)
            if not artist_id:
                continue
            artist = artists_by_rating_key.get(item.get("artistRatingKey"))
            if artist is None:
                candidates = artists_by_name.get(
                    (item.get("artistName") or "").casefold(), []
                )
                artist = candidates[0] if len(candidates) == 1 else None
            if artist is not None and not artist.get("musicbrainzId"):
                artist["musicbrainzId"] = artist_id
                changed_artists.append(artist)
        track_mappings = {}
        for release_id, metadata in (release_metadata or {}).items():
            if not metadata:
                continue
            group_id = (metadata.get("release-group") or {}).get("id", "")
            for medium in metadata.get("media", []):
                for mb_track in medium.get("tracks", []):
                    recording = mb_track.get("recording") or {}
                    recording_id = _musicbrainz_id([f"mbid://{recording.get('id', '')}"])
                    if mb_track.get("id") and recording_id:
                        track_mappings[(release_id, mb_track["id"].casefold())] = {
                            "musicbrainzRecordingId": recording_id,
                            "musicbrainzReleaseGroupId": group_id,
                            "isrcs": track_search_index.normalize_isrcs(recording.get("isrcs")),
                            "mappingSource": "release_track", "mappingConfidence": "exact",
                        }
        changed_tracks = []
        for track in payload.get("tracks", []):
            mapping = track_mappings.get((track.get("musicbrainzReleaseId"), track.get("musicbrainzTrackId")))
            if mapping and any(track.get(field) != value for field, value in mapping.items()):
                track.update(mapping)
                changed_tracks.append(track)
        if not changed_releases and not changed_artists and not changed_tracks:
            return 0
        _save_snapshot(config, payload, guid_inventory={
            "artists": changed_artists,
            "releaseGroups": changed_releases,
        }, track_inventory=changed_tracks, invalidate_details=bool(changed_artists or changed_releases))
        return len(changed_releases)


def fallback_track_ids(config, track_ids=None):
    """Only fall back when a parent release is absent or confirmed unusable."""
    snapshot = cached_library_snapshot(config)
    return _fallback_track_ids(snapshot, track_ids)


def _fallback_track_ids(snapshot, track_ids):
    albums = {item.get("ratingKey"): item for item in snapshot.get("releaseGroups", [])}
    return sorted({
        track["musicbrainzTrackId"] for track in snapshot.get("tracks", [])
        if track.get("musicbrainzTrackId") and not track.get("musicbrainzRecordingId")
        and (track_ids is None or track["musicbrainzTrackId"] in track_ids)
        and (not track.get("musicbrainzReleaseId")
             or albums.get(track.get("albumRatingKey"), {}).get("releaseMetadataMissing"))
    })


def apply_track_recording_mappings(config, mappings):
    if not mappings:
        return 0
    with scan_lock:
        payload = get_cache_document("plex-library", _snapshot_id(config), allow_expired=True)
        if not payload:
            return 0
        eligible = set(_fallback_track_ids(payload, set(mappings)))
        changed = []
        for track in payload.get("tracks", []):
            track_id = track.get("musicbrainzTrackId")
            if track_id not in eligible:
                continue
            recording = mappings[track_id]
            recording_id = _musicbrainz_id([f"mbid://{recording.get('id', '')}"])
            if not recording_id:
                continue
            track.update(
                musicbrainzRecordingId=recording_id,
                isrcs=track_search_index.normalize_isrcs(recording.get("isrcs")),
                mappingSource="track_search", mappingConfidence="exact",
            )
            changed.append(track)
        if changed:
            _save_snapshot(
                config, payload, guid_inventory={"artists": [], "releaseGroups": []},
                track_inventory=changed, invalidate_details=False,
            )
        return len(changed)


def recording_availability(config, recording_id):
    """Exact local recording lookup shared by availability and request APIs."""
    if not config:
        return {"available": False, "recordingMbid": recording_id, "tracks": []}
    tracks = track_search_index.plex_recording_tracks(
        _snapshot_id(config), recording_id, section_ids=config.get("librarySectionIds"),
    )
    return {"available": bool(tracks), "recordingMbid": recording_id, "tracks": tracks}


def recording_availabilities(config, recording_mbids, *, include_tracks=False):
    """Batch exact indexed availability with the same server/section scope."""
    copies = {}
    if config:
        lookup = (
            track_search_index.plex_recording_tracks_batch if include_tracks
            else track_search_index.plex_recording_copy_counts
        )
        copies = lookup(
            _snapshot_id(config), recording_mbids,
            section_ids=config.get("librarySectionIds"),
        )
    result = {}
    for identity in recording_mbids:
        tracks = copies.get(identity, []) if include_tracks else None
        count = len(tracks) if include_tracks else copies.get(identity, 0)
        result[identity] = {
            "recordingMbid": identity, "available": bool(count), "plexCopyCount": count,
            **({"tracks": tracks} if include_tracks else {}),
        }
    return result
