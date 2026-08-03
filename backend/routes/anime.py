"""Anime discovery detail routes."""

from urllib.parse import urlparse
from uuid import UUID

import requests
from flask import Blueprint, jsonify, request

if __package__ == "backend.routes":
    from ..responses import api_error
    from ..security import admin_required, login_required
    from ..services import (
        anime_mapping_registry,
        anime_musicbrainz,
        animethemes,
        lidarr,
        musicbrainz,
    )
    from ..workers import anime_metadata as anime_metadata_worker
else:  # Support the existing `python backend/app.py` entry point.
    from responses import api_error
    from security import admin_required, login_required
    from services import (
        anime_mapping_registry,
        anime_musicbrainz,
        animethemes,
        lidarr,
        musicbrainz,
    )
    from workers import anime_metadata as anime_metadata_worker


blueprint = Blueprint("anime", __name__)


class AutomaticMatchUnavailable(Exception):
    """The displayed automatic result can no longer be safely confirmed."""


def _load_anime(slug):
    anime = animethemes.detail(slug)
    if anime is None:
        raise LookupError("Anime was not found on AnimeThemes.")
    return anime


def _load_series(slug):
    series = animethemes.series_detail(slug)
    if series is None:
        raise LookupError("Series was not found on AnimeThemes.")
    return series


def _public_mapping(mapping, lidarr_groups=None):
    """Normalize cached resolver data to the browser's release-card shape."""
    result = dict(mapping or {})
    result["status"] = result.get("state", "pending")
    lidarr_groups = lidarr_groups or {}

    def public_groups(groups):
        normalized = {}
        for group in groups or []:
            if not isinstance(group, dict) or not group.get("id"):
                continue
            group_id = str(group["id"])
            availability = lidarr_groups.get(group_id)
            normalized.setdefault(group_id, {
                **group,
                "name": (
                    group.get("name")
                    or group.get("title")
                    or "Untitled release"
                ),
                "availableInLidarr": bool(availability),
                "fullyAvailableInLidarr": bool(
                    availability and availability.get("fullyAvailable")
                ),
            })
        return list(normalized.values())

    result["releaseGroups"] = public_groups(result.get("releaseGroups"))
    recording_candidates = []
    for recording in result.get("recordingCandidates") or []:
        if not isinstance(recording, dict):
            continue
        recording_candidates.append({
            **recording,
            "releaseGroups": public_groups(recording.get("releaseGroups")),
        })
    result["recordingCandidates"] = recording_candidates
    if result.get("state") == "ambiguous":
        alternatives = []
        for recording in recording_candidates:
            alternatives.extend(recording["releaseGroups"])
        result["candidates"] = public_groups(alternatives)
    return result


