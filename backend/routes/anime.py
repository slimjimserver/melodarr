"""Anime discovery detail routes."""

from urllib.parse import urlparse
from uuid import UUID

import requests
from flask import Blueprint, jsonify, request

if __package__ == "backend.routes":
    from ..responses import api_error
    from ..security import admin_required, current_user, login_required
    from ..services import (
        anime_mapping_registry,
        anime_musicbrainz,
        anime_theme_links,
        animethemes,
        lidarr,
        musicbrainz,
        plex,
    )
    from ..storage import get_service
    from ..workers import anime_metadata as anime_metadata_worker
else:  # Support the existing `python backend/app.py` entry point.
    from responses import api_error
    from security import admin_required, current_user, login_required
    from services import (
        anime_mapping_registry,
        anime_musicbrainz,
        anime_theme_links,
        animethemes,
        lidarr,
        musicbrainz,
        plex,
    )
    from storage import get_service
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


def _plex_index():
    """Return cached Plex lookups without ever triggering a library scan."""
    config = get_service("plex")
    if not config:
        return {"releaseGroupsByMbid": {}}
    try:
        return plex.cached_library_index(config)
    except (KeyError, TypeError, ValueError, requests.RequestException):
        return {"releaseGroupsByMbid": {}}


def _plex_release_summary(item):
    return {
        "name": item.get("name"),
        "releaseType": item.get("releaseType"),
        "releaseId": item.get("musicbrainzReleaseId"),
        "url": item.get("url"),
        "plexampUrl": item.get("plexampUrl"),
    }


def _public_mapping(mapping, lidarr_groups=None, plex_groups=None):
    """Normalize cached resolver data to the browser's release-card shape."""
    result = dict(mapping or {})
    result["status"] = result.get("state", "pending")
    lidarr_groups = lidarr_groups or {}
    plex_groups = plex_groups or {}

    def public_groups(groups):
        normalized = {}
        for group in groups or []:
            if not isinstance(group, dict) or not group.get("id"):
                continue
            group_id = str(group["id"])
            availability = lidarr_groups.get(group_id)
            plex_releases = plex_groups.get(group_id) or []
            plex_item = next(
                (item for item in plex_releases if item.get("url")),
                plex_releases[0] if plex_releases else None,
            )
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
                "availableInPlex": bool(plex_releases),
                "plexUrl": (plex_item or {}).get("url") or "",
                "plexampUrl": (plex_item or {}).get("plexampUrl") or "",
                "plexReleases": [
                    _plex_release_summary(item) for item in plex_releases
                ],
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


def _automatic_candidate_groups(mapping):
    """Index safe cached candidate groups with their recording context."""
    candidates = {}

    def add_group(group, recording=None):
        if not isinstance(group, dict) or not group.get("id"):
            return
        try:
            group_id = _release_group_mbid(str(group["id"]))
        except ValueError:
            return
        entry = candidates.setdefault(group_id, {
            "group": group,
            "recordingIds": set(),
            "artistIds": set(),
        })
        if recording is None:
            return
        recording_id = str(recording.get("recordingId") or "").strip()
        if recording_id:
            entry["recordingIds"].add(recording_id)
        entry["artistIds"].update(
            str(value).strip()
            for value in recording.get("artistIds") or []
            if str(value).strip()
        )

    for group in mapping.get("releaseGroups") or []:
        add_group(group)
    for recording in mapping.get("recordingCandidates") or []:
        if not isinstance(recording, dict):
            continue
        for group in recording.get("releaseGroups") or []:
            add_group(group, recording)
    return candidates


