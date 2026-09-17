"""Private notification preference and administrator agent routes."""

import time
from urllib.parse import urlparse

from flask import Blueprint, jsonify

if __package__ == "backend.routes":
    from .. import notifications
    from ..responses import api_error, request_json_object
    from ..security import admin_required, current_user, login_required
    from ..storage import db
else:
    import notifications
    from responses import api_error, request_json_object
    from security import admin_required, current_user, login_required
    from storage import db


blueprint = Blueprint("notifications", __name__)
PUSH_HOSTS = (
    "fcm.googleapis.com", "push.services.mozilla.com", "web.push.apple.com",
    "notify.windows.com",
)


def _section_payload(values, allowed):
    """Reject cross-card fields instead of silently merging them into settings."""
    if not isinstance(values, dict):
        raise ValueError("Request body must be a JSON object.")
    unknown = set(values).difference(allowed)
    if unknown:
        raise ValueError("This notification settings section contains unsupported fields.")
    return values


def _supported_push_endpoint(value):
    if not isinstance(value, str) or len(value) > notifications.MAX_ENDPOINT:
        return False
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower().rstrip(".")
    try:
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https" and bool(host) and not parsed.username and not parsed.password
        and port in {None, 443}
        and any(host == allowed or host.endswith(f".{allowed}") for allowed in PUSH_HOSTS)
    )


@blueprint.get("/api/settings/notifications")
@admin_required
def get_global_notifications():
    return jsonify(notifications.public_config())


@blueprint.put("/api/settings/notifications")
@admin_required
def put_global_notifications():
    values = request_json_object()
    try:
        return jsonify(notifications.save_config(values))
    except ValueError as exc:
        return api_error(str(exc))


@blueprint.put("/api/settings/notifications/global")
@admin_required
def put_notification_global_section():
    try:
        return jsonify(notifications.save_global_config(_section_payload(
            request_json_object(), {"enabled", "applicationUrl", "delaySeconds"})))
    except ValueError as exc:
        return api_error(str(exc))


@blueprint.put("/api/settings/notifications/email")
@admin_required
def put_notification_email_section():
    try:
        return jsonify(notifications.save_email_config(_section_payload(
            request_json_object(), {"enabled", "host", "port", "encryption", "username", "senderName", "sender", "password", "clearPassword"})))
    except ValueError as exc:
        return api_error(str(exc))


@blueprint.put("/api/settings/notifications/web-push")
@admin_required
def put_notification_web_push_section():
    try:
        return jsonify(notifications.save_web_push_config(_section_payload(
            request_json_object(), {"enabled", "contact"})))
    except ValueError as exc:
        return api_error(str(exc))


def _test_delivery(user, *, email_target=None, subscription=None):
    return {
        "release_title": "Melodarr test message", "artist_name": "Notification check",
        "release_mbid": "11111111-1111-1111-1111-111111111111", "username": user["username"],
        "email_target": email_target, "push_endpoint": subscription["endpoint"] if subscription else None,
        "push_p256dh": subscription["p256dh"] if subscription else None,
        "push_auth": subscription["auth"] if subscription else None,
    }


@blueprint.post("/api/settings/notifications/email/test")
@admin_required
def test_notification_email():
    user = current_user()
    preferences = notifications.user_preferences(user)
    target = preferences["effectiveEmail"]
    config = notifications.notification_config().get("email") or {}
    if not (config.get("host") and config.get("sender")):
        return api_error("Save a configured SMTP host and sender address before testing email.")
    if not target:
        return api_error("Your account needs a custom or Plex notification email before testing.")
    try:
        if __package__ == "backend.routes":
            from ..workers.notifications import _email
        else:
            from workers.notifications import _email
        _email(_test_delivery(user, email_target=target), test=True)
    except Exception:
        return api_error("Melodarr could not send the test email. Check the saved SMTP settings and try again.")
    return jsonify({"message": "Representative test email sent."})


@blueprint.post("/api/settings/notifications/web-push/test")
@admin_required
def test_notification_web_push():
    user = current_user()
    config = notifications.notification_config().get("webPush") or {}
    if not config.get("contact"):
        return api_error("Save a configured VAPID contact before testing Web Push.")
    try:
        notifications.ensure_vapid_key()
    except Exception:
        return api_error("Melodarr could not initialize Web Push for this test.")
    with db() as connection:
        subscriptions = connection.execute("SELECT endpoint,p256dh,auth FROM web_push_subscriptions WHERE user_id=? ORDER BY id", (user["id"],)).fetchall()
    if not subscriptions:
        return api_error("Register a Web Push device for your account before testing.")
    try:
        if __package__ == "backend.routes":
            from ..workers.notifications import _push
        else:
            from workers.notifications import _push
        failures = 0
        for subscription in subscriptions:
            try:
                _push(_test_delivery(user, subscription=subscription), test=True)
            except Exception:
                # Test delivery is deliberately best-effort across every owned
                # snapshot; provider details remain server-side only.
                failures += 1
        if failures:
            return api_error(f"Melodarr could not send the Web Push test to {failures} registered device{'s' if failures != 1 else ''}. Check the saved Web Push settings and try again.")
    except Exception:
        return api_error("Melodarr could not prepare the Web Push test. Check the saved Web Push settings and try again.")
    return jsonify({"message": f"Representative Web Push test sent to {len(subscriptions)} registered device{'s' if len(subscriptions) != 1 else ''}."})


