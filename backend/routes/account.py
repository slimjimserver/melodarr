"""Profile, general preferences, and linked-account routes."""

import hashlib
import re
import secrets
import sqlite3
import time
from urllib.parse import quote

import requests
from flask import Blueprint, current_app, jsonify, request
from werkzeug.security import generate_password_hash

if __package__ == "backend.routes":
    from ..responses import api_error, request_json_object
    from ..security import (
        admin_required,
        current_user,
        login_required,
        resolve_account_user,
    )
    from ..services import anime_theme_links, lastfm, listenbrainz, musicbrainz, plex
    from ..storage import (
        db,
        count_request_history,
        delete_recommendation_cache,
        get_lastfm_api_key,
        get_request_history,
        get_service,
    )
    from ..workers import recommendations as recommendation_worker
else:  # Support the existing `python backend/app.py` entry point.
    from responses import api_error, request_json_object
    from security import (
        admin_required,
        current_user,
        login_required,
        resolve_account_user,
    )
    from services import anime_theme_links, lastfm, listenbrainz, musicbrainz, plex
    from storage import (
        db,
        count_request_history,
        delete_recommendation_cache,
        get_lastfm_api_key,
        get_request_history,
        get_service,
    )
    from workers import recommendations as recommendation_worker


blueprint = Blueprint("account", __name__)
REQUESTS_PAGE_SIZE = 100
SQLITE_MAX_INTEGER = (2 ** 63) - 1


def _safe_error_label(error):
    """Describe an upstream failure without logging its prepared URL."""
    status = getattr(getattr(error, "response", None), "status_code", None)
    name = type(error).__name__
    return f"{name} HTTP {status}" if isinstance(status, int) else name


def _requested_page():
    raw_page = request.args.get("page", "1")
    try:
        page = int(raw_page)
    except (TypeError, ValueError):
        return None
    if (
        page < 1
        or page - 1 > SQLITE_MAX_INTEGER // REQUESTS_PAGE_SIZE
    ):
        return None
    return page


def _recommendation_inputs_changed(user_id):
    delete_recommendation_cache(user_id)
    recommendation_worker.request_refresh()


def _profile_plex_index():
    """Read current Plex availability without triggering a library scan."""
    config = get_service("plex")
    if not config:
        return {"artistsByMbid": {}, "releaseGroupsByMbid": {}}
    try:
        return plex.cached_library_index(config)
    except (KeyError, TypeError, ValueError, requests.RequestException):
        return {"artistsByMbid": {}, "releaseGroupsByMbid": {}}


def _cached_release_group_metadata(mbid):
    """Backfill legacy history rows only when detail metadata is already cached."""
    try:
        data = musicbrainz.get(
            f"/release-group/{quote(mbid)}",
            "aliases+artist-credits+url-rels",
            priority="prefetch",
            cache_only=True,
        )
    except (KeyError, TypeError, ValueError, requests.RequestException):
        return {}
    if not data:
        return {}
    artist_credit = data.get("artist-credit") or []
    return {
        "artist_name": " · ".join(
            str(credit.get("name") or "").strip()
            for credit in artist_credit
            if isinstance(credit, dict) and credit.get("name")
        ),
        "release_type": data.get("primary-type") or "",
        "release_date": data.get("first-release-date") or "",
    }


def _explicit_anime_history_link(item):
    if not item.get("anime_slug") or not item.get("theme_id"):
        return None
    return {
        "animeSlug": item["anime_slug"],
        "animeName": item.get("anime_name") or "Anime",
        "animePath": f"/anime/{item['anime_slug']}#theme-{item['theme_id']}",
        "themeId": item["theme_id"],
        "themeLabel": item.get("theme_label") or "Theme",
        "themeType": "",
        "sequence": None,
        "songId": item.get("song_id"),
        "songTitle": item.get("song_title") or "",
    }


