"""Administrator-only user listing and account-management routes."""

import re
import sqlite3

from flask import Blueprint, jsonify, request
from werkzeug.security import generate_password_hash

if __package__ == "backend.routes":
    from .account import _profile_history_item, _profile_plex_index
    from ..responses import api_error
    from ..security import admin_required, current_user
    from ..storage import db, delete_recommendation_cache, get_lastfm_api_key
    from ..workers import recommendations as recommendation_worker
else:  # Support the existing `python backend/app.py` entry point.
    from routes.account import _profile_history_item, _profile_plex_index
    from responses import api_error
    from security import admin_required, current_user
    from storage import db, delete_recommendation_cache, get_lastfm_api_key
    from workers import recommendations as recommendation_worker


blueprint = Blueprint("admin", __name__)
USERNAME_PATTERN = re.compile(r"[A-Za-z0-9_.-]{3,32}")
REQUESTS_PAGE_SIZE = 100
EDITABLE_FIELDS = frozenset({
    "role",
    "localUsername",
    "password",
    "listenbrainzUsername",
    "lastfmUsername",
})
READ_ONLY_FIELDS = frozenset({
    "authProvider",
    "plexAvatar",
    "plexEmail",
    "plexId",
    "plexLinked",
    "plexUsername",
    "userType",
})
USER_LIST_QUERY = """
    SELECT
        users.id,
        users.username,
        users.role,
        users.plex_id,
        users.plex_username,
        users.plex_email,
        users.plex_avatar,
        users.listenbrainz_username,
        users.lastfm_username,
        users.created_at,
        COUNT(request_history.id) AS request_count
    FROM users
    LEFT JOIN request_history ON request_history.user_id = users.id
"""
REQUEST_HISTORY_FIELDS = (
    "kind",
    "mbid",
    "name",
    "artist_name",
    "release_type",
    "release_date",
    "created_at",
)


def _user_payload(row):
    """Return the admin-list representation without account secrets."""
    is_plex_user = bool(row["plex_id"])
    return {
        "id": row["id"],
        "username": (
            row["plex_username"] or row["username"]
            if is_plex_user
            else row["username"]
        ),
        "localUsername": row["username"],
        "requestCount": row["request_count"],
        "userType": "plex" if is_plex_user else "local",
        "role": row["role"],
        "joinedAt": row["created_at"],
        "plexUsername": row["plex_username"] or "",
        "plexEmail": row["plex_email"] or "",
        "plexAvatar": row["plex_avatar"] or "",
        "listenbrainzUsername": row["listenbrainz_username"] or "",
        "lastfmUsername": row["lastfm_username"] or "",
        "lastfmConfigured": bool(
            row["lastfm_username"] and get_lastfm_api_key()
        ),
    }


def _get_user(connection, user_id):
    return connection.execute(
        USER_LIST_QUERY
        + """
        WHERE users.id = ?
        GROUP BY users.id
        """,
        (user_id,),
    ).fetchone()


def _requester_payload(row):
    """Return the public account identity associated with an admin request."""
    is_plex_user = bool(row["plex_id"])
    return {
        "id": row["user_id"],
        "username": (
            row["plex_username"] or row["local_username"]
            if is_plex_user
            else row["local_username"]
        ),
        "localUsername": row["local_username"],
        "userType": "plex" if is_plex_user else "local",
        "role": row["role"],
        "plexUsername": row["plex_username"] or "",
        "plexEmail": row["plex_email"] or "",
        "plexAvatar": row["plex_avatar"] or "",
    }


@blueprint.get("/api/admin/users")
@admin_required
def users():
    with db() as connection:
        rows = connection.execute(
            USER_LIST_QUERY
            + """
            GROUP BY users.id
            ORDER BY users.created_at, users.id
            """
        ).fetchall()
    return jsonify({"users": [_user_payload(row) for row in rows]})


@blueprint.get("/api/admin/requests")
@admin_required
def requests():
    """Return one page of every user's request history to an administrator."""
    raw_page = request.args.get("page", "1")
    try:
        page = int(raw_page)
    except (TypeError, ValueError):
        return api_error("Page must be a positive integer.")
    if page < 1:
        return api_error("Page must be a positive integer.")

    with db() as connection:
        total = connection.execute(
            "SELECT COUNT(*) AS total FROM request_history"
        ).fetchone()["total"]
        rows = connection.execute(
            """
            SELECT
                request_history.id AS request_id,
                request_history.user_id,
                request_history.kind,
                request_history.mbid,
                request_history.name,
                request_history.artist_name,
                request_history.release_type,
                request_history.release_date,
                request_history.created_at,
                users.username AS local_username,
                users.role,
                users.plex_id,
                users.plex_username,
                users.plex_email,
                users.plex_avatar
            FROM request_history
            JOIN users ON users.id = request_history.user_id
            ORDER BY request_history.created_at DESC, request_history.id DESC
            LIMIT ? OFFSET ?
            """,
            (REQUESTS_PAGE_SIZE, (page - 1) * REQUESTS_PAGE_SIZE),
        ).fetchall()

    plex_index = _profile_plex_index()
    payload = []
    for row in rows:
        history_item = _profile_history_item(
            {
                "id": row["request_id"],
                **{field: row[field] for field in REQUEST_HISTORY_FIELDS},
            },
            plex_index,
        )
        history_item["requester"] = _requester_payload(row)
        payload.append(history_item)
    return jsonify({
        "requests": payload,
        "pagination": {
            "page": page,
            "pageSize": REQUESTS_PAGE_SIZE,
            "total": total,
            "totalPages": (
                (total + REQUESTS_PAGE_SIZE - 1) // REQUESTS_PAGE_SIZE
            ),
        },
    })


