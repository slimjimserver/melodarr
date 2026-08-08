"""At-least-once notification delivery worker.

The lease is committed before SMTP/Web Push I/O. A process crash after a provider
accepts a request and before we mark sent can therefore duplicate a notification;
this is the intentional recoverable at-least-once tradeoff.
"""

import html
import json
import secrets
import smtplib
import ssl
import time
import re
from email.utils import formataddr
from email.message import EmailMessage
from pathlib import Path
from threading import Event
import requests

if __package__ == "backend.workers":
    from ..config import VAPID_PRIVATE_KEY_FILE
    from ..notifications import global_channels, notification_config
    from ..storage import db
else:
    from config import VAPID_PRIVATE_KEY_FILE
    from notifications import global_channels, notification_config
    from storage import db


POLL_INTERVAL = 10
LEASE_SECONDS = 90
MAX_ATTEMPTS = 6
WEB_PUSH_TTL_SECONDS = 86400
wake_requested = Event()


class NoRedirectSession(requests.Session):
    """Prevent a push provider endpoint from redirecting worker traffic."""

    def post(self, url, *args, **kwargs):
        kwargs["allow_redirects"] = False
        return super().post(url, *args, **kwargs)


def claim_due_delivery():
    now = time.time()
    token = secrets.token_urlsafe(24)
    master, email_enabled, push_enabled = global_channels()
    channels = tuple(channel for channel, enabled in (("email", master and email_enabled), ("web-push", master and push_enabled)) if enabled)
    if not channels:
        return None
    placeholders = ",".join("?" for _ in channels)
    with db() as connection:
        connection.execute("""UPDATE notification_deliveries SET status='dead', lease_token=NULL,
            lease_until=NULL, last_error='attempt limit reached', updated_at=?
            WHERE attempts >= ? AND ((status='pending' AND next_attempt_at <= ?)
            OR (status='leased' AND lease_until <= ?)) AND channel IN (""" + placeholders + ")", (now, MAX_ATTEMPTS, now, now, *channels))
        row = connection.execute("""SELECT d.id FROM notification_deliveries d
            WHERE d.attempts < ? AND ((d.status='pending' AND d.next_attempt_at <= ?)
               OR (d.status='leased' AND d.lease_until <= ?)
            ) AND d.channel IN (""" + placeholders + ") ORDER BY d.next_attempt_at, d.id LIMIT 1", (MAX_ATTEMPTS, now, now, *channels)).fetchone()
        if not row:
            return None
        cursor = connection.execute("""UPDATE notification_deliveries SET status='leased', lease_token=?,
            lease_until=?, attempts=attempts+1, updated_at=? WHERE id=? AND
            attempts < ? AND ((status='pending' AND next_attempt_at <= ?) OR (status='leased' AND lease_until <= ?))""",
            (token, now + LEASE_SECONDS, now, row["id"], MAX_ATTEMPTS, now, now))
        if not cursor.rowcount:
            return None
        return connection.execute("""SELECT d.*, e.release_mbid, e.artist_name, e.release_title, u.username,
            d.push_endpoint, d.push_p256dh, d.push_auth FROM notification_deliveries d
            JOIN notification_events e ON e.id=d.event_id
            JOIN users u ON u.id=d.user_id
            WHERE d.id=?""", (row["id"],)).fetchone()


def _header_text(value, fallback):
    value = re.sub(r"[\r\n]+", " ", str(value or fallback)).strip()
    return value or fallback


