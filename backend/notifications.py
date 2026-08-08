"""Notification configuration, availability events, and durable delivery helpers."""

import base64
import os
import re
import time
from tempfile import NamedTemporaryFile
from threading import Lock, RLock
from urllib.parse import urlparse

if __package__:
    from .config import VAPID_PRIVATE_KEY_FILE
    from .storage import db, get_service, save_service
else:
    from config import VAPID_PRIVATE_KEY_FILE
    from storage import db, get_service, save_service


MBID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
EMAIL_RE = re.compile(r"^[^\s@]{1,64}@[^\s@]{1,253}$")
MAX_ENDPOINT = 2048
MAX_PUSH_KEY = 512
MAX_DEVICE_TEXT = 100
_vapid_lock = Lock()
_notification_config_lock = RLock()


def _bool(values, key, default=False):
    value = values.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be true or false.")
    return value


def valid_mbid(value):
    return isinstance(value, str) and bool(MBID_RE.fullmatch(value))


def valid_email(value):
    return isinstance(value, str) and len(value) <= 254 and bool(EMAIL_RE.fullmatch(value.strip()))


def _url(value, *, required=False):
    value = str(value or "").strip()
    if not value and not required:
        return ""
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or len(value) > 512:
        raise ValueError("Application URL must be an absolute HTTP(S) URL.")
    return value.rstrip("/")


def safe_text(value, *, field, maximum=MAX_DEVICE_TEXT):
    """Accept short, plain display text without allowing header/control text."""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text.")
    value = re.sub(r"[\r\n]+", " ", value).strip()
    if len(value) > maximum or any(ord(character) < 32 for character in value):
        raise ValueError(f"{field} is invalid.")
    return value


def notification_config():
    config = get_service("notifications") or {}
    return config if isinstance(config, dict) else {}


def _public_key():
    if not os.path.exists(VAPID_PRIVATE_KEY_FILE):
        return ""
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePrivateKey
        with open(VAPID_PRIVATE_KEY_FILE, "rb") as file:
            private = serialization.load_pem_private_key(file.read(), password=None)
        if not isinstance(private, EllipticCurvePrivateKey):
            return ""
        raw = private.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    except (ImportError, OSError, ValueError, TypeError):
        return ""


def ensure_vapid_key():
    """Create one persistent VAPID P-256 key without placing it in settings.json."""
    with _vapid_lock:
        existing = _public_key()
        if existing:
            return existing
        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric import ec
        except ImportError as exc:
            raise ValueError("Web Push support is unavailable until pywebpush is installed.") from exc
        directory = os.path.dirname(os.path.abspath(VAPID_PRIVATE_KEY_FILE))
        os.makedirs(directory, exist_ok=True)
        contents = ec.generate_private_key(ec.SECP256R1()).private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        with NamedTemporaryFile("wb", dir=directory, delete=False) as file:
            file.write(contents)
            file.flush()
            os.fsync(file.fileno())
            temporary = file.name
        try:
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            try:
                os.link(temporary, VAPID_PRIVATE_KEY_FILE)
            except FileExistsError:
                pass
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        existing = _public_key()
        if not existing:
            raise RuntimeError("Could not initialize the VAPID private key.")
        return existing


def public_config():
    config = notification_config()
    email = config.get("email") if isinstance(config.get("email"), dict) else {}
    push = config.get("webPush") if isinstance(config.get("webPush"), dict) else {}
    return {
        "enabled": bool(config.get("enabled")),
        "applicationUrl": str(config.get("applicationUrl") or ""),
        "email": {
            "enabled": bool(email.get("enabled")), "configured": bool(email.get("host") and email.get("sender")),
            "host": str(email.get("host") or ""), "port": int(email.get("port") or 0),
            "encryption": str(email.get("encryption") or "starttls"), "username": str(email.get("username") or ""),
            "sender": str(email.get("sender") or ""), "passwordConfigured": bool(email.get("password")),
            "senderName": str(email.get("senderName") or ""),
        },
        "webPush": {
            "enabled": bool(push.get("enabled")), "configured": bool(push.get("contact") and _public_key()),
            "contact": str(push.get("contact") or ""), "vapidSubject": str(push.get("contact") or ""),
            "publicKey": _public_key(),
        },
    }


