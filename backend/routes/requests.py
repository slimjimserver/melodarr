"""Lidarr artist and release-group request routes."""

import re

import requests
from flask import Blueprint, jsonify

if __package__ == "backend.routes":
    from ._safe_errors import public_exception_message
    from ..responses import api_error, request_json_object
    from ..security import current_user, login_required
    from ..services import lidarr, release_requests
    from ..storage import (
        get_service,
        record_request,
    )
    from ..workers import lidarr_library as lidarr_library_worker
else:  # Support the existing `python backend/app.py` entry point.
    from routes._safe_errors import public_exception_message
    from responses import api_error, request_json_object
    from security import current_user, login_required
    from services import lidarr, release_requests
    from storage import (
        get_service,
        record_request,
    )
    from workers import lidarr_library as lidarr_library_worker


blueprint = Blueprint("requests", __name__)

_ANIME_SLUG_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,199}")
_ANIME_CONTEXT_MESSAGES = (
    "Anime slug is invalid.",
    "Anime slug is required.",
    "Anime slug must be 200 characters or fewer.",
    "Anime name is required.",
    *(f"{label} must be text." for label in (
        "Anime slug", "Anime name", "Theme label", "Song title"
    )),
    *(f"{label} must be 500 characters or fewer." for label in (
        "Anime name", "Theme label", "Song title"
    )),
    "Theme label is required.",
    "AnimeThemes theme ID must be a positive integer.",
    "AnimeThemes song ID must be a positive integer.",
)


def _context_text(body, name, label, *, maximum=500, required=False):
    value = body.get(name)
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text.")
    value = value.strip()
    if required and not value:
        raise ValueError(f"{label} is required.")
    if len(value) > maximum:
        raise ValueError(f"{label} must be {maximum} characters or fewer.")
    return value


def _context_id(body, name, label, *, required=False):
    value = body.get(name)
    if value in (None, "") and not required:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a positive integer.")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a positive integer.") from exc
    if normalized <= 0 or str(value).strip() != str(normalized):
        raise ValueError(f"{label} must be a positive integer.")
    return normalized


def _anime_request_context(body):
    """Validate optional snapshots used to link history back to an anime theme."""
    names = {
        "animeSlug",
        "animeName",
        "themeId",
        "themeLabel",
        "songId",
        "songTitle",
    }
    if not any(name in body for name in names):
        return {}
    anime_slug = _context_text(
        body,
        "animeSlug",
        "Anime slug",
        maximum=200,
        required=True,
    )
    if not _ANIME_SLUG_PATTERN.fullmatch(anime_slug):
        raise ValueError("Anime slug is invalid.")
    return {
        "anime_slug": anime_slug,
        "anime_name": _context_text(
            body, "animeName", "Anime name", required=True
        ),
        "theme_id": _context_id(
            body, "themeId", "AnimeThemes theme ID", required=True
        ),
        "theme_label": _context_text(
            body, "themeLabel", "Theme label", required=True
        ),
        "song_id": _context_id(body, "songId", "AnimeThemes song ID"),
        "song_title": _context_text(body, "songTitle", "Song title"),
    }


@blueprint.post("/api/request")
@login_required
def request_artist():
    body = request_json_object()
    if body is None:
        return api_error("Request body must be a JSON object.")
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
            record_request(current_user()["id"], "artist", mbid, artist.get("artistName", "Artist"), search_metadata=(artist,))
            lidarr_library_worker.request_scan()
            return jsonify({"message": "This artist is already in Lidarr.", "alreadyExists": True})
        added.raise_for_status()
        created_artist = added.json()

        editor_update = lidarr.update_artists({
            "artistIds": [created_artist["id"]],
            "monitorNewItems": artist["monitorNewItems"],
        })
        editor_update.raise_for_status()
        record_request(current_user()["id"], "artist", mbid, artist.get("artistName", "Artist"), search_metadata=(artist, created_artist))
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
    body = request_json_object()
    if body is None:
        return api_error("Request body must be a JSON object.")
    mbid = str(body.get("mbid", "")).strip()
    if not mbid:
        return api_error("A MusicBrainz release-group ID is required.")
    user = current_user()
    try:
        anime_context = _anime_request_context(body)
    except ValueError as exc:
        return api_error(public_exception_message(
            exc, _ANIME_CONTEXT_MESSAGES, "Invalid anime request context.",
            context="Anime release-group request context",
        ))
    result = release_requests.request_release_group_for_user(mbid, user, anime_context=anime_context)
    return jsonify(result.payload), result.status_code
