"""Lidarr artist and release-group request routes."""

import requests
from flask import Blueprint, jsonify, request

if __package__ == "backend.routes":
    from ..responses import api_error
    from ..security import current_user, login_required
    from ..services import lidarr
    from ..storage import (
        enqueue_lidarr_search,
        get_service,
        pending_lidarr_search,
        record_request,
    )
    from ..workers import lidarr_searches as lidarr_search_worker
    from ..workers import lidarr_library as lidarr_library_worker
else:  # Support the existing `python backend/app.py` entry point.
    from responses import api_error
    from security import current_user, login_required
    from services import lidarr
    from storage import (
        enqueue_lidarr_search,
        get_service,
        pending_lidarr_search,
        record_request,
    )
    from workers import lidarr_searches as lidarr_search_worker
    from workers import lidarr_library as lidarr_library_worker


blueprint = Blueprint("requests", __name__)


def _release_history_metadata(*albums):
    """Extract display metadata from Lidarr's lookup and created-album shapes."""
    artist_name = ""
    release_type = ""
    release_date = ""
    for album in albums:
        if not isinstance(album, dict):
            continue
        artist = album.get("artist") or {}
        if not isinstance(artist, dict):
            artist = {}
        artist_name = artist_name or str(
            album.get("artistName")
            or artist.get("artistName")
            or artist.get("name")
            or ""
        ).strip()
        release_type = release_type or str(
            album.get("albumType")
            or album.get("releaseType")
            or album.get("type")
            or ""
        ).strip()
        release_date = release_date or str(
            album.get("releaseDate")
            or album.get("firstReleaseDate")
            or ""
        ).strip()[:10]
    return {
        "artist_name": artist_name,
        "release_type": release_type,
        "release_date": release_date,
    }


@blueprint.post("/api/request")
@login_required
def request_artist():
    body = request.get_json(silent=True) or {}
    mbid = str(body.get("mbid", "")).strip()
    if not mbid:
        return api_error("A MusicBrainz artist ID is required.")
    try:
        lookup = lidarr.lookup_artist(mbid)
        lookup.raise_for_status()
        matches = lookup.json()
        if not matches:
            return api_error("Lidarr could not find this artist.", 404)
        artist = matches[0]

        defaults = (get_service("lidarr") or {}).get("defaults", {})
        if not (body.get("rootFolderPath") or defaults.get("rootFolderPath")) or not defaults.get("qualityProfileId") or not defaults.get("metadataProfileId"):
            return api_error("Finish configuring Lidarr's root folder and profiles in Settings.", 503)
        artist.update({
            "rootFolderPath": body.get("rootFolderPath") or defaults.get("rootFolderPath"),
            "qualityProfileId": int(defaults.get("qualityProfileId")),
            "metadataProfileId": int(defaults.get("metadataProfileId")),
            "tags": body.get("tags") if body.get("tags") is not None else defaults.get("tags", []),
            "monitored": True,
            "monitorNewItems": defaults.get("monitorNewItems", "all"),
            "addOptions": {
                "monitor": defaults.get("monitor", "all"),
                "searchForMissingAlbums": body.get("searchForMissingAlbums") if body.get("searchForMissingAlbums") is not None else defaults.get("searchForMissingAlbums", True),
            },
        })
        added = lidarr.add_artist(artist)
        if added.status_code == 400 and "already" in added.text.lower():
            record_request(current_user()["id"], "artist", mbid, artist.get("artistName", "Artist"))
            lidarr_library_worker.request_scan()
            return jsonify({"message": "This artist is already in Lidarr.", "alreadyExists": True})
        added.raise_for_status()
        created_artist = added.json()

        editor_update = lidarr.update_artists({
            "artistIds": [created_artist["id"]],
            "monitorNewItems": artist["monitorNewItems"],
        })
        editor_update.raise_for_status()
        record_request(current_user()["id"], "artist", mbid, artist.get("artistName", "Artist"))
        lidarr_library_worker.request_scan()
        return jsonify({"message": f"{artist.get('artistName', 'Artist')} was sent to Lidarr.", "artist": created_artist}), 201
    except (ValueError, TypeError):
        return api_error("Choose a root folder, quality profile, and metadata profile.")
    except requests.HTTPError as exc:
        detail = exc.response.text[:300] if exc.response is not None else ""
        return api_error(f"Lidarr rejected the request. {detail}", 502)
    except requests.RequestException:
        return api_error("Lidarr could not be reached.", 502)


