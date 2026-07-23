"""Artist and release-group artwork routes."""

from uuid import UUID

from flask import Blueprint, request

if __package__ == "backend.routes":
    from ..artwork_cache import cached_artwork, plex_artist_artwork_key
    from ..responses import api_error
    from ..security import login_required
    from ..services import lidarr, musicbrainz, plex
    from ..storage import get_service
else:
    from artwork_cache import cached_artwork, plex_artist_artwork_key
    from responses import api_error
    from security import login_required
    from services import lidarr, musicbrainz, plex
    from storage import get_service


blueprint = Blueprint("artwork", __name__)


def _requested_size():
    """Read the variant requested by the browser, defaulting to the original."""
    return request.args.get("size") or None


@blueprint.get("/api/artwork/release-group/<mbid>")
@login_required
def release_group_artwork(mbid):
    try:
        mbid = str(UUID(mbid))
    except ValueError:
        return api_error("Invalid MusicBrainz release-group ID.")
    return cached_artwork(
        f"release-group-{mbid}",
        # Cache the larger Cover Art Archive rendition once and derive every
        # smaller variant from it, instead of sending browsers to the archive.
        musicbrainz.cover_art_url(mbid, size=500),
        size=_requested_size(),
    )


@blueprint.get("/api/artwork/artist/<mbid>")
@login_required
def artist_artwork(mbid):
    try:
        mbid = str(UUID(mbid))
    except ValueError:
        return api_error("Invalid MusicBrainz artist ID.")
    return cached_artwork(
        f"artist-{mbid}",
        lambda: lidarr.artist_image_url(mbid),
        size=_requested_size(),
    )


@blueprint.get("/api/artwork/artist/<mbid>/large")
@login_required
def artist_large_artwork(mbid):
    """Serve the detail-page rendition, kept for previously issued URLs."""
    try:
        mbid = str(UUID(mbid))
    except ValueError:
        return api_error("Invalid MusicBrainz artist ID.")
    return cached_artwork(
        f"artist-{mbid}",
        lambda: lidarr.artist_image_url(mbid),
        size="large",
    )


@blueprint.get("/api/artwork/plex-artist/<rating_key>")
@login_required
def plex_artist_artwork(rating_key):
    """Serve a selected-library artist thumbnail using Plex authentication."""
    config = get_service("plex")
    if not config:
        return "", 404
    artist = plex.cached_library_index(config)["artistsByRatingKey"].get(rating_key)
    if not artist or not artist.get("thumb"):
        return "", 404
    server_id = config.get("machineIdentifier") or config.get("url", "")
    source_url = f"{config['url'].rstrip('/')}/{artist['thumb'].lstrip('/')}"
    return cached_artwork(
        plex_artist_artwork_key(server_id, rating_key, artist["thumb"]),
        source_url,
        headers={"X-Plex-Token": config.get("token", "")},
        size=_requested_size(),
    )
