"""MusicBrainz artist, release-group, and release detail routes."""

import sqlite3
from contextlib import nullcontext
from urllib.parse import quote
from uuid import UUID

import requests
from flask import Blueprint, jsonify, request

if __package__ == "backend.routes":
    from .. import detail_cache, track_search_index
    from ..media_urls import (
        artist_cover_art,
        artist_large_cover_art,
        release_group_cover_art,
    )
    from ..responses import api_error
    from ..security import login_required
    from ..services import (
        anime_artist_links,
        anime_theme_links,
        lastfm,
        lidarr,
        musicbrainz,
        plex,
    )
    from ..storage import (
        get_lastfm_api_key,
        get_service,
        pending_lidarr_search_mbids,
    )
    from ..workers import artist_metadata as artist_metadata_worker
    from ..workers import similar_artists as similar_artist_worker
else:
    import detail_cache
    import track_search_index
    from media_urls import (
        artist_cover_art,
        artist_large_cover_art,
        release_group_cover_art,
    )
    from responses import api_error
    from security import login_required
    from services import (
        anime_artist_links,
        anime_theme_links,
        lastfm,
        lidarr,
        musicbrainz,
        plex,
    )
    from storage import (
        get_lastfm_api_key,
        get_service,
        pending_lidarr_search_mbids,
    )
    from workers import artist_metadata as artist_metadata_worker
    from workers import similar_artists as similar_artist_worker


blueprint = Blueprint("music", __name__)
RELEASE_TRACK_INCLUDES = "recordings+artist-credits+release-groups"
LEGACY_RELEASE_TRACK_INCLUDES = "recordings+artist-credits"


def _prefetch_cache_miss():
    """Return an intentionally uncacheable empty response for speculative misses."""
    return "", 204, {"Cache-Control": "no-store"}


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


def _availability_settled(*, available_in_lidarr, available_in_plex):
    """Stop polling after every configured library has observed the item."""
    expected = []
    if get_service("lidarr"):
        expected.append(bool(available_in_lidarr))
    if get_service("plex"):
        expected.append(bool(available_in_plex))
    return all(expected) if expected else True


def _availability_response(payload):
    response = jsonify(payload)
    response.headers["Cache-Control"] = "no-store"
    return response


def _release_group_lifecycle(mbid, lidarr_album=None, download=None, pending=False):
    """Apply imported > queue > durable follow-up > request lifecycle precedence."""
    if lidarr_album and lidarr_album.get("fullyAvailable"):
        return "available", None
    if download:
        return "downloading", lidarr.public_download_status(download)
    if pending:
        return "queued", None
    return "requested", None


def _download_snapshot():
    try:
        return lidarr.cached_download_availability()
    except Exception:
        # Live queue status is optional; cache trouble must not fail detail pages.
        return {}


SIMILAR_ARTIST_CANDIDATE_LIMIT = 50
SIMILAR_ARTIST_PAGE_LIMIT = 12


def _similar_artist_candidates(items, current_artist_id):
    """Normalize and deduplicate the shared Last.fm candidate set."""
    candidates = []
    seen_ids = {current_artist_id.casefold()}
    seen_names = set()
    for item in items:
        name = " ".join(str(item.get("name") or "").strip().split())
        normalized_name = name.casefold()
        if not name or normalized_name in seen_names:
            continue
        try:
            artist_id = str(UUID(str(item.get("mbid") or "")))
        except (TypeError, ValueError, AttributeError):
            artist_id = ""
        normalized_id = artist_id.casefold()
        if normalized_id and normalized_id in seen_ids:
            continue
        seen_names.add(normalized_name)
        if normalized_id:
            seen_ids.add(normalized_id)
        try:
            match = max(0.0, min(1.0, float(item.get("match") or 0)))
        except (TypeError, ValueError):
            match = 0.0
        candidates.append(
            {
                "id": artist_id,
                "name": name,
                "url": str(item.get("url") or "").strip()[:500],
                "match": round(match, 4),
            }
        )
    return candidates


