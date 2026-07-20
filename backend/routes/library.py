"""Plex library API routes."""

import requests
from flask import Blueprint, jsonify, request

if __package__ == "backend.routes":
    from ..responses import api_error
    from ..security import admin_required
    from ..services import plex
    from ..storage import get_service
else:  # Support the existing `python backend/app.py` entry point.
    from responses import api_error
    from security import admin_required
    from services import plex
    from storage import get_service


blueprint = Blueprint("library", __name__)


@blueprint.get("/api/library")
@admin_required
def plex_library():
    config = get_service("plex")
    if not config:
        return api_error("Plex is not configured.", 503)
    try:
        inventory = plex.library_snapshot(config)
        artists = inventory.get("artists", [])
        release_groups = inventory.get("releaseGroups", [])
        payload = {
            "artists": artists,
            "count": len(artists),
            "artistCount": len(artists),
            "releaseGroupCount": len(release_groups),
        }
        if request.args.get("includeReleases") == "1":
            payload["releaseGroups"] = release_groups
        return jsonify(payload)
    except requests.RequestException:
        return api_error("Plex could not be reached or did not accept the token.", 502)
