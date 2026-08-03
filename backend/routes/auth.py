"""Local account, Plex authentication, and session routes."""

import hashlib
import json
import re
import secrets
import sqlite3
import time
from hmac import compare_digest

import requests
from flask import Blueprint, current_app, jsonify, request, session
from werkzeug.security import check_password_hash, generate_password_hash

if __package__ == "backend.routes":
    from ..api_cache import clear_cache
    from ..detail_cache import invalidate_all as invalidate_detail_payloads
    from ..responses import api_error, request_json_object
    from ..security import (
        current_user,
        login_required,
        resolve_account_user,
        start_session,
        user_payload,
    )
    from ..services import plex, plex_auth
    from ..storage import db, get_service, save_service
    from ..workers import plex as plex_worker
    from ..workers import plex_history as plex_history_worker
else:  # Support the existing `python backend/app.py` entry point.
    from api_cache import clear_cache
    from detail_cache import invalidate_all as invalidate_detail_payloads
    from responses import api_error, request_json_object
    from security import (
        current_user,
        login_required,
        resolve_account_user,
        start_session,
        user_payload,
    )
    from services import plex, plex_auth
    from storage import db, get_service, save_service
    from workers import plex as plex_worker
    from workers import plex_history as plex_history_worker


blueprint = Blueprint("auth", __name__)
PLEX_FLOW_TTL = 15 * 60
USER_COLUMNS = (
    "id, username, password_hash, role, listenbrainz_username, "
    "lastfm_username, plex_id, plex_username, "
    "plex_email, plex_avatar"
)


def _invitation_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _flow_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _purge_expired_plex_flows(connection, now=None):
    connection.execute(
        "DELETE FROM plex_auth_flows WHERE expires_at <= ?",
        (time.time() if now is None else now,),
    )


def _discard_plex_flow(flow_token):
    with db() as connection:
        connection.execute(
            "DELETE FROM plex_auth_flows WHERE flow_hash = ?",
            (_flow_hash(flow_token),),
        )


def _log_plex_failure(message, error):
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    suffix = f", HTTP {status}" if status is not None else ""
    current_app.logger.warning(
        "%s (%s%s)",
        message,
        type(error).__name__,
        suffix,
    )


def _client_identifier():
    secret = current_app.config["SECRET_KEY"]
    if isinstance(secret, bytes):
        secret = secret.decode("utf-8", "replace")
    return hashlib.sha256(f"melodarr-plex:{secret}".encode("utf-8")).hexdigest()


def _load_flow(flow_token, purpose=None):
    if not isinstance(flow_token, str) or not (32 <= len(flow_token) <= 256):
        return None, api_error("This Plex sign-in has expired. Start again.", 400)
    flow_hash = _flow_hash(flow_token)
    with db() as connection:
        _purge_expired_plex_flows(connection)
        row = connection.execute(
            "SELECT * FROM plex_auth_flows WHERE flow_hash = ?", (flow_hash,)
        ).fetchone()
        if not row:
            return None, api_error("This Plex sign-in has expired. Start again.", 410)
        if purpose and row["purpose"] != purpose:
            return None, api_error("This Plex sign-in cannot be used here.", 400)
        if row["user_id"]:
            user = current_user()
            if not user:
                return None, api_error("Sign in is required.", 401)
            if (
                user["id"] != row["user_id"]
                and not (
                    row["purpose"] == "link"
                    and user["role"] == "admin"
                )
            ):
                return None, api_error(
                    "This Plex sign-in belongs to another Melodarr user.", 403
                )
            if row["purpose"] == "server" and user["role"] != "admin":
                return None, api_error("Administrator access is required.", 403)
        elif row["purpose"] == "server":
            has_users = connection.execute("SELECT 1 FROM users LIMIT 1").fetchone()
            if has_users:
                return None, api_error("The initial setup was already completed.", 409)
    return row, None


def _public_servers(resources, *, owned_only=False):
    return [
        {
            "id": resource["clientIdentifier"],
            "name": resource["name"],
            "owned": bool(resource["owned"]),
            "connections": resource["connections"],
        }
        for resource in resources
        if (not owned_only or resource["owned"]) and resource["connections"]
    ]


