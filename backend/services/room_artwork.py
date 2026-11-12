"""Room presentation backed by the existing Plex index and artwork disk cache."""

import logging
import re
import sqlite3
from urllib.parse import urlencode

if __package__ == "backend.services":
    from .. import storage, track_search_index
    from ..artwork_cache import artwork_cache_file, plex_album_artwork_key, warm_artwork
    from . import plex
else:
    import storage
    import track_search_index
    from artwork_cache import artwork_cache_file, plex_album_artwork_key, warm_artwork
    from services import plex


logger = logging.getLogger(__name__)


def album_cache_key(server_id, album):
    thumb = str(album.get("thumb") or "")
    # Only indexed, server-local album thumbnail paths are accepted. Tokens,
    # external sources and query strings never become public image identities.
    if not album.get("ratingKey") or not re.fullmatch(
        r"/library/metadata/\d+/thumb(?:/[^?\s]+)?", thumb
    ):
        return ""
    return plex_album_artwork_key(server_id, album["ratingKey"], thumb)


def warm_album_artwork(config):
    """Library worker fills the shared cache; Room HTTP paths stay cache-only."""
    if not config.get("token"):
        return
    server_id = config.get("machineIdentifier") or config.get("url", "")
    albums = plex.cached_library_index(config)["releaseGroupsByRatingKey"].values()
    for album in albums:
        key = album_cache_key(server_id, album)
        if not key:
            continue
        query = urlencode(
            {
                "url": album["thumb"],
                "width": 640,
                "height": 640,
                "minSize": 0,
                "upscale": 0,
                "format": "jpeg",
            }
        )
        warm_artwork(
            key,
            f"{config['url'].rstrip('/')}/photo/:/transcode?{query}",
            headers={"X-Plex-Token": config["token"]},
        )


def context(room, rating_keys):
    """Optional metadata failures cannot interrupt queue synchronization."""
    try:
        config = storage.get_service("plex") or {}
        server_id = config.get("machineIdentifier") or config.get("url", "")
        if server_id != room["server_id"]:
            return {}, {}
        return (
            track_search_index.plex_tracks_by_rating_key(server_id, rating_keys),
            plex.cached_library_index(config)["releaseGroupsByRatingKey"],
        )
    except (OSError, sqlite3.Error, ValueError) as exc:
        logger.warning("Room artwork lookup failed (%s)", type(exc).__name__)
        return {}, {}


def fields(room, context, *, rating_key=None, release_group=None):
    tracks, albums = context
    track = tracks.get(str(rating_key or ""), {})
    album = albums.get(track.get("albumRatingKey"), {})
    release_group = release_group or track.get("musicbrainzReleaseGroupId")
    fallback = (
        f"/api/rooms/{room['code']}/artwork/{release_group}" if release_group else ""
    )
    key = album_cache_key(room["server_id"], album)
    artwork = (
        f"/api/rooms/{room['code']}/plex-artwork/{key}"
        if key and artwork_cache_file(key)
        else fallback
    )
    return {
        "artwork": artwork,
        "artworkFallback": fallback if artwork != fallback else "",
    }


def public_track(room, stored, context):
    """Internal Plex IDs are retained in storage and omitted at the HTTP boundary."""
    if not stored:
        return {}
    return {
        **{field: stored.get(field, "") for field in ("title", "artist", "album")},
        **fields(room, context, rating_key=stored.get("ratingKey")),
    }
