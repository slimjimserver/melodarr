"""Session lookup, authorization decorators, and CSRF enforcement."""

import os
from functools import wraps
from hmac import compare_digest

from flask import jsonify, request, session

if __package__:
    from .responses import api_error
    from .storage import db
else:  # Support the existing `python backend/app.py` entry point.
    from responses import api_error
    from storage import db


def get_user(user_id):
    with db() as connection:
        return connection.execute(
            "SELECT id, username, password_hash, role, listenbrainz_username, "
            "lastfm_username, lastfm_api_key FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()


def current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    user = get_user(user_id)
    if not user:
        session.clear()
    return user


def user_payload(user, include_csrf=False):
    payload = {
        "username": user["username"],
        "role": user["role"],
        "listenbrainzUsername": user["listenbrainz_username"] or "",
        "lastfmUsername": user["lastfm_username"] or "",
        "lastfmConfigured": bool(user["lastfm_username"] and user["lastfm_api_key"]),
    }
    if include_csrf:
        payload["csrfToken"] = session["csrf_token"]
    return payload


def start_session(user, remember=False):
    session.clear()
    session.permanent = remember
    session["user_id"] = user["id"]
    session["csrf_token"] = os.urandom(32).hex()
    return jsonify(user_payload(user, include_csrf=True))


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user():
            return api_error("Sign in is required.", 401)
        return view(*args, **kwargs)

    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if not user:
            return api_error("Sign in is required.", 401)
        if user["role"] != "admin":
            return api_error("Administrator access is required.", 403)
        return view(*args, **kwargs)

    return wrapped


def verify_csrf_token():
    """Reject state-changing API requests without the session's CSRF token."""
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"} or not request.path.startswith("/api/"):
        return None
    if request.path in {"/api/auth/login", "/api/auth/register"}:
        return None
    expected_token = session.get("csrf_token", "")
    received_token = request.headers.get("X-CSRF-Token", "")
    if not expected_token or not compare_digest(expected_token, received_token):
        return api_error("Invalid or missing CSRF token.", 403)
    return None