def _username_candidate(connection, account):
    raw = (
        account.get("username")
        or str(account.get("email", "")).partition("@")[0]
        or f"plex-{account['id']}"
    )
    base = re.sub(r"[^A-Za-z0-9_.-]+", "-", raw).strip("._-")[:32]
    if len(base) < 3:
        base = f"plex-{account['id']}"[:32]
    candidate = base
    suffix = 1
    while connection.execute(
        "SELECT 1 FROM users WHERE username = ?", (candidate,)
    ).fetchone():
        suffix += 1
        ending = f"-{suffix}"
        candidate = f"{base[:32 - len(ending)]}{ending}"
    return candidate


def _create_plex_user(connection, account, role):
    username = _username_candidate(connection, account)
    connection.execute(
        "INSERT INTO users "
        "(username, password_hash, role, plex_id, plex_username, plex_email, "
        "plex_avatar, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            username,
            generate_password_hash(secrets.token_urlsafe(48)),
            role,
            account["id"],
            account.get("username") or account.get("title") or username,
            account.get("email") or None,
            account.get("thumb") or None,
            time.time(),
        ),
    )
    return connection.execute(
        f"SELECT {USER_COLUMNS} FROM users WHERE plex_id = ?", (account["id"],)
    ).fetchone()


def _finish_plex_login(account, resources):
    plex_config = get_service("plex")
    if not plex_config:
        return api_error("Plex sign-in is not enabled on this Melodarr instance.", 403)
    machine_identifier = str(plex_config.get("machineIdentifier", ""))
    has_access = any(
        resource["clientIdentifier"] == machine_identifier for resource in resources
    )
    if not has_access:
        return api_error("Your Plex account does not have access to this server.", 403)

    created = False
    try:
        with db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            user = connection.execute(
                f"SELECT {USER_COLUMNS} FROM users WHERE plex_id = ?",
                (account["id"],),
            ).fetchone()
            if not user:
                owner_of_configured_server = any(
                    resource["clientIdentifier"] == machine_identifier
                    and resource["owned"]
                    for resource in resources
                )
                unlinked_admin = connection.execute(
                    "SELECT 1 FROM users WHERE role = 'admin' AND plex_id IS NULL "
                    "LIMIT 1"
                ).fetchone()
                if owner_of_configured_server and unlinked_admin:
                    return api_error(
                        "Sign in with your local administrator account, then link "
                        "this Plex owner from Settings before using Plex sign-in.",
                        409,
                    )
                user = _create_plex_user(connection, account, "user")
                created = True
            else:
                connection.execute(
                    "UPDATE users SET plex_username = ?, plex_email = ?, plex_avatar = ? "
                    "WHERE id = ?",
                    (
                        account.get("username") or account.get("title"),
                        account.get("email") or None,
                        account.get("thumb") or None,
                        user["id"],
                    ),
                )
                user = connection.execute(
                    f"SELECT {USER_COLUMNS} FROM users WHERE id = ?", (user["id"],)
                ).fetchone()
    except sqlite3.IntegrityError:
        return api_error("That Plex account is already linked to another user.", 409)
    if created:
        plex_history_worker.request_full_sync()
    return start_session(user, remember=True)


