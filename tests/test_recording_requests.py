"""Recording requests: exact Plex completion, durable intent, and local polling.

Matrix A–V from the action-layer request is defined here before implementation.
All writes use the test database and all providers are blocked unless mocked.
"""

from ._test_environment import TEST_ROOT  # noqa: F401
from .test_backend import DatabaseTestCase, Response

import json
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from threading import Event, Thread, current_thread
from unittest.mock import patch

import requests

from backend import api_cache, cache_memo, storage, track_search_index
from backend.services import lidarr, musicbrainz, plex, recording_acquisition, recording_requests, release_requests
from backend.workers import lidarr_searches, plex as plex_worker


RECORDING = "a1111111-1111-4111-8111-111111111111"
OTHER_RECORDING = "11111111-1111-4111-8111-111111111112"
SINGLE = "22222222-2222-4222-8222-222222222222"
ALBUM = "33333333-3333-4333-8333-333333333333"
URL = f"/api/music/recording/{RECORDING}/request"
TARGET = {"releaseGroupMbid": SINGLE, "title": "Song Single", "artistName": "Artist", "primaryType": "Single"}
RESOLUTION = {"state": "resolved", "recordingTitle": "Song", "target": TARGET,
              "alternatives": [{**TARGET, "releaseGroupMbid": ALBUM, "primaryType": "Album"}]}


class RecordingRequestTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        with storage.db() as connection:
            connection.execute("DELETE FROM recording_acquisitions")
        with api_cache.cache_db() as connection:
            connection.execute("DELETE FROM track_search_plex_tracks")
            connection.execute("DELETE FROM track_search_plex_isrcs")
        self.csrf = self.register()
        with storage.db() as connection:
            self.user_id = connection.execute("SELECT id FROM users").fetchone()[0]
        storage.save_service("plex", {"machineIdentifier": "server", "token": "private-plex-token", "librarySectionIds": ["1"]})
        storage.save_service("lidarr", {"url": "http://lidarr.invalid", "apiKey": "private-lidarr-key", "defaults": {
            "rootFolderPath": "/private/music", "qualityProfileId": 1, "metadataProfileId": 2, "tags": [3],
        }})
        self.enterContext(patch("requests.sessions.Session.request", side_effect=AssertionError("Live service call")))
        self.enterContext(patch.object(musicbrainz, "get", side_effect=AssertionError("MusicBrainz call")))
        self.resolve = self.enterContext(patch.object(recording_acquisition, "resolve", return_value=RESOLUTION))
        self.lookup = self.enterContext(patch.object(lidarr, "lookup_album", side_effect=lambda mbid: Response(payload=[{
            "title": "Song Single", "foreignAlbumId": mbid, "artist": {"artistName": "Artist"}, "albumType": "Single",
        }])))
        self.add = self.enterContext(patch.object(lidarr, "add_album", return_value=Response(201, {"id": 33, "artistId": 44, "title": "Song Single"})))
        self.existing = self.enterContext(patch.object(lidarr, "albums_by_release_group", side_effect=AssertionError("Unexpected lookup")))
        self.notify = self.enterContext(patch.object(release_requests.notifications, "queue_admin_request"))
        self.wake = self.enterContext(patch.object(release_requests.lidarr_search_worker, "request_work"))
        self.scan = self.enterContext(patch.object(release_requests.lidarr_library_worker, "request_scan"))
        self.enterContext(patch.object(plex_worker, "request_recent_scan", side_effect=AssertionError("Plex scan")))
        self.enterContext(patch.object(plex_worker, "request_full_scan", side_effect=AssertionError("Plex scan")))

    def post(self, client=None, csrf=None, url=URL):
        return (client or self.client).post(url, headers={"X-CSRF-Token": csrf or self.csrf})

    def get(self):
        response = self.client.get(URL)
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        return response.get_json()

    def rows(self, table):
        with storage.db() as connection:
            return [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]

    def intent(self):
        response = self.post()
        self.assertEqual(response.status_code, 202, response.get_json())
        return response.get_json()

    def library(self, fully=True):
        api_cache.set_cache_document("lidarr-library", "albums", {"albums": {SINGLE: {"fullyAvailable": fully}}}, 600)
        cache_memo.invalidate_document(lidarr.LIBRARY_INDEX_KEY)

    def download(self):
        api_cache.set_cache_document(lidarr.DOWNLOAD_SNAPSHOT_NAMESPACE, lidarr.DOWNLOAD_SNAPSHOT_KEY,
            {"albums": {SINGLE: {"progress": 63, "status": "downloading", "downloadId": "private-id", "path": "/private/music"}}}, 600)

    def plex_copies(self, count=1, recording=RECORDING, section="1"):
        track_search_index.index_plex_library({"serverId": "server", "tracks": [{
            "ratingKey": str(i), "key": f"/library/metadata/{i}", "title": "Song", "albumTitle": "Album",
            "musicbrainzRecordingId": recording, "librarySectionId": section,
        } for i in range(1, count + 1)]})

    def other_user(self):
        with storage.db() as connection:
            user_id = connection.execute("INSERT INTO users(username,password_hash,role,created_at) VALUES ('listener','unused','user',0)").lastrowid
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["user_id"], session["csrf_token"] = user_id, "listener-csrf"
        return client, user_id

    def test_a_already_available_returns_ready_without_work(self):
        self.plex_copies(2)
        response = self.post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        payload = response.get_json()
        self.assertEqual(payload["status"], "ready")
        self.assertTrue(payload["alreadyAvailable"])
        self.assertEqual(len(payload["tracks"]), 2)
        for call in (self.resolve, self.lookup, self.add, self.notify, self.wake, self.scan):
            call.assert_not_called()
        for table in ("recording_acquisitions", "recording_acquisition_requesters", "pending_lidarr_searches", "request_history"):
            self.assertEqual(self.rows(table), [])

    def test_b_new_request_persists_intent_and_requester(self):
        payload = self.intent()
        self.assertEqual(payload["status"], "queued")
        self.assertFalse(payload["alreadyRequested"])
        self.assertFalse(payload["alreadyAvailable"])
        self.assertFalse(payload["available"])
        self.assertEqual(payload["target"], TARGET)
        self.assertEqual(self.rows("recording_acquisitions")[0]["release_group_mbid"], SINGLE)
        self.assertEqual(self.rows("recording_acquisition_requesters")[0]["user_id"], self.user_id)
        self.assertEqual(len(self.rows("request_history")), 1)
        self.notify.assert_called_once()
        self.wake.assert_called_once()
        self.scan.assert_called_once()
        self.assertFalse(self.add.call_args.args[0]["addOptions"]["searchForNewAlbum"])

    def test_c_preferred_single_is_submitted_to_shared_service(self):
        with patch.object(release_requests, "request_release_group_for_user", wraps=release_requests.request_release_group_for_user) as core:
            self.intent()
        self.assertEqual(core.call_args.args[0], SINGLE)
        self.lookup.assert_called_once_with(SINGLE)

    def test_d_repeated_post_does_not_resolve_or_duplicate(self):
        first = self.intent()
        requesters = self.rows("recording_acquisition_requesters")
        self.resolve.side_effect = AssertionError("Must not retarget")
        for _ in range(2):
            payload = self.post().get_json()
            self.assertTrue(payload["alreadyRequested"])
            self.assertEqual(payload["target"], first["target"])
            self.assertEqual(payload["requestedAt"], first["requestedAt"])
        self.assertEqual(len(self.rows("recording_acquisitions")), 1)
        self.assertEqual(len(self.rows("recording_acquisition_requesters")), 1)
        self.assertEqual(self.rows("recording_acquisition_requesters"), requesters)
        self.add.assert_called_once()
        self.notify.assert_called_once()

    def test_e_second_user_attaches_without_lidarr_work(self):
        self.intent()
        other, user_id = self.other_user()
        response = self.post(other, "listener-csrf")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["alreadyRequested"])
        self.assertEqual({row["user_id"] for row in self.rows("recording_acquisition_requesters")}, {self.user_id, user_id})
        self.resolve.assert_called_once()
        self.add.assert_called_once()
        self.notify.assert_called_once()

    def test_f_concurrent_posts_coalesce_work_and_preserve_both_users(self):
        other, _ = self.other_user()
        entered, arrived, release = Event(), Event(), Event()
        original_availability = recording_requests._availability
        def availability(mbid):
            payload = original_availability(mbid)
            if current_thread().name == "second-recording-post":
                arrived.set()
            return payload
        self.enterContext(patch.object(recording_requests, "_availability", side_effect=availability))
        def resolve(_):
            entered.set()
            self.assertTrue(release.wait(5))
            return RESOLUTION
        self.resolve.side_effect = resolve
        responses, errors = [], []
        def post(client, csrf):
            try:
                responses.append(self.post(client, csrf))
            except Exception as exc:
                errors.append(exc)
        one = Thread(target=post, args=(self.client, self.csrf))
        two = Thread(target=post, args=(other, "listener-csrf"), name="second-recording-post")
        one.start()
        self.assertTrue(entered.wait(5))
        two.start()
        self.assertTrue(arrived.wait(5))
        release.set()
        one.join(10)
        two.join(10)
        self.assertFalse(one.is_alive() or two.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(sorted(response.status_code for response in responses), [200, 202])
        self.resolve.assert_called_once()
        self.add.assert_called_once()
        self.notify.assert_called_once()
        self.assertEqual(len(self.rows("recording_acquisitions")), 1)
        self.assertEqual(len(self.rows("recording_acquisition_requesters")), 2)

    def test_g_resolver_failures_create_no_work(self):
        for state, code in (("no_releases", 404), ("no_release_groups", 404), ("musicbrainz_unavailable", 502)):
            with self.subTest(state=state):
                self.resolve.return_value = {**RESOLUTION, "state": state, "target": None}
                response = self.post()
                self.assertEqual(response.status_code, code)
                self.assertIn("error", response.get_json())
                self.assertEqual(self.rows("recording_acquisitions"), [])
        self.lookup.assert_not_called()
        self.add.assert_not_called()

    def test_h_sync_lidarr_failure_can_be_retried_and_hides_details(self):
        self.lookup.side_effect = requests.ConnectionError("private-lidarr-key /private/music")
        response = self.post()
        self.assertEqual(response.status_code, 502)
        self.assertEqual(self.rows("recording_acquisitions"), [])
        self.assertEqual(self.rows("recording_acquisition_requesters"), [])
        self.assertNotIn("private", response.get_data(as_text=True))
        self.lookup.side_effect = None
        self.lookup.return_value = Response(payload=[{"title": "Single", "artist": {}}])
        self.intent()

    def test_h_rejected_or_missing_album_never_persists_intent(self):
        for response, code in ((Response(400, text="private upstream payload"), 502), (Response(payload=[]), 404)):
            self.lookup.side_effect = None
            self.lookup.return_value = response
            result = self.post()
            self.assertEqual(result.status_code, code)
            self.assertNotIn("private", result.get_data(as_text=True))
            self.assertEqual(self.rows("recording_acquisitions"), [])

    def test_h_invalid_configuration_does_not_persist_intent(self):
        storage.save_service("lidarr", {"defaults": {}})
        self.assertEqual(self.post().status_code, 503)
        self.assertEqual(self.rows("recording_acquisitions"), [])
        self.add.assert_not_called()

    def test_i_existing_pending_release_group_is_reused(self):
        storage.enqueue_lidarr_search(self.user_id, SINGLE, 33, 44, "Existing Single")
        payload = self.intent()
        self.assertEqual(payload["status"], "queued")
        self.assertEqual(len(self.rows("pending_lidarr_searches")), 1)
        self.lookup.assert_not_called()
        self.add.assert_not_called()
        self.notify.assert_called_once()

    def test_j_live_fully_available_acceptance_still_waits_for_plex(self):
        self.add.return_value = Response(400, text="already exists")
        self.existing.side_effect = None
        self.existing.return_value = Response(payload=[{"id": 33, "foreignAlbumId": SINGLE,
            "title": "Single", "statistics": {"totalTrackCount": 2, "trackFileCount": 2}}])
        self.library()
        response = self.post()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.get_json()["status"], "waiting_for_plex")
        self.assertFalse(response.get_json()["available"])
        self.assertEqual(self.rows("pending_lidarr_searches"), [])
        self.notify.assert_called_once()

    def test_k_cached_download_progress_is_sanitized(self):
        self.intent()
        self.download()
        payload = self.get()
        self.assertEqual(payload["status"], "downloading")
        self.assertEqual(payload["downloadStatus"], {"progress": 63, "status": "downloading"})
        self.assertNotIn("private", json.dumps(payload))

    def test_l_retryable_pending_errors_remain_queued(self):
        self.intent()
        job = self.rows("pending_lidarr_searches")[0]
        storage.defer_lidarr_search(job["id"], "private-token http://lidarr/private")
        payload = self.get()
        self.assertEqual(payload["status"], "queued")
        self.assertTrue(payload["retrying"])
        self.assertNotIn("private", json.dumps(payload))

    def test_l_submitted_search_remains_queued_until_job_removed(self):
        self.intent()
        job = self.rows("pending_lidarr_searches")[0]
        storage.set_lidarr_search_command(job["id"], 66)
        self.assertEqual(self.get()["status"], "queued")
        lidarr_searches.process_job(storage.pending_lidarr_search(SINGLE))
        self.assertEqual(self.get()["status"], "requested")

    def test_m_imported_target_waits_then_exact_recording_becomes_ready(self):
        self.intent()
        before = self.rows("recording_acquisitions")
        self.library()
        self.assertEqual(self.get()["status"], "waiting_for_plex")
        self.plex_copies()
        self.assertEqual(self.get()["status"], "ready")
        self.assertEqual(self.rows("recording_acquisitions"), before)

    def test_n_ready_wins_over_stale_download_state(self):
        self.intent()
        self.download()
        self.plex_copies()
        payload = self.get()
        self.assertEqual(payload["status"], "ready")
        self.assertIsNone(payload["downloadStatus"])
        self.assertEqual(payload["target"], TARGET)

    def test_o_ready_returns_every_copy_like_availability(self):
        self.intent()
        self.plex_copies(2)
        self.assertEqual(self.get()["tracks"], self.client.get(f"/api/music/recording/{RECORDING}/availability").get_json()["tracks"])
        self.assertEqual(len(self.get()["tracks"]), 2)

    def test_o_copies_and_isrcs_use_one_indexed_query(self):
        self.plex_copies(3)
        with api_cache.cache_db() as connection:
            connection.executemany("INSERT INTO track_search_plex_isrcs VALUES (?, ?, ?)",
                [("server", str(i), isrc) for i in range(1, 4) for isrc in ("USABC1234567", "GBABC1234567")])
        queries = []
        original = track_search_index.cache_db
        @contextmanager
        def traced():
            with original() as connection:
                connection.set_trace_callback(queries.append)
                yield connection
        with patch.object(track_search_index, "cache_db", side_effect=traced):
            payload = self.get()
        self.assertEqual(len(payload["tracks"]), 3)
        self.assertTrue(all(track["isrcs"] == ["GBABC1234567", "USABC1234567"] for track in payload["tracks"]))
        self.assertEqual(len([query for query in queries if query.startswith("SELECT")]), 1)

    def test_p_not_requested_is_local_and_empty(self):
        payload = self.get()
        self.assertEqual(payload["status"], "not_requested")
        self.assertIsNone(payload["target"])
        self.assertIsNone(payload["requestedAt"])
        self.assertEqual(payload["tracks"], [])
        self.resolve.assert_not_called()
        self.lookup.assert_not_called()

    def test_q_get_with_network_scans_and_database_writes_blocked(self):
        self.intent()
        self.download()
        original_db, original_cache_db = storage.db, api_cache.cache_db
        @contextmanager
        def readonly(factory):
            with factory() as connection:
                def authorize(action, *_):
                    return sqlite3.SQLITE_DENY if action in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
                        sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_DROP_TABLE, sqlite3.SQLITE_CREATE_INDEX} else sqlite3.SQLITE_OK
                connection.set_authorizer(authorize)
                yield connection
        with (patch.object(storage, "db", side_effect=lambda: readonly(original_db)),
              patch.object(api_cache, "cache_db", side_effect=lambda: readonly(original_cache_db)),
              patch.object(track_search_index, "cache_db", side_effect=lambda: readonly(original_cache_db)),
              patch("socket.create_connection", side_effect=AssertionError("Socket")),
              patch("socket.socket.connect", side_effect=AssertionError("Socket")),
              patch.object(plex, "full_library_scan", side_effect=AssertionError("Scan")),
              patch.object(plex, "recently_added_scan", side_effect=AssertionError("Scan")),
              patch.object(track_search_index, "rebuild_from_cache", side_effect=AssertionError("Rebuild"))):
            self.assertEqual(self.get()["status"], "downloading")
        self.resolve.assert_called_once()
        self.lookup.assert_called_once()
        self.wake.assert_called_once()
        self.scan.assert_called_once()

    def test_q_corrupt_cache_documents_are_ignored_without_deletion(self):
        self.intent()
        with api_cache.cache_db() as connection:
            for namespace, key in (("lidarr-library", "albums"), (lidarr.DOWNLOAD_SNAPSHOT_NAMESPACE, lidarr.DOWNLOAD_SNAPSHOT_KEY)):
                connection.execute("INSERT OR REPLACE INTO api_cache VALUES (?, '{broken', 9999999999)",
                                   (api_cache.document_cache_key(namespace, key),))
        cache_memo.invalidate_document(lidarr.LIBRARY_INDEX_KEY)
        with self.assertLogs("backend.api_cache", level="WARNING"):
            self.assertEqual(self.get()["status"], "queued")
        with api_cache.cache_db() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM api_cache WHERE value = '{broken'").fetchone()[0], 2)

    def test_r_intent_survives_app_reinitialization_without_resolver(self):
        payload = self.intent()
        self.resolve.side_effect = AssertionError("Resolver on restart")
        from backend.application import create_app
        app = create_app({"TESTING": True, "SECRET_KEY": "test-secret"})
        client = app.test_client()
        with client.session_transaction() as session:
            session["user_id"] = self.user_id
        response = client.get(URL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["target"], payload["target"])
        self.assertEqual(response.get_json()["requestedAt"], payload["requestedAt"])

    def test_s_both_routes_use_the_shared_release_group_service(self):
        with patch.object(release_requests, "request_release_group_for_user", wraps=release_requests.request_release_group_for_user) as core:
            response = self.client.post("/api/request/release-group", json={"mbid": SINGLE}, headers={"X-CSRF-Token": self.csrf})
            self.assertEqual(response.status_code, 202)
            self.intent()
        self.assertEqual(core.call_count, 2)
        self.add.assert_called_once()

    def test_t_anime_history_and_notification_are_preserved(self):
        response = self.client.post("/api/request/release-group", json={"mbid": SINGLE,
            "animeSlug": "series", "animeName": "Series", "themeId": 1, "themeLabel": "Opening 1", "songTitle": "Song"},
            headers={"X-CSRF-Token": self.csrf})
        self.assertEqual(response.status_code, 202)
        row = self.rows("request_history")[0]
        self.assertEqual((row["anime_slug"], row["anime_name"], row["theme_id"], row["theme_label"], row["song_title"]),
                         ("series", "Series", 1, "Opening 1", "Song"))
        self.notify.assert_called_once_with(self.user_id, "test-user", SINGLE, "Song Single", "Artist")

    def test_u_invalid_uuid_rejected_and_uppercase_normalized(self):
        for method in (self.client.get, lambda url: self.post(url=url)):
            self.assertEqual(method("/api/music/recording/bad/request").status_code, 400)
        self.assertEqual(self.post(url=URL.upper().replace("/API/MUSIC/RECORDING/", "/api/music/recording/").replace("/REQUEST", "/request")).status_code, 202)
        self.assertEqual(self.rows("recording_acquisitions")[0]["recording_mbid"], RECORDING)

    def test_v_auth_and_csrf_follow_session_conventions(self):
        anonymous = self.app.test_client()
        self.assertEqual(anonymous.get(URL).status_code, 401)
        self.assertEqual(anonymous.post(URL).status_code, 401)
        self.assertEqual(self.client.post(URL).status_code, 403)
        self.resolve.assert_not_called()

    def test_requested_fallback_after_pending_job_finishes(self):
        self.intent()
        storage.complete_lidarr_search(self.rows("pending_lidarr_searches")[0]["id"])
        self.assertEqual(self.get()["status"], "requested")

    def test_wrong_recording_or_unselected_plex_section_is_not_ready(self):
        self.intent()
        self.library()
        self.plex_copies(recording=OTHER_RECORDING)
        self.assertEqual(self.get()["status"], "waiting_for_plex")
        self.plex_copies(section="2")
        self.assertEqual(self.get()["status"], "waiting_for_plex")

    def test_waiting_for_plex_overrides_download_and_pending(self):
        self.intent()
        self.download()
        self.library()
        payload = self.get()
        self.assertEqual(payload["status"], "waiting_for_plex")
        self.assertIsNone(payload["downloadStatus"])

    def test_local_storage_error_has_safe_503(self):
        with patch.object(plex, "recording_availability", side_effect=sqlite3.OperationalError("private-path")), \
             patch.object(plex, "recording_availabilities", side_effect=sqlite3.OperationalError("private-path")):
            for response in (self.client.get(URL), self.post()):
                self.assertEqual(response.status_code, 503)
                self.assertNotIn("private", response.get_data(as_text=True))

    def test_migration_indexes_constraints_and_user_deletion(self):
        self.intent()
        storage.init_db()
        with storage.db() as connection:
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(recording_acquisitions)")}
            self.assertNotIn("status", columns)
            self.assertTrue(connection.execute("PRAGMA index_list(recording_acquisitions)").fetchall())
            self.assertTrue(connection.execute("PRAGMA index_list(recording_acquisition_requesters)").fetchall())
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("INSERT INTO recording_acquisition_requesters(recording_mbid, user_id, requested_at) VALUES (?, 999999, 0)", (RECORDING,))
            connection.execute("DELETE FROM users WHERE id = ?", (self.user_id,))
        self.assertEqual(self.rows("recording_acquisition_requesters"), [])
        self.assertEqual(len(self.rows("recording_acquisitions")), 1)

    def test_existing_installation_migrates_without_changing_history(self):
        storage.record_request(self.user_id, "release-group", ALBUM, "Legacy Album")
        before = self.rows("request_history")
        with storage.db() as connection:
            connection.execute("DROP TABLE recording_acquisition_requesters")
            connection.execute("DROP TABLE recording_acquisitions")
        storage.init_db()
        storage.init_db()
        self.assertEqual(self.rows("recording_acquisitions"), [])
        self.assertEqual(self.rows("recording_acquisition_requesters"), [])
        self.assertEqual(self.rows("request_history"), before)

    def test_two_recordings_selecting_one_group_coalesce_lidarr_work(self):
        other, _ = self.other_user()
        entered, second_resolved, release = Event(), Event(), Event()
        def resolve(recording):
            if recording == OTHER_RECORDING:
                second_resolved.set()
            return RESOLUTION
        self.resolve.side_effect = resolve
        original_add = self.add.return_value
        def add(_):
            entered.set()
            self.assertTrue(release.wait(5))
            return original_add
        self.add.side_effect = add
        responses = []
        first = Thread(target=lambda: responses.append(self.post()))
        second = Thread(target=lambda: responses.append(self.post(other, "listener-csrf", url=f"/api/music/recording/{OTHER_RECORDING}/request")))
        first.start()
        self.assertTrue(entered.wait(5))
        second.start()
        self.assertTrue(second_resolved.wait(5))
        release.set()
        first.join(10)
        second.join(10)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual([response.status_code for response in responses], [202, 202])
        self.assertEqual(len(self.rows("recording_acquisitions")), 2)
        self.assertEqual(len(self.rows("pending_lidarr_searches")), 1)
        self.add.assert_called_once()

    def test_intent_write_failure_after_acceptance_recovers_via_pending_job(self):
        with patch.object(storage, "save_recording_acquisition", side_effect=sqlite3.OperationalError("private-db-path")):
            self.assertEqual(self.post().status_code, 503)
        self.assertEqual(self.rows("recording_acquisitions"), [])
        self.assertEqual(len(self.rows("pending_lidarr_searches")), 1)
        self.intent()
        self.add.assert_called_once()

    def test_unexpected_provider_error_is_safe_and_retryable(self):
        self.resolve.side_effect = RuntimeError("private-secret")
        with self.assertLogs("backend.application", level="WARNING"):
            response = self.post()
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("private", response.get_data(as_text=True))
        self.assertEqual(self.rows("recording_acquisitions"), [])

    def test_cross_process_lock_blocks_and_releases_after_process_exit(self):
        from backend.request_locks import request_lock
        script = "from backend.request_locks import request_lock; import sys;\nwith request_lock('recording', sys.argv[1]):\n print('locked', flush=True); sys.stdin.readline()"
        process = subprocess.Popen([sys.executable, "-c", script, RECORDING], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), "locked")
            with self.assertRaises(TimeoutError):
                with request_lock("recording", RECORDING, timeout=0.1):
                    self.fail("Concurrent process acquired lock")
            process.terminate()
            process.wait(timeout=5)
            with request_lock("recording", RECORDING, timeout=1):
                pass
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()