def _similar_artist_payload(
    candidates,
    current_artist_id,
    offset,
    end,
    available_artist_ids,
):
    """Resolve one stable raw slice and deduplicate identities across pages."""
    artists = []
    pending = 0
    seen_ids = {current_artist_id.casefold()}
    for index, candidate in enumerate(candidates[:end]):
        try:
            resolution = (
                {"id": candidate["id"], "name": candidate["name"]}
                if candidate["id"]
                else similar_artist_worker.cached_resolution(candidate)
            )
        except sqlite3.Error:
            resolution = None
        if resolution is None:
            if index >= offset:
                pending += 1
            continue
        artist_id = str(resolution.get("id") or "")
        normalized_id = artist_id.casefold()
        if not artist_id or normalized_id in seen_ids:
            continue
        seen_ids.add(normalized_id)
        if index < offset:
            continue
        match = candidate["match"]
        artists.append(
            {
                "id": artist_id,
                "name": str(resolution.get("name") or candidate["name"]),
                "type": f"{round(match * 100)}% match" if match else "Similar artist",
                "rank": index,
                "match": match,
                "coverArt": artist_cover_art(artist_id),
                "availableInLidarr": normalized_id in available_artist_ids,
                "recommendationSource": "Similar on Last.fm",
            }
        )
    return artists, pending


@blueprint.get("/api/music/artist/<mbid>/similar")
@login_required
def artist_similar(mbid):
    """Return cached Last.fm neighbors without delaying artist detail."""
    try:
        artist_id = str(UUID(mbid))
    except ValueError:
        return api_error("Invalid MusicBrainz artist ID.")
    api_key = get_lastfm_api_key()
    if not api_key:
        return jsonify(
            {
                "artists": [],
                "configured": False,
                "offset": 0,
                "limit": SIMILAR_ARTIST_PAGE_LIMIT,
                "total": 0,
                "nextOffset": None,
                "hasMore": False,
                "pending": 0,
            }
        )
    offset = max(0, request.args.get("offset", 0, type=int) or 0)
    page_limit = max(
        1,
        min(
            SIMILAR_ARTIST_PAGE_LIMIT,
            request.args.get("limit", SIMILAR_ARTIST_PAGE_LIMIT, type=int)
            or SIMILAR_ARTIST_PAGE_LIMIT,
        ),
    )
    try:
        items = (
            lastfm.get_public(
                "artist.getsimilar",
                api_key,
                mbid=artist_id,
                limit=SIMILAR_ARTIST_CANDIDATE_LIMIT,
                autocorrect=1,
            )
            .get("similarartists", {})
            .get("artist", [])
        )
        candidates = _similar_artist_candidates(items, artist_id)
    except (ValueError, requests.RequestException, TypeError, AttributeError):
        return api_error("Last.fm could not load similar artists.", 502)
    unresolved = [item for item in candidates if not item["id"]]
    try:
        similar_artist_worker.request_resolutions(unresolved)
    except sqlite3.Error:
        pass
    try:
        available_artist_ids = {
            str(value).casefold() for value in lidarr.cached_artist_availability()
        }
    except (sqlite3.Error, ValueError):
        available_artist_ids = set()
    end = min(len(candidates), offset + page_limit)
    artists, pending = _similar_artist_payload(
        candidates,
        artist_id,
        offset,
        end,
        available_artist_ids,
    )
    has_more = end < len(candidates)
    next_offset = end if has_more and not pending else None
    response = jsonify(
        {
            "artists": artists,
            "configured": True,
            "offset": offset,
            "limit": page_limit,
            "total": len(candidates),
            "nextOffset": next_offset,
            "hasMore": has_more,
            "pending": pending,
        }
    )
    response.headers["Cache-Control"] = "no-store" if pending else "private, max-age=60"
    return response


