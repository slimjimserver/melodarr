"""MusicBrainz artist, release-group, and release detail routes."""

from contextlib import nullcontext
from urllib.parse import quote
from uuid import UUID

import requests
from flask import Blueprint, jsonify, request

if __package__ == "backend.routes":
    from ..media_urls import artist_large_cover_art, release_group_cover_art
    from ..responses import api_error
    from ..security import login_required
    from ..services import lidarr, musicbrainz, plex
    from ..storage import get_service
else:
    from media_urls import artist_large_cover_art, release_group_cover_art
    from responses import api_error
    from security import login_required
    from services import lidarr, musicbrainz, plex
    from storage import get_service


blueprint = Blueprint("music", __name__)


def _musicbrainz_priority():
    return "prefetch" if request.args.get("prefetch") == "1" else "interactive"


def _plex_index():
    """Return the memoized Plex lookup tables, or empty ones when unavailable."""
    config = get_service("plex")
    if not config:
        return {"artistsByMbid": {}, "releaseGroupsByMbid": {}}
    try:
        return plex.cached_library_index(config)
    except (ValueError, requests.RequestException):
        return {"artistsByMbid": {}, "releaseGroupsByMbid": {}}


def _plex_release_group_inventory():
    return _plex_index()["releaseGroupsByMbid"]


def _plex_artist(mbid):
    return _plex_index()["artistsByMbid"].get(mbid)


def _plex_release_summary(item):
    return {
        "name": item.get("name"),
        "releaseType": item.get("releaseType"),
        "releaseId": item.get("musicbrainzReleaseId"),
        "url": item.get("url"),
        "plexampUrl": item.get("plexampUrl"),
    }


def _artist_detail_payload(
    mbid,
    priority,
    force_refresh=False,
    cache_only=False,
):
    operation = musicbrainz.critical_operation() if priority == "critical" else None
    with operation or nullcontext():
        data = musicbrainz.get(
            f"/artist/{quote(mbid)}", "aliases+url-rels+genres", priority=priority,
            force_refresh=force_refresh,
            cache_only=cache_only,
        )
        if data is None:
            return None
        raw_groups, offset = [], 0
        while True:
            page = musicbrainz.get(
                "/release-group", "", priority=priority,
                force_refresh=force_refresh,
                cache_only=cache_only,
                artist=mbid, limit=100, offset=offset,
            )
            if page is None:
                return None
            batch = page.get("release-groups", [])
            raw_groups.extend(batch)
            total = page.get("release-group-count", len(raw_groups))
            if offset + len(batch) >= total or not batch:
                break
            offset += len(batch)
    plex_groups = _plex_release_group_inventory()
    lidarr_groups = lidarr.cached_library_availability()
    groups = [
        {
            "id": group["id"], "title": group.get("title", "Untitled"),
            "date": group.get("first-release-date", ""),
            "type": group.get("primary-type") or "Other",
            "secondaryTypes": [name for name in group.get("secondary-types") or [] if name],
            "disambiguation": group.get("disambiguation", ""),
            "coverArt": release_group_cover_art(group["id"]),
            "availableInPlex": group["id"] in plex_groups,
            "availableInLidarr": group["id"] in lidarr_groups,
            "fullyAvailableInLidarr": bool(
                lidarr_groups.get(group["id"], {}).get("fullyAvailable")
            ),
            "plexReleases": [
                _plex_release_summary(item) for item in plex_groups.get(group["id"], [])
            ],
        }
        for group in raw_groups
    ]
    groups.sort(key=lambda group: group["date"] or "9999")
    sections = {}
    for group in groups:
        sections.setdefault(" + ".join([group["type"], *group["secondaryTypes"]]), []).append(group)
    spotify = next((
        relation.get("url", {}).get("resource")
        for relation in data.get("relations", [])
        if "spotify.com" in relation.get("url", {}).get("resource", "")
    ), "")
    plex_artist = _plex_artist(mbid)
    lidarr_artist = lidarr.cached_artist_availability().get(mbid)
    return {
        "id": data["id"], "name": data.get("name"),
        "romanizedName": musicbrainz.romanized_artist_name(data),
        "country": data.get("country", ""),
        "disambiguation": data.get("disambiguation", ""), "type": data.get("type", ""),
        "gender": data.get("gender", ""), "area": (data.get("area") or {}).get("name", ""),
        "lifeSpan": data.get("life-span", {}),
        "genres": [genre.get("name") for genre in data.get("genres", [])],
        "spotify": spotify, "coverArtLarge": artist_large_cover_art(data["id"]),
        "availableInPlex": bool(plex_artist),
        "availableInLidarr": bool(lidarr_artist),
        "plexUrl": plex_artist.get("url", "") if plex_artist else "",
        "plexampUrl": plex_artist.get("plexampUrl", "") if plex_artist else "",
        "sections": sections, "total": len(groups), "nextOffset": None,
        "provisional": False, "metadataSource": "MusicBrainz",
    }