def _automatic_confirmation_target(theme, requested_release_group=None):
    """Return a verified cached automatic target safe for local confirmation."""
    mapping = anime_musicbrainz.cached_mapping(theme)
    if not mapping:
        raise AutomaticMatchUnavailable(
            "The automatic match is no longer available. "
            "Run matching again before confirming it."
        )
    groups = [
        group for group in mapping.get("releaseGroups") or []
        if isinstance(group, dict) and group.get("id")
    ]
    state = str(mapping.get("state") or "").strip()
    reason = str(mapping.get("reason") or "").strip()
    match_method = str(mapping.get("matchMethod") or "").strip()
    recording_ids = []
    artist_ids = []
    confirmation_kind = "recommended"

    if state == "resolved":
        recording_id = str(mapping.get("recordingId") or "").strip()
        if (
            reason
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
        candidate = _automatic_candidate_groups(mapping).get(recommended_id)
        if candidate is None:
            raise AutomaticMatchUnavailable(
                "The recommended release group is no longer part of this match."
            )
        group = candidate["group"]
        recording_ids = [recording_id] if recording_id else []
        artist_ids = [
            str(value).strip()
            for value in mapping.get("artistIds") or []
            if str(value).strip()
        ]
    elif state == "ambiguous":
        if not requested_release_group:
            raise AutomaticMatchUnavailable(
                "Choose an automatic release-group candidate before confirming."
            )
        requested_id = _release_group_mbid(requested_release_group)
        candidate = _automatic_candidate_groups(mapping).get(requested_id)
        if candidate is None:
            raise AutomaticMatchUnavailable(
                "That release group is not part of the current automatic candidates."
            )
        group = candidate["group"]
        recording_ids = sorted(candidate["recordingIds"])
        artist_ids = sorted(candidate["artistIds"])
        if not artist_ids:
            artist_ids = [
                str(value).strip()
                for value in mapping.get("artistIds") or []
                if str(value).strip()
            ]
        confirmation_kind = "candidate"
    else:
        raise AutomaticMatchUnavailable(
            "The automatic match is no longer available. "
            "Run matching again before confirming it."
        )

    source_artists = _artist_names_for_theme(theme)
    return requested_id, {
        "releaseGroupId": requested_id,
        "recordingIds": recording_ids,
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
    }, confirmation_kind


def _artist_names_for_theme(theme):
    song = theme.get("song") if isinstance(theme, dict) else {}
    return [
        str(artist.get("as") or artist.get("name") or "").strip()
        for artist in (song or {}).get("artists") or []
        if isinstance(artist, dict)
        and str(artist.get("as") or artist.get("name") or "").strip()
    ]


def _automatic_theme_mapping(theme, lidarr_groups=None, plex_groups=None):
    key = anime_musicbrainz.theme_mapping_key(theme)
    documents = anime_metadata_worker.mappings_for([theme])
    return _public_mapping(documents.get(key), lidarr_groups, plex_groups)


def _mapping_payload(anime, aggregate=None):
    themes = anime.get("themes") or []
    # Resolver documents are deliberately independent of a user's library.
    # Overlay the current cached Lidarr snapshot at response time so matching
    # remains reusable while request buttons always reflect local ownership.
    lidarr_groups = lidarr.cached_library_availability()
    plex_groups = _plex_index().get("releaseGroupsByMbid", {})
    user = current_user()
    proposals = (
        anime_mapping_registry.mapping_proposals_for_anime(
            anime.get("slug") or "",
            submitter_user_id=user["id"] if user else None,
            include_all_pending=bool(user and user["role"] == "admin"),
        )
        if user
        else []
    )
    proposals_by_theme = {}
    own_proposals_by_theme = {}
    for proposal in proposals:
        theme_key = str(proposal["themeId"])
        if proposal["status"] == "pending":
            proposals_by_theme.setdefault(theme_key, []).append(proposal)
        if user and proposal["userId"] == user["id"]:
            own_proposals_by_theme.setdefault(theme_key, proposal)
    mapping_documents = (
        aggregate.get("mappings", {})
        if aggregate is not None
        else anime_metadata_worker.mappings_for(themes)
    )
    mappings_by_theme_id = {}
    for theme in themes:
        key = anime_musicbrainz.theme_mapping_key(theme)
        mapping = _public_mapping(
            mapping_documents.get(key),
            lidarr_groups,
            plex_groups,
        )
        theme_key = str(theme.get("id"))
        if user:
            mapping["myProposal"] = own_proposals_by_theme.get(theme_key)
            if user["role"] == "admin":
                mapping["proposals"] = proposals_by_theme.get(theme_key, [])
        anime_theme_links.sync_anime_theme_mapping(anime, theme, mapping)
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
            mbid, target, confirmation_kind = _automatic_confirmation_target(
                theme,
                body.get("releaseGroup") or body.get("releaseGroupMbid"),
            )
            provenance = "manual-confirmation"
            message = (
                "Automatic match candidate confirmed."
                if confirmation_kind == "candidate"
                else "Recommended automatic match confirmed."
            )
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
            _plex_index().get("releaseGroupsByMbid", {}),
        )
        anime_theme_links.sync_anime_theme_mapping(anime, theme, public_mapping)
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
            _plex_index().get("releaseGroupsByMbid", {}),
        )
        anime_theme_links.sync_anime_theme_mapping(anime, theme, mapping)
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
    body = request.get_json(silent=True)
    if body is not None and not isinstance(body, dict):
        return api_error("Request body must be a JSON object.")
    requested_theme_ids = None
    if body is not None and "themeIds" in body:
        raw_ids = body["themeIds"]
        if not isinstance(raw_ids, list):
            return api_error("themeIds must be a list of AnimeThemes theme IDs.")
        available_ids = {
            str(theme.get("id"))
            for theme in anime.get("themes") or []
            if theme.get("id") is not None
        }
        requested_theme_ids = []
        for value in raw_ids:
            if isinstance(value, bool) or not isinstance(value, (int, str)):
                return api_error("Each theme ID must be a positive integer.")
            normalized = str(value).strip()
            if not normalized.isdigit() or int(normalized) <= 0:
                return api_error("Each theme ID must be a positive integer.")
            normalized = str(int(normalized))
            if normalized not in available_ids:
                return api_error(f"Anime theme {normalized} was not found.")
            if normalized not in requested_theme_ids:
                requested_theme_ids.append(normalized)
    aggregate = anime_metadata_worker.request_resolution(
        slug,
        anime["themes"],
        requested_theme_ids=requested_theme_ids,
    )
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