@blueprint.get("/api/music/artist/<mbid>/availability")
@login_required
def artist_availability(mbid):
    """Return live artist ownership without rebuilding MusicBrainz detail."""
    plex_artist = _plex_artist(mbid)
    lidarr_artist = lidarr.cached_artist_availability().get(mbid)
    available_in_plex = bool(plex_artist)
    available_in_lidarr = bool(lidarr_artist)
    release_group_ids = list(
        dict.fromkeys(
            value.strip()
            for value in request.args.getlist("releaseGroup")
            if value.strip()
        )
    )[:50]
    plex_groups = (
        _plex_release_group_inventory() if release_group_ids else {}
    )
    lidarr_groups = (
        lidarr.cached_library_availability() if release_group_ids else {}
    )
    downloads = _download_snapshot() if release_group_ids else {}
    pending = pending_lidarr_search_mbids(release_group_ids)
    return _availability_response({
        "id": mbid,
        "availableInPlex": available_in_plex,
        "availableInLidarr": available_in_lidarr,
        "plexUrl": plex_artist.get("url", "") if plex_artist else "",
        "plexampUrl": plex_artist.get("plexampUrl", "") if plex_artist else "",
        "releaseGroups": {
            release_group_id: {
                "availableInPlex": release_group_id in plex_groups,
                "availableInLidarr": release_group_id.casefold() in lidarr_groups,
                "fullyAvailableInLidarr": bool(
                    lidarr_groups.get(release_group_id.casefold(), {}).get(
                        "fullyAvailable"
                    )
                ),
                "requestStatus": _release_group_lifecycle(
                    release_group_id, lidarr_groups.get(release_group_id.casefold()),
                    downloads.get(release_group_id.casefold()),
                    release_group_id.casefold() in pending,
                )[0],
                "downloadStatus": _release_group_lifecycle(
                    release_group_id, lidarr_groups.get(release_group_id.casefold()),
                    downloads.get(release_group_id.casefold()),
                    release_group_id.casefold() in pending,
                )[1],
            }
            for release_group_id in release_group_ids
        },
        "settled": _availability_settled(
            available_in_lidarr=available_in_lidarr,
            available_in_plex=available_in_plex,
        ),
    })


@blueprint.get("/api/music/artist/<mbid>/tracks")
@login_required
def artist_track_search(mbid):
    """Find cached release groups containing a matching track by this artist."""
    try:
        mbid = str(UUID(mbid))
    except ValueError:
        return api_error("Invalid MusicBrainz artist ID.")
    query = request.args.get("q", "").strip()
    if len(query) < 2:
        return api_error("Enter at least two characters.")
    matches = track_search_index.search_artist_tracks(mbid, query)
    if not matches:
        escaped_query = query.replace("\\", "\\\\").replace('"', '\\"')
        words = track_search_index.normalize_text(query).split()
        clauses = [f'recording:"{escaped_query}"']
        if words:
            clauses.append("(" + " AND ".join(
                f'recording:"{word}"' for word in words
            ) + ")")
        recording_query = (
            f"({' OR '.join(clauses)}) AND arid:{mbid}"
        )
        try:
            response = musicbrainz.search(
                recording_query,
                "track",
                priority="interactive",
                plain_search=False,
                limit=50,
            )
            track_search_index.index_recording_search(response)
            matches = track_search_index.search_artist_tracks(mbid, query)
        except requests.RequestException:
            # Release-title filtering remains useful when MusicBrainz is busy.
            pass
    group_ids = list(dict.fromkeys(
        match["release_group_mbid"] for match in matches
    ))
    groups = track_search_index.cached_release_groups(group_ids)
    matched_tracks = {}
    for match in matches:
        matched_tracks.setdefault(match["release_group_mbid"], []).append(
            match["normalized_title"]
        )

    plex_groups = _plex_release_group_inventory() if group_ids else {}
    lidarr_groups = lidarr.cached_library_availability() if group_ids else {}
    download_groups = _download_snapshot() if group_ids else {}
    pending_groups = pending_lidarr_search_mbids(group_ids)
    anime_names = anime_theme_links.anime_names_for_release_groups(group_ids)
    results = []
    for group_id in group_ids:
        group = groups.get(group_id)
        if not group:
            continue
        lidarr_group = lidarr_groups.get(group_id.casefold())
        request_status, download_status = _release_group_lifecycle(
            group_id,
            lidarr_group,
            download_groups.get(group_id.casefold()),
            group_id.casefold() in pending_groups,
        )
        results.append({
            "id": group_id,
            "title": group.get("title") or "Untitled",
            "romanizedTitle": musicbrainz.romanized_release_group_title(group),
            "date": group.get("first-release-date") or "",
            "type": group.get("primary-type") or "Other",
            "secondaryTypes": [
                name for name in group.get("secondary-types") or [] if name
            ],
            "disambiguation": group.get("disambiguation") or "",
            "coverArt": release_group_cover_art(group_id),
            "animeNames": anime_names.get(group_id.casefold(), []),
            "availableInPlex": group_id in plex_groups,
            "availableInLidarr": bool(lidarr_group),
            "fullyAvailableInLidarr": bool(
                lidarr_group and lidarr_group.get("fullyAvailable")
            ),
            "requestStatus": request_status,
            "downloadStatus": download_status,
            "plexReleases": [
                _plex_release_summary(item)
                for item in plex_groups.get(group_id, [])
            ],
            "matchedTracks": list(dict.fromkeys(matched_tracks[group_id])),
        })
    response = jsonify({
        "results": results,
        "candidateCount": len(results),
    })
    response.headers["Cache-Control"] = "private, max-age=30"
    return response