def _profile_history_item(row, plex_index, anime_link_cache=None):
    item = dict(row)
    explicit_anime_link = _explicit_anime_history_link(item)
    if explicit_anime_link:
        anime_links = [explicit_anime_link]
    elif item["kind"] == "release-group":
        anime_link_cache = anime_link_cache if anime_link_cache is not None else {}
        if item["mbid"] not in anime_link_cache:
            anime_link_cache[item["mbid"]] = (
                anime_theme_links.links_for_release_group(item["mbid"])
            )
        anime_links = anime_link_cache[item["mbid"]]
    else:
        anime_links = []
    item["animeThemes"] = anime_links
    item["animePath"] = anime_links[0]["animePath"] if anime_links else ""
    plex_item = None
    if item["kind"] == "artist":
        plex_item = plex_index.get("artistsByMbid", {}).get(item["mbid"])
    else:
        plex_items = plex_index.get("releaseGroupsByMbid", {}).get(
            item["mbid"], []
        )
        plex_item = next(
            (entry for entry in plex_items if entry.get("url")),
            plex_items[0] if plex_items else None,
        )
        if not all(
            item.get(field)
            for field in ("artist_name", "release_type", "release_date")
        ):
            cached = _cached_release_group_metadata(item["mbid"])
            item["artist_name"] = (
                item.get("artist_name")
                or cached.get("artist_name")
                or (plex_item or {}).get("artistName")
                or ""
            )
            item["release_type"] = (
                item.get("release_type")
                or cached.get("release_type")
                or (plex_item or {}).get("releaseType")
                or ""
            )
            item["release_date"] = (
                item.get("release_date")
                or cached.get("release_date")
                or str((plex_item or {}).get("year") or "")
            )
    item.update({
        "availableInPlex": bool(plex_item),
        "plexUrl": (plex_item or {}).get("url") or "",
        "plexampUrl": (plex_item or {}).get("plexampUrl") or "",
    })
    return item


def _profile_user_payload(user):
    """Return profile identity without authentication or integration secrets."""
    is_plex_user = bool(user["plex_id"])
    return {
        "id": user["id"],
        "username": (
            user["plex_username"] or user["username"]
            if is_plex_user
            else user["username"]
        ),
        "localUsername": user["username"],
        "userType": "plex" if is_plex_user else "local",
        "role": user["role"],
        "plexUsername": user["plex_username"] or "",
        "plexEmail": user["plex_email"] or "",
        "plexAvatar": user["plex_avatar"] or "",
    }


@blueprint.post("/api/account/invitations")
@admin_required
def create_invitation():
    """Create a one-time account invitation without storing its bearer token."""
    token = secrets.token_urlsafe(32)
    now = time.time()
    expires_at = now + (7 * 24 * 60 * 60)
    with db() as connection:
        connection.execute(
            "INSERT INTO account_invitations "
            "(token_hash, created_by, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (
                hashlib.sha256(token.encode("utf-8")).hexdigest(),
                current_user()["id"],
                now,
                expires_at,
            ),
        )
    return jsonify({
        "path": f"/register?invite={token}",
        "expiresAt": expires_at,
    }), 201


@blueprint.get("/api/account/settings")
@login_required
def account_settings():
    user, error = resolve_account_user(
        current_user(),
        request.args.get("username"),
    )
    if error:
        return error
    return jsonify({
        "username": user["username"],
        "plexConfigured": bool(get_service("plex")),
        "plexLinked": bool(user["plex_id"]),
        "plexUsername": user["plex_username"] or "",
        "plexEmail": user["plex_email"] or "",
        "listenbrainzUsername": user["listenbrainz_username"] or "",
        "lastfmUsername": user["lastfm_username"] or "",
        "lastfmConfigured": bool(
            user["lastfm_username"] and get_lastfm_api_key()
        ),
    })


@blueprint.get("/api/account/profile")
@login_required
def account_profile():
    signed_in_user = current_user()
    user, error = resolve_account_user(
        signed_in_user,
        request.args.get("username"),
    )
    if error:
        return error
    page = _requested_page()
    if page is None:
        return api_error("Page must be a positive integer.")
    total = count_request_history(user["id"])
    history = {"artist": [], "release-group": []}
    plex_index = _profile_plex_index()
    anime_link_cache = {}
    for row in get_request_history(
        user["id"],
        limit=REQUESTS_PAGE_SIZE,
        offset=(page - 1) * REQUESTS_PAGE_SIZE,
    ):
        history[row["kind"]].append(
            _profile_history_item(row, plex_index, anime_link_cache)
        )
    return jsonify({
        "username": user["username"],
        "user": _profile_user_payload(user),
        "requests": history,
        "pagination": {
            "page": page,
            "pageSize": REQUESTS_PAGE_SIZE,
            "total": total,
            "totalPages": (
                (total + REQUESTS_PAGE_SIZE - 1) // REQUESTS_PAGE_SIZE
            ),
        },
    })