@blueprint.post(
    "/api/anime/<slug>/themes/<int:theme_id>/mapping-proposals"
)
@login_required
def propose_anime_theme_mapping(slug, theme_id):
    """Submit a verified personal override for administrator review."""
    try:
        anime = _load_anime(slug)
        theme = _theme_by_id(anime, theme_id)
        song_id = _theme_song_id(theme)
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return api_error("A JSON request body is required.")
        mbid = _release_group_mbid(
            body.get("releaseGroup") or body.get("releaseGroupMbid")
        )
        group = _musicbrainz_release_group(mbid)
        song = theme.get("song") or {}
        proposal = anime_mapping_registry.submit_mapping_proposal(
            current_user()["id"],
            anime_slug=str(anime.get("slug") or slug),
            anime_name=str(anime.get("name") or "Untitled anime"),
            theme_id=theme_id,
            theme_label=str(theme.get("label") or "Theme"),
            song_id=song_id,
            song_title=str(song.get("title") or "Untitled song"),
            artists=_artist_names_for_theme(theme) or ["Unknown artist"],
            target=_registry_target(group),
        )
    except ValueError as exc:
        return api_error(str(exc))
    except LookupError as exc:
        return api_error(str(exc), 404)
    except requests.RequestException:
        return api_error("MusicBrainz could not verify that release group.", 502)
    return jsonify({
        "proposal": proposal,
        "message": "Override mapping submitted for administrator review.",
    }), 201