def build_email_message(delivery, *, test=False):
    """Build the same safe multipart message used for test and real delivery."""
    config = notification_config().get("email") or {}
    release = _header_text(delivery["release_title"], "Music")
    artist = _header_text(delivery["artist_name"], "your requested artist")
    username = _header_text(delivery.get("username") if hasattr(delivery, "get") else delivery["username"], "there")
    base_url = str(notification_config().get("applicationUrl") or "").rstrip("/")
    path = f"/albums/{delivery['release_mbid']}"
    destination = f"{base_url}{path}" if base_url else path
    message = EmailMessage()
    message["Subject"] = f"Music Now Available - {release} by {artist}"
    message["From"] = formataddr((_header_text(config.get("senderName"), "Melodarr"), config["sender"]))
    message["To"] = delivery["email_target"]
    test_line = "This is a representative Melodarr test notification.\n" if test else ""
    text = f"{test_line}{release} by {artist} is now available in Melodarr.\n{destination}\n"
    message.set_content(text)
    artwork = f"https://coverartarchive.org/release-group/{html.escape(str(delivery['release_mbid']), quote=True)}/front-500"
    escaped_release = html.escape(release)
    escaped_artist = html.escape(artist)
    escaped_user = html.escape(username)
    escaped_destination = html.escape(destination, quote=True)
    test_copy = "<p style=\"margin:0 0 18px;color:#d9d2cf;font:14px/1.55 Arial,sans-serif\">This is a representative Melodarr test notification; it does not indicate that a request became available.</p>" if test else ""
    markup = f'''<!doctype html><html><body style="margin:0;padding:0;background:#16131b"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="width:100%;background:#16131b"><tr><td align="center" style="padding:32px 16px"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:640px;background:#241f2b;border-radius:20px;overflow:hidden"><tr><td align="center" style="padding:34px 28px 22px;background:#30293a"><img src="cid:melodarr-logo" width="96" height="96" alt="Melodarr" style="display:block;border:0;max-width:96px;height:auto"><div style="margin-top:12px;color:#fff7f2;font:700 30px/1 Arial,sans-serif;letter-spacing:-1px">Melodarr</div></td></tr><tr><td style="padding:32px 28px"><h1 style="margin:0 0 12px;color:#fff7f2;font:700 28px/1.2 Arial,sans-serif">Hi, {escaped_user}!</h1><p style="margin:0 0 22px;color:#d9d2cf;font:16px/1.55 Arial,sans-serif">Great news — music you care about is now available in your library.</p>{test_copy}<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="width:100%;border:1px solid #51465d;border-radius:14px;background:#1d1923"><tr><td style="padding:20px;vertical-align:middle"><div style="color:#c4b6d8;font:700 12px/1.2 Arial,sans-serif;letter-spacing:1px;text-transform:uppercase">Now available</div><div style="margin-top:8px;color:#fff7f2;font:700 21px/1.25 Arial,sans-serif">{escaped_release}</div><div style="margin-top:5px;color:#d9d2cf;font:15px/1.4 Arial,sans-serif">{escaped_artist}</div></td><td width="132" style="padding:12px 12px 12px 0;vertical-align:middle"><img src="{artwork}" width="120" height="120" alt="Cover art for {escaped_release}" style="display:block;width:120px;height:120px;max-width:100%;border:0;border-radius:10px;background:#51465d;color:#fff7f2;font:12px Arial,sans-serif;object-fit:cover"></td></tr></table><table role="presentation" cellpadding="0" cellspacing="0" style="margin:26px auto 0"><tr><td align="center" bgcolor="#c7593a" style="border-radius:999px"><a href="{escaped_destination}" style="display:inline-block;padding:15px 28px;color:#fffaf7;font:700 16px Arial,sans-serif;text-decoration:none">Open in Melodarr</a></td></tr></table></td></tr></table></td></tr></table></body></html>'''
    message.add_alternative(markup, subtype="html")
    logo_path = Path(__file__).resolve().parents[2] / "frontend" / "icons" / "melodarr-180.png"
    try:
        logo = logo_path.read_bytes()
    except OSError:
        logo = b""
    if logo:
        message.get_payload()[-1].add_related(logo, maintype="image", subtype="png", cid="<melodarr-logo>", filename="melodarr-180.png")
    return message


