"""Shared release-group initiation, including existing history and notifications."""

from dataclasses import dataclass

import requests

if __package__ == "backend.services":
    from .. import notifications
    from ..request_locks import request_lock
    from ..storage import enqueue_lidarr_search, get_service, pending_lidarr_search, record_request
    from ..workers import lidarr_searches as lidarr_search_worker
    from ..workers import lidarr_library as lidarr_library_worker
    from . import lidarr
else:
    import notifications
    from request_locks import request_lock
    from storage import enqueue_lidarr_search, get_service, pending_lidarr_search, record_request
    from workers import lidarr_searches as lidarr_search_worker
    from workers import lidarr_library as lidarr_library_worker
    from services import lidarr


@dataclass(frozen=True)
class ReleaseGroupRequestResult:
    payload: dict
    status_code: int
    safe_error: str = ""

    @property
    def accepted(self):
        return 200 <= self.status_code < 300


def _error(message, status_code=400, *, safe_error=None):
    return ReleaseGroupRequestResult({"error": message}, status_code, safe_error or message)


def request_release_group_for_user(mbid, user, *, anime_context=None):
    """Submit one shared acquisition; callers render the structured result.

    Anime snapshots are validated by the route. Locking also coalesces separate
    recordings that select the same group and requests from separate processes.
    No main-database transaction is held during upstream I/O.
    """
    try:
        with request_lock("release-group", mbid.casefold()):
            return _request_release_group(mbid, user, anime_context or {})
    except TimeoutError:
        return _error("Another release-group request is still being processed. Retry shortly.", 503)


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
        "search_metadata": albums,
    }


def _request_release_group(mbid, user, anime_context):
    pending = pending_lidarr_search(mbid)
    if pending:
        record_request(
            user["id"],
            "release-group",
            mbid,
            pending["name"],
            **anime_context,
        )
        notifications.queue_admin_request(
            user["id"], user["username"], mbid, pending["name"]
        )
        return ReleaseGroupRequestResult({
            "message": (
                f"{pending['name']} is already queued. Its album search will start "
                "automatically after Lidarr finishes refreshing its metadata."
            ),
            "pending": True,
        }, 202)

    lidarr_config = get_service("lidarr")
    if not lidarr_config:
        return _error("Lidarr is not configured.", 503)

    try:
        lookup = lidarr.lookup_album(mbid)
        lookup.raise_for_status()
        albums = lookup.json()
        if not albums:
            return _error("Lidarr could not find this release group.", 404)

        album = albums[0]
        defaults = lidarr_config.get("defaults", {})
        if not defaults.get("qualityProfileId") or not defaults.get("metadataProfileId"):
            return _error("Finish configuring Lidarr's quality and metadata profiles in Settings.", 503)

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
                return _error("This release group is in Lidarr, but Melodarr could not find its album record.", 502)

            statistics = created_album.get("statistics", {})
            total_tracks = statistics.get("totalTrackCount", created_album.get("trackCount", 0))
            downloaded_tracks = statistics.get("trackFileCount", 0)
            if total_tracks and downloaded_tracks >= total_tracks:
                history_metadata = _release_history_metadata(
                    created_album, album
                )
                record_request(
                    user["id"],
                    "release-group",
                    mbid,
                    created_album.get("title", album.get("title", "Release group")),
                    **history_metadata,
                    **anime_context,
                )
                notifications.queue_admin_request(
                    user["id"],
                    user["username"],
                    mbid,
                    created_album.get(
                        "title", album.get("title", "Release group")
                    ),
                    history_metadata["artist_name"],
                )
                lidarr_library_worker.request_scan()
                return ReleaseGroupRequestResult({"message": "This release group is already fully available in Lidarr.", "alreadyExists": True}, 200)
        else:
            added.raise_for_status()
            created_album = added.json()

        artist_id = (
            created_album.get("artistId")
            or (created_album.get("artist") or {}).get("id")
        )
        if not artist_id:
            return _error(
                "Lidarr did not return an artist ID for the metadata refresh.", 502
            )

        title = created_album.get("title", album.get("title", "Release group"))
        history_metadata = _release_history_metadata(created_album, album)
        enqueue_lidarr_search(
            user["id"],
            mbid,
            created_album["id"],
            artist_id,
            title,
            **history_metadata,
            **anime_context,
        )
        notifications.queue_admin_request(
            user["id"],
            user["username"],
            mbid,
            title,
            history_metadata["artist_name"],
        )
        lidarr_search_worker.request_work()
        lidarr_library_worker.request_scan()
        return ReleaseGroupRequestResult({
            "message": (
                f"{title} was sent to Lidarr. Its album search is queued and will "
                "start automatically after the release group refresh completes."
            ),
            "album": created_album,
            "pending": True,
            "refreshType": "album",
        }, 202)
    except requests.HTTPError as exc:
        detail = exc.response.text[:300] if exc.response is not None else ""
        return _error(f"Lidarr rejected the release group. {detail}", 502, safe_error="Lidarr rejected the release group.")
    except (ValueError, TypeError):
        return _error("Lidarr configuration is invalid.", 503)
    except requests.RequestException:
        return _error("Lidarr could not be reached.", 502)
