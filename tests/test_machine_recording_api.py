"""Machine recording/search authentication, explicit origin, and shared work."""

from ._test_environment import TEST_ROOT  # noqa: F401
from .test_backend import DatabaseTestCase, Response
from . import test_recording_requests as fixtures

import sqlite3
from threading import Event, Thread
from unittest.mock import patch

from backend import api_cache, notifications, storage, track_search_index
from backend.routes import discovery
from backend.services import lidarr, musicbrainz, plex, recording_acquisition, release_requests
from backend.workers import plex as plex_worker


API_KEY = "machine-test-secret-01234567890123456789"
BASE = f"/api/v1/music/recordings/{fixtures.RECORDING}"


class MachineRecordingAPITests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.app.config["AUTOMATION_API_KEY"] = API_KEY
        self.csrf = self.register()
        self.machine = self.app.test_client()
        with storage.db() as connection:
            self.user_id = connection.execute("SELECT id FROM users").fetchone()[0]
            connection.execute("DELETE FROM automation_request_history")
        with api_cache.cache_db() as connection:
            connection.execute("DELETE FROM track_search_plex_tracks")
            connection.execute("DELETE FROM track_search_plex_isrcs")
        storage.save_service("plex", {"machineIdentifier": "server", "librarySectionIds": ["1"]})
        storage.save_service("lidarr", {"url": "http://lidarr.invalid", "apiKey": "private-lidarr-key", "defaults": {
            "qualityProfileId": 1, "metadataProfileId": 2, "rootFolderPath": "/private/music",
        }})
        self.enterContext(patch("requests.sessions.Session.request", side_effect=AssertionError("Live provider")))
        self.enterContext(patch.object(musicbrainz, "get", side_effect=AssertionError("Live MusicBrainz")))
        self.resolve = self.enterContext(patch.object(recording_acquisition, "resolve", return_value=fixtures.RESOLUTION))
        self.lookup = self.enterContext(patch.object(lidarr, "lookup_album", return_value=Response(payload=[{
            "title": "Song Single", "artist": {"artistName": "Artist"}, "albumType": "Single",
        }])))
        self.add = self.enterContext(patch.object(lidarr, "add_album", return_value=Response(201, {"id": 33, "artistId": 44, "title": "Song Single"})))
        self.original_notify = notifications.queue_admin_request
        self.notify = self.enterContext(patch.object(notifications, "queue_admin_request"))
        self.wake = self.enterContext(patch.object(release_requests.lidarr_search_worker, "request_work"))
        self.scan = self.enterContext(patch.object(release_requests.lidarr_library_worker, "request_scan"))
        self.enterContext(patch.object(plex_worker, "request_recent_scan", side_effect=AssertionError("Plex scan")))
        self.enterContext(patch.object(plex_worker, "request_full_scan", side_effect=AssertionError("Plex scan")))

    rows = fixtures.RecordingRequestTests.rows
    plex_copies = fixtures.RecordingRequestTests.plex_copies

    def api(self, method="get", suffix="/request", *, key=API_KEY, client=None):
        headers = {"Content-Type": "application/json"}
        if key is not None:
            headers["X-Api-Key"] = key
        return getattr(client or self.machine, method)(BASE + suffix, headers=headers)

    def user_post(self):
        return self.client.post(fixtures.URL, headers={"X-CSRF-Token": self.csrf})

    def test_valid_key_gets_recording_availability_without_session(self):
        self.plex_copies(2)
        response = self.api(suffix="/availability")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["available"])
        self.assertEqual(len(response.get_json()["tracks"]), 2)
        self.resolve.assert_not_called()

    def test_valid_key_gets_acquisition_without_requesting(self):
        resolution = {**fixtures.RESOLUTION, "alternatives": []}
        self.resolve.return_value = resolution
        response = self.api(suffix="/acquisition")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["target"]["releaseGroupMbid"], fixtures.SINGLE)
        self.assertEqual(self.rows("recording_acquisitions"), [])
        self.add.assert_not_called()

    def test_valid_key_gets_request_lifecycle_without_session(self):
        response = self.api()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "not_requested")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.resolve.assert_not_called()

    def test_valid_key_post_needs_no_cookie_or_csrf_and_uses_shared_service(self):
        with patch.object(release_requests, "request_release_group", wraps=release_requests.request_release_group) as core:
            response = self.api("post")
        self.assertEqual(response.status_code, 202, response.get_json())
        self.assertEqual(response.get_json()["status"], "queued")
        self.assertNotIn("Set-Cookie", response.headers)
        self.assertEqual(core.call_args.args, (fixtures.SINGLE,))
        self.add.assert_called_once()
        self.wake.assert_called_once()
        self.scan.assert_called_once()

    def test_missing_and_invalid_keys_return_401_without_work(self):
        for key in (None, "invalid-key", "invalid-key-é"):
            for method, suffix in (("get", "/availability"), ("get", "/acquisition"), ("get", "/request"), ("post", "/request")):
                with self.subTest(key=key, method=method, suffix=suffix):
                    self.assertEqual(self.api(method, suffix, key=key).status_code, 401)
        self.resolve.assert_not_called()
        self.add.assert_not_called()

    def test_automation_origin_has_null_user_and_separate_durable_audit(self):
        with storage.db() as connection:
            connection.execute("DELETE FROM users")
        response = self.api("post")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.rows("users"), [])
        requester = self.rows("recording_acquisition_requesters")[0]
        self.assertEqual((requester["source"], requester["user_id"]), ("automation", None))
        pending = self.rows("pending_lidarr_search_requesters")[0]
        self.assertEqual((pending["source"], pending["user_id"]), ("automation", None))
        audit = self.rows("automation_request_history")[0]
        self.assertEqual((audit["source"], audit["user_id"], audit["mbid"]), ("automation", None, fixtures.SINGLE))
        self.assertEqual(self.rows("request_history"), [])
        self.notify.assert_called_once_with(None, "Automation API", fixtures.SINGLE, "Song Single", "Artist")

    def test_api_then_user_reuses_acquisition_and_attaches_user(self):
        self.api("post")
        response = self.user_post()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["alreadyRequested"])
        self.assert_origins_and_one_job()
        self.assertEqual(self.rows("request_history"), [])
        self.assertEqual(len(self.rows("automation_request_history")), 1)

    def test_api_post_audit_appears_in_admin_requests(self):
        response = self.api("post")
        self.assertEqual(response.status_code, 202)
        with (
            patch("backend.routes.admin._profile_plex_index", return_value={}),
            patch("backend.routes.account._cached_release_group_metadata", return_value={"release_date": "2026"}),
        ):
            response = self.client.get("/api/admin/requests")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["pagination"]["total"], 1)
        item = payload["requests"][0]
        self.assertEqual(item["mbid"], fixtures.SINGLE)
        self.assertEqual(item["source"], "automation")
        self.assertIsNone(item["requester"]["id"])
        self.assertEqual(item["requester"]["username"], "Automation API")
        self.assertEqual(item["requestStatus"], "queued")
        self.assertEqual(self.rows("request_history"), [])
        self.add.assert_called_once()

    def test_user_then_api_reuses_acquisition_and_attaches_automation(self):
        self.user_post()
        response = self.api("post")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["alreadyRequested"])
        self.assert_origins_and_one_job()
        self.assertEqual(len(self.rows("request_history")), 1)

    def assert_origins_and_one_job(self):
        self.assertEqual(len(self.rows("recording_acquisitions")), 1)
        self.assertEqual(len(self.rows("pending_lidarr_searches")), 1)
        self.assertEqual({(row["source"], row["user_id"]) for row in self.rows("recording_acquisition_requesters")},
                         {("user", self.user_id), ("automation", None)})
        self.assertEqual({(row["source"], row["user_id"]) for row in self.rows("pending_lidarr_search_requesters")},
                         {("user", self.user_id), ("automation", None)})
        self.add.assert_called_once()
        self.resolve.assert_called_once()
        self.notify.assert_called_once()

    def test_repeated_api_post_is_idempotent(self):
        first = self.api("post").get_json()
        before = {table: self.rows(table) for table in ("recording_acquisitions", "recording_acquisition_requesters",
                    "pending_lidarr_searches", "pending_lidarr_search_requesters", "automation_request_history")}
        for _ in range(2):
            response = self.api("post")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()["requestedAt"], first["requestedAt"])
        self.assertEqual(before, {table: self.rows(table) for table in before})
        self.add.assert_called_once()
        self.notify.assert_called_once()

    def test_ready_automation_post_creates_no_work_or_requester(self):
        self.plex_copies()
        self.assertEqual(self.api("post").get_json()["status"], "ready")
        for table in ("recording_acquisitions", "recording_acquisition_requesters", "automation_request_history"):
            self.assertEqual(self.rows(table), [])
        self.resolve.assert_not_called()
        self.add.assert_not_called()

    def test_browser_routes_keep_session_and_csrf_rules(self):
        self.assertEqual(self.machine.get(fixtures.URL, headers={"X-Api-Key": API_KEY}).status_code, 401)
        self.assertEqual(self.machine.post(fixtures.URL, headers={"X-Api-Key": API_KEY}).status_code, 401)
        self.assertEqual(self.client.post(fixtures.URL).status_code, 403)
        self.assertEqual(self.user_post().status_code, 202)
        self.assertEqual(self.rows("recording_acquisition_requesters")[0]["source"], "user")

    def test_machine_routes_allow_session_but_still_require_session_csrf(self):
        self.assertEqual(self.api(client=self.client, key=None).status_code, 200)
        self.assertEqual(self.api("post", client=self.client, key=None).status_code, 403)
        response = self.client.post(BASE + "/request", headers={"X-CSRF-Token": self.csrf})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.rows("recording_acquisition_requesters")[0]["source"], "user")

    def test_valid_key_takes_automation_origin_even_with_session_cookie(self):
        self.assertEqual(self.api("post", client=self.client).status_code, 202)
        self.assertEqual(self.rows("recording_acquisition_requesters")[0]["source"], "automation")
        self.assertIsNone(self.rows("recording_acquisition_requesters")[0]["user_id"])

    def test_machine_search_reuses_compact_recording_state(self):
        self.plex_copies(2)
        recording = {"id": fixtures.RECORDING, "title": "Song", "score": 100,
                     "releases": [{"title": "Song Single", "release-group": {"id": fixtures.SINGLE, "primary-type": "Single"}}]}
        with patch.object(discovery, "_local_track_resolution", side_effect=lambda plan: {"plan": plan, "results": []}), \
             patch.object(musicbrainz, "search", side_effect=[{"recordings": [recording]}, {"release-groups": []}]):
            response = self.machine.get("/api/v1/search?type=track&q=Song", headers={"X-Api-Key": API_KEY})
        self.assertEqual(response.status_code, 200)
        result = response.get_json()["results"][0]
        self.assertEqual(result["recordingMbid"], fixtures.RECORDING)
        self.assertEqual(result["recordingState"]["status"], "ready")
        self.assertEqual(result["recordingState"]["plexCopyCount"], 2)

    def test_machine_search_auth_and_query_validation(self):
        for headers in ({}, {"X-Api-Key": "wrong"}):
            self.assertEqual(self.machine.get("/api/v1/search?type=track&q=Song", headers=headers).status_code, 401)
        self.assertEqual(self.machine.get("/api/v1/search?type=track&q=x", headers={"X-Api-Key": API_KEY}).status_code, 400)

    def test_key_does_not_authorize_unrelated_admin_or_user_routes(self):
        for method, path in (("get", "/api/settings"), ("post", "/api/request/release-group"), ("get", "/api/account/settings")):
            response = getattr(self.machine, method)(path, headers={"X-Api-Key": API_KEY}, json={})
            self.assertIn(response.status_code, (401, 404))

    def test_key_is_absent_from_success_errors_logs_and_audit(self):
        response = self.api("post")
        self.assertNotIn(API_KEY, response.get_data(as_text=True))
        for table in ("recording_acquisition_requesters", "automation_request_history", "pending_lidarr_search_requesters"):
            self.assertNotIn(API_KEY, str(self.rows(table)))
        self.resolve.side_effect = RuntimeError(API_KEY)
        other = BASE.replace(fixtures.RECORDING, fixtures.OTHER_RECORDING)
        with self.assertLogs(self.app.logger, level="WARNING") as logs:
            response = self.machine.post(other + "/request", headers={"X-Api-Key": API_KEY})
        self.assertEqual(response.status_code, 502)
        self.assertNotIn(API_KEY, response.get_data(as_text=True))
        self.assertNotIn(API_KEY, str(logs.output))

    def test_automation_pending_work_survives_user_deletion_and_restart(self):
        self.user_post()
        self.api("post")
        with storage.db() as connection:
            connection.execute("DELETE FROM users WHERE id = ?", (self.user_id,))
        storage.init_db()
        storage.init_db()
        self.assertEqual(len(self.rows("pending_lidarr_searches")), 1)
        self.assertEqual(self.rows("pending_lidarr_search_requesters")[0]["source"], "automation")
        self.assertEqual(self.api().get_json()["status"], "queued")

    def test_legacy_requester_migration_preserves_users_timestamps_and_jobs(self):
        self.user_post()
        for table, identity, reference, timestamp in (
            ("recording_acquisition_requesters", "recording_mbid", "recording_acquisitions(recording_mbid)", True),
            ("pending_lidarr_search_requesters", "job_id", "pending_lidarr_searches(id)", False),
        ):
            rows = self.rows(table)
            columns = [identity, "user_id", *(["requested_at"] if timestamp else [])]
            with storage.db() as connection:
                connection.execute(f"DROP TABLE {table}")
                connection.execute(f"CREATE TABLE {table} ({identity} {'TEXT' if timestamp else 'INTEGER'} NOT NULL REFERENCES {reference} ON DELETE CASCADE, "
                                   "user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, "
                                   + ("requested_at REAL NOT NULL, " if timestamp else "") + f"PRIMARY KEY({identity}, user_id))")
                connection.executemany(f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
                                       [tuple(row[column] for column in columns) for row in rows])
            storage.init_db()
            self.assertEqual([{column: row[column] for column in columns} for row in self.rows(table)],
                             [{column: row[column] for column in columns} for row in rows])
            self.assertEqual(self.rows(table)[0]["source"], "user")
        self.api("post")
        self.assert_origins_and_one_job()

    def test_requester_source_constraints_and_automation_uniqueness(self):
        self.api("post")
        with storage.db() as connection:
            for source, user_id in (("user", None), ("automation", self.user_id), ("unknown", None)):
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute("INSERT INTO recording_acquisition_requesters(recording_mbid,user_id,requested_at,source) VALUES (?,?,0,?)",
                                       (fixtures.RECORDING, user_id, source))
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("INSERT INTO recording_acquisition_requesters(recording_mbid,user_id,requested_at,source) VALUES (?,NULL,0,'automation')", (fixtures.RECORDING,))
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_concurrent_machine_and_user_posts_coalesce_work(self):
        entered, release = Event(), Event()
        def resolve(_):
            entered.set()
            self.assertTrue(release.wait(5))
            return fixtures.RESOLUTION
        self.resolve.side_effect = resolve
        responses, errors = [], []
        def call(operation):
            try:
                responses.append(operation())
            except Exception as exc:
                errors.append(exc)
        first = Thread(target=call, args=(lambda: self.api("post"),))
        second = Thread(target=call, args=(self.user_post,))
        first.start()
        self.assertTrue(entered.wait(5))
        second.start()
        release.set()
        first.join(10)
        second.join(10)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(sorted(response.status_code for response in responses), [200, 202])
        self.assert_origins_and_one_job()

    def test_real_admin_notification_accepts_automation_without_excluding_admins(self):
        with storage.db() as connection:
            connection.execute("UPDATE users SET plex_email='admin@example.test' WHERE id=?", (self.user_id,))
            connection.execute("INSERT INTO user_notification_preferences(user_id,enabled,email_enabled,web_push_enabled,requested_available,all_new_music,admin_request_notifications,updated_at) "
                               "VALUES (?,1,1,0,1,0,1,0)", (self.user_id,))
        with patch.object(notifications, "global_channels", return_value=(True, True, False)):
            queued = self.original_notify(None, "Automation API", fixtures.SINGLE, "Song Single", "Artist")
        self.assertEqual(queued, 1)
        self.assertEqual(self.rows("notification_events")[0]["requester_username"], "Automation API")
