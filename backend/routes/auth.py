"""Account registration and session routes."""

import hashlib
import re
import sqlite3
import time

from flask import Blueprint, jsonify, request, session
from werkzeug.security import check_password_hash, generate_password_hash

if __package__ == "backend.routes":
    from ..responses import api_error
    from ..security import current_user, login_required, start_session, user_payload
    from ..storage import db
else:  # Support the existing `python backend/app.py` entry point.
    from responses import api_error
    from security import current_user, login_required, start_session, user_payload
    from storage import db


blueprint = Blueprint("auth", __name__)


def _invitation_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@blueprint.get("/api/auth/status")
def auth_status():
    token = str(request.args.get("invite", ""))[:512]
    with db() as connection:
        user_count = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        invitation_valid = False
        if user_count and token:
            invitation_valid = connection.execute(
                "SELECT 1 FROM account_invitations WHERE token_hash = ? "
                "AND used_at IS NULL AND expires_at > ?",
                (_invitation_hash(token), time.time()),
            ).fetchone() is not None
    return jsonify({
        "firstAccount": user_count == 0,
        "invitationValid": invitation_valid,
    })


@blueprint.post("/api/auth/register")
def register():
    values = request.get_json(silent=True) or {}
    username = str(values.get("username", "")).strip()
    password = str(values.get("password", ""))
    invitation_token = str(values.get("invitationToken", ""))[:512]
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,32}", username):
        return api_error("Username must be 3–32 characters using letters, numbers, dots, underscores, or hyphens.")
    if len(password) < 12:
        return api_error("Password must be at least 12 characters.")
    try:
        with db() as connection:
            # Serialize first-account creation so two concurrent sign-ups
            # cannot both receive the administrator role.
            connection.execute("BEGIN IMMEDIATE")
            first_account = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
            invitation = None
            if not first_account:
                if not invitation_token:
                    return api_error("A valid invitation link is required to create an account.", 403)
                invitation = connection.execute(
                    "SELECT id FROM account_invitations WHERE token_hash = ? "
                    "AND used_at IS NULL AND expires_at > ?",
                    (_invitation_hash(invitation_token), time.time()),
                ).fetchone()
                if not invitation:
                    return api_error("This invitation link is invalid, expired, or already used.", 403)
            connection.execute(
                "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
                (username, generate_password_hash(password), "admin" if first_account else "user", time.time()),
            )
            if invitation:
                connection.execute(
                    "UPDATE account_invitations SET used_at = ? WHERE id = ?",
                    (time.time(), invitation["id"]),
                )
            user = connection.execute(
                "SELECT id, username, password_hash, role, listenbrainz_username, "
                "lastfm_username, lastfm_api_key FROM users WHERE username = ?",
                (username,),
            ).fetchone()
    except sqlite3.IntegrityError:
        return api_error("That username is already registered.", 409)
    except sqlite3.OperationalError:
        return api_error("Could not create the account right now. Try again shortly.", 503)
    return start_session(user), 201


@blueprint.post("/api/auth/login")
def login():
    values = request.get_json(silent=True) or {}
    username = str(values.get("username", "")).strip()
    password = str(values.get("password", ""))
    with db() as connection:
        user = connection.execute(
            "SELECT id, username, password_hash, role, listenbrainz_username, "
            "lastfm_username, lastfm_api_key FROM users WHERE username = ?",
            (username,),
        ).fetchone()
    if not user or not check_password_hash(user["password_hash"], password):
        return api_error("Invalid username or password.", 401)
    remember = values.get("remember") is True or str(values.get("remember", "")).lower() in {
        "1", "true", "on",
    }
    return start_session(user, remember=remember)


@blueprint.get("/api/auth/me")
def auth_me():
    user = current_user()
    if not user:
        return api_error("Sign in is required.", 401)
    return jsonify(user_payload(user, include_csrf=True))


@blueprint.post("/api/auth/logout")
@login_required
def logout():
    session.clear()
    return jsonify({"message": "Signed out."})