@blueprint.get("/api/music/release-group/<mbid>/availability")
@login_required
def release_group_availability(mbid):
    """Return live album completion and Plex editions from local indexes."""
    plex_releases = _plex_release_group_inventory().get(mbid, [])
    lidarr_album = lidarr.cached_library_availability().get(mbid.casefold())
    available_in_plex = bool(plex_releases)
    fully_available_in_lidarr = bool(
        lidarr_album and lidarr_album.get("fullyAvailable")
    )
    request_status, download_status = _release_group_lifecycle(
        mbid, lidarr_album, _download_snapshot().get(mbid.casefold()),
        mbid.casefold() in pending_lidarr_search_mbids([mbid]),
    )
    return _availability_response({
        "id": mbid,
        "availableInPlex": available_in_plex,
        "availableInLidarr": bool(lidarr_album),
        "fullyAvailableInLidarr": fully_available_in_lidarr,
        "requestStatus": request_status,
        "downloadStatus": download_status,
        "plexReleases": [
            _plex_release_summary(item) for item in plex_releases
        ],
        "ownedReleaseIds": sorted({
            item["musicbrainzReleaseId"]
            for item in plex_releases
            if item.get("musicbrainzReleaseId")
        }),
        "settled": request_status not in {"queued", "downloading"} and _availability_settled(
            available_in_lidarr=fully_available_in_lidarr,
            available_in_plex=available_in_plex,
        ),
    })


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
        track_search_index.index_artist(data)
        raw_groups, index_pages, offset = [], [], 0
        while True:
            page = musicbrainz.get(
                "/release-group", "aliases", priority=priority,
                force_refresh=force_refresh,
                cache_only=cache_only,
                artist=mbid, limit=100, offset=offset,
            )
            if page is None:
                return None
            index_pages.append((
                page,
                musicbrainz.metadata_cache_key(
                    "/release-group",
                    "aliases",
                    artist=mbid,
                    limit=100,
                    offset=offset,
                ),
            ))
            batch = page.get("release-groups", [])
            raw_groups.extend(batch)
            total = page.get("release-group-count", len(raw_groups))
            if offset + len(batch) >= total or not batch:
                break
            offset += len(batch)
        track_search_index.replace_musicbrainz_artist_discography(
            mbid,
            index_pages,
        )
    plex_groups = _plex_release_group_inventory()
    lidarr_groups = lidarr.cached_library_availability()
    download_groups = _download_snapshot()
    pending_groups = pending_lidarr_search_mbids(
        group.get("id") for group in raw_groups
    )
    anime_names = anime_theme_links.anime_names_for_release_groups(
        group.get("id") for group in raw_groups
    )
    groups = [
        {
            "animeNames": anime_names.get(str(group["id"]).casefold(), []),
            "id": group["id"], "title": group.get("title", "Untitled"),
            "romanizedTitle": musicbrainz.romanized_release_group_title(group),
            "date": group.get("first-release-date", ""),
            "type": group.get("primary-type") or "Other",
            "secondaryTypes": [name for name in group.get("secondary-types") or [] if name],
            "disambiguation": group.get("disambiguation", ""),
            "coverArt": release_group_cover_art(group["id"]),
            "availableInPlex": group["id"] in plex_groups,
            "availableInLidarr": str(group["id"]).casefold() in lidarr_groups,
            "fullyAvailableInLidarr": bool(
                lidarr_groups.get(str(group["id"]).casefold(), {}).get("fullyAvailable")
            ),
            "requestStatus": _release_group_lifecycle(
                group["id"], lidarr_groups.get(str(group["id"]).casefold()),
                download_groups.get(str(group["id"]).casefold()),
                str(group["id"]).casefold() in pending_groups,
            )[0],
            "downloadStatus": _release_group_lifecycle(
                group["id"], lidarr_groups.get(str(group["id"]).casefold()),
                download_groups.get(str(group["id"]).casefold()),
                str(group["id"]).casefold() in pending_groups,
            )[1],
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
    anime_names = anime_theme_links.anime_names_for_release_groups(
        album.get("foreignAlbumId") for album in albums
    )
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
            "animeNames": anime_names.get(str(group_id).casefold(), []),
            "title": album.get("title") or "Untitled",
            "romanizedTitle": musicbrainz.romanized_release_group_title({
                "title": album.get("title"),
            }),
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
        cache_key = ("artist", mbid.casefold())
        assembled = detail_cache.cached_response(cache_key)
        if assembled is not None:
            return assembled
        with detail_cache.build_lock(cache_key) as generation:
            assembled = detail_cache.cached_response(cache_key)
            if assembled is not None:
                return assembled
            prefetch = request.args.get("prefetch") == "1"
            priority = "prefetch" if prefetch else "critical"
            cached = _artist_detail_payload(mbid, priority, cache_only=True)
            if cached is not None:
                return detail_cache.payload_response(
                    cache_key, cached, generation
                )
            if prefetch:
                return _prefetch_cache_miss()
            if request.args.get("complete") != "1":
                try:
                    provisional = _lidarr_artist_detail_payload(mbid)
                except (ValueError, requests.RequestException):
                    provisional = None
                if provisional is not None:
                    return jsonify(provisional)
            payload = _artist_detail_payload(mbid, priority)
            return detail_cache.payload_response(cache_key, payload, generation)
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
        cache_key = ("artist", mbid.casefold())
        artist_metadata_worker.refresh_artist_metadata(mbid, "critical")
        with detail_cache.build_lock(cache_key) as generation:
            assembled = detail_cache.cached_response(cache_key)
            if assembled is not None:
                return assembled
            payload = _artist_detail_payload(
                mbid,
                "critical",
                cache_only=True,
            )
            if payload is None:
                raise requests.RequestException(
                    "The refreshed artist metadata was not cached."
                )
            return detail_cache.payload_response(cache_key, payload, generation)
    except requests.RequestException:
        return api_error(
            "MusicBrainz could not refresh this artist. The previous cache was kept.",
            502,
        )


@blueprint.post("/api/music/artist/<mbid>/revalidate")
@login_required
def revalidate_artist_detail(mbid):
    try:
        mbid = str(UUID(mbid))
    except ValueError:
        return api_error("Invalid MusicBrainz artist ID.")
    result = artist_metadata_worker.request_revalidation(mbid)
    response = jsonify(result)
    response.headers["Cache-Control"] = "no-store"
    return response, 202 if result["polling"] else 200


@blueprint.get("/api/music/artist/<mbid>/revalidation")
@login_required
def artist_revalidation_status(mbid):
    try:
        mbid = str(UUID(mbid))
    except ValueError:
        return api_error("Invalid MusicBrainz artist ID.")
    response = jsonify(artist_metadata_worker.status(mbid))
    response.headers["Cache-Control"] = "no-store"
    return response


def _release_group_detail_payload(mbid, priority, *, cache_only=False):
    data = musicbrainz.get(
        f"/release-group/{quote(mbid)}",
        "aliases+artist-credits+url-rels",
        priority=priority,
        cache_only=cache_only,
    )
    if data is None:
        return None
    track_search_index.index_release_group_page(
        {"release-groups": [data]},
        musicbrainz.metadata_cache_key(
            f"/release-group/{mbid}",
            "aliases+artist-credits+url-rels",
        ),
    )
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
    lidarr_album = lidarr.cached_library_availability().get(mbid.casefold())
    request_status, download_status = _release_group_lifecycle(
        mbid, lidarr_album, _download_snapshot().get(mbid.casefold()),
        mbid.casefold() in pending_lidarr_search_mbids([mbid]),
    )
    owned_release_ids = {
        item.get("musicbrainzReleaseId") for item in plex_releases
        if item.get("musicbrainzReleaseId")
    }
    releases = [
        {
            "id": release["id"], "title": release.get("title", data.get("title", "Untitled")),
            "romanizedTitle": musicbrainz.romanized_release_group_title({
                "title": release.get("title", data.get("title")),
            }),
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
        "romanizedTitle": musicbrainz.romanized_release_group_title(data),
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
        "requestStatus": request_status,
        "downloadStatus": download_status,
        "plexReleases": [
            _plex_release_summary(item) for item in plex_releases
        ],
        "animeThemes": anime_theme_links.links_for_release_group(mbid),
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
    cached_album = lidarr.cached_library_availability().get(mbid.casefold())
    request_status, download_status = _release_group_lifecycle(
        mbid, cached_album, _download_snapshot().get(mbid.casefold()),
        mbid.casefold() in pending_lidarr_search_mbids([mbid]),
    )
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
            "romanizedTitle": musicbrainz.romanized_release_group_title({
                "title": release.get("title") or album.get("title"),
            }),
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
        "romanizedTitle": musicbrainz.romanized_release_group_title({
            "title": album.get("title"),
        }),
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
        "fullyAvailableInLidarr": bool(
            cached_album and cached_album.get("fullyAvailable")
        ),
        "requestStatus": request_status,
        "downloadStatus": download_status,
        "plexReleases": [
            _plex_release_summary(item) for item in plex_releases
        ],
        "animeThemes": anime_theme_links.links_for_release_group(mbid),
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
        cache_key = ("release-group", mbid.casefold())
        assembled = detail_cache.cached_response(cache_key)
        if assembled is not None:
            return assembled
        with detail_cache.build_lock(cache_key) as generation:
            assembled = detail_cache.cached_response(cache_key)
            if assembled is not None:
                return assembled
            prefetch = request.args.get("prefetch") == "1"
            priority = _musicbrainz_priority()
            cached = _release_group_detail_payload(mbid, priority, cache_only=True)
            if cached is not None:
                return detail_cache.payload_response(
                    cache_key, cached, generation
                )
            if prefetch:
                return _prefetch_cache_miss()
            if request.args.get("complete") != "1":
                try:
                    provisional = _lidarr_release_group_detail_payload(mbid)
                except (ValueError, requests.RequestException):
                    provisional = None
                if provisional is not None:
                    return jsonify(provisional)
            payload = _release_group_detail_payload(mbid, priority)
            return detail_cache.payload_response(cache_key, payload, generation)
    except requests.RequestException:
        return api_error("MusicBrainz could not load this album.", 502)


def _release_detail_payload(mbid, priority, *, cache_only=False):
    data = musicbrainz.get(
        f"/release/{quote(mbid)}",
        RELEASE_TRACK_INCLUDES,
        priority=priority,
        cache_only=cache_only,
    )
    if data is None:
        return None
    track_search_index.index_release(
        data,
        musicbrainz.metadata_cache_key(
            f"/release/{mbid}",
            RELEASE_TRACK_INCLUDES,
        ),
    )
    tracks = []
    for medium in data.get("media", []):
        for track in medium.get("tracks", []):
            title = (
                track.get("title")
                or (track.get("recording") or {}).get("title")
                or "Untitled"
            )
            tracks.append({
                "number": track.get("number", ""),
                "title": title,
                "romanizedTitle": musicbrainz.romanized_track_title(title),
                "length": track.get("length"),
                "artist": " · ".join(
                    credit.get("name", "")
                    for credit in track.get("artist-credit", [])
                ),
            })
    return {
        "id": data["id"], "title": data.get("title"),
        "artist": " · ".join(credit.get("name", "") for credit in data.get("artist-credit", [])),
        "date": data.get("date", ""), "country": data.get("country", ""), "tracks": tracks,
    }


def _index_cached_release_detail(mbid):
    indexed = track_search_index.index_cached_release(
        musicbrainz.metadata_cache_key(
            f"/release/{mbid}",
            RELEASE_TRACK_INCLUDES,
        )
    )
    if not indexed:
        track_search_index.index_cached_release(
            musicbrainz.metadata_cache_key(
                f"/release/{mbid}",
                LEGACY_RELEASE_TRACK_INCLUDES,
            )
        )


@blueprint.get("/api/music/release/<mbid>")
@login_required
def release_detail(mbid):
    try:
        cache_key = ("release", mbid.casefold())
        assembled = detail_cache.cached_response(cache_key)
        if assembled is not None:
            _index_cached_release_detail(mbid)
            return assembled
        with detail_cache.build_lock(cache_key) as generation:
            assembled = detail_cache.cached_response(cache_key)
            if assembled is not None:
                _index_cached_release_detail(mbid)
                return assembled
            prefetch = request.args.get("prefetch") == "1"
            payload = _release_detail_payload(
                mbid,
                _musicbrainz_priority(),
                cache_only=prefetch,
            )
            if payload is None:
                return _prefetch_cache_miss()
            return detail_cache.payload_response(cache_key, payload, generation)
    except requests.RequestException:
        return api_error("MusicBrainz could not load this release.", 502)


@blueprint.get("/api/music/artist/<mbid>/anime")
@login_required
def artist_anime(mbid):
    try:
        payload = anime_artist_links.appearances(mbid)
        performances = [
            performance for anime in payload["anime"]
            for performance in anime.get("performances") or []
        ]
        targets = anime_theme_links.release_groups_for_performances(performances)
        group_ids = {group["id"] for groups in targets.values() for group in groups}
        library = lidarr.cached_library_availability() if group_ids else {}
        downloads = _download_snapshot() if group_ids else {}
        pending = pending_lidarr_search_mbids(group_ids) if group_ids else set()
        for performance in performances:
            groups = targets.get((performance["animeSlug"], performance["themeId"]), [])
            performance["releaseGroups"] = []
            for group in groups:
                group_id = group["id"].casefold()
                album = library.get(group_id)
                status, download = _release_group_lifecycle(
                    group_id, album, downloads.get(group_id), group_id in pending,
                )
                performance["releaseGroups"].append({
                    **group, "availableInLidarr": bool(album),
                    "fullyAvailableInLidarr": bool(album and album.get("fullyAvailable")),
                    "requestStatus": status, "downloadStatus": download,
                })
        return jsonify(payload)
    except ValueError:
        return api_error("Invalid artist ID.", 400)
    except requests.RequestException:
        return api_error("Anime appearances could not be loaded. Try again shortly.", 502)
