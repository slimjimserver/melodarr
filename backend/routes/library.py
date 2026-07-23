"""Plex library API routes."""

import requests
from flask import Blueprint, jsonify, request

if __package__ == "backend.routes":
    from ..responses import api_error
    from ..security import login_required
    from ..services import plex
    from ..storage import get_service
else:  # Support the existing `python backend/app.py` entry point.
    from responses import api_error
    from security import login_required
    from services import plex
    from storage import get_service


blueprint = Blueprint("library", __name__)

ARTIST_FIELDS = ("name", "sortName", "section", "musicbrainzId", "artwork", "url")


def _artist_summary(artist):
    """Send only the fields the browser renders.

    The stored snapshot also carries Plex GUIDs, thumbnail paths, and library
    keys, none of which the frontend reads.
    """
    return {field: artist.get(field, "") for field in ARTIST_FIELDS}


@blueprint.get("/api/library")
@login_required
def plex_library():
    config = get_service("plex")
    if not config:
        return api_error("Plex is not configured.", 503)
    try:
        # Serve the background worker's snapshot. Only a genuinely empty cache
        # falls through to a scan, so a page load never blocks on Plex.
        inventory = plex.cached_library_snapshot(config)
        if not inventory.get("artists") and not inventory.get("releaseGroups"):
            inventory = plex.library_snapshot(config)
        artists = inventory.get("artists", [])
        release_groups = inventory.get("releaseGroups", [])
        payload = {
            "artists": [_artist_summary(artist) for artist in artists],
            "count": len(artists),
            "artistCount": len(artists),
            "releaseGroupCount": len(release_groups),
        }
        if request.args.get("includeReleases") == "1":
            payload["releaseGroups"] = release_groups
        response = jsonify(payload)
        response.headers["Cache-Control"] = "private, max-age=60"
        response.set_etag(
            f"{inventory.get('scannedAt', 0)}-{len(artists)}-{len(release_groups)}"
        )
        return response.make_conditional(request)
    except requests.RequestException:
        return api_error("Plex could not be reached or did not accept the token.", 502)