def _email(delivery, *, test=False):
    config = notification_config().get("email") or {}
    message = build_email_message(delivery, test=test)
    cls = smtplib.SMTP_SSL if config.get("encryption") == "tls" else smtplib.SMTP
    tls_context = ssl.create_default_context()
    with (cls(config["host"], int(config.get("port") or 587), timeout=20, context=tls_context)
          if cls is smtplib.SMTP_SSL else cls(config["host"], int(config.get("port") or 587), timeout=20)) as server:
        if config.get("encryption") == "starttls":
            server.starttls(context=tls_context)
        if config.get("username"):
            server.login(config["username"], config.get("password") or "")
        server.send_message(message)


def _push(delivery, *, test=False):
    if not delivery["push_endpoint"]:
        raise RuntimeError("Push delivery snapshot is missing")
    try:
        from pywebpush import WebPushException, webpush
    except ImportError as exc:
        raise RuntimeError("pywebpush is not installed") from exc
    config = notification_config().get("webPush") or {}
    payload = json.dumps(
        {"title": "Melodarr test notification", "body": "This is a representative test notification. No music availability changed.", "url": "/"}
        if test else {"title": f"{delivery['release_title']} is now available", "body": f"{delivery['artist_name']} is now available in Melodarr.", "url": f"/albums/{delivery['release_mbid']}"}
    )
    session = NoRedirectSession()
    try:
        webpush({"endpoint": delivery["push_endpoint"], "keys": {"p256dh": delivery["push_p256dh"], "auth": delivery["push_auth"]}}, payload, vapid_private_key=VAPID_PRIVATE_KEY_FILE, vapid_claims={"sub": config["contact"]}, ttl=WEB_PUSH_TTL_SECONDS, requests_session=session, timeout=20)
    except WebPushException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status in {404, 410}:
            exc.expired_subscription = True
        raise
    finally:
        session.close()


def _channel_enabled(channel):
    master, email_enabled, push_enabled = global_channels()
    return master and (email_enabled if channel == "email" else push_enabled)


def pause_delivery(delivery):
    with db() as connection:
        connection.execute("""UPDATE notification_deliveries SET status='pending', attempts=MAX(attempts-1, 0),
            lease_token=NULL, lease_until=NULL, updated_at=? WHERE id=? AND lease_token=?""", (time.time(), delivery["id"], delivery["lease_token"]))


def complete_delivery(delivery, *, error=None, expired_subscription=False):
    now = time.time()
    with db() as connection:
        if error is None:
            connection.execute("UPDATE notification_deliveries SET status='sent', lease_token=NULL, lease_until=NULL, last_error=NULL, updated_at=? WHERE id=? AND lease_token=?", (now, delivery["id"], delivery["lease_token"]))
            return
        terminal = expired_subscription or delivery["attempts"] >= MAX_ATTEMPTS
        delay = min(30 * (2 ** min(max(delivery["attempts"] - 1, 0), 6)), 3600)
        updated = connection.execute("""UPDATE notification_deliveries SET status=?, lease_token=NULL, lease_until=NULL,
            next_attempt_at=?, last_error=?, updated_at=? WHERE id=? AND lease_token=?""",
            ("dead" if terminal else "pending", now if terminal else now + delay, type(error).__name__[:120], now, delivery["id"], delivery["lease_token"]))
        if updated.rowcount and expired_subscription and delivery["subscription_id"]:
            connection.execute("DELETE FROM web_push_subscriptions WHERE id=? AND user_id=? AND endpoint=?", (delivery["subscription_id"], delivery["user_id"], delivery["push_endpoint"]))


def process_one():
    delivery = claim_due_delivery()
    if not delivery:
        return False
    if not _channel_enabled(delivery["channel"]):
        pause_delivery(delivery)
        return False
    try:
        if delivery["channel"] == "email":
            _email(delivery)
        else:
            _push(delivery)
    except Exception as exc:  # provider errors are intentionally reduced before persisting
        complete_delivery(delivery, error=exc, expired_subscription=bool(getattr(exc, "expired_subscription", False)))
    else:
        complete_delivery(delivery)
    return True


def run():
    while True:
        while process_one():
            pass
        wake_requested.wait(POLL_INTERVAL)
        wake_requested.clear()