def _lidarr_artist_detail_payload(mbid):
    """Build a fast provisional discography from Lidarr's local database."""
    config = get_service("lidarr")
    if not config:
        return None
    artist = lidarr.tracked_artist(mbid, config)
    if not artist:
        return None
    artist_id = artist.get("id")
    albums = lidarr.albums_by_artist(artist_id, config) if artist_id is not None else []
    plex_groups = _plex_release_group_inventory()
    groups = []
    for album in albums:
        group_id = album.get("foreignAlbumId")
        if not group_id:
            continue
        secondary_types = [
            str(value) for value in album.get("secondaryTypes") or [] if value
        ]
        groups.append({
            "id": group_id,
            "title": album.get("title") or "Untitled",
            "date": str(album.get("releaseDate") or "")[:10],
            "type": album.get("albumType") or "Other",
            "secondaryTypes": secondary_types,
            "disambiguation": album.get("disambiguation") or "",
            "coverArt": release_group_cover_art(group_id),
            "availableInPlex": group_id in plex_groups,
            "availableInLidarr": True,
            "fullyAvailableInLidarr": lidarr.album_availability(album)["fullyAvailable"],
            "plexReleases": [
                _plex_release_summary(item)
                for item in plex_groups.get(group_id, [])
            ],
        })
    groups.sort(key=lambda group: group["date"] or "9999")
    sections = {}
    for group in groups:
        section_name = " + ".join([group["type"], *group["secondaryTypes"]])
        sections.setdefault(section_name, []).append(group)
    plex_artist = _plex_artist(mbid)
    return {
        "id": mbid,
        "name": artist.get("artistName") or artist.get("name") or "Unknown artist",
        "romanizedName": musicbrainz.romanized_artist_name({
            "name": artist.get("artistName") or artist.get("name"),
            "sortName": artist.get("sortName"),
        }),
        "country": artist.get("country") or "",
        "disambiguation": artist.get("disambiguation") or "",
        "type": artist.get("artistType") or "",
        "gender": "",
        "area": "",
        "lifeSpan": {},
        "genres": artist.get("genres") or [],
        "spotify": "",
        "coverArtLarge": artist_large_cover_art(mbid),
        "availableInPlex": bool(plex_artist),
        "availableInLidarr": True,
        "plexUrl": plex_artist.get("url", "") if plex_artist else "",
        "plexampUrl": plex_artist.get("plexampUrl", "") if plex_artist else "",
        "sections": sections,
        "total": len(groups),
        "nextOffset": None,
        "provisional": True,
        "metadataSource": "Lidarr",
    }


