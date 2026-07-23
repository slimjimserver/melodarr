"""Discovery, recommendations, charts, and search routes."""

import json
import logging

import requests
from flask import Blueprint, jsonify, request

if __package__ == "backend.routes":
    from .. import recommendations as recommendation_engine
    from ..media_urls import artist_cover_art, release_group_cover_art
    from ..responses import api_error
    from ..security import current_user, login_required
    from ..services import lastfm, musicbrainz, plex
    from ..storage import get_recommendation_cache, get_service
else:  # Support the existing `python backend/app.py` entry point.
    import recommendations as recommendation_engine
    from media_urls import artist_cover_art, release_group_cover_art
    from responses import api_error
    from security import current_user, login_required
    from services import lastfm, musicbrainz, plex
    from storage import get_recommendation_cache, get_service


blueprint = Blueprint("discovery", __name__)
logger = logging.getLogger(__name__)

_SEARCH_RESULT_LIMIT = 25
_PRIMARY_RELEASE_TYPE_RANK = {
    "single": 0,
    "album": 1,
    "ep": 2,
    "broadcast": 3,
    "other": 4,
}


def _plex_search_artists():
    """Use the same MBID-indexed Plex records as artist detail pages."""
    config = get_service("plex")
    if not config:
        return {}
    try:
        return plex.cached_library_index(config)["artistsByMbid"]
    except (ValueError, requests.RequestException):
        return {}


def _plex_search_link(artist):
    if not artist:
        return None
    return {
        "url": artist.get("url", ""),
        "plexampUrl": artist.get("plexampUrl", ""),
        "plexGuid": artist.get("plexGuid", ""),
        "guids": artist.get("guids", []),
        "key": artist.get("key", ""),
    }


def _artist_credit_name(entity):
    """Return a readable MusicBrainz artist credit from a search entity."""
    names = [
        str(
            credit.get("name")
            or (credit.get("artist") or {}).get("name")
            or ""
        ).strip()
        for credit in entity.get("artist-credit") or []
    ]
    return " · ".join(name for name in names if name)


def _recording_score(recording):
    try:
        return int(recording.get("score") or 0)
    except (TypeError, ValueError):
        return 0


def _track_release_rank(recording, release, recording_position, release_position):
    """Prefer strong recording matches, then original official editions."""
    group = release.get("release-group") or {}
    secondary_types = [
        str(name).casefold() for name in group.get("secondary-types") or []
    ]
    if "compilation" in secondary_types:
        secondary_type_rank = 2
    elif secondary_types:
        secondary_type_rank = 1
    else:
        secondary_type_rank = 0

    status = str(release.get("status") or "").casefold()
    status_rank = 0 if status == "official" else (1 if not status else 2)
    primary_type = str(group.get("primary-type") or "other").casefold()
    return (
        -_recording_score(recording),
        status_rank,
        secondary_type_rank,
        _PRIMARY_RELEASE_TYPE_RANK.get(primary_type, 5),
        release.get("date") or recording.get("first-release-date") or "9999",
        recording_position,
        release_position,
    )


def _recording_release_group_candidates(response):
    """Flatten recording releases and retain the best edition per release group."""
    candidates = {}
    for recording_position, recording in enumerate(response.get("recordings") or []):
        for release_position, release in enumerate(recording.get("releases") or []):
            group = release.get("release-group") or {}
            group_id = str(group.get("id") or "").strip()
            if not group_id:
                continue

            rank = _track_release_rank(
                recording, release, recording_position, release_position
            )
            existing = candidates.get(group_id)
            if existing and existing["rank"] <= rank:
                continue

            title = str(
                group.get("title")
                or release.get("title")
                or recording.get("title")
                or "Untitled release"
            ).strip()
            candidates[group_id] = {
                "id": group_id,
                "name": title,
                "artist": (
                    _artist_credit_name(group)
                    or _artist_credit_name(release)
                    or _artist_credit_name(recording)
                ),
                "date": (
                    release.get("date")
                    or recording.get("first-release-date")
                    or ""
                ),
                "type": group.get("primary-type") or "Other",
                "secondaryTypes": [
                    name for name in group.get("secondary-types") or [] if name
                ],
                "disambiguation": "",
                "score": _recording_score(recording),
                "matchedTrack": recording.get("title") or "Untitled track",
                "matchedTrackArtist": _artist_credit_name(recording),
                "rank": rank,
            }

    return sorted(candidates.values(), key=lambda item: item["rank"])[
        :_SEARCH_RESULT_LIMIT
    ]