@blueprint.post("/api/account/general")
@login_required
def account_general():
    user, error = resolve_account_user(
        current_user(),
        request.args.get("username"),
    )
    if error:
        return error
    values = request_json_object()
    if values is None:
        return api_error("Request body must be a JSON object.")
    username = str(values.get("username", "")).strip()
    password = str(values.get("password", ""))
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,32}", username):
        return api_error("Username must be 3–32 characters using letters, numbers, dots, underscores, or hyphens.")
    if password and len(password) < 12:
        return api_error("Password must be at least 12 characters.")
    try:
        with db() as connection:
            if password:
                connection.execute(
                    "UPDATE users SET username = ?, password_hash = ? WHERE id = ?",
                    (username, generate_password_hash(password), user["id"]),
                )
            else:
                connection.execute(
                    "UPDATE users SET username = ? WHERE id = ?",
                    (username, user["id"]),
                )
        return jsonify({"message": "General settings saved.", "username": username})
    except sqlite3.IntegrityError:
        return api_error("That username is already registered.", 409)


@blueprint.post("/api/account/settings")
@login_required
def configure_listenbrainz():
    user, error = resolve_account_user(
        current_user(),
        request.args.get("username"),
    )
    if error:
        return error
    user_id = user["id"]
    values = request_json_object()
    if values is None:
        return api_error("Request body must be a JSON object.")
    username = str(values.get("username", "")).strip()
    if not username:
        with db() as connection:
            connection.execute(
                "UPDATE users SET listenbrainz_username = NULL WHERE id = ?",
                (user_id,),
            )
        _recommendation_inputs_changed(user_id)
        return jsonify({"message": "ListenBrainz account removed."})
    validation_deferred = False
    try:
        response = listenbrainz.user_listen_count(username)
        if response.status_code == 404:
            return api_error("That ListenBrainz user was not found.", 404)
        response.raise_for_status()
    except requests.RequestException as exc:
        # Linking a public username should not depend on ListenBrainz being
        # healthy at this exact moment. Its API can temporarily return rate
        # limits, gateway errors, or timeouts; recommendations will retry on
        # their next request and during the background refresh.
        validation_deferred = True
        current_app.logger.warning(
            "ListenBrainz username validation deferred for user id %s (%s)",
            user_id,
            _safe_error_label(exc),
        )

    with db() as connection:
        connection.execute(
            "UPDATE users SET listenbrainz_username = ? WHERE id = ?",
            (username, user_id),
        )
    _recommendation_inputs_changed(user_id)
    if validation_deferred:
        return jsonify({
            "message": (
                "ListenBrainz account saved. ListenBrainz could not validate it right now; "
                "recommendations will retry automatically."
            ),
            "validationDeferred": True,
        })
    return jsonify({"message": "ListenBrainz account saved.", "validationDeferred": False})


@blueprint.post("/api/account/lastfm")
@login_required
def configure_lastfm():
    user, error = resolve_account_user(
        current_user(),
        request.args.get("username"),
    )
    if error:
        return error
    values = request_json_object()
    if values is None:
        return api_error("Request body must be a JSON object.")
    username = str(values.get("username", "")).strip()
    previous_username = str(user["lastfm_username"] or "").strip()
    if not username:
        lastfm.clear_user_cache(previous_username)
        with db() as connection:
            connection.execute(
                "UPDATE users SET lastfm_username = NULL, lastfm_api_key = NULL WHERE id = ?",
                (user["id"],),
            )
        _recommendation_inputs_changed(user["id"])
        return jsonify({"message": "Last.fm account removed."})
    api_key = get_lastfm_api_key()
    if not api_key:
        return api_error(
            "Last.fm is not configured. Ask an administrator to add the API key.",
            503,
        )
    try:
        lastfm.get("user.getinfo", username, api_key)
        if username != previous_username:
            lastfm.clear_user_cache(previous_username)
        with db() as connection:
            connection.execute(
                "UPDATE users SET lastfm_username = ?, lastfm_api_key = NULL WHERE id = ?",
                (username, user["id"]),
            )
        _recommendation_inputs_changed(user["id"])
        return jsonify({"message": "Last.fm account saved."})
    except ValueError as exc:
        return api_error(str(exc), 400)
    except requests.RequestException:
        return api_error("Could not connect to Last.fm. Try again shortly.", 502)