@blueprint.get("/api/music/artist/<mbid>")
@login_required
def artist_detail(mbid):
    try:
        priority = "prefetch" if request.args.get("prefetch") == "1" else "critical"
        cached = _artist_detail_payload(mbid, priority, cache_only=True)
        if cached is not None:
            return jsonify(cached)
        if request.args.get("complete") != "1":
            try:
                provisional = _lidarr_artist_detail_payload(mbid)
            except (ValueError, requests.RequestException):
                provisional = None
            if provisional is not None:
                return jsonify(provisional)
        return jsonify(_artist_detail_payload(mbid, priority))
    except requests.RequestException:
        return api_error("MusicBrainz could not load this artist.", 502)


@blueprint.post("/api/music/artist/<mbid>/refresh")
@login_required
def refresh_artist_detail(mbid):
    try:
        mbid = str(UUID(mbid))
    except ValueError:
        return api_error("Invalid MusicBrainz artist ID.")
    try:
        return jsonify(_artist_detail_payload(mbid, "critical", force_refresh=True))
    except requests.RequestException:
        return api_error(
            "MusicBrainz could not refresh this artist. The previous cache was kept.",
            502,
        )


def _release_group_detail_payload(mbid, priority, *, cache_only=False):
    data = musicbrainz.get(
        f"/release-group/{quote(mbid)}",
        "artist-credits+url-rels",
        priority=priority,
        cache_only=cache_only,
    )
    if data is None:
        return None
    raw_releases, offset = [], 0
    while True:
        page = musicbrainz.get(
            "/release", "media", priority=priority, cache_only=cache_only,
            **{"release-group": mbid}, limit=100, offset=offset
        )
        if page is None:
            return None
        batch = page.get("releases", [])
        raw_releases.extend(batch)
        total = page.get("release-count", len(raw_releases))
        if offset + len(batch) >= total or not batch:
            break
        offset += len(batch)
    plex_releases = _plex_release_group_inventory().get(mbid, [])
    lidarr_album = lidarr.cached_library_availability().get(mbid)
    owned_release_ids = {
        item.get("musicbrainzReleaseId") for item in plex_releases
        if item.get("musicbrainzReleaseId")
    }
    releases = [
        {
            "id": release["id"], "title": release.get("title", data.get("title", "Untitled")),
            "date": release.get("date", ""), "country": release.get("country", ""),
            "status": release.get("status", ""), "disambiguation": release.get("disambiguation", ""),
            "format": ", ".join(media.get("format", "") for media in release.get("media", []) if media.get("format")),
            "trackCount": sum(media.get("track-count", 0) for media in release.get("media", [])),
            "availableInPlex": release["id"] in owned_release_ids,
        }
        for release in raw_releases
    ]
    releases.sort(key=lambda release: release["date"] or "9999")
    spotify = next((
        relation.get("url", {}).get("resource")
        for relation in data.get("relations", [])
        if "spotify.com" in relation.get("url", {}).get("resource", "")
    ), "")
    artist_credit = data.get("artist-credit", [])
    primary_artist = artist_credit[0].get("artist", {}) if artist_credit else {}
    return {
        "id": data["id"], "title": data.get("title"),
        "artist": " · ".join(credit.get("name", "") for credit in artist_credit),
        "artistId": primary_artist.get("id", ""), "date": data.get("first-release-date", ""),
        "type": data.get("primary-type", "Album"), "spotify": spotify,
        "coverArt": release_group_cover_art(data["id"]),
        "coverArtLarge": musicbrainz.cover_art_url(data["id"], size=500),
        "availableInPlex": bool(plex_releases),
        "availableInLidarr": bool(lidarr_album),
        "fullyAvailableInLidarr": bool(
            lidarr_album and lidarr_album.get("fullyAvailable")
        ),
        "plexReleases": [
            _plex_release_summary(item) for item in plex_releases
        ],
        "releases": releases, "total": len(releases), "nextOffset": None,
        "provisional": False, "metadataSource": "MusicBrainz",
    }