def _finish_plex_link(flow, account, resources):
    """Attach a verified Plex identity to the user that started this flow."""
    plex_config = get_service("plex")
    if not plex_config:
        return api_error("Plex is not configured on this Melodarr instance.", 403)
    machine_identifier = str(plex_config.get("machineIdentifier", ""))
    if not machine_identifier:
        return api_error(
            "The configured Plex server cannot be verified. Ask an administrator "
            "to reconnect it.",
            409,
        )
    if not any(
        resource["clientIdentifier"] == machine_identifier for resource in resources
    ):
        return api_error(
            "That Plex account does not have access to this server.", 403
        )

    try:
        with db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            duplicate = connection.execute(
                "SELECT id FROM users WHERE plex_id = ? AND id != ?",
                (account["id"], flow["user_id"]),
            ).fetchone()
            if duplicate:
                return api_error(
                    "That Plex account is already linked to another Melodarr user.",
                    409,
                )
            existing = connection.execute(
                "SELECT plex_id FROM users WHERE id = ?", (flow["user_id"],)
            ).fetchone()
            if not existing:
                return api_error("Sign in is required.", 401)
            if existing["plex_id"] and existing["plex_id"] != account["id"]:
                return api_error(
                    "This Melodarr account is already linked to a different Plex "
                    "account.",
                    409,
                )
            connection.execute(
                "UPDATE users SET plex_id = ?, plex_username = ?, plex_email = ?, "
                "plex_avatar = ? WHERE id = ?",
                (
                    account["id"],
                    account.get("username") or account.get("title"),
                    account.get("email") or None,
                    account.get("thumb") or None,
                    flow["user_id"],
                ),
            )
            user = connection.execute(
                f"SELECT {USER_COLUMNS} FROM users WHERE id = ?",
                (flow["user_id"],),
            ).fetchone()
    except sqlite3.IntegrityError:
        return api_error(
            "That Plex account is already linked to another Melodarr user.", 409
        )

    payload = user_payload(user)
    payload["message"] = (
        "Plex account linked. You can now sign in with either your local "
        "credentials or Plex."
    )
    plex_history_worker.request_full_sync()
    return jsonify(payload)


def _link_csrf_error():
    expected = session.get("csrf_token", "")
    received = request.headers.get("X-CSRF-Token", "")
    if not expected or not compare_digest(expected, received):
        return api_error("Invalid or missing CSRF token.", 403)
    return None


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
        "plexConfigured": bool(get_service("plex")),
    })


@blueprint.post("/api/auth/register")
def register():
    values = request_json_object()
    if values is None:
        return api_error("Request body must be a JSON object.")
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
                f"SELECT {USER_COLUMNS} FROM users WHERE username = ?",
                (username,),
            ).fetchone()
    except sqlite3.IntegrityError:
        return api_error("That username is already registered.", 409)
    except sqlite3.OperationalError:
        return api_error("Could not create the account right now. Try again shortly.", 503)
    return start_session(user), 201


@blueprint.post("/api/auth/login")
def login():
    values = request_json_object()
    if values is None:
        return api_error("Request body must be a JSON object.")
    username = str(values.get("username", "")).strip()
    password = str(values.get("password", ""))
    with db() as connection:
        user = connection.execute(
            f"SELECT {USER_COLUMNS} FROM users WHERE username = ?",
            (username,),
        ).fetchone()
    if not user or not check_password_hash(user["password_hash"], password):
        return api_error("Invalid username or password.", 401)
    remember = values.get("remember") is True or str(values.get("remember", "")).lower() in {
        "1", "true", "on",
    }
    return start_session(user, remember=remember)


@blueprint.post("/api/auth/plex/start")
def start_plex_auth():
    values = request_json_object()
    if values is None:
        return api_error("Request body must be a JSON object.")
    purpose = str(values.get("purpose", "login"))
    if purpose not in {"login", "server", "link"}:
        return api_error("Unknown Plex sign-in purpose.")

    with db() as connection:
        _purge_expired_plex_flows(connection)

    flow_user_id = None
    if purpose == "link":
        user = current_user()
        if not user:
            return api_error("Sign in is required.", 401)
        csrf_error = _link_csrf_error()
        if csrf_error:
            return csrf_error
        if not get_service("plex"):
            return api_error(
                "Plex is not configured on this Melodarr instance.", 403
            )
        target_user, error = resolve_account_user(
            user,
            values.get("username"),
        )
        if error:
            return error
        if target_user["plex_id"]:
            return api_error(
                "This Melodarr account is already linked to Plex.", 409
            )
        flow_user_id = target_user["id"]
    elif purpose == "server":
        with db() as connection:
            has_users = connection.execute("SELECT 1 FROM users LIMIT 1").fetchone()
        if has_users:
            user = current_user()
            if not user:
                return api_error("Sign in is required.", 401)
            if user["role"] != "admin":
                return api_error("Administrator access is required.", 403)
            flow_user_id = user["id"]
    elif not get_service("plex"):
        return api_error("Plex sign-in is not enabled on this Melodarr instance.", 403)

    client_identifier = _client_identifier()
    try:
        pin = plex_auth.create_pin(client_identifier)
    except (ValueError, requests.RequestException) as exc:
        _log_plex_failure("Could not create a Plex sign-in PIN", exc)
        return api_error("Plex sign-in could not be started. Try again.", 502)

    flow_token = secrets.token_urlsafe(48)
    now = time.time()
    expires_at = min(pin.get("expiresAt") or now + PLEX_FLOW_TTL, now + PLEX_FLOW_TTL)
    with db() as connection:
        connection.execute(
            "INSERT INTO plex_auth_flows "
            "(flow_hash, pin_id, client_identifier, purpose, user_id, created_at, "
            "expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                _flow_hash(flow_token),
                pin["id"],
                client_identifier,
                purpose,
                flow_user_id,
                now,
                expires_at,
            ),
        )
    return jsonify({
        "flowToken": flow_token,
        "authorizationUrl": pin["authorizationUrl"],
        "expiresAt": expires_at,
    }), 201