def _proposal_for_path(slug, theme_id, proposal_id):
    proposal = anime_mapping_registry.get_mapping_proposal(proposal_id)
    if proposal is None:
        raise LookupError("Mapping proposal was not found.")
    if proposal["animeSlug"] != slug or proposal["themeId"] != theme_id:
        raise LookupError("Mapping proposal was not found.")
    return proposal


def _proposal_theme(proposal):
    return {
        "id": proposal["themeId"],
        "label": proposal["themeLabel"],
        "song": {
            "id": proposal["songId"],
            "title": proposal["songTitle"],
            "artists": [{"name": artist} for artist in proposal["artists"]],
        },
    }


def _admin_proposal_state(slug, theme_id, mapping):
    """Attach the refreshed review queue to a public mapping response."""
    user_id = current_user()["id"]
    visible_proposals = anime_mapping_registry.mapping_proposals_for_anime(
        slug,
        submitter_user_id=user_id,
        include_all_pending=True,
    )
    mapping["proposals"] = [
        item
        for item in visible_proposals
        if item["themeId"] == theme_id and item["status"] == "pending"
    ]
    mapping["myProposal"] = next(
        (
            item
            for item in visible_proposals
            if item["themeId"] == theme_id and item["userId"] == user_id
        ),
        None,
    )
    return mapping


@blueprint.post(
    "/api/anime/<slug>/themes/<int:theme_id>/mapping-proposals/"
    "<int:proposal_id>/approve"
)
@admin_required
def approve_anime_theme_mapping_proposal(slug, theme_id, proposal_id):
    """Publish a proposal as the universal confirmed mapping atomically."""
    try:
        source_proposal = _proposal_for_path(slug, theme_id, proposal_id)
        proposal = anime_mapping_registry.approve_mapping_proposal(
            proposal_id,
            current_user()["id"],
        )
    except ValueError as exc:
        return api_error(str(exc), 409)
    except LookupError as exc:
        return api_error(str(exc), 404)
    mapping = _admin_proposal_state(
        slug,
        theme_id,
        _public_mapping(
            anime_musicbrainz.registered_mapping(
                _proposal_theme(source_proposal)
            ),
            lidarr.cached_library_availability(),
            _plex_index().get("releaseGroupsByMbid", {}),
        ),
    )
    anime_theme_links.sync_anime_theme_mapping(
        {
            "slug": source_proposal["animeSlug"],
            "name": source_proposal["animeName"],
        },
        _proposal_theme(source_proposal),
        mapping,
    )
    return jsonify({
        "proposal": proposal,
        "mapping": mapping,
        "proposals": mapping["proposals"],
        "myProposal": mapping["myProposal"],
        "message": "Override mapping approved for everyone.",
    })


@blueprint.delete(
    "/api/anime/<slug>/themes/<int:theme_id>/mapping-proposals/"
    "<int:proposal_id>"
)
@admin_required
def reject_anime_theme_mapping_proposal(slug, theme_id, proposal_id):
    """Reject a pending proposal without changing the universal mapping."""
    try:
        source_proposal = _proposal_for_path(slug, theme_id, proposal_id)
        proposal = anime_mapping_registry.reject_mapping_proposal(
            proposal_id,
            current_user()["id"],
        )
    except ValueError as exc:
        return api_error(str(exc), 409)
    except LookupError as exc:
        return api_error(str(exc), 404)
    theme = _proposal_theme(source_proposal)
    mapping = _admin_proposal_state(
        slug,
        theme_id,
        _automatic_theme_mapping(
            theme,
            lidarr.cached_library_availability(),
            _plex_index().get("releaseGroupsByMbid", {}),
        ),
    )
    anime_theme_links.sync_anime_theme_mapping(
        {
            "slug": source_proposal["animeSlug"],
            "name": source_proposal["animeName"],
        },
        theme,
        mapping,
    )
    return jsonify({
        "proposal": proposal,
        "mapping": mapping,
        "proposals": mapping["proposals"],
        "myProposal": mapping["myProposal"],
        "message": "Override mapping rejected.",
    })
