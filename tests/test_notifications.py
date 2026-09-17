if __package__:
    from ._test_environment import TEST_ROOT
else:
    from _test_environment import TEST_ROOT

import json
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from backend import notifications, storage
from backend.workers import notifications as notification_worker

try:
    from backend.application import create_app
except ImportError:  # Keep storage-only tests usable in the compile runtime.
    create_app = None


class NotificationStorageTests(unittest.TestCase):
    def setUp(self):
        storage.init_db()
        with storage.db() as connection:
            for table in ("notification_deliveries", "notification_events", "release_availability_state", "notification_mutes", "web_push_subscriptions", "user_notification_preferences", "request_history", "users"):
                connection.execute(f"DELETE FROM {table}")
            connection.execute("INSERT INTO users (username,password_hash,role,created_at) VALUES ('user','x','user',0)")
            self.user_id = connection.execute("SELECT id FROM users").fetchone()["id"]
            connection.execute("INSERT INTO user_notification_preferences (user_id,enabled,email_enabled,web_push_enabled,requested_available,all_new_music,updated_at) VALUES (?,1,1,0,1,0,0)", (self.user_id,))
            connection.execute("INSERT INTO request_history (user_id,kind,mbid,name,created_at) VALUES (?,'release-group','11111111-1111-1111-1111-111111111111','Album',0)", (self.user_id,))

    def test_first_scan_silent_then_false_true_creates_one_event(self):
        album = {"fullyAvailable": True, "title": "Album", "artistMbid": "22222222-2222-2222-2222-222222222222", "artistName": "Artist"}
        with patch("backend.notifications.global_channels", return_value=(True, True, False)):
            self.assertEqual(notifications.observe_availability({"11111111-1111-1111-1111-111111111111": album}), 0)
            album["fullyAvailable"] = False
            notifications.observe_availability({"11111111-1111-1111-1111-111111111111": album})
            album["fullyAvailable"] = True
            self.assertEqual(notifications.observe_availability({"11111111-1111-1111-1111-111111111111": album}), 1)
            self.assertEqual(notifications.observe_availability({"11111111-1111-1111-1111-111111111111": album}), 0)
        with storage.db() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM notification_events").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM notification_deliveries").fetchone()[0], 0)

    def test_request_alerts_only_other_enabled_admins(self):
        with storage.db() as connection:
            connection.execute(
                "UPDATE users SET role='admin', plex_email='requester@example.test' "
                "WHERE id=?",
                (self.user_id,),
            )
            other_admin_id = connection.execute(
                "INSERT INTO users "
                "(username,password_hash,role,plex_email,created_at) "
                "VALUES ('other-admin','x','admin','other@example.test',0)"
            ).lastrowid
            regular_user_id = connection.execute(
                "INSERT INTO users "
                "(username,password_hash,role,plex_email,created_at) "
                "VALUES ('listener','x','user','listener@example.test',0)"
            ).lastrowid
            for user_id in (other_admin_id, regular_user_id):
                connection.execute(
                    "INSERT INTO user_notification_preferences "
                    "(user_id,enabled,email_enabled,web_push_enabled,"
                    "requested_available,all_new_music,updated_at) "
                    "VALUES (?,1,1,0,1,0,0)",
                    (user_id,),
                )

        with patch(
            "backend.notifications.global_channels",
            return_value=(True, True, False),
        ), patch("backend.workers.notifications.wake_requested.set") as wake:
            queued = notifications.queue_admin_request(
                self.user_id,
                "user",
                "11111111-1111-1111-1111-111111111111",
                "Album",
                "Artist",
            )

        self.assertEqual(queued, 1)
        wake.assert_called_once_with()
        with storage.db() as connection:
            event = connection.execute(
                "SELECT * FROM notification_events"
            ).fetchone()
            deliveries = connection.execute(
                "SELECT user_id,email_target FROM notification_deliveries"
            ).fetchall()
        self.assertEqual(event["event_type"], "request")
        self.assertEqual(event["requester_username"], "user")
        self.assertLess(event["generation"], 0)
        self.assertEqual(
            [(row["user_id"], row["email_target"]) for row in deliveries],
            [(other_admin_id, "other@example.test")],
        )

        with storage.db() as connection:
            connection.execute(
                "UPDATE user_notification_preferences "
                "SET admin_request_notifications=0 WHERE user_id=?",
                (other_admin_id,),
            )
            connection.execute("DELETE FROM notification_events")
        with patch(
            "backend.notifications.global_channels",
            return_value=(True, True, False),
        ):
            queued = notifications.queue_admin_request(
                self.user_id,
                "user",
                "11111111-1111-1111-1111-111111111111",
                "Album",
                "Artist",
            )
        self.assertEqual(queued, 0)
        with storage.db() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM notification_events"
                ).fetchone()[0],
                0,
            )

    def test_missing_scan_entry_is_unknown(self):
        album = {"fullyAvailable": False, "title": "Album"}
        with patch("backend.notifications.global_channels", return_value=(False, False, False)):
            notifications.observe_availability({"11111111-1111-1111-1111-111111111111": album})
            notifications.observe_availability({})
        with storage.db() as connection:
            self.assertEqual(connection.execute("SELECT fully_available FROM release_availability_state").fetchone()[0], 0)

    def test_availability_notification_waits_for_configured_delay(self):
        album = {
            "fullyAvailable": False,
            "title": "Album",
            "artistMbid": "22222222-2222-2222-2222-222222222222",
            "artistName": "Artist",
        }
        with storage.db() as connection:
            connection.execute(
                "UPDATE user_notification_preferences "
                "SET notification_email='user@example.test' WHERE user_id=?",
                (self.user_id,),
            )
        with patch(
            "backend.notifications.global_channels",
            return_value=(True, True, False),
        ), patch(
            "backend.notifications.notification_config",
            return_value={"delaySeconds": 2},
        ), patch("backend.notifications.time.time", return_value=1000):
            notifications.observe_availability(
                {"11111111-1111-1111-1111-111111111111": album}
            )
            album["fullyAvailable"] = True
            notifications.observe_availability(
                {"11111111-1111-1111-1111-111111111111": album}
            )

        with storage.db() as connection:
            delivery = connection.execute(
                "SELECT next_attempt_at FROM notification_deliveries"
            ).fetchone()
        self.assertEqual(delivery["next_attempt_at"], 1002)
        with patch(
            "backend.workers.notifications.global_channels",
            return_value=(True, True, False),
        ), patch("backend.workers.notifications.time.time", return_value=1001):
            self.assertIsNone(notification_worker.claim_due_delivery())
        with patch(
            "backend.workers.notifications.global_channels",
            return_value=(True, True, False),
        ), patch("backend.workers.notifications.time.time", return_value=1002):
            self.assertIsNotNone(notification_worker.claim_due_delivery())

    def test_lease_is_claimed_before_sender_and_retries(self):
        with storage.db() as connection:
            event = connection.execute("INSERT INTO notification_events (release_mbid,generation,artist_mbid,artist_name,release_title,created_at) VALUES ('11111111-1111-1111-1111-111111111111',1,'','Artist','Album',0)")
            connection.execute("INSERT INTO notification_deliveries (event_id,user_id,channel,email_target,status,attempts,next_attempt_at,created_at,updated_at) VALUES (?,?,'email','u@example.test','pending',0,0,0,0)", (event.lastrowid, self.user_id))
        with patch.object(notification_worker, "_email", side_effect=RuntimeError("nope")):
            self.assertTrue(notification_worker.process_one())
        with storage.db() as connection:
            row = connection.execute("SELECT status,attempts,lease_token FROM notification_deliveries").fetchone()
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["attempts"], 1)
        self.assertIsNone(row["lease_token"])

    def test_disabled_channels_pause_due_work_without_attempts(self):
        with storage.db() as connection:
            event = connection.execute("INSERT INTO notification_events (release_mbid,generation,artist_mbid,artist_name,release_title,created_at) VALUES ('11111111-1111-1111-1111-111111111111',1,'','Artist','Album',0)")
            connection.execute("INSERT INTO notification_deliveries (event_id,user_id,channel,email_target,status,attempts,next_attempt_at,created_at,updated_at) VALUES (?,?,'email','u@example.test','pending',0,0,0,0)", (event.lastrowid, self.user_id))
        with patch("backend.workers.notifications.global_channels", return_value=(False, True, True)):
            self.assertIsNone(notification_worker.claim_due_delivery())
        with storage.db() as connection:
            self.assertEqual(connection.execute("SELECT attempts,status FROM notification_deliveries").fetchone()["attempts"], 0)
        with patch("backend.workers.notifications.global_channels", return_value=(True, False, True)):
            self.assertIsNone(notification_worker.claim_due_delivery())
        with storage.db() as connection:
            row = connection.execute("SELECT attempts,status,lease_token FROM notification_deliveries").fetchone()
            self.assertEqual((row["attempts"], row["status"], row["lease_token"]), (0, "pending", None))
        with patch("backend.workers.notifications.global_channels", return_value=(True, True, True)):
            self.assertIsNotNone(notification_worker.claim_due_delivery())

    def test_post_claim_channel_disable_releases_lease_without_sending(self):
        with storage.db() as connection:
            event = connection.execute("INSERT INTO notification_events (release_mbid,generation,artist_mbid,artist_name,release_title,created_at) VALUES ('11111111-1111-1111-1111-111111111111',1,'','Artist','Album',0)")
            connection.execute("INSERT INTO notification_deliveries (event_id,user_id,channel,email_target,status,attempts,next_attempt_at,created_at,updated_at) VALUES (?,?,'email','u@example.test','pending',0,0,0,0)", (event.lastrowid, self.user_id))
        with patch("backend.workers.notifications.global_channels", side_effect=[(True, True, True), (False, True, True)]), patch.object(notification_worker, "_email") as email:
            self.assertFalse(notification_worker.process_one())
            email.assert_not_called()
        with storage.db() as connection:
            row = connection.execute("SELECT status,attempts,lease_token FROM notification_deliveries").fetchone()
        self.assertEqual((row["status"], row["attempts"], row["lease_token"]), ("pending", 0, None))

    def test_run_recovers_after_transient_claim_failure_without_logging_error_details(self):
        with patch.object(
            notification_worker,
            "claim_due_delivery",
            side_effect=[sqlite3.OperationalError("claim secret"), None],
        ), patch.object(notification_worker.wake_requested, "wait", side_effect=[False, KeyboardInterrupt]) as wait, patch.object(
            notification_worker,
            "process_one",
            wraps=notification_worker.process_one,
        ) as process_one, self.assertLogs(notification_worker.logger, level="WARNING") as logs:
            with self.assertRaises(KeyboardInterrupt):
                notification_worker.run()
        self.assertEqual(process_one.call_count, 2)
        self.assertEqual(
            [call.args[0] for call in wait.call_args_list],
            [notification_worker.FAILURE_BACKOFF_INITIAL_SECONDS, notification_worker.POLL_INTERVAL],
        )
        self.assertIn("Notification delivery worker pass failed; retrying after 1 seconds", logs.output[0])
        self.assertNotIn("claim secret", logs.output[0])

    def test_run_survives_completion_persistence_failure_and_leaves_lease_reclaimable(self):
        with storage.db() as connection:
            event = connection.execute("INSERT INTO notification_events (release_mbid,generation,artist_mbid,artist_name,release_title,created_at) VALUES ('11111111-1111-1111-1111-111111111111',1,'','Artist','Album',0)")
            connection.execute("INSERT INTO notification_deliveries (event_id,user_id,channel,email_target,status,attempts,next_attempt_at,created_at,updated_at) VALUES (?,?,'email','u@example.test','pending',0,0,0,0)", (event.lastrowid, self.user_id))
        with patch.object(notification_worker, "_email") as email, patch.object(
            notification_worker,
            "complete_delivery",
            side_effect=sqlite3.OperationalError("completion secret"),
        ), patch.object(notification_worker.wake_requested, "wait", side_effect=[False, KeyboardInterrupt]) as wait, patch.object(
            notification_worker,
            "process_one",
            wraps=notification_worker.process_one,
        ) as process_one, self.assertLogs(notification_worker.logger, level="WARNING") as logs:
            with self.assertRaises(KeyboardInterrupt):
                notification_worker.run()
        self.assertEqual(process_one.call_count, 2)
        self.assertEqual([call.args[0] for call in wait.call_args_list], [notification_worker.FAILURE_BACKOFF_INITIAL_SECONDS, notification_worker.POLL_INTERVAL])
        email.assert_called_once()
        self.assertNotIn("completion secret", logs.output[0])
        with storage.db() as connection:
            row = connection.execute("SELECT status, attempts, lease_token FROM notification_deliveries").fetchone()
        self.assertEqual((row["status"], row["attempts"]), ("leased", 1))
        self.assertIsNotNone(row["lease_token"])

    def test_provider_and_enable_device_contracts_are_explicit(self):
        root = Path(__file__).parents[1]
        worker = (root / "backend" / "workers" / "notifications.py").read_text(encoding="utf-8")
        app = (root / "frontend" / "src" / "app.ts").read_text(encoding="utf-8")
        self.assertIn("ttl=WEB_PUSH_TTL_SECONDS", worker)
        self.assertIn("requests_session=session", worker)
        self.assertIn("allow_redirects", worker)
        self.assertIn("ssl.create_default_context", worker)
        self.assertIn("pushManager.getSubscription", app)
        self.assertIn("webPushEnabled: true", app)

    def test_notification_frontend_has_standalone_settings_and_preference_contracts(self):
        root = Path(__file__).parents[1]
        html = (root / "frontend" / "static" / "index.html").read_text(encoding="utf-8")
        app = (root / "frontend" / "src" / "app.ts").read_text(encoding="utf-8")
        discovery = (root / "frontend" / "src" / "discovery.ts").read_text(encoding="utf-8")
        style = (root / "frontend" / "src" / "style.css").read_text(encoding="utf-8")

        self.assertIn('data-settings-page="notifications">Notifications', html)
        self.assertIn('id="settings-notifications"', html)
        services = html[html.index('id="settings-services"'):html.index('id="settings-notifications"')]
        self.assertNotIn('id="notification-settings"', services)
        self.assertIn('"/settings/notifications"', app)
        self.assertIn('"settings/notifications"', app)

        notification_panel = html[html.index('id="settings-notifications"'):html.index('id="settings-requests"')]
        expected_admin_order = [
            "Application URL",
            "Notification Delay (seconds)",
            "Enable Notifications Globally",
            "Enable Email Delivery",
            "SMTP Host",
            "SMTP Port",
            "Encryption",
            "Username",
            "Sender Name",
            "Sender Address",
            "Password",
            "Enable Web Push Delivery",
            "VAPID Contact",
        ]
        self.assertEqual(sorted(expected_admin_order, key=notification_panel.index), expected_admin_order)
        self.assertIn('/icons/email.svg', notification_panel)
        self.assertIn('/icons/web-push.svg', notification_panel)
        self.assertIn('id="notification-global-settings"', notification_panel)
        self.assertIn('id="notification-email-settings"', notification_panel)
        self.assertIn('id="notification-web-push-settings"', notification_panel)
        self.assertIn("Save Global Settings", notification_panel)
        self.assertIn("Save Email Settings", notification_panel)
        self.assertIn("Save Web Push Settings", notification_panel)
        self.assertEqual(notification_panel.count('class="notification-save"'), 3)
        self.assertIn(".notification-settings-form .notification-save", style)
        self.assertIn('service-intro notification-intro', notification_panel)
        self.assertIn('.notification-intro { grid-template-columns: minmax(0, 1fr) auto; }', style)
        self.assertIn('.notification-intro { grid-template-columns: minmax(0, 1fr); }', style)

        self.assertIn('name="musicNotifications"', app)
        self.assertIn('Requested Music', app)
        self.assertIn('All Music', app)
        self.assertIn('prefs.allNewMusic ? "all" : "requested"', app)
        self.assertEqual(app.count('requestedAvailable: false, allNewMusic: true'), 1)
        self.assertEqual(app.count('requestedAvailable: true, allNewMusic: false'), 1)
        self.assertEqual(app.count('...musicPreferencePayload()'), 2)
        self.assertIn('name="adminRequestNotifications"', app)
        self.assertIn('user.role === "admin" ? `<fieldset', app)
        self.assertEqual(app.count('...adminRequestPreferencePayload()'), 2)
        self.assertNotIn('Requested music when available', app)
        self.assertNotIn('All newly available music', app)
        self.assertNotIn('Choose how Melodarr contacts you.', app)

        self.assertIn('/icons/bell-alert.svg', discovery)
        self.assertIn('/icons/bell-snooze.svg', discovery)
        self.assertIn('bell.setAttribute("aria-pressed", String(muted))', discovery)
        self.assertIn('addMuteButton(requiredDescendant<HTMLElement>(meta, ".external-icons")', discovery)
        self.assertNotIn('addMuteButton(actions', discovery)
        self.assertIn(':is(.notification-card legend img, .external-icons .notification-mute img) { filter:', style)
        self.assertIn("pushDeviceMetadata", app)
        self.assertIn("Manage Devices", app)

    def test_smtp_uses_verified_context_for_tls_and_starttls(self):
        delivery = {"release_title": "Album", "artist_name": "Artist", "release_mbid": "11111111-1111-1111-1111-111111111111", "email_target": "u@example.test"}
        context = Mock()
        tls_server, plain_server = MagicMock(), MagicMock()
        tls_server.__enter__.return_value = tls_server
        plain_server.__enter__.return_value = plain_server
        with patch("backend.workers.notifications.notification_config", return_value={"applicationUrl": "", "email": {"sender": "sender@example.test", "host": "smtp.example", "port": 465, "encryption": "tls"}}), patch("backend.workers.notifications.ssl.create_default_context", return_value=context), patch("backend.workers.notifications.smtplib.SMTP_SSL", return_value=tls_server) as smtp_ssl:
            notification_worker._email(delivery)
            self.assertIs(smtp_ssl.call_args.kwargs["context"], context)
        with patch("backend.workers.notifications.notification_config", return_value={"applicationUrl": "", "email": {"sender": "sender@example.test", "host": "smtp.example", "port": 587, "encryption": "starttls"}}), patch("backend.workers.notifications.ssl.create_default_context", return_value=context), patch("backend.workers.notifications.smtplib.SMTP", return_value=plain_server):
            notification_worker._email(delivery)
            plain_server.starttls.assert_called_once_with(context=context)

    def test_availability_email_is_branded_safe_and_multipart(self):
        delivery = {
            "release_title": "Release\r\nInjected", "artist_name": "Artist\nName",
            "release_mbid": "11111111-1111-1111-1111-111111111111", "email_target": "u@example.test",
            "username": "Ada <admin>",
        }
        with patch("backend.workers.notifications.notification_config", return_value={
            "applicationUrl": "https://melodarr.example", "email": {
                "host": "smtp.example", "sender": "sender@example.test", "senderName": "Melodarr\r\nTeam",
            },
        }):
            message = notification_worker.build_email_message(delivery)
        self.assertEqual(message["Subject"], "Music Now Available - Release Injected by Artist Name")
        self.assertIn("Melodarr Team <sender@example.test>", message["From"])
        self.assertTrue(message.is_multipart())
        rendered = message.get_body(preferencelist=("html",)).get_content()
        self.assertIn("Hi, Ada &lt;admin&gt;!", rendered)
        self.assertIn("cid:melodarr-logo", rendered)
        self.assertIn("coverartarchive.org/release-group/11111111-1111-1111-1111-111111111111/front-500", rendered)
        self.assertIn("Open in Melodarr", rendered)
        self.assertIn("Content-ID: <melodarr-logo>", message.as_string())

    def test_request_email_reuses_branded_template_and_names_requester(self):
        delivery = {
            "event_type": "request",
            "requester_username": "listener <one>",
            "release_title": "Requested Album",
            "artist_name": "Requested Artist",
            "release_mbid": "11111111-1111-1111-1111-111111111111",
            "email_target": "admin@example.test",
            "username": "admin",
        }
        with patch(
            "backend.workers.notifications.notification_config",
            return_value={
                "applicationUrl": "https://melodarr.example",
                "email": {
                    "host": "smtp.example",
                    "sender": "sender@example.test",
                    "senderName": "Melodarr",
                },
            },
        ):
            message = notification_worker.build_email_message(delivery)

        self.assertEqual(
            message["Subject"],
            "New Music Request - Requested Album by Requested Artist",
        )
        self.assertIn(
            "listener <one> requested Requested Album by Requested Artist.",
            message.get_body(preferencelist=("plain",)).get_content(),
        )
        rendered = message.get_body(preferencelist=("html",)).get_content()
        self.assertIn(
            "listener &lt;one&gt; requested Requested Album by Requested Artist.",
            rendered,
        )
        self.assertIn("New request", rendered)
        self.assertIn("cid:melodarr-logo", rendered)
        self.assertIn("Open in Melodarr", rendered)

    def test_partial_config_save_retains_sibling_sections_and_password(self):
        previous = notifications.notification_config()
        self.addCleanup(storage.save_service, "notifications", previous)
        storage.save_service("notifications", {
            "enabled": False, "applicationUrl": "https://old.example", "delaySeconds": 2,
            "email": {"enabled": False, "host": "smtp.old", "port": 587, "encryption": "starttls", "username": "old", "password": "secret", "sender": "old@example.test"},
            "webPush": {"enabled": False, "contact": "mailto:old@example.test"},
        })
        notifications.save_email_config({"enabled": True, "host": "smtp.new", "port": 465, "encryption": "tls", "username": "new", "senderName": "Melodarr", "sender": "new@example.test", "password": ""})
        saved = notifications.notification_config()
        self.assertEqual(saved["applicationUrl"], "https://old.example")
        self.assertEqual(saved["delaySeconds"], 2)
        self.assertEqual(saved["webPush"]["contact"], "mailto:old@example.test")
        self.assertEqual(saved["email"]["password"], "secret")
        self.assertEqual(saved["email"]["senderName"], "Melodarr")

    def test_push_uses_ttl_no_redirect_session_and_closes_it(self):
        delivery = {"push_endpoint": "https://fcm.googleapis.com/push", "push_p256dh": "key", "push_auth": "auth", "release_title": "Album", "artist_name": "Artist", "release_mbid": "11111111-1111-1111-1111-111111111111"}
        def send(*_args, **kwargs):
            kwargs["requests_session"].post("https://fcm.googleapis.com/push", allow_redirects=True)
        with patch("backend.workers.notifications.notification_config", return_value={"webPush": {"contact": "mailto:admin@example.test"}}), patch("pywebpush.webpush", side_effect=send) as webpush, patch.object(notification_worker.requests.Session, "post", return_value=Mock()) as post, patch.object(notification_worker.NoRedirectSession, "close") as close:
            notification_worker._push(delivery)
        self.assertEqual(webpush.call_args.kwargs["ttl"], 86400)
        self.assertFalse(post.call_args.kwargs["allow_redirects"])
        close.assert_called_once_with()

    def test_web_push_payload_copy_is_exact_for_each_notification_type(self):
        delivery = {
            "push_endpoint": "https://fcm.googleapis.com/push",
            "push_p256dh": "key",
            "push_auth": "auth",
            "release_title": "Album",
            "artist_name": "Artist",
            "release_mbid": "11111111-1111-1111-1111-111111111111",
        }
        cases = (
            (
                "availability",
                {},
                False,
                {
                    "title": "Album by Artist",
                    "body": "This Release Group is now available",
                    "url": "/albums/11111111-1111-1111-1111-111111111111",
                },
            ),
            (
                "request",
                {"event_type": "request", "requester_username": "listener"},
                False,
                {
                    "title": "Album by Artist",
                    "body": "listener requested a new release group",
                    "url": "/albums/11111111-1111-1111-1111-111111111111",
                },
            ),
            (
                "test",
                {},
                True,
                {
                    "title": "Test Notification",
                    "body": "This is a test notification. No new music added.",
                    "url": "/",
                },
            ),
        )
        for name, overrides, test, expected in cases:
            with self.subTest(name=name), patch(
                "backend.workers.notifications.notification_config",
                return_value={
                    "webPush": {"contact": "mailto:admin@example.test"}
                },
            ), patch("pywebpush.webpush") as webpush, patch.object(
                notification_worker.NoRedirectSession, "close"
            ):
                notification_worker._push({**delivery, **overrides}, test=test)
            self.assertEqual(json.loads(webpush.call_args.args[1]), expected)

    def test_artist_mute_overrides_matching_artist_request(self):
        release = "11111111-1111-1111-1111-111111111111"
        artist = "22222222-2222-2222-2222-222222222222"
        with storage.db() as connection:
            connection.execute("DELETE FROM request_history WHERE user_id=?", (self.user_id,))
            connection.execute("INSERT INTO request_history (user_id,kind,mbid,name,created_at) VALUES (?,'artist',?,'Artist',0)", (self.user_id, artist))
            connection.execute("INSERT INTO notification_mutes (user_id,kind,mbid,created_at) VALUES (?,'artist',?,0)", (self.user_id, artist))
        album = {"fullyAvailable": False, "title": "Album", "artistMbid": artist, "artistName": "Artist"}
        with patch("backend.notifications.global_channels", return_value=(True, True, False)):
            notifications.observe_availability({release: album})
            album["fullyAvailable"] = True
            notifications.observe_availability({release: album})
        with storage.db() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM notification_events").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM notification_deliveries").fetchone()[0], 0)

    def test_email_and_each_push_device_are_snapshotted(self):
        release = "11111111-1111-1111-1111-111111111111"
        artist = "22222222-2222-2222-2222-222222222222"
        with storage.db() as connection:
            connection.execute("UPDATE user_notification_preferences SET notification_email='user@example.test', email_enabled=1, web_push_enabled=1 WHERE user_id=?", (self.user_id,))
            connection.execute("INSERT INTO web_push_subscriptions (user_id,endpoint,p256dh,auth,created_at,updated_at) VALUES (?, 'https://push.example/one','key-one','auth-one',0,0)", (self.user_id,))
            connection.execute("INSERT INTO web_push_subscriptions (user_id,endpoint,p256dh,auth,created_at,updated_at) VALUES (?, 'https://push.example/two','key-two','auth-two',0,0)", (self.user_id,))
        album = {"fullyAvailable": False, "title": "Album", "artistMbid": artist, "artistName": "Artist"}
        with patch("backend.notifications.global_channels", return_value=(True, True, True)):
            notifications.observe_availability({release: album})
            album["fullyAvailable"] = True
            notifications.observe_availability({release: album})
        with storage.db() as connection:
            email = connection.execute("SELECT email_target FROM notification_deliveries WHERE channel='email'").fetchone()
            pushes = connection.execute("SELECT push_endpoint,push_p256dh,push_auth FROM notification_deliveries WHERE channel='web-push' ORDER BY push_endpoint").fetchall()
            self.assertEqual(email["email_target"], "user@example.test")
            self.assertEqual([(row["push_endpoint"], row["push_p256dh"], row["push_auth"]) for row in pushes], [("https://push.example/one", "key-one", "auth-one"), ("https://push.example/two", "key-two", "auth-two")])
            connection.execute("INSERT INTO users (username,password_hash,role,created_at) VALUES ('other','x','user',0)")
            other_id = connection.execute("SELECT id FROM users WHERE username='other'").fetchone()["id"]
            connection.execute("UPDATE web_push_subscriptions SET user_id=?, p256dh='replacement' WHERE endpoint='https://push.example/one'", (other_id,))
            original = connection.execute("SELECT push_p256dh,user_id FROM notification_deliveries WHERE push_endpoint='https://push.example/one'").fetchone()
        self.assertEqual(original["push_p256dh"], "key-one")
        self.assertEqual(original["user_id"], self.user_id)

    def test_service_worker_is_a_tracked_build_entry(self):
        root = Path(__file__).parents[1]
        source = root / "frontend" / "src" / "service-worker.ts"
        build = (root / "frontend" / "scripts" / "build.mjs").read_text(encoding="utf-8")
        self.assertTrue(source.is_file())
        self.assertIn('"src/service-worker.ts"', build)
        worker = source.read_text(encoding="utf-8")
        self.assertIn("new URL(raw, self.location.origin)", worker)
        self.assertIn("parsed.origin === self.location.origin", worker)
        self.assertIn("//", worker)

    def test_vapid_and_docker_build_context_exclude_private_key(self):
        root = Path(__file__).parents[1]
        self.assertIn("vapid-private.pem", (root / ".dockerignore").read_text(encoding="utf-8"))

    def test_expired_attempt_limit_dead_letters_before_reclaim(self):
        with storage.db() as connection:
            event = connection.execute("INSERT INTO notification_events (release_mbid,generation,artist_mbid,artist_name,release_title,created_at) VALUES ('11111111-1111-1111-1111-111111111111',1,'','Artist','Album',0)")
            connection.execute("INSERT INTO notification_deliveries (event_id,user_id,channel,email_target,status,attempts,next_attempt_at,lease_token,lease_until,created_at,updated_at) VALUES (?,?,'email','u@example.test','leased',?,0,'expired',0,0,0)", (event.lastrowid, self.user_id, notification_worker.MAX_ATTEMPTS))
        with patch("backend.workers.notifications.global_channels", return_value=(True, True, True)):
            self.assertIsNone(notification_worker.claim_due_delivery())
        with storage.db() as connection:
            self.assertEqual(connection.execute("SELECT status FROM notification_deliveries").fetchone()["status"], "dead")

    def test_concurrent_vapid_initialization_keeps_one_identity(self):
        try:
            import cryptography  # noqa: F401
        except ImportError:
            self.skipTest("cryptography is installed through pywebpush in production")
        with tempfile.TemporaryDirectory() as directory:
            key_path = os.path.join(directory, "vapid-private.pem")
            with patch("backend.notifications.VAPID_PRIVATE_KEY_FILE", key_path):
                keys = []
                threads = [threading.Thread(target=lambda: keys.append(notifications.ensure_vapid_key())) for _ in range(4)]
                for thread in threads: thread.start()
                for thread in threads: thread.join()
            self.assertEqual(len(set(keys)), 1)
            self.assertTrue(os.path.isfile(key_path))

    def test_expired_push_cleanup_is_scoped_and_delivery_becomes_dead(self):
        with storage.db() as connection:
            event = connection.execute("INSERT INTO notification_events (release_mbid,generation,artist_mbid,artist_name,release_title,created_at) VALUES ('11111111-1111-1111-1111-111111111111',1,'','Artist','Album',0)")
            subscription = connection.execute("INSERT INTO web_push_subscriptions (user_id,endpoint,p256dh,auth,created_at,updated_at) VALUES (?, 'https://push.example/expired','key','auth',0,0)", (self.user_id,))
            connection.execute("INSERT INTO notification_deliveries (event_id,user_id,channel,subscription_id,push_endpoint,push_p256dh,push_auth,status,attempts,next_attempt_at,lease_token,lease_until,created_at,updated_at) VALUES (?,?,'web-push',?,'https://push.example/expired','key','auth','leased',1,0,'lease',9999999999,0,0)", (event.lastrowid, self.user_id, subscription.lastrowid))
        with storage.db() as connection:
            delivery = connection.execute("SELECT * FROM notification_deliveries").fetchone()
        notification_worker.complete_delivery(delivery, error=RuntimeError("gone"), expired_subscription=True)
        with storage.db() as connection:
            self.assertIsNone(connection.execute("SELECT 1 FROM web_push_subscriptions WHERE id=?", (subscription.lastrowid,)).fetchone())
            self.assertEqual(connection.execute("SELECT status FROM notification_deliveries").fetchone()["status"], "dead")

    def test_stale_expired_worker_cannot_remove_reclaimed_subscription(self):
        with storage.db() as connection:
            event = connection.execute("INSERT INTO notification_events (release_mbid,generation,artist_mbid,artist_name,release_title,created_at) VALUES ('11111111-1111-1111-1111-111111111111',1,'','Artist','Album',0)")
            subscription = connection.execute("INSERT INTO web_push_subscriptions (user_id,endpoint,p256dh,auth,created_at,updated_at) VALUES (?, 'https://push.example/race','key','auth',0,0)", (self.user_id,))
            connection.execute("INSERT INTO notification_deliveries (event_id,user_id,channel,subscription_id,push_endpoint,push_p256dh,push_auth,status,attempts,next_attempt_at,lease_token,lease_until,created_at,updated_at) VALUES (?,?,'web-push',?,'https://push.example/race','key','auth','leased',1,0,'new-lease',9999999999,0,0)", (event.lastrowid, self.user_id, subscription.lastrowid))
            stale = dict(connection.execute("SELECT * FROM notification_deliveries").fetchone())
            stale["lease_token"] = "old-lease"
        notification_worker.complete_delivery(stale, error=RuntimeError("gone"), expired_subscription=True)
        with storage.db() as connection:
            self.assertIsNotNone(connection.execute("SELECT 1 FROM web_push_subscriptions WHERE id=?", (subscription.lastrowid,)).fetchone())
            self.assertEqual(connection.execute("SELECT lease_token FROM notification_deliveries").fetchone()["lease_token"], "new-lease")

    def test_global_config_redacts_smtp_password(self):
        storage.save_service("notifications", {"enabled": True, "applicationUrl": "", "email": {"enabled": True, "host": "smtp.example.test", "port": 587, "encryption": "starttls", "username": "mail", "password": "secret", "sender": "sender@example.test"}, "webPush": {"enabled": False, "contact": ""}})
        public = notifications.public_config()
        self.assertNotIn("password", public["email"])
        self.assertTrue(public["email"]["passwordConfigured"])

    def test_notification_delay_is_validated_and_public(self):
        previous = notifications.notification_config()
        self.addCleanup(storage.save_service, "notifications", previous)
        base = {
            "enabled": False,
            "applicationUrl": "",
            "email": {
                "enabled": False,
                "host": "",
                "port": 587,
                "encryption": "starttls",
                "username": "",
                "password": "",
                "sender": "",
                "senderName": "",
            },
            "webPush": {"enabled": False, "contact": ""},
        }
        saved = notifications.save_config({**base, "delaySeconds": "2"})
        self.assertEqual(saved["delaySeconds"], 2)
        capped = notifications.save_config({**base, "delaySeconds": 86400})
        self.assertEqual(capped["delaySeconds"], 86400)
        for value in (-1, 86401, 1.5, True, "later"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                notifications.save_config({**base, "delaySeconds": value})

    def test_web_push_contact_requires_a_real_bounded_destination(self):
        base = {"enabled": False, "applicationUrl": "", "email": {"enabled": False, "host": "", "port": 587, "encryption": "starttls", "username": "", "password": "", "sender": ""}}
        for contact in ("https://", "mailto:not-an-email", "https://" + "a" * 600):
            with self.subTest(contact=contact):
                with self.assertRaises(ValueError):
                    notifications.save_config({**base, "webPush": {"enabled": False, "contact": contact}})


@unittest.skipIf(create_app is None, "Flask dependency is not installed in this narrow runtime")
class NotificationRouteTests(unittest.TestCase):
    def setUp(self):
        storage.init_db()
        with storage.db() as connection:
            for table in ("notification_deliveries", "notification_events", "release_availability_state", "notification_mutes", "web_push_subscriptions", "user_notification_preferences", "request_history", "account_invitations", "users"):
                connection.execute(f"DELETE FROM {table}")
            admin = connection.execute("INSERT INTO users (username,password_hash,role,created_at) VALUES ('admin','x','admin',0)")
            user = connection.execute("INSERT INTO users (username,password_hash,role,created_at) VALUES ('user','x','user',0)")
            other = connection.execute("INSERT INTO users (username,password_hash,role,created_at) VALUES ('other','x','user',0)")
            self.admin_id, self.user_id, self.other_id = admin.lastrowid, user.lastrowid, other.lastrowid
        self.app = create_app({"TESTING": True, "SECRET_KEY": "notification-routes"})
        self.client = self.app.test_client()

    def _login_as(self, user_id):
        with self.client.session_transaction() as session:
            session["user_id"] = user_id
            session["csrf_token"] = "csrf"

    def test_auth_redaction_and_private_device_ownership(self):
        self.assertEqual(self.client.get("/api/account/notifications").status_code, 401)
        self._login_as(self.user_id)
        self.assertEqual(self.client.get("/api/settings/notifications").status_code, 403)
        with storage.db() as connection:
            subscription = connection.execute("INSERT INTO web_push_subscriptions (user_id,endpoint,p256dh,auth,created_at,updated_at) VALUES (?, 'https://push.example/other','key','auth',0,0)", (self.other_id,))
        rejected = self.client.delete(f"/api/account/notifications/subscriptions/{subscription.lastrowid}", headers={"X-CSRF-Token": "csrf"})
        self.assertEqual(rejected.status_code, 404)
        own_preferences = self.client.get("/api/account/notifications").get_json()
        self.assertEqual(own_preferences["devices"], [])
        self._login_as(self.admin_id)
        storage.save_service("notifications", {"enabled": True, "applicationUrl": "", "email": {"enabled": True, "host": "smtp.example", "port": 587, "encryption": "starttls", "username": "u", "password": "secret", "sender": "sender@example.test"}, "webPush": {"enabled": False, "contact": ""}})
        public = self.client.get("/api/settings/notifications")
        self.assertEqual(public.status_code, 200)
        self.assertNotIn("password", public.get_json()["email"])

    def test_settings_notifications_direct_load_returns_frontend_without_cache(self):
        response = self.client.get("/settings/notifications")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "text/html")
        self.assertEqual(response.headers["Cache-Control"], "no-cache")
        self.assertIn(b'id="settings-notifications"', response.data)

    def test_admin_request_alert_preference_is_admin_only_and_persisted(self):
        payload = {
            "enabled": True,
            "emailEnabled": True,
            "webPushEnabled": False,
            "requestedAvailable": True,
            "allNewMusic": False,
            "adminRequestNotifications": False,
            "notificationEmail": "",
        }
        headers = {"X-CSRF-Token": "csrf"}

        self._login_as(self.admin_id)
        admin_response = self.client.put(
            "/api/account/notifications", json=payload, headers=headers
        )
        self.assertEqual(admin_response.status_code, 200)
        self.assertFalse(admin_response.get_json()["adminRequestNotifications"])

        self._login_as(self.user_id)
        user_response = self.client.put(
            "/api/account/notifications",
            json={**payload, "adminRequestNotifications": True},
            headers=headers,
        )
        self.assertEqual(user_response.status_code, 200)
        self.assertFalse(user_response.get_json()["adminRequestNotifications"])
        with storage.db() as connection:
            rows = connection.execute(
                "SELECT user_id,admin_request_notifications "
                "FROM user_notification_preferences "
                "WHERE user_id IN (?,?) ORDER BY user_id",
                (self.admin_id, self.user_id),
            ).fetchall()
        self.assertEqual(
            [(row["user_id"], row["admin_request_notifications"]) for row in rows],
            # A normal user cannot alter the dormant preference. If promoted
            # later, the administrator default remains enabled.
            [(self.admin_id, 0), (self.user_id, 1)],
        )

    def test_subscription_endpoint_allowlist_blocks_ssrf(self):
        self._login_as(self.user_id)
        headers = {"X-CSRF-Token": "csrf"}
        valid = {"endpoint": "https://fcm.googleapis.com/fcm/send/token", "keys": {"p256dh": "key", "auth": "auth"}}
        self.assertEqual(self.client.post("/api/account/notifications/subscriptions", json=valid, headers=headers).status_code, 201)
        for endpoint in ("https://localhost/push", "https://127.0.0.1/push", "https://example.test/push", "https://user@fcm.googleapis.com/push", "https://fcm.googleapis.com:8443/push"):
            with self.subTest(endpoint=endpoint):
                body = {**valid, "endpoint": endpoint}
                self.assertEqual(self.client.post("/api/account/notifications/subscriptions", json=body, headers=headers).status_code, 400)

    def test_partial_notification_routes_reject_sibling_fields_without_clobbering(self):
        self._login_as(self.admin_id)
        headers = {"X-CSRF-Token": "csrf"}
        storage.save_service("notifications", {
            "enabled": False, "applicationUrl": "https://old.example", "delaySeconds": 2,
            "email": {"enabled": False, "host": "smtp.old", "port": 587, "encryption": "starttls", "username": "old", "password": "secret", "sender": "old@example.test"},
            "webPush": {"enabled": False, "contact": "mailto:old@example.test"},
        })
        rejected = [
            ("/api/settings/notifications/global", {"enabled": True, "email": {}}),
            ("/api/settings/notifications/global", {"enabled": True, "unexpected": "value"}),
            ("/api/settings/notifications/email", {"enabled": True, "webPush": {}}),
            ("/api/settings/notifications/web-push", {"enabled": True, "applicationUrl": "https://bad.example"}),
        ]
        for path, payload in rejected:
            with self.subTest(path=path):
                self.assertEqual(self.client.put(path, json=payload, headers=headers).status_code, 400)
        saved = notifications.notification_config()
        self.assertEqual(saved["applicationUrl"], "https://old.example")
        self.assertEqual(saved["delaySeconds"], 2)
        self.assertEqual(saved["email"]["host"], "smtp.old")
        self.assertEqual(saved["webPush"]["contact"], "mailto:old@example.test")

    def test_global_notification_route_saves_delay(self):
        self._login_as(self.admin_id)
        response = self.client.put(
            "/api/settings/notifications/global",
            json={
                "enabled": False,
                "applicationUrl": "",
                "delaySeconds": "2",
            },
            headers={"X-CSRF-Token": "csrf"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["delaySeconds"], 2)
        self.assertEqual(notifications.notification_config()["delaySeconds"], 2)

    def test_web_push_test_targets_all_and_only_current_admin_devices(self):
        self._login_as(self.admin_id)
        headers = {"X-CSRF-Token": "csrf"}
        storage.save_service("notifications", {"enabled": False, "applicationUrl": "", "email": {"enabled": False, "host": "", "port": 587, "encryption": "starttls", "username": "", "password": "", "sender": ""}, "webPush": {"enabled": False, "contact": "mailto:admin@example.test"}})
        with storage.db() as connection:
            for endpoint, user_id in (("https://push.example/admin-one", self.admin_id), ("https://push.example/admin-two", self.admin_id), ("https://push.example/other", self.other_id)):
                connection.execute("INSERT INTO web_push_subscriptions (user_id,endpoint,p256dh,auth,created_at,updated_at) VALUES (?,?,?,?,0,0)", (user_id, endpoint, "key", "auth"))
        with patch("backend.routes.notifications.notifications.ensure_vapid_key"), patch("backend.workers.notifications._push") as push:
            response = self.client.post("/api/settings/notifications/web-push/test", headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertIn("2 registered devices", response.get_json()["message"])
        self.assertEqual([call.args[0]["push_endpoint"] for call in push.call_args_list], ["https://push.example/admin-one", "https://push.example/admin-two"])
        self.assertTrue(all(call.kwargs["test"] for call in push.call_args_list))

    def test_web_push_test_attempts_every_owned_device_after_provider_failure(self):
        self._login_as(self.admin_id)
        headers = {"X-CSRF-Token": "csrf"}
        storage.save_service("notifications", {"enabled": False, "applicationUrl": "", "email": {"enabled": False, "host": "", "port": 587, "encryption": "starttls", "username": "", "password": "", "sender": ""}, "webPush": {"enabled": False, "contact": "mailto:admin@example.test"}})
        with storage.db() as connection:
            for endpoint, user_id in (("https://push.example/admin-one", self.admin_id), ("https://push.example/admin-two", self.admin_id), ("https://push.example/admin-three", self.admin_id), ("https://push.example/other", self.other_id)):
                connection.execute("INSERT INTO web_push_subscriptions (user_id,endpoint,p256dh,auth,created_at,updated_at) VALUES (?,?,?,?,0,0)", (user_id, endpoint, "key", "auth"))
        def failing_first(delivery, *, test):
            if delivery["push_endpoint"] == "https://push.example/admin-one":
                raise RuntimeError("raw provider failure detail")
        with patch("backend.routes.notifications.notifications.ensure_vapid_key"), patch("backend.workers.notifications._push", side_effect=failing_first) as push:
            response = self.client.post("/api/settings/notifications/web-push/test", headers=headers)
        self.assertEqual(response.status_code, 400)
        self.assertIn("1 registered device", response.get_json()["error"])
        self.assertNotIn("raw provider failure detail", response.get_json()["error"])
        self.assertEqual([call.args[0]["push_endpoint"] for call in push.call_args_list], ["https://push.example/admin-one", "https://push.example/admin-two", "https://push.example/admin-three"])
        self.assertTrue(all(call.kwargs["test"] for call in push.call_args_list))

    def test_service_worker_has_root_headers(self):
        with tempfile.TemporaryDirectory() as directory:
            static = Path(directory, "static")
            static.mkdir()
            (static / "service-worker.js").write_text("self.addEventListener('install', () => {});", encoding="utf-8")
            with patch("backend.application.FRONTEND_ROOT", directory):
                app = create_app({"TESTING": True, "SECRET_KEY": "service-worker-route"})
                with app.test_client() as client, client.get("/service-worker.js") as response:
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.mimetype, "application/javascript")
                    self.assertEqual(response.headers.get("Cache-Control"), "no-cache")


if __name__ == "__main__":
    unittest.main()