def _theme_song_id(theme):
    song = theme.get("song") if isinstance(theme, dict) else None
    song_id = song.get("id") if isinstance(song, dict) else None
    try:
        song_id = int(song_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("This theme does not have a usable AnimeThemes song ID.") from exc
    if song_id <= 0:
        raise ValueError("This theme does not have a usable AnimeThemes song ID.")
    return song_id


def _theme_by_id(anime, theme_id):
    theme = next(
        (
            item for item in anime.get("themes") or []
            if isinstance(item, dict) and str(item.get("id")) == str(theme_id)
        ),
        None,
    )
    if theme is None:
        raise LookupError("Anime theme was not found on AnimeThemes.")
    return theme


def _release_group_mbid(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Enter a MusicBrainz release-group URL or MBID.")
    raw = value.strip()
    if "://" in raw:
        try:
            parsed = urlparse(raw)
            hostname = (parsed.hostname or "").casefold()
            path_parts = [part for part in parsed.path.split("/") if part]
        except ValueError as exc:
            raise ValueError(
                "Enter a valid MusicBrainz release-group URL or MBID."
            ) from exc
        if parsed.scheme.casefold() != "https":
            raise ValueError("MusicBrainz release-group URLs must use HTTPS.")
        if hostname not in {"musicbrainz.org", "www.musicbrainz.org"}:
            raise ValueError("The URL must point to musicbrainz.org.")
        if len(path_parts) == 2 and path_parts[0] == "release":
            raise ValueError(
                "Use a MusicBrainz release-group URL, not an individual release URL."
            )
        if len(path_parts) != 2 or path_parts[0] != "release-group":
            raise ValueError("Enter a MusicBrainz release-group URL or MBID.")
        raw = path_parts[1]
    try:
        return str(UUID(raw))
    except (ValueError, AttributeError) as exc:
        raise ValueError("MusicBrainz release-group MBID must be a valid UUID.") from exc


def _musicbrainz_release_group(mbid):
    try:
        group = musicbrainz.get(
            f"/release-group/{mbid}",
            "aliases+artist-credits",
            priority="critical",
        )
    except requests.HTTPError as exc:
        if getattr(exc.response, "status_code", None) == 404:
            raise LookupError("MusicBrainz release group was not found.") from exc
        raise
    if not isinstance(group, dict) or str(group.get("id") or "") != mbid:
        raise LookupError("MusicBrainz release group was not found.")
    return group


def _registry_target(group):
    credits = [
        credit for credit in group.get("artist-credit") or []
        if isinstance(credit, dict)
    ]
    return {
        "releaseGroupId": str(group["id"]),
        "recordingIds": [],
        "artistIds": [
            str(credit.get("artist", {}).get("id"))
            for credit in credits
            if credit.get("artist", {}).get("id")
        ],
        "releaseGroupTitle": str(group.get("title") or "Untitled release"),
        "artistName": " · ".join(
            str(credit.get("name") or "").strip()
            for credit in credits
            if str(credit.get("name") or "").strip()
        ) or "Unknown artist",
        "primaryType": str(group.get("primary-type") or "Other"),
        "firstReleaseDate": str(group.get("first-release-date") or ""),
        "preferred": True,
    }


def _automatic_confirmation_target(theme, requested_release_group=None):
    """Return only the recommended group from a supported cached match."""
    mapping = anime_musicbrainz.cached_mapping(theme)
    groups = [
        group for group in (mapping or {}).get("releaseGroups") or []
        if isinstance(group, dict) and group.get("id")
    ]
    match_method = str((mapping or {}).get("matchMethod") or "").strip()
    recording_id = str((mapping or {}).get("recordingId") or "").strip()
    if (
        not mapping
        or mapping.get("state") != "resolved"
        or match_method not in {
            "recording-search",
            "artist-discography-title",
        }
        or (match_method == "recording-search" and not recording_id)
        or not groups
    ):
        raise AutomaticMatchUnavailable(
            "The automatic match is no longer available. "
            "Run matching again before confirming it."
        )

    recommended_id = str(
        mapping.get("recommendedReleaseGroupId")
        or (mapping.get("recommended") or {}).get("id")
        or groups[0]["id"]
    )
    recommended_id = _release_group_mbid(recommended_id)
    requested_id = (
        _release_group_mbid(requested_release_group)
        if requested_release_group
        else recommended_id
    )
    if requested_id != recommended_id:
        raise AutomaticMatchUnavailable(
            "Only the recommended release group can be confirmed directly. "
            "Use Override mapping to select a different release group."
        )
    group = next(
        (item for item in groups if str(item.get("id")) == recommended_id),
        None,
    )
    if group is None:
        raise AutomaticMatchUnavailable(
            "The recommended release group is no longer part of this match."
        )
    artist_ids = [
        str(value).strip()
        for value in mapping.get("artistIds") or []
        if str(value).strip()
    ]
    source_artists = _artist_names_for_theme(theme)
    return recommended_id, {
        "releaseGroupId": recommended_id,
        "recordingIds": [recording_id] if recording_id else [],
        "artistIds": artist_ids,
        "releaseGroupTitle": str(
            group.get("name") or group.get("title") or "Untitled release"
        ),
        "artistName": str(
            group.get("artist") or " · ".join(source_artists) or "Unknown artist"
        ),
        "primaryType": str(group.get("type") or "Other"),
        "firstReleaseDate": str(group.get("date") or ""),
        "scope": str(
            group.get("mappingScope")
            or mapping.get("mappingScope")
            or "unknown"
        ),
        "preferred": True,
    }


def _artist_names_for_theme(theme):
    song = theme.get("song") if isinstance(theme, dict) else {}
    return [
        str(artist.get("as") or artist.get("name") or "").strip()
        for artist in (song or {}).get("artists") or []
        if isinstance(artist, dict)
        and str(artist.get("as") or artist.get("name") or "").strip()
    ]


def _automatic_theme_mapping(theme, lidarr_groups=None):
    key = anime_musicbrainz.theme_mapping_key(theme)
    documents = anime_metadata_worker.mappings_for([theme])
    return _public_mapping(documents.get(key), lidarr_groups)


def _mapping_payload(anime, aggregate=None):
    themes = anime.get("themes") or []
    # Resolver documents are deliberately independent of a user's library.
    # Overlay the current cached Lidarr snapshot at response time so matching
    # remains reusable while request buttons always reflect local ownership.
    lidarr_groups = lidarr.cached_library_availability()
    mapping_documents = (
        aggregate.get("mappings", {})
        if aggregate is not None
        else anime_metadata_worker.mappings_for(themes)
    )
    mappings_by_theme_id = {}
    for theme in themes:
        key = anime_musicbrainz.theme_mapping_key(theme)
        mapping = _public_mapping(mapping_documents.get(key), lidarr_groups)
        theme["mapping"] = mapping
        if theme.get("id") is not None:
            mappings_by_theme_id[str(theme["id"])] = mapping
    return mappings_by_theme_id


def _provider_error(exc, subject="Anime"):
    if isinstance(exc, ValueError):
        return api_error(str(exc))
    if isinstance(exc, LookupError):
        return api_error(str(exc), 404)
    if isinstance(exc, requests.HTTPError):
        status = getattr(exc.response, "status_code", None)
        if status == 404:
            return api_error(f"{subject} was not found on AnimeThemes.", 404)
    return api_error(f"AnimeThemes could not load this {subject.casefold()}.", 502)


@blueprint.get("/api/anime/<slug>")
@login_required
def anime_detail(slug):
    try:
        anime = _load_anime(slug)
        _mapping_payload(anime)
    except (ValueError, LookupError, requests.RequestException) as exc:
        return _provider_error(exc)
    return jsonify(anime)


@blueprint.get("/api/series/<slug>")
@login_required
def series_detail(slug):
    try:
        series = _load_series(slug)
    except (ValueError, LookupError, requests.RequestException) as exc:
        return _provider_error(exc, "Series")
    return jsonify(series)


@blueprint.put("/api/anime/<slug>/themes/<int:theme_id>/mapping")
@admin_required
def link_anime_theme_mapping(slug, theme_id):
    """Create or correct a verified local song-to-release-group mapping."""
    try:
        anime = _load_anime(slug)
        theme = _theme_by_id(anime, theme_id)
        song_id = _theme_song_id(theme)
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return api_error("A JSON request body is required.")
        song = theme.get("song") or {}
        artists = _artist_names_for_theme(theme)
        if body.get("confirmAutomatic") is True:
            mbid, target = _automatic_confirmation_target(
                theme,
                body.get("releaseGroup") or body.get("releaseGroupMbid"),
            )
            provenance = "manual-confirmation"
            message = "Recommended automatic match confirmed."
        else:
            mbid = _release_group_mbid(
                body.get("releaseGroup") or body.get("releaseGroupMbid")
            )
            group = _musicbrainz_release_group(mbid)
            target = _registry_target(group)
            provenance = "manual"
            message = "Anime theme mapping saved."
        anime_mapping_registry.upsert_mapping(
            song_id,
            title=str(song.get("title") or "Untitled song"),
            artists=artists or ["Unknown artist"],
            status="confirmed",
            provenance=provenance,
            scope="unknown",
            targets=[target],
            preferred_release_group_mbid=mbid,
        )
        public_mapping = _public_mapping(
            anime_musicbrainz.registered_mapping(theme),
            lidarr.cached_library_availability(),
        )
    except ValueError as exc:
        return api_error(str(exc))
    except AutomaticMatchUnavailable as exc:
        return api_error(str(exc), 409)
    except LookupError as exc:
        return api_error(str(exc), 404)
    except requests.RequestException:
        return api_error("MusicBrainz could not verify that release group.", 502)
    return jsonify({
        "mapping": public_mapping,
        "message": message,
    })


@blueprint.delete("/api/anime/<slug>/themes/<int:theme_id>/mapping")
@admin_required
def unlink_anime_theme_mapping(slug, theme_id):
    """Remove a local mapping and expose any cache-backed automatic result."""
    try:
        anime = _load_anime(slug)
        theme = _theme_by_id(anime, theme_id)
        song_id = _theme_song_id(theme)
        existing = anime_mapping_registry.get_mapping(song_id)
        if not existing:
            return api_error("This theme does not have a local mapping.", 404)
        if existing.get("provenance") == "builtin-seed":
            song = theme.get("song") or {}
            artists = [
                str(artist.get("as") or artist.get("name") or "").strip()
                for artist in song.get("artists") or []
                if isinstance(artist, dict)
                and str(artist.get("as") or artist.get("name") or "").strip()
            ]
            anime_mapping_registry.reject_mapping(
                song_id,
                title=str(song.get("title") or "Untitled song"),
                artists=artists or ["Unknown artist"],
                provenance="manual-rejection",
            )
            message = "Built-in anime theme mapping suppressed locally."
        else:
            anime_mapping_registry.delete_mapping(song_id)
            message = "Local anime theme mapping removed."
        mapping = _automatic_theme_mapping(
            theme,
            lidarr.cached_library_availability(),
        )
    except ValueError as exc:
        return api_error(str(exc))
    except LookupError as exc:
        return api_error(str(exc), 404)
    except requests.RequestException as exc:
        return _provider_error(exc)
    return jsonify({
        "mapping": mapping,
        "message": message,
    })


@blueprint.post("/api/anime/<slug>/resolve")
@login_required
def resolve_anime_themes(slug):
    """Queue uncached song mappings without holding the web request open."""
    try:
        anime = _load_anime(slug)
    except (ValueError, LookupError, requests.RequestException) as exc:
        return _provider_error(exc)
    aggregate = anime_metadata_worker.request_resolution(slug, anime["themes"])
    aggregate["mappings"] = _mapping_payload(anime, aggregate)
    return jsonify(aggregate), 202 if aggregate.get("polling") else 200


@blueprint.get("/api/anime/<slug>/resolution")
@login_required
def anime_theme_resolution(slug):
    """Return cache-backed progress updates for an open anime page."""
    try:
        anime = _load_anime(slug)
    except (ValueError, LookupError, requests.RequestException) as exc:
        return _provider_error(exc)
    aggregate = anime_metadata_worker.status(slug, anime["themes"])
    aggregate["mappings"] = _mapping_payload(anime, aggregate)
    return jsonify(aggregate)