@blueprint.get("/api/account/notifications")
@login_required
def get_notifications():
    return jsonify(notifications.user_preferences(current_user()))


@blueprint.put("/api/account/notifications")
@login_required
def put_notifications():
    try:
        return jsonify(notifications.save_user_preferences(current_user(), request_json_object()))
    except ValueError as exc:
        return api_error(str(exc))


@blueprint.post("/api/account/notifications/subscriptions")
@login_required
def create_subscription():
    values = request_json_object()
    if not isinstance(values, dict):
        return api_error("Request body must be a JSON object.")
    endpoint = values.get("endpoint")
    keys = values.get("keys")
    p256dh = keys.get("p256dh") if isinstance(keys, dict) else None
    auth = keys.get("auth") if isinstance(keys, dict) else None
    metadata = {}
    for key, label in (("deviceName", "Device name"), ("operatingSystem", "Operating system"),
                       ("browser", "Browser"), ("engine", "Engine")):
        try:
            metadata[key] = notifications.safe_text(values[key], field=label) if key in values else ""
        except ValueError as exc:
            return api_error(str(exc))
    if (not _supported_push_endpoint(endpoint) or not isinstance(p256dh, str)
            or not isinstance(auth, str) or not p256dh or not auth
            or len(p256dh) > notifications.MAX_PUSH_KEY or len(auth) > notifications.MAX_PUSH_KEY):
        return api_error("Push subscription is invalid.")
    now = time.time()
    with db() as connection:
        connection.execute("""INSERT INTO web_push_subscriptions (user_id, endpoint, p256dh, auth, device_name, operating_system, browser, engine, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(endpoint) DO UPDATE SET user_id=excluded.user_id,
            p256dh=excluded.p256dh, auth=excluded.auth, device_name=excluded.device_name,
            operating_system=excluded.operating_system, browser=excluded.browser, engine=excluded.engine, updated_at=excluded.updated_at""",
            (current_user()["id"], endpoint, p256dh, auth, metadata["deviceName"], metadata["operatingSystem"], metadata["browser"], metadata["engine"], now, now))
        row = connection.execute("SELECT id, endpoint, device_name, operating_system, browser, engine, created_at, updated_at FROM web_push_subscriptions WHERE endpoint=?", (endpoint,)).fetchone()
    return jsonify(dict(row)), 201


@blueprint.delete("/api/account/notifications/subscriptions/<int:subscription_id>")
@login_required
def delete_subscription(subscription_id):
    with db() as connection:
        cursor = connection.execute("DELETE FROM web_push_subscriptions WHERE id=? AND user_id=?", (subscription_id, current_user()["id"]))
    if not cursor.rowcount:
        return api_error("Push device was not found.", 404)
    return jsonify({"message": "Push device removed."})


@blueprint.get("/api/account/notifications/mutes/<kind>/<mbid>")
@login_required
def get_mute(kind, mbid):
    if kind not in {"artist", "release-group"} or not notifications.valid_mbid(mbid):
        return api_error("Invalid notification mute target.")
    with db() as connection:
        muted = bool(connection.execute("SELECT 1 FROM notification_mutes WHERE user_id=? AND kind=? AND lower(mbid)=lower(?)", (current_user()["id"], kind, mbid)).fetchone())
    return jsonify({"kind": kind, "mbid": mbid, "muted": muted})


@blueprint.put("/api/account/notifications/mutes/<kind>/<mbid>")
@login_required
def put_mute(kind, mbid):
    if kind not in {"artist", "release-group"} or not notifications.valid_mbid(mbid):
        return api_error("Invalid notification mute target.")
    values = request_json_object()
    if not isinstance(values, dict) or not isinstance(values.get("muted"), bool):
        return api_error("muted must be true or false.")
    with db() as connection:
        if values["muted"]:
            connection.execute("INSERT OR IGNORE INTO notification_mutes (user_id,kind,mbid,created_at) VALUES (?,?,?,?)", (current_user()["id"], kind, mbid, time.time()))
        else:
            connection.execute("DELETE FROM notification_mutes WHERE user_id=? AND kind=? AND lower(mbid)=lower(?)", (current_user()["id"], kind, mbid))
    return jsonify({"kind": kind, "mbid": mbid, "muted": values["muted"]})