def _lidarr_release_group_detail_payload(mbid):
    """Build a fast provisional album page from Lidarr's local database."""
    config = get_service("lidarr")
    if not config:
        return None
    response = lidarr.albums_by_release_group(mbid, config)
    response.raise_for_status()
    albums = response.json()
    if isinstance(albums, dict):
        albums = albums.get("records", [])
    elif not isinstance(albums, list):
        albums = []
    album = next(
        (item for item in albums if item.get("foreignAlbumId") == mbid),
        None,
    )
    if not album:
        return None
    availability = lidarr.album_availability(album)
    artist = album.get("artist") or {}
    plex_releases = _plex_release_group_inventory().get(mbid, [])
    owned_release_ids = {
        item.get("musicbrainzReleaseId") for item in plex_releases
        if item.get("musicbrainzReleaseId")
    }
    releases = []
    for release in album.get("releases") or []:
        release_id = release.get("foreignReleaseId")
        if not release_id:
            continue
        releases.append({
            "id": release_id,
            "title": release.get("title") or album.get("title") or "Untitled",
            "date": str(release.get("releaseDate") or "")[:10],
            "country": release.get("country") or "",
            "status": release.get("status") or "",
            "disambiguation": release.get("disambiguation") or "",
            "format": release.get("format") or "",
            "trackCount": release.get("trackCount") or 0,
            "availableInPlex": release_id in owned_release_ids,
        })
    releases.sort(key=lambda release: release["date"] or "9999")
    return {
        "id": mbid,
        "title": album.get("title") or "Untitled",
        "artist": (
            artist.get("artistName")
            or album.get("artistTitle")
            or album.get("artistName")
            or ""
        ),
        "artistId": artist.get("foreignArtistId") or "",
        "date": str(album.get("releaseDate") or "")[:10],
        "type": album.get("albumType") or "Album",
        "spotify": "",
        "coverArt": release_group_cover_art(mbid),
        "coverArtLarge": musicbrainz.cover_art_url(mbid, size=500),
        "availableInPlex": bool(plex_releases),
        "availableInLidarr": True,
        "fullyAvailableInLidarr": availability["fullyAvailable"],
        "plexReleases": [
            _plex_release_summary(item) for item in plex_releases
        ],
        "releases": releases,
        "total": len(releases),
        "nextOffset": None,
        "provisional": True,
        "metadataSource": "Lidarr",
    }


@blueprint.get("/api/music/release-group/<mbid>")
@login_required
def release_group_detail(mbid):
    try:
        priority = _musicbrainz_priority()
        cached = _release_group_detail_payload(mbid, priority, cache_only=True)
        if cached is not None:
            return jsonify(cached)
        if request.args.get("complete") != "1":
            try:
                provisional = _lidarr_release_group_detail_payload(mbid)
            except (ValueError, requests.RequestException):
                provisional = None
            if provisional is not None:
                return jsonify(provisional)
        return jsonify(_release_group_detail_payload(mbid, priority))
    except requests.RequestException:
        return api_error("MusicBrainz could not load this album.", 502)


@blueprint.get("/api/music/release/<mbid>")
@login_required
def release_detail(mbid):
    try:
        data = musicbrainz.get(
            f"/release/{quote(mbid)}",
            "recordings+artist-credits",
            priority=_musicbrainz_priority(),
        )
        tracks = [
            {
                "number": track.get("number", ""), "title": track.get("title", "Untitled"),
                "length": track.get("length"),
                "artist": " · ".join(credit.get("name", "") for credit in track.get("artist-credit", [])),
            }
            for medium in data.get("media", []) for track in medium.get("tracks", [])
        ]
        return jsonify({
            "id": data["id"], "title": data.get("title"),
            "artist": " · ".join(credit.get("name", "") for credit in data.get("artist-credit", [])),
            "date": data.get("date", ""), "country": data.get("country", ""), "tracks": tracks,
        })
    except requests.RequestException:
        return api_error("MusicBrainz could not load this release.", 502)