def save_config(values):
    """Validate and replace the complete legacy configuration payload."""
    if not isinstance(values, dict):
        raise ValueError("Request body must be a JSON object.")
    with _notification_config_lock:
        old = notification_config()
        result = _validated_config(values, old)
        if result["webPush"]["enabled"]:
            ensure_vapid_key()
        save_service("notifications", result)
    return public_config()


def _validated_config(values, old):
    old_email = old.get("email") if isinstance(old.get("email"), dict) else {}
    email_values = values.get("email", {})
    push_values = values.get("webPush", {})
    if not isinstance(email_values, dict) or not isinstance(push_values, dict):
        raise ValueError("email and webPush must be objects.")
    for key in ("host", "username", "sender", "password", "senderName"):
        if key in email_values and not isinstance(email_values[key], str):
            raise ValueError(f"SMTP {key} must be text.")
    host = str(email_values.get("host", "")).strip()
    username = str(email_values.get("username", "")).strip()
    sender = str(email_values.get("sender", "")).strip()
    sender_name = safe_text(email_values.get("senderName", ""), field="SMTP sender name", maximum=120)
    try:
        port = int(email_values.get("port", 587))
    except (TypeError, ValueError) as exc:
        raise ValueError("SMTP port must be a valid TCP port.") from exc
    encryption = str(email_values.get("encryption", "starttls")).lower()
    if host and (len(host) > 255 or any(c.isspace() for c in host)):
        raise ValueError("SMTP host is invalid.")
    if len(username) > 254 or len(sender) > 254:
        raise ValueError("SMTP username or sender is too long.")
    if not 1 <= port <= 65535 or encryption not in {"none", "starttls", "tls"}:
        raise ValueError("SMTP port or encryption is invalid.")
    if sender and not valid_email(sender):
        raise ValueError("SMTP sender must be a valid email address.")
    password = email_values.get("password", "")
    if not isinstance(password, str) or len(password) > 1024:
        raise ValueError("SMTP password must be text.")
    if email_values.get("clearPassword") is True:
        password = ""
    elif not password:
        password = str(old_email.get("password") or "")
    if "contact" in push_values and not isinstance(push_values["contact"], str):
        raise ValueError("Web Push contact must be text.")
    contact = str(push_values.get("contact", "")).strip()
    web_push_enabled = _bool(push_values, "enabled")
    parsed_contact = urlparse(contact)
    valid_contact = (
        contact.startswith("mailto:") and valid_email(contact[7:])
    ) or (
        parsed_contact.scheme == "https" and bool(parsed_contact.netloc)
    )
    if len(contact) > 512 or (contact and not valid_contact):
        raise ValueError("Web Push contact must be a mailto: address or HTTPS URL.")
    if web_push_enabled and not contact:
        raise ValueError("Web Push contact is required when Web Push is enabled.")
    result = {
        "enabled": _bool(values, "enabled"), "applicationUrl": _url(values.get("applicationUrl"), required=False),
        "email": {"enabled": _bool(email_values, "enabled"), "host": host, "port": port,
                  "encryption": encryption, "username": username, "password": password, "sender": sender,
                  "senderName": sender_name},
        "webPush": {"enabled": web_push_enabled, "contact": contact},
    }
    return result