@blueprint.post("/api/auth/plex/poll")
def poll_plex_auth():
    values = request_json_object()
    if values is None:
        return api_error("Request body must be a JSON object.")
    flow_token = str(values.get("flowToken", ""))
    flow, error = _load_flow(flow_token)
    if error:
        return error
    if flow["purpose"] == "link":
        csrf_error = _link_csrf_error()
        if csrf_error:
            return csrf_error

    if flow["auth_token"]:
        token = flow["auth_token"]
        account = json.loads(flow["account_json"])
        resources = json.loads(flow["resources_json"])
    else:
        try:
            token = plex_auth.poll_pin(flow["pin_id"], flow["client_identifier"])
            if not token:
                return jsonify({"pending": True}), 202
            account = plex_auth.get_account(token, flow["client_identifier"])
            resources = plex_auth.get_resources(token, flow["client_identifier"])
        except (ValueError, requests.RequestException) as exc:
            _log_plex_failure("Could not complete Plex account discovery", exc)
            _discard_plex_flow(flow_token)
            return api_error("Plex sign-in could not be completed. Try again.", 502)
        with db() as connection:
            connection.execute(
                "UPDATE plex_auth_flows SET auth_token = ?, account_json = ?, "
                "resources_json = ? WHERE flow_hash = ?",
                (
                    token,
                    json.dumps(account, separators=(",", ":")),
                    json.dumps(resources, separators=(",", ":")),
                    _flow_hash(flow_token),
                ),
            )

    if flow["purpose"] == "login":
        response = _finish_plex_login(account, resources)
        if isinstance(response, tuple):
            _discard_plex_flow(flow_token)
            return response
        _discard_plex_flow(flow_token)
        return response
    if flow["purpose"] == "link":
        response = _finish_plex_link(flow, account, resources)
        _discard_plex_flow(flow_token)
        return response

    servers = _public_servers(resources, owned_only=True)
    if not servers:
        _discard_plex_flow(flow_token)
        return api_error(
            "Use the Plex account that owns the server you want to connect.", 403
        )
    return jsonify({
        "pending": False,
        "account": {
            "username": account.get("username") or account.get("title"),
            "email": account.get("email"),
        },
        "servers": servers,
    })


