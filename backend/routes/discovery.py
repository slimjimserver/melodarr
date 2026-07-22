"""Discovery, recommendations, charts, and search routes."""

import json

import requests
from flask import Blueprint, jsonify, request

if __package__ == "backend.routes":
    from .. import recommendations as recommendation_engine
    from ..media_urls import artist_cover_art, release_group_cover_art
    from ..responses import api_error
    from ..security import current_user, login_required
    from ..services import lastfm, musicbrainz
    from ..storage import get_recommendation_cache
else:  # Support the existing `python backend/app.py` entry point.
    import recommendations as recommendation_engine
    from media_urls import artist_cover_art, release_group_cover_art
    from responses import api_error
    from security import current_user, login_required
    from services import lastfm, musicbrainz
    from storage import get_recommendation_cache


blueprint = Blueprint("discovery", __name__)


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
    if search_type not in {"artist", "album"}:
        return api_error("Search type must be artist or album.")
    try:
        response = musicbrainz.search(query, search_type)
    except requests.RequestException:
        return api_error("MusicBrainz could not be reached. Try again shortly.", 502)

    if search_type == "artist":
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
            }
            for artist in response.get("artists", [])
        ]
    else:
        results = [
            {
                "id": album["id"],
                "name": album.get("title", "Untitled release"),
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
    return jsonify({"results": results, "type": search_type})