def _merged_config(section, values):
    """Apply one admin card while retaining siblings under a single lock."""
    if not isinstance(values, dict):
        raise ValueError("Request body must be a JSON object.")
    with _notification_config_lock:
        old = notification_config()
        old_email = old.get("email") if isinstance(old.get("email"), dict) else {}
        old_push = old.get("webPush") if isinstance(old.get("webPush"), dict) else {}
        merged = {
            "enabled": bool(old.get("enabled")),
            "applicationUrl": str(old.get("applicationUrl") or ""),
            "email": {"enabled": bool(old_email.get("enabled")), "host": str(old_email.get("host") or ""),
                      "port": old_email.get("port") or 587, "encryption": str(old_email.get("encryption") or "starttls"),
                      "username": str(old_email.get("username") or ""), "password": str(old_email.get("password") or ""),
                      "sender": str(old_email.get("sender") or ""), "senderName": str(old_email.get("senderName") or "")},
            "webPush": {"enabled": bool(old_push.get("enabled")), "contact": str(old_push.get("contact") or "")},
        }
        if section == "global":
            merged.update(values)
        elif section == "email":
            merged["email"].update(values)
        elif section == "webPush":
            merged["webPush"].update(values)
        else:
            raise ValueError("Unknown notification settings section.")
        result = _validated_config(merged, old)
        if result["webPush"]["enabled"]:
            ensure_vapid_key()
        save_service("notifications", result)
    return public_config()


def save_global_config(values):
    return _merged_config("global", values)


def save_email_config(values):
    return _merged_config("email", values)


def save_web_push_config(values):
    return _merged_config("webPush", values)


def global_channels():
    config = notification_config()
    email = config.get("email") or {}
    push = config.get("webPush") or {}
    return bool(config.get("enabled")), bool(email.get("enabled") and email.get("host") and email.get("sender")), bool(push.get("enabled") and push.get("contact") and _public_key())


def user_preferences(user):
    master, email_available, push_available = global_channels()
    with db() as connection:
        row = connection.execute("SELECT * FROM user_notification_preferences WHERE user_id = ?", (user["id"],)).fetchone()
        devices = connection.execute("""SELECT id, endpoint, device_name, operating_system, browser, engine,
            created_at, updated_at FROM web_push_subscriptions WHERE user_id = ? ORDER BY id""", (user["id"],)).fetchall()
    row = dict(row) if row else {}
    fallback = str(user["plex_email"] or "").strip()
    return {"enabled": bool(row.get("enabled", 0)), "emailEnabled": bool(row.get("email_enabled", 0)),
            "webPushEnabled": bool(row.get("web_push_enabled", 0)), "requestedAvailable": bool(row.get("requested_available", 1)),
            "allNewMusic": bool(row.get("all_new_music", 0)),
            # notificationEmail is the display/effective value.  The explicit
            # override field lets clients avoid persisting an unchanged Plex
            # address and therefore preserve the live fallback relationship.
            "notificationEmail": row.get("notification_email") or fallback,
            "notificationEmailOverride": row.get("notification_email") or "",
            "notificationEmailSource": "custom" if row.get("notification_email") else ("plex" if fallback else "empty"),
            "fallbackEmail": fallback, "effectiveEmail": row.get("notification_email") or fallback,
            "global": {"enabled": master, "emailAvailable": email_available, "webPushAvailable": push_available, "publicKey": _public_key()},
            "devices": [dict(device) for device in devices]}


def save_user_preferences(user, values):
    if not isinstance(values, dict):
        raise ValueError("Request body must be a JSON object.")
    email = str(values.get("notificationEmail", "")).strip()
    if email and not valid_email(email):
        raise ValueError("Notification email must be valid.")
    flags = {key: _bool(values, key) for key in ("enabled", "emailEnabled", "webPushEnabled", "requestedAvailable", "allNewMusic")}
    with db() as connection:
        connection.execute("""INSERT INTO user_notification_preferences
            (user_id, notification_email, enabled, email_enabled, web_push_enabled, requested_available, all_new_music, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET notification_email=excluded.notification_email, enabled=excluded.enabled,
            email_enabled=excluded.email_enabled, web_push_enabled=excluded.web_push_enabled,
            requested_available=excluded.requested_available, all_new_music=excluded.all_new_music, updated_at=excluded.updated_at""",
            (user["id"], email or None, *(int(flags[key]) for key in flags), time.time()))
    return user_preferences(user)