@blueprint.post("/api/auth/plex/inspect")
def inspect_plex_server():
    values = request_json_object()
    if values is None:
        return api_error("Request body must be a JSON object.")
    flow_token = str(values.get("flowToken", ""))
    flow, error = _load_flow(flow_token, purpose="server")
    if error:
        return error
    if not flow["auth_token"] or not flow["resources_json"]:
        return api_error("Finish signing in with Plex first.", 409)

    server_id = str(values.get("serverId", ""))
    connection_uri = str(values.get("connectionUri", "")).rstrip("/")
    resources = json.loads(flow["resources_json"])
    server = next(
        (
            resource
            for resource in resources
            if resource["clientIdentifier"] == server_id and resource["owned"]
        ),
        None,
    )
    connection = next(
        (
            item
            for item in (server or {}).get("connections", [])
            if item["uri"] == connection_uri
        ),
        None,
    )
    if not server or not connection:
        return api_error("Choose a server and connection returned by Plex.", 400)

    config = {
        "url": connection["uri"],
        "token": server.get("accessToken") or flow["auth_token"],
    }
    try:
        machine_identifier = plex.machine_identifier(config)
        if machine_identifier != server["clientIdentifier"]:
            return api_error("That connection belongs to a different Plex server.", 409)
        libraries = plex.music_sections(config)
    except (ValueError, requests.RequestException) as exc:
        _log_plex_failure("Could not inspect the selected Plex server", exc)
        return api_error(
            "Melodarr could not reach that Plex connection. Try another address.",
            502,
        )
    if not libraries:
        return api_error("This Plex server has no music libraries available.", 409)

    config["machineIdentifier"] = machine_identifier
    selection = {"serverName": server["name"], "config": config}
    with db() as connection_db:
        connection_db.execute(
            "UPDATE plex_auth_flows SET selection_json = ?, libraries_json = ? "
            "WHERE flow_hash = ?",
            (
                json.dumps(selection, separators=(",", ":")),
                json.dumps(libraries, separators=(",", ":")),
                _flow_hash(flow_token),
            ),
        )
    return jsonify({
        "message": f"Connected to {server['name']}. Choose the music libraries to scan.",
        "libraries": libraries,
    })


@blueprint.post("/api/auth/plex/complete")
def complete_plex_server():
    values = request_json_object()
    if values is None:
        return api_error("Request body must be a JSON object.")
    flow_token = str(values.get("flowToken", ""))
    flow, error = _load_flow(flow_token, purpose="server")
    if error:
        return error
    if not flow["selection_json"] or not flow["libraries_json"] or not flow["account_json"]:
        return api_error("Test a Plex server connection before saving it.", 409)

    requested_ids = values.get("librarySectionIds", [])
    if not isinstance(requested_ids, list):
        requested_ids = [requested_ids]
    libraries = json.loads(flow["libraries_json"])
    available_ids = {item["id"] for item in libraries}
    selected_ids = [str(value) for value in requested_ids if str(value) in available_ids]
    if not selected_ids:
        return api_error("Select at least one Plex music library.")

    account = json.loads(flow["account_json"])
    selection = json.loads(flow["selection_json"])
    config = {
        **selection["config"],
        "serverName": selection["serverName"],
        "libraries": libraries,
        "librarySectionIds": selected_ids,
    }
    flow_hash = _flow_hash(flow_token)
    try:
        with db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if flow["user_id"]:
                duplicate = connection.execute(
                    "SELECT id FROM users WHERE plex_id = ? AND id != ?",
                    (account["id"], flow["user_id"]),
                ).fetchone()
                if duplicate:
                    return api_error(
                        "That Plex account is already linked to another Melodarr user.",
                        409,
                    )
                connection.execute(
                    "UPDATE users SET plex_id = ?, plex_username = ?, plex_email = ?, "
                    "plex_avatar = ? WHERE id = ? AND role = 'admin'",
                    (
                        account["id"],
                        account.get("username") or account.get("title"),
                        account.get("email") or None,
                        account.get("thumb") or None,
                        flow["user_id"],
                    ),
                )
                user = connection.execute(
                    f"SELECT {USER_COLUMNS} FROM users WHERE id = ?",
                    (flow["user_id"],),
                ).fetchone()
            else:
                if connection.execute("SELECT 1 FROM users LIMIT 1").fetchone():
                    return api_error("The initial setup was already completed.", 409)
                user = _create_plex_user(connection, account, "admin")

            save_service("plex", config)
            connection.execute(
                "DELETE FROM plex_auth_flows WHERE flow_hash = ?", (flow_hash,)
            )
    except (OSError, RuntimeError):
        return api_error("Plex settings could not be saved.", 500)
    except sqlite3.IntegrityError:
        return api_error("That Plex account is already linked to another user.", 409)

    clear_cache("plex-library")
    clear_cache("plex-guid")
    invalidate_detail_payloads()
    plex_worker.request_full_scan()
    plex_history_worker.request_full_sync()
    return start_session(user, remember=True)


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
