"""Session lookup, authorization decorators, and CSRF enforcement."""

import os
from functools import wraps
from hmac import compare_digest

from flask import g, has_request_context, jsonify, request, session

if __package__:
    from .responses import api_error
    from .storage import db, get_lastfm_api_key
else:  # Support the existing `python backend/app.py` entry point.
    from responses import api_error
    from storage import db, get_lastfm_api_key


def get_user(user_id):
    with db() as connection:
        return connection.execute(
            "SELECT id, username, password_hash, role, listenbrainz_username, "
            "lastfm_username, plex_id, plex_username, "
            "plex_email, plex_avatar FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()


_MISSING = object()
_REQUEST_USER_KEY = "_melodarr_current_user"


def current_user():
    """Return the signed-in user, querying SQLite at most once per request."""
    user_id = session.get("user_id")
    if not user_id:
        return None
    if has_request_context():
        cached = getattr(g, _REQUEST_USER_KEY, _MISSING)
        if cached is not _MISSING and cached[0] == user_id:
            return cached[1]
    user = get_user(user_id)
    if not user:
        session.clear()
    if has_request_context():
        setattr(g, _REQUEST_USER_KEY, (user_id, user))
    return user


def resolve_account_user(signed_in_user, requested_username=None):
    """Resolve a username-scoped account without allowing user enumeration."""
    if requested_username is None:
        return signed_in_user, None

    requested_username = str(requested_username).strip()
    if not requested_username:
        return None, api_error("User not found.", 404)
    own_names = {
        str(signed_in_user["username"] or "").casefold(),
        str(signed_in_user["plex_username"] or "").casefold(),
    }
    if requested_username.casefold() in own_names:
        return signed_in_user, None
    if signed_in_user["role"] != "admin":
        return None, api_error(
            "Administrator access is required to access another user's account.",
            403,
        )

    with db() as connection:
        user = connection.execute(
            """
            SELECT
                id,
                username,
                role,
                listenbrainz_username,
                lastfm_username,
                plex_id,
                plex_username,
                plex_email,
                plex_avatar
            FROM users
            WHERE username = ? COLLATE NOCASE
                OR plex_username = ? COLLATE NOCASE
            ORDER BY
                CASE WHEN username = ? COLLATE NOCASE THEN 0 ELSE 1 END,
                id
            LIMIT 1
            """,
            (
                requested_username,
                requested_username,
                requested_username,
            ),
        ).fetchone()
    if not user:
        return None, api_error("User not found.", 404)
    return user, None


def user_payload(user, include_csrf=False):
    payload = {
        "id": user["id"],
        "username": user["username"],
        "role": user["role"],
        "authProvider": "plex" if user["plex_id"] else "local",
        "plexLinked": bool(user["plex_id"]),
        "plexUsername": user["plex_username"] or "",
        "plexEmail": user["plex_email"] or "",
        "listenbrainzUsername": user["listenbrainz_username"] or "",
        "lastfmUsername": user["lastfm_username"] or "",
        "lastfmConfigured": bool(
            user["lastfm_username"] and get_lastfm_api_key()
        ),
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
    if request.path in {"/api/auth/login", "/api/auth/register"} or request.path.startswith(
        "/api/auth/plex/"
    ):
        return None
    expected_token = session.get("csrf_token", "")
    received_token = request.headers.get("X-CSRF-Token", "")
    if not expected_token or not compare_digest(expected_token, received_token):
        return api_error("Invalid or missing CSRF token.", 403)
    return None