def observe_availability(albums):
    """Persist a complete successful scan. Missing albums are deliberately unknown."""
    master, email_on, push_on = global_channels()
    now = time.time()
    events = 0
    with db() as connection:
        for release_mbid, album in albums.items():
            if not valid_mbid(str(release_mbid)) or not isinstance(album, dict):
                continue
            available = int(bool(album.get("fullyAvailable")))
            title = str(album.get("title") or "")[:500]
            artist_mbid = str(album.get("artistMbid") or "")[:64]
            artist_name = str(album.get("artistName") or "")[:500]
            previous = connection.execute("SELECT * FROM release_availability_state WHERE release_mbid = ?", (release_mbid,)).fetchone()
            if not previous:
                connection.execute("INSERT INTO release_availability_state (release_mbid, fully_available, generation, artist_mbid, artist_name, release_title, created_at, updated_at) VALUES (?, ?, 0, ?, ?, ?, ?, ?)", (release_mbid, available, artist_mbid, artist_name, title, now, now))
                continue
            generation = previous["generation"]
            transitioned = not previous["fully_available"] and available
            if transitioned:
                generation += 1
            connection.execute("UPDATE release_availability_state SET fully_available=?, generation=?, artist_mbid=?, artist_name=?, release_title=?, updated_at=? WHERE release_mbid=?", (available, generation, artist_mbid, artist_name, title, now, release_mbid))
            if not transitioned or not master or not (email_on or push_on):
                continue
            event = connection.execute("INSERT OR IGNORE INTO notification_events (release_mbid, generation, artist_mbid, artist_name, release_title, created_at) VALUES (?, ?, ?, ?, ?, ?)", (release_mbid, generation, artist_mbid, artist_name, title, now))
            if not event.rowcount:
                continue
            event_id = event.lastrowid
            preferences = connection.execute("""SELECT p.*, u.plex_email, u.username FROM user_notification_preferences p
                JOIN users u ON u.id=p.user_id WHERE p.enabled=1""").fetchall()
            for pref in preferences:
                requested = pref["requested_available"] and connection.execute("""SELECT 1 FROM request_history WHERE user_id=? AND
                    ((kind='release-group' AND lower(mbid)=lower(?)) OR (kind='artist' AND lower(mbid)=lower(?))) LIMIT 1""", (pref["user_id"], release_mbid, artist_mbid)).fetchone()
                if not pref["all_new_music"] and not requested:
                    continue
                muted = connection.execute("SELECT 1 FROM notification_mutes WHERE user_id=? AND ((kind='release-group' AND lower(mbid)=lower(?)) OR (kind='artist' AND lower(mbid)=lower(?))) LIMIT 1", (pref["user_id"], release_mbid, artist_mbid)).fetchone()
                if muted:
                    continue
                if email_on and pref["email_enabled"]:
                    target = str(pref["notification_email"] or pref["plex_email"] or "").strip()
                    if valid_email(target):
                        connection.execute("INSERT OR IGNORE INTO notification_deliveries (event_id,user_id,channel,email_target,status,attempts,next_attempt_at,created_at,updated_at) VALUES (?,?,'email',?,'pending',0,?,?,?)", (event_id, pref["user_id"], target, now, now, now))
                if push_on and pref["web_push_enabled"]:
                    subscriptions = connection.execute("SELECT id, endpoint, p256dh, auth FROM web_push_subscriptions WHERE user_id=?", (pref["user_id"],)).fetchall()
                    for subscription in subscriptions:
                        connection.execute("INSERT OR IGNORE INTO notification_deliveries (event_id,user_id,channel,subscription_id,push_endpoint,push_p256dh,push_auth,status,attempts,next_attempt_at,created_at,updated_at) VALUES (?,?,'web-push',?,?,?,?, 'pending',0,?,?,?)", (event_id, pref["user_id"], subscription["id"], subscription["endpoint"], subscription["p256dh"], subscription["auth"], now, now, now))
            events += 1
    return events
