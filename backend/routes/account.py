"""Profile, general preferences, and linked-account routes."""

import hashlib
import re
import secrets
import sqlite3
import time

import requests
from flask import Blueprint, current_app, jsonify, request
from werkzeug.security import generate_password_hash

if __package__ == "backend.routes":
    from ..responses import api_error
    from ..security import admin_required, current_user, login_required
    from ..services import lastfm, listenbrainz
    from ..storage import db, delete_recommendation_cache, get_request_history
    from ..workers import recommendations as recommendation_worker
else:  # Support the existing `python backend/app.py` entry point.
    from responses import api_error
    from security import admin_required, current_user, login_required
    from services import lastfm, listenbrainz
    from storage import db, delete_recommendation_cache, get_request_history
    from workers import recommendations as recommendation_worker


blueprint = Blueprint("account", __name__)


def _recommendation_inputs_changed(user_id):
    delete_recommendation_cache(user_id)
    recommendation_worker.request_refresh()


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
    for row in get_request_history(user["id"]):
        history[row["kind"]].append(dict(row))
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
