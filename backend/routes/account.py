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
    from ..responses import api_error
    from ..security import admin_required, current_user, login_required
    from ..services import lastfm, listenbrainz, musicbrainz, plex
    from ..storage import (
        db,
        delete_recommendation_cache,
        get_request_history,
        get_service,
    )
    from ..workers import recommendations as recommendation_worker
else:  # Support the existing `python backend/app.py` entry point.
    from responses import api_error
    from security import admin_required, current_user, login_required
    from services import lastfm, listenbrainz, musicbrainz, plex
    from storage import (
        db,
        delete_recommendation_cache,
        get_request_history,
        get_service,
    )
    from workers import recommendations as recommendation_worker


blueprint = Blueprint("account", __name__)


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


def _profile_history_item(row, plex_index):
    item = dict(row)
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
    user = current_user()
    return jsonify({
        "listenbrainzUsername": user["listenbrainz_username"] or "",
        "lastfmUsername": user["lastfm_username"] or "",
        "lastfmConfigured": bool(user["lastfm_username"] and user["lastfm_api_key"]),
    })


@blueprint.get("/api/account/profile")
@login_required
def account_profile():
    user = current_user()
    history = {"artist": [], "release-group": []}
    plex_index = _profile_plex_index()
    for row in get_request_history(user["id"]):
        history[row["kind"]].append(_profile_history_item(row, plex_index))
    return jsonify({"username": user["username"], "requests": history})


@blueprint.post("/api/account/general")
@login_required
def account_general():
    values = request.get_json(silent=True) or {}
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
                    (username, generate_password_hash(password), current_user()["id"]),
                )
            else:
                connection.execute(
                    "UPDATE users SET username = ? WHERE id = ?",
                    (username, current_user()["id"]),
                )
        return jsonify({"message": "General settings saved.", "username": username})
    except sqlite3.IntegrityError:
        return api_error("That username is already registered.", 409)


@blueprint.post("/api/account/settings")
@login_required
def configure_listenbrainz():
    values = request.get_json(silent=True) or {}
    username = str(values.get("username", "")).strip()
    if not username:
        user_id = current_user()["id"]
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
            "ListenBrainz username validation deferred for %s: %s",
            username,
            exc,
        )

    user_id = current_user()["id"]
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
    values = request.get_json(silent=True) or {}
    username = str(values.get("username", "")).strip()
    supplied_api_key = str(values.get("apiKey", "")).strip()
    user = current_user()
    if not username and not supplied_api_key:
        with db() as connection:
            connection.execute(
                "UPDATE users SET lastfm_username = NULL, lastfm_api_key = NULL WHERE id = ?",
                (user["id"],),
            )
        _recommendation_inputs_changed(user["id"])
        return jsonify({"message": "Last.fm account removed."})
    api_key = supplied_api_key or user["lastfm_api_key"] or ""
    if not username or not api_key:
        return api_error("Enter both a Last.fm username and API key.")
    try:
        lastfm.get("user.getinfo", username, api_key)
        with db() as connection:
            connection.execute(
                "UPDATE users SET lastfm_username = ?, lastfm_api_key = ? WHERE id = ?",
                (username, api_key, user["id"]),
            )
        _recommendation_inputs_changed(user["id"])
        return jsonify({"message": "Last.fm account saved."})
    except ValueError as exc:
        return api_error(str(exc), 400)
    except requests.RequestException:
        return api_error("Could not connect to Last.fm. Try again shortly.", 502)