def _recording_release_group_results(response):
    """Return canonical, ranked release groups reached through recording matches."""
    candidates = _recording_release_group_candidates(response)
    canonical_groups = {}
    if candidates:
        query = " OR ".join(f"rgid:{candidate['id']}" for candidate in candidates)
        try:
            group_response = musicbrainz.search(query, "album")
            canonical_groups = {
                group["id"]: group
                for group in group_response.get("release-groups") or []
                if group.get("id")
            }
        except requests.RequestException as exc:
            # The recording response still contains enough release metadata to
            # offer a useful result if canonical enrichment is temporarily down.
            logger.warning(
                "Could not enrich track search release groups: %s", exc
            )

    results = []
    for candidate in candidates:
        group = canonical_groups.get(candidate["id"], {})
        title = group.get("title") or candidate["name"]
        results.append({
            "id": candidate["id"],
            "name": title,
            "romanizedTitle": musicbrainz.romanized_release_group_title(
                group or {"title": title}
            ),
            "artist": _artist_credit_name(group) or candidate["artist"],
            "date": group.get("first-release-date") or candidate["date"],
            "type": group.get("primary-type") or candidate["type"],
            "secondaryTypes": [
                name
                for name in (
                    group.get("secondary-types")
                    if group.get("secondary-types") is not None
                    else candidate["secondaryTypes"]
                )
                if name
            ],
            "disambiguation": (
                group.get("disambiguation") or candidate["disambiguation"]
            ),
            "score": candidate["score"],
            "matchedTrack": candidate["matchedTrack"],
            "matchedTrackArtist": candidate["matchedTrackArtist"],
        })
    return results


@blueprint.get("/api/recommendations")
@login_required
def recommendations():
    user = current_user()
    username = user["listenbrainz_username"]
    if not username:
        return api_error(
            "Add your ListenBrainz username in the account menu to get recommendations.",
            503,
        )
    try:
        artists, albums = recommendation_engine.listenbrainz_recommendations(username)
        return jsonify({"username": username, "artists": artists, "albums": albums})
    except requests.RequestException:
        return api_error(
            "ListenBrainz recommendations could not be loaded. Try again shortly.",
            502,
        )


@blueprint.get("/api/recommendations/lastfm")
@login_required
def lastfm_recommendations():
    user = current_user()
    if not user["lastfm_username"] or not user["lastfm_api_key"]:
        return api_error(
            "Add your Last.fm username and API key in Linked accounts to get recommendations.",
            503,
        )
    try:
        artists, albums = recommendation_engine.lastfm_recommendations(
            user["lastfm_username"], user["lastfm_api_key"]
        )
        return jsonify({
            "username": user["lastfm_username"],
            "artists": artists,
            "albums": albums,
        })
    except ValueError as exc:
        return api_error(str(exc), 502)
    except requests.RequestException:
        return api_error(
            "Last.fm recommendations could not be loaded. Try again shortly.",
            502,
        )


@blueprint.get("/api/charts/lastfm")
@login_required
def lastfm_charts():
    user = current_user()
    if not user["lastfm_api_key"]:
        return api_error(
            "Add a Last.fm API key in Linked accounts to load Last.fm charts.", 503
        )
    try:
        artists_data = lastfm.get(
            "chart.gettopartists",
            user["lastfm_username"] or "melodarr",
            user["lastfm_api_key"],
            limit=20,
        )
        artists = [
            {
                "id": artist.get("mbid"),
                "name": artist.get("name"),
                "type": "Last.fm global chart",
                "coverArt": artist_cover_art(artist["mbid"]),
                "url": artist.get("url", ""),
            }
            for artist in artists_data.get("artists", {}).get("artist", [])
            if artist.get("mbid") and artist.get("name")
        ]
        return jsonify({"artists": artists})
    except (ValueError, requests.RequestException):
        return api_error("Last.fm charts could not be loaded. Try again shortly.", 502)