@blueprint.post("/api/request/release-group")
@login_required
def request_release_group():
    body = request.get_json(silent=True) or {}
    mbid = str(body.get("mbid", "")).strip()
    if not mbid:
        return api_error("A MusicBrainz release-group ID is required.")
    pending = pending_lidarr_search(mbid)
    if pending:
        return jsonify({
            "message": (
                f"{pending['name']} is already queued. Its album search will start "
                "automatically after Lidarr finishes refreshing its metadata."
            ),
            "pending": True,
        }), 202

    try:
        lookup = lidarr.lookup_album(mbid)
        lookup.raise_for_status()
        albums = lookup.json()
        if not albums:
            return api_error("Lidarr could not find this release group.", 404)

        album = albums[0]
        defaults = (get_service("lidarr") or {}).get("defaults", {})
        if not defaults.get("qualityProfileId") or not defaults.get("metadataProfileId"):
            return api_error("Finish configuring Lidarr's quality and metadata profiles in Settings.", 503)

        album_artist = album.setdefault("artist", {})
        album_artist.update({
            "qualityProfileId": int(defaults["qualityProfileId"]),
            "metadataProfileId": int(defaults["metadataProfileId"]),
            "rootFolderPath": defaults.get("rootFolderPath"),
            "tags": defaults.get("tags", []),
            "monitored": True,
            "monitorNewItems": defaults.get("monitorNewItems", "all"),
        })
        album["monitored"] = True
        album["addOptions"] = {
            "addType": "automatic",
            # Melodarr explicitly queues the search after its required metadata
            # refresh. Letting Lidarr auto-search here can race metadata creation.
            "searchForNewAlbum": False,
        }
        added = lidarr.add_album(album)
        if added.status_code == 400 and "already" in added.text.lower():
            existing_response = lidarr.albums_by_release_group(mbid)
            existing_response.raise_for_status()
            existing_albums = existing_response.json()
            if isinstance(existing_albums, dict):
                existing_albums = existing_albums.get("records", [])
            created_album = next(
                (item for item in existing_albums if item.get("foreignAlbumId") == mbid),
                None,
            )
            if not created_album:
                return api_error("This release group is in Lidarr, but Melodarr could not find its album record.", 502)

            statistics = created_album.get("statistics", {})
            total_tracks = statistics.get("totalTrackCount", created_album.get("trackCount", 0))
            downloaded_tracks = statistics.get("trackFileCount", 0)
            if total_tracks and downloaded_tracks >= total_tracks:
                record_request(
                    current_user()["id"],
                    "release-group",
                    mbid,
                    created_album.get("title", album.get("title", "Release group")),
                    **_release_history_metadata(created_album, album),
                )
                lidarr_library_worker.request_scan()
                return jsonify({"message": "This release group is already fully available in Lidarr.", "alreadyExists": True})
        else:
            added.raise_for_status()
            created_album = added.json()

        artist_id = (
            created_album.get("artistId")
            or (created_album.get("artist") or {}).get("id")
        )
        if not artist_id:
            return api_error(
                "Lidarr did not return an artist ID for the metadata refresh.", 502
            )

        title = created_album.get("title", album.get("title", "Release group"))
        enqueue_lidarr_search(
            current_user()["id"],
            mbid,
            created_album["id"],
            artist_id,
            title,
            **_release_history_metadata(created_album, album),
        )
        lidarr_search_worker.request_work()
        lidarr_library_worker.request_scan()
        return jsonify({
            "message": (
                f"{title} was sent to Lidarr. Its album search is queued and will "
                "start automatically after the release group refresh completes."
            ),
            "album": created_album,
            "pending": True,
            "refreshType": "album",
        }), 202
    except requests.HTTPError as exc:
        detail = exc.response.text[:300] if exc.response is not None else ""
        return api_error(f"Lidarr rejected the release group. {detail}", 502)
    except requests.RequestException:
        return api_error("Lidarr could not be reached.", 502)