@blueprint.patch("/api/admin/users/<int:user_id>")
@admin_required
def update_user(user_id):
    values = request.get_json(silent=True)
    if not isinstance(values, dict):
        return api_error("Enter the user settings to save.")

    read_only_fields = sorted(READ_ONLY_FIELDS.intersection(values))
    if read_only_fields:
        return api_error(
            f"{read_only_fields[0]} is managed by Plex and cannot be changed."
        )
    unknown_fields = sorted(set(values).difference(EDITABLE_FIELDS))
    if unknown_fields:
        return api_error(f"Unknown user setting: {unknown_fields[0]}.")

    role = values.get("role")
    if role is not None and role not in {"admin", "user"}:
        return api_error("Role must be either admin or user.")

    local_username = values.get("localUsername")
    if local_username is not None:
        if not isinstance(local_username, str):
            return api_error("Local username must be text.")
        local_username = local_username.strip()
        if not USERNAME_PATTERN.fullmatch(local_username):
            return api_error(
                "Username must be 3–32 characters using letters, numbers, "
                "dots, underscores, or hyphens."
            )

    password = values.get("password")
    if password is not None and not isinstance(password, str):
        return api_error("Password must be text.")
    if password and len(password) < 12:
        return api_error("Password must be at least 12 characters.")

    recommendation_fields = (
        "listenbrainzUsername",
        "lastfmUsername",
    )
    for field in recommendation_fields:
        if field in values and values[field] is not None and not isinstance(
            values[field], str
        ):
            return api_error(f"{field} must be text.")

    recommendation_inputs_changed = False
    try:
        with db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            if not existing:
                return api_error("User not found.", 404)

            if role == "user" and existing["role"] == "admin":
                if existing["id"] == current_user()["id"]:
                    return api_error(
                        "Administrators cannot change their own role.", 409
                    )
                admin_count = connection.execute(
                    "SELECT COUNT(*) FROM users WHERE role = 'admin'"
                ).fetchone()[0]
                if admin_count <= 1:
                    return api_error(
                        "The final administrator cannot be demoted.", 409
                    )

            updates = []
            parameters = []
            if role is not None and role != existing["role"]:
                updates.append("role = ?")
                parameters.append(role)
            if (
                local_username is not None
                and local_username != existing["username"]
            ):
                updates.append("username = ?")
                parameters.append(local_username)
            # A blank password intentionally preserves the existing hash.
            if password:
                updates.append("password_hash = ?")
                parameters.append(generate_password_hash(password))

            if "listenbrainzUsername" in values:
                listenbrainz_username = (
                    str(values["listenbrainzUsername"] or "").strip() or None
                )
                if listenbrainz_username != existing["listenbrainz_username"]:
                    updates.append("listenbrainz_username = ?")
                    parameters.append(listenbrainz_username)
                    recommendation_inputs_changed = True

            if "lastfmUsername" in values:
                lastfm_username = (
                    str(values["lastfmUsername"] or "").strip() or None
                )
                if lastfm_username != existing["lastfm_username"]:
                    updates.extend([
                        "lastfm_username = ?",
                        "lastfm_api_key = NULL",
                    ])
                    parameters.append(lastfm_username)
                    recommendation_inputs_changed = True

            if updates:
                connection.execute(
                    f"UPDATE users SET {', '.join(updates)} WHERE id = ?",
                    (*parameters, user_id),
                )
            updated = _get_user(connection, user_id)
    except sqlite3.IntegrityError:
        return api_error("That username is already registered.", 409)

    if recommendation_inputs_changed:
        delete_recommendation_cache(user_id)
        recommendation_worker.request_refresh()

    return jsonify({
        "message": "User settings saved.",
        "user": _user_payload(updated),
    })


@blueprint.delete("/api/admin/users/<int:user_id>")
@admin_required
def delete_user(user_id):
    """Permanently remove an account and its user-owned data."""
    with db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT id, role FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if not existing:
            return api_error("User not found.", 404)
        if existing["id"] == current_user()["id"]:
            return api_error(
                "Administrators cannot delete their own account.", 409
            )
        if existing["role"] == "admin":
            admin_count = connection.execute(
                "SELECT COUNT(*) FROM users WHERE role = 'admin'"
            ).fetchone()[0]
            if admin_count <= 1:
                return api_error(
                    "The final administrator cannot be deleted.", 409
                )

        # The schema predates SQLite foreign-key cascades. Delete every
        # user-owned row explicitly so removal cannot leave orphaned account
        # data or background work behind on upgraded installations.
        connection.execute(
            "DELETE FROM plex_auth_flows WHERE user_id = ?", (user_id,)
        )
        connection.execute(
            "DELETE FROM pending_lidarr_searches WHERE user_id = ?", (user_id,)
        )
        connection.execute(
            "DELETE FROM recommendation_cache WHERE user_id = ?", (user_id,)
        )
        connection.execute(
            "DELETE FROM plex_listens WHERE user_id = ?", (user_id,)
        )
        connection.execute(
            "DELETE FROM request_history WHERE user_id = ?", (user_id,)
        )
        connection.execute(
            "DELETE FROM account_invitations WHERE created_by = ?", (user_id,)
        )
        connection.execute("DELETE FROM users WHERE id = ?", (user_id,))

    return jsonify({"message": "User deleted."})