@blueprint.get("/api/charts/lastfm/tags")
@login_required
def lastfm_tags():
    user = current_user()
    if not user["lastfm_username"] or not user["lastfm_api_key"]:
        return api_error(
            "Add a Last.fm username and API key in Linked accounts to load tag charts.",
            503,
        )
    try:
        tags = recommendation_engine.lastfm_top_tags(
            user["lastfm_username"], user["lastfm_api_key"], limit=10
        )
        return jsonify({"tags": [tag["name"] for tag in tags[:10] if tag.get("name")]})
    except (ValueError, requests.RequestException):
        return api_error("Last.fm tag charts could not be loaded. Try again shortly.", 502)


@blueprint.get("/api/charts/lastfm/tag-albums")
@login_required
def lastfm_tag_albums():
    user = current_user()
    tag_name = request.args.get("tag", "").strip()
    if not user["lastfm_username"] or not user["lastfm_api_key"]:
        return api_error(
            "Add a Last.fm username and API key in Linked accounts to load tag charts.",
            503,
        )
    if not tag_name or len(tag_name) > 100:
        return api_error("A valid Last.fm tag is required.")
    try:
        albums = lastfm.get(
            "tag.gettopalbums",
            user["lastfm_username"],
            user["lastfm_api_key"],
            tag=tag_name,
            limit=10,
        ).get("albums", {}).get("album", [])
        mapped = []
        for album in albums:
            mbid = recommendation_engine.lastfm_album_mbid(
                album, user["lastfm_username"], user["lastfm_api_key"]
            )
            if mbid and album.get("name"):
                mapped.append({
                    "id": mbid,
                    "name": album["name"],
                    "artist": (album.get("artist") or {}).get("name", ""),
                    "type": f"Top {tag_name} album",
                    "date": "",
                    "coverArt": release_group_cover_art(mbid),
                })
        return jsonify({"tag": tag_name, "albums": mapped})
    except (ValueError, requests.RequestException):
        return api_error(f"Last.fm albums for {tag_name} could not be loaded.", 502)


@blueprint.get("/api/discover")
@login_required
def cached_discover():
    row = get_recommendation_cache(current_user()["id"])
    if not row:
        return jsonify({
            "pending": True,
            "artists": [],
            "albums": [],
            "chartArtists": [],
            "tagRows": [],
        })
    return jsonify({
        "pending": False,
        "refreshedAt": row["refreshed_at"],
        **json.loads(row["value"]),
    })


@blueprint.get("/api/search")
@login_required
def search():
    query = request.args.get("q", "").strip()
    search_type = request.args.get("type", "artist")
    if len(query) < 2:
        return api_error("Enter at least two characters.")
    if search_type not in {"artist", "album", "track"}:
        return api_error("Search type must be artist, album, or track.")
    try:
        response = musicbrainz.search(query, search_type, plain_search=True)
    except requests.RequestException:
        return api_error("MusicBrainz could not be reached. Try again shortly.", 502)

    if search_type == "artist":
        plex_artists = _plex_search_artists()
        results = [
            {
                "id": artist["id"],
                "name": artist.get("name", "Unknown artist"),
                "romanizedName": musicbrainz.romanized_artist_name(artist),
                "disambiguation": artist.get("disambiguation", ""),
                "country": artist.get("country", ""),
                "type": artist.get("type", ""),
                "score": artist.get("score", 0),
                "coverArt": artist_cover_art(artist["id"]),
                "plex": _plex_search_link(plex_artists.get(artist["id"])),
            }
            for artist in response.get("artists", [])
        ]
    elif search_type == "album":
        results = [
            {
                "id": album["id"],
                "name": album.get("title", "Untitled release"),
                "romanizedTitle": musicbrainz.romanized_release_group_title(album),
                "artist": " · ".join(
                    credit.get("name", "") for credit in album.get("artist-credit", [])
                ),
                "date": album.get("first-release-date", ""),
                "type": album.get("primary-type", "Album"),
                "disambiguation": album.get("disambiguation", ""),
                "score": album.get("score", 0),
            }
            for album in response.get("release-groups", [])
        ]
    else:
        results = _recording_release_group_results(response)
    return jsonify({"results": results, "type": search_type})
