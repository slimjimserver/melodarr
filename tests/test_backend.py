"""High-value backend regression tests using Flask's built-in test client."""

if __package__:
    from ._test_environment import TEST_DATA, TEST_ROOT
else:  # Support direct execution: python tests/test_backend.py
    from _test_environment import TEST_DATA, TEST_ROOT

import gzip
import io
import json
import os
import runpy
import sqlite3
import tempfile
import time
import unittest
from datetime import timedelta, timezone
from threading import Barrier, BrokenBarrierError, Event, Thread
from urllib.parse import parse_qs, urlparse
from unittest.mock import Mock, patch

from werkzeug.security import check_password_hash, generate_password_hash

import requests
from flask import Flask
from PIL import Image

from backend import api_cache
from backend import artwork_cache
from backend import cache_memo
from backend import config as backend_config
from backend import detail_cache
from backend import instance_settings
from backend import recommendations as recommendation_engine
from backend import security
from backend import storage as storage_module
from backend import track_search_index
from backend.api_cache import (
    cache_db,
    cache_key,
    cached_json_get,
    clear_cache,
    commit_json_responses,
    get_cache_document,
    migrate_legacy_cache,
    upsert_cache_documents,
)
from backend.application import MAX_REQUEST_BODY_BYTES, create_app
from backend.config import ARTWORK_CACHE_DIRECTORY
from backend.http_security import RedirectRejected
from backend.routes import discovery
from backend.services import (
    anime_theme_links,
    lidarr,
    musicbrainz,
    plex,
    plex_auth,
    plex_history,
)
from backend.storage import (
    db,
    enqueue_lidarr_search,
    get_lastfm_api_key,
    get_plex_listens,
    get_service,
    init_db,
    insert_plex_listens,
    plex_listen_stats,
    prune_plex_listens,
    save_service,
    set_lidarr_refresh_command,
    write_settings_file,
)
from backend import worker
from backend.workers import anime_metadata as anime_metadata_worker
from backend.workers import artist_metadata as artist_metadata_worker
from backend.workers import lidarr_searches as lidarr_search_worker
from backend.workers import lidarr_library as lidarr_library_worker
from backend.workers import lidarr_downloads as lidarr_download_worker
from backend.workers import plex as plex_worker
from backend.workers import plex_history as plex_history_worker
from backend.workers import plex_metadata as plex_metadata_worker
from backend.workers import recommendations as recommendation_worker
from backend.workers import similar_artists as similar_artist_worker


class Response:
    """Small requests.Response stand-in for external-client tests."""

    def __init__(
        self,
        status_code=200,
        payload=None,
        content=b"",
        text="",
        headers=None,
        chunks=(),
    ):
        self.status_code = status_code
        self._payload = payload
        self.content = content
        self.text = text
        self.headers = headers or {}
        self._chunks = chunks

    def raise_for_status(self):
        if self.status_code >= 400:
            error = requests.HTTPError(f"HTTP {self.status_code}")
            error.response = self
            raise error

    def json(self):
        return self._payload

    def iter_content(self, chunk_size=None):
        return iter(self._chunks)


class _StopWorker(BaseException):
    """Test-only signal that can escape worker Exception handlers."""


class DatabaseTestCase(unittest.TestCase):
    """Create an isolated app and reset mutable database state per test."""

    def setUp(self):
        self.app = create_app({"TESTING": True, "SECRET_KEY": "test-secret"})
        self.client = self.app.test_client()
        with cache_memo._lock:
            cache_memo._entries.clear()
            cache_memo._generations.clear()
        with artwork_cache._size_lock:
            artwork_cache._cached_size_bytes = None
        with artwork_cache._key_locks_lock:
            artwork_cache._key_locks.clear()
        artwork_cache._last_trim_at = None
        detail_cache.invalidate_all()
        with detail_cache._key_locks_lock:
            detail_cache._key_locks.clear()
        with anime_metadata_worker.queue_lock:
            anime_metadata_worker.queued_themes.clear()
            anime_metadata_worker.active_theme_keys.clear()
            anime_metadata_worker.job_state.update(
                running=False,
                queued=0,
                completed=0,
                total=0,
                lastCompletedAt=None,
            )
        anime_metadata_worker.wake_requested.clear()
        with artist_metadata_worker.queue_lock:
            artist_metadata_worker.queued_artist_ids.clear()
            artist_metadata_worker.forced_track_artist_ids.clear()
            artist_metadata_worker.active_artist_phases.clear()
            artist_metadata_worker.job_state.update(
                running=False,
                queued=0,
                completed=0,
                lastCompletedAt=None,
            )
        artist_metadata_worker.wake_requested.clear()
        with similar_artist_worker.queue_lock:
            similar_artist_worker.queued_candidates.clear()
            similar_artist_worker.active_candidate_keys.clear()
            similar_artist_worker.job_state.update(
                running=False, queued=0, completed=0, lastCompletedAt=None,
            )
        similar_artist_worker.wake_requested.clear()
        with plex_history_worker.request_lock:
            plex_history_worker.sync_requested = False
            plex_history_worker.full_sync_requested = False
        plex_history_worker.wake_requested.clear()
        plex_history_worker.job_state.update(
            running=False,
            lastCompletedAt=None,
            lastSuccessfulAt=None,
            nextExecutionAt=None,
            lastError=None,
            pages=0,
            scanned=0,
            tracks=0,
            normalized=0,
            selected=0,
            sections=0,
            cachedArtists=0,
            cachedAlbums=0,
            fetched=0,
            mapped=0,
            inserted=0,
            pruned=0,
            stored=0,
            users=0,
            oldestPlayedAt=None,
            newestPlayedAt=None,
        )
        with db() as connection:
            connection.execute("DELETE FROM plex_auth_flows")
            connection.execute("DELETE FROM pending_lidarr_searches")
            connection.execute("DELETE FROM recommendation_cache")
            connection.execute("DELETE FROM plex_listens")
            connection.execute("DELETE FROM request_history")
            connection.execute("DELETE FROM account_invitations")
            connection.execute("DELETE FROM users")
        write_settings_file({})
        with cache_db() as connection:
            connection.execute("DELETE FROM api_cache")
            connection.execute("DELETE FROM track_search_artist_snapshot_tracks")
            connection.execute("DELETE FROM track_search_artist_snapshot_meta")

    def register(self):
        response = self.client.post(
            "/api/auth/register",
            json={"username": "test-user", "password": "a-secure-password"},
        )
        self.assertEqual(response.status_code, 201)
        return response.get_json()["csrfToken"]


class ApplicationFactoryTests(DatabaseTestCase):
    def test_factory_registers_every_application_route_once(self):
        rules = [rule for rule in self.app.url_map.iter_rules() if rule.endpoint != "static"]
        route_methods = {
            (rule.rule, method)
            for rule in rules
            for method in rule.methods
            if method not in {"HEAD", "OPTIONS"}
        }
        self.assertEqual(len(rules), 108)
        self.assertEqual(len(route_methods), 108)
        for route in (("/api/discover/charts", "GET"), ("/api/discover/preferences", "GET"),
                      ("/api/discover/preferences", "POST"), ("/api/discover/request-influence", "POST")):
            self.assertIn(route, route_methods)
        self.assertIn(("/api/music/artist/<mbid>/similar", "GET"), route_methods)
        self.assertIn(
            ("/api/music/release-groups/availability", "GET"),
            route_methods,
        )
        self.assertIn(("/api/v1/animethemes/resolve", "POST"), route_methods)
        self.assertIn(("/api/settings/musicbrainz", "POST"), route_methods)
        self.assertIn(("/api/settings/musicbrainz/test", "POST"), route_methods)
        self.assertIn(("/api/settings/melodarr", "POST"), route_methods)
        self.assertIn(
            ("/api/settings/melodarr/api-key/regenerate", "POST"),
            route_methods,
        )
        notification_routes = {
            ("/api/settings/notifications", "GET"),
            ("/api/settings/notifications", "PUT"),
            ("/api/settings/notifications/global", "PUT"),
            ("/api/settings/notifications/email", "PUT"),
            ("/api/settings/notifications/web-push", "PUT"),
            ("/api/settings/notifications/email/test", "POST"),
            ("/api/settings/notifications/web-push/test", "POST"),
            ("/api/account/notifications", "GET"),
            ("/api/account/notifications", "PUT"),
            ("/api/account/notifications/subscriptions", "POST"),
            ("/api/account/notifications/subscriptions/<int:subscription_id>", "DELETE"),
            ("/api/account/notifications/mutes/<kind>/<mbid>", "GET"),
            ("/api/account/notifications/mutes/<kind>/<mbid>", "PUT"),
            ("/service-worker.js", "GET"),
        }
        self.assertTrue(notification_routes <= route_methods)
        self.assertIn(("/settings/notifications", "GET"), route_methods)

    def test_factory_applies_test_configuration(self):
        self.assertTrue(self.app.config["TESTING"])
        self.assertEqual(self.app.config["SECRET_KEY"], "test-secret")
        self.assertEqual(
            self.app.config["MAX_CONTENT_LENGTH"],
            MAX_REQUEST_BODY_BYTES,
        )

    def test_anonymous_json_body_at_limit_is_accepted(self):
        prefix = b'{"username":"missing-user","password":"'
        suffix = b'"}'
        body = (
            prefix
            + (b"x" * (MAX_REQUEST_BODY_BYTES - len(prefix) - len(suffix)))
            + suffix
        )
        self.assertEqual(len(body), MAX_REQUEST_BODY_BYTES)

        response = self.client.post(
            "/api/auth/login",
            data=body,
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(
            response.get_json(),
            {"error": "Invalid username or password."},
        )

    def test_anonymous_oversized_json_body_returns_api_error(self):
        response = self.client.post(
            "/api/auth/login",
            json={
                "username": "missing-user",
                "password": "x" * MAX_REQUEST_BODY_BYTES,
            },
        )

        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.mimetype, "application/json")
        self.assertEqual(
            response.get_json(),
            {"error": "Request body is too large."},
        )

    def test_authenticated_oversized_json_body_returns_api_error(self):
        csrf_token = self.register()

        response = self.client.post(
            "/api/settings/melodarr",
            json={"applicationTitle": "x" * MAX_REQUEST_BODY_BYTES},
            headers={"X-CSRF-Token": csrf_token},
        )

        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.mimetype, "application/json")
        self.assertEqual(
            response.get_json(),
            {"error": "Request body is too large."},
        )

    def test_automation_api_key_uses_the_environment_configuration(self):
        with patch.dict(
            os.environ,
            {"MELODARR_AUTOMATION_API_KEY": "a" * 32},
        ):
            self.assertEqual(backend_config.load_automation_api_key(), "a" * 32)

        with patch.dict(
            os.environ,
            {"MELODARR_AUTOMATION_API_KEY": "too-short"},
        ):
            with self.assertRaisesRegex(RuntimeError, "at least 32 characters"):
                backend_config.load_automation_api_key()

    def test_first_start_api_key_is_generated_once_and_persisted(self):
        with tempfile.TemporaryDirectory(prefix="melodarr-api-key-") as directory:
            settings_path = os.path.join(directory, "settings.json")
            with patch.object(storage_module, "SETTINGS_FILE", settings_path):
                storage_module._settings_cache_signature = None
                storage_module._settings_cache_value = None
                first = instance_settings.ensure_instance_settings()
                second = instance_settings.ensure_instance_settings()

                self.assertGreaterEqual(len(first["apiKey"]), 32)
                self.assertEqual(first["apiKey"], second["apiKey"])
                self.assertEqual(
                    storage_module.get_service("melodarr")["apiKey"],
                    first["apiKey"],
                )
            storage_module._settings_cache_signature = None
            storage_module._settings_cache_value = None

    def test_environment_api_key_is_not_copied_to_persistent_settings(self):
        override = "environment-owned-api-key-0123456789"
        with tempfile.TemporaryDirectory(prefix="melodarr-api-key-") as directory:
            settings_path = os.path.join(directory, "settings.json")
            with patch.object(storage_module, "SETTINGS_FILE", settings_path):
                storage_module._settings_cache_signature = None
                storage_module._settings_cache_value = None
                result = instance_settings.ensure_instance_settings(override)

                self.assertEqual(result["apiKey"], override)
                self.assertTrue(result["apiKeyManagedByEnvironment"])
                self.assertNotIn(
                    "apiKey",
                    storage_module.get_service("melodarr"),
                )
            storage_module._settings_cache_signature = None
            storage_module._settings_cache_value = None

    def test_empty_session_secret_file_is_replaced(self):
        with tempfile.TemporaryDirectory(prefix="melodarr-secret-") as directory:
            secret_path = os.path.join(directory, "session-secret.key")
            with open(secret_path, "w", encoding="utf-8") as file:
                file.write(" \n")

            with patch.object(
                backend_config,
                "SECRET_KEY_FILE",
                secret_path,
            ):
                secret = backend_config.load_session_secret()

            self.assertEqual(len(secret), 96)
            self.assertTrue(secret)
            with open(secret_path, encoding="utf-8") as file:
                self.assertEqual(file.read(), secret)

    def test_legacy_manifest_url_redirects_to_the_active_manifest(self):
        response = self.client.get("/manifest.webmanifest")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/static/site.webmanifest")

    def test_conventional_icon_urls_serve_canonical_brand_assets(self):
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cases = (
            ("/favicon.ico", "melodarr.svg"),
            ("/apple-touch-icon.png", "melodarr-180.png"),
        )

        for url, filename in cases:
            with self.subTest(url=url):
                with self.client.get(url) as response:
                    with open(
                        os.path.join(project_root, "frontend", "icons", filename),
                        "rb",
                    ) as file:
                        expected = file.read()

                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.data, expected)


class PlexListenStorageTests(DatabaseTestCase):
    def test_listens_are_deduplicated_scoped_and_pruned(self):
        self.register()
        with db() as connection:
            user_id = connection.execute(
                "SELECT id FROM users WHERE username = 'test-user'"
            ).fetchone()["id"]
        listens = [
            {
                "server_id": "server-1",
                "history_key": "history-1",
                "user_id": user_id,
                "artist_rating_key": "artist-1",
                "album_rating_key": "album-1",
                "played_at": 1_000.0,
            },
            {
                "server_id": "server-1",
                "history_key": "history-2",
                "user_id": user_id,
                "artist_rating_key": "artist-2",
                "album_rating_key": None,
                "played_at": 2_000.0,
            },
        ]

        self.assertEqual(insert_plex_listens(listens), 2)
        self.assertEqual(insert_plex_listens(listens), 0)
        rows = get_plex_listens(user_id, 1_500, server_id="server-1")
        self.assertEqual([row["history_key"] for row in rows], ["history-2"])
        self.assertEqual(plex_listen_stats(user_id=user_id)["count"], 2)
        self.assertEqual(prune_plex_listens(1_500), 1)
        self.assertEqual(plex_listen_stats(user_id=user_id)["count"], 1)


class SettingsStorageTests(DatabaseTestCase):
    def test_upgrade_removes_legacy_ai_data_without_touching_user_data(self):
        with tempfile.TemporaryDirectory(prefix="melodarr-ai-removal-") as directory:
            database = os.path.join(directory, "melodarr.db")
            settings_path = os.path.join(directory, "settings.json")
            with (
                patch.object(storage_module, "DATABASE", database),
                patch.object(storage_module, "SETTINGS_FILE", settings_path),
            ):
                storage_module.init_db()
                with storage_module.db() as connection:
                    user_id = connection.execute(
                        "INSERT INTO users "
                        "(username, password_hash, role, created_at) "
                        "VALUES ('upgrade-user', 'hash', 'user', 1)"
                    ).lastrowid
                    connection.execute(
                        "INSERT INTO request_history "
                        "(user_id, kind, mbid, name, created_at) "
                        "VALUES (?, 'artist', 'artist-mbid', 'Kept Artist', 2)",
                        (user_id,),
                    )
                    connection.execute(
                        "INSERT INTO plex_listens "
                        "(server_id, history_key, user_id, artist_rating_key, "
                        "album_rating_key, played_at) VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            "server-1",
                            "history-1",
                            user_id,
                            "artist-1",
                            "album-1",
                            3,
                        ),
                    )
                    connection.execute(
                        "CREATE TABLE listening_profiles "
                        "(user_id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
                    )
                    connection.execute(
                        "INSERT INTO listening_profiles (user_id, value) "
                        "VALUES (?, ?)",
                        (user_id, '{"private":"taste-data"}'),
                    )
                storage_module.write_settings_file({
                    "ai": {
                        "provider": "openai",
                        "apiKey": "legacy-ai-secret",
                    },
                    "lidarr": {"url": "http://lidarr:8686"},
                    "lastfm": {"apiKey": "shared-lastfm-key"},
                    "plex": {
                        "url": "http://plex:32400",
                        "token": "preserved-plex-token",
                        "machineIdentifier": "server-1",
                    },
                })

                storage_module.init_db()

                with storage_module.db() as connection:
                    profile_table = connection.execute(
                        "SELECT 1 FROM sqlite_master "
                        "WHERE type = 'table' AND name = 'listening_profiles'"
                    ).fetchone()
                    user = connection.execute(
                        "SELECT username FROM users WHERE id = ?", (user_id,)
                    ).fetchone()
                    request = connection.execute(
                        "SELECT name FROM request_history WHERE user_id = ?",
                        (user_id,),
                    ).fetchone()
                    listen = connection.execute(
                        "SELECT history_key FROM plex_listens WHERE user_id = ?",
                        (user_id,),
                    ).fetchone()
                settings = storage_module.load_settings_file()

            self.assertIsNone(profile_table)
            self.assertEqual(user["username"], "upgrade-user")
            self.assertEqual(request["name"], "Kept Artist")
            self.assertEqual(listen["history_key"], "history-1")
            self.assertNotIn("ai", settings)
            self.assertEqual(settings["lidarr"], {"url": "http://lidarr:8686"})
            self.assertEqual(settings["lastfm"], {"apiKey": "shared-lastfm-key"})
            self.assertEqual(settings["plex"], {
                "url": "http://plex:32400",
                "token": "preserved-plex-token",
                "machineIdentifier": "server-1",
            })
            self.assertNotIn("legacy-ai-secret", json.dumps(settings))

    def test_parallel_service_saves_preserve_unrelated_settings(self):
        write_barrier = Barrier(2)
        real_write = storage_module.write_settings_file
        errors = []

        def delayed_write(settings):
            try:
                write_barrier.wait(timeout=0.25)
            except BrokenBarrierError:
                pass
            real_write(settings)

        def save(name, values):
            try:
                save_service(name, values)
            except Exception as exc:
                errors.append(exc)

        with patch.object(
            storage_module,
            "write_settings_file",
            side_effect=delayed_write,
        ):
            threads = [
                Thread(target=save, args=("lidarr", {"url": "http://lidarr"})),
                Thread(target=save, args=("plex", {"url": "http://plex"})),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=2)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(get_service("lidarr"), {"url": "http://lidarr"})
        self.assertEqual(get_service("plex"), {"url": "http://plex"})


class WorkerEntrypointTests(unittest.TestCase):
    def test_refresh_request_wakes_sleeping_worker(self):
        recommendation_worker.refresh_requested.clear()
        recommendation_worker.request_refresh()
        self.assertTrue(recommendation_worker.refresh_requested.is_set())
        recommendation_worker.refresh_requested.clear()

    @patch("backend.worker.Thread")
    @patch("backend.worker.recommendation_worker.run")
    @patch("backend.worker.init_db")
    @patch("backend.worker.init_cache_db")
    def test_worker_initializes_storage_and_background_loops(
        self, init_cache_db, init_db, run, thread_class
    ):
        calls = []
        anime_metadata_thread = Mock()
        anime_enrichment_thread = Mock()
        artist_metadata_thread = Mock()
        similar_artist_thread = Mock()
        lidarr_thread = Mock()
        lidarr_download_thread = Mock()
        plex_thread = Mock()
        plex_metadata_thread = Mock()
        plex_history_thread = Mock()
        lidarr_library_thread = Mock()
        notification_thread = Mock()
        thread_class.side_effect = [
            anime_metadata_thread,
            anime_enrichment_thread,
            artist_metadata_thread,
            similar_artist_thread,
            lidarr_thread,
            lidarr_download_thread,
            lidarr_library_thread,
            plex_thread,
            plex_metadata_thread,
            plex_history_thread,
            notification_thread,
            Mock(), Mock(),
        ]
        init_db.side_effect = lambda: calls.append("database")
        init_cache_db.side_effect = lambda: calls.append("cache")
        run.side_effect = lambda *_args: calls.append("recommendations")
        worker.main()
        self.assertEqual(calls, ["cache", "database", "recommendations"])
        self.assertEqual(thread_class.call_count, 13)
        thread_class.assert_any_call(
            target=anime_metadata_worker.run,
            name="anime-musicbrainz-resolution",
            daemon=True,
        )
        thread_class.assert_any_call(
            target=artist_metadata_worker.run,
            name="musicbrainz-artist-revalidation",
            daemon=True,
        )
        thread_class.assert_any_call(
            target=similar_artist_worker.run,
            name="lastfm-similar-artist-resolution",
            daemon=True,
        )
        thread_class.assert_any_call(
            target=lidarr_search_worker.run, name="lidarr-search-followups", daemon=True
        )
        thread_class.assert_any_call(
            target=lidarr_download_worker.run,
            name="lidarr-download-status",
            daemon=True,
        )
        thread_class.assert_any_call(
            target=worker.notification_worker.run,
            name="notification-deliveries",
            daemon=True,
        )
        thread_class.assert_any_call(
            target=lidarr_library_worker.run,
            args=(worker.LIDARR_LIBRARY_STARTUP_DELAY,),
            name="lidarr-library-scan",
            daemon=True,
        )
        thread_class.assert_any_call(
            target=plex_worker.run,
            args=(worker.PLEX_LIBRARY_STARTUP_DELAY,),
            name="plex-library-scans",
            daemon=True,
        )
        thread_class.assert_any_call(
            target=plex_metadata_worker.run,
            name="plex-musicbrainz-enrichment",
            daemon=True,
        )
        thread_class.assert_any_call(
            target=plex_history_worker.run,
            args=(worker.PLEX_HISTORY_STARTUP_DELAY,),
            name="plex-listening-history",
            daemon=True,
        )
        lidarr_thread.start.assert_called_once_with()
        lidarr_download_thread.start.assert_called_once_with()
        lidarr_library_thread.start.assert_called_once_with()
        plex_thread.start.assert_called_once_with()
        plex_metadata_thread.start.assert_called_once_with()
        plex_history_thread.start.assert_called_once_with()
        anime_metadata_thread.start.assert_called_once_with()
        anime_enrichment_thread.start.assert_called_once_with()
        thread_class.assert_any_call(target=worker.anime_artist_enrichment.run,
                                     name="anime-artist-enrichment", daemon=True)
        notification_thread.start.assert_called_once_with()
        artist_metadata_thread.start.assert_called_once_with()
        similar_artist_thread.start.assert_called_once_with()
        for country in ("us", "jp"):
            thread_class.assert_any_call(target=worker.chart_worker.run, args=(country,),
                                         name=f"album-chart-{country}", daemon=True)
        run.assert_called_once_with(worker.RECOMMENDATION_STARTUP_DEADLINE)

    @patch("backend.workers.lidarr_library.time.time", return_value=100)
    @patch("backend.workers.lidarr_library._run_scan", side_effect=StopIteration)
    def test_lidarr_startup_delay_preserves_an_early_manual_wake(
        self, run_scan, current_time
    ):
        lidarr_library_worker.wake_requested.set()

        with self.assertRaises(StopIteration):
            lidarr_library_worker.run(initial_delay=10)

        run_scan.assert_called_once_with()
        lidarr_library_worker.wake_requested.clear()

    @patch(
        "backend.workers.recommendations.refresh_recommendation_cache",
        side_effect=_StopWorker,
    )
    @patch("backend.workers.recommendations.time.time", return_value=100)
    @patch("backend.workers.recommendations.refresh_requested.wait")
    def test_recommendations_wait_for_startup_deadline(
        self, wait, current_time, refresh
    ):
        recommendation_worker.refresh_requested.clear()

        with self.assertRaises(_StopWorker):
            recommendation_worker.run(initial_delay=120)

        wait.assert_called_once_with(120)

    @patch(
        "backend.workers.recommendations.refresh_recommendation_cache",
        side_effect=RuntimeError("unexpected refresh failure"),
    )
    @patch(
        "backend.workers.recommendations.refresh_requested.wait",
        side_effect=[False, _StopWorker],
    )
    def test_recommendation_worker_retries_after_unexpected_failure(
        self, wait, refresh
    ):
        recommendation_worker.refresh_requested.clear()
        recommendation_worker.running.clear()

        with self.assertLogs(
            "backend.workers.recommendations",
            level="ERROR",
        ):
            with self.assertRaises(_StopWorker):
                recommendation_worker.run()

        refresh.assert_called_once_with()
        self.assertEqual(wait.call_count, 2)
        self.assertFalse(recommendation_worker.running.is_set())

    @patch("backend.workers.plex_history.recommendation_worker.request_refresh")
    @patch("backend.workers.plex_history._run_sync", side_effect=StopIteration)
    def test_first_history_attempt_releases_recommendation_startup(
        self, run_sync, request_refresh
    ):
        with plex_history_worker.request_lock:
            plex_history_worker.sync_requested = True

        with self.assertRaises(StopIteration):
            plex_history_worker.run(initial_delay=60)

        run_sync.assert_called_once_with(full=False)
        request_refresh.assert_called_once_with()

    @patch("backend.worker.Thread")
    def test_background_worker_uses_one_daemon_thread(self, thread_class):
        thread = Mock()
        thread_class.return_value = thread
        result = worker.start_background_thread()
        self.assertIs(result, thread)
        thread_class.assert_called_once_with(
            target=worker.main,
            name="recommendation-refresh",
            daemon=True,
        )
        thread.start.assert_called_once_with()


class PlexMetadataWorkerTests(unittest.TestCase):
    def setUp(self):
        with plex_metadata_worker.queue_lock:
            plex_metadata_worker.queued_artist_ids.clear()
            plex_metadata_worker.queued_release_ids.clear()
            plex_metadata_worker.full_enrichment_requested = False
            plex_metadata_worker.job_state["queued"] = 0
        plex_metadata_worker.wake_requested.clear()

    @patch("backend.workers.plex_metadata.plex.apply_release_group_mappings")
    @patch("backend.workers.plex_metadata.plex.unresolved_musicbrainz_releases")
    @patch("backend.workers.plex_metadata.musicbrainz.get")
    def test_release_ids_are_resolved_to_release_groups_in_background(
        self, musicbrainz_get, unresolved, apply_mappings
    ):
        unresolved.return_value = [{"musicbrainzReleaseId": "release-1"}]
        musicbrainz_get.return_value = {
            "release-group": {"id": "release-group-1"},
            "artist-credit": [{"artist": {"id": "artist-1"}}],
        }

        plex_metadata_worker._resolve_release_groups({"url": "http://plex"})

        musicbrainz_get.assert_called_once_with(
            "/release/release-1",
            "release-groups+artist-credits",
            priority="background",
        )
        apply_mappings.assert_called_once_with(
            {"url": "http://plex"},
            {"release-1": "release-group-1"},
            artist_mappings={"release-1": "artist-1"},
        )

    @patch("backend.workers.plex_metadata.plex.music_library")
    @patch("backend.workers.plex_metadata.musicbrainz.get")
    def test_plex_artist_discography_uses_the_normal_metadata_cache(
        self, musicbrainz_get, music_library
    ):
        music_library.return_value = [{"musicbrainzId": "artist-1"}]
        musicbrainz_get.side_effect = [
            {"id": "artist-1"},
            {"release-groups": [], "release-group-count": 0},
        ]

        plex_metadata_worker._warm_artist_discographies({"url": "http://plex"})

        self.assertEqual(musicbrainz_get.call_count, 2)
        musicbrainz_get.assert_any_call(
            "/artist/artist-1", "url-rels+genres", priority="background"
        )
        musicbrainz_get.assert_any_call(
            "/release-group",
            "",
            priority="background",
            artist="artist-1",
            limit=100,
            offset=0,
        )

    @patch("backend.workers.plex_metadata.plex.music_library")
    @patch("backend.workers.plex_metadata.musicbrainz.get")
    def test_targeted_artist_enrichment_does_not_walk_the_plex_library(
        self, musicbrainz_get, music_library
    ):
        musicbrainz_get.side_effect = [
            {"id": "artist-2"},
            {"release-groups": [], "release-group-count": 0},
        ]

        plex_metadata_worker._warm_artist_discographies(
            {"url": "http://plex"}, {"artist-2"}
        )

        music_library.assert_not_called()
        musicbrainz_get.assert_any_call(
            "/artist/artist-2", "url-rels+genres", priority="background"
        )

    @patch("backend.workers.plex_metadata.plex.unresolved_musicbrainz_releases")
    @patch("backend.workers.plex_metadata.plex.apply_release_group_mappings")
    @patch("backend.workers.plex_metadata.musicbrainz.get")
    def test_targeted_release_enrichment_does_not_walk_unresolved_inventory(
        self, musicbrainz_get, apply_mappings, unresolved
    ):
        musicbrainz_get.return_value = {
            "release-group": {"id": "release-group-2"},
            "artist-credit": [{"artist": {"id": "artist-2"}}],
        }

        plex_metadata_worker._resolve_release_groups(
            {"url": "http://plex"}, {"release-2"}
        )

        unresolved.assert_not_called()
        apply_mappings.assert_called_once_with(
            {"url": "http://plex"},
            {"release-2": "release-group-2"},
            artist_mappings={"release-2": "artist-2"},
        )

    @patch("backend.workers.plex_metadata.wake_requested.set")
    def test_enrichment_queue_deduplicates_targets_and_keeps_manual_full_pass(
        self, wake
    ):
        plex_metadata_worker.request_enrichment(
            artist_ids=["artist-1", "artist-1"],
            release_ids=["release-1"],
        )
        plex_metadata_worker.request_enrichment(
            artist_ids=["artist-1"],
            release_ids=["release-1"],
        )

        self.assertEqual(plex_metadata_worker.queued_artist_ids, {"artist-1"})
        self.assertEqual(plex_metadata_worker.queued_release_ids, {"release-1"})
        self.assertEqual(plex_metadata_worker.job_state["queued"], 2)

        plex_metadata_worker.request_enrichment()

        self.assertTrue(plex_metadata_worker.full_enrichment_requested)
        self.assertEqual(plex_metadata_worker.job_state["queued"], 1)
        self.assertEqual(wake.call_count, 3)


class ArtistMetadataWorkerTests(DatabaseTestCase):
    artist_id = "11111111-1111-1111-1111-111111111111"

    def _cache_value(self, key):
        with cache_db() as connection:
            row = connection.execute(
                "SELECT value FROM api_cache WHERE cache_key = ?",
                (key,),
            ).fetchone()
        return json.loads(row["value"]) if row else None

    @patch("backend.workers.artist_metadata._cached_discography_page")
    def test_click_queue_deduplicates_artist_and_requires_cached_discography(
        self, cached_page
    ):
        cached_page.return_value = {
            "release-groups": [],
            "release-group-count": 0,
        }

        first = artist_metadata_worker.request_revalidation(self.artist_id)
        second = artist_metadata_worker.request_revalidation(self.artist_id)

        self.assertTrue(first["polling"])
        self.assertEqual(first["status"], "queued")
        self.assertTrue(second["polling"])
        self.assertEqual(
            artist_metadata_worker.queued_artist_ids,
            {self.artist_id},
        )
        cached_page.assert_called_once_with(self.artist_id)

    @patch("backend.workers.artist_metadata.track_search_index.artist_track_snapshot_info")
    @patch("backend.workers.artist_metadata.musicbrainz.artist_entity_counts")
    @patch("backend.workers.artist_metadata.musicbrainz.get")
    def test_equal_count_records_check_without_full_refresh(
        self, get, counts, snapshot,
    ):
        get.return_value = {
            "release-groups": [{"id": "cached"}], "release-group-count": 1,
        }
        counts.return_value = {
            "releaseGroupCount": 1, "releaseCount": 2, "recordingCount": 3,
        }
        snapshot.return_value = {
            "release_count": 2, "recording_count": 3,
            "indexed_at": time.time(),
        }

        artist_metadata_worker._process_artist(self.artist_id)

        get.assert_called_once()
        counts.assert_called_once_with(self.artist_id, priority="background")
        state = get_cache_document(
            artist_metadata_worker.STATE_NAMESPACE,
            self.artist_id,
        )
        self.assertEqual(state["outcome"], "unchanged")
        self.assertEqual(state["cachedCount"], 1)
        self.assertEqual(state["observedCount"], 1)
        self.assertGreater(state["nextCheckAt"], state["lastCheckedAt"])

        get.reset_mock()
        result = artist_metadata_worker.request_revalidation(self.artist_id)
        self.assertFalse(result["polling"])
        self.assertEqual(result["status"], "unchanged")
        get.assert_not_called()

    @patch("backend.workers.artist_metadata._refresh_artist_tracks")
    @patch("backend.workers.artist_metadata.track_search_index.artist_track_snapshot_info")
    @patch("backend.workers.artist_metadata.musicbrainz.artist_entity_counts")
    @patch("backend.workers.artist_metadata.musicbrainz.get")
    def test_recording_count_change_rebuilds_tracks_without_album_refresh(
        self, get, counts, snapshot, refresh_tracks,
    ):
        get.return_value = {"release-groups": [], "release-group-count": 0}
        counts.return_value = {
            "releaseGroupCount": 0, "releaseCount": 1, "recordingCount": 2,
        }
        snapshot.return_value = {
            "release_count": 1, "recording_count": 1,
            "indexed_at": time.time(),
        }

        artist_metadata_worker._process_artist(self.artist_id)

        refresh_tracks.assert_called_once_with(self.artist_id, counts.return_value)
        get.assert_called_once()
        self.assertEqual(
            get_cache_document(
                artist_metadata_worker.STATE_NAMESPACE, self.artist_id,
            )["outcome"], "unchanged",
        )

    def test_newly_cached_discography_does_not_trigger_redundant_probe(self):
        page = {"release-groups": [], "release-group-count": 0}
        commit_json_responses([musicbrainz.metadata_cache_record(
            "/release-group",
            "aliases",
            page,
            artist=self.artist_id,
            limit=100,
            offset=0,
        )])

        result = artist_metadata_worker.request_revalidation(self.artist_id)

        self.assertTrue(result["polling"])
        self.assertEqual(result["status"], "queued")
        self.assertEqual(
            artist_metadata_worker.forced_track_artist_ids, {self.artist_id},
        )

    @patch("backend.workers.artist_metadata._cached_discography_page")
    def test_failed_initial_track_build_honors_retry_cooldown(self, cached_page):
        cached_page.return_value = {
            "release-groups": [], "release-group-count": 0,
        }
        artist_metadata_worker._record_failure(self.artist_id)

        result = artist_metadata_worker.request_revalidation(self.artist_id)

        self.assertFalse(result["polling"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(artist_metadata_worker.queued_artist_ids, set())

    @patch(
        "backend.workers.artist_metadata.track_search_index."
        "replace_musicbrainz_artist_discography"
    )
    @patch("backend.workers.artist_metadata._refresh_artist_tracks")
    @patch("backend.workers.artist_metadata.musicbrainz.artist_entity_counts")
    @patch("backend.workers.artist_metadata.musicbrainz.get")
    def test_changed_count_refreshes_pages_then_artist_and_removes_old_pages(
        self, get, counts, refresh_tracks, replace_discography
    ):
        counts.return_value = {
            "releaseGroupCount": 102, "releaseCount": 120,
            "recordingCount": 130,
        }
        old_offset = 200
        old_key = musicbrainz.metadata_cache_key(
            "/release-group",
            "aliases",
            artist=self.artist_id,
            limit=100,
            offset=old_offset,
        )
        commit_json_responses([musicbrainz.metadata_cache_record(
            "/release-group",
            "aliases",
            {"release-groups": [{"id": "obsolete"}], "release-group-count": 201},
            artist=self.artist_id,
            limit=100,
            offset=old_offset,
        )])
        first_page_groups = [
            {"id": f"group-{index}", "title": f"Group {index}"}
            for index in range(100)
        ]
        second_page_groups = [
            {"id": "group-100", "title": "Group 100"},
            {"id": "group-101", "title": "Group 101"},
        ]
        get.side_effect = [
            {"release-groups": [], "release-group-count": 201},
            {
                "release-groups": first_page_groups,
                "release-group-count": 102,
            },
            {
                "release-groups": second_page_groups,
                "release-group-count": 102,
            },
            {
                "id": self.artist_id,
                "name": "Fresh Artist",
                "relations": [],
                "genres": [],
            },
        ]

        artist_metadata_worker._process_artist(self.artist_id)

        paths = [call.args[0] for call in get.call_args_list]
        self.assertEqual(paths, [
            "/release-group",
            "/release-group",
            "/release-group",
            f"/artist/{self.artist_id}",
        ])
        staged_calls = get.call_args_list[1:]
        self.assertTrue(all(
            call.kwargs["priority"] == "background"
            and call.kwargs["force_refresh"]
            and not call.kwargs["cache_response"]
            for call in staged_calls
        ))
        self.assertEqual(get.call_args_list[1].kwargs["offset"], 0)
        self.assertEqual(get.call_args_list[2].kwargs["offset"], 100)
        refresh_tracks.assert_called_once_with(self.artist_id, counts.return_value)
        self.assertIsNone(self._cache_value(old_key))

        artist_key = musicbrainz.metadata_cache_key(
            f"/artist/{self.artist_id}",
            "aliases+url-rels+genres",
        )
        self.assertEqual(
            self._cache_value(artist_key)["name"],
            "Fresh Artist",
        )
        state = get_cache_document(
            artist_metadata_worker.STATE_NAMESPACE,
            self.artist_id,
        )
        self.assertEqual(state["outcome"], "refreshed")
        self.assertEqual(state["cachedCount"], 102)
        self.assertEqual(state["observedCount"], 102)
        replace_discography.assert_called_once()
        self.assertEqual(replace_discography.call_args.args[0], self.artist_id)
        self.assertEqual(len(replace_discography.call_args.args[1]), 2)
        self.assertIn(
            old_key,
            replace_discography.call_args.kwargs["obsolete_cache_keys"],
        )

    @patch(
        "backend.workers.artist_metadata.track_search_index."
        "replace_musicbrainz_artist_discography",
        side_effect=sqlite3.OperationalError("index is busy"),
    )
    @patch("backend.workers.artist_metadata.commit_json_responses")
    @patch(
        "backend.workers.artist_metadata._stage_artist_refresh",
        return_value=([], set(), 2, []),
    )
    def test_index_reconciliation_failure_does_not_discard_completed_refresh(
        self, stage_refresh, commit_responses, replace_discography
    ):
        result = artist_metadata_worker.refresh_artist_metadata(
            self.artist_id,
            "background",
            cached_count=1,
        )

        self.assertEqual(result["count"], 2)
        commit_responses.assert_called_once_with([], delete_keys=set())
        replace_discography.assert_called_once()
        state = get_cache_document(
            artist_metadata_worker.STATE_NAMESPACE,
            self.artist_id,
        )
        self.assertEqual(state["outcome"], "refreshed")
        self.assertEqual(state["cachedCount"], 2)

    @patch("backend.workers.artist_metadata.musicbrainz.get")
    def test_failed_staged_refresh_preserves_previous_cache(self, get):
        old_page = {
            "release-groups": [{"id": "old-group"}],
            "release-group-count": 1,
        }
        old_artist = {
            "id": self.artist_id,
            "name": "Old Artist",
        }
        page_key = musicbrainz.metadata_cache_key(
            "/release-group",
            "aliases",
            artist=self.artist_id,
            limit=100,
            offset=0,
        )
        artist_key = musicbrainz.metadata_cache_key(
            f"/artist/{self.artist_id}",
            "aliases+url-rels+genres",
        )
        commit_json_responses([
            musicbrainz.metadata_cache_record(
                "/release-group",
                "aliases",
                old_page,
                artist=self.artist_id,
                limit=100,
                offset=0,
            ),
            musicbrainz.metadata_cache_record(
                f"/artist/{self.artist_id}",
                "aliases+url-rels+genres",
                old_artist,
            ),
        ])
        get.side_effect = [
            old_page,
            {
                "release-groups": [
                    {"id": "new-one"},
                    {"id": "new-two"},
                ],
                "release-group-count": 2,
            },
            requests.Timeout("artist lookup timed out"),
        ]

        with self.assertRaises(requests.Timeout):
            artist_metadata_worker.refresh_artist_metadata(
                self.artist_id,
                "background",
            )

        self.assertEqual(self._cache_value(page_key), old_page)
        self.assertEqual(self._cache_value(artist_key), old_artist)

    @patch("backend.workers.artist_metadata.time.time", return_value=1_000)
    def test_failure_uses_retry_cooldown(self, now):
        artist_metadata_worker._record_failure(self.artist_id)

        state = get_cache_document(
            artist_metadata_worker.STATE_NAMESPACE,
            self.artist_id,
        )
        self.assertEqual(state["outcome"], "failed")
        self.assertEqual(
            state["nextCheckAt"],
            1_000
            + artist_metadata_worker.MUSICBRAINZ_ARTIST_REVALIDATION_RETRY_INTERVAL,
        )

    @patch("backend.workers.artist_metadata.musicbrainz.get")
    def test_track_refresh_advances_by_actual_release_page_size(self, get):
        group_id = "22222222-2222-4222-8222-222222222222"
        releases = [
            {
                "id": f"44444444-4444-4444-8444-{number:012d}",
                "release-group": {"id": group_id},
                "media": [{"tracks": [{
                    "title": "じゃあな",
                    "recording": {
                        "id": "33333333-3333-4333-8333-333333333333",
                        "title": "じゃあな",
                    },
                }]}],
            }
            for number in range(2)
        ]
        get.side_effect = [
            {"release-count": 2, "releases": releases[:1]},
            {"release-count": 2, "releases": releases[1:]},
        ]

        artist_metadata_worker._refresh_artist_tracks(
            self.artist_id, {"recordingCount": 1},
        )

        self.assertEqual(
            [call.kwargs["offset"] for call in get.call_args_list], [0, 1],
        )
        self.assertEqual(
            track_search_index.artist_track_snapshot_info(self.artist_id)[
                "release_count"
            ], 2,
        )
        self.assertEqual(
            track_search_index.search_artist_tracks(self.artist_id, "jaana"),
            [{
                "normalized_title": track_search_index.normalize_text("じゃあな"),
                "release_group_mbid": group_id,
            }],
        )

    @patch("backend.workers.artist_metadata.musicbrainz.get")
    def test_incomplete_track_refresh_keeps_previous_snapshot(self, get):
        group_id = "22222222-2222-4222-8222-222222222222"
        old_release = {
            "id": "44444444-4444-4444-8444-444444444444",
            "release-group": {"id": group_id},
            "media": [{"tracks": [{"title": "Old song"}]}],
        }
        track_search_index.replace_artist_track_snapshot(
            self.artist_id, [old_release],
            release_count=1, recording_count=1,
        )
        get.side_effect = [
            {"release-count": 2, "releases": [old_release]},
            requests.Timeout("second page timed out"),
        ]

        with self.assertRaises(requests.Timeout):
            artist_metadata_worker._refresh_artist_tracks(
                self.artist_id, {"recordingCount": 2},
            )

        self.assertEqual(
            track_search_index.search_artist_tracks(self.artist_id, "Old song"),
            [{"normalized_title": "old song", "release_group_mbid": group_id}],
        )
        self.assertEqual(
            track_search_index.artist_track_snapshot_info(self.artist_id)[
                "recording_count"
            ], 1,
        )


class PlexScanWorkerTests(unittest.TestCase):
    @patch("backend.workers.plex.remove_stale_plex_artist_artwork")
    @patch("backend.workers.plex.plex_metadata.request_enrichment")
    @patch("backend.workers.plex.plex.full_library_scan")
    @patch("backend.workers.plex.get_service")
    def test_full_scan_retains_only_current_thumbnail_versions(
        self,
        get_service,
        full_library_scan,
        request_enrichment,
        remove_stale_artwork,
    ):
        get_service.return_value = {
            "url": "http://plex",
            "machineIdentifier": "server-1",
        }
        full_library_scan.return_value = {
            "artists": [{
                "ratingKey": "100",
                "thumb": "/library/metadata/100/thumb/200",
            }],
            "artistMbids": [],
            "releaseMbids": [],
            "changed": True,
        }

        plex_worker._run_scan("full")

        remove_stale_artwork.assert_called_once_with({
            artwork_cache.plex_artist_artwork_key(
                "server-1",
                "100",
                "/library/metadata/100/thumb/200",
            )
        })
        request_enrichment.assert_not_called()

    @patch("backend.workers.plex.plex_metadata.request_enrichment")
    @patch("backend.workers.plex.plex.recently_added_scan")
    @patch("backend.workers.plex.get_service")
    def test_unchanged_recent_scan_does_not_queue_enrichment(
        self, get_service, recently_added_scan, request_enrichment
    ):
        get_service.return_value = {"url": "http://plex"}
        recently_added_scan.return_value = {
            "artists": [],
            "artistMbids": [],
            "releaseMbids": [],
            "changed": False,
        }

        plex_worker._run_scan("recent")

        request_enrichment.assert_not_called()

    @patch("backend.workers.plex.plex_metadata.request_enrichment")
    @patch("backend.workers.plex.plex.recently_added_scan")
    @patch("backend.workers.plex.get_service")
    def test_recent_scan_queues_only_returned_enrichment_targets(
        self, get_service, recently_added_scan, request_enrichment
    ):
        get_service.return_value = {"url": "http://plex"}
        recently_added_scan.return_value = {
            "artists": [],
            "artistMbids": ["artist-2"],
            "releaseMbids": ["release-2"],
            "changed": True,
        }

        plex_worker._run_scan("recent")

        request_enrichment.assert_called_once_with(
            artist_ids=["artist-2"],
            release_ids=["release-2"],
        )


class LidarrSearchWorkerTests(unittest.TestCase):
    @patch("backend.workers.lidarr_searches.set_lidarr_refresh_command")
    @patch("backend.workers.lidarr_searches.lidarr.start_command")
    def test_job_starts_and_persists_album_refresh(self, start_command, set_refresh):
        start_command.return_value = Response(201, {"id": 55})
        job = {
            "id": 1,
            "name": "Queued Album",
            "album_id": 33,
            "artist_id": 44,
            "refresh_command_id": None,
            "search_command_id": None,
        }

        lidarr_search_worker.process_job(job)

        start_command.assert_called_once_with({
            "name": "RefreshAlbum",
            "albumId": 33,
        })
        set_refresh.assert_called_once_with([1], 55)

    @patch("backend.workers.lidarr_searches.set_lidarr_refresh_command")
    @patch("backend.workers.lidarr_searches.lidarr.start_command")
    def test_same_artist_jobs_refresh_each_album(self, start_command, set_refresh):
        start_command.side_effect = [
            Response(201, {"id": 55}),
            Response(201, {"id": 56}),
        ]
        jobs = [
            {
                "id": job_id,
                "name": f"Queued Album {job_id}",
                "album_id": album_id,
                "artist_id": 44,
                "refresh_command_id": None,
                "search_command_id": None,
            }
            for job_id, album_id in ((1, 33), (2, 34))
        ]

        lidarr_search_worker.process_jobs(jobs)

        self.assertEqual(start_command.call_count, 2)
        start_command.assert_any_call({"name": "RefreshAlbum", "albumId": 33})
        start_command.assert_any_call({"name": "RefreshAlbum", "albumId": 34})
        self.assertEqual(set_refresh.call_args_list[0].args, ([1], 55))
        self.assertEqual(set_refresh.call_args_list[1].args, ([2], 56))

    @patch("backend.workers.lidarr_searches.set_lidarr_refresh_command")
    @patch("backend.workers.lidarr_searches.lidarr.start_command")
    def test_legacy_artist_refresh_job_is_forced_to_album(
        self, start_command, set_refresh
    ):
        start_command.return_value = Response(201, {"id": 56})
        job = {
            "id": 1,
            "name": "Existing Artist Album",
            "album_id": 33,
            "artist_id": 44,
            "refresh_type": "artist",
            "refresh_command_id": None,
            "search_command_id": None,
        }

        lidarr_search_worker.process_job(job)

        start_command.assert_called_once_with({
            "name": "RefreshAlbum",
            "albumId": 33,
        })
        set_refresh.assert_called_once_with([1], 56)

    @patch("backend.workers.lidarr_searches.lidarr_downloads.request_poll")
    @patch("backend.workers.lidarr_searches.set_lidarr_search_command")
    @patch("backend.workers.lidarr_searches.lidarr.start_command")
    @patch("backend.workers.lidarr_searches.lidarr.monitor_albums")
    @patch("backend.workers.lidarr_searches.lidarr.command")
    def test_completed_refresh_queues_album_search(
        self, command, monitor_albums, start_command, set_search, request_poll
    ):
        operations = []
        command.return_value = Response(200, {"status": "completed"})
        monitor_albums.side_effect = lambda album_ids: (
            operations.append(("monitor", album_ids)) or Response(200)
        )
        start_command.side_effect = lambda values: (
            operations.append(("search", values)) or Response(201, {"id": 66})
        )
        job = {
            "id": 1,
            "name": "Queued Album",
            "album_id": 33,
            "artist_id": 44,
            "refresh_command_id": 55,
            "search_command_id": None,
        }

        lidarr_search_worker.process_job(job)

        command.assert_called_once_with(55)
        monitor_albums.assert_called_once_with([33])
        start_command.assert_called_once_with({
            "name": "AlbumSearch",
            "albumIds": [33],
        })
        self.assertEqual([name for name, _ in operations], ["monitor", "search"])
        set_search.assert_called_once_with(1, 66)
        request_poll.assert_called_once_with()

    @patch("backend.workers.lidarr_searches.defer_lidarr_search")
    @patch("backend.workers.lidarr_searches.lidarr.start_command")
    @patch("backend.workers.lidarr_searches.lidarr.monitor_albums")
    @patch("backend.workers.lidarr_searches.lidarr.command")
    def test_failed_monitor_update_defers_without_searching(
        self, command, monitor_albums, start_command, defer_search
    ):
        command.return_value = Response(200, {"status": "completed"})
        monitor_albums.return_value = Response(500, text="monitor failed")
        job = {
            "id": 1,
            "name": "Unmonitored Album",
            "album_id": 33,
            "artist_id": 44,
            "refresh_command_id": 55,
            "search_command_id": None,
        }

        lidarr_search_worker.process_job(job)

        monitor_albums.assert_called_once_with([33])
        start_command.assert_not_called()
        defer_search.assert_called_once()

    @patch("backend.workers.lidarr_searches.set_lidarr_search_command")
    @patch("backend.workers.lidarr_searches.lidarr.start_command")
    @patch("backend.workers.lidarr_searches.lidarr.monitor_albums")
    @patch("backend.workers.lidarr_searches.lidarr.command")
    def test_shared_refresh_is_polled_once_then_searches_every_album(
        self, command, monitor_albums, start_command, set_search
    ):
        command.return_value = Response(200, {"status": "completed"})
        monitor_albums.return_value = Response(200)
        start_command.side_effect = [
            Response(201, {"id": 66}),
            Response(201, {"id": 67}),
        ]
        jobs = [
            {
                "id": job_id,
                "name": f"Queued Album {job_id}",
                "album_id": album_id,
                "artist_id": 44,
                "refresh_command_id": 55,
                "search_command_id": None,
            }
            for job_id, album_id in ((1, 33), (2, 34))
        ]

        lidarr_search_worker.process_jobs(jobs)

        command.assert_called_once_with(55)
        self.assertEqual(monitor_albums.call_count, 2)
        monitor_albums.assert_any_call([33])
        monitor_albums.assert_any_call([34])
        self.assertEqual(start_command.call_count, 2)
        start_command.assert_any_call({"name": "AlbumSearch", "albumIds": [33]})
        start_command.assert_any_call({"name": "AlbumSearch", "albumIds": [34]})
        self.assertEqual(set_search.call_args_list[0].args, (1, 66))
        self.assertEqual(set_search.call_args_list[1].args, (2, 67))

    @patch("backend.workers.lidarr_searches.schedule_lidarr_search_poll")
    @patch("backend.workers.lidarr_searches.lidarr.start_command")
    @patch("backend.workers.lidarr_searches.lidarr.command")
    def test_running_refresh_is_polled_without_starting_album_search(
        self, command, start_command, schedule_poll
    ):
        command.return_value = Response(200, {"status": "started"})
        job = {
            "id": 1,
            "name": "Slow Refresh Album",
            "album_id": 33,
            "artist_id": 44,
            "refresh_command_id": 55,
            "search_command_id": None,
        }

        lidarr_search_worker.process_job(job)

        command.assert_called_once_with(55)
        start_command.assert_not_called()
        schedule_poll.assert_called_once_with(1)


class LidarrSearchQueueTests(DatabaseTestCase):
    def test_enqueue_persists_follow_up_and_request_history_together(self):
        self.register()
        with db() as connection:
            user_id = connection.execute(
                "SELECT id FROM users WHERE username = 'test-user'"
            ).fetchone()["id"]

        inserted = enqueue_lidarr_search(
            user_id,
            "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            33,
            44,
            "Queued Album",
            artist_name="Queue Artist",
            release_type="Album",
            release_date="2026-07-23",
        )

        self.assertTrue(inserted)
        with db() as connection:
            job = connection.execute(
                "SELECT * FROM pending_lidarr_searches WHERE album_id = 33"
            ).fetchone()
            history = connection.execute(
                "SELECT * FROM request_history WHERE mbid = ?",
                ("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",),
            ).fetchone()
        self.assertEqual(job["artist_id"], 44)
        self.assertEqual(job["refresh_type"], "album")
        self.assertEqual(history["name"], "Queued Album")
        self.assertEqual(history["artist_name"], "Queue Artist")
        self.assertEqual(history["release_type"], "Album")
        self.assertEqual(history["release_date"], "2026-07-23")

        with db() as connection:
            second_user_id = connection.execute(
                "INSERT INTO users (username, password_hash, role, created_at) "
                "VALUES (?, ?, 'user', ?)",
                (
                    "second-listener",
                    generate_password_hash("listener-password"),
                    time.time(),
                ),
            ).lastrowid

        duplicate = enqueue_lidarr_search(
            second_user_id,
            "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            33,
            44,
            "Queued Album",
        )

        self.assertFalse(duplicate)
        with db() as connection:
            history_user_ids = [
                row["user_id"]
                for row in connection.execute(
                    "SELECT user_id FROM request_history WHERE mbid = ? "
                    "ORDER BY id",
                    ("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",),
                )
            ]
            queued_jobs = connection.execute(
                "SELECT COUNT(*) FROM pending_lidarr_searches WHERE mbid = ?",
                ("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",),
            ).fetchone()[0]
        self.assertEqual(history_user_ids, [user_id, second_user_id])
        self.assertEqual(queued_jobs, 1)

    def test_one_refresh_command_is_persisted_for_an_exact_job_batch(self):
        self.register()
        with db() as connection:
            user_id = connection.execute(
                "SELECT id FROM users WHERE username = 'test-user'"
            ).fetchone()["id"]
        for index in (1, 2):
            enqueue_lidarr_search(
                user_id,
                f"aaaaaaaa-bbbb-cccc-dddd-{index:012d}",
                30 + index,
                44,
                f"Queued Album {index}",
            )
        with db() as connection:
            job_ids = [
                row["id"]
                for row in connection.execute(
                    "SELECT id FROM pending_lidarr_searches ORDER BY id"
                )
            ]

        set_lidarr_refresh_command(job_ids, 55)

        with db() as connection:
            command_ids = {
                row["refresh_command_id"]
                for row in connection.execute(
                    "SELECT refresh_command_id FROM pending_lidarr_searches"
                )
            }
        self.assertEqual(command_ids, {55})

    def test_main_database_connections_enforce_foreign_keys(self):
        with db() as connection:
            self.assertEqual(
                connection.execute("PRAGMA foreign_keys").fetchone()[0],
                1,
            )

    def test_stale_worker_writes_do_not_resurrect_deleted_user_data(self):
        self.register()
        with db() as connection:
            user_id = connection.execute(
                "SELECT id FROM users WHERE username = 'test-user'"
            ).fetchone()["id"]
            connection.execute("DELETE FROM users WHERE id = ?", (user_id,))

        self.assertFalse(
            storage_module.save_recommendation_cache(user_id, {"private": True})
        )
        self.assertEqual(
            insert_plex_listens([
                {
                    "server_id": "server-1",
                    "history_key": "stale-history",
                    "user_id": user_id,
                    "artist_rating_key": "artist-1",
                    "album_rating_key": None,
                    "played_at": time.time(),
                }
            ]),
            0,
        )
        with db() as connection:
            for table in (
                "recommendation_cache",
                "plex_listens",
            ):
                self.assertEqual(
                    connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],
                    0,
                )

    def test_legacy_queue_migration_recovers_every_requester(self):
        with tempfile.TemporaryDirectory(prefix="melodarr-queue-migration-") as directory:
            database = os.path.join(directory, "melodarr.db")
            settings = os.path.join(directory, "settings.json")
            connection = sqlite3.connect(database)
            connection.executescript("""
                CREATE TABLE users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE request_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    kind TEXT NOT NULL,
                    mbid TEXT NOT NULL,
                    name TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE pending_lidarr_searches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    mbid TEXT NOT NULL UNIQUE,
                    album_id INTEGER NOT NULL,
                    artist_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    refresh_command_id INTEGER,
                    search_command_id INTEGER,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL NOT NULL,
                    last_error TEXT,
                    created_at REAL NOT NULL
                );
                CREATE TABLE recommendation_cache (
                    user_id INTEGER PRIMARY KEY REFERENCES users(id),
                    value TEXT NOT NULL,
                    refreshed_at REAL NOT NULL
                );
                INSERT INTO recommendation_cache
                    (user_id, value, refreshed_at)
                    VALUES (999, '{"orphaned":true}', 1);
            """)
            connection.executemany(
                "INSERT INTO users "
                "(id, username, password_hash, role, created_at) "
                "VALUES (?, ?, 'hash', 'user', 1)",
                ((1, "first-user"), (2, "second-user")),
            )
            connection.executemany(
                "INSERT INTO request_history "
                "(user_id, kind, mbid, name, created_at) "
                "VALUES (?, 'release-group', 'shared-mbid', 'Shared Album', 1)",
                ((1,), (2,)),
            )
            connection.execute(
                "INSERT INTO pending_lidarr_searches "
                "(user_id, mbid, album_id, artist_id, name, "
                "next_attempt_at, created_at) "
                "VALUES (1, 'shared-mbid', 10, 20, 'Shared Album', 1, 1)"
            )
            connection.commit()
            connection.close()

            with (
                patch.object(storage_module, "DATABASE", database),
                patch.object(storage_module, "SETTINGS_FILE", settings),
            ):
                storage_module.init_db()
                with storage_module.db() as migrated:
                    columns = {
                        row["name"]
                        for row in migrated.execute(
                            "PRAGMA table_info(pending_lidarr_searches)"
                        )
                    }
                    requester_ids = [
                        row["user_id"]
                        for row in migrated.execute(
                            "SELECT user_id FROM pending_lidarr_search_requesters "
                            "ORDER BY user_id"
                        )
                    ]
                    violations = migrated.execute(
                        "PRAGMA foreign_key_check"
                    ).fetchall()
                    orphaned_recommendations = migrated.execute(
                        "SELECT COUNT(*) FROM recommendation_cache"
                    ).fetchone()[0]

            self.assertNotIn("user_id", columns)
            self.assertEqual(requester_ids, [1, 2])
            self.assertEqual(violations, [])
            self.assertEqual(orphaned_recommendations, 0)


class DeploymentConfigTests(unittest.TestCase):
    def test_blank_path_environment_variables_fail_fast(self):
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        config_path = os.path.join(project_root, "backend", "config.py")
        for variable in (
            "MELODARR_DATABASE",
            "MELODARR_CACHE_DATABASE",
            "MELODARR_SETTINGS",
            "MELODARR_SECRET_KEY_FILE",
            "MELODARR_ARTWORK_CACHE",
        ):
            with self.subTest(variable=variable):
                with patch.dict(os.environ, {variable: " \t "}):
                    with self.assertRaisesRegex(RuntimeError, variable):
                        runpy.run_path(config_path)

    def test_backup_guidance_requires_a_consistent_sqlite_snapshot(self):
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(project_root, "README.md"), encoding="utf-8") as file:
            readme = file.read()

        self.assertIn("stop Melodarr cleanly", readme)
        self.assertIn("SQLite's online backup API", readme)
        self.assertIn("do not make a raw copy of a live database", readme)

    def test_production_frontend_is_minified_and_precompressed_without_maps(self):
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(
            os.path.join(project_root, "frontend", "scripts", "build.mjs"),
            encoding="utf-8",
        ) as file:
            build_script = file.read()
        with open(os.path.join(project_root, "Dockerfile"), encoding="utf-8") as file:
            dockerfile = file.read()

        self.assertIn("minify: true", build_script)
        self.assertIn("sourcemap: false", build_script)
        self.assertIn("brotliCompressSync", build_script)
        self.assertIn("gzipSync", build_script)
        self.assertNotIn(".js.map", dockerfile)
        self.assertIn(
            "COPY --from=frontend-build /app/frontend/static /app/frontend/static",
            dockerfile,
        )

    def test_auth_ui_uses_first_run_and_invitation_flows(self):
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(
            os.path.join(project_root, "frontend", "static", "index.html"),
            encoding="utf-8",
        ) as file:
            frontend = file.read()
        with open(
            os.path.join(project_root, "frontend", "src", "app.ts"),
            encoding="utf-8",
        ) as file:
            typescript = file.read()
        with open(
            os.path.join(project_root, "frontend", "src", "style.css"),
            encoding="utf-8",
        ) as file:
            stylesheet = file.read()
        self.assertNotIn("<summary>Create an account</summary>", frontend)
        self.assertIn('name="remember"', frontend)
        self.assertIn('data-account-route="invitations"', frontend)
        self.assertIn('data-discovery-src="/static/discovery.js"', frontend)
        self.assertNotIn('<script src="/static/discovery.js"', frontend)
        self.assertIn("function loadDiscovery()", typescript)
        self.assertIn("status.firstAccount", typescript)
        self.assertIn("status.invitationValid", typescript)
        self.assertIn('id="setup-wizard"', frontend)
        self.assertIn('id="setup-choose-plex"', frontend)
        self.assertIn('id="setup-skip-plex"', frontend)
        self.assertIn('id="setup-plex-message"', frontend)
        self.assertIn('id="plex-current-connection"', frontend)
        self.assertIn('"#setup-plex-message"', typescript)
        self.assertIn('"#plex-current-connection"', typescript)
        self.assertIn("if (popup.closed)", typescript)
        self.assertIn('"/api/auth/plex/start"', typescript)
        self.assertIn('id="account-link-plex"', typescript)
        self.assertIn('startPlexAuthentication("link"', typescript)
        self.assertIn(".linked-account-summary", stylesheet)
        self.assertIn(".service-card > .plex-current-connection", stylesheet)
        self.assertNotIn("#plex-settings-config { display: grid !important", stylesheet)
        self.assertNotIn('name="token"', frontend)

    def test_color_theme_switcher_preserves_midnight_and_warm_palettes(self):
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(
            os.path.join(project_root, "frontend", "static", "index.html"),
            encoding="utf-8",
        ) as file:
            frontend = file.read()
        with open(
            os.path.join(project_root, "frontend", "src", "theme.ts"),
            encoding="utf-8",
        ) as file:
            theme_typescript = file.read()
        with open(
            os.path.join(project_root, "frontend", "src", "style.css"),
            encoding="utf-8",
        ) as file:
            stylesheet = file.read()

        self.assertIn('<script src="/static/theme.js"></script>', frontend)
        self.assertIn('id="theme-toggle"', frontend)
        self.assertIn('"melodarr-theme"', theme_typescript)
        self.assertIn('theme = "midnight"', theme_typescript)
        self.assertIn(':root[data-theme="midnight"]', stylesheet)
        self.assertIn(':root[data-theme="warm"]', stylesheet)
        self.assertEqual(stylesheet.count("--requested:"), 2)
        self.assertIn(
            ".request-lifecycle.requested { background: var(--requested-surface); "
            "color: var(--requested); }",
            stylesheet,
        )

    def test_detail_navigation_and_mobile_back_to_top_preserve_context(self):
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(
            os.path.join(project_root, "frontend", "src", "discovery.ts"),
            encoding="utf-8",
        ) as file:
            discovery_typescript = file.read()
        with open(
            os.path.join(project_root, "frontend", "src", "style.css"),
            encoding="utf-8",
        ) as file:
            stylesheet = file.read()

        self.assertIn(
            'const currentNavigationView = id === "detail" ? detailOrigin.view : id;',
            discovery_typescript,
        )
        self.assertIn(
            "const isCurrent = button.dataset.view === currentNavigationView;",
            discovery_typescript,
        )
        self.assertIn(
            "#back-to-top { right: 16px; width: 44px; height: 44px; padding: 0; }",
            stylesheet,
        )

    def test_recommendation_source_badges_stay_inside_mobile_cards(self):
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(
            os.path.join(project_root, "frontend", "src", "style.css"),
            encoding="utf-8",
        ) as file:
            stylesheet = file.read()

        self.assertIn(
            "max-width: calc(100% - 16px)",
            stylesheet,
        )
        self.assertNotIn(
            ".recommendation-source { display: flex; position: absolute; "
            "top: 8px; left: 8px; align-items: center; max-width: 136px;",
            stylesheet,
        )

    def test_mobile_logout_remains_visible_and_accessible(self):
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(
            os.path.join(project_root, "frontend", "static", "index.html"),
            encoding="utf-8",
        ) as file:
            frontend = file.read()
        with open(
            os.path.join(project_root, "frontend", "src", "style.css"),
            encoding="utf-8",
        ) as file:
            stylesheet = file.read()

        self.assertIn('class="logout-icon"', frontend)
        self.assertIn('<span class="logout-label">Sign out</span>', frontend)
        self.assertIn(
            ".logout { display: grid; flex: 0 0 44px; width: 44px; "
            "height: 44px; place-items: center; padding: 0; }",
            stylesheet,
        )
        self.assertNotIn(".logout { display: none; }", stylesheet)

    def test_logout_resets_stale_authentication_messages(self):
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(
            os.path.join(project_root, "frontend", "src", "app.ts"),
            encoding="utf-8",
        ) as file:
            typescript = file.read()

        self.assertIn(
            'setMessage(requiredDescendant(loginForm, ".form-message"), "");',
            typescript,
        )
        self.assertIn(
            'setMessage(requiredDescendant(plexLoginOption, ".form-message"), "");',
            typescript,
        )
        self.assertIn('$<HTMLButtonElement>("#plex-login").disabled = false;', typescript)
        self.assertIn('element.classList.add("message");', typescript)
        self.assertIn('element.classList.toggle("error", isError);', typescript)
        self.assertNotIn("element.className = `message", typescript)

    def test_invitation_copy_supports_http_lan_hosts(self):
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(
            os.path.join(project_root, "frontend", "src", "app.ts"),
            encoding="utf-8",
        ) as file:
            typescript = file.read()

        self.assertIn("window.isSecureContext", typescript)
        self.assertIn('document.execCommand("copy")', typescript)
        self.assertIn("copied ? \"Invitation link copied.\"", typescript)

    def test_settings_use_plex_login_and_invalidate_lidarr_links(self):
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(
            os.path.join(project_root, "frontend", "src", "app.ts"),
            encoding="utf-8",
        ) as file:
            app_typescript = file.read()
        with open(
            os.path.join(project_root, "frontend", "src", "discovery.ts"),
            encoding="utf-8",
        ) as file:
            discovery_typescript = file.read()

        self.assertIn('startPlexAuthentication("server"', app_typescript)
        self.assertNotIn('"/api/settings/plex/test"', app_typescript)
        self.assertIn('form.apiKey.value = "";', app_typescript)
        self.assertIn(
            'new Event("melodarr-lidarr-settings-changed")',
            app_typescript,
        )
        self.assertIn(
            'window.addEventListener("melodarr-lidarr-settings-changed"',
            discovery_typescript,
        )

    def test_melodarr_settings_precede_plex_and_manage_the_automation_key(self):
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(
            os.path.join(project_root, "frontend", "static", "index.html"),
            encoding="utf-8",
        ) as file:
            frontend = file.read()
        with open(
            os.path.join(project_root, "frontend", "src", "app.ts"),
            encoding="utf-8",
        ) as file:
            typescript = file.read()

        self.assertLess(
            frontend.index('id="melodarr-settings"'),
            frontend.index('id="plex-settings"'),
        )
        self.assertIn('id="melodarr-version"', frontend)
        self.assertIn('id="copy-melodarr-api-key"', frontend)
        self.assertIn('id="toggle-melodarr-api-key"', frontend)
        self.assertIn('id="regenerate-melodarr-api-key"', frontend)
        self.assertIn('name="applicationTitle"', frontend)
        self.assertIn('name="applicationUrl"', frontend)
        self.assertIn('name="apiKey" type="password"', frontend)
        self.assertNotIn(
            'id="notification-global-settings" class="notification-settings-form '
            'notification-card-form">\n            <fieldset class="notification-card '
            'notification-global-card">\n              <legend>Global delivery</legend>\n'
            '              <label>Application URL',
            frontend,
        )
        self.assertIn('"/api/settings/melodarr"', typescript)
        self.assertIn(
            '"/api/settings/melodarr/api-key/regenerate"',
            typescript,
        )
        self.assertIn("copyInputValue(form.apiKey)", typescript)
        self.assertIn('form.apiKey.type === "password"', typescript)
        self.assertIn(
            "https://github.com/slimjimserver/melodarr/releases/tag/",
            typescript,
        )
        self.assertIn('versionBadge.removeAttribute("href")', typescript)

    def test_musicbrainz_service_settings_support_self_hosting_and_testing(self):
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(
            os.path.join(project_root, "frontend", "static", "index.html"),
            encoding="utf-8",
        ) as file:
            frontend = file.read()
        with open(
            os.path.join(project_root, "frontend", "src", "app.ts"),
            encoding="utf-8",
        ) as file:
            app_typescript = file.read()

        self.assertIn('id="musicbrainz-settings"', frontend)
        self.assertIn('name="baseUrl"', frontend)
        self.assertIn('name="userAgent"', frontend)
        self.assertIn('name="requestIntervalMs"', frontend)
        self.assertNotIn("Cache lifetime (days)", frontend)
        self.assertIn('"/api/settings/musicbrainz"', app_typescript)
        self.assertIn('"/api/settings/musicbrainz/test"', app_typescript)
        self.assertIn("setupMusicBrainzSettings();", app_typescript)

    def test_lastfm_key_is_admin_managed_and_user_forms_only_collect_usernames(self):
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(
            os.path.join(project_root, "frontend", "static", "index.html"),
            encoding="utf-8",
        ) as file:
            frontend = file.read()
        with open(
            os.path.join(project_root, "frontend", "src", "app.ts"),
            encoding="utf-8",
        ) as file:
            app_typescript = file.read()

        self.assertIn('id="lastfm-settings"', frontend)
        self.assertIn('id="lastfm-state"', frontend)
        self.assertIn('id="save-lastfm-key"', frontend)
        self.assertIn('id="clear-lastfm-key"', frontend)
        self.assertIn('name="apiKey"', frontend)
        self.assertIn('name="listenbrainzUsername"', app_typescript)
        self.assertIn('name="lastfmUsername"', app_typescript)
        self.assertIn('"/api/settings/lastfm"', app_typescript)
        self.assertIn('"/api/account/lastfm"', app_typescript)
        self.assertNotIn('name="lastfmApiKey"', frontend)
        self.assertNotIn('name="lastfmApiKey"', app_typescript)
        self.assertNotIn("lastfmApiKey", app_typescript)

    def test_discovery_search_offers_track_to_release_group_results(self):
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(
            os.path.join(project_root, "frontend", "static", "index.html"),
            encoding="utf-8",
        ) as file:
            frontend = file.read()
        with open(
            os.path.join(project_root, "frontend", "src", "discovery.ts"),
            encoding="utf-8",
        ) as file:
            discovery_typescript = file.read()

        self.assertIn('<option value="track">Tracks</option>', frontend)
        self.assertIn('track: { placeholder: "Search tracks…"', discovery_typescript)
        self.assertIn('"release group"', discovery_typescript)
        self.assertIn("for matching tracks", discovery_typescript)
        self.assertIn("Matched track:", discovery_typescript)
        self.assertIn(
            'showDetail("release-group", result.id)',
            discovery_typescript,
        )
        self.assertIn("const batchSize =", discovery_typescript)
        self.assertIn("deferredTasteRows", discovery_typescript)

    def test_brand_navigation_stays_inside_the_loaded_application(self):
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(
            os.path.join(project_root, "frontend", "static", "index.html"),
            encoding="utf-8",
        ) as file:
            frontend = file.read()
        with open(
            os.path.join(project_root, "frontend", "src", "app.ts"),
            encoding="utf-8",
        ) as file:
            app_typescript = file.read()
        with open(
            os.path.join(project_root, "frontend", "src", "discovery.ts"),
            encoding="utf-8",
        ) as file:
            discovery_typescript = file.read()

        self.assertIn('<link rel="icon" href="/icons/melodarr.svg"', frontend)
        self.assertIn('<link rel="apple-touch-icon" href="/icons/melodarr-180.png">', frontend)
        self.assertIn('<link rel="manifest" href="/static/site.webmanifest">', frontend)
        self.assertIn('<a class="brand" href="/" aria-label="Melodarr home">', frontend)
        self.assertIn(
            '<img src="/icons/melodarr.svg" alt="" width="32" height="32">',
            frontend,
        )
        self.assertIn('$(".brand").addEventListener("click"', app_typescript)
        self.assertIn('showView("discover")', app_typescript)
        self.assertIn('new Event("melodarr-home")', app_typescript)
        self.assertIn(
            'window.addEventListener("melodarr-home"', discovery_typescript
        )
        self.assertIn('$("#search-form").reset()', discovery_typescript)
        self.assertIn('$("#results").replaceChildren()', discovery_typescript)
        self.assertIn("searchRequestVersion += 1", discovery_typescript)
        self.assertIn("const maxArtworkRequests = 6", discovery_typescript)
        self.assertIn('kind === "artist" ? 120_000', discovery_typescript)
        self.assertIn("loadArtworkWhenNear", discovery_typescript)
        self.assertIn('"/icons/listenbrainz.svg"', discovery_typescript)
        self.assertIn('"/icons/last-fm.svg"', discovery_typescript)
        self.assertIn('"/icons/plex.svg"', discovery_typescript)
        self.assertIn('services.className = "card-service-icons"', discovery_typescript)
        self.assertNotIn(
            '"This artist is in your selected Plex libraries."',
            discovery_typescript,
        )

    def test_library_auto_loads_once_and_renders_large_collections_in_batches(self):
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(
            os.path.join(project_root, "frontend", "src", "app.ts"),
            encoding="utf-8",
        ) as file:
            app_typescript = file.read()
        with open(
            os.path.join(project_root, "frontend", "static", "index.html"),
            encoding="utf-8",
        ) as file:
            frontend = file.read()

        self.assertIn('new Event("melodarr-library-visible")', app_typescript)
        self.assertIn(
            'window.addEventListener("melodarr-library-visible"',
            app_typescript,
        )
        self.assertIn('loadState !== "idle"', app_typescript)
        self.assertIn("const renderBatchSize = 24", app_typescript)
        self.assertIn(
            'sentinel.className = "library-render-sentinel"',
            app_typescript,
        )
        self.assertIn(
            "paginationObserver?.observe(renderSentinel)",
            app_typescript,
        )
        self.assertNotIn(
            "window.requestAnimationFrame(() => renderArtists(version, end))",
            app_typescript,
        )
        self.assertIn("const maxArtworkRequests = 6", app_typescript)
        self.assertIn("new IntersectionObserver", app_typescript)
        self.assertIn('includes("?") ? "&" : "?"', app_typescript)
        self.assertIn("[artist.name, artist.sortName]", app_typescript)
        self.assertIn(">Reload</button>", frontend)

    def test_artist_detail_returns_to_its_originating_view(self):
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(
            os.path.join(project_root, "frontend", "src", "discovery.ts"),
            encoding="utf-8",
        ) as file:
            discovery_typescript = file.read()

        self.assertIn('view: "discover" | "library"', discovery_typescript)
        self.assertIn('activeView === "library"', discovery_typescript)
        self.assertIn('"← Back to library"', discovery_typescript)
        self.assertIn('origin.view === "library" ? "/library" : "/"', discovery_typescript)
        self.assertIn("detailNavigationState(kind, id)", discovery_typescript)
        self.assertIn("detailHistory: [...detailHistory]", discovery_typescript)
        self.assertIn("top: origin.scrollY", discovery_typescript)

    def test_artist_detail_uses_the_lazy_discography_renderer(self):
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(
            os.path.join(project_root, "frontend", "src", "discovery.ts"),
            encoding="utf-8",
        ) as file:
            discovery_typescript = file.read()

        self.assertIn(
            "results.append(renderDiscography(data));",
            discovery_typescript,
        )
        self.assertEqual(
            discovery_typescript.count('layout.className = "discography-layout"'),
            1,
        )
        self.assertEqual(
            discovery_typescript.count('filter.className = "discography-filter"'),
            1,
        )
        self.assertIn('filterInput.addEventListener("input"', discovery_typescript)
        self.assertIn("Search releases or tracks", discovery_typescript)
        self.assertIn("/tracks?q=", discovery_typescript)
        self.assertIn(
            "[group.date, ...(group.animeNames || []), ...(group.secondaryTypes || []), group.disambiguation]",
            discovery_typescript,
        )

    def test_discovery_search_uses_only_the_search_response_for_plex_matches(self):
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(
            os.path.join(project_root, "frontend", "src", "discovery.ts"),
            encoding="utf-8",
        ) as file:
            discovery_typescript = file.read()

        self.assertNotIn('getJson("/api/library")', discovery_typescript)
        self.assertNotIn("getPlexArtists", discovery_typescript)
        self.assertNotIn("normalizedArtistName", discovery_typescript)
        self.assertIn(
            "createPlexArtistCard(result, description, result.plex)",
            discovery_typescript,
        )

    def test_account_menu_has_a_profile_link_fallback(self):
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(
            os.path.join(project_root, "frontend", "static", "index.html"),
            encoding="utf-8",
        ) as file:
            frontend = file.read()
        with open(
            os.path.join(project_root, "frontend", "src", "app.ts"),
            encoding="utf-8",
        ) as file:
            typescript = file.read()

        self.assertIn('<a id="account-menu"', frontend)
        self.assertIn("accountMenu.href = `/${encodeURIComponent(user.username)}`", typescript)
        self.assertIn(
            'showAccountPage?.("profile", true, currentUser.username)',
            typescript,
        )
        self.assertIn(
            '<a data-account-route="requests" href="#">Requests</a>',
            frontend,
        )
        self.assertIn('return `/${encodedUsername}/requests${query}`', typescript)
        self.assertIn('className = "request-pagination"', typescript)
        self.assertIn(
            '/api/account/profile?username=${encodeURIComponent(targetUsername)}'
            '&page=${encodeURIComponent(targetRequestPage)}',
            typescript,
        )
        # The header and the mobile tab bar both carry a button per view, and
        # detail/account views have none, so this must not use the strict
        # single-element helper that throws when a selector matches nothing.
        self.assertIn(
            'document.querySelectorAll<HTMLElement>("[data-view]")',
            typescript,
        )
        self.assertNotIn('$(`[data-view=${view}]`)', typescript)

    def test_admin_user_edit_link_navigates_to_the_profile(self):
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(
            os.path.join(project_root, "frontend", "src", "app.ts"),
            encoding="utf-8",
        ) as file:
            typescript = file.read()

        self.assertIn('const edit = document.createElement("a")', typescript)
        self.assertIn(
            "edit.href = `/${encodeURIComponent(routeUsername)}`",
            typescript,
        )
        self.assertIn(
            'showAccountPage?.("profile", true, routeUsername)',
            typescript,
        )
        self.assertIn(
            "const routeUsername = adminUserRouteUsername(user);",
            typescript,
        )
        self.assertIn(
            "requesterName.href = `/${encodeURIComponent(adminUserRouteUsername(item.requester))}`",
            typescript,
        )
        self.assertIn(
            "requestLink.href = `/${encodeURIComponent(adminUserRouteUsername(user))}/requests`",
            typescript,
        )
        self.assertNotIn(
            "const routeUsername = adminUserDisplayName(user);",
            typescript,
        )
        self.assertNotIn(
            'edit.addEventListener("click", () => openAdminUserDialog(user))',
            typescript,
        )

    def test_admin_account_navigation_targets_other_users_settings(self):
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(
            os.path.join(project_root, "frontend", "src", "app.ts"),
            encoding="utf-8",
        ) as file:
            typescript = file.read()

        self.assertIn(
            'if (isOwnAccount || currentUser.role === "admin")',
            typescript,
        )
        self.assertIn(
            'if (currentUser.role === "admin") allowedPages.push("invitations")',
            typescript,
        )
        self.assertIn(
            'api(accountApiPath("/api/account/general")',
            typescript,
        )
        self.assertIn(
            'api(accountApiPath("/api/account/settings")',
            typescript,
        )
        self.assertIn("}, targetUsername);", typescript)

    def test_frontend_invalidates_stale_account_and_expired_session_requests(self):
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(
            os.path.join(project_root, "frontend", "src", "app.ts"),
            encoding="utf-8",
        ) as file:
            app_typescript = file.read()
        with open(
            os.path.join(project_root, "frontend", "src", "discovery.ts"),
            encoding="utf-8",
        ) as file:
            discovery_typescript = file.read()

        self.assertIn("const renderGeneration = ++accountRenderGeneration", app_typescript)
        self.assertIn("accountRenderAbort?.abort()", app_typescript)
        self.assertIn("if (!isCurrentRender()) return", app_typescript)
        self.assertIn(
            "`${path}?username=${encodeURIComponent(targetUsername)}`",
            app_typescript,
        )
        self.assertNotIn(
            "encodeURIComponent(activeAccountUsername || targetUsername)",
            app_typescript,
        )
        self.assertIn("handleAuthenticationFailure(response);", app_typescript)
        self.assertIn("handleAuthenticationFailure(response);", discovery_typescript)
        self.assertIn('response.status !== 401', app_typescript)
        self.assertIn('new Event("melodarr-signed-out")', app_typescript)

    def test_non_admin_settings_deep_links_are_normalized_without_polling(self):
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(
            os.path.join(project_root, "frontend", "src", "app.ts"),
            encoding="utf-8",
        ) as file:
            typescript = file.read()

        self.assertIn('if (currentUser?.role !== "admin") {', typescript)
        self.assertIn(
            'window.history.replaceState({ view: "discover" }, "", "/")',
            typescript,
        )
        self.assertIn('if (currentUser?.role !== "admin") return;', typescript)

    def test_library_navigation_is_available_to_every_authenticated_user(self):
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(
            os.path.join(project_root, "frontend", "static", "index.html"),
            encoding="utf-8",
        ) as file:
            frontend = file.read()

        self.assertIn(
            '<a class="nav-link" href="/library" '
            'data-view="library">Your library</a>',
            frontend,
        )
        self.assertIn(
            '<a class="nav-link" href="/library" data-view="library">'
            '<span class="tab-icon" aria-hidden="true"><svg',
            frontend,
        )
        self.assertIn('</svg></span>Library</a>', frontend)
        self.assertNotIn(
            'class="nav-link admin-only" href="/library"',
            frontend,
        )

    def test_gunicorn_runs_one_process_with_threaded_concurrency(self):
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        config_path = os.path.join(project_root, "backend", "gunicorn.conf.py")
        config = runpy.run_path(config_path)
        self.assertEqual(config["workers"], 1)
        self.assertEqual(config["worker_class"], "gthread")
        self.assertEqual(config["threads"], 16)
        self.assertEqual(config["timeout"], 60)
        self.assertFalse(config["preload_app"])
        self.assertTrue(config["control_socket_disable"])

        with open(os.path.join(project_root, "Dockerfile"), encoding="utf-8") as file:
            dockerfile = file.read()
        self.assertIn('"gunicorn"', dockerfile)
        self.assertIn('"--chdir=/app"', dockerfile)
        self.assertIn('"--config=/app/backend/gunicorn.conf.py"', dockerfile)
        self.assertIn("USER melodarr:melodarr", dockerfile)
        self.assertNotIn("ENTRYPOINT", dockerfile)
        self.assertNotIn("gosu", dockerfile)
        self.assertNotIn("PUID", dockerfile)
        self.assertNotIn("PGID", dockerfile)

        with open(
            os.path.join(project_root, "docker-compose.yml"),
            encoding="utf-8",
        ) as file:
            compose = file.read()
        self.assertIn('user: "1000:1000"', compose)
        self.assertNotIn("PUID", compose)
        self.assertNotIn("PGID", compose)

        with open(
            os.path.join(project_root, "frontend", "static", "index.html"),
            encoding="utf-8",
        ) as file:
            frontend = file.read()
        self.assertNotIn("fonts.googleapis.com", frontend)

    @patch("backend.worker.start_background_thread")
    def test_gunicorn_hook_starts_recommendations_once(self, start_thread):
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        config = runpy.run_path(os.path.join(
            project_root,
            "backend",
            "gunicorn.conf.py",
        ))
        gunicorn_worker = Mock()
        config["post_worker_init"](gunicorn_worker)
        start_thread.assert_called_once_with()
        gunicorn_worker.log.info.assert_called_once_with("Background workers started")

class AuthenticationTests(DatabaseTestCase):
    def login_non_admin(self, username="local-listener"):
        admin_csrf = self.register()
        with db() as connection:
            connection.execute(
                "INSERT INTO users (username, password_hash, role, created_at) "
                "VALUES (?, ?, 'user', ?)",
                (
                    username,
                    generate_password_hash("listener-password"),
                    time.time(),
                ),
            )
        self.client.post(
            "/api/auth/logout", headers={"X-CSRF-Token": admin_csrf}
        )
        response = self.client.post(
            "/api/auth/login",
            json={"username": username, "password": "listener-password"},
        )
        self.assertEqual(response.status_code, 200)
        return response.get_json()["csrfToken"]

    def test_empty_install_redirects_to_owner_setup(self):
        response = self.client.get("/", follow_redirects=False)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/setup")
        status = self.client.get("/api/auth/status").get_json()
        self.assertTrue(status["firstAccount"])

    def test_first_registration_creates_admin_session(self):
        response = self.client.post(
            "/api/auth/register",
            json={"username": "test-user", "password": "a-secure-password"},
        )
        payload = response.get_json()
        self.assertEqual(response.status_code, 201)
        self.assertEqual(payload["role"], "admin")
        self.assertTrue(payload["csrfToken"])

    def test_json_endpoints_reject_non_object_bodies(self):
        invalid_body = ["not", "a", "JSON object"]
        for path in ("/api/auth/register", "/api/auth/login"):
            with self.subTest(path=path):
                response = self.client.post(path, json=invalid_body)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(
                    response.get_json(),
                    {"error": "Request body must be a JSON object."},
                )

        csrf = self.register()
        paths = (
            "/api/account/general",
            "/api/account/settings",
            "/api/account/lastfm",
            "/api/request",
            "/api/request/release-group",
            "/api/settings/lidarr",
            "/api/settings/lidarr/test",
            "/api/settings/musicbrainz",
            "/api/settings/musicbrainz/test",
            "/api/auth/plex/start",
            "/api/auth/plex/poll",
            "/api/auth/plex/inspect",
            "/api/auth/plex/complete",
        )
        for path in paths:
            with self.subTest(path=path):
                response = self.client.post(
                    path,
                    json=invalid_body,
                    headers={"X-CSRF-Token": csrf},
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(
                    response.get_json(),
                    {"error": "Request body must be a JSON object."},
                )

    @patch("backend.routes.settings.lidarr.system_status")
    def test_lidarr_settings_reject_embedded_url_credentials(self, system_status):
        csrf = self.register()
        for path in ("/api/settings/lidarr", "/api/settings/lidarr/test"):
            with self.subTest(path=path):
                response = self.client.post(
                    path,
                    json={"hostname": "http://user:password@lidarr:8686", "apiKey": "key"},
                    headers={"X-CSRF-Token": csrf},
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.get_json(), {
                    "error": "Lidarr URL must not contain a username or password.",
                })
        system_status.assert_not_called()

    @patch("backend.routes.settings.lidarr.options", return_value={
        "rootFolders": [], "qualityProfiles": [], "metadataProfiles": [], "tags": [],
    })
    @patch("backend.routes.settings.lidarr.system_status")
    def test_lidarr_settings_accept_normal_url(self, system_status, options):
        csrf = self.register()
        system_status.return_value = Response(payload={"version": "1.0"})
        response = self.client.post(
            "/api/settings/lidarr",
            json={"hostname": "http://lidarr:8686", "apiKey": "key"},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(get_service("lidarr")["url"], "http://lidarr:8686")

    def test_plex_poll_stays_pending_until_the_pin_is_authorized(self):
        with (
            patch("backend.routes.auth.plex_auth.create_pin") as create_pin,
            patch("backend.routes.auth.plex_auth.poll_pin", return_value=""),
        ):
            create_pin.return_value = {
                "id": 42,
                "authorizationUrl": "https://app.plex.tv/auth/#!?code=ABCD",
                "expiresAt": time.time() + 600,
            }
            flow_token = self.client.post(
                "/api/auth/plex/start", json={"purpose": "server"}
            ).get_json()["flowToken"]

            response = self.client.post(
                "/api/auth/plex/poll", json={"flowToken": flow_token}
            )

        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.get_json()["pending"])

    def test_non_admin_can_link_plex_and_use_both_login_methods(self):
        csrf_token = self.login_non_admin()
        original_settings = {
            "url": "http://plex:32400",
            "token": "server-owner-token",
            "machineIdentifier": "server-1",
            "libraries": [{"id": "1", "title": "Music"}],
            "librarySectionIds": ["1"],
        }
        save_service("plex", original_settings)
        account = {
            "id": "linked-101",
            "username": "plex-listener",
            "title": "Plex Listener",
            "email": "listener@example.com",
            "thumb": "https://plex.tv/listener.png",
        }
        resources = [{"clientIdentifier": "server-1", "owned": False}]
        with db() as connection:
            before = connection.execute(
                "SELECT id, password_hash FROM users WHERE username = 'local-listener'"
            ).fetchone()

        with (
            patch("backend.routes.auth.plex_auth.create_pin") as create_pin,
            patch(
                "backend.routes.auth.plex_auth.poll_pin",
                return_value="listener-token",
            ),
            patch(
                "backend.routes.auth.plex_auth.get_account", return_value=account
            ),
            patch(
                "backend.routes.auth.plex_auth.get_resources",
                return_value=resources,
            ),
        ):
            create_pin.return_value = {
                "id": 501,
                "authorizationUrl": "https://app.plex.tv/auth/#!?code=LINK",
                "expiresAt": time.time() + 600,
            }
            started = self.client.post(
                "/api/auth/plex/start",
                json={"purpose": "link"},
                headers={"X-CSRF-Token": csrf_token},
            )
            self.assertEqual(started.status_code, 201)
            linked = self.client.post(
                "/api/auth/plex/poll",
                json={"flowToken": started.get_json()["flowToken"]},
                headers={"X-CSRF-Token": csrf_token},
            )

        self.assertEqual(linked.status_code, 200)
        self.assertTrue(linked.get_json()["plexLinked"])
        self.assertEqual(linked.get_json()["plexUsername"], "plex-listener")
        with db() as connection:
            after = connection.execute(
                "SELECT id, username, password_hash, plex_id FROM users "
                "WHERE username = 'local-listener'"
            ).fetchone()
        self.assertEqual(after["id"], before["id"])
        self.assertEqual(after["password_hash"], before["password_hash"])
        self.assertEqual(after["plex_id"], "linked-101")
        self.assertEqual(get_service("plex"), original_settings)

        self.client.post(
            "/api/auth/logout", headers={"X-CSRF-Token": csrf_token}
        )
        local_login = self.client.post(
            "/api/auth/login",
            json={
                "username": "local-listener",
                "password": "listener-password",
            },
        )
        self.assertEqual(local_login.status_code, 200)
        self.assertEqual(local_login.get_json()["username"], "local-listener")
        self.client.post(
            "/api/auth/logout",
            headers={"X-CSRF-Token": local_login.get_json()["csrfToken"]},
        )

        with (
            patch("backend.routes.auth.plex_auth.create_pin") as create_pin,
            patch(
                "backend.routes.auth.plex_auth.poll_pin",
                return_value="listener-token",
            ),
            patch(
                "backend.routes.auth.plex_auth.get_account", return_value=account
            ),
            patch(
                "backend.routes.auth.plex_auth.get_resources",
                return_value=resources,
            ),
        ):
            create_pin.return_value = {
                "id": 502,
                "authorizationUrl": "https://app.plex.tv/auth/#!?code=LOGIN",
                "expiresAt": time.time() + 600,
            }
            login_flow = self.client.post(
                "/api/auth/plex/start", json={"purpose": "login"}
            ).get_json()["flowToken"]
            plex_login = self.client.post(
                "/api/auth/plex/poll", json={"flowToken": login_flow}
            )

        self.assertEqual(plex_login.status_code, 200)
        self.assertEqual(plex_login.get_json()["username"], "local-listener")

    def test_linking_plex_requires_access_to_the_configured_server(self):
        csrf_token = self.login_non_admin()
        original_settings = {
            "url": "http://plex:32400",
            "token": "server-owner-token",
            "machineIdentifier": "server-1",
            "libraries": [{"id": "1", "title": "Music"}],
            "librarySectionIds": ["1"],
        }
        save_service("plex", original_settings)
        with (
            patch("backend.routes.auth.plex_auth.create_pin") as create_pin,
            patch(
                "backend.routes.auth.plex_auth.poll_pin",
                return_value="other-token",
            ),
            patch(
                "backend.routes.auth.plex_auth.get_account",
                return_value={
                    "id": "no-access",
                    "username": "outside-user",
                    "email": "",
                    "thumb": "",
                },
            ),
            patch(
                "backend.routes.auth.plex_auth.get_resources",
                return_value=[{"clientIdentifier": "another-server"}],
            ),
        ):
            create_pin.return_value = {
                "id": 503,
                "authorizationUrl": "https://app.plex.tv/auth/#!?code=DENY",
                "expiresAt": time.time() + 600,
            }
            started = self.client.post(
                "/api/auth/plex/start",
                json={"purpose": "link"},
                headers={"X-CSRF-Token": csrf_token},
            )
            response = self.client.post(
                "/api/auth/plex/poll",
                json={"flowToken": started.get_json()["flowToken"]},
                headers={"X-CSRF-Token": csrf_token},
            )

        self.assertEqual(response.status_code, 403)
        self.assertIn("does not have access", response.get_json()["error"])
        with db() as connection:
            saved = connection.execute(
                "SELECT plex_id FROM users WHERE username = 'local-listener'"
            ).fetchone()
        self.assertIsNone(saved["plex_id"])
        self.assertEqual(get_service("plex"), original_settings)

    def test_linking_rejects_a_plex_identity_owned_by_another_user(self):
        csrf_token = self.login_non_admin()
        save_service("plex", {
            "url": "http://plex:32400",
            "token": "server-owner-token",
            "machineIdentifier": "server-1",
        })
        with db() as connection:
            connection.execute(
                "INSERT INTO users "
                "(username, password_hash, role, plex_id, created_at) "
                "VALUES (?, ?, 'user', ?, ?)",
                (
                    "already-linked",
                    generate_password_hash("another-password"),
                    "duplicate-plex-id",
                    time.time(),
                ),
            )
        with (
            patch("backend.routes.auth.plex_auth.create_pin") as create_pin,
            patch(
                "backend.routes.auth.plex_auth.poll_pin",
                return_value="duplicate-token",
            ),
            patch(
                "backend.routes.auth.plex_auth.get_account",
                return_value={
                    "id": "duplicate-plex-id",
                    "username": "duplicate-plex",
                    "email": "",
                    "thumb": "",
                },
            ),
            patch(
                "backend.routes.auth.plex_auth.get_resources",
                return_value=[{"clientIdentifier": "server-1"}],
            ),
        ):
            create_pin.return_value = {
                "id": 504,
                "authorizationUrl": "https://app.plex.tv/auth/#!?code=DUPL",
                "expiresAt": time.time() + 600,
            }
            started = self.client.post(
                "/api/auth/plex/start",
                json={"purpose": "link"},
                headers={"X-CSRF-Token": csrf_token},
            )
            response = self.client.post(
                "/api/auth/plex/poll",
                json={"flowToken": started.get_json()["flowToken"]},
                headers={"X-CSRF-Token": csrf_token},
            )

        self.assertEqual(response.status_code, 409)
        self.assertIn("another Melodarr user", response.get_json()["error"])
        with db() as connection:
            saved = connection.execute(
                "SELECT plex_id FROM users WHERE username = 'local-listener'"
            ).fetchone()
        self.assertIsNone(saved["plex_id"])

    def test_plex_link_flow_requires_csrf_and_stays_bound_to_its_user(self):
        self.assertEqual(
            self.client.post(
                "/api/auth/plex/start", json={"purpose": "link"}
            ).status_code,
            401,
        )
        owner_csrf = self.register()
        save_service("plex", {
            "url": "http://plex:32400",
            "token": "server-owner-token",
            "machineIdentifier": "server-1",
        })
        self.assertEqual(
            self.client.post(
                "/api/auth/plex/start", json={"purpose": "link"}
            ).status_code,
            403,
        )
        with patch("backend.routes.auth.plex_auth.create_pin") as create_pin:
            create_pin.return_value = {
                "id": 505,
                "authorizationUrl": "https://app.plex.tv/auth/#!?code=BOUND",
                "expiresAt": time.time() + 600,
            }
            started = self.client.post(
                "/api/auth/plex/start",
                json={"purpose": "link"},
                headers={"X-CSRF-Token": owner_csrf},
            )
        flow_token = started.get_json()["flowToken"]

        with db() as connection:
            connection.execute(
                "INSERT INTO users (username, password_hash, role, created_at) "
                "VALUES (?, ?, 'user', ?)",
                (
                    "other-local-user",
                    generate_password_hash("other-local-password"),
                    time.time(),
                ),
            )
        other_client = self.app.test_client()
        other_login = other_client.post(
            "/api/auth/login",
            json={
                "username": "other-local-user",
                "password": "other-local-password",
            },
        )
        other_csrf = other_login.get_json()["csrfToken"]
        wrong_user = other_client.post(
            "/api/auth/plex/poll",
            json={"flowToken": flow_token},
            headers={"X-CSRF-Token": other_csrf},
        )
        self.assertEqual(wrong_user.status_code, 403)
        self.assertIn("another Melodarr user", wrong_user.get_json()["error"])

        with patch("backend.routes.auth.plex_auth.poll_pin", return_value=""):
            owner_poll = self.client.post(
                "/api/auth/plex/poll",
                json={"flowToken": flow_token},
                headers={"X-CSRF-Token": owner_csrf},
            )
        self.assertEqual(owner_poll.status_code, 202)

    def test_plex_link_flow_cannot_change_server_configuration(self):
        csrf_token = self.login_non_admin()
        settings = {
            "url": "http://plex:32400",
            "token": "server-owner-token",
            "machineIdentifier": "server-1",
            "libraries": [{"id": "1", "title": "Music"}],
            "librarySectionIds": ["1"],
        }
        save_service("plex", settings)
        with patch("backend.routes.auth.plex_auth.create_pin") as create_pin:
            create_pin.return_value = {
                "id": 506,
                "authorizationUrl": "https://app.plex.tv/auth/#!?code=LIMIT",
                "expiresAt": time.time() + 600,
            }
            started = self.client.post(
                "/api/auth/plex/start",
                json={"purpose": "link"},
                headers={"X-CSRF-Token": csrf_token},
            )
        flow_token = started.get_json()["flowToken"]

        inspected = self.client.post(
            "/api/auth/plex/inspect",
            json={
                "flowToken": flow_token,
                "serverId": "other",
                "connectionUri": "http://other:32400",
            },
            headers={"X-CSRF-Token": csrf_token},
        )
        completed = self.client.post(
            "/api/auth/plex/complete",
            json={"flowToken": flow_token, "librarySectionIds": ["999"]},
            headers={"X-CSRF-Token": csrf_token},
        )

        self.assertEqual(inspected.status_code, 400)
        self.assertEqual(completed.status_code, 400)
        self.assertEqual(get_service("plex"), settings)

    def test_starting_plex_auth_purges_expired_flows(self):
        with db() as connection:
            connection.execute(
                "INSERT INTO plex_auth_flows "
                "(flow_hash, pin_id, client_identifier, purpose, created_at, "
                "expires_at) VALUES (?, ?, ?, ?, ?, ?)",
                ("expired-flow", 1, "old-client", "server", time.time() - 60, time.time() - 1),
            )

        with patch("backend.routes.auth.plex_auth.create_pin") as create_pin:
            create_pin.return_value = {
                "id": 42,
                "authorizationUrl": "https://app.plex.tv/auth/#!?code=ABCD",
                "expiresAt": time.time() + 600,
            }
            response = self.client.post(
                "/api/auth/plex/start", json={"purpose": "server"}
            )

        self.assertEqual(response.status_code, 201)
        with db() as connection:
            rows = connection.execute(
                "SELECT flow_hash FROM plex_auth_flows"
            ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertNotEqual(rows[0]["flow_hash"], "expired-flow")

    def test_plex_owner_setup_discovers_server_and_creates_admin(self):
        owned_server = {
            "name": "Music Plex",
            "product": "Plex Media Server",
            "clientIdentifier": "server-1",
            "provides": ["server"],
            "owned": True,
            "accessToken": "server-token",
            "connections": [{
                "uri": "https://server-1.plex.direct:32400",
                "protocol": "https",
                "address": "server-1.plex.direct",
                "port": 32400,
                "local": True,
                "secure": True,
            }],
        }
        libraries = [
            {"id": "1", "title": "Main Music"},
            {"id": "2", "title": "Concerts"},
        ]
        with (
            patch("backend.routes.auth.plex_auth.create_pin") as create_pin,
            patch("backend.routes.auth.plex_auth.poll_pin", return_value="account-token"),
            patch("backend.routes.auth.plex_auth.get_account") as get_account,
            patch(
                "backend.routes.auth.plex_auth.get_resources",
                return_value=[owned_server],
            ),
            patch(
                "backend.routes.auth.plex.machine_identifier",
                return_value="server-1",
            ),
            patch("backend.routes.auth.plex.music_sections", return_value=libraries),
            patch("backend.routes.auth.plex_worker.request_full_scan") as request_scan,
        ):
            create_pin.return_value = {
                "id": 42,
                "authorizationUrl": "https://app.plex.tv/auth/#!?code=ABCD",
                "expiresAt": time.time() + 600,
            }
            get_account.return_value = {
                "id": "99",
                "username": "plex-owner",
                "title": "Plex Owner",
                "email": "owner@example.com",
                "thumb": "https://plex.tv/avatar.png",
            }

            started = self.client.post(
                "/api/auth/plex/start", json={"purpose": "server"}
            )
            self.assertEqual(started.status_code, 201)
            flow_token = started.get_json()["flowToken"]

            signed_in = self.client.post(
                "/api/auth/plex/poll", json={"flowToken": flow_token}
            )
            self.assertEqual(signed_in.status_code, 200)
            self.assertEqual(signed_in.get_json()["servers"][0]["id"], "server-1")
            self.assertNotIn("accessToken", signed_in.get_json()["servers"][0])

            inspected = self.client.post(
                "/api/auth/plex/inspect",
                json={
                    "flowToken": flow_token,
                    "serverId": "server-1",
                    "connectionUri": "https://server-1.plex.direct:32400",
                },
            )
            self.assertEqual(inspected.status_code, 200)
            self.assertEqual(inspected.get_json()["libraries"], libraries)

            completed = self.client.post(
                "/api/auth/plex/complete",
                json={"flowToken": flow_token, "librarySectionIds": ["1"]},
            )

        self.assertEqual(completed.status_code, 200)
        self.assertEqual(completed.get_json()["role"], "admin")
        self.assertEqual(completed.get_json()["authProvider"], "plex")
        plex_settings = get_service("plex")
        self.assertEqual(plex_settings["token"], "server-token")
        self.assertEqual(plex_settings["machineIdentifier"], "server-1")
        self.assertEqual(plex_settings["librarySectionIds"], ["1"])
        request_scan.assert_called_once_with()

    def test_server_setup_requires_an_owned_plex_server(self):
        shared_server = {
            "name": "Shared Plex",
            "product": "Plex Media Server",
            "clientIdentifier": "shared-1",
            "provides": ["server"],
            "owned": False,
            "accessToken": "shared-token",
            "connections": [{
                "uri": "https://shared.plex.direct:32400",
                "protocol": "https",
                "address": "shared.plex.direct",
                "port": 32400,
                "local": False,
                "secure": True,
            }],
        }
        with (
            patch("backend.routes.auth.plex_auth.create_pin") as create_pin,
            patch("backend.routes.auth.plex_auth.poll_pin", return_value="account-token"),
            patch("backend.routes.auth.plex_auth.get_account") as get_account,
            patch(
                "backend.routes.auth.plex_auth.get_resources",
                return_value=[shared_server],
            ),
        ):
            create_pin.return_value = {
                "id": 43,
                "authorizationUrl": "https://app.plex.tv/auth/#!?code=EFGH",
                "expiresAt": time.time() + 600,
            }
            get_account.return_value = {
                "id": "100",
                "username": "shared-user",
                "title": "Shared User",
                "email": "shared@example.com",
                "thumb": "",
            }
            flow_token = self.client.post(
                "/api/auth/plex/start", json={"purpose": "server"}
            ).get_json()["flowToken"]
            response = self.client.post(
                "/api/auth/plex/poll", json={"flowToken": flow_token}
            )

        self.assertEqual(response.status_code, 403)
        self.assertIn("owns the server", response.get_json()["error"])

    def test_plex_sso_creates_a_user_with_access_to_the_configured_server(self):
        csrf_token = self.register()
        save_service("plex", {
            "url": "http://plex:32400",
            "token": "owner-token",
            "machineIdentifier": "server-1",
            "libraries": [{"id": "1", "title": "Music"}],
            "librarySectionIds": ["1"],
        })
        self.client.post(
            "/api/auth/logout", headers={"X-CSRF-Token": csrf_token}
        )
        shared_server = {
            "name": "Music Plex",
            "product": "Plex Media Server",
            "clientIdentifier": "server-1",
            "provides": ["server"],
            "owned": False,
            "accessToken": "user-token",
            "connections": [{
                "uri": "https://server-1.plex.direct:32400",
                "protocol": "https",
                "address": "server-1.plex.direct",
                "port": 32400,
                "local": False,
                "secure": True,
            }],
        }
        with (
            patch("backend.routes.auth.plex_auth.create_pin") as create_pin,
            patch("backend.routes.auth.plex_auth.poll_pin", return_value="user-token"),
            patch("backend.routes.auth.plex_auth.get_account") as get_account,
            patch(
                "backend.routes.auth.plex_auth.get_resources",
                return_value=[shared_server],
            ),
        ):
            create_pin.return_value = {
                "id": 44,
                "authorizationUrl": "https://app.plex.tv/auth/#!?code=IJKL",
                "expiresAt": time.time() + 600,
            }
            get_account.return_value = {
                "id": "101",
                "username": "plex-friend",
                "title": "Plex Friend",
                "email": "friend@example.com",
                "thumb": "",
            }
            flow_token = self.client.post(
                "/api/auth/plex/start", json={"purpose": "login"}
            ).get_json()["flowToken"]
            response = self.client.post(
                "/api/auth/plex/poll", json={"flowToken": flow_token}
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["role"], "user")
        self.assertEqual(response.get_json()["authProvider"], "plex")
        with db() as connection:
            saved = connection.execute(
                "SELECT plex_id FROM users WHERE username = 'plex-friend'"
            ).fetchone()
        self.assertEqual(saved["plex_id"], "101")

    def test_legacy_plex_owner_must_link_the_local_admin_before_sso(self):
        csrf_token = self.register()
        save_service("plex", {
            "url": "http://plex:32400",
            "token": "owner-token",
            "machineIdentifier": "server-1",
            "libraries": [{"id": "1", "title": "Music"}],
            "librarySectionIds": ["1"],
        })
        self.client.post(
            "/api/auth/logout", headers={"X-CSRF-Token": csrf_token}
        )
        owned_server = {
            "name": "Music Plex",
            "product": "Plex Media Server",
            "clientIdentifier": "server-1",
            "provides": ["server"],
            "owned": True,
            "accessToken": "owner-token",
            "connections": [{
                "uri": "http://plex:32400",
                "protocol": "http",
                "address": "plex",
                "port": 32400,
                "local": True,
                "secure": False,
            }],
        }
        with (
            patch("backend.routes.auth.plex_auth.create_pin") as create_pin,
            patch("backend.routes.auth.plex_auth.poll_pin", return_value="owner-token"),
            patch("backend.routes.auth.plex_auth.get_account") as get_account,
            patch(
                "backend.routes.auth.plex_auth.get_resources",
                return_value=[owned_server],
            ),
        ):
            create_pin.return_value = {
                "id": 45,
                "authorizationUrl": "https://app.plex.tv/auth/#!?code=MNOP",
                "expiresAt": time.time() + 600,
            }
            get_account.return_value = {
                "id": "102",
                "username": "plex-owner",
                "title": "Plex Owner",
                "email": "owner@example.com",
                "thumb": "",
            }
            flow_token = self.client.post(
                "/api/auth/plex/start", json={"purpose": "login"}
            ).get_json()["flowToken"]
            response = self.client.post(
                "/api/auth/plex/poll", json={"flowToken": flow_token}
            )

        self.assertEqual(response.status_code, 409)
        self.assertIn("local administrator", response.get_json()["error"])
        with db() as connection:
            users = connection.execute(
                "SELECT username, plex_id FROM users"
            ).fetchall()
            remaining_flows = connection.execute(
                "SELECT COUNT(*) FROM plex_auth_flows"
            ).fetchone()[0]
        self.assertEqual([(row["username"], row["plex_id"]) for row in users], [
            ("test-user", None),
        ])
        self.assertEqual(remaining_flows, 0)

    def test_registration_requires_one_time_admin_invitation_after_setup(self):
        csrf_token = self.register()
        invitation_response = self.client.post(
            "/api/account/invitations",
            headers={"X-CSRF-Token": csrf_token},
        )
        self.assertEqual(invitation_response.status_code, 201)
        invitation_path = invitation_response.get_json()["path"]
        invitation_token = parse_qs(urlparse(invitation_path).query)["invite"][0]

        with db() as connection:
            stored = connection.execute(
                "SELECT token_hash FROM account_invitations"
            ).fetchone()["token_hash"]
        self.assertNotEqual(stored, invitation_token)

        invited_client = self.app.test_client()
        invited = invited_client.post(
            "/api/auth/register",
            json={
                "username": "invited-user",
                "password": "another-secure-password",
                "invitationToken": invitation_token,
            },
        )
        self.assertEqual(invited.status_code, 201)
        self.assertEqual(invited.get_json()["role"], "user")

        reused = self.app.test_client().post(
            "/api/auth/register",
            json={
                "username": "uninvited-user",
                "password": "another-secure-password",
                "invitationToken": invitation_token,
            },
        )
        self.assertEqual(reused.status_code, 403)

    def test_open_registration_is_rejected_after_owner_exists(self):
        self.register()
        response = self.app.test_client().post(
            "/api/auth/register",
            json={
                "username": "uninvited-user",
                "password": "another-secure-password",
            },
        )
        self.assertEqual(response.status_code, 403)

    def test_remember_me_creates_a_permanent_session(self):
        csrf_token = self.register()
        self.client.post(
            "/api/auth/logout",
            headers={"X-CSRF-Token": csrf_token},
        )
        response = self.client.post(
            "/api/auth/login",
            json={
                "username": "test-user",
                "password": "a-secure-password",
                "remember": True,
            },
        )
        self.assertEqual(response.status_code, 200)
        with self.client.session_transaction() as saved_session:
            self.assertTrue(saved_session.permanent)

    def test_blank_general_password_preserves_the_existing_password(self):
        csrf_token = self.register()
        saved = self.client.post(
            "/api/account/general",
            json={"username": "test-user", "password": ""},
            headers={"X-CSRF-Token": csrf_token},
        )
        self.assertEqual(saved.status_code, 200)

        signed_out = self.client.post(
            "/api/auth/logout",
            headers={"X-CSRF-Token": csrf_token},
        )
        self.assertEqual(signed_out.status_code, 200)

        signed_in = self.client.post(
            "/api/auth/login",
            json={"username": "test-user", "password": "a-secure-password"},
        )
        self.assertEqual(signed_in.status_code, 200)

    def test_csrf_protects_authenticated_writes(self):
        token = self.register()
        rejected = self.client.post("/api/auth/logout")
        self.assertEqual(rejected.status_code, 403)
        accepted = self.client.post(
            "/api/auth/logout",
            headers={"X-CSRF-Token": token},
        )
        self.assertEqual(accepted.status_code, 200)
        expired = self.client.post(
            "/api/auth/logout",
            headers={"X-CSRF-Token": token},
        )
        self.assertEqual(expired.status_code, 401)
        self.assertEqual(expired.get_json()["error"], "Sign in is required.")

    def test_authenticated_route_loads_the_user_once(self):
        self.register()
        original_get_user = security.get_user
        with patch("backend.security.get_user", wraps=original_get_user) as get_user:
            response = self.client.get("/api/account/settings")

        self.assertEqual(response.status_code, 200)
        get_user.assert_called_once()


class AdminUsersTests(DatabaseTestCase):
    def add_user(
        self,
        username,
        *,
        role="user",
        created_at=1_700_000_000,
        plex_id=None,
        plex_username=None,
        plex_email=None,
        plex_avatar=None,
        listenbrainz_username=None,
        lastfm_username=None,
        lastfm_api_key=None,
        password="listener-password",
    ):
        with db() as connection:
            cursor = connection.execute(
                "INSERT INTO users "
                "(username, password_hash, role, plex_id, plex_username, "
                "plex_email, plex_avatar, listenbrainz_username, "
                "lastfm_username, lastfm_api_key, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    username,
                    generate_password_hash(password),
                    role,
                    plex_id,
                    plex_username,
                    plex_email,
                    plex_avatar,
                    listenbrainz_username,
                    lastfm_username,
                    lastfm_api_key,
                    created_at,
                ),
            )
            return cursor.lastrowid

    def test_admin_list_uses_plex_display_name_and_counts_requests(self):
        self.register()
        save_service("lastfm", {"apiKey": "admin-shared-key"})
        user_id = self.add_user(
            "generated-plex-name",
            plex_id="plex-42",
            plex_username="Plex Listener",
            plex_email="plex@example.com",
            plex_avatar="https://plex.example/avatar.jpg",
            listenbrainz_username="listener",
            lastfm_username="last-listener",
            lastfm_api_key="never-return-this",
        )
        with db() as connection:
            connection.executemany(
                "INSERT INTO request_history "
                "(user_id, kind, mbid, name, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    (user_id, "artist", "artist-1", "Artist One", 10),
                    (
                        user_id,
                        "release-group",
                        "release-1",
                        "Album One",
                        20,
                    ),
                ],
            )

        response = self.client.get("/api/admin/users")

        self.assertEqual(response.status_code, 200)
        users = response.get_json()["users"]
        plex_user = next(user for user in users if user["id"] == user_id)
        self.assertEqual(plex_user["username"], "Plex Listener")
        self.assertEqual(plex_user["localUsername"], "generated-plex-name")
        self.assertEqual(plex_user["requestCount"], 2)
        self.assertEqual(plex_user["userType"], "plex")
        self.assertEqual(plex_user["role"], "user")
        self.assertEqual(plex_user["joinedAt"], 1_700_000_000)
        self.assertEqual(plex_user["plexEmail"], "plex@example.com")
        self.assertTrue(plex_user["lastfmConfigured"])
        self.assertNotIn("lastfmApiKey", plex_user)
        self.assertNotIn("passwordHash", plex_user)
        self.assertNotIn("plexId", plex_user)

    def test_canonical_username_disambiguates_a_plex_display_name_collision(self):
        self.register()
        local_user_id = self.add_user("SharedName")
        plex_user_id = self.add_user(
            "shared-name-2",
            plex_id="plex-shared-name",
            plex_username="SharedName",
        )

        users = self.client.get("/api/admin/users").get_json()["users"]
        plex_user = next(user for user in users if user["id"] == plex_user_id)
        self.assertEqual(plex_user["username"], "SharedName")
        self.assertEqual(plex_user["localUsername"], "shared-name-2")

        canonical = self.client.get(
            "/api/account/profile?username=shared-name-2"
        )
        ambiguous_display_name = self.client.get(
            "/api/account/profile?username=SharedName"
        )

        self.assertEqual(canonical.status_code, 200)
        self.assertEqual(canonical.get_json()["user"]["id"], plex_user_id)
        self.assertEqual(ambiguous_display_name.status_code, 200)
        self.assertEqual(
            ambiguous_display_name.get_json()["user"]["id"],
            local_user_id,
        )

    def test_admin_reads_canonical_target_despite_own_plex_name_collision(self):
        self.register()
        target_id = self.add_user("target-user")
        with db() as connection:
            connection.execute(
                "UPDATE users SET plex_username = ? WHERE username = ?",
                ("target-user", "test-user"),
            )

        response = self.client.get("/api/account/profile?username=target-user")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["user"]["id"], target_id)

    def test_admin_updates_canonical_target_despite_own_plex_name_collision(self):
        csrf_token = self.register()
        target_id = self.add_user("target-user")
        with db() as connection:
            connection.execute(
                "UPDATE users SET plex_username = ? WHERE username = ?",
                ("target-user", "test-user"),
            )

        response = self.client.post(
            "/api/account/general?username=target-user",
            json={"username": "renamed-target", "password": ""},
            headers={"X-CSRF-Token": csrf_token},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["username"], "renamed-target")
        with db() as connection:
            admin = connection.execute(
                "SELECT username FROM users WHERE username = 'test-user'"
            ).fetchone()
            target = connection.execute(
                "SELECT username FROM users WHERE id = ?",
                (target_id,),
            ).fetchone()
        self.assertIsNotNone(admin)
        self.assertEqual(admin["username"], "test-user")
        self.assertEqual(target["username"], "renamed-target")

    @patch("backend.routes.admin._profile_plex_index")
    def test_admin_request_list_includes_metadata_availability_and_requesters(
        self, profile_plex_index
    ):
        self.register()
        local_user_id = self.add_user(
            "local-listener",
            lastfm_api_key="never-return-this-local-key",
        )
        plex_user_id = self.add_user(
            "generated-plex-name",
            plex_id="never-return-this-plex-id",
            plex_username="Plex Listener",
            plex_email="plex@example.com",
            plex_avatar="https://plex.example/avatar.jpg",
            lastfm_api_key="never-return-this-plex-key",
        )
        with db() as connection:
            connection.executemany(
                "INSERT INTO request_history "
                "(user_id, kind, mbid, name, artist_name, release_type, "
                "release_date, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        local_user_id,
                        "artist",
                        "artist-1",
                        "Artist One",
                        None,
                        None,
                        None,
                        100,
                    ),
                    (
                        plex_user_id,
                        "release-group",
                        "release-1",
                        "Album One",
                        "Artist Two",
                        "Album",
                        "2024-02-03",
                        300,
                    ),
                    (
                        local_user_id,
                        "release-group",
                        "release-2",
                        "Newest at Same Time",
                        "Artist Three",
                        "EP",
                        "2025",
                        300,
                    ),
                ],
            )
        anime_theme_links.sync_anime_theme_mapping(
            {"slug": "anime", "name": "Admin Anime"},
            {
                "id": 5,
                "label": "Ending 1",
                "type": "ED",
                "sequence": 1,
                "song": {"id": 6, "title": "Ending Song"},
            },
            {"state": "resolved", "releaseGroups": [{"id": "release-1"}]},
        )
        profile_plex_index.return_value = {
            "artistsByMbid": {
                "artist-1": {
                    "url": "https://app.plex.tv/artist",
                    "plexampUrl": "https://listen.plex.tv/artist/example",
                },
            },
            "releaseGroupsByMbid": {
                "release-1": [{
                    "artistName": "Artist Two",
                    "releaseType": "Album",
                    "year": 2024,
                    "url": "https://app.plex.tv/album",
                    "plexampUrl": "https://listen.plex.tv/album/example",
                }],
            },
        }

        response = self.client.get("/api/admin/requests")

        self.assertEqual(response.status_code, 200)
        requests_payload = response.get_json()["requests"]
        self.assertEqual(response.get_json()["pagination"], {
            "page": 1,
            "pageSize": 100,
            "total": 3,
            "totalPages": 1,
        })
        self.assertEqual(
            [item["name"] for item in requests_payload],
            ["Newest at Same Time", "Album One", "Artist One"],
        )
        self.assertTrue(all(isinstance(item["id"], int) for item in requests_payload))
        self.assertGreater(requests_payload[0]["id"], requests_payload[1]["id"])

        release = requests_payload[1]
        self.assertEqual(release["kind"], "release-group")
        self.assertEqual(release["mbid"], "release-1")
        self.assertEqual(release["artist_name"], "Artist Two")
        self.assertEqual(release["release_type"], "Album")
        self.assertEqual(release["release_date"], "2024-02-03")
        self.assertEqual(release["created_at"], 300)
        self.assertEqual(release["animeThemes"][0]["animePath"], "/anime/anime#theme-5")
        self.assertTrue(release["availableInPlex"])
        self.assertEqual(release["plexUrl"], "https://app.plex.tv/album")
        self.assertEqual(
            release["plexampUrl"],
            "https://listen.plex.tv/album/example",
        )
        self.assertEqual(release["requester"], {
            "id": plex_user_id,
            "username": "Plex Listener",
            "localUsername": "generated-plex-name",
            "userType": "plex",
            "role": "user",
            "plexUsername": "Plex Listener",
            "plexEmail": "plex@example.com",
            "plexAvatar": "https://plex.example/avatar.jpg",
        })

        artist = requests_payload[2]
        self.assertTrue(artist["availableInPlex"])
        self.assertEqual(artist["plexUrl"], "https://app.plex.tv/artist")
        self.assertEqual(
            artist["plexampUrl"],
            "https://listen.plex.tv/artist/example",
        )
        self.assertEqual(artist["requester"], {
            "id": local_user_id,
            "username": "local-listener",
            "localUsername": "local-listener",
            "userType": "local",
            "role": "user",
            "plexUsername": "",
            "plexEmail": "",
            "plexAvatar": "",
        })

        serialized = response.get_data(as_text=True)
        self.assertNotIn("never-return-this-local-key", serialized)
        self.assertNotIn("never-return-this-plex-key", serialized)
        self.assertNotIn("never-return-this-plex-id", serialized)
        self.assertNotIn("password_hash", serialized)

    def test_admin_requests_include_safe_download_lifecycle_and_requester(self):
        self.register()
        requester_id = self.add_user(
            "download-requester", plex_id="plex-private", plex_username="Plex Requester"
        )
        with db() as connection:
            connection.execute(
                "INSERT INTO request_history (user_id, kind, mbid, name, created_at) "
                "VALUES (?, 'release-group', 'admin-group', 'Admin Album', 100)",
                (requester_id,),
            )
        with (
            patch("backend.routes.admin._profile_plex_index", return_value={
                "artistsByMbid": {}, "releaseGroupsByMbid": {},
            }),
            patch("backend.routes.account.lidarr.cached_library_availability", return_value={}),
            patch("backend.routes.account.lidarr.cached_download_availability", return_value={
                "admin-group": {"progress": 70, "status": "downloading",
                                "indexer": "private", "downloadClient": "private"},
            }),
            patch("backend.routes.account.pending_lidarr_search_mbids", return_value={"admin-group"}),
        ):
            response = self.client.get("/api/admin/requests")

        self.assertEqual(response.status_code, 200)
        item = next(value for value in response.get_json()["requests"] if value["mbid"] == "admin-group")
        self.assertEqual(item["requestStatus"], "downloading")
        self.assertEqual(item["downloadStatus"], {"progress": 70, "status": "downloading"})
        self.assertEqual(item["requester"]["id"], requester_id)
        self.assertEqual(item["requester"]["username"], "Plex Requester")
        serialized = response.get_data(as_text=True)
        self.assertNotIn("private", serialized)
        self.assertNotIn("plex-private", serialized)

    @patch(
        "backend.routes.admin._profile_plex_index",
        return_value={"artistsByMbid": {}, "releaseGroupsByMbid": {}},
    )
    def test_admin_request_list_paginates_at_100_items(
        self, profile_plex_index
    ):
        self.register()
        user_id = self.add_user("prolific-listener")
        with db() as connection:
            connection.executemany(
                "INSERT INTO request_history "
                "(user_id, kind, mbid, name, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    (
                        user_id,
                        "artist",
                        f"artist-{index}",
                        f"Request {index}",
                        index,
                    )
                    for index in range(205)
                ],
            )

        first = self.client.get("/api/admin/requests").get_json()
        second = self.client.get("/api/admin/requests?page=2").get_json()
        third = self.client.get("/api/admin/requests?page=3").get_json()

        self.assertEqual(len(first["requests"]), 100)
        self.assertEqual(first["requests"][0]["name"], "Request 204")
        self.assertEqual(first["requests"][-1]["name"], "Request 105")
        self.assertEqual(len(second["requests"]), 100)
        self.assertEqual(second["requests"][0]["name"], "Request 104")
        self.assertEqual(second["requests"][-1]["name"], "Request 5")
        self.assertEqual(
            [item["name"] for item in third["requests"]],
            [
                "Request 4",
                "Request 3",
                "Request 2",
                "Request 1",
                "Request 0",
            ],
        )
        self.assertEqual(first["pagination"], {
            "page": 1,
            "pageSize": 100,
            "total": 205,
            "totalPages": 3,
        })
        self.assertEqual(second["pagination"]["page"], 2)
        self.assertEqual(third["pagination"]["page"], 3)
        profile_plex_index.assert_called()

    def test_admin_request_list_rejects_invalid_pages(self):
        self.register()

        for page in ("0", "-1", "nope", "1.5", str(2 ** 100)):
            with self.subTest(page=page):
                response = self.client.get(
                    f"/api/admin/requests?page={page}"
                )
                self.assertEqual(response.status_code, 400)
                self.assertIn("positive integer", response.get_json()["error"])

    def test_admin_request_list_requires_an_administrator(self):
        self.assertEqual(
            self.client.get("/api/admin/requests").status_code,
            401,
        )
        admin_csrf = self.register()
        self.add_user("ordinary-user")
        self.client.post(
            "/api/auth/logout",
            headers={"X-CSRF-Token": admin_csrf},
        )
        login = self.client.post(
            "/api/auth/login",
            json={
                "username": "ordinary-user",
                "password": "listener-password",
            },
        )
        self.assertEqual(login.status_code, 200)

        response = self.client.get("/api/admin/requests")

        self.assertEqual(response.status_code, 403)
        self.assertIn("Administrator", response.get_json()["error"])

    def test_user_list_edits_and_deletion_require_an_admin(self):
        self.assertEqual(self.client.get("/api/admin/users").status_code, 401)
        admin_csrf = self.register()
        user_id = self.add_user("ordinary-user")
        self.assertEqual(
            self.client.delete(f"/api/admin/users/{user_id}").status_code,
            403,
        )
        self.client.post(
            "/api/auth/logout", headers={"X-CSRF-Token": admin_csrf}
        )
        login = self.client.post(
            "/api/auth/login",
            json={
                "username": "ordinary-user",
                "password": "listener-password",
            },
        )
        csrf_token = login.get_json()["csrfToken"]

        self.assertEqual(self.client.get("/api/admin/users").status_code, 403)
        self.assertEqual(
            self.client.patch(
                f"/api/admin/users/{user_id}",
                json={"role": "admin"},
                headers={"X-CSRF-Token": csrf_token},
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.delete(
                f"/api/admin/users/{user_id}",
                headers={"X-CSRF-Token": csrf_token},
            ).status_code,
            403,
        )

    @patch("backend.routes.admin.recommendation_worker.request_refresh")
    def test_admin_can_edit_account_and_recommendation_settings(
        self, request_refresh
    ):
        csrf_token = self.register()
        save_service("lastfm", {"apiKey": "admin-shared-key"})
        original_hash = generate_password_hash("original-password")
        user_id = self.add_user(
            "old-name",
            lastfm_username="old-lastfm",
        )
        with db() as connection:
            connection.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (original_hash, user_id),
            )
            connection.execute(
                "INSERT INTO recommendation_cache "
                "(user_id, value, refreshed_at) VALUES (?, '{}', ?)",
                (user_id, time.time()),
            )

        response = self.client.patch(
            f"/api/admin/users/{user_id}",
            json={
                "role": "admin",
                "localUsername": "new-name",
                "password": "",
                "listenbrainzUsername": "new-listens",
                "lastfmUsername": "new-lastfm",
            },
            headers={"X-CSRF-Token": csrf_token},
        )

        self.assertEqual(response.status_code, 200)
        user = response.get_json()["user"]
        self.assertEqual(user["localUsername"], "new-name")
        self.assertEqual(user["role"], "admin")
        self.assertEqual(user["listenbrainzUsername"], "new-listens")
        self.assertEqual(user["lastfmUsername"], "new-lastfm")
        self.assertTrue(user["lastfmConfigured"])
        self.assertNotIn("lastfmApiKey", user)
        with db() as connection:
            saved = connection.execute(
                "SELECT password_hash, lastfm_username FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
            cached = connection.execute(
                "SELECT 1 FROM recommendation_cache WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        self.assertEqual(saved["password_hash"], original_hash)
        self.assertEqual(saved["lastfm_username"], "new-lastfm")
        self.assertIsNone(cached)
        request_refresh.assert_called_once_with()

    def test_admin_edit_validation_protects_roles_and_plex_identity(self):
        csrf_token = self.register()
        with db() as connection:
            admin_id = connection.execute(
                "SELECT id FROM users WHERE username = 'test-user'"
            ).fetchone()["id"]
        self.assertEqual(
            self.client.patch(
                f"/api/admin/users/{admin_id}",
                json={"role": "user"},
                headers={"X-CSRF-Token": csrf_token},
            ).status_code,
            409,
        )

        second_admin_id = self.add_user("second-admin", role="admin")
        self.assertEqual(
            self.client.patch(
                f"/api/admin/users/{admin_id}",
                json={"role": "user"},
                headers={"X-CSRF-Token": csrf_token},
            ).status_code,
            409,
        )
        readonly = self.client.patch(
            f"/api/admin/users/{second_admin_id}",
            json={"plexUsername": "impersonated"},
            headers={"X-CSRF-Token": csrf_token},
        )
        self.assertEqual(readonly.status_code, 400)
        self.assertIn("managed by Plex", readonly.get_json()["error"])

    def test_admin_edit_validates_local_credentials_and_duplicates(self):
        csrf_token = self.register()
        user_id = self.add_user("ordinary-user")
        duplicate = self.client.patch(
            f"/api/admin/users/{user_id}",
            json={"localUsername": "test-user"},
            headers={"X-CSRF-Token": csrf_token},
        )
        short_password = self.client.patch(
            f"/api/admin/users/{user_id}",
            json={"password": "too-short"},
            headers={"X-CSRF-Token": csrf_token},
        )
        missing = self.client.patch(
            "/api/admin/users/999999",
            json={"role": "user"},
            headers={"X-CSRF-Token": csrf_token},
        )

        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(short_password.status_code, 400)
        self.assertEqual(missing.status_code, 404)

    def test_admin_can_delete_a_user_and_all_owned_data(self):
        csrf_token = self.register()
        user_id = self.add_user(
            "departing-user",
            plex_id="deleted-plex-id",
            plex_username="Departing Plex User",
        )
        now = time.time()
        with db() as connection:
            connection.execute(
                "INSERT INTO plex_auth_flows "
                "(flow_hash, pin_id, client_identifier, purpose, user_id, "
                "created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    "departing-flow",
                    123,
                    "client-id",
                    "link",
                    user_id,
                    now,
                    now + 600,
                ),
            )
            connection.execute(
                "INSERT INTO request_history "
                "(user_id, kind, mbid, name, created_at) "
                "VALUES (?, 'artist', ?, ?, ?)",
                (user_id, "departing-artist", "Departing Artist", now),
            )
            connection.execute(
                "INSERT INTO recommendation_cache "
                "(user_id, value, refreshed_at) VALUES (?, '{}', ?)",
                (user_id, now),
            )
            connection.execute(
                "INSERT INTO plex_listens "
                "(server_id, history_key, user_id, artist_rating_key, played_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("server-1", "departing-history", user_id, "artist-1", now),
            )
            connection.execute(
                "INSERT INTO account_invitations "
                "(token_hash, created_by, created_at, expires_at) "
                "VALUES (?, ?, ?, ?)",
                ("departing-invite", user_id, now, now + 600),
            )
            job_id = connection.execute(
                "INSERT INTO pending_lidarr_searches "
                "(mbid, album_id, artist_id, name, next_attempt_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    "departing-release",
                    41,
                    42,
                    "Departing Album",
                    now,
                    now,
                ),
            ).lastrowid
            connection.execute(
                "INSERT INTO pending_lidarr_search_requesters "
                "(job_id, user_id) VALUES (?, ?)",
                (job_id, user_id),
            )

        response = self.client.delete(
            f"/api/admin/users/{user_id}",
            headers={"X-CSRF-Token": csrf_token},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["message"], "User deleted.")
        with db() as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM users WHERE id = ?", (user_id,)
                ).fetchone()
            )
            for table, column in (
                ("plex_auth_flows", "user_id"),
                ("pending_lidarr_search_requesters", "user_id"),
                ("recommendation_cache", "user_id"),
                ("plex_listens", "user_id"),
                ("request_history", "user_id"),
                ("account_invitations", "created_by"),
            ):
                self.assertIsNone(
                    connection.execute(
                        f"SELECT 1 FROM {table} WHERE {column} = ?",
                        (user_id,),
                    ).fetchone()
                )
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM pending_lidarr_searches WHERE id = ?",
                    (job_id,),
                ).fetchone()
            )

    def test_deleting_one_requester_preserves_shared_lidarr_work(self):
        csrf_token = self.register()
        first_user_id = self.add_user("first-requester")
        second_user_id = self.add_user("second-requester")
        mbid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        enqueue_lidarr_search(
            first_user_id,
            mbid,
            41,
            42,
            "Shared Album",
        )
        storage_module.record_request(
            second_user_id,
            "release-group",
            mbid,
            "Shared Album",
        )

        first_delete = self.client.delete(
            f"/api/admin/users/{first_user_id}",
            headers={"X-CSRF-Token": csrf_token},
        )

        self.assertEqual(first_delete.status_code, 200)
        with db() as connection:
            job = connection.execute(
                "SELECT id FROM pending_lidarr_searches WHERE mbid = ?",
                (mbid,),
            ).fetchone()
            requester_ids = [
                row["user_id"]
                for row in connection.execute(
                    "SELECT user_id FROM pending_lidarr_search_requesters "
                    "WHERE job_id = ? ORDER BY user_id",
                    (job["id"],),
                )
            ]
        self.assertIsNotNone(job)
        self.assertEqual(requester_ids, [second_user_id])

        second_delete = self.client.delete(
            f"/api/admin/users/{second_user_id}",
            headers={"X-CSRF-Token": csrf_token},
        )

        self.assertEqual(second_delete.status_code, 200)
        with db() as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM pending_lidarr_searches WHERE mbid = ?",
                    (mbid,),
                ).fetchone()
            )

    def test_admin_cannot_delete_self_and_missing_user_returns_not_found(self):
        csrf_token = self.register()
        with db() as connection:
            admin_id = connection.execute(
                "SELECT id FROM users WHERE username = 'test-user'"
            ).fetchone()["id"]

        self_delete = self.client.delete(
            f"/api/admin/users/{admin_id}",
            headers={"X-CSRF-Token": csrf_token},
        )
        missing = self.client.delete(
            "/api/admin/users/999999",
            headers={"X-CSRF-Token": csrf_token},
        )

        self.assertEqual(self_delete.status_code, 409)
        self.assertIn("own account", self_delete.get_json()["error"])
        self.assertEqual(missing.status_code, 404)


class SettingsMaintenanceTests(DatabaseTestCase):
    def test_admin_can_view_and_save_melodarr_instance_settings(self):
        token = self.register()

        initial = self.client.get("/api/settings")
        self.assertEqual(initial.status_code, 200)
        melodarr = initial.get_json()["melodarr"]
        self.assertEqual(melodarr["applicationTitle"], "Melodarr")
        self.assertEqual(melodarr["version"], backend_config.APPLICATION_VERSION)
        self.assertEqual(
            melodarr["apiKey"],
            self.app.config["AUTOMATION_API_KEY"],
        )

        without_csrf = self.client.post(
            "/api/settings/melodarr",
            json={
                "applicationTitle": "Family Music",
                "applicationUrl": "https://music.example.com",
            },
        )
        self.assertEqual(without_csrf.status_code, 403)

        saved = self.client.post(
            "/api/settings/melodarr",
            json={
                "applicationTitle": "Family Music",
                "applicationUrl": "https://music.example.com/",
            },
            headers={"X-CSRF-Token": token},
        )

        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.get_json()["melodarr"]["applicationTitle"], "Family Music")
        self.assertEqual(
            saved.get_json()["melodarr"]["applicationUrl"],
            "https://music.example.com",
        )
        self.assertEqual(
            get_service("melodarr")["applicationUrl"],
            "https://music.example.com",
        )
        self.assertEqual(
            self.client.get("/api/settings/notifications").get_json()["applicationUrl"],
            "https://music.example.com",
        )

    def test_regenerating_api_key_immediately_replaces_resolver_access(self):
        token = self.register()
        old_key = self.app.config["AUTOMATION_API_KEY"]

        rotated = self.client.post(
            "/api/settings/melodarr/api-key/regenerate",
            headers={"X-CSRF-Token": token},
        )

        self.assertEqual(rotated.status_code, 200)
        new_key = rotated.get_json()["melodarr"]["apiKey"]
        self.assertNotEqual(new_key, old_key)
        self.assertEqual(get_service("melodarr")["apiKey"], new_key)
        with self.client.session_transaction() as session:
            session.clear()
        payload = {"releaseGroupId": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"}
        old_access = self.client.post(
            "/api/v1/animethemes/resolve",
            json=payload,
            headers={"X-Api-Key": old_key},
        )
        new_access = self.client.post(
            "/api/v1/animethemes/resolve",
            json=payload,
            headers={"X-Api-Key": new_key},
        )
        self.assertEqual(old_access.status_code, 401)
        self.assertEqual(new_access.status_code, 200)
        self.assertEqual(new_access.get_json(), {"series": []})

    def test_environment_managed_api_key_cannot_be_regenerated_in_ui(self):
        token = self.register()
        self.app.config["AUTOMATION_API_KEY_MANAGED_BY_ENVIRONMENT"] = True
        original = self.app.config["AUTOMATION_API_KEY"]

        response = self.client.post(
            "/api/settings/melodarr/api-key/regenerate",
            headers={"X-CSRF-Token": token},
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.app.config["AUTOMATION_API_KEY"], original)
        self.assertIn("MELODARR_AUTOMATION_API_KEY", response.get_json()["error"])

    def test_melodarr_settings_validate_title_url_and_payload_shape(self):
        token = self.register()
        headers = {"X-CSRF-Token": token}

        blank_title = self.client.post(
            "/api/settings/melodarr",
            json={"applicationTitle": " ", "applicationUrl": ""},
            headers=headers,
        )
        credential_url = self.client.post(
            "/api/settings/melodarr",
            json={
                "applicationTitle": "Melodarr",
                "applicationUrl": "https://user:secret@example.com",
            },
            headers=headers,
        )
        extra_field = self.client.post(
            "/api/settings/melodarr",
            json={
                "applicationTitle": "Melodarr",
                "applicationUrl": "",
                "apiKey": "caller-must-not-set-this",
            },
            headers=headers,
        )

        self.assertEqual(blank_title.status_code, 400)
        self.assertEqual(credential_url.status_code, 400)
        self.assertEqual(extra_field.status_code, 400)

    def test_legacy_user_key_is_promoted_and_per_user_copies_are_scrubbed(self):
        self.register()
        with db() as connection:
            connection.execute(
                "UPDATE users SET lastfm_api_key = ? WHERE username = ?",
                ("legacy-admin-key", "test-user"),
            )
            connection.execute(
                "INSERT INTO users "
                "(username, password_hash, role, lastfm_api_key, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    "legacy-listener",
                    generate_password_hash("listener-password"),
                    "user",
                    "legacy-user-key",
                    time.time(),
                ),
            )
        write_settings_file({})

        init_db()

        self.assertEqual(get_lastfm_api_key(), "legacy-admin-key")
        with db() as connection:
            remaining = connection.execute(
                "SELECT COUNT(*) AS total FROM users "
                "WHERE lastfm_api_key IS NOT NULL"
            ).fetchone()["total"]
        self.assertEqual(remaining, 0)

    @patch("backend.routes.settings.recommendation_worker.request_refresh")
    @patch("backend.routes.settings.lastfm.get")
    def test_admin_can_save_replace_and_clear_the_shared_lastfm_key(
        self, lastfm_get, request_refresh
    ):
        token = self.register()
        initial = self.client.get("/api/settings")
        self.assertEqual(initial.status_code, 200)
        self.assertEqual(initial.get_json()["lastfm"], {"configured": False})

        saved = self.client.post(
            "/api/settings/lastfm",
            json={"apiKey": "first-shared-key"},
            headers={"X-CSRF-Token": token},
        )
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(
            saved.get_json()["message"],
            "Last.fm API key saved.",
        )
        self.assertEqual(get_service("lastfm"), {"apiKey": "first-shared-key"})
        lastfm_get.assert_called_once_with(
            "chart.gettopartists",
            "melodarr",
            "first-shared-key",
            limit=1,
            force_refresh=True,
            cache_response=False,
        )

        configured = self.client.get("/api/settings")
        self.assertEqual(
            configured.get_json()["lastfm"],
            {"configured": True},
        )
        self.assertNotIn("first-shared-key", configured.get_data(as_text=True))
        self.assertNotIn("first-shared-key", saved.get_data(as_text=True))

        replaced = self.client.post(
            "/api/settings/lastfm",
            json={"apiKey": "replacement-shared-key"},
            headers={"X-CSRF-Token": token},
        )
        self.assertEqual(replaced.status_code, 200)
        self.assertEqual(
            get_service("lastfm"),
            {"apiKey": "replacement-shared-key"},
        )
        self.assertNotIn(
            "replacement-shared-key",
            replaced.get_data(as_text=True),
        )

        cleared = self.client.post(
            "/api/settings/lastfm",
            json={"apiKey": "   "},
            headers={"X-CSRF-Token": token},
        )
        self.assertEqual(cleared.status_code, 200)
        self.assertEqual(
            cleared.get_json()["message"],
            "Last.fm API key removed.",
        )
        self.assertFalse(get_service("lastfm"))
        self.assertEqual(
            self.client.get("/api/settings").get_json()["lastfm"],
            {"configured": False},
        )
        self.assertEqual(lastfm_get.call_count, 2)
        self.assertEqual(request_refresh.call_count, 3)

    @patch("backend.routes.settings.lastfm.get")
    def test_shared_lastfm_key_is_validated_and_never_returned(
        self, lastfm_get
    ):
        token = self.register()
        lastfm_get.side_effect = ValueError("Invalid Last.fm API key.")

        invalid = self.client.post(
            "/api/settings/lastfm",
            json={"apiKey": "invalid-shared-key"},
            headers={"X-CSRF-Token": token},
        )
        missing = self.client.post(
            "/api/settings/lastfm",
            json={},
            headers={"X-CSRF-Token": token},
        )
        wrong_type = self.client.post(
            "/api/settings/lastfm",
            json={"apiKey": ["not", "a", "string"]},
            headers={"X-CSRF-Token": token},
        )

        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(missing.status_code, 400)
        self.assertEqual(wrong_type.status_code, 400)
        self.assertFalse(get_service("lastfm"))
        self.assertNotIn(
            "invalid-shared-key",
            invalid.get_data(as_text=True),
        )

    @patch("backend.routes.settings.lastfm.get")
    def test_shared_lastfm_validation_does_not_reflect_provider_text(self, lastfm_get):
        lastfm_get.side_effect = ValueError("sentinel-secret-provider-detail")
        token = self.register()
        with self.assertLogs(self.app.logger.name, level="WARNING") as logs:
            response = self.client.post(
                "/api/settings/lastfm",
                json={"apiKey": "candidate-key"},
                headers={"X-CSRF-Token": token},
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.get_json(), {"error": "Unable to validate the Last.fm API key."}
        )
        self.assertNotIn("sentinel-secret-provider-detail", logs.output[0])
        self.assertFalse(get_service("lastfm"))

    def test_only_administrators_can_manage_the_shared_lastfm_key(self):
        admin_token = self.register()
        with db() as connection:
            connection.execute(
                "INSERT INTO users "
                "(username, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
                (
                    "ordinary-user",
                    generate_password_hash("listener-password"),
                    "user",
                    time.time(),
                ),
            )
        self.client.post(
            "/api/auth/logout",
            headers={"X-CSRF-Token": admin_token},
        )
        login = self.client.post(
            "/api/auth/login",
            json={
                "username": "ordinary-user",
                "password": "listener-password",
            },
        )
        response = self.client.post(
            "/api/settings/lastfm",
            json={"apiKey": "user-supplied-key"},
            headers={"X-CSRF-Token": login.get_json()["csrfToken"]},
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(get_service("lastfm"))

    @patch("backend.routes.settings.musicbrainz.test_connection")
    def test_admin_can_save_and_test_self_hosted_musicbrainz(self, test_connection):
        token = self.register()
        initial = self.client.get("/api/settings")
        self.assertEqual(initial.status_code, 200)
        self.assertEqual(initial.get_json()["musicbrainz"], {
            "baseUrl": "https://musicbrainz.org/ws/2",
            "userAgent": backend_config.USER_AGENT,
            "requestIntervalMs": 1100,
        })

        proposed = {
            "baseUrl": "http://192.168.1.10:5000/ws/2/",
            "userAgent": "Melodarr Self Hosted/1.0",
            "requestIntervalMs": 0,
        }
        saved = self.client.post(
            "/api/settings/musicbrainz",
            json=proposed,
            headers={"X-CSRF-Token": token},
        )
        self.assertEqual(saved.status_code, 200)
        expected = {**proposed, "baseUrl": proposed["baseUrl"].rstrip("/")}
        self.assertEqual(saved.get_json()["musicbrainz"], expected)
        self.assertEqual(get_service("musicbrainz"), expected)
        self.assertEqual(
            self.client.get("/api/settings").get_json()["musicbrainz"],
            expected,
        )

        test_connection.return_value = {
            "message": "Connected to MusicBrainz WS2.",
            "baseUrl": expected["baseUrl"],
            "latencyMs": 12,
            "recording": {
                "id": musicbrainz.TEST_RECORDING_ID,
                "title": "Intro",
            },
        }
        tested = self.client.post(
            "/api/settings/musicbrainz/test",
            json=proposed,
            headers={"X-CSRF-Token": token},
        )
        self.assertEqual(tested.status_code, 200)
        self.assertEqual(tested.get_json()["latencyMs"], 12)
        test_connection.assert_called_once_with(proposed)

    def test_invalid_musicbrainz_settings_are_not_saved(self):
        token = self.register()
        valid = {
            "baseUrl": "http://musicbrainz.local:5000/ws/2",
            "userAgent": "Melodarr Test/1.0",
            "requestIntervalMs": 0,
        }
        invalid_values = (
            {**valid, "baseUrl": "ftp://musicbrainz.local/ws/2"},
            {**valid, "userAgent": "   "},
            {**valid, "requestIntervalMs": -1},
            {**valid, "requestIntervalMs": 1.5},
        )

        for values in invalid_values:
            with self.subTest(values=values):
                response = self.client.post(
                    "/api/settings/musicbrainz",
                    json=values,
                    headers={"X-CSRF-Token": token},
                )
                self.assertEqual(response.status_code, 400)
        self.assertIsNone(get_service("musicbrainz"))

    @patch("backend.routes.settings.plex_history_worker.request_full_sync")
    @patch("backend.routes.settings.lidarr_library_worker.request_scan")
    @patch("backend.routes.settings.plex_metadata_worker.request_enrichment")
    @patch("backend.routes.settings.plex_worker.request_full_scan")
    @patch("backend.routes.settings.plex_worker.request_recent_scan")
    @patch("backend.routes.settings.lidarr_search_worker.request_work")
    @patch("backend.routes.settings.recommendation_worker.request_refresh")
    def test_jobs_are_listed_and_can_be_manually_queued(
        self, request_refresh, request_work, request_recent, request_full,
        request_enrichment, request_lidarr_scan, request_history,
    ):
        token = self.register()
        response = self.client.get("/api/settings/maintenance")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [job["id"] for job in response.get_json()["jobs"]],
            [
                "recommendations",
                "lidarr-followups",
                "lidarr-library",
                "plex-recent",
                "plex-full",
                "plex-metadata",
                "plex-history",
            ],
        )
        job_rows = response.get_json()["jobs"]
        self.assertEqual(
            [job["name"] for job in job_rows],
            [
                "Recommendation Refresh",
                "Lidarr Search Follow-Ups",
                "Lidarr Library Scan",
                "Plex Recently Added Scan",
                "Plex Full Library Scan",
                "Plex MusicBrainz Enrichment",
                "Plex Listening History",
            ],
        )
        jobs = {job["id"]: job for job in job_rows}
        self.assertEqual(backend_config.LIDARR_LIBRARY_SCAN_INTERVAL, 2 * 60)
        self.assertEqual(backend_config.PLEX_RECENT_SCAN_INTERVAL, 3 * 60)
        self.assertEqual(jobs["lidarr-library"]["schedule"], "Every 2 minutes")
        self.assertEqual(jobs["plex-recent"]["schedule"], "Every 3 minutes")
        self.assertEqual(jobs["plex-history"]["schedule"], "Every 24 hours")

        recommendation = self.client.post(
            "/api/settings/jobs/recommendations/run",
            headers={"X-CSRF-Token": token},
        )
        lidarr = self.client.post(
            "/api/settings/jobs/lidarr-followups/run",
            headers={"X-CSRF-Token": token},
        )
        lidarr_library = self.client.post(
            "/api/settings/jobs/lidarr-library/run",
            headers={"X-CSRF-Token": token},
        )
        recent = self.client.post(
            "/api/settings/jobs/plex-recent/run",
            headers={"X-CSRF-Token": token},
        )
        full = self.client.post(
            "/api/settings/jobs/plex-full/run",
            headers={"X-CSRF-Token": token},
        )
        enrichment = self.client.post(
            "/api/settings/jobs/plex-metadata/run",
            headers={"X-CSRF-Token": token},
        )
        history = self.client.post(
            "/api/settings/jobs/plex-history/run",
            headers={"X-CSRF-Token": token},
        )
        self.assertEqual(recommendation.status_code, 200)
        self.assertEqual(lidarr.status_code, 200)
        self.assertEqual(lidarr_library.status_code, 200)
        self.assertEqual(recent.status_code, 200)
        self.assertEqual(full.status_code, 200)
        self.assertEqual(enrichment.status_code, 200)
        self.assertEqual(history.status_code, 200)
        request_refresh.assert_called_once_with()
        request_work.assert_called_once_with()
        request_lidarr_scan.assert_called_once_with()
        request_recent.assert_called_once_with()
        request_full.assert_called_once_with()
        request_enrichment.assert_called_once_with()
        request_history.assert_called_once_with()

    def test_raw_plex_tokens_are_not_accepted_by_settings(self):
        token = self.register()
        response = self.client.post(
            "/api/settings/plex",
            json={"url": "http://plex:32400", "token": "manually-copied-token"},
            headers={"X-CSRF-Token": token},
        )
        self.assertEqual(response.status_code, 405)

    @patch("backend.routes.settings.plex_worker.request_full_scan")
    def test_flushing_plex_library_cache_queues_a_full_scan(self, request_full_scan):
        token = self.register()
        with cache_db() as connection:
            connection.executemany(
                "INSERT INTO api_cache (cache_key, value, expires_at) VALUES (?, ?, ?)",
                [
                    ("plex-library:test", "{}", time.time() + 60),
                    ("plex-guid:test", "{}", time.time() + 60),
                ],
            )
        response = self.client.post(
            "/api/settings/cache/plex-library/flush",
            headers={"X-CSRF-Token": token},
        )
        self.assertEqual(response.status_code, 200)
        with cache_db() as connection:
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM api_cache WHERE cache_key LIKE 'plex-library:%' "
                "OR cache_key LIKE 'plex-guid:%'"
            ).fetchone())
        request_full_scan.assert_called_once_with()

    @patch("backend.routes.settings.recommendation_worker.request_refresh")
    def test_cache_stats_and_targeted_flushes(self, request_refresh):
        token = self.register()
        with db() as connection:
            user_id = connection.execute(
                "SELECT id FROM users WHERE username = 'test-user'"
            ).fetchone()["id"]
            connection.execute(
                "INSERT INTO recommendation_cache (user_id, value, refreshed_at) "
                "VALUES (?, ?, ?)",
                (user_id, json.dumps({"artists": [1]}), time.time()),
            )
        with cache_db() as connection:
            connection.execute(
                "INSERT INTO api_cache (cache_key, value, expires_at) VALUES (?, ?, ?)",
                ("musicbrainz-metadata:test", json.dumps({"name": "cached"}), time.time() + 60),
            )
        os.makedirs(ARTWORK_CACHE_DIRECTORY, exist_ok=True)
        artwork_path = os.path.join(ARTWORK_CACHE_DIRECTORY, "maintenance-test.jpg")
        with open(artwork_path, "wb") as file:
            file.write(b"artwork")

        caches = self.client.get("/api/settings/maintenance").get_json()["caches"]
        self.assertEqual(
            [cache["id"] for cache in caches],
            [
                "musicbrainz-search",
                "musicbrainz-metadata",
                "musicbrainz-artist-revalidation",
                "listenbrainz-metadata",
                "lastfm",
                "lidarr-options",
                "lidarr-library",
                "lidarr-artist-metadata",
                "plex-library",
                "plex-guid",
                "recommendations",
                "artwork",
            ],
        )
        self.assertEqual(
            [cache["name"] for cache in caches],
            [
                "MusicBrainz Search",
                "MusicBrainz Metadata",
                "MusicBrainz Artist Revalidation",
                "ListenBrainz Metadata",
                "Last.fm",
                "Lidarr Options",
                "Lidarr Library Availability",
                "Lidarr Artist Metadata",
                "Plex Library Inventory",
                "Plex GUID Mappings",
                "Assembled Recommendations",
                "Artwork Files",
            ],
        )
        by_id = {cache["id"]: cache for cache in caches}
        self.assertEqual(by_id["musicbrainz-metadata"]["entries"], 1)
        self.assertEqual(by_id["recommendations"]["entries"], 1)
        self.assertGreaterEqual(by_id["artwork"]["entries"], 1)

        for cache_id in ("musicbrainz-metadata", "recommendations", "artwork"):
            response = self.client.post(
                f"/api/settings/cache/{cache_id}/flush",
                headers={"X-CSRF-Token": token},
            )
            self.assertEqual(response.status_code, 200)

        with cache_db() as connection:
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM api_cache WHERE cache_key LIKE 'musicbrainz-metadata:%'"
            ).fetchone())
        with db() as connection:
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM recommendation_cache"
            ).fetchone())
        self.assertFalse(os.path.exists(artwork_path))
        request_refresh.assert_called_once_with()


class ListenBrainzLinkingTests(DatabaseTestCase):
    def link(self, token):
        return self.client.post(
            "/api/account/settings",
            json={"username": "bitemyear"},
            headers={"X-CSRF-Token": token},
        )

    def saved_username(self):
        with db() as connection:
            row = connection.execute(
                "SELECT listenbrainz_username FROM users WHERE username = ?",
                ("test-user",),
            ).fetchone()
        return row["listenbrainz_username"]

    @patch("backend.routes.account.recommendation_worker.request_refresh")
    @patch("backend.routes.account.listenbrainz.user_listen_count")
    def test_valid_username_is_saved_and_refresh_is_queued(
        self, listen_count, request_refresh
    ):
        listen_count.return_value = Response(200, {"payload": {"count": 10}})
        token = self.register()
        with db() as connection:
            user_id = connection.execute(
                "SELECT id FROM users WHERE username = ?", ("test-user",)
            ).fetchone()["id"]
            connection.execute(
                "INSERT INTO recommendation_cache (user_id, value, refreshed_at) "
                "VALUES (?, ?, ?)",
                (user_id, '{"artists": []}', 0),
            )

        response = self.link(token)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()["validationDeferred"])
        self.assertEqual(self.saved_username(), "bitemyear")
        with db() as connection:
            cache = connection.execute(
                "SELECT 1 FROM recommendation_cache WHERE user_id = ?", (user_id,)
            ).fetchone()
        self.assertIsNone(cache)
        request_refresh.assert_called_once_with()

    @patch("backend.routes.account.listenbrainz.user_listen_count")
    def test_confirmed_missing_username_is_rejected(self, listen_count):
        listen_count.return_value = Response(404)
        response = self.link(self.register())
        self.assertEqual(response.status_code, 404)
        self.assertIsNone(self.saved_username())

    @patch("backend.routes.account.listenbrainz.user_listen_count")
    def test_transient_failure_defers_validation_and_saves(self, listen_count):
        private_url = (
            "https://api.listenbrainz.example/user/"
            "bitemyear/listen-count?token=sentinel-secret"
        )
        error = requests.HTTPError(f"HTTP 503 for url: {private_url}")
        error.response = Response(503)
        listen_count.side_effect = error
        with self.assertLogs(level="WARNING") as logs:
            response = self.link(self.register())
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["validationDeferred"])
        self.assertEqual(self.saved_username(), "bitemyear")
        rendered = "\n".join(logs.output)
        self.assertIn("validation deferred", rendered)
        self.assertIn("HTTPError HTTP 503", rendered)
        for private_value in (
            "bitemyear",
            "sentinel-secret",
            private_url,
            "HTTP 503 for url",
        ):
            self.assertNotIn(private_value, rendered)


class LastFmLinkingTests(DatabaseTestCase):
    def saved_lastfm_fields(self):
        with db() as connection:
            return connection.execute(
                "SELECT lastfm_username, lastfm_api_key "
                "FROM users WHERE username = ?",
                ("test-user",),
            ).fetchone()

    @patch("backend.routes.account.recommendation_worker.request_refresh")
    @patch("backend.routes.account.lastfm.get")
    def test_user_saves_only_a_username_with_the_shared_key(
        self, lastfm_get, request_refresh
    ):
        save_service("lastfm", {"apiKey": "admin-shared-key"})
        token = self.register()

        response = self.client.post(
            "/api/account/lastfm",
            json={"username": "personal-listener"},
            headers={"X-CSRF-Token": token},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json()["message"],
            "Last.fm account saved.",
        )
        lastfm_get.assert_called_once_with(
            "user.getinfo",
            "personal-listener",
            "admin-shared-key",
        )
        saved = self.saved_lastfm_fields()
        self.assertEqual(saved["lastfm_username"], "personal-listener")
        self.assertIsNone(saved["lastfm_api_key"])
        account = self.client.get("/api/account/settings")
        self.assertEqual(account.get_json()["lastfmUsername"], "personal-listener")
        self.assertTrue(account.get_json()["lastfmConfigured"])
        self.assertNotIn("admin-shared-key", account.get_data(as_text=True))
        self.assertNotIn("apiKey", account.get_data(as_text=True))
        request_refresh.assert_called_once_with()

    @patch("backend.routes.account.lastfm.get")
    def test_username_requires_an_admin_configured_shared_key(self, lastfm_get):
        response = self.client.post(
            "/api/account/lastfm",
            json={"username": "personal-listener"},
            headers={"X-CSRF-Token": self.register()},
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.get_json(),
            {"error": "Last.fm is not configured. Ask an administrator to add the API key."},
        )
        self.assertIsNone(self.saved_lastfm_fields()["lastfm_username"])
        lastfm_get.assert_not_called()

    @patch("backend.routes.account.lastfm.get")
    def test_provider_error_is_not_reflected_when_linking(self, lastfm_get):
        save_service("lastfm", {"apiKey": "admin-shared-key"})
        lastfm_get.side_effect = ValueError("sentinel-secret-provider-detail")

        with self.assertLogs(self.app.logger.name, level="WARNING") as logs:
            response = self.client.post(
                "/api/account/lastfm",
                json={"username": "personal-listener"},
                headers={"X-CSRF-Token": self.register()},
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json(), {"error": "Unable to link Last.fm account."})
        self.assertNotIn("sentinel-secret-provider-detail", response.get_data(as_text=True))
        self.assertIsNone(self.saved_lastfm_fields()["lastfm_username"])
        self.assertIn("Last.fm account linking failed", logs.output[0])
        self.assertIn("ValueError", logs.output[0])
        self.assertNotIn("sentinel-secret-provider-detail", logs.output[0])

    @patch("backend.routes.account.recommendation_worker.request_refresh")
    @patch("backend.routes.account.lastfm.get")
    def test_user_can_clear_their_username_without_supplying_a_key(
        self, lastfm_get, request_refresh
    ):
        save_service("lastfm", {"apiKey": "admin-shared-key"})
        token = self.register()
        with db() as connection:
            connection.execute(
                "UPDATE users SET lastfm_username = ?, lastfm_api_key = ? "
                "WHERE username = ?",
                ("old-listener", "legacy-user-key", "test-user"),
            )

        response = self.client.post(
            "/api/account/lastfm",
            json={"username": " "},
            headers={"X-CSRF-Token": token},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json()["message"],
            "Last.fm account removed.",
        )
        saved = self.saved_lastfm_fields()
        self.assertIsNone(saved["lastfm_username"])
        self.assertIsNone(saved["lastfm_api_key"])
        lastfm_get.assert_not_called()
        request_refresh.assert_called_once_with()


class ApiCacheTests(DatabaseTestCase):
    @patch("backend.api_cache.requests.get")
    def test_redirects_never_forward_custom_credentials(self, get):
        origin = "https://source.test/data"
        secret = "sentinel-api-key"
        destinations = {
            "same-origin": "https://source.test/redirected",
            "cross-origin": "https://other.test/credential-target",
        }

        for status_code in (301, 302, 307):
            for destination_kind, destination in destinations.items():
                with self.subTest(
                    status_code=status_code,
                    destination_kind=destination_kind,
                ):
                    get.reset_mock()
                    get.return_value = Response(
                        status_code,
                        headers={"Location": destination},
                    )

                    with self.assertRaises(RedirectRejected):
                        cached_json_get(
                            origin,
                            headers={"X-Api-Key": secret},
                            namespace=(
                                f"redirect-{status_code}-{destination_kind}"
                            ),
                            reject_redirects=True,
                            ttl=60,
                        )

                    get.assert_called_once()
                    self.assertEqual(get.call_args.args[0], origin)
                    self.assertEqual(
                        get.call_args.kwargs["headers"],
                        {"X-Api-Key": secret},
                    )
                    self.assertFalse(get.call_args.kwargs["allow_redirects"])
                    self.assertNotEqual(
                        urlparse(get.call_args.args[0]).netloc,
                        "other.test",
                    )

    def test_expiry_cleanup_has_an_index(self):
        with cache_db() as connection:
            indexes = {
                row["name"] for row in connection.execute("PRAGMA index_list(api_cache)")
            }

        self.assertIn("idx_api_cache_expires_at", indexes)

    @patch("backend.api_cache.time.monotonic", return_value=100.0)
    def test_expired_rows_are_cleaned_at_most_once_per_interval(self, monotonic):
        original_last_cleanup = api_cache._last_cleanup_at
        api_cache._last_cleanup_at = None
        try:
            with cache_db() as connection:
                connection.execute(
                    "INSERT INTO api_cache (cache_key, value, expires_at) VALUES (?, ?, ?)",
                    ("expired:first", "{}", time.time() - 1),
                )
            self.assertEqual(api_cache.cleanup_expired_cache(), 1)

            with cache_db() as connection:
                connection.execute(
                    "INSERT INTO api_cache (cache_key, value, expires_at) VALUES (?, ?, ?)",
                    ("expired:second", "{}", time.time() - 1),
                )
            self.assertEqual(api_cache.cleanup_expired_cache(), 0)
            with cache_db() as connection:
                remaining = connection.execute(
                    "SELECT COUNT(*) FROM api_cache WHERE cache_key = 'expired:second'"
                ).fetchone()[0]
            self.assertEqual(remaining, 1)

            monotonic.return_value += api_cache.API_CACHE_CLEANUP_INTERVAL + 1
            self.assertEqual(api_cache.cleanup_expired_cache(), 1)
        finally:
            api_cache._last_cleanup_at = original_last_cleanup

    def test_cache_database_is_configured_for_concurrent_access(self):
        with cache_db() as connection:
            journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
            busy_timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]

        self.assertEqual(journal_mode.casefold(), "wal")
        self.assertGreaterEqual(busy_timeout, 5000)

    @patch("backend.api_cache.time.sleep")
    def test_transient_database_lock_is_retried(self, sleep):
        attempts = 0

        def operation(connection):
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise sqlite3.OperationalError("database is locked")
            return "recovered"

        result = api_cache._cache_operation(operation)

        self.assertEqual(result, "recovered")
        self.assertEqual(attempts, 3)
        self.assertEqual(
            [call.args[0] for call in sleep.call_args_list],
            list(api_cache.CACHE_LOCK_RETRY_DELAYS),
        )

    def test_cache_documents_are_upserted_in_one_batch(self):
        upsert_cache_documents(
            "batch-test",
            {
                "first": {"value": 1},
                "second": {"value": 2},
            },
            60,
        )

        self.assertEqual(
            get_cache_document("batch-test", "first"), {"value": 1}
        )
        self.assertEqual(
            get_cache_document("batch-test", "second"), {"value": 2}
        )

    def test_invalid_cache_document_is_discarded(self):
        key = api_cache.document_cache_key("corrupt-test", "document")
        with cache_db() as connection:
            connection.execute(
                "INSERT INTO api_cache (cache_key, value, expires_at) "
                "VALUES (?, ?, ?)",
                (key, "{invalid", time.time() + 60),
            )

        with self.assertLogs("backend.api_cache", level="WARNING"):
            value = get_cache_document("corrupt-test", "document")

        self.assertIsNone(value)
        with cache_db() as connection:
            row = connection.execute(
                "SELECT 1 FROM api_cache WHERE cache_key = ?",
                (key,),
            ).fetchone()
        self.assertIsNone(row)

    def test_legacy_cache_is_moved_out_of_the_application_database(self):
        with db() as connection:
            connection.execute("""
                CREATE TABLE api_cache (
                    cache_key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    expires_at REAL NOT NULL
                )
            """)
            connection.execute(
                "INSERT INTO api_cache (cache_key, value, expires_at) VALUES (?, ?, ?)",
                ("legacy:key", json.dumps({"result": "preserved"}), time.time() + 60),
            )

        migrate_legacy_cache()

        with db() as connection:
            legacy_table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'api_cache'"
            ).fetchone()
        with cache_db() as connection:
            migrated = connection.execute(
                "SELECT value FROM api_cache WHERE cache_key = 'legacy:key'"
            ).fetchone()
        self.assertIsNone(legacy_table)
        self.assertEqual(json.loads(migrated["value"]), {"result": "preserved"})

    @patch("backend.api_cache.requests.get")
    def test_fresh_external_response_is_reused(self, get):
        get.return_value = Response(200, {"result": "cached"})
        first = cached_json_get(
            "https://example.test/data",
            namespace="test",
            ttl=60,
            include_cache_status=True,
        )
        second = cached_json_get(
            "https://example.test/data",
            namespace="test",
            ttl=60,
            include_cache_status=True,
        )
        self.assertEqual(first, ({"result": "cached"}, False))
        self.assertEqual(second, ({"result": "cached"}, True))
        get.assert_called_once()

    def test_simultaneous_cache_misses_share_one_external_request(self):
        callers_ready = Barrier(3)
        request_started = Event()
        release_request = Event()
        results = []
        failures = []
        calls = 0

        def request_get(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            request_started.set()
            release_request.wait(2)
            return Response(200, {"result": "shared"})

        def load():
            try:
                callers_ready.wait()
                results.append(cached_json_get(
                    "https://example.test/simultaneous",
                    namespace="simultaneous-test",
                    ttl=60,
                    include_cache_status=True,
                    request_get=request_get,
                ))
            except (
                RuntimeError,
                ValueError,
                requests.RequestException,
                sqlite3.Error,
            ) as exc:  # pragma: no cover - asserted below
                failures.append(exc)

        threads = [Thread(target=load), Thread(target=load)]
        for thread in threads:
            thread.start()
        callers_ready.wait()
        self.assertTrue(request_started.wait(1))
        time.sleep(0.05)
        release_request.set()
        for thread in threads:
            thread.join(2)

        self.assertFalse(failures)
        self.assertEqual(calls, 1)
        self.assertCountEqual(
            results,
            [({"result": "shared"}, False), ({"result": "shared"}, True)],
        )
        self.assertEqual(api_cache._request_locks, {})

    def test_failed_coalesced_cache_misses_clean_up_and_allow_a_later_request(self):
        url = "https://example.test/coalesced-failure"
        namespace = "coalesced-failure-test"
        key = cache_key(namespace, url)
        callers_ready = Barrier(3)
        request_started = Event()
        release_request = Event()
        results = []
        failures = []
        calls = 0

        def failing_request(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            request_started.set()
            release_request.wait(2)
            raise requests.ConnectionError("coalesced upstream failure")

        def load():
            try:
                callers_ready.wait()
                results.append(cached_json_get(
                    url,
                    namespace=namespace,
                    ttl=60,
                    request_get=failing_request,
                ))
            except requests.ConnectionError as exc:
                failures.append(exc)

        threads = [Thread(target=load), Thread(target=load)]
        for thread in threads:
            thread.start()
        callers_ready.wait()
        self.assertTrue(request_started.wait(1))

        try:
            deadline = time.monotonic() + 1
            waiting_users = 0
            while time.monotonic() < deadline:
                with api_cache._request_locks_lock:
                    entry = api_cache._request_locks.get(key)
                    waiting_users = entry["users"] if entry else 0
                if waiting_users == 2:
                    break
                time.sleep(0.01)
            self.assertEqual(waiting_users, 2)
        finally:
            release_request.set()

        for thread in threads:
            thread.join(2)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(results, [])
        self.assertEqual(calls, 2)
        self.assertEqual(len(failures), 2)
        self.assertTrue(all(
            str(failure) == "coalesced upstream failure" for failure in failures
        ))
        self.assertEqual(api_cache._request_locks, {})

        recovered = cached_json_get(
            url,
            namespace=namespace,
            ttl=60,
            request_get=lambda *_args, **_kwargs: Response(
                200, {"result": "recovered"}
            ),
        )

        self.assertEqual(recovered, {"result": "recovered"})
        self.assertEqual(api_cache._request_locks, {})

    @patch("backend.api_cache.requests.get")
    def test_cache_only_miss_does_not_call_external_service(self, get):
        result = cached_json_get(
            "https://example.test/not-cached",
            namespace="cache-only-test",
            ttl=60,
            cache_only=True,
        )

        self.assertIsNone(result)
        get.assert_not_called()

    @patch("backend.api_cache.requests.get")
    def test_live_response_can_be_staged_without_changing_cache(self, get):
        url = "https://example.test/staged"
        key = cache_key("staged-test", url)
        get.return_value = Response(200, {"result": "staged"})

        result = cached_json_get(
            url,
            namespace="staged-test",
            ttl=60,
            force_refresh=True,
            cache_response=False,
        )

        self.assertEqual(result, {"result": "staged"})
        with cache_db() as connection:
            cached = connection.execute(
                "SELECT 1 FROM api_cache WHERE cache_key = ?",
                (key,),
            ).fetchone()
        self.assertIsNone(cached)

    @patch("backend.api_cache.requests.get")
    def test_cache_is_rechecked_after_waiting_for_request_slot(self, get):
        url = "https://example.test/coalesced"
        key = cache_key("coalesced-test", url)

        def populate_cache():
            with cache_db() as connection:
                connection.execute(
                    "INSERT INTO api_cache (cache_key, value, expires_at) VALUES (?, ?, ?)",
                    (key, json.dumps({"result": "from-click"}), time.time() + 60),
                )

        result = cached_json_get(
            url,
            namespace="coalesced-test",
            ttl=60,
            before_request=populate_cache,
        )

        self.assertEqual(result, {"result": "from-click"})
        get.assert_not_called()

    @patch("backend.api_cache.time.sleep")
    @patch("backend.api_cache.requests.get")
    def test_transient_statuses_retry_with_exponential_backoff(self, get, sleep):
        get.side_effect = [
            Response(503),
            Response(429),
            Response(200, {"result": "recovered"}),
        ]
        before_request = Mock()
        with self.assertLogs("backend.api_cache", level="WARNING"):
            result = cached_json_get(
                "https://example.test/transient",
                namespace="retry-test",
                ttl=60,
                before_request=before_request,
                retry_statuses={429, 503},
                max_attempts=3,
                retry_backoff=1.0,
            )
        self.assertEqual(result, {"result": "recovered"})
        self.assertEqual(get.call_count, 3)
        self.assertEqual(before_request.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1.0, 2.0])

    @patch("backend.api_cache.time.sleep")
    @patch("backend.api_cache.requests.get")
    def test_transient_connection_errors_retry_without_user_action(self, get, sleep):
        get.side_effect = [
            requests.Timeout("slow upstream"),
            Response(200, {"result": "recovered"}),
        ]
        with self.assertLogs("backend.api_cache", level="WARNING"):
            result = cached_json_get(
                "https://example.test/timeout",
                namespace="timeout-retry-test",
                ttl=60,
                retry_exceptions=(requests.Timeout, requests.ConnectionError),
                max_attempts=3,
            )
        self.assertEqual(result, {"result": "recovered"})
        self.assertEqual(get.call_count, 2)
        sleep.assert_called_once_with(1.0)

    @patch("backend.api_cache.time.sleep")
    @patch("backend.api_cache.requests.get")
    def test_retry_logs_never_include_prepared_urls_or_exception_text(
        self, get, sleep
    ):
        api_key = "sentinel-lastfm-api-key"
        username = "sentinel-linked-username"
        private_scope = "lastfm:user:sentinel-pseudonymous-handle-hash"
        cache_url = "https://example.test/private"
        secret_url = (
            f"{cache_url}?api_key={api_key}&user={username}"
        )
        request_error = requests.ConnectionError(f"failed request for {secret_url}")
        request_error.request = requests.Request("GET", secret_url).prepare()
        get.side_effect = [
            request_error,
            Response(503),
            Response(200, {"result": "recovered"}),
        ]

        with self.assertLogs("backend.api_cache", level="WARNING") as logs:
            result = cached_json_get(
                cache_url,
                namespace=private_scope,
                ttl=60,
                retry_statuses={503},
                retry_exceptions=(requests.ConnectionError,),
                max_attempts=3,
            )

        rendered = "\n".join(logs.output)
        self.assertEqual(result, {"result": "recovered"})
        self.assertIn("ConnectionError", rendered)
        self.assertIn("HTTP 503", rendered)
        for private_value in (
            api_key,
            username,
            private_scope,
            "sentinel-pseudonymous-handle-hash",
            secret_url,
            "failed request",
        ):
            self.assertNotIn(private_value, rendered)
        self.assertEqual(get.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    @patch("backend.api_cache.requests.get")
    def test_force_refresh_replaces_a_fresh_cached_response(self, get):
        get.side_effect = [
            Response(200, {"result": "original"}),
            Response(200, {"result": "refreshed"}),
        ]
        url = "https://example.test/refresh"
        original = cached_json_get(url, namespace="refresh-test", ttl=60)
        refreshed = cached_json_get(
            url, namespace="refresh-test", ttl=60, force_refresh=True
        )
        cached = cached_json_get(url, namespace="refresh-test", ttl=60)

        self.assertEqual(original, {"result": "original"})
        self.assertEqual(refreshed, {"result": "refreshed"})
        self.assertEqual(cached, {"result": "refreshed"})
        self.assertEqual(get.call_count, 2)


class CacheMemoTests(DatabaseTestCase):
    @patch("backend.cache_memo.time.monotonic", return_value=100.0)
    def test_parsed_document_is_shared_across_requests(self, monotonic):
        build = Mock(return_value={"artists": [1, 2, 3]})

        with self.app.test_request_context():
            first = cache_memo.memoized_document("library", build)
            self.assertIs(first, cache_memo.memoized_document("library", build))
        with self.app.test_request_context():
            second = cache_memo.memoized_document("library", build)

        self.assertIs(first, second)
        build.assert_called_once_with()

    @patch("backend.cache_memo.time.monotonic", return_value=100.0)
    def test_expired_shared_document_is_rebuilt(self, monotonic):
        build = Mock(side_effect=[{"version": 1}, {"version": 2}])

        with self.app.test_request_context():
            first = cache_memo.memoized_document("library", build)
        monotonic.return_value = 131.0
        with self.app.test_request_context():
            second = cache_memo.memoized_document("library", build)

        self.assertEqual(first["version"], 1)
        self.assertEqual(second["version"], 2)
        self.assertEqual(build.call_count, 2)

    @patch("backend.cache_memo.time.monotonic", return_value=100.0)
    def test_invalidation_rebuilds_the_next_request(self, monotonic):
        build = Mock(side_effect=[{"version": 1}, {"version": 2}])

        with self.app.test_request_context():
            cache_memo.memoized_document("library", build)
        cache_memo.invalidate_document("library")
        with self.app.test_request_context():
            refreshed = cache_memo.memoized_document("library", build)

        self.assertEqual(refreshed["version"], 2)
        self.assertEqual(build.call_count, 2)

    @patch("backend.cache_memo.time.monotonic", return_value=100.0)
    def test_invalidation_during_build_does_not_republish_stale_value(self, monotonic):
        calls = 0

        def build():
            nonlocal calls
            calls += 1
            if calls == 1:
                cache_memo.invalidate_document("library")
            return {"version": calls}

        with self.app.test_request_context():
            first = cache_memo.memoized_document("library", build)
        with self.app.test_request_context():
            second = cache_memo.memoized_document("library", build)

        self.assertEqual(first["version"], 1)
        self.assertEqual(second["version"], 2)
        self.assertEqual(calls, 2)


class LidarrRequestTests(DatabaseTestCase):
    artist_mbid = "11111111-1111-1111-1111-111111111111"
    album_mbid = "22222222-2222-2222-2222-222222222222"
    defaults = {
        "rootFolderPath": "/music",
        "qualityProfileId": 1,
        "metadataProfileId": 2,
        "monitor": "all",
        "monitorNewItems": "all",
        "tags": [3],
        "searchForMissingAlbums": True,
    }

    def lidarr_config(self):
        return {"defaults": self.defaults}

    def request_history(self):
        with db() as connection:
            return connection.execute(
                "SELECT kind, mbid, name FROM request_history ORDER BY id"
            ).fetchall()

    @patch("backend.routes.requests._anime_request_context")
    def test_anime_context_does_not_reflect_unexpected_exception(self, context):
        context.side_effect = ValueError("sentinel-secret-provider-detail")
        response = self.client.post(
            "/api/request/release-group",
            json={"mbid": self.album_mbid, "animeSlug": "anime"},
            headers={"X-CSRF-Token": self.register()},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json(), {"error": "Invalid anime request context."})

    def test_already_queued_release_group_is_recorded_for_the_current_user(self):
        admin_csrf = self.register()
        with db() as connection:
            admin_id = connection.execute(
                "SELECT id FROM users WHERE username = 'test-user'"
            ).fetchone()["id"]
            listener_id = connection.execute(
                "INSERT INTO users (username, password_hash, role, created_at) "
                "VALUES (?, ?, 'user', ?)",
                (
                    "queue-listener",
                    generate_password_hash("listener-password"),
                    time.time(),
                ),
            ).lastrowid
        enqueue_lidarr_search(
            admin_id,
            self.album_mbid,
            33,
            44,
            "Already Queued Album",
        )
        self.client.post(
            "/api/auth/logout",
            headers={"X-CSRF-Token": admin_csrf},
        )
        login = self.client.post(
            "/api/auth/login",
            json={
                "username": "queue-listener",
                "password": "listener-password",
            },
        )

        response = self.client.post(
            "/api/request/release-group",
            json={
                "mbid": self.album_mbid,
                "animeSlug": "naruto",
                "animeName": "Naruto",
                "themeId": 1477,
                "themeLabel": "Opening 2",
                "songId": 1477,
                "songTitle": "Haruka Kanata",
            },
            headers={"X-CSRF-Token": login.get_json()["csrfToken"]},
        )

        self.assertEqual(response.status_code, 202)
        with db() as connection:
            listener_history = connection.execute(
                "SELECT name, anime_slug, anime_name, theme_id, theme_label, "
                "song_id, song_title FROM request_history "
                "WHERE user_id = ? AND mbid = ?",
                (listener_id, self.album_mbid),
            ).fetchall()
        self.assertEqual(
            [row["name"] for row in listener_history],
            ["Already Queued Album"],
        )
        self.assertEqual(listener_history[0]["anime_slug"], "naruto")
        self.assertEqual(listener_history[0]["theme_id"], 1477)
        self.assertEqual(listener_history[0]["song_title"], "Haruka Kanata")

    @patch("backend.routes.requests.lidarr.lookup_album")
    @patch("backend.routes.requests.get_service", return_value=None)
    def test_release_group_request_reports_unconfigured_lidarr_as_json(
        self, get_service, lookup_album
    ):
        response = self.client.post(
            "/api/request/release-group",
            json={"mbid": self.album_mbid},
            headers={"X-CSRF-Token": self.register()},
        )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json(), {"error": "Lidarr is not configured."})
        get_service.assert_called_once_with("lidarr")
        lookup_album.assert_not_called()

    @patch("backend.routes.requests.lidarr.update_artists")
    @patch("backend.routes.requests.lidarr.add_artist")
    @patch("backend.routes.requests.lidarr.lookup_artist")
    @patch("backend.routes.requests.get_service")
    def test_artist_request_applies_defaults_and_records_history(
        self, get_service, lookup_artist, add_artist, update_artists
    ):
        get_service.return_value = self.lidarr_config()
        lookup_artist.return_value = Response(payload=[{
            "artistName": "Test Artist",
            "foreignArtistId": self.artist_mbid,
        }])
        add_artist.return_value = Response(201, {"id": 42, "artistName": "Test Artist"})
        update_artists.return_value = Response(202)

        response = self.client.post(
            "/api/request",
            json={"mbid": self.artist_mbid},
            headers={"X-CSRF-Token": self.register()},
        )

        self.assertEqual(response.status_code, 201)
        submitted = add_artist.call_args.args[0]
        self.assertEqual(submitted["rootFolderPath"], "/music")
        self.assertEqual(submitted["qualityProfileId"], 1)
        self.assertEqual(submitted["metadataProfileId"], 2)
        self.assertEqual(submitted["tags"], [3])
        update_artists.assert_called_once_with({
            "artistIds": [42],
            "monitorNewItems": "all",
        })
        history = self.request_history()
        self.assertEqual((history[0]["kind"], history[0]["mbid"]), ("artist", self.artist_mbid))

    @patch("backend.routes.requests.notifications.queue_admin_request")
    @patch("backend.routes.requests.lidarr_search_worker.request_work")
    @patch("backend.routes.requests.enqueue_lidarr_search")
    @patch("backend.routes.requests.lidarr.start_command")
    @patch("backend.routes.requests.lidarr.add_album")
    @patch("backend.routes.requests.lidarr.lookup_album")
    @patch("backend.routes.requests.get_service")
    def test_new_album_persists_refresh_then_search_job(
        self, get_service, lookup_album, add_album, start_command,
        enqueue_search, request_work, queue_admin_request
    ):
        get_service.return_value = self.lidarr_config()
        lookup_album.return_value = Response(payload=[{
            "title": "Test Album",
            "foreignAlbumId": self.album_mbid,
            "artist": {"artistName": "Test Artist"},
            "albumType": "Album",
            "releaseDate": "2020-03-18T00:00:00Z",
        }])
        add_album.return_value = Response(201, {
            "id": 33,
            "artistId": 44,
            "title": "Test Album",
        })
        enqueue_search.return_value = True

        response = self.client.post(
            "/api/request/release-group",
            json={
                "mbid": self.album_mbid,
                "animeSlug": "naruto",
                "animeName": "Naruto",
                "themeId": 1477,
                "themeLabel": "Opening 2",
                "songId": 1477,
                "songTitle": "Haruka Kanata",
            },
            headers={"X-CSRF-Token": self.register()},
        )

        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.get_json()["pending"])
        self.assertFalse(add_album.call_args.args[0]["addOptions"]["searchForNewAlbum"])
        user_id, mbid, album_id, artist_id, title = enqueue_search.call_args.args
        self.assertIsInstance(user_id, int)
        self.assertEqual((mbid, album_id, artist_id, title), (
            self.album_mbid, 33, 44, "Test Album",
        ))
        self.assertEqual(enqueue_search.call_args.kwargs, {
            "artist_name": "Test Artist",
            "release_type": "Album",
            "release_date": "2020-03-18",
            "anime_slug": "naruto",
            "anime_name": "Naruto",
            "theme_id": 1477,
            "theme_label": "Opening 2",
            "song_id": 1477,
            "song_title": "Haruka Kanata",
        })
        queue_admin_request.assert_called_once_with(
            user_id,
            "test-user",
            self.album_mbid,
            "Test Album",
            "Test Artist",
        )
        self.assertEqual(response.get_json()["refreshType"], "album")
        request_work.assert_called_once_with()
        start_command.assert_not_called()

    @patch("backend.routes.requests.lidarr_search_worker.request_work")
    @patch("backend.routes.requests.enqueue_lidarr_search")
    @patch("backend.routes.requests.lidarr.albums_by_release_group")
    @patch("backend.routes.requests.lidarr.add_album")
    @patch("backend.routes.requests.lidarr.lookup_album")
    @patch("backend.routes.requests.get_service")
    def test_existing_incomplete_album_refreshes_before_search(
        self, get_service, lookup_album, add_album, albums_by_release_group,
        enqueue_search, request_work
    ):
        get_service.return_value = self.lidarr_config()
        lookup_album.return_value = Response(payload=[{
            "title": "Existing Album",
            "foreignAlbumId": self.album_mbid,
            "artist": {},
        }])
        add_album.return_value = Response(400, text="Album already exists")
        albums_by_release_group.return_value = Response(payload=[{
            "id": 77,
            "artistId": 44,
            "foreignAlbumId": self.album_mbid,
            "title": "Existing Album",
            "statistics": {"totalTrackCount": 10, "trackFileCount": 2},
        }])
        enqueue_search.return_value = True

        response = self.client.post(
            "/api/request/release-group",
            json={"mbid": self.album_mbid},
            headers={"X-CSRF-Token": self.register()},
        )

        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.get_json()["pending"])
        enqueue_search.assert_called_once()
        self.assertEqual(
            enqueue_search.call_args.args[1:],
            (self.album_mbid, 77, 44, "Existing Album"),
        )
        request_work.assert_called_once_with()

    @patch("backend.routes.requests.record_request")
    @patch("backend.routes.requests.lidarr.albums_by_release_group")
    @patch("backend.routes.requests.lidarr.add_album")
    @patch("backend.routes.requests.lidarr.lookup_album")
    @patch("backend.routes.requests.get_service")
    def test_fully_available_album_history_preserves_anime_context(
        self, get_service, lookup_album, add_album, albums_by_release_group,
        record_history
    ):
        get_service.return_value = self.lidarr_config()
        lookup_album.return_value = Response(payload=[{
            "title": "Theme Single",
            "foreignAlbumId": self.album_mbid,
            "artist": {"artistName": "Theme Artist"},
            "albumType": "Single",
            "releaseDate": "2026-01-02T00:00:00Z",
        }])
        add_album.return_value = Response(400, text="Album already exists")
        albums_by_release_group.return_value = Response(payload=[{
            "id": 77,
            "artistId": 44,
            "foreignAlbumId": self.album_mbid,
            "title": "Theme Single",
            "artist": {"artistName": "Theme Artist"},
            "statistics": {"totalTrackCount": 3, "trackFileCount": 3},
        }])

        response = self.client.post(
            "/api/request/release-group",
            json={
                "mbid": self.album_mbid,
                "animeSlug": "naruto",
                "animeName": "Naruto",
                "themeId": 1477,
                "themeLabel": "Opening 2",
                "songId": 1477,
                "songTitle": "Haruka Kanata",
            },
            headers={"X-CSRF-Token": self.register()},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["alreadyExists"])
        self.assertEqual(record_history.call_args.args[1:4], (
            "release-group", self.album_mbid, "Theme Single"
        ))
        self.assertEqual(record_history.call_args.kwargs, {
            "artist_name": "Theme Artist",
            "release_type": "Single",
            "release_date": "2026-01-02",
            "anime_slug": "naruto",
            "anime_name": "Naruto",
            "theme_id": 1477,
            "theme_label": "Opening 2",
            "song_id": 1477,
            "song_title": "Haruka Kanata",
        })

    @patch("backend.routes.requests.lidarr_search_worker.request_work")
    @patch("backend.routes.requests.enqueue_lidarr_search")
    @patch("backend.routes.requests.lidarr.add_album")
    @patch("backend.routes.requests.lidarr.lookup_album")
    @patch("backend.routes.requests.get_service")
    def test_new_album_uses_album_refresh_regardless_of_artist_state(
        self, get_service, lookup_album, add_album, enqueue_search, request_work
    ):
        get_service.return_value = self.lidarr_config()
        lookup_album.return_value = Response(payload=[{
            "title": "New Album",
            "foreignAlbumId": self.album_mbid,
            "artist": {
                "id": 44,
                "artistName": "Existing Artist",
            },
        }])
        add_album.return_value = Response(201, {
            "id": 33,
            "artistId": 44,
            "title": "New Album",
        })
        enqueue_search.return_value = True

        response = self.client.post(
            "/api/request/release-group",
            json={
                "mbid": self.album_mbid,
                "artistMbid": self.artist_mbid,
                "artistInLidarr": False,
            },
            headers={
                "X-CSRF-Token": self.register(),
                "Referer": f"http://melodarr.test/artists/{self.artist_mbid}",
            },
        )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.get_json()["refreshType"], "album")
        self.assertEqual(
            enqueue_search.call_args.args[1:],
            (self.album_mbid, 33, 44, "New Album"),
        )
        request_work.assert_called_once_with()


class AccountSettingsAccessTests(DatabaseTestCase):
    def add_target_user(self):
        with db() as connection:
            cursor = connection.execute(
                "INSERT INTO users "
                "(username, password_hash, role, listenbrainz_username, "
                "lastfm_username, plex_id, plex_username, plex_email, "
                "plex_avatar, created_at) "
                "VALUES (?, ?, 'user', ?, ?, ?, ?, ?, ?, ?)",
                (
                    "target-user",
                    generate_password_hash("target-old-password"),
                    "old-listenbrainz",
                    "old-lastfm",
                    None,
                    "Plex Listener",
                    "listener@example.com",
                    "https://plex.example/listener.jpg",
                    100,
                ),
            )
        return cursor.lastrowid

    @patch("backend.routes.account.recommendation_worker.request_refresh")
    @patch("backend.routes.account.lastfm.get")
    @patch("backend.routes.account.listenbrainz.user_listen_count")
    def test_admin_settings_requests_read_and_update_the_target_account(
        self,
        listen_count,
        lastfm_get,
        request_refresh,
    ):
        csrf = self.register()
        target_id = self.add_target_user()
        save_service("lastfm", {"apiKey": "shared-lastfm-key"})
        listen_count.return_value = Response(200, {"payload": {"count": 10}})

        settings = self.client.get(
            "/api/account/settings?username=Plex%20Listener"
        )
        general = self.client.post(
            "/api/account/general?username=Plex%20Listener",
            json={
                "username": "renamed-target",
                "password": "target-new-password",
            },
            headers={"X-CSRF-Token": csrf},
        )
        listenbrainz = self.client.post(
            "/api/account/settings?username=renamed-target",
            json={"username": "new-listenbrainz"},
            headers={"X-CSRF-Token": csrf},
        )
        lastfm = self.client.post(
            "/api/account/lastfm?username=renamed-target",
            json={"username": "new-lastfm"},
            headers={"X-CSRF-Token": csrf},
        )

        self.assertEqual(settings.status_code, 200)
        self.assertEqual(settings.get_json()["username"], "target-user")
        self.assertEqual(
            settings.get_json()["listenbrainzUsername"],
            "old-listenbrainz",
        )
        self.assertEqual(settings.get_json()["lastfmUsername"], "old-lastfm")
        self.assertEqual(settings.get_json()["plexUsername"], "Plex Listener")
        self.assertNotIn("password_hash", settings.get_data(as_text=True))
        self.assertEqual(general.status_code, 200)
        self.assertEqual(general.get_json()["username"], "renamed-target")
        self.assertEqual(listenbrainz.status_code, 200)
        self.assertEqual(lastfm.status_code, 200)
        listen_count.assert_called_once_with("new-listenbrainz")
        lastfm_get.assert_called_once_with(
            "user.getinfo",
            "new-lastfm",
            "shared-lastfm-key",
        )
        self.assertEqual(request_refresh.call_count, 2)

        with db() as connection:
            admin = connection.execute(
                "SELECT username, listenbrainz_username, lastfm_username "
                "FROM users WHERE username = 'test-user'"
            ).fetchone()
            target = connection.execute(
                "SELECT username, password_hash, listenbrainz_username, "
                "lastfm_username FROM users WHERE id = ?",
                (target_id,),
            ).fetchone()
        self.assertEqual(admin["username"], "test-user")
        self.assertIsNone(admin["listenbrainz_username"])
        self.assertIsNone(admin["lastfm_username"])
        self.assertEqual(target["username"], "renamed-target")
        self.assertTrue(
            check_password_hash(
                target["password_hash"],
                "target-new-password",
            )
        )
        self.assertEqual(
            target["listenbrainz_username"],
            "new-listenbrainz",
        )
        self.assertEqual(target["lastfm_username"], "new-lastfm")

    def test_non_admin_cannot_read_or_update_another_users_settings(self):
        admin_csrf = self.register()
        target_id = self.add_target_user()
        with db() as connection:
            connection.execute(
                "INSERT INTO users "
                "(username, password_hash, role, created_at) "
                "VALUES (?, ?, 'user', ?)",
                (
                    "ordinary-user",
                    generate_password_hash("ordinary-password"),
                    200,
                ),
            )
        self.client.post(
            "/api/auth/logout",
            headers={"X-CSRF-Token": admin_csrf},
        )
        login = self.client.post(
            "/api/auth/login",
            json={
                "username": "ordinary-user",
                "password": "ordinary-password",
            },
        )
        csrf = login.get_json()["csrfToken"]

        settings = self.client.get(
            "/api/account/settings?username=target-user"
        )
        general = self.client.post(
            "/api/account/general?username=target-user",
            json={"username": "stolen-name", "password": ""},
            headers={"X-CSRF-Token": csrf},
        )
        linked = self.client.post(
            "/api/account/settings?username=target-user",
            json={"username": ""},
            headers={"X-CSRF-Token": csrf},
        )

        self.assertEqual(settings.status_code, 403)
        self.assertEqual(general.status_code, 403)
        self.assertEqual(linked.status_code, 403)
        with db() as connection:
            target = connection.execute(
                "SELECT username, listenbrainz_username FROM users WHERE id = ?",
                (target_id,),
            ).fetchone()
        self.assertEqual(target["username"], "target-user")
        self.assertEqual(
            target["listenbrainz_username"],
            "old-listenbrainz",
        )

    @patch("backend.routes.auth.plex_auth.get_resources")
    @patch("backend.routes.auth.plex_auth.get_account")
    @patch("backend.routes.auth.plex_auth.poll_pin")
    @patch("backend.routes.auth.plex_auth.create_pin")
    def test_admin_can_link_plex_to_the_target_account(
        self,
        create_pin,
        poll_pin,
        get_account,
        get_resources,
    ):
        csrf = self.register()
        target_id = self.add_target_user()
        save_service("plex", {
            "url": "http://plex:32400",
            "token": "server-owner-token",
            "machineIdentifier": "server-1",
        })
        create_pin.return_value = {
            "id": 987,
            "authorizationUrl": "https://app.plex.tv/auth/#!?code=TARGET",
            "expiresAt": time.time() + 600,
        }
        poll_pin.return_value = "target-token"
        get_account.return_value = {
            "id": "target-plex-id",
            "username": "target-plex-user",
            "email": "target-plex@example.com",
            "thumb": "https://plex.example/target.jpg",
        }
        get_resources.return_value = [{
            "clientIdentifier": "server-1",
        }]

        started = self.client.post(
            "/api/auth/plex/start",
            json={"purpose": "link", "username": "target-user"},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(started.status_code, 201)
        flow_token = started.get_json()["flowToken"]
        with db() as connection:
            flow = connection.execute(
                "SELECT user_id FROM plex_auth_flows"
            ).fetchone()
        self.assertEqual(flow["user_id"], target_id)

        linked = self.client.post(
            "/api/auth/plex/poll",
            json={"flowToken": flow_token},
            headers={"X-CSRF-Token": csrf},
        )

        self.assertEqual(linked.status_code, 200)
        self.assertEqual(
            linked.get_json()["plexUsername"],
            "target-plex-user",
        )
        with db() as connection:
            admin = connection.execute(
                "SELECT plex_id FROM users WHERE username = 'test-user'"
            ).fetchone()
            target = connection.execute(
                "SELECT plex_id, plex_username FROM users WHERE id = ?",
                (target_id,),
            ).fetchone()
        self.assertIsNone(admin["plex_id"])
        self.assertEqual(target["plex_id"], "target-plex-id")
        self.assertEqual(target["plex_username"], "target-plex-user")


class AccountProfileTests(DatabaseTestCase):
    release_group_mbid = "33333333-3333-3333-3333-333333333333"

    @patch("backend.routes.account.musicbrainz.get")
    @patch("backend.routes.account.plex.cached_library_index")
    @patch("backend.routes.account.get_service")
    def test_profile_returns_stored_release_metadata_and_plex_availability(
        self, get_service, cached_library_index, musicbrainz_get
    ):
        self.register()
        with db() as connection:
            user_id = connection.execute(
                "SELECT id FROM users WHERE username = 'test-user'"
            ).fetchone()["id"]
            connection.execute(
                "INSERT INTO request_history "
                "(user_id, kind, mbid, name, artist_name, release_type, "
                "release_date, anime_slug, anime_name, theme_id, theme_label, "
                "song_id, song_title, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    user_id,
                    "release-group",
                    self.release_group_mbid,
                    "SKZ2020",
                    "Stray Kids",
                    "Album",
                    "2020-03-18",
                    "naruto",
                    "Naruto",
                    1477,
                    "Opening 2",
                    1477,
                    "Haruka Kanata",
                    1000,
                ),
            )
        anime_theme_links.sync_anime_theme_mapping(
            {"slug": "other-anime", "name": "Other Anime"},
            {
                "id": 8,
                "label": "Ending 1",
                "type": "ED",
                "sequence": 1,
                "song": {"id": 9, "title": "Other Song"},
            },
            {
                "state": "resolved",
                "releaseGroups": [{"id": self.release_group_mbid}],
            },
        )
        get_service.return_value = {"url": "http://plex", "token": "token"}
        cached_library_index.return_value = {
            "artistsByMbid": {},
            "releaseGroupsByMbid": {
                self.release_group_mbid: [{
                    "url": "https://app.plex.tv/album",
                    "plexampUrl": "https://listen.plex.tv/album/example",
                }],
            },
        }

        response = self.client.get("/api/account/profile")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["user"], {
            "id": user_id,
            "username": "test-user",
            "localUsername": "test-user",
            "userType": "local",
            "role": "admin",
            "plexUsername": "",
            "plexEmail": "",
            "plexAvatar": "",
        })
        item = payload["requests"]["release-group"][0]
        self.assertEqual(item["artist_name"], "Stray Kids")
        self.assertEqual(item["release_type"], "Album")
        self.assertEqual(item["release_date"], "2020-03-18")
        self.assertEqual(item["anime_slug"], "naruto")
        self.assertEqual(item["anime_name"], "Naruto")
        self.assertEqual(item["theme_id"], 1477)
        self.assertEqual(item["theme_label"], "Opening 2")
        self.assertEqual(item["song_id"], 1477)
        self.assertEqual(item["song_title"], "Haruka Kanata")
        self.assertEqual(item["animePath"], "/anime/naruto#theme-1477")
        self.assertEqual(
            [link["animeSlug"] for link in item["animeThemes"]],
            ["naruto"],
        )
        self.assertTrue(item["availableInPlex"])
        self.assertEqual(item["plexUrl"], "https://app.plex.tv/album")
        self.assertEqual(
            item["plexampUrl"],
            "https://listen.plex.tv/album/example",
        )
        musicbrainz_get.assert_not_called()

    def test_profile_release_history_includes_private_safe_download_lifecycle(self):
        self.register()
        with db() as connection:
            user_id = connection.execute(
                "SELECT id FROM users WHERE username = 'test-user'"
            ).fetchone()["id"]
            connection.execute(
                "INSERT INTO request_history (user_id, kind, mbid, name, created_at) "
                "VALUES (?, 'release-group', 'profile-group', 'Profile Album', 100)",
                (user_id,),
            )
        with (
            patch("backend.routes.account.get_service", return_value=None),
            patch("backend.routes.account.lidarr.cached_library_availability", return_value={}),
            patch("backend.routes.account.lidarr.cached_download_availability", return_value={
                "profile-group": {"progress": 20,
                                  "status": {"title": "private nested status"},
                                  "trackedDownloadStatus": {"id": "private nested tracked status"},
                                  "trackedDownloadState": ["private nested state"],
                                  "timeLeft": ("private nested time",),
                                  "estimatedCompletionTime": {"path": "private nested eta"},
                                  "downloadId": 4, "title": "private", "path": "private"},
            }),
            patch("backend.routes.account.pending_lidarr_search_mbids", return_value={"profile-group"}),
        ):
            response = self.client.get("/api/account/profile")

        self.assertEqual(response.status_code, 200)
        item = response.get_json()["requests"]["release-group"][0]
        self.assertEqual(item["requestStatus"], "downloading")
        self.assertEqual(item["downloadStatus"], {"progress": 20})
        serialized = response.get_data(as_text=True)
        for forbidden in ("downloadId", "private", "path", "nested"):
            self.assertNotIn(forbidden, serialized)

    @patch("backend.routes.account.musicbrainz.get")
    @patch("backend.routes.account.get_service", return_value=None)
    def test_profile_backfills_legacy_rows_from_cached_musicbrainz_only(
        self, get_service, musicbrainz_get
    ):
        self.register()
        with db() as connection:
            user_id = connection.execute(
                "SELECT id FROM users WHERE username = 'test-user'"
            ).fetchone()["id"]
            connection.execute(
                "INSERT INTO request_history "
                "(user_id, kind, mbid, name, created_at) VALUES (?, ?, ?, ?, ?)",
                (
                    user_id,
                    "release-group",
                    self.release_group_mbid,
                    "Legacy Album",
                    1000,
                ),
            )
        anime_theme_links.sync_anime_theme_mapping(
            {"slug": "legacy-anime", "name": "Legacy Anime"},
            {
                "id": 10,
                "label": "Opening 1",
                "type": "OP",
                "sequence": 1,
                "song": {"id": 11, "title": "Legacy Song"},
            },
            {
                "state": "resolved",
                "releaseGroups": [{"id": self.release_group_mbid}],
            },
        )
        musicbrainz_get.return_value = {
            "artist-credit": [{"name": "Legacy Artist"}],
            "primary-type": "EP",
            "first-release-date": "2019-04-05",
        }

        response = self.client.get("/api/account/profile")

        self.assertEqual(response.status_code, 200)
        item = response.get_json()["requests"]["release-group"][0]
        self.assertEqual(item["artist_name"], "Legacy Artist")
        self.assertEqual(item["release_type"], "EP")
        self.assertEqual(item["release_date"], "2019-04-05")
        self.assertEqual(item["animePath"], "/anime/legacy-anime#theme-10")
        self.assertEqual(item["animeThemes"][0]["songTitle"], "Legacy Song")
        self.assertFalse(item["availableInPlex"])
        self.assertTrue(musicbrainz_get.call_args.kwargs["cache_only"])

    @patch("backend.routes.account.get_service", return_value=None)
    def test_profile_request_history_paginates_at_100_items(
        self, get_service
    ):
        self.register()
        with db() as connection:
            user_id = connection.execute(
                "SELECT id FROM users WHERE username = 'test-user'"
            ).fetchone()["id"]
            connection.executemany(
                "INSERT INTO request_history "
                "(user_id, kind, mbid, name, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    (
                        user_id,
                        "artist" if index % 2 else "release-group",
                        f"request-{index}",
                        f"Request {index}",
                        index,
                    )
                    for index in range(205)
                ],
            )

        first = self.client.get("/api/account/profile").get_json()
        second = self.client.get("/api/account/profile?page=2").get_json()
        third = self.client.get("/api/account/profile?page=3").get_json()

        self.assertEqual(
            sum(len(items) for items in first["requests"].values()),
            100,
        )
        self.assertEqual(
            sum(len(items) for items in second["requests"].values()),
            100,
        )
        self.assertEqual(
            sum(len(items) for items in third["requests"].values()),
            5,
        )
        self.assertEqual(first["pagination"], {
            "page": 1,
            "pageSize": 100,
            "total": 205,
            "totalPages": 3,
        })
        self.assertEqual(second["pagination"]["page"], 2)
        self.assertEqual(third["pagination"]["page"], 3)
        first_names = {
            item["name"]
            for items in first["requests"].values()
            for item in items
        }
        third_names = {
            item["name"]
            for items in third["requests"].values()
            for item in items
        }
        self.assertIn("Request 204", first_names)
        self.assertNotIn("Request 104", first_names)
        self.assertEqual(
            third_names,
            {f"Request {index}" for index in range(5)},
        )

    def test_profile_request_history_rejects_invalid_pages(self):
        self.register()

        for page in ("0", "-1", "nope", "1.5", str(2 ** 100)):
            with self.subTest(page=page):
                response = self.client.get(
                    f"/api/account/profile?page={page}"
                )
                self.assertEqual(response.status_code, 400)
                self.assertIn("positive integer", response.get_json()["error"])

    @patch("backend.routes.account.get_service", return_value=None)
    def test_admin_can_view_another_profile_by_local_or_plex_username(
        self, get_service
    ):
        self.register()
        with db() as connection:
            cursor = connection.execute(
                "INSERT INTO users "
                "(username, password_hash, role, plex_id, plex_username, "
                "plex_email, plex_avatar, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "generated-plex-name",
                    generate_password_hash("listener-password"),
                    "user",
                    "private-plex-id",
                    "Plex Listener",
                    "plex@example.com",
                    "https://plex.example/avatar.jpg",
                    100,
                ),
            )
            user_id = cursor.lastrowid
            connection.execute(
                "INSERT INTO request_history "
                "(user_id, kind, mbid, name, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (user_id, "artist", "artist-1", "Requested Artist", 200),
            )

        by_local = self.client.get(
            "/api/account/profile?username=GENERATED-PLEX-NAME"
        )
        by_plex = self.client.get(
            "/api/account/profile?username=Plex%20Listener"
        )

        self.assertEqual(by_local.status_code, 200)
        self.assertEqual(by_plex.status_code, 200)
        payload = by_plex.get_json()
        self.assertEqual(payload["username"], "generated-plex-name")
        self.assertEqual(payload["user"], {
            "id": user_id,
            "username": "Plex Listener",
            "localUsername": "generated-plex-name",
            "userType": "plex",
            "role": "user",
            "plexUsername": "Plex Listener",
            "plexEmail": "plex@example.com",
            "plexAvatar": "https://plex.example/avatar.jpg",
        })
        self.assertEqual(
            payload["requests"]["artist"][0]["name"],
            "Requested Artist",
        )
        self.assertEqual(by_local.get_json()["user"], payload["user"])
        serialized = by_plex.get_data(as_text=True)
        self.assertNotIn("private-plex-id", serialized)
        self.assertNotIn("password_hash", serialized)

    def test_non_admin_cannot_view_another_users_profile(self):
        admin_csrf = self.register()
        with db() as connection:
            connection.executemany(
                "INSERT INTO users "
                "(username, password_hash, role, created_at) "
                "VALUES (?, ?, ?, ?)",
                [
                    (
                        "ordinary-user",
                        generate_password_hash("listener-password"),
                        "user",
                        100,
                    ),
                    (
                        "another-user",
                        generate_password_hash("listener-password"),
                        "user",
                        200,
                    ),
                ],
            )
        self.client.post(
            "/api/auth/logout",
            headers={"X-CSRF-Token": admin_csrf},
        )
        login = self.client.post(
            "/api/auth/login",
            json={
                "username": "ordinary-user",
                "password": "listener-password",
            },
        )
        self.assertEqual(login.status_code, 200)

        own_profile = self.client.get(
            "/api/account/profile?username=ORDINARY-USER"
        )
        other_profile = self.client.get(
            "/api/account/profile?username=another-user"
        )
        missing_profile = self.client.get(
            "/api/account/profile?username=missing-user"
        )

        self.assertEqual(own_profile.status_code, 200)
        self.assertEqual(
            own_profile.get_json()["user"]["localUsername"],
            "ordinary-user",
        )
        self.assertEqual(other_profile.status_code, 403)
        self.assertEqual(missing_profile.status_code, 403)

    def test_admin_profile_lookup_returns_not_found(self):
        self.register()

        response = self.client.get(
            "/api/account/profile?username=missing-user"
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()["error"], "User not found.")


class LidarrDownloadStatusTests(unittest.TestCase):
    @patch("backend.services.lidarr._request")
    def test_queue_fetches_paginated_records(self, request):
        request.side_effect = [
            Response(200, {"records": [{"albumId": 1}] * 1000, "totalRecords": 1001}),
            Response(200, {"records": [{"albumId": 2}], "totalRecords": 1001}),
        ]

        records = lidarr.queue_records({"url": "http://lidarr", "apiKey": "key"})

        self.assertEqual(len(records), 1001)
        self.assertEqual(request.call_args_list[0].kwargs["params"]["page"], 1)
        self.assertEqual(request.call_args_list[1].kwargs["params"]["page"], 2)

    @patch("backend.services.lidarr._request")
    def test_queue_continues_after_a_short_page_when_total_records_remain(self, request):
        request.side_effect = [
            Response(200, {"records": [{"albumId": 1}] * 100, "totalRecords": 101}),
            Response(200, {"records": [{"albumId": 2}], "totalRecords": 101}),
        ]

        records = lidarr.queue_records({"url": "http://lidarr", "apiKey": "key"})

        self.assertEqual(len(records), 101)
        self.assertEqual(request.call_args_list[1].kwargs["params"]["page"], 2)

    def test_duplicate_queue_records_use_weighted_progress_and_stable_state(self):
        snapshot = lidarr.normalize_download_snapshot([
            {"album": {"foreignAlbumId": "GROUP"}, "size": 100, "sizeleft": 25,
             "status": "Downloading", "timeleft": "2 minutes", "downloadId": 1,
             "title": "private", "indexer": "private", "downloadClient": "private",
             "outputPath": "private"},
            {"album": {"foreignAlbumId": "group"}, "size": 300, "sizeleft": 300,
             "status": "Queued", "timeleft": "3 minutes"},
        ])

        self.assertEqual(snapshot["albums"]["group"]["progress"], 19)
        self.assertEqual(snapshot["albums"]["group"]["status"], "queued")
        self.assertNotIn("id", snapshot["albums"]["group"])
        serialized = json.dumps(snapshot)
        for forbidden in ("downloadId", "private", "indexer", "downloadClient", "outputPath"):
            self.assertNotIn(forbidden, serialized)

    def test_equal_remaining_duplicates_choose_all_safe_fields_stably(self):
        records = [
            {"album": {"foreignAlbumId": "group"}, "size": 100, "sizeleft": 50,
             "status": "downloading", "trackedDownloadStatus": "alpha",
             "trackedDownloadState": "first", "timeleft": "2 minutes",
             "estimatedCompletionTime": "tomorrow"},
            {"album": {"foreignAlbumId": "group"}, "size": 100, "sizeleft": 50,
             "status": "downloading", "trackedDownloadStatus": "beta",
             "trackedDownloadState": "second", "timeleft": "2 minutes",
             "estimatedCompletionTime": "tomorrow"},
        ]

        forward = lidarr.normalize_download_snapshot(records)
        reversed_snapshot = lidarr.normalize_download_snapshot(list(reversed(records)))

        self.assertEqual(forward, reversed_snapshot)
        self.assertEqual(forward["albums"]["group"]["trackedDownloadStatus"], "beta")

    def test_public_download_status_sanitizes_values_without_mutating_cache(self):
        value = {
            "progress": 150.6,
            "status": {"title": "private status"},
            "trackedDownloadStatus": ["private tracked status"],
            "trackedDownloadState": ("private tracked state",),
            "timeLeft": {"path": "private time"},
            "estimatedCompletionTime": {"downloadId": "private eta"},
        }

        safe = lidarr.public_download_status(value)

        self.assertEqual(safe, {"progress": 100})
        self.assertEqual(value["status"], {"title": "private status"})
        self.assertEqual(lidarr.public_download_status({"progress": -1}), {"progress": 0})
        self.assertEqual(lidarr.public_download_status({"progress": True}), {})
        self.assertEqual(lidarr.public_download_status({"progress": float("nan")}), {})
        self.assertEqual(lidarr.public_download_status({"progress": "50"}), {})
        serialized = json.dumps(safe)
        for forbidden in ("private status", "private tracked", "private time", "private eta"):
            self.assertNotIn(forbidden, serialized)

    def test_public_download_status_formats_eta_in_the_container_timezone(self):
        with patch(
            "backend.services.lidarr._container_timezone",
            return_value=timezone(timedelta(hours=-4)),
        ):
            safe = lidarr.public_download_status({
                "progress": 59,
                "timeLeft": "00:24:06.4120410",
                "estimatedCompletionTime": "2026-08-05T20:56:32Z",
            })

        self.assertEqual(safe["timeLeft"], "00:24:06")
        self.assertEqual(
            safe["estimatedCompletionTime"],
            "08/05/2026 04:56 PM",
        )
        self.assertEqual(
            lidarr.public_download_status({"timeLeft": "1.02:03:04.5"}),
            {"timeLeft": "26:03:04"},
        )

    @patch("backend.services.lidarr.set_cache_document")
    @patch("backend.services.lidarr.cached_library_index", return_value={"albums": {}})
    @patch("backend.services.lidarr.queue_records", return_value=[])
    def test_successful_empty_poll_clears_snapshot(self, records, index, set_document):
        lidarr.refresh_download_snapshot({"url": "http://lidarr", "apiKey": "key"})

        self.assertEqual(set_document.call_args.args[2], {"albums": {}})

    @patch("backend.services.lidarr.set_cache_document")
    @patch("backend.services.lidarr.cached_library_index", return_value={"albums": {}})
    @patch("backend.services.lidarr.queue_records", side_effect=requests.RequestException())
    def test_failed_poll_keeps_the_prior_snapshot_until_expiry(self, records, index, set_document):
        with self.assertRaises(requests.RequestException):
            lidarr.refresh_download_snapshot({"url": "http://lidarr", "apiKey": "key"})

        set_document.assert_not_called()

    @patch("backend.services.lidarr.get_cache_document", return_value=["invalid"])
    def test_malformed_cached_download_document_degrades_to_empty(self, get_document):
        self.assertEqual(lidarr.cached_download_availability(), {})


class LidarrClientTests(unittest.TestCase):
    def test_connection_normalizes_url_and_retains_saved_key(self):
        result = lidarr.connection(
            {"hostname": "lidarr", "port": "8686", "apiKey": ""},
            {"apiKey": "saved-key"},
        )
        self.assertEqual(result, {
            "url": "http://lidarr:8686",
            "apiKey": "saved-key",
        })

    def test_connection_rejects_embedded_http_credentials(self):
        for hostname in (
            "http://user:password@lidarr:8686",
            "https://user@lidarr:8686",
            "user:password@lidarr:8686",
        ):
            with self.subTest(hostname=hostname):
                with self.assertRaisesRegex(
                    ValueError, "Lidarr URL must not contain a username or password"
                ):
                    lidarr.connection({"hostname": hostname, "apiKey": "key"})

    def test_cached_options_reject_legacy_url_with_embedded_credentials(self):
        with patch("backend.services.lidarr.cached_json_get") as cached_get:
            with self.assertRaisesRegex(ValueError, "username or password"):
                lidarr.options({
                    "url": "http://user:password@lidarr:8686", "apiKey": "key",
                })
        cached_get.assert_not_called()

    @patch("backend.services.lidarr.requests.request")
    def test_authenticated_request_rejects_redirect(self, request):
        request.return_value = Response(
            302,
            headers={"Location": "https://other.test/lidarr"},
        )

        with self.assertRaises(RedirectRejected):
            lidarr.system_status(
                {"url": "http://lidarr:8686", "apiKey": "private-key"}
            )

        request.assert_called_once()
        self.assertFalse(request.call_args.kwargs["allow_redirects"])
        self.assertEqual(
            request.call_args.kwargs["headers"],
            {"X-Api-Key": "private-key"},
        )

    @patch("backend.services.lidarr.requests.request")
    def test_artist_lookup_uses_authenticated_v1_endpoint(self, request):
        response = Response()
        request.return_value = response
        result = lidarr.lookup_artist(
            "artist-id",
            {"url": "http://lidarr:8686", "apiKey": "key"},
        )
        self.assertIs(result, response)
        request.assert_called_once_with(
            "GET",
            "http://lidarr:8686/api/v1/artist/lookup",
            allow_redirects=False,
            headers={"X-Api-Key": "key"},
            timeout=15,
            params={"term": "mbid:artist-id"},
        )

    @patch("backend.services.lidarr.requests.request")
    def test_monitor_albums_uses_album_monitor_endpoint(self, request):
        response = Response()
        request.return_value = response

        result = lidarr.monitor_albums(
            [33],
            config={"url": "http://lidarr:8686", "apiKey": "key"},
        )

        self.assertIs(result, response)
        request.assert_called_once_with(
            "PUT",
            "http://lidarr:8686/api/v1/album/monitor",
            allow_redirects=False,
            headers={"X-Api-Key": "key"},
            timeout=15,
            json={"albumIds": [33], "monitored": True},
        )

    @patch("backend.services.lidarr.library_artists")
    def test_tracked_artist_matches_musicbrainz_id_locally(self, artists):
        artists.return_value = [
            {"id": 1, "foreignArtistId": "other-id"},
            {"id": 2, "foreignArtistId": "artist-id"},
        ]

        result = lidarr.tracked_artist("artist-id", {"url": "http://lidarr"})

        self.assertEqual(result["id"], 2)

    @patch("backend.services.lidarr.requests.request")
    def test_artist_albums_use_local_artist_filter(self, request):
        request.return_value = Response(payload=[{"foreignAlbumId": "group-id"}])
        config = {"url": "http://lidarr:8686", "apiKey": "key"}

        result = lidarr.albums_by_artist(42, config)

        self.assertEqual(result[0]["foreignAlbumId"], "group-id")
        request.assert_called_once_with(
            "GET",
            "http://lidarr:8686/api/v1/album",
            allow_redirects=False,
            headers={"X-Api-Key": "key"},
            timeout=20,
            params={"artistId": 42},
        )

    @patch("backend.services.lidarr._metadata_artist")
    @patch("backend.services.lidarr.lookup_artist")
    def test_artist_image_prefers_configured_lidarr(self, lookup, metadata_artist):
        lookup.return_value = Response(payload=[{
            "foreignArtistId": "artist-id",
            "images": [
                {"coverType": "Fanart", "remoteUrl": "https://images/fanart.jpg"},
                {"coverType": "Poster", "remoteUrl": "https://images/poster.jpg"},
            ],
        }])

        result = lidarr.artist_image_url("artist-id", {"url": "http://lidarr"})

        self.assertEqual(result, "https://images/poster.jpg")
        metadata_artist.assert_not_called()

    @patch("backend.services.lidarr.cached_json_get")
    @patch("backend.services.lidarr.lookup_artist", side_effect=ValueError)
    def test_artist_image_uses_public_metadata_without_local_lidarr(
        self, _lookup, cached_get
    ):
        cached_get.return_value = {
            "images": [
                {"CoverType": "Fanart", "remoteUrl": "https://images/fanart.jpg"},
                {"CoverType": "Poster", "remoteUrl": "https://images/poster.jpg"},
            ]
        }

        result = lidarr.artist_image_url("artist id")

        self.assertEqual(result, "https://images/poster.jpg")
        self.assertEqual(
            cached_get.call_args.args[0],
            "https://api.lidarr.audio/api/v0.4/artist/artist%20id",
        )
        self.assertEqual(
            cached_get.call_args.kwargs["namespace"], "lidarr-artist-metadata"
        )

    @patch("backend.services.lidarr.cached_json_get")
    @patch("backend.services.lidarr.lookup_artist", side_effect=ValueError)
    def test_artist_image_handles_public_metadata_failure(self, _lookup, cached_get):
        cached_get.side_effect = requests.ConnectionError("metadata unavailable")

        self.assertIsNone(lidarr.artist_image_url("artist-id"))


class MusicBrainzClientTests(unittest.TestCase):
    def setUp(self):
        self.original_next_request_at = musicbrainz._next_request_at
        self.original_critical_waiters = musicbrainz._critical_waiters
        self.original_interactive_waiters = musicbrainz._interactive_waiters
        self.original_prefetch_waiters = musicbrainz._prefetch_waiters
        self.original_critical_streak = musicbrainz._critical_streak
        self.original_critical_operations = musicbrainz._critical_operations
        self.original_background_failure_streak = (
            musicbrainz._background_failure_streak
        )
        self.original_background_resume_at = musicbrainz._background_resume_at
        musicbrainz._next_request_at = 0.0
        musicbrainz._critical_waiters = 0
        musicbrainz._interactive_waiters = 0
        musicbrainz._prefetch_waiters = 0
        musicbrainz._critical_streak = 0
        musicbrainz._critical_operations = 0
        musicbrainz._background_failure_streak = 0
        musicbrainz._background_resume_at = 0.0
        if hasattr(musicbrainz._session_state, "session"):
            del musicbrainz._session_state.session

    def tearDown(self):
        musicbrainz._next_request_at = self.original_next_request_at
        musicbrainz._critical_waiters = self.original_critical_waiters
        musicbrainz._interactive_waiters = self.original_interactive_waiters
        musicbrainz._prefetch_waiters = self.original_prefetch_waiters
        musicbrainz._critical_streak = self.original_critical_streak
        musicbrainz._critical_operations = self.original_critical_operations
        musicbrainz._background_failure_streak = (
            self.original_background_failure_streak
        )
        musicbrainz._background_resume_at = self.original_background_resume_at
        if hasattr(musicbrainz._session_state, "session"):
            del musicbrainz._session_state.session

    @patch("backend.services.musicbrainz._wait_for_request_slot")
    @patch("backend.services.musicbrainz._http_get")
    @patch("backend.services.musicbrainz.configuration")
    def test_artist_count_probe_uses_one_paced_xml_lookup(
        self, configuration, http_get, wait_for_slot,
    ):
        mbid = "11111111-1111-4111-8111-111111111111"
        configuration.return_value = {
            "baseUrl": "https://musicbrainz.org/ws/2",
            "userAgent": "Melodarr Test/1.0",
            "requestIntervalMs": 1100,
        }
        http_get.return_value = Response(content=(
            f'<metadata xmlns="http://musicbrainz.org/ns/mmd-2.0#">'
            f'<artist id="{mbid}">'
            '<release-group-list count="57"/>'
            '<release-list count="181"/>'
            '<recording-list count="357"/>'
            '</artist></metadata>'
        ).encode())

        result = musicbrainz.artist_entity_counts(mbid)

        self.assertEqual(result, {
            "releaseGroupCount": 57,
            "releaseCount": 181,
            "recordingCount": 357,
        })
        wait_for_slot.assert_called_once_with("background", 1.1)
        http_get.assert_called_once()
        self.assertEqual(http_get.call_args.kwargs["params"]["fmt"], "xml")

    @patch("backend.services.musicbrainz.get_service")
    def test_configuration_normalizes_self_hosted_settings(self, get_service):
        get_service.return_value = {
            "baseUrl": "http://musicbrainz.local:5000/ws/2/",
            "userAgent": "  Melodarr Local/1.0  ",
            "requestIntervalMs": "0",
        }

        self.assertEqual(musicbrainz.configuration(), {
            "baseUrl": "http://musicbrainz.local:5000/ws/2",
            "userAgent": "Melodarr Local/1.0",
            "requestIntervalMs": 0,
        })

    @patch("backend.services.musicbrainz.cached_json_get")
    @patch("backend.services.musicbrainz.configuration")
    def test_metadata_uses_configured_endpoint_user_agent_and_interval(
        self, configuration, cached_get
    ):
        configuration.return_value = {
            "baseUrl": "http://musicbrainz.local:5000/ws/2",
            "userAgent": "Melodarr Local/1.0",
            "requestIntervalMs": 0,
        }
        cached_get.return_value = {"id": "artist-id"}

        result = musicbrainz.get("/artist/artist-id", "genres")

        self.assertEqual(result, {"id": "artist-id"})
        self.assertEqual(
            cached_get.call_args.args[0],
            "http://musicbrainz.local:5000/ws/2/artist/artist-id",
        )
        self.assertEqual(
            cached_get.call_args.kwargs["headers"],
            {"User-Agent": "Melodarr Local/1.0"},
        )
        with patch.object(musicbrainz, "_wait_for_request_slot") as wait_for_slot:
            cached_get.call_args.kwargs["before_request"]()
        wait_for_slot.assert_called_once_with("interactive", 0.0)

    @patch("backend.services.musicbrainz.time.sleep")
    @patch("backend.services.musicbrainz.time.monotonic")
    @patch("backend.services.musicbrainz._http_get")
    def test_connection_probe_resolves_a_known_ws2_recording(
        self, http_get, monotonic, sleep
    ):
        monotonic.side_effect = [10.0, 10.042]
        http_get.side_effect = [
            Response(200, {
                "id": musicbrainz.TEST_RECORDING_ID,
                "title": "Intro",
            }),
            Response(200, {
                "artists": [{"id": "artist-id", "name": "3 Doors Down"}],
            }),
        ]
        proposed = {
            "baseUrl": "http://musicbrainz.local:5000/ws/2/",
            "userAgent": "Melodarr Local/1.0",
            "requestIntervalMs": 1100,
        }

        result = musicbrainz.test_connection(proposed)

        self.assertEqual(result["latencyMs"], 42)
        self.assertEqual(result["recording"]["title"], "Intro")
        sleep.assert_called_once_with(1.1)
        self.assertEqual(http_get.call_count, 2)
        lookup_call, search_call = http_get.call_args_list
        self.assertEqual(
            lookup_call.args[0],
            f"http://musicbrainz.local:5000/ws/2/recording/"
            f"{musicbrainz.TEST_RECORDING_ID}",
        )
        self.assertEqual(lookup_call.kwargs, {
            "params": {"fmt": "json"},
            "headers": {
                "Accept": "application/json",
                "User-Agent": "Melodarr Local/1.0",
            },
            "timeout": 15,
            "allow_redirects": False,
        })
        self.assertEqual(
            search_call.args[0],
            "http://musicbrainz.local:5000/ws/2/artist/",
        )
        self.assertEqual(search_call.kwargs["params"], {
            "query": "3 Doors Down",
            "fmt": "json",
            "limit": 1,
            "dismax": "true",
        })

    @patch("backend.services.musicbrainz._http_get")
    def test_connection_rejects_a_server_with_broken_search(self, http_get):
        http_get.side_effect = [
            Response(200, {
                "id": musicbrainz.TEST_RECORDING_ID,
                "title": "Intro",
            }),
            Response(503, {
                "error": "Can't connect to search:8983 (No address associated)",
            }),
        ]

        with self.assertRaises(musicbrainz.SearchUnavailableError) as raised:
            musicbrainz.test_connection({
                "baseUrl": "http://musicbrainz.local:5000/ws/2",
                "userAgent": "Melodarr Local/1.0",
                "requestIntervalMs": 0,
            })

        self.assertIn("search/Solr service", str(raised.exception))

    @patch("backend.services.musicbrainz.requests.Session")
    def test_musicbrainz_reuses_a_thread_local_http_session(self, session_factory):
        session = session_factory.return_value

        musicbrainz._http_get("https://musicbrainz.test/one")
        musicbrainz._http_get("https://musicbrainz.test/two")

        session_factory.assert_called_once_with()
        self.assertEqual(session.get.call_count, 2)

    @patch("backend.services.musicbrainz.time.sleep")
    @patch("backend.services.musicbrainz.time.monotonic")
    def test_background_transport_failure_opens_bounded_circuit(
        self, monotonic, sleep
    ):
        monotonic.side_effect = [100.0, 100.0, 130.0]

        with self.assertLogs("backend.services.musicbrainz", level="WARNING"):
            musicbrainz._record_background_failure(
                requests.exceptions.SSLError("upstream TLS closed")
            )
        musicbrainz._wait_for_background_circuit()

        sleep.assert_called_once_with(30.0)
        self.assertEqual(musicbrainz._background_failure_streak, 1)

    @patch("backend.services.musicbrainz.time.monotonic")
    def test_repeated_background_failures_cap_cooldown_at_sixty_seconds(
        self, monotonic
    ):
        monotonic.side_effect = [100.0, 101.0, 102.0]

        with self.assertLogs("backend.services.musicbrainz", level="WARNING"):
            musicbrainz._record_background_failure(
                requests.exceptions.SSLError("first")
            )
            musicbrainz._record_background_failure(
                requests.exceptions.SSLError("second")
            )
            musicbrainz._record_background_failure(
                requests.exceptions.SSLError("third")
            )

        self.assertEqual(musicbrainz._background_failure_streak, 3)
        self.assertEqual(musicbrainz._background_resume_at, 162.0)

    @patch("backend.services.musicbrainz._record_background_failure")
    @patch("backend.services.musicbrainz.cached_json_get")
    def test_only_background_failures_open_the_circuit(
        self, cached_get, record_failure
    ):
        error = requests.exceptions.SSLError("upstream TLS closed")
        cached_get.side_effect = error

        with self.assertRaises(requests.exceptions.SSLError):
            musicbrainz.get("/artist/artist-id", "", priority="interactive")
        record_failure.assert_not_called()

        with self.assertRaises(requests.exceptions.SSLError):
            musicbrainz.get("/artist/artist-id", "", priority="background")
        record_failure.assert_called_once_with(error)

    @patch("backend.services.musicbrainz.time.sleep")
    @patch("backend.services.musicbrainz.time.monotonic")
    def test_live_request_slots_are_shared_and_spaced(self, monotonic, sleep):
        monotonic.side_effect = [10.0, 10.2, 11.1]
        musicbrainz._wait_for_request_slot(request_interval_seconds=1.1)
        musicbrainz._wait_for_request_slot(request_interval_seconds=1.1)
        sleep.assert_called_once_with(0.9000000000000004)
        self.assertAlmostEqual(musicbrainz._next_request_at, 12.2)

    @patch("backend.services.musicbrainz.time.sleep")
    @patch("backend.services.musicbrainz.time.monotonic", return_value=10.0)
    def test_zero_request_interval_does_not_sleep(self, _monotonic, sleep):
        musicbrainz._wait_for_request_slot(request_interval_seconds=0)

        sleep.assert_not_called()
        self.assertEqual(musicbrainz._next_request_at, 10.0)

    @patch("backend.services.musicbrainz._wait_for_request_slot")
    @patch("backend.services.musicbrainz.cached_json_get")
    def test_background_priority_is_applied_only_on_live_cache_miss(
        self, cached_get, wait_for_slot
    ):
        cached_get.return_value = {"id": "group"}
        musicbrainz.get("/release-group/group", "", priority="background")
        before_request = cached_get.call_args.kwargs["before_request"]

        wait_for_slot.assert_not_called()
        before_request()
        wait_for_slot.assert_called_once_with("background", 1.1)

        wait_for_slot.reset_mock()
        musicbrainz.get("/release-group/group", "", priority="prefetch")
        cached_get.call_args.kwargs["before_request"]()
        wait_for_slot.assert_called_once_with("prefetch", 1.1)

    @patch("backend.services.musicbrainz.cached_json_get")
    def test_critical_discography_calls_get_extended_retries(self, cached_get):
        cached_get.return_value = {"release-groups": []}

        musicbrainz.get(
            "/release-group",
            "",
            artist="artist-id",
            limit=100,
            priority="critical",
        )

        kwargs = cached_get.call_args.kwargs
        self.assertEqual(kwargs["max_attempts"], 5)
        self.assertEqual(kwargs["request_timeout"], 20)
        self.assertIn(requests.Timeout, kwargs["retry_exceptions"])
        self.assertIs(kwargs["request_get"], musicbrainz._http_get)

    def test_discography_burst_yields_to_waiting_interactive_search(self):
        musicbrainz._critical_waiters = 1
        musicbrainz._interactive_waiters = 1
        musicbrainz._critical_streak = musicbrainz._CRITICAL_BURST_LIMIT

        self.assertTrue(musicbrainz._priority_is_blocked("critical"))
        self.assertFalse(musicbrainz._priority_is_blocked("interactive"))

        musicbrainz._critical_streak = 0
        self.assertFalse(musicbrainz._priority_is_blocked("critical"))
        self.assertTrue(musicbrainz._priority_is_blocked("interactive"))

    def test_discography_operation_only_blocks_speculative_priorities(self):
        with musicbrainz.critical_operation():
            self.assertFalse(musicbrainz._priority_is_blocked("interactive"))
            self.assertTrue(musicbrainz._priority_is_blocked("prefetch"))
            self.assertTrue(musicbrainz._priority_is_blocked("background"))

    @patch("backend.services.musicbrainz.cached_json_get")
    def test_release_group_search_uses_search_cache(self, cached_get):
        cached_get.return_value = {"release-groups": []}
        result = musicbrainz.search("artist:Test", "release-group", True)
        self.assertEqual(result, {"release-groups": []})
        _, kwargs = cached_get.call_args
        self.assertTrue(cached_get.call_args.args[0].endswith("/release-group/"))
        self.assertEqual(kwargs["namespace"], "musicbrainz-search")
        self.assertTrue(kwargs["include_cache_status"])
        self.assertEqual(kwargs["params"]["limit"], 100)

    @patch("backend.services.musicbrainz.cached_json_get")
    def test_artist_search_keeps_the_default_candidate_limit(self, cached_get):
        cached_get.return_value = {"artists": []}

        musicbrainz.search("Test", "artist")

        self.assertEqual(cached_get.call_args.kwargs["params"]["limit"], 25)

    @patch("backend.services.musicbrainz.cached_json_get")
    def test_track_search_uses_recording_resource(self, cached_get):
        cached_get.return_value = {"recordings": []}

        result = musicbrainz.search("Song title", "track", plain_search=True)

        self.assertEqual(result, {"recordings": []})
        self.assertTrue(cached_get.call_args.args[0].endswith("/recording/"))
        self.assertEqual(
            cached_get.call_args.kwargs["namespace"],
            "musicbrainz-search",
        )
        self.assertEqual(
            cached_get.call_args.kwargs["params"]["dismax"],
            "true",
        )
        self.assertEqual(cached_get.call_args.kwargs["params"]["limit"], 25)

    @patch("backend.services.musicbrainz.cached_json_get")
    def test_metadata_lookup_forwards_includes_and_paging(self, cached_get):
        cached_get.return_value = {"release-groups": []}
        musicbrainz.get(
            "/release-group",
            "artist-credits",
            artist="artist-id",
            limit=100,
            offset=200,
        )
        _, kwargs = cached_get.call_args
        self.assertEqual(kwargs["namespace"], "musicbrainz-metadata")
        self.assertEqual(kwargs["params"], {
            "fmt": "json",
            "artist": "artist-id",
            "limit": 100,
            "offset": 200,
            "inc": "artist-credits",
        })

    @patch("backend.services.musicbrainz.cached_json_get")
    def test_metadata_lookup_can_force_refresh(self, cached_get):
        cached_get.return_value = {"release-groups": []}
        musicbrainz.get("/artist/artist-id", "genres", force_refresh=True)
        self.assertTrue(cached_get.call_args.kwargs["force_refresh"])

    @patch("backend.services.musicbrainz.cached_json_get")
    def test_metadata_lookup_can_read_cache_without_live_request(self, cached_get):
        cached_get.return_value = None
        musicbrainz.get("/artist/artist-id", "genres", cache_only=True)
        self.assertTrue(cached_get.call_args.kwargs["cache_only"])

    def test_artist_name_prefers_primary_english_alias(self):
        artist = {
            "name": "ポルカドットスティングレイ",
            "sort-name": "POLKADOT STINGRAY",
            "aliases": [
                {"name": "Porukadotto Sutingurei", "locale": "ja-Latn"},
                {"name": "POLKADOT STINGRAY", "locale": "en", "primary": True},
            ],
        }

        self.assertEqual(
            musicbrainz.romanized_artist_name(artist), "POLKADOT STINGRAY"
        )

    def test_artist_name_falls_back_to_latin_sort_name(self):
        artist = {"name": "雫", "sort-name": "Shizuku", "aliases": []}

        self.assertEqual(musicbrainz.romanized_artist_name(artist), "Shizuku")

    def test_artist_name_omits_duplicate_for_latin_canonical_name(self):
        artist = {"name": "BAND-MAID", "sort-name": "BAND-MAID"}

        self.assertEqual(musicbrainz.romanized_artist_name(artist), "")

    def test_release_title_prefers_english_alias(self):
        group = {
            "title": "極彩",
            "aliases": [{"name": "In Full Color", "locale": "en"}],
        }

        self.assertEqual(
            musicbrainz.romanized_release_group_title(group), "In Full Color"
        )

    def test_release_title_romanizes_japanese_without_alias(self):
        group = {"title": "雫", "aliases": []}

        self.assertEqual(
            musicbrainz.romanized_release_group_title(group), "Shizuku"
        )

    def test_release_title_omits_duplicate_for_latin_title(self):
        self.assertEqual(
            musicbrainz.romanized_release_group_title({"title": "BLACKBOX"}),
            "",
        )

    def test_track_title_romanizes_locally_without_using_aliases(self):
        self.assertEqual(musicbrainz.romanized_track_title("じゃあな"), "Jaana")
        self.assertEqual(
            musicbrainz.romanized_track_title("more than words"), ""
        )


class LastFmDiscoveryTests(DatabaseTestCase):
    @patch("backend.routes.discovery.recommendation_engine.lastfm_recommendations")
    def test_personal_recommendations_use_the_shared_key(
        self, lastfm_recommendations
    ):
        token = self.register()
        save_service("lastfm", {"apiKey": "admin-shared-key"})
        with db() as connection:
            connection.execute(
                "UPDATE users SET lastfm_username = ?, lastfm_api_key = ? "
                "WHERE username = ?",
                (
                    "personal-listener",
                    "legacy-key-that-must-not-be-used",
                    "test-user",
                ),
            )
        lastfm_recommendations.return_value = (
            [{"id": "artist-id", "name": "Artist"}],
            [{"id": "album-id", "name": "Album"}],
        )

        response = self.client.get(
            "/api/recommendations/lastfm",
            headers={"X-CSRF-Token": token},
        )

        self.assertEqual(response.status_code, 200)
        lastfm_recommendations.assert_called_once_with(
            "personal-listener",
            "admin-shared-key",
        )
        self.assertEqual(response.get_json()["username"], "personal-listener")
        serialized = response.get_data(as_text=True)
        self.assertNotIn("admin-shared-key", serialized)
        self.assertNotIn("legacy-key-that-must-not-be-used", serialized)

    @patch("backend.routes.discovery.lastfm.get")
    def test_global_chart_uses_the_shared_key_without_a_personal_username(
        self, lastfm_get
    ):
        token = self.register()
        save_service("lastfm", {"apiKey": "admin-shared-key"})
        lastfm_get.return_value = {"artists": {"artist": []}}

        response = self.client.get(
            "/api/charts/lastfm",
            headers={"X-CSRF-Token": token},
        )

        self.assertEqual(response.status_code, 200)
        lastfm_get.assert_called_once_with(
            "chart.gettopartists",
            "melodarr",
            "admin-shared-key",
            limit=20,
        )
        self.assertNotIn("admin-shared-key", response.get_data(as_text=True))


def _release_group_fixture(
    identifier,
    title,
    *,
    score=80,
    date="2020-01-01",
    artist="Example Artist",
    primary_type="Album",
    secondary_types=None,
):
    return {
        "id": identifier,
        "title": title,
        "score": score,
        "first-release-date": date,
        "primary-type": primary_type,
        "secondary-types": secondary_types or [],
        "artist-credit": [{
            "name": artist,
            "artist": {"name": artist, "sort-name": artist},
        }],
    }


class ReleaseGroupRankingTests(unittest.TestCase):
    @staticmethod
    def _recording(
        identifier,
        title,
        *,
        score=80,
        artist="Example Artist",
        disambiguation="",
        primary_type="Album",
        secondary_types=None,
    ):
        return {
            "id": f"recording-{identifier}",
            "title": title,
            "score": score,
            "disambiguation": disambiguation,
            "artist-credit": [{
                "name": artist,
                "artist": {"name": artist, "sort-name": artist},
            }],
            "releases": [{
                "id": f"release-{identifier}",
                "title": title,
                "status": "Official",
                "date": "2020-01-01",
                "release-group": {
                    "id": f"group-{identifier}",
                    "primary-type": primary_type,
                    "secondary-types": secondary_types or [],
                },
            }],
        }

    def test_album_ranking_prefers_exact_and_strong_title_matches(self):
        candidates = [
            _release_group_fixture(
                "substring",
                "Music From The Odyssey Sessions",
                score=100,
                date="2026-01-01",
            ),
            _release_group_fixture(
                "prefix-old",
                "The Odyssey: A Journey",
                score=80,
                date="2000-01-01",
            ),
            _release_group_fixture(
                "exact",
                "The Odyssey",
                score=60,
                date="1990-01-01",
            ),
            _release_group_fixture(
                "prefix-new",
                "The Odyssey: Original Motion Picture Soundtrack",
                score=80,
                date="2026-01-01",
            ),
        ]

        ranked = discovery._rank_release_groups(candidates, "The Odyssey")

        self.assertEqual(
            [group["id"] for group in ranked],
            ["exact", "prefix-new", "prefix-old", "substring"],
        )

    def test_album_ranking_keeps_relevance_ahead_of_recency(self):
        candidates = [
            _release_group_fixture(
                "newer",
                "The Odyssey: New Recording",
                score=70,
                date="2026-01-01",
            ),
            _release_group_fixture(
                "stronger",
                "The Odyssey: Definitive Recording",
                score=95,
                date="1980-01-01",
            ),
        ]

        ranked = discovery._rank_release_groups(candidates, "The Odyssey")

        self.assertEqual([group["id"] for group in ranked], ["stronger", "newer"])

    def test_album_ranking_diversifies_duplicate_titles_deterministically(self):
        candidates = [
            _release_group_fixture(f"exact-{index}", "The Odyssey", score=100 - index)
            for index in range(6)
        ]
        candidates.extend(
            _release_group_fixture(
                f"related-{index}",
                f"The Odyssey: Related Album {index}",
                score=80 - index,
            )
            for index in range(30)
        )

        first = discovery._rank_release_groups(candidates, "The Odyssey")
        second = discovery._rank_release_groups(candidates, "The Odyssey")

        self.assertEqual(
            sum(group["title"] == "The Odyssey" for group in first[:25]),
            2,
        )
        self.assertEqual(
            {group["id"] for group in first},
            {group["id"] for group in candidates},
        )
        self.assertEqual(
            [group["id"] for group in first],
            [group["id"] for group in second],
        )

    @patch("backend.routes.discovery.musicbrainz.search")
    def test_artist_qualified_album_search_uses_one_fielded_request(self, search):
        search.return_value = {"release-groups": [
            _release_group_fixture(
                "qualified",
                "The Odyssey",
                score=75,
                artist="Ludwig Göransson",
            ),
            _release_group_fixture(
                "literal",
                "The Odyssey Ludwig Göransson",
                score=75,
                artist="Someone Else",
            ),
        ]}

        app = Flask(__name__)
        with app.test_request_context(
            "/api/search?q=The%20Odyssey%20Ludwig%20G%C3%B6ransson&type=album"
        ):
            response = discovery.search.__wrapped__()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["results"][0]["id"], "qualified")
        search.assert_called_once()
        search_query = search.call_args.args[0]
        self.assertIn('releasegroup:"The Odyssey"', search_query)
        self.assertIn('artist:"Ludwig Göransson"', search_query)
        self.assertFalse(search.call_args.kwargs["plain_search"])

    @patch("backend.routes.discovery.musicbrainz.search")
    def test_album_search_supports_a_one_word_artist_suffix(self, search):
        search.return_value = {"release-groups": [
            _release_group_fixture(
                "drake-album",
                "Nothing Was the Same",
                score=90,
                artist="Drake",
            ),
            _release_group_fixture(
                "literal",
                "Nothing Was the Same Drake",
                score=95,
                artist="Someone Else",
            ),
        ]}

        app = Flask(__name__)
        with app.test_request_context(
            "/api/search?q=Nothing%20Was%20the%20same%20Drake&type=album"
        ):
            response = discovery.search.__wrapped__()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["results"][0]["id"], "drake-album")
        search.assert_called_once()
        search_query = search.call_args.args[0]
        self.assertIn('releasegroup:"Nothing Was the same"', search_query)
        self.assertIn('artist:"Drake"', search_query)
        self.assertFalse(search.call_args.kwargs["plain_search"])

    @patch("backend.routes.discovery.musicbrainz.search")
    def test_album_search_adds_conservative_title_typo_tolerance(self, search):
        search.return_value = {"release-groups": [
            _release_group_fixture(
                "mamma-mia",
                "Mamma Mia! Here We Go Again",
                score=100,
                artist="Benny Andersson · Björn Ulvaeus",
                secondary_types=["Soundtrack"],
            ),
        ]}

        app = Flask(__name__)
        with app.test_request_context(
            "/api/search?q=Mama%20Mia%20here%20we%20go%20again&type=album"
        ):
            response = discovery.search.__wrapped__()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["results"][0]["id"], "mamma-mia")
        search.assert_called_once()
        search_query = search.call_args.args[0]
        self.assertIn("releasegroup:(mama~1", search_query)
        self.assertIn('AND "mia"', search_query)
        self.assertFalse(search.call_args.kwargs["plain_search"])

    def test_track_title_quality_precedes_musicbrainz_score(self):
        recordings = [
            self._recording(
                "all-terms",
                "Lights After Blinding",
                score=100,
            ),
            self._recording(
                "phrase",
                "The Blinding Lights Theme",
                score=95,
            ),
            self._recording(
                "prefix",
                "Blinding Lights Extended",
                score=90,
            ),
            self._recording("exact", "Blinding Lights", score=70),
        ]
        plan = discovery._track_search_plan("Blinding Lights")

        first = discovery._recording_release_group_candidates(
            {"recordings": recordings},
            plan,
        )
        second = discovery._recording_release_group_candidates(
            {"recordings": recordings},
            plan,
        )

        expected = ["group-exact", "group-prefix", "group-phrase", "group-all-terms"]
        self.assertEqual([candidate["id"] for candidate in first], expected)
        self.assertEqual([candidate["id"] for candidate in second], expected)

    def test_explicit_track_artist_match_precedes_musicbrainz_score(self):
        plan = discovery._track_search_plan("Blinding Lights - The Weeknd")
        recordings = [
            self._recording(
                "wrong-artist",
                "Blinding Lights",
                score=100,
                artist="Example Band",
            ),
            self._recording(
                "right-artist",
                "Blinding Lights",
                score=75,
                artist="The Weeknd",
            ),
        ]

        candidates = discovery._recording_release_group_candidates(
            {"recordings": recordings},
            plan,
        )

        self.assertEqual(candidates[0]["id"], "group-right-artist")
        self.assertIn('recording:"Blinding Lights"', plan["query"])
        self.assertIn('artist:"The Weeknd"', plan["query"])
        self.assertFalse(plan["plainSearch"])

    def test_track_search_matches_a_transliterated_pseudo_release_title(self):
        plan = discovery._track_search_plan("Kessen Spirit chico")
        target = self._recording(
            "kessen-spirit",
            "決戦スピリット (アニメsize)",
            score=68,
            artist="CHiCO with HoneyWorks",
            primary_type="Single",
        )
        target["releases"] = [{
            "id": "release-kessen-spirit",
            "title": "Kessen Spirit",
            "status": "Pseudo-Release",
            "date": "2020-02-26",
            "artist-credit": target["artist-credit"],
            "release-group": {
                "id": "4a81a32b-c395-4e01-90db-52e80a270491",
                "title": "決戦スピリット",
                "primary-type": "Single",
                "secondary-types": [],
                "artist-credit": target["artist-credit"],
            },
        }]
        unrelated = self._recording(
            "unrelated",
            "Kessen",
            score=100,
            artist="Someone Else",
        )

        candidates = discovery._recording_release_group_candidates(
            {"recordings": [unrelated, target]},
            plan,
        )

        self.assertIn('release:"Kessen Spirit"', plan["query"])
        self.assertIn('artist:"chico"', plan["query"])
        self.assertEqual(
            candidates[0]["id"],
            "4a81a32b-c395-4e01-90db-52e80a270491",
        )

    def test_track_search_recovers_a_release_group_romanized_alias(self):
        plan = discovery._track_search_plan(
            "Sekai wa Koi ni Ochiteiru chico"
        )
        artist_mbid = "11111111-1111-4111-8111-111111111111"
        interpretation = next(
            item
            for item in plan["interpretations"]
            if item["artist"].casefold() == "chico"
        )
        plan = discovery._with_resolved_artist(
            plan,
            interpretation,
            artist_mbid,
        )
        target = _release_group_fixture(
            "bc465d63-9087-4707-ab43-c01bdad7dc0a",
            "世界は恋に落ちている",
            score=100,
            date="2014-08-06",
            artist="CHiCO with HoneyWorks",
            primary_type="Single",
        )
        target["artist-credit"][0]["artist"]["id"] = artist_mbid
        weak = _release_group_fixture(
            "weak-alias-result",
            "Unrelated",
            score=70,
            artist="CHiCO with HoneyWorks",
            primary_type="Single",
        )
        weak["artist-credit"][0]["artist"]["id"] = artist_mbid

        with patch.object(
            discovery.musicbrainz,
            "search",
            return_value={"release-groups": [weak, target]},
        ) as search:
            results = discovery._track_release_group_alias_results(plan)

        search_query = search.call_args.args[0]
        self.assertIn('alias:"Sekai wa Koi ni Ochiteiru"', search_query)
        self.assertIn(f"arid:{artist_mbid}", search_query)
        self.assertIn("primarytype:single", search_query)
        self.assertEqual(search.call_args.kwargs["limit"], 100)
        self.assertEqual(len(results), 1)
        self.assertEqual(
            results[0]["id"],
            "bc465d63-9087-4707-ab43-c01bdad7dc0a",
        )
        self.assertEqual(results[0]["matchedTrack"], "Sekai wa Koi ni Ochiteiru")

    @patch(
        "backend.routes.discovery._recording_release_group_results",
        return_value=[],
    )
    @patch("backend.routes.discovery.musicbrainz.search")
    def test_track_route_uses_alias_fallback_only_after_a_recording_miss(
        self,
        search,
        release_group_results,
    ):
        target = _release_group_fixture(
            "bc465d63-9087-4707-ab43-c01bdad7dc0a",
            "世界は恋に落ちている",
            score=100,
            date="2014-08-06",
            artist="CHiCO with HoneyWorks",
            primary_type="Single",
        )
        search.side_effect = [
            {"recordings": []},
            {"release-groups": [target]},
        ]
        app = Flask(__name__)
        with app.test_request_context(
            "/api/search?q=Sekai%20wa%20Koi%20ni%20Ochiteiru%20chico&type=track"
        ):
            response = discovery.search.__wrapped__()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json()["results"][0]["id"],
            "bc465d63-9087-4707-ab43-c01bdad7dc0a",
        )
        self.assertEqual(response.get_json()["source"], "musicbrainz")
        self.assertEqual(search.call_count, 2)
        self.assertEqual(search.call_args_list[0].args[1], "track")
        self.assertEqual(search.call_args_list[1].args[1], "album")
        release_group_results.assert_not_called()

    def test_track_version_intent_beats_a_generic_higher_score(self):
        plan = discovery._track_search_plan("Numb live")
        recordings = [
            self._recording("studio", "Numb", score=100),
            self._recording(
                "live",
                "Numb",
                score=70,
                disambiguation="live",
                secondary_types=["Live"],
            ),
        ]

        candidates = discovery._recording_release_group_candidates(
            {"recordings": recordings},
            plan,
        )

        self.assertEqual(candidates[0]["id"], "group-live")

    def test_track_query_parsing_supports_space_suffixes_and_isrc(self):
        by_plan = discovery._track_search_plan("Blinding Lights by The Weeknd")
        one_word_by_plan = discovery._track_search_plan("yellow by coldplay")
        space_only = discovery._track_search_plan("Blinding Lights The Weeknd")
        yellow = discovery._track_search_plan("Yellow Coldplay")
        title_with_by = discovery._track_search_plan("Stand By Me")
        isrc = discovery._track_search_plan("US-RC1-76-07839")

        self.assertEqual(by_plan["title"], "Blinding Lights")
        self.assertEqual(by_plan["artist"], "The Weeknd")
        self.assertEqual(one_word_by_plan["title"], "yellow")
        self.assertEqual(one_word_by_plan["artist"], "coldplay")
        self.assertNotIn('recording:"yellow by coldplay"', one_word_by_plan["query"])
        self.assertIn('recording:"yellow"', one_word_by_plan["query"])
        self.assertIn('artist:"coldplay"', one_word_by_plan["query"])
        self.assertIn(
            {"title": "Blinding Lights", "artist": "The Weeknd", "source": "inferred"},
            space_only["interpretations"],
        )
        self.assertIn(
            {"title": "Yellow", "artist": "Coldplay", "source": "inferred"},
            yellow["interpretations"],
        )
        self.assertIn('artistname:"Coldplay"))^2', yellow["query"])
        self.assertFalse(space_only["plainSearch"])
        self.assertEqual(title_with_by["artist"], "")
        self.assertEqual(title_with_by["literalTitle"], "Stand By Me")
        self.assertIn(
            {"title": "Stand By Me", "artist": "", "source": "literal"},
            title_with_by["interpretations"],
        )
        self.assertEqual(isrc["query"], "isrc:USRC17607839")
        self.assertFalse(isrc["plainSearch"])

    def test_explicit_track_artist_handles_repetitive_separator_input(self):
        split = discovery._explicit_track_artist
        self.assertEqual(split("Song - Artist"), ("Song", "Artist"))
        self.assertEqual(split("Song – Artist"), ("Song", "Artist"))
        self.assertEqual(split("Song — Artist"), ("Song", "Artist"))
        self.assertEqual(split("Song by Artist Name"), ("Song", "Artist Name"))
        self.assertEqual(split("Song by Artist - Remix"), ("Song by Artist", "Remix"))
        self.assertEqual(split("Stand By Me"), ("Stand By Me", ""))

        repeated = "a" * 6000 + " " * 6000
        self.assertEqual(split(repeated + "- Artist"), ("a" * 6000, "Artist"))
        self.assertEqual(
            split(repeated + "by Artist Name"),
            ("a" * 6000, "Artist Name"),
        )

    def test_space_only_track_search_uses_the_strongest_valid_split(self):
        plan = discovery._track_search_plan("Blinding Lights The Weeknd")
        recordings = [
            self._recording(
                "generic",
                "Blinding Lights The",
                score=100,
                artist="Someone Else",
            ),
            self._recording(
                "qualified",
                "Blinding Lights",
                score=70,
                artist="The Weeknd",
            ),
        ]

        candidates = discovery._recording_release_group_candidates(
            {"recordings": recordings},
            plan,
        )

        self.assertEqual(candidates[0]["id"], "group-qualified")

    def test_exact_literal_track_title_precedes_an_inferred_split(self):
        plan = discovery._track_search_plan("Yellow Coldplay")
        recordings = [
            self._recording(
                "qualified",
                "Yellow",
                score=100,
                artist="Coldplay",
            ),
            self._recording(
                "literal",
                "Yellow Coldplay",
                score=60,
                artist="Someone Else",
            ),
        ]

        candidates = discovery._recording_release_group_candidates(
            {"recordings": recordings},
            plan,
        )

        self.assertEqual(candidates[0]["id"], "group-literal")

    def test_parenthetical_cover_does_not_beat_matching_artist_album(self):
        plan = discovery._track_search_plan("Yellow Coldplay")
        cover = self._recording(
            "cover",
            "Yellow (Coldplay)",
            score=100,
            artist="Cover Artist",
        )
        coldplay = self._recording(
            "parachutes",
            "Yellow",
            score=76,
            artist="Coldplay",
        )
        coldplay["releases"][0].update({
            "title": "Parachutes",
            "status": "",
            "date": "2000-07-10",
            "artist-credit": [{
                "name": "Coldplay",
                "artist": {"name": "Coldplay", "sort-name": "Coldplay"},
            }],
        })
        coldplay["releases"][0]["release-group"].update({
            "id": "1dc4c347-a1db-32aa-b14f-bc9cc507b843",
            "title": "Parachutes",
            "artist-credit": [{
                "name": "Coldplay",
                "artist": {"name": "Coldplay", "sort-name": "Coldplay"},
            }],
        })
        compilation = self._recording(
            "compilation",
            "Yellow",
            score=76,
            artist="Coldplay",
            secondary_types=["Compilation"],
        )
        compilation["releases"][0]["release-group"]["artist-credit"] = [{
            "name": "Various Artists",
            "artist": {
                "name": "Various Artists",
                "sort-name": "Various Artists",
            },
        }]

        candidates = discovery._recording_release_group_candidates(
            {"recordings": [cover, compilation, coldplay]},
            plan,
        )

        self.assertEqual(
            candidates[0]["id"],
            "1dc4c347-a1db-32aa-b14f-bc9cc507b843",
        )
        self.assertEqual(candidates[0]["name"], "Parachutes")

    def test_exact_title_with_by_still_beats_artist_interpretation(self):
        plan = discovery._track_search_plan("Stand By Me")
        recordings = [
            self._recording(
                "qualified",
                "Stand",
                score=100,
                artist="Me",
            ),
            self._recording(
                "literal",
                "Stand By Me",
                score=60,
                artist="Ben E. King",
            ),
        ]

        candidates = discovery._recording_release_group_candidates(
            {"recordings": recordings},
            plan,
        )

        self.assertEqual(candidates[0]["id"], "group-literal")

    @patch(
        "backend.routes.discovery._recording_release_group_results",
        return_value=[],
    )
    @patch("backend.routes.discovery.musicbrainz.search")
    def test_track_route_uses_the_structured_plan_without_parser_lookup(
        self,
        search,
        release_group_results,
    ):
        search.return_value = {"recordings": [self._recording(
            "weeknd",
            "Blinding Lights",
            artist="The Weeknd",
        )]}
        app = Flask(__name__)
        with app.test_request_context(
            "/api/search?q=Blinding%20Lights%20-%20The%20Weeknd&type=track"
        ):
            response = discovery.search.__wrapped__()

        self.assertEqual(response.status_code, 200)
        search.assert_called_once()
        query, search_type = search.call_args.args
        self.assertEqual(search_type, "track")
        self.assertIn('recording:"Blinding Lights"', query)
        self.assertIn('artist:"The Weeknd"', query)
        self.assertEqual(
            search.call_args.kwargs,
            {"plain_search": False, "limit": 50},
        )
        plan = release_group_results.call_args.args[1]
        self.assertEqual(plan["title"], "Blinding Lights")
        self.assertEqual(plan["artist"], "The Weeknd")

    @patch(
        "backend.routes.discovery._recording_release_group_results",
        return_value=[],
    )
    @patch("backend.routes.discovery.musicbrainz.search")
    def test_space_only_artist_aware_track_route_fetches_50_candidates(
        self,
        search,
        release_group_results,
    ):
        search.return_value = {"recordings": []}
        app = Flask(__name__)
        with app.test_request_context(
            "/api/search?q=Yellow%20Coldplay&type=track"
        ):
            response = discovery.search.__wrapped__()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(search.call_count, 2)
        self.assertEqual(
            search.call_args_list[0].kwargs,
            {"plain_search": False, "limit": 50},
        )
        self.assertEqual(search.call_args_list[1].args[1], "album")
        self.assertEqual(
            search.call_args_list[1].kwargs,
            {"plain_search": False, "limit": 100},
        )
        plan = release_group_results.call_args.args[1]
        self.assertEqual(plan["artist"], "")
        self.assertIn(
            {"title": "Yellow", "artist": "Coldplay", "source": "inferred"},
            plan["interpretations"],
        )


class LocalTrackSearchIndexTests(unittest.TestCase):
    artist_mbid = "11111111-1111-4111-8111-111111111111"
    second_artist_mbid = "11111111-1111-4111-8111-222222222222"
    release_group_mbid = "22222222-2222-4222-8222-222222222222"
    recording_mbid = "33333333-3333-4333-8333-333333333333"
    release_mbid = "44444444-4444-4444-8444-444444444444"

    def setUp(self):
        api_cache.init_cache_db()
        track_search_index.initialize()
        with api_cache.cache_db() as connection:
            connection.execute("DELETE FROM track_search_artist_snapshot_tracks")
            connection.execute("DELETE FROM track_search_artist_snapshot_meta")
            connection.execute("DELETE FROM track_search_artist_names")
            connection.execute("DELETE FROM track_search_relations")
            connection.execute("DELETE FROM track_search_title_keys")
            connection.execute("DELETE FROM track_search_release_group_refs")
            connection.execute(
                "DELETE FROM api_cache WHERE cache_key LIKE 'track-search-test:%'"
            )
            connection.executemany(
                "DELETE FROM api_cache WHERE cache_key = ?",
                ((key,) for key in self._metadata_keys()),
            )

    def tearDown(self):
        with api_cache.cache_db() as connection:
            connection.execute("DELETE FROM track_search_artist_snapshot_tracks")
            connection.execute("DELETE FROM track_search_artist_snapshot_meta")
            connection.execute("DELETE FROM track_search_artist_names")
            connection.execute("DELETE FROM track_search_relations")
            connection.execute("DELETE FROM track_search_title_keys")
            connection.execute("DELETE FROM track_search_release_group_refs")
            connection.execute(
                "DELETE FROM api_cache WHERE cache_key LIKE 'track-search-test:%'"
            )
            connection.executemany(
                "DELETE FROM api_cache WHERE cache_key = ?",
                ((key,) for key in self._metadata_keys()),
            )

    def _metadata_keys(self):
        return (
            musicbrainz.metadata_cache_key(
                f"/artist/{self.artist_mbid}",
                "aliases+url-rels+genres",
            ),
            musicbrainz.metadata_cache_key(
                "/release-group",
                "aliases",
                artist=self.artist_mbid,
                limit=100,
                offset=0,
            ),
            musicbrainz.metadata_cache_key(
                f"/release/{self.release_mbid}",
                "recordings+artist-credits",
            ),
        )

    def _artist(self, mbid=None, name="羊文学", aliases=None):
        return {
            "id": mbid or self.artist_mbid,
            "name": name,
            "sort-name": name,
            "aliases": aliases or [{"name": "Hitsuji Bungaku"}],
        }

    def _group(self):
        artist = self._artist()
        return {
            "id": self.release_group_mbid,
            "title": "12 hugs (like butterflies)",
            "first-release-date": "2023-12-06",
            "primary-type": "Album",
            "secondary-types": [],
            "artist-credit": [{"name": "羊文学", "artist": artist}],
        }

    def _release(self):
        group = self._group()
        return {
            "id": self.release_mbid,
            "title": "12 hugs (like butterflies)",
            "release-group": group,
            "artist-credit": group["artist-credit"],
            "media": [{"tracks": [{
                "title": "more than words",
                "artist-credit": group["artist-credit"],
                "recording": {
                    "id": self.recording_mbid,
                    "title": "more than words",
                    "artist-credit": group["artist-credit"],
                },
            }]}],
        }

    def _seed_complete_index(self):
        artist = self._artist()
        group = self._group()
        page = {"release-groups": [group], "release-group-count": 1}
        api_cache.set_cache_document(
            "track-search-test",
            "groups",
            page,
            3600,
        )
        cache_key = api_cache.document_cache_key(
            "track-search-test",
            "groups",
        )
        track_search_index.index_artist(artist)
        track_search_index.index_release_group_page(page, cache_key)
        release = self._release()
        track_search_index.index_release(release, cache_key)
        track_search_index.index_release(release, cache_key)

    def test_rebuild_imports_only_compact_identity_and_pointer_rows(self):
        artist = self._artist()
        page = {
            "release-groups": [self._group()],
            "release-group-count": 1,
        }
        commit_json_responses([
            musicbrainz.metadata_cache_record(
                f"/artist/{self.artist_mbid}",
                "aliases+url-rels+genres",
                artist,
            ),
            musicbrainz.metadata_cache_record(
                "/release-group",
                "aliases",
                page,
                artist=self.artist_mbid,
                limit=100,
                offset=0,
            ),
            musicbrainz.metadata_cache_record(
                f"/release/{self.release_mbid}",
                "recordings+artist-credits",
                self._release(),
            ),
        ])

        stats = track_search_index.rebuild_from_cache()

        self.assertEqual(stats["trackRelationRows"], 1)
        self.assertGreaterEqual(stats["artistNameRows"], 3)
        self.assertGreaterEqual(stats["releaseGroupRefRows"], 1)
        with api_cache.cache_db() as connection:
            relation_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(track_search_relations)"
                )
            }
        self.assertEqual(relation_columns, {
            "normalized_title",
            "artist_mbid",
            "recording_mbid",
            "release_group_mbid",
            "source_mask",
        })

    def test_schema_upgrade_backfills_tracks_from_cached_releases(self):
        legacy_release = self._release()
        legacy_release.pop("release-group")
        commit_json_responses([
            musicbrainz.metadata_cache_record(
                "/release-group",
                "aliases",
                {"release-groups": [self._group()]},
                artist=self.artist_mbid,
                limit=100,
                offset=0,
            ),
            musicbrainz.metadata_cache_record(
                f"/release/{self.release_mbid}",
                "recordings+artist-credits",
                legacy_release,
            ),
        ])
        with api_cache.cache_db() as connection:
            connection.execute("DELETE FROM track_search_relations")
            connection.execute(
                "INSERT OR REPLACE INTO track_search_meta (key, value) "
                "VALUES ('schema-version', '3')"
            )

        with patch.object(track_search_index, "_initialized", False):
            track_search_index.initialize()

        matches = track_search_index.search_artist_tracks(
            self.artist_mbid,
            "more than words",
        )
        self.assertEqual(
            [match["release_group_mbid"] for match in matches],
            [self.release_group_mbid],
        )

    def test_cached_release_can_backfill_a_missing_track_relation(self):
        cache_key = musicbrainz.metadata_cache_key(
            f"/release/{self.release_mbid}",
            "recordings+artist-credits",
        )
        legacy_release = self._release()
        legacy_release.pop("release-group")
        track_search_index.index_release_group_page(
            {"release-groups": [self._group()]},
            "track-search-test:groups",
        )
        commit_json_responses([
            musicbrainz.metadata_cache_record(
                f"/release/{self.release_mbid}",
                "recordings+artist-credits",
                legacy_release,
            ),
        ])

        self.assertTrue(track_search_index.index_cached_release(cache_key))
        self.assertEqual(
            track_search_index.search_artist_tracks(
                self.artist_mbid,
                "more than words",
            )[0]["release_group_mbid"],
            self.release_group_mbid,
        )

    def test_artist_track_lookup_returns_every_matching_release_group(self):
        self._seed_complete_index()
        single_group_id = "55555555-5555-4555-8555-555555555555"
        single = self._release()
        single["id"] = "66666666-6666-4666-8666-666666666666"
        single["release-group"] = {
            **self._group(),
            "id": single_group_id,
            "title": "more than words",
            "primary-type": "Single",
        }
        track_search_index.index_release(single, "track-search-test:single")

        matches = track_search_index.search_artist_tracks(
            self.artist_mbid,
            "more than words",
        )

        self.assertEqual(
            {match["release_group_mbid"] for match in matches},
            {self.release_group_mbid, single_group_id},
        )
        self.assertEqual(
            track_search_index.search_artist_tracks(self.artist_mbid, "more words"),
            [],
        )
        self.assertEqual(
            track_search_index.search_artist_tracks(
                self.second_artist_mbid,
                "more than words",
            ),
            [],
        )

    def test_artist_track_lookup_matches_contiguous_phrase_in_both_indexes(self):
        releases = []
        for number, title in enumerate(
            ("Calling My Name", "SMALL TOWN FAME", "Call Me", "All Me"),
            start=1,
        ):
            release = self._release()
            release["id"] = f"44444444-4444-4444-8444-{number:012d}"
            release["release-group"]["id"] = (
                f"22222222-2222-4222-8222-{number:012d}"
            )
            release["title"] = f"Phrase regression {number}"
            release["release-group"]["title"] = release["title"]
            track = release["media"][0]["tracks"][0]
            track["title"] = title
            track["recording"]["title"] = title
            track["recording"]["id"] = (
                f"33333333-3333-4333-8333-{number:012d}"
            )
            releases.append(release)
            track_search_index.index_release(
                release, f"track-search-test:phrase:{number}",
            )

        expected_group = releases[-1]["release-group"]["id"]
        for has_snapshot in (False, True):
            with self.subTest(has_snapshot=has_snapshot):
                if has_snapshot:
                    track_search_index.replace_artist_track_snapshot(
                        self.artist_mbid, releases,
                        release_count=4, recording_count=4,
                    )
                for query in ("All me", "all m"):
                    self.assertEqual(
                        [match["release_group_mbid"] for match in
                         track_search_index.search_artist_tracks(
                             self.artist_mbid, query,
                         )],
                        [expected_group],
                    )
                self.assertEqual(
                    track_search_index.search_artist_tracks(
                        self.artist_mbid, "me all",
                    ),
                    [],
                )

    def test_artist_recording_fallback_requires_contiguous_phrase(self):
        recordings = []
        for number, title in enumerate(
            ("Calling My Name", "SMALL TOWN FAME", "Call Me", "All Me"),
            start=1,
        ):
            recordings.append({
                "title": title,
                "artist-credit": self._group()["artist-credit"],
                "releases": [{"release-group": {
                    "id": f"22222222-2222-4222-8222-{number:012d}",
                }}],
            })

        self.assertEqual(
            track_search_index.recording_search_matches(
                {"recordings": recordings}, self.artist_mbid, "All me",
            ),
            [{
                "normalized_title": "all me",
                "release_group_mbid": "22222222-2222-4222-8222-000000000004",
            }],
        )

    def test_recording_search_populates_track_release_group_relations(self):
        group = self._group()
        recording = {
            "id": self.recording_mbid,
            "title": "more than words",
            "artist-credit": group["artist-credit"],
            "releases": [{
                "id": self.release_mbid,
                "title": group["title"],
                "release-group": group,
            }],
        }

        track_search_index.index_recording_search({"recordings": [recording]})

        matches = track_search_index.search_artist_tracks(
            self.artist_mbid,
            "more than words",
        )
        self.assertEqual(
            [match["release_group_mbid"] for match in matches],
            [self.release_group_mbid],
        )

    def test_romanized_track_and_recording_alias_find_canonical_title(self):
        release = self._release()
        track = release["media"][0]["tracks"][0]
        track["title"] = "幸せ。"
        track["recording"]["title"] = "幸せ。"
        track["recording"]["aliases"] = [{"name": "Happiness"}]
        track_search_index.index_release(release, "track-search-test:shiawase")

        for query in ("幸せ", "shiawase", "Happiness"):
            with self.subTest(query=query):
                matches = track_search_index.search_artist_tracks(
                    self.artist_mbid, query,
                )
                self.assertEqual(matches, [{
                    "normalized_title": "幸せ",
                    "release_group_mbid": self.release_group_mbid,
                }])
        self.assertEqual(
            track_search_index.exact_track_matches(
                self.artist_mbid, "shiawase",
            )[0]["normalized_title"],
            "幸せ",
        )
        self.assertEqual(
            track_search_index.search_artist_tracks(
                self.second_artist_mbid, "shiawase",
            ),
            [],
        )

    def test_version_five_migration_preserves_uncached_track_relations(self):
        release = self._release()
        track = release["media"][0]["tracks"][0]
        track["title"] = "幸せ。"
        track["recording"]["title"] = "幸せ。"
        track_search_index.index_release(release, "")
        with api_cache.cache_db() as connection:
            connection.execute("DELETE FROM track_search_title_keys")
            connection.execute(
                "UPDATE track_search_meta SET value = '5' "
                "WHERE key = 'schema-version'"
            )

        with patch.object(track_search_index, "_initialized", False):
            track_search_index.initialize()

        matches = track_search_index.search_artist_tracks(
            self.artist_mbid, "shiawase",
        )
        self.assertEqual(
            [match["release_group_mbid"] for match in matches],
            [self.release_group_mbid],
        )

    def test_cached_group_uses_compact_metadata_after_source_cache_expires(self):
        self._seed_complete_index()
        cache_key = api_cache.document_cache_key(
            "track-search-test",
            "groups",
        )
        with api_cache.cache_db() as connection:
            connection.execute(
                "DELETE FROM api_cache WHERE cache_key = ?",
                (cache_key,),
            )

        groups = track_search_index.cached_release_groups([
            self.release_group_mbid,
        ])

        self.assertEqual(
            groups[self.release_group_mbid]["title"],
            "12 hugs (like butterflies)",
        )
        self.assertEqual(groups[self.release_group_mbid]["primary-type"], "Album")
        self.assertEqual(
            groups[self.release_group_mbid]["artist-credit"][0]["artist"]["id"],
            self.artist_mbid,
        )

    @patch("backend.routes.discovery.musicbrainz.search")
    def test_transliterated_and_canonical_queries_use_zero_musicbrainz_calls(
        self,
        search,
    ):
        self._seed_complete_index()
        app = Flask(__name__)
        queries = (
            "more%20than%20words%20Hitsujibungaku",
            "more%20than%20words%20Hitsuji%20Bungaku",
            "more%20than%20words%20%E7%BE%8A%E6%96%87%E5%AD%A6",
        )
        responses = []
        for query in queries:
            with app.test_request_context(f"/api/search?q={query}&type=track"):
                responses.append(discovery.search.__wrapped__().get_json())

        self.assertEqual(
            [payload["results"][0]["id"] for payload in responses],
            [self.release_group_mbid] * 3,
        )
        self.assertEqual(
            responses[0]["results"][0]["name"],
            "12 hugs (like butterflies)",
        )
        self.assertTrue(all(payload["source"] == "local" for payload in responses))
        search.assert_not_called()
        stats = track_search_index.stats()
        self.assertEqual(stats["trackRelationRows"], 1)

    @patch("backend.routes.discovery._has_strong_recording_match", return_value=True)
    @patch("backend.routes.discovery.musicbrainz.search")
    def test_explicit_musicbrainz_track_search_bypasses_local_hit(
        self, search, strong_match
    ):
        self._seed_complete_index()
        remote_group = {
            **self._group(),
            "id": "55555555-5555-4555-8555-555555555555",
            "title": "More Than Words (Live)",
        }
        remote_recording = {
            "id": "66666666-6666-4666-8666-666666666666",
            "title": "more than words",
            "artist-credit": remote_group["artist-credit"],
            "releases": [{
                "id": "77777777-7777-4777-8777-777777777777",
                "title": remote_group["title"],
                "release-group": remote_group,
                "artist-credit": remote_group["artist-credit"],
            }],
        }
        search.side_effect = [
            {"recordings": [remote_recording]},
            {"release-groups": [remote_group]},
        ]
        app = Flask(__name__)
        query = "more%20than%20words%20Hitsujibungaku"
        with app.test_request_context(f"/api/search?q={query}&type=track"):
            local = discovery.search.__wrapped__()
        with app.test_request_context(
            f"/api/search?q={query}&type=track&musicbrainz=1"
        ):
            direct = discovery.search.__wrapped__()

        self.assertEqual(local.get_json()["source"], "local")
        self.assertEqual(local.get_json()["results"][0]["id"], self.release_group_mbid)
        self.assertEqual(direct.status_code, 200)
        self.assertEqual(direct.get_json()["source"], "musicbrainz")
        self.assertEqual(direct.get_json()["results"][0]["id"], remote_group["id"])
        self.assertEqual([call.args[1] for call in search.call_args_list], ["track", "album"])
        self.assertIn(f"arid:{self.artist_mbid}", search.call_args_list[0].args[0])
        strong_match.assert_called_once()

    @patch(
        "backend.routes.discovery._recording_release_group_results",
        return_value=[],
    )
    @patch("backend.routes.discovery.musicbrainz.search")
    def test_ambiguous_alias_is_not_guessed(self, search, release_results):
        track_search_index.index_artist(self._artist())
        track_search_index.index_artist(self._artist(
            self.second_artist_mbid,
            name="Hitsujibungaku",
            aliases=[],
        ))
        search.side_effect = [
            {"recordings": []},
            {"release-groups": []},
            {"artists": [self._artist(), self._artist(self.second_artist_mbid)]},
        ]
        app = Flask(__name__)
        with app.test_request_context(
            "/api/search?q=more%20than%20words%20Hitsujibungaku&type=track"
        ):
            response = discovery.search.__wrapped__()

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("arid:", search.call_args_list[0].args[0])
        self.assertEqual(len(search.call_args_list), 3)
        self.assertEqual(search.call_args_list[1].args[1], "album")
        self.assertEqual(search.call_args_list[2].args[1], "artist")
        release_results.assert_called_once()

    @patch(
        "backend.routes.discovery._recording_release_group_results",
        return_value=[],
    )
    @patch("backend.routes.discovery.musicbrainz.search")
    def test_incomplete_local_track_index_falls_back_with_resolved_artist(
        self,
        search,
        release_results,
    ):
        track_search_index.index_artist(self._artist())
        search.return_value = {"recordings": []}
        app = Flask(__name__)
        with app.test_request_context(
            "/api/search?q=more%20than%20words%20Hitsujibungaku&type=track"
        ):
            response = discovery.search.__wrapped__()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(search.call_count, 2)
        self.assertIn(f"arid:{self.artist_mbid}", search.call_args_list[0].args[0])
        self.assertEqual(search.call_args_list[0].args[1], "track")
        self.assertIn(f"arid:{self.artist_mbid}", search.call_args_list[1].args[0])
        self.assertEqual(search.call_args_list[1].args[1], "album")
        release_results.assert_called_once()

    @patch(
        "backend.routes.discovery._recording_release_group_results",
        return_value=[],
    )
    @patch("backend.routes.discovery.musicbrainz.search")
    def test_alias_fallback_is_cached_for_the_next_search(
        self,
        search,
        release_results,
    ):
        search.side_effect = [
            {"recordings": []},
            {"release-groups": []},
            {"artists": [self._artist()]},
            {"recordings": []},
        ]
        app = Flask(__name__)
        url = "/api/search?q=more%20than%20words%20Hitsujibungaku&type=track"
        with app.test_request_context(url):
            first = discovery.search.__wrapped__()

        self.assertEqual(first.status_code, 200)
        self.assertEqual(len(search.call_args_list), 4)
        self.assertEqual(search.call_args_list[1].args[1], "album")
        self.assertEqual(search.call_args_list[2].args[1], "artist")

        search.reset_mock()
        search.side_effect = None
        search.return_value = {"recordings": []}
        with app.test_request_context(url):
            second = discovery.search.__wrapped__()

        self.assertEqual(second.status_code, 200)
        self.assertEqual(search.call_count, 2)
        self.assertIn(f"arid:{self.artist_mbid}", search.call_args_list[0].args[0])
        self.assertIn(f"arid:{self.artist_mbid}", search.call_args_list[1].args[0])
        release_results.assert_called()


class LocalAlbumSearchIndexTests(unittest.TestCase):
    coldplay_mbid = "11111111-1111-4111-8111-111111111111"
    hitsuji_mbid = "22222222-2222-4222-8222-222222222222"
    ludwig_mbid = "33333333-3333-4333-8333-333333333333"
    other_mbid = "44444444-4444-4444-8444-444444444444"

    def setUp(self):
        api_cache.init_cache_db()
        track_search_index.initialize()
        with api_cache.cache_db() as connection:
            connection.execute("DELETE FROM track_search_artist_discography_groups")
            connection.execute("DELETE FROM track_search_release_group_artists")
            connection.execute("DELETE FROM track_search_release_groups")
            connection.execute("DELETE FROM track_search_artist_names")

    def tearDown(self):
        with api_cache.cache_db() as connection:
            connection.execute("DELETE FROM track_search_artist_discography_groups")
            connection.execute("DELETE FROM track_search_release_group_artists")
            connection.execute("DELETE FROM track_search_release_groups")
            connection.execute("DELETE FROM track_search_artist_names")

    @staticmethod
    def _artist(mbid, name, aliases=None):
        return {
            "id": mbid,
            "name": name,
            "sort-name": name,
            "aliases": aliases or [],
        }

    def _group(
        self,
        mbid,
        title,
        artist,
        *,
        primary_type="Album",
        secondary_types=None,
        date="2020-01-01",
    ):
        return {
            "id": mbid,
            "title": title,
            "first-release-date": date,
            "primary-type": primary_type,
            "secondary-types": secondary_types or [],
            "artist-credit": [{"name": artist["name"], "artist": artist}],
        }

    def _search(self, query, *, musicbrainz=False):
        app = Flask(__name__)
        direct = "&musicbrainz=1" if musicbrainz else ""
        with app.test_request_context(f"/api/search?q={query}&type=album{direct}"):
            return discovery.search.__wrapped__()

    @patch("backend.routes.discovery.musicbrainz.search")
    def test_exact_local_album_uses_no_musicbrainz_request(self, search):
        coldplay = self._artist(self.coldplay_mbid, "Coldplay")
        group = self._group(
            "1dc4c347-a1db-32aa-b14f-bc9cc507b843",
            "Parachutes",
            coldplay,
            date="2000-07-10",
        )
        track_search_index.index_release_groups(
            [group],
            source_mask=track_search_index.SOURCE_LIDARR,
        )

        response = self._search("Parachutes")

        self.assertEqual(response.status_code, 200)
        result = response.get_json()["results"][0]
        self.assertEqual(result["id"], group["id"])
        self.assertEqual(result["artist"], "Coldplay")
        self.assertEqual(response.get_json()["source"], "local")
        self.assertEqual(
            result["coverArt"],
            f"/api/artwork/release-group/{group['id']}?size=thumb",
        )
        search.assert_not_called()

    @patch("backend.routes.discovery.artist_metadata_worker.request_revalidation")
    @patch("backend.routes.discovery.musicbrainz.search")
    def test_explicit_musicbrainz_search_bypasses_local_hit(
        self, search, request_revalidation
    ):
        coldplay = self._artist(self.coldplay_mbid, "Coldplay")
        other = self._artist(self.other_mbid, "Other Artist")
        local = self._group(
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "Parachutes",
            coldplay,
        )
        remote = self._group(
            "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            "Parachutes",
            other,
        )
        track_search_index.index_release_groups(
            [local], source_mask=track_search_index.SOURCE_LIDARR
        )
        search.return_value = {"release-groups": [remote]}

        initial = self._search("Parachutes%20Coldplay")
        direct = self._search("Parachutes%20Coldplay", musicbrainz=True)

        self.assertEqual(initial.get_json()["source"], "local")
        self.assertEqual(direct.status_code, 200)
        self.assertEqual(direct.get_json()["source"], "musicbrainz")
        self.assertEqual(direct.get_json()["results"][0]["id"], remote["id"])
        search.assert_called_once()
        self.assertEqual(search.call_args.args[1], "album")
        request_revalidation.assert_called_once_with(self.coldplay_mbid)

    @patch("backend.routes.discovery.artist_metadata_worker.request_revalidation")
    @patch("backend.routes.discovery.musicbrainz.search")
    def test_artist_qualified_and_romanized_album_searches_are_local(
        self, search, request_revalidation
    ):
        hitsuji = self._artist(
            self.hitsuji_mbid,
            "羊文学",
            aliases=[{"name": "Hitsuji Bungaku", "locale": "en"}],
        )
        group = self._group(
            "55555555-5555-4555-8555-555555555555",
            "12 hugs (like butterflies)",
            hitsuji,
            date="2023-12-06",
        )
        track_search_index.index_release_groups([group])

        spaced = self._search("12%20hugs%20like%20butterflies%20Hitsujibungaku")
        canonical = self._search(
            "12%20hugs%20like%20butterflies%20%E7%BE%8A%E6%96%87%E5%AD%A6"
        )

        self.assertEqual(spaced.get_json()["results"][0]["id"], group["id"])
        self.assertEqual(canonical.get_json()["results"][0]["id"], group["id"])
        search.assert_not_called()
        self.assertEqual(request_revalidation.call_count, 2)
        request_revalidation.assert_any_call(self.hitsuji_mbid)

    @patch("backend.routes.discovery.musicbrainz.search")
    def test_soundtrack_intent_requires_matching_local_metadata(self, search):
        ludwig = self._artist(self.ludwig_mbid, "Ludwig Göransson")
        generic = self._group(
            "66666666-6666-4666-8666-666666666666",
            "The Odyssey",
            ludwig,
        )
        soundtrack = self._group(
            "77777777-7777-4777-8777-777777777777",
            "The Odyssey: Original Motion Picture Soundtrack",
            ludwig,
            secondary_types=["Soundtrack"],
            date="2026-03-20",
        )
        track_search_index.index_release_groups([generic])
        search.return_value = {"release-groups": []}

        fallback = self._search("The%20Odyssey%20soundtrack")

        self.assertEqual(fallback.status_code, 200)
        search.assert_called_once()

        search.reset_mock()
        track_search_index.index_release_groups(
            [soundtrack],
            source_mask=track_search_index.SOURCE_LIDARR,
        )
        local = self._search("The%20Odyssey%20soundtrack")

        self.assertEqual(local.get_json()["results"][0]["id"], soundtrack["id"])
        search.assert_not_called()

    @patch("backend.routes.discovery.musicbrainz.search")
    def test_weak_and_unknown_local_album_matches_fall_back(self, search):
        coldplay = self._artist(self.coldplay_mbid, "Coldplay")
        track_search_index.index_release_groups([self._group(
            "88888888-8888-4888-8888-888888888888",
            "Parachutes Live Sessions",
            coldplay,
        )])
        search.return_value = {"release-groups": []}

        weak = self._search("Parachutes")
        unknown = self._search("Unknown%20Album")

        self.assertEqual(weak.status_code, 200)
        self.assertEqual(unknown.status_code, 200)
        self.assertEqual(search.call_count, 2)

    @patch("backend.routes.discovery.musicbrainz.search")
    def test_ambiguous_artist_alias_falls_back_instead_of_guessing(self, search):
        first = self._artist(
            self.hitsuji_mbid,
            "羊文学",
            aliases=[{"name": "Hitsujibungaku"}],
        )
        second = self._artist(
            self.other_mbid,
            "Hitsujibungaku",
            aliases=[{"name": "Hitsujibungaku"}],
        )
        track_search_index.index_release_groups([self._group(
            "99999999-9999-4999-8999-999999999999",
            "12 hugs (like butterflies)",
            first,
        )])
        track_search_index.index_artist(second)
        search.return_value = {"release-groups": []}

        response = self._search(
            "12%20hugs%20like%20butterflies%20Hitsujibungaku"
        )

        self.assertEqual(response.status_code, 200)
        search.assert_called_once()

    @patch("backend.routes.discovery.musicbrainz.search")
    def test_remote_album_candidates_enable_specific_local_followup(self, search):
        coldplay = self._artist(self.coldplay_mbid, "Coldplay")
        group = self._group(
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "A Rush of Blood to the Head",
            coldplay,
        )
        search.return_value = {"release-groups": [group]}

        first = self._search("A%20Rush%20of%20Blood%20to%20the%20Head")
        self.assertEqual(first.status_code, 200)
        search.assert_called_once()

        search.reset_mock()
        second = self._search(
            "A%20Rush%20of%20Blood%20to%20the%20Head%20Coldplay"
        )

        self.assertEqual(second.get_json()["results"][0]["id"], group["id"])
        search.assert_not_called()
        self.assertEqual(track_search_index.stats()["releaseGroupRows"], 1)

    @patch("backend.routes.discovery.musicbrainz.search")
    def test_generic_title_does_not_short_circuit_from_local_rows(self, search):
        artist = self._artist(self.coldplay_mbid, "Example Artist")
        track_search_index.index_release_groups(
            [self._group(
                "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                "Greatest Hits",
                artist,
            )],
            source_mask=track_search_index.SOURCE_LIDARR,
        )
        search.return_value = {"release-groups": []}

        response = self._search("Greatest%20Hits")

        self.assertEqual(response.status_code, 200)
        search.assert_called_once()

    @patch(
        "backend.routes.discovery.artist_metadata_worker.discography_is_fresh",
        return_value=True,
    )
    @patch("backend.routes.discovery.musicbrainz.search")
    def test_fresh_complete_discography_allows_distinctive_title_fast_path(
        self, search, discography_is_fresh
    ):
        coldplay = self._artist(self.coldplay_mbid, "Coldplay")
        group = self._group(
            "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
            "Parachutes",
            coldplay,
        )
        track_search_index.replace_musicbrainz_artist_discography(
            self.coldplay_mbid,
            [({"release-groups": [group]}, "discography-page")],
        )

        response = self._search("Parachutes")

        self.assertEqual(response.get_json()["results"][0]["id"], group["id"])
        search.assert_not_called()
        discography_is_fresh.assert_called_once_with(self.coldplay_mbid)

    @patch(
        "backend.routes.discovery.artist_metadata_worker.discography_is_fresh",
        return_value=False,
    )
    @patch("backend.routes.discovery.musicbrainz.search")
    def test_stale_discography_title_falls_back_to_musicbrainz(
        self, search, discography_is_fresh
    ):
        coldplay = self._artist(self.coldplay_mbid, "Coldplay")
        track_search_index.replace_musicbrainz_artist_discography(
            self.coldplay_mbid,
            [({"release-groups": [self._group(
                "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
                "Parachutes",
                coldplay,
            )]}, "discography-page")],
        )
        search.return_value = {"release-groups": []}

        response = self._search("Parachutes")

        self.assertEqual(response.status_code, 200)
        search.assert_called_once()
        discography_is_fresh.assert_called_once_with(self.coldplay_mbid)

    @patch("backend.routes.discovery.musicbrainz.search")
    def test_album_query_length_is_bounded(self, search):
        response, status = self._search("a" * 201)

        self.assertEqual(status, 400)
        self.assertIn("200 characters", response.get_json()["error"])
        search.assert_not_called()

    def test_artist_discography_replacement_prunes_only_musicbrainz_rows(self):
        artist = self._artist(self.coldplay_mbid, "Coldplay")
        retained = self._group(
            "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
            "Retained",
            artist,
        )
        removed = self._group(
            "ffffffff-ffff-4fff-8fff-ffffffffffff",
            "Removed",
            artist,
        )
        replacement = self._group(
            "12121212-1212-4212-8212-121212121212",
            "Replacement",
            artist,
        )
        track_search_index.replace_musicbrainz_artist_discography(
            self.coldplay_mbid,
            [({"release-groups": [retained, removed]}, "old-page")],
        )
        track_search_index.index_release_groups(
            [removed],
            source_mask=track_search_index.SOURCE_LIDARR,
        )

        track_search_index.replace_musicbrainz_artist_discography(
            self.coldplay_mbid,
            [({"release-groups": [retained, replacement]}, "new-page")],
            obsolete_cache_keys={"old-page"},
        )

        self.assertEqual(
            [group["id"] for group in track_search_index.search_release_groups(
                "Retained"
            )],
            [retained["id"]],
        )
        self.assertEqual(
            [group["id"] for group in track_search_index.search_release_groups(
                "Replacement"
            )],
            [replacement["id"]],
        )
        preserved = track_search_index.search_release_groups("Removed")
        self.assertEqual([group["id"] for group in preserved], [removed["id"]])
        self.assertEqual(
            preserved[0]["sourceMask"],
            track_search_index.SOURCE_LIDARR,
        )
        self.assertEqual(
            track_search_index.stats()["artistDiscographyRows"],
            2,
        )

    def test_compact_index_deduplicates_and_removes_stale_lidarr_groups(self):
        payload = {
            "artists": {
                self.coldplay_mbid: {"name": "Coldplay"},
            },
            "albums": {
                "1dc4c347-a1db-32aa-b14f-bc9cc507b843": {
                    "title": "Parachutes",
                    "artistMbid": self.coldplay_mbid,
                    "artistName": "Coldplay",
                    "type": "Album",
                    "releaseDate": "2000-07-10",
                },
            },
        }
        track_search_index.index_lidarr_library(payload)
        track_search_index.index_lidarr_library(payload)

        stats = track_search_index.stats()
        self.assertEqual(stats["releaseGroupRows"], 1)
        self.assertEqual(stats["releaseGroupArtistRows"], 1)
        with api_cache.cache_db() as connection:
            columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(track_search_release_groups)"
                )
            }
        self.assertNotIn("artwork", columns)
        self.assertNotIn("payload", columns)

        track_search_index.index_lidarr_library({"artists": {}, "albums": {}})

        self.assertEqual(track_search_index.stats()["releaseGroupRows"], 0)


class DiscoveryRoutesTests(DatabaseTestCase):
    @patch("backend.routes.discovery.musicbrainz.search")
    def test_musicbrainz_503_reports_an_unavailable_search_service(self, search):
        failure_response = Response(503, {
            "error": "Could not retrieve results from search:8983",
        })
        error = requests.HTTPError("HTTP 503")
        error.response = failure_response
        search.side_effect = error

        response = self.client.get(
            "/api/search?q=3%20doors%20down&type=artist",
            headers={"X-CSRF-Token": self.register()},
        )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.get_json()["error"],
            musicbrainz.SEARCH_UNAVAILABLE_MESSAGE,
        )
        self.assertNotIn("search:8983", response.get_data(as_text=True))

    @patch("backend.routes.discovery.animethemes.search")
    def test_anime_search_uses_the_anime_provider(self, search):
        search.return_value = [{
            "id": 2028,
            "slug": "naruto",
            "name": "Naruto",
            "year": 2002,
            "season": "Fall",
            "format": "TV",
            "coverArt": "",
        }]

        response = self.client.get(
            "/api/search?q=naruto&type=anime",
            headers={"X-CSRF-Token": self.register()},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {
            "results": search.return_value,
            "type": "anime",
        })
        search.assert_called_once_with("naruto")

    @patch("backend.routes.discovery.animethemes.search")
    def test_anime_search_does_not_reflect_provider_exception(self, search):
        search.side_effect = ValueError("sentinel-secret-provider-detail")
        response = self.client.get(
            "/api/search?q=naruto&type=anime",
            headers={"X-CSRF-Token": self.register()},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.get_json(), {"error": "AnimeThemes search could not be completed."}
        )

    @patch("backend.routes.discovery.plex.cached_library_index")
    @patch("backend.routes.discovery.get_service")
    @patch("backend.routes.discovery.musicbrainz.search")
    def test_artist_search_uses_the_detail_page_plexamp_link(
        self, search, get_service, plex_index
    ):
        artist_id = "b16c0872-31a7-4db9-8569-0e3146fcecfc"
        plexamp_url = (
            "https://listen.plex.tv/artist/635398caff6cd5445df237ef?"
            "source=server-1&key=%2Flibrary%2Fmetadata%2F214575"
        )
        search.return_value = {"artists": [{"id": artist_id, "name": "Ella Langley"}]}
        get_service.return_value = {"url": "http://plex", "token": "token"}
        plex_index.return_value = {"artistsByMbid": {
            artist_id: {
                "url": "https://app.plex.tv/artist",
                "plexampUrl": plexamp_url,
                "plexGuid": "plex://artist/635398caff6cd5445df237ef",
                "guids": [f"mbid://{artist_id}"],
                "key": "/library/metadata/214575",
            }
        }}

        response = self.client.get(
            "/api/search?q=ella%20langley&type=artist",
            headers={"X-CSRF-Token": self.register()},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["results"][0]["plex"]["plexampUrl"], plexamp_url)

    @patch("backend.routes.discovery.musicbrainz.search")
    def test_artist_search_returns_english_alias_with_canonical_name(self, search):
        search.return_value = {"artists": [{
            "id": "0f0caf6e-e815-4ad3-93db-fb37be9adcc8",
            "name": "ポルカドットスティングレイ",
            "sort-name": "POLKADOT STINGRAY",
            "aliases": [{
                "name": "POLKADOT STINGRAY",
                "locale": "en",
                "primary": True,
            }],
        }]}

        response = self.client.get(
            "/api/search?q=polkadot%20stingray&type=artist",
            headers={"X-CSRF-Token": self.register()},
        )

        artist = response.get_json()["results"][0]
        self.assertEqual(artist["name"], "ポルカドットスティングレイ")
        self.assertEqual(artist["romanizedName"], "POLKADOT STINGRAY")

    @patch("backend.routes.discovery.musicbrainz.search")
    def test_album_search_returns_english_alias_with_canonical_title(self, search):
        search.return_value = {"release-groups": [{
            "id": "42ef0ba4-111c-4b2b-86f2-a5831d72244e",
            "title": "極彩",
            "aliases": [{"name": "In Full Color", "locale": "en"}],
            "artist-credit": [],
        }]}

        response = self.client.get(
            "/api/search?q=full%20color&type=album",
            headers={"X-CSRF-Token": self.register()},
        )

        album = response.get_json()["results"][0]
        self.assertEqual(album["name"], "極彩")
        self.assertEqual(album["romanizedTitle"], "In Full Color")

    @patch("backend.routes.discovery.musicbrainz.search")
    def test_track_search_ranks_and_deduplicates_release_groups(self, search):
        artist_credit = [{"name": "Example Artist"}]
        search.side_effect = [
            {
                "recordings": [
                    {
                        "id": "recording-studio",
                        "score": "100",
                        "title": "Example Song",
                        "artist-credit": artist_credit,
                        "first-release-date": "2020-01-01",
                        "releases": [
                            {
                                "id": "release-compilation",
                                "title": "Big Compilation",
                                "status": "Official",
                                "date": "2020-06-01",
                                "artist-credit": [{"name": "Various Artists"}],
                                "release-group": {
                                    "id": "group-compilation",
                                    "primary-type": "Album",
                                    "secondary-types": ["Compilation"],
                                },
                            },
                            {
                                "id": "release-promo",
                                "title": "Example Song (Promo)",
                                "status": "Promotion",
                                "date": "2019-12-01",
                                "release-group": {
                                    "id": "group-single",
                                    "primary-type": "Single",
                                },
                            },
                            {
                                "id": "release-official",
                                "title": "Example Song (Digital Edition)",
                                "status": "Official",
                                "date": "2020-01-01",
                                "release-group": {
                                    "id": "group-single",
                                    "primary-type": "Single",
                                },
                            },
                            {
                                "id": "release-without-group",
                                "title": "Incomplete metadata",
                            },
                        ],
                    },
                    {
                        "id": "recording-live",
                        "score": 90,
                        "title": "Example Song (Live)",
                        "artist-credit": artist_credit,
                        "releases": [{
                            "id": "release-live",
                            "title": "Example Song Live",
                            "status": "Official",
                            "date": "2021-01-01",
                            "release-group": {
                                "id": "group-live",
                                "primary-type": "Album",
                                "secondary-types": ["Live"],
                            },
                        }],
                    },
                ],
            },
            {
                "release-groups": [
                    {
                        "id": "group-compilation",
                        "title": "Canonical Compilation",
                        "artist-credit": [{"name": "Various Artists"}],
                        "first-release-date": "2020-06-01",
                        "primary-type": "Album",
                        "secondary-types": ["Compilation"],
                    },
                    {
                        "id": "group-single",
                        "title": "Canonical Example Song",
                        "artist-credit": artist_credit,
                        "first-release-date": "2020-01-01",
                        "primary-type": "Single",
                    },
                    {
                        "id": "group-live",
                        "title": "Canonical Live Album",
                        "artist-credit": artist_credit,
                        "first-release-date": "2021-01-01",
                        "primary-type": "Album",
                        "secondary-types": ["Live"],
                    },
                ],
            },
        ]

        response = self.client.get(
            "/api/search?q=example%20song&type=track",
            headers={"X-CSRF-Token": self.register()},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["type"], "track")
        self.assertEqual(
            [result["id"] for result in payload["results"]],
            ["group-single", "group-compilation", "group-live"],
        )
        self.assertEqual(
            payload["results"][0]["name"],
            "Canonical Example Song",
        )
        self.assertEqual(
            payload["results"][0]["matchedTrack"],
            "Example Song",
        )
        self.assertEqual(
            payload["results"][1]["secondaryTypes"],
            ["Compilation"],
        )
        recording_query, recording_type = search.call_args_list[0].args
        self.assertEqual(recording_type, "track")
        self.assertIn('recording:"example song"', recording_query)
        self.assertIn('recording:"example"', recording_query)
        self.assertIn('artist:"song"', recording_query)
        self.assertEqual(
            search.call_args_list[0].kwargs,
            {"plain_search": False, "limit": 50},
        )
        enrichment_query, enrichment_type = search.call_args_list[1].args
        self.assertEqual(enrichment_type, "album")
        self.assertEqual(search.call_args_list[1].kwargs, {})
        self.assertEqual(enrichment_query.count("rgid:group-single"), 1)
        self.assertEqual(enrichment_query.count("rgid:group-compilation"), 1)
        self.assertEqual(enrichment_query.count("rgid:group-live"), 1)

    @patch("backend.routes.discovery.musicbrainz.search")
    def test_track_search_caps_unique_release_groups(self, search):
        releases = [
            {
                "id": f"release-{index}",
                "title": f"Release {index}",
                "status": "Official",
                "date": f"2020-{(index % 12) + 1:02d}-01",
                "release-group": {
                    "id": f"group-{index}",
                    "primary-type": "Album",
                },
            }
            for index in range(30)
        ]
        search.side_effect = [
            {
                "recordings": [{
                    "id": "recording",
                    "score": 100,
                    "title": "Song",
                    "artist-credit": [{"name": "Artist"}],
                    "releases": releases,
                }],
            },
            {"release-groups": []},
        ]

        response = self.client.get(
            "/api/search?q=song&type=track",
            headers={"X-CSRF-Token": self.register()},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.get_json()["results"]), 25)


class MusicRoutesTests(DatabaseTestCase):
    @patch("backend.routes.music.musicbrainz.search")
    def test_complete_artist_track_snapshot_still_searches_musicbrainz_on_miss(
        self, search,
    ):
        artist_id = "11111111-1111-4111-8111-111111111111"
        search.return_value = {"recordings": []}
        track_search_index.replace_artist_track_snapshot(
            artist_id, [], release_count=0, recording_count=0,
        )

        response = self.client.get(
            f"/api/music/artist/{artist_id}/tracks?q=Unknown",
            headers={"X-CSRF-Token": self.register()},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["candidateCount"], 0)
        search.assert_called_once()

    @patch("backend.routes.music.track_search_index.cached_release_groups")
    @patch("backend.routes.music.musicbrainz.search")
    def test_snapshot_miss_can_find_guest_release_from_fresh_response(
        self, search, cached_groups,
    ):
        artist_id = "11111111-1111-4111-8111-111111111111"
        group_id = "22222222-2222-4222-8222-222222222222"
        track_search_index.replace_artist_track_snapshot(
            artist_id, [], release_count=0, recording_count=1,
        )
        search.return_value = {"recordings": [{
            "id": "33333333-3333-4333-8333-333333333333",
            "title": "Guest song",
            "artist-credit": [{
                "name": "Artist", "artist": {"id": artist_id},
            }],
            "releases": [{
                "id": "44444444-4444-4444-8444-444444444444",
                "release-group": {"id": group_id, "title": "Compilation"},
            }],
        }]}
        cached_groups.return_value = {
            group_id: {
                "id": group_id, "title": "Compilation",
                "primary-type": "Album", "secondary-types": [],
            },
        }

        response = self.client.get(
            f"/api/music/artist/{artist_id}/tracks?q=Guest%20song",
            headers={"X-CSRF-Token": self.register()},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["candidateCount"], 1)
        self.assertEqual(response.get_json()["results"][0]["id"], group_id)

    @patch("backend.routes.music.track_search_index.index_release")
    @patch("backend.routes.music.musicbrainz.get")
    def test_release_tracklist_includes_local_romanization_without_extra_lookup(
        self, get, index_release
    ):
        get.return_value = {
            "id": "release-with-japanese-tracks",
            "title": "Example release",
            "artist-credit": [{"name": "ReoNa"}],
            "media": [{"tracks": [
                {"number": "1", "title": "じゃあな"},
                {"number": "2", "title": "more than words"},
                {"number": "3", "recording": {"title": "幸せ。"}},
            ]}],
        }

        response = self.client.get(
            "/api/music/release/release-with-japanese-tracks",
            headers={"X-CSRF-Token": self.register()},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [(track["title"], track["romanizedTitle"])
             for track in response.get_json()["tracks"]],
            [("じゃあな", "Jaana"), ("more than words", ""),
             ("幸せ。", "Shiawase.")],
        )
        get.assert_called_once()
        self.assertEqual(
            get.call_args.args[1],
            "recordings+artist-credits+release-groups",
        )
        index_release.assert_called_once()

    @patch("backend.routes.music.track_search_index.cached_release_groups")
    @patch("backend.routes.music.track_search_index.search_artist_tracks")
    def test_artist_track_search_returns_cached_release_group_cards(
        self,
        search_tracks,
        cached_groups,
    ):
        artist_id = "11111111-1111-4111-8111-111111111111"
        album_id = "22222222-2222-4222-8222-222222222222"
        single_id = "33333333-3333-4333-8333-333333333333"
        search_tracks.return_value = [
            {
                "normalized_title": "honeymoon avenue",
                "release_group_mbid": album_id,
            },
            {
                "normalized_title": "honeymoon avenue",
                "release_group_mbid": single_id,
            },
        ]
        cached_groups.return_value = {
            album_id: {
                "id": album_id,
                "title": "Yours Truly",
                "primary-type": "Album",
                "secondary-types": [],
                "first-release-date": "2013-08-30",
            },
            single_id: {
                "id": single_id,
                "title": "Honeymoon Avenue",
                "primary-type": "Single",
                "secondary-types": [],
                "first-release-date": "2013",
            },
        }

        response = self.client.get(
            f"/api/music/artist/{artist_id}/tracks?q=Honeymoon%20Avenue",
            headers={"X-CSRF-Token": self.register()},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["candidateCount"], 2)
        self.assertEqual(
            [(group["title"], group["type"]) for group in payload["results"]],
            [("Yours Truly", "Album"), ("Honeymoon Avenue", "Single")],
        )
        self.assertTrue(all(
            group["matchedTracks"] == ["honeymoon avenue"]
            for group in payload["results"]
        ))
        search_tracks.assert_called_once_with(artist_id, "Honeymoon Avenue")

    @patch("backend.routes.music.track_search_index.index_recording_search")
    @patch("backend.routes.music.track_search_index.cached_release_groups")
    @patch("backend.routes.music.track_search_index.search_artist_tracks")
    @patch("backend.routes.music.musicbrainz.search")
    def test_artist_track_search_populates_a_new_artist_on_demand(
        self,
        search,
        search_tracks,
        cached_groups,
        index_recording_search,
    ):
        artist_id = "020bfbb4-05c3-4c86-b372-17825c262094"
        album_id = "22222222-2222-4222-8222-222222222222"
        response = {"recordings": [{"id": "recording-id"}]}
        search.return_value = response
        search_tracks.side_effect = [
            [],
            [{
                "normalized_title": "like a stone",
                "release_group_mbid": album_id,
            }],
        ]
        cached_groups.return_value = {
            album_id: {
                "id": album_id,
                "title": "Audioslave",
                "primary-type": "Album",
                "secondary-types": [],
                "first-release-date": "2002-11-19",
            },
        }

        result = self.client.get(
            f"/api/music/artist/{artist_id}/tracks?q=Like%20a%20Stone",
            headers={"X-CSRF-Token": self.register()},
        )

        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.get_json()["results"][0]["title"], "Audioslave")
        recording_query, search_type = search.call_args.args
        self.assertIn('recording:"Like a Stone"', recording_query)
        self.assertIn(f"arid:{artist_id}", recording_query)
        self.assertEqual(search_type, "track")
        self.assertEqual(search.call_args.kwargs, {
            "priority": "interactive",
            "plain_search": False,
            "limit": 50,
        })
        index_recording_search.assert_called_once_with(response)
        self.assertEqual(search_tracks.call_count, 2)

    @patch("backend.routes.music.track_search_index.index_cached_release")
    @patch("backend.routes.music.detail_cache.cached_response")
    @patch("backend.routes.music.musicbrainz.get")
    def test_cached_release_response_backfills_the_track_index(
        self,
        get,
        cached_response,
        index_cached_release,
    ):
        cached_response.return_value = {"id": "release-id"}
        index_cached_release.side_effect = [False, True]

        response = self.client.get(
            "/api/music/release/release-id",
            headers={"X-CSRF-Token": self.register()},
        )

        self.assertEqual(response.status_code, 200)
        get.assert_not_called()
        self.assertEqual(
            [item.args[0] for item in index_cached_release.call_args_list],
            [
                musicbrainz.metadata_cache_key(
                    "/release/release-id",
                    "recordings+artist-credits+release-groups",
                ),
                musicbrainz.metadata_cache_key(
                    "/release/release-id",
                    "recordings+artist-credits",
                ),
            ],
        )

    @patch("backend.routes.music.lastfm.get_public")
    @patch("backend.routes.music.get_lastfm_api_key", return_value="")
    def test_similar_artists_is_empty_without_a_shared_lastfm_key(
        self, get_api_key, get_public
    ):
        artist_id = "11111111-1111-1111-1111-111111111111"
        self.register()

        response = self.client.get(f"/api/music/artist/{artist_id}/similar")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {
            "artists": [],
            "configured": False,
            "offset": 0,
            "limit": 12,
            "total": 0,
            "nextOffset": None,
            "hasMore": False,
            "pending": 0,
        })
        get_api_key.assert_called_once_with()
        get_public.assert_not_called()

    @patch("backend.routes.music.lidarr.cached_artist_availability")
    @patch("backend.routes.music.lastfm.get_public")
    @patch(
        "backend.routes.music.get_lastfm_api_key",
        return_value="shared-lastfm-key",
    )
    def test_similar_artists_returns_navigable_deduplicated_matches(
        self, get_api_key, get_public, artist_availability
    ):
        artist_id = "11111111-1111-1111-1111-111111111111"
        similar_id = "22222222-2222-2222-2222-222222222222"
        tracked_id = "33333333-3333-3333-3333-333333333333"
        get_public.return_value = {
            "similarartists": {
                "artist": [
                    {"mbid": artist_id, "name": "Current artist", "match": "1"},
                    {"mbid": similar_id, "name": "Neighbor", "match": "0.913"},
                    {"mbid": similar_id, "name": "Duplicate", "match": "0.8"},
                    {"mbid": "", "name": "Missing identifier", "match": "0.7"},
                    {"mbid": tracked_id, "name": "Tracked", "match": "0.5"},
                    {"mbid": "invalid", "name": "Invalid identifier", "match": "0.4"},
                ],
            },
        }
        artist_availability.return_value = {tracked_id: {"id": 42}}
        self.register()

        response = self.client.get(f"/api/music/artist/{artist_id}/similar")
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertTrue(payload["configured"])
        self.assertEqual(payload["total"], 4)
        self.assertEqual(payload["pending"], 2)
        self.assertFalse(payload["hasMore"])
        self.assertIsNone(payload["nextOffset"])
        self.assertEqual([artist["id"] for artist in payload["artists"]], [
            similar_id,
            tracked_id,
        ])
        self.assertEqual(payload["artists"][0], {
            "id": similar_id,
            "name": "Neighbor",
            "type": "91% match",
            "rank": 0,
            "match": 0.913,
            "coverArt": f"/api/artwork/artist/{similar_id}?size=thumb",
            "availableInLidarr": False,
            "recommendationSource": "Similar on Last.fm",
        })
        self.assertTrue(payload["artists"][1]["availableInLidarr"])
        get_api_key.assert_called_once_with()
        get_public.assert_called_once_with(
            "artist.getsimilar",
            "shared-lastfm-key",
            mbid=artist_id,
            limit=50,
            autocorrect=1,
        )

    @patch("backend.routes.music.get_service")
    @patch("backend.routes.music.lidarr.cached_artist_availability")
    @patch("backend.routes.music._plex_artist")
    def test_artist_availability_returns_live_cached_service_links(
        self, plex_artist, artist_availability, get_service
    ):
        get_service.side_effect = lambda name: {"configured": name}
        plex_artist.return_value = {
            "url": "https://app.plex.tv/artist",
            "plexampUrl": "https://listen.plex.tv/artist/example",
        }
        artist_availability.return_value = {
            "artist-id": {"id": 42, "name": "Tracked Artist"},
        }
        self.register()

        response = self.client.get(
            "/api/music/artist/artist-id/availability"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.get_json(), {
            "id": "artist-id",
            "availableInPlex": True,
            "availableInLidarr": True,
            "plexUrl": "https://app.plex.tv/artist",
            "plexampUrl": "https://listen.plex.tv/artist/example",
            "releaseGroups": {},
            "settled": True,
        })

    @patch("backend.routes.music.get_service")
    @patch("backend.routes.music.lidarr.cached_library_availability")
    @patch("backend.routes.music.lidarr.cached_artist_availability")
    @patch("backend.routes.music._plex_release_group_inventory")
    @patch("backend.routes.music._plex_artist")
    def test_artist_availability_includes_incomplete_discography_groups(
        self,
        plex_artist,
        plex_inventory,
        artist_availability,
        album_availability,
        get_service,
    ):
        get_service.side_effect = lambda name: {"configured": name}
        plex_artist.return_value = {"url": "https://app.plex.tv/artist"}
        plex_inventory.return_value = {"group-id": [{"name": "Owned Album"}]}
        artist_availability.return_value = {"artist-id": {"id": 42}}
        album_availability.return_value = {
            "group-id": {"fullyAvailable": True},
        }
        self.register()

        response = self.client.get(
            "/api/music/artist/artist-id/availability"
            "?releaseGroup=group-id"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["releaseGroups"], {
            "group-id": {
                "availableInPlex": True,
                "availableInLidarr": True,
                "fullyAvailableInLidarr": True,
                "requestStatus": "available",
                "downloadStatus": None,
            },
        })

    def test_batch_release_group_availability_uses_live_cached_status(self):
        with (
            patch("backend.routes.music._plex_release_group_inventory") as plex_groups,
            patch("backend.routes.music.lidarr.cached_library_availability") as albums,
            patch("backend.routes.music._download_snapshot") as downloads,
            patch("backend.routes.music.pending_lidarr_search_mbids") as pending,
        ):
            plex_groups.return_value = {
                "owned": [{"name": "Owned", "url": "https://app.plex.tv/owned"}],
            }
            albums.return_value = {
                "loading": {"fullyAvailable": False},
                "owned": {"fullyAvailable": True},
            }
            downloads.return_value = {"loading": {
                "progress": 42, "status": "downloading", "downloadId": 99,
            }}
            pending.return_value = {"loading"}
            self.register()

            response = self.client.get(
                "/api/music/release-groups/availability"
                "?releaseGroup=loading&releaseGroup=owned&releaseGroup=loading"
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        groups = response.get_json()["releaseGroups"]
        self.assertEqual(list(groups), ["loading", "owned"])
        self.assertEqual(groups["loading"]["requestStatus"], "downloading")
        self.assertEqual(groups["loading"]["downloadStatus"]["progress"], 42)
        self.assertFalse(groups["loading"]["fullyAvailableInLidarr"])
        self.assertEqual(groups["owned"]["requestStatus"], "available")
        self.assertEqual(groups["owned"]["plexReleases"][0]["url"], "https://app.plex.tv/owned")
        self.assertNotIn("downloadId", response.get_data(as_text=True))

    @patch("backend.routes.music.get_service")
    @patch("backend.routes.music.lidarr.cached_library_availability")
    @patch("backend.routes.music._plex_release_group_inventory")
    def test_release_group_availability_waits_for_lidarr_download_completion(
        self, plex_inventory, lidarr_availability, get_service
    ):
        get_service.side_effect = lambda name: {"configured": name}
        plex_inventory.return_value = {
            "group-id": [{
                "name": "Owned Album",
                "releaseType": "album",
                "musicbrainzReleaseId": "release-id",
                "url": "https://app.plex.tv/album",
                "plexampUrl": "https://listen.plex.tv/album/example",
            }],
        }
        lidarr_availability.return_value = {
            "group-id": {
                "fullyAvailable": False,
                "trackFileCount": 3,
                "totalTrackCount": 10,
            },
        }
        self.register()

        response = self.client.get(
            "/api/music/release-group/group-id/availability"
        )
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertTrue(payload["availableInPlex"])
        self.assertTrue(payload["availableInLidarr"])
        self.assertFalse(payload["fullyAvailableInLidarr"])
        self.assertFalse(payload["settled"])
        self.assertEqual(payload["ownedReleaseIds"], ["release-id"])
        self.assertEqual(
            payload["plexReleases"][0]["url"],
            "https://app.plex.tv/album",
        )

    def test_release_group_availability_uses_safe_lifecycle_precedence(self):
        self.register()
        download = {
            "progress": 45,
            "status": "downloading",
            "trackedDownloadStatus": "ok",
            "trackedDownloadState": "downloading",
            "timeLeft": "00:05:00.1234567",
            "estimatedCompletionTime": "2026-08-05T20:56:32Z",
            "downloadId": 17,
            "title": "private title",
            "indexer": "private indexer",
            "downloadClient": "private client",
            "path": "private path",
        }
        with (
            patch("backend.routes.music._plex_release_group_inventory", return_value={}),
            patch("backend.routes.music.lidarr.cached_library_availability") as albums,
            patch("backend.routes.music.lidarr.cached_download_availability") as downloads,
            patch("backend.routes.music.pending_lidarr_search_mbids") as pending,
        ):
            albums.return_value = {"group-id": {"fullyAvailable": True}}
            downloads.return_value = {"group-id": download}
            pending.return_value = {"group-id"}
            available = self.client.get("/api/music/release-group/group-id/availability")
            available_payload = available.get_json()
            self.assertEqual(available_payload["requestStatus"], "available")
            self.assertIsNone(available_payload["downloadStatus"])

            albums.return_value = {"group-id": {"fullyAvailable": False}}
            pending.return_value = {"group-id"}
            downloading = self.client.get("/api/music/release-group/group-id/availability")
            downloading_payload = downloading.get_json()
            self.assertEqual(downloading_payload["requestStatus"], "downloading")
            self.assertEqual(set(downloading_payload["downloadStatus"]), {
                "progress", "status", "trackedDownloadStatus", "trackedDownloadState",
                "timeLeft", "estimatedCompletionTime",
            })
            serialized = downloading.get_data(as_text=True)
            for forbidden in ("downloadId", "private title", "private indexer", "private client", "private path"):
                self.assertNotIn(forbidden, serialized)

            downloads.return_value = {}
            queued = self.client.get("/api/music/release-group/group-id/availability")
            self.assertEqual(queued.get_json()["requestStatus"], "queued")

    @patch("backend.routes.music.musicbrainz.get")
    def test_completed_artist_payload_uses_shared_cache_and_etag(self, get):
        get.side_effect = [
            {
                "id": "etag-artist",
                "name": "Cached Artist",
                "relations": [],
                "genres": [],
            },
            {"release-groups": [], "release-group-count": 0},
        ]
        self.register()

        first = self.client.get("/api/music/artist/etag-artist")
        etag = first.headers.get("ETag")
        second = self.client.get(
            "/api/music/artist/etag-artist",
            headers={"If-None-Match": etag},
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.get_json()["name"], "Cached Artist")
        self.assertEqual(first.headers["Cache-Control"], "private, max-age=60")
        self.assertTrue(etag.startswith('W/"'))
        self.assertEqual(second.status_code, 304)
        self.assertEqual(second.data, b"")
        self.assertEqual(get.call_count, 2)

    @patch("backend.routes.music.lidarr.tracked_artist")
    @patch("backend.routes.music.musicbrainz.get")
    def test_artist_prefetch_miss_does_not_call_providers(self, get, tracked_artist):
        get.return_value = None

        response = self.client.get(
            "/api/music/artist/artist-id?prefetch=1",
            headers={"X-CSRF-Token": self.register()},
        )

        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        get.assert_called_once()
        self.assertTrue(get.call_args.kwargs["cache_only"])
        tracked_artist.assert_not_called()

    @patch("backend.routes.music.lidarr.albums_by_release_group")
    @patch("backend.routes.music.musicbrainz.get")
    def test_release_group_prefetch_miss_does_not_call_providers(
        self, get, albums_by_release_group
    ):
        get.return_value = None

        response = self.client.get(
            "/api/music/release-group/group-id?prefetch=1",
            headers={"X-CSRF-Token": self.register()},
        )

        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        get.assert_called_once()
        self.assertTrue(get.call_args.kwargs["cache_only"])
        albums_by_release_group.assert_not_called()

    @patch("backend.routes.music.musicbrainz.get")
    def test_release_prefetch_miss_does_not_call_musicbrainz(self, get):
        get.return_value = None

        response = self.client.get(
            "/api/music/release/release-id?prefetch=1",
            headers={"X-CSRF-Token": self.register()},
        )

        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        get.assert_called_once()
        self.assertTrue(get.call_args.kwargs["cache_only"])
        self.assertEqual(
            get.call_args.args[1],
            "recordings+artist-credits+release-groups",
        )

    @patch("backend.routes.music.musicbrainz.get")
    def test_artist_detail_returns_english_alias(self, get):
        get.side_effect = [
            {
                "id": "artist-id",
                "name": "ポルカドットスティングレイ",
                "aliases": [{
                    "name": "POLKADOT STINGRAY",
                    "locale": "en",
                    "primary": True,
                }],
                "relations": [],
                "genres": [],
            },
            {"release-groups": [], "release-group-count": 0},
        ]

        response = self.client.get(
            "/api/music/artist/artist-id",
            headers={"X-CSRF-Token": self.register()},
        )

        self.assertEqual(response.get_json()["romanizedName"], "POLKADOT STINGRAY")
        self.assertEqual(get.call_args_list[0].args[1], "aliases+url-rels+genres")

    @patch("backend.routes.music.musicbrainz.get")
    def test_artist_discography_returns_romanized_release_titles(self, get):
        get.side_effect = [
            {
                "id": "artist-id",
                "name": "ポルカドットスティングレイ",
                "relations": [],
                "genres": [],
            },
            {
                "release-groups": [{
                    "id": "group-id",
                    "title": "極彩",
                    "aliases": [{"name": "In Full Color", "locale": "en"}],
                    "primary-type": "Album",
                }],
                "release-group-count": 1,
            },
        ]

        response = self.client.get(
            "/api/music/artist/artist-id",
            headers={"X-CSRF-Token": self.register()},
        )

        group = response.get_json()["sections"]["Album"][0]
        self.assertEqual(group["title"], "極彩")
        self.assertEqual(group["romanizedTitle"], "In Full Color")
        self.assertEqual(get.call_args_list[1].args[1], "aliases")

    @patch("backend.routes.music.musicbrainz.get")
    def test_release_group_detail_returns_romanized_title(self, get):
        get.side_effect = [
            {
                "id": "group-id",
                "title": "極彩",
                "aliases": [{"name": "In Full Color", "locale": "en"}],
                "artist-credit": [],
                "relations": [],
            },
            {"releases": [], "release-count": 0},
        ]
        anime_theme_links.sync_anime_theme_mapping(
            {"slug": "anime", "name": "Example Anime"},
            {
                "id": 99,
                "label": "Opening 1",
                "type": "OP",
                "sequence": 1,
                "song": {"id": 100, "title": "Theme Song"},
            },
            {"state": "resolved", "releaseGroups": [{"id": "group-id"}]},
        )

        response = self.client.get(
            "/api/music/release-group/group-id",
            headers={"X-CSRF-Token": self.register()},
        )

        self.assertEqual(response.get_json()["romanizedTitle"], "In Full Color")
        self.assertEqual(response.get_json()["animeThemes"], [{
            "animeSlug": "anime",
            "animeName": "Example Anime",
            "animePath": "/anime/anime#theme-99",
            "themeId": 99,
            "themeLabel": "Opening 1",
            "themeType": "OP",
            "sequence": 1,
            "songId": 100,
            "songTitle": "Theme Song",
        }])
        self.assertEqual(
            get.call_args_list[0].args[1], "aliases+artist-credits+url-rels"
        )

    @patch("backend.routes.music.musicbrainz.get")
    @patch("backend.routes.music.lidarr.cached_artist_availability")
    def test_artist_detail_marks_artist_already_tracked_in_lidarr(
        self, artist_availability, get
    ):
        artist_availability.return_value = {
            "artist-id": {"id": 42, "name": "Tracked Artist"}
        }
        get.side_effect = [
            {
                "id": "artist-id",
                "name": "Tracked Artist",
                "relations": [],
                "genres": [],
            },
            {"release-groups": [], "release-group-count": 0},
        ]

        response = self.client.get(
            "/api/music/artist/artist-id",
            headers={"X-CSRF-Token": self.register()},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["availableInLidarr"])

    @patch("backend.routes.music.plex.cached_library_snapshot")
    @patch("backend.routes.music.lidarr.albums_by_artist")
    @patch("backend.routes.music.lidarr.tracked_artist")
    @patch("backend.routes.music.get_service")
    @patch("backend.routes.music.musicbrainz.get")
    def test_cold_artist_uses_lidarr_while_musicbrainz_cache_is_empty(
        self,
        get,
        get_service,
        tracked_artist,
        albums_by_artist,
        plex_snapshot,
    ):
        get.return_value = None
        get_service.side_effect = lambda name: (
            {"url": "http://lidarr", "apiKey": "key"}
            if name == "lidarr"
            else {"url": "http://plex"}
        )
        tracked_artist.return_value = {
            "id": 42,
            "foreignArtistId": "artist-id",
            "artistName": "Fast Artist",
            "artistType": "Group",
        }
        albums_by_artist.return_value = [{
            "foreignAlbumId": "group-id",
            "title": "Fast Album",
            "albumType": "Album",
            "releaseDate": "2025-04-03T00:00:00Z",
        }]
        plex_snapshot.return_value = {"artists": [], "releaseGroups": []}

        response = self.client.get(
            "/api/music/artist/artist-id",
            headers={"X-CSRF-Token": self.register()},
        )

        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["provisional"])
        self.assertEqual(payload["metadataSource"], "Lidarr")
        self.assertEqual(payload["sections"]["Album"][0]["id"], "group-id")
        self.assertTrue(all(call.kwargs["cache_only"] for call in get.call_args_list))
        albums_by_artist.assert_called_once_with(42, get_service("lidarr"))

    @patch("backend.routes.music.lidarr.tracked_artist")
    @patch("backend.routes.music.musicbrainz.get")
    def test_artist_completion_skips_lidarr_and_populates_musicbrainz(
        self, get, tracked_artist
    ):
        get.side_effect = [
            None,
            {
                "id": "artist-id",
                "name": "Complete Artist",
                "relations": [],
                "genres": [],
            },
            {"release-groups": [], "release-group-count": 0},
        ]

        response = self.client.get(
            "/api/music/artist/artist-id?complete=1",
            headers={"X-CSRF-Token": self.register()},
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()["provisional"])
        self.assertEqual(response.get_json()["metadataSource"], "MusicBrainz")
        tracked_artist.assert_not_called()
        self.assertTrue(get.call_args_list[0].kwargs["cache_only"])
        self.assertFalse(get.call_args_list[1].kwargs["cache_only"])

    @patch("backend.routes.music.plex.cached_library_snapshot")
    @patch("backend.routes.music.lidarr.albums_by_release_group")
    @patch("backend.routes.music.get_service")
    @patch("backend.routes.music.musicbrainz.get")
    def test_cold_release_group_uses_lidarr_before_musicbrainz(
        self, get, get_service, albums_by_release_group, plex_snapshot
    ):
        get.return_value = None
        get_service.side_effect = lambda name: (
            {"url": "http://lidarr", "apiKey": "key"}
            if name == "lidarr"
            else {"url": "http://plex"}
        )
        albums_by_release_group.return_value = Response(payload=[{
            "foreignAlbumId": "group-id",
            "title": "Fast Album",
            "albumType": "Album",
            "artist": {
                "foreignArtistId": "artist-id",
                "artistName": "Fast Artist",
            },
            "releases": [{"foreignReleaseId": "release-id"}],
        }])
        plex_snapshot.return_value = {"artists": [], "releaseGroups": []}
        anime_theme_links.sync_anime_theme_mapping(
            {"slug": "anime", "name": "Example Anime"},
            {
                "id": 99,
                "label": "Opening 1",
                "type": "OP",
                "sequence": 1,
                "song": {"id": 100, "title": "Theme Song"},
            },
            {"state": "resolved", "releaseGroups": [{"id": "group-id"}]},
        )

        response = self.client.get(
            "/api/music/release-group/group-id",
            headers={"X-CSRF-Token": self.register()},
        )

        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["provisional"])
        self.assertEqual(payload["artistId"], "artist-id")
        self.assertEqual(payload["releases"][0]["id"], "release-id")
        self.assertEqual(payload["animeThemes"][0]["animePath"], "/anime/anime#theme-99")
        self.assertTrue(all(call.kwargs["cache_only"] for call in get.call_args_list))

    @patch("backend.routes.music.musicbrainz.get")
    def test_clicked_artist_uses_critical_priority_for_every_discography_page(
        self, get
    ):
        get.side_effect = [
            {
                "id": "artist-id",
                "name": "Large Artist",
                "relations": [],
                "genres": [],
            },
            {"release-groups": [], "release-group-count": 0},
        ]

        response = self.client.get(
            "/api/music/artist/artist-id",
            headers={"X-CSRF-Token": self.register()},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(get.call_args_list[0].kwargs["priority"], "critical")
        self.assertEqual(get.call_args_list[1].kwargs["priority"], "critical")

    @patch("backend.routes.music.plex.cached_library_snapshot")
    @patch("backend.routes.music.get_service")
    @patch("backend.routes.music.musicbrainz.get")
    def test_artist_discography_marks_release_groups_owned_in_plex(
        self, get, get_service, library_snapshot
    ):
        get.side_effect = [
            {"id": "artist-id", "name": "Artist", "relations": [], "genres": []},
            {
                "release-groups": [{
                    "id": "group-id",
                    "title": "Owned Album",
                    "primary-type": "Album",
                }],
                "release-group-count": 1,
            },
        ]
        get_service.return_value = {"url": "http://plex"}
        library_snapshot.return_value = {
            "releaseGroups": [{
                "name": "Owned Album",
                "musicbrainzReleaseId": "release-id",
                "musicbrainzReleaseGroupId": "group-id",
                "url": "https://app.plex.tv/album",
            }]
        }

        response = self.client.get(
            "/api/music/artist/artist-id",
            headers={"X-CSRF-Token": self.register()},
        )

        group = response.get_json()["sections"]["Album"][0]
        self.assertTrue(group["availableInPlex"])
        self.assertEqual(group["plexReleases"][0]["releaseId"], "release-id")

    @patch("backend.routes.music.lidarr.cached_library_availability")
    @patch("backend.routes.music.plex.cached_library_snapshot")
    @patch("backend.routes.music.get_service")
    @patch("backend.routes.music.musicbrainz.get")
    def test_release_group_marks_the_exact_plex_edition(
        self, get, get_service, library_snapshot, lidarr_availability
    ):
        get.side_effect = [
            {
                "id": "group-id",
                "title": "Owned Album",
                "artist-credit": [{
                    "name": "Artist",
                    "artist": {"id": "artist-id"},
                }],
                "relations": [],
            },
            {
                "releases": [{
                    "id": "release-id",
                    "title": "Owned Album",
                    "media": [],
                }],
                "release-count": 1,
            },
        ]
        get_service.return_value = {"url": "http://plex"}
        library_snapshot.return_value = {
            "releaseGroups": [{
                "name": "Owned Album",
                "musicbrainzReleaseId": "release-id",
                "musicbrainzReleaseGroupId": "group-id",
                "url": "https://app.plex.tv/album",
            }]
        }
        lidarr_availability.return_value = {
            "group-id": {"fullyAvailable": True}
        }

        response = self.client.get(
            "/api/music/release-group/group-id",
            headers={"X-CSRF-Token": self.register()},
        )

        payload = response.get_json()
        self.assertTrue(payload["availableInPlex"])
        self.assertTrue(payload["releases"][0]["availableInPlex"])
        self.assertTrue(payload["availableInLidarr"])
        self.assertTrue(payload["fullyAvailableInLidarr"])

    @patch("backend.routes.music.artist_metadata_worker.refresh_artist_metadata")
    @patch("backend.routes.music.musicbrainz.get")
    def test_refresh_artist_uses_atomic_critical_refresh(
        self, get, refresh_artist_metadata
    ):
        artist_id = "11111111-1111-1111-1111-111111111111"
        get.side_effect = [
            {"id": artist_id, "name": "Fresh Artist", "relations": [], "genres": []},
            {"release-groups": [], "release-group-count": 0},
        ]
        csrf_token = self.register()

        response = self.client.post(
            f"/api/music/artist/{artist_id}/refresh",
            json={},
            headers={"X-CSRF-Token": csrf_token},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["name"], "Fresh Artist")
        self.assertEqual(get.call_count, 2)
        refresh_artist_metadata.assert_called_once_with(artist_id, "critical")
        self.assertTrue(all(call.kwargs["cache_only"] for call in get.call_args_list))
        self.assertTrue(all(call.kwargs["priority"] == "critical" for call in get.call_args_list))

    @patch("backend.routes.music.artist_metadata_worker.request_revalidation")
    def test_artist_revalidation_route_queues_background_check(self, request_check):
        artist_id = "11111111-1111-1111-1111-111111111111"
        request_check.return_value = {
            "status": "queued",
            "polling": True,
            "lastRefreshAt": None,
        }
        csrf_token = self.register()

        response = self.client.post(
            f"/api/music/artist/{artist_id}/revalidate",
            json={},
            headers={"X-CSRF-Token": csrf_token},
        )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.get_json()["status"], "queued")
        request_check.assert_called_once_with(artist_id)

    @patch("backend.routes.music.artist_metadata_worker.status")
    def test_artist_revalidation_status_is_read_only(self, status):
        artist_id = "11111111-1111-1111-1111-111111111111"
        status.return_value = {
            "status": "refreshing",
            "polling": True,
            "lastRefreshAt": None,
        }
        self.register()

        response = self.client.get(
            f"/api/music/artist/{artist_id}/revalidation",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "refreshing")
        status.assert_called_once_with(artist_id)

    @patch("backend.routes.music.artist_metadata_worker.request_revalidation")
    def test_artist_revalidation_rejects_invalid_mbid(self, request_check):
        csrf_token = self.register()

        response = self.client.post(
            "/api/music/artist/not-an-mbid/revalidate",
            json={},
            headers={"X-CSRF-Token": csrf_token},
        )

        self.assertEqual(response.status_code, 400)
        request_check.assert_not_called()


class RecommendationAssemblyTests(unittest.TestCase):
    def setUp(self):
        # These tests isolate legacy provider assembly and worker behavior.
        # Personal feed integration has its own database-backed suite.
        enrichment = patch("backend.recommendation_feed.build_personal_feed",
                           side_effect=lambda _user, payload, _shared=None: payload)
        enrichment.start()
        self.addCleanup(enrichment.stop)

    @patch("backend.recommendations.plex.cached_library_index")
    @patch("backend.recommendations.get_plex_listens")
    def test_plex_profile_applies_recency_play_counts_and_snapshot_tags(
        self, get_listens, cached_index
    ):
        now = 400 * 24 * 60 * 60
        get_listens.return_value = [
            {
                "artist_rating_key": "artist-1",
                "album_rating_key": "album-1",
                "played_at": now - 10 * 24 * 60 * 60,
            },
            {
                "artist_rating_key": "artist-1",
                "album_rating_key": "album-1",
                "played_at": now - 200 * 24 * 60 * 60,
            },
            {
                "artist_rating_key": "missing",
                "album_rating_key": None,
                "played_at": now,
            },
        ]
        cached_index.return_value = {
            "artistsByRatingKey": {
                "artist-1": {
                    "musicbrainzId": "artist-mbid",
                    "name": "Known Artist",
                    "genres": ["Alternative"],
                    "styles": ["Indie Rock"],
                    "moods": ["Energetic"],
                },
            },
            "releaseGroupsByRatingKey": {
                "album-1": {
                    "genres": ["Alternative"],
                    "styles": ["Garage Rock"],
                    "moods": ["Reflective"],
                },
            },
        }

        seeds, tags = recommendation_engine.plex_taste_profile(
            7,
            {
                "url": "http://plex:32400",
                "machineIdentifier": "server-1",
            },
            now=now,
        )

        self.assertEqual(len(seeds), 1)
        self.assertEqual(seeds[0]["id"], "artist-mbid")
        self.assertEqual(seeds[0]["playCount"], 2)
        self.assertAlmostEqual(seeds[0]["score"], 1.35)
        self.assertEqual(tags["alternative"], 1.0)
        self.assertGreater(tags["indie rock"], tags["energetic"])
        get_listens.assert_called_once_with(
            7,
            now - 365 * 24 * 60 * 60,
            server_id="server-1",
        )

    @patch("backend.recommendations._musicbrainz_lookup")
    @patch("backend.recommendations.resolve_lastfm_album_mbid")
    @patch("backend.recommendations.lastfm.get_public")
    @patch("backend.recommendations.plex_taste_profile")
    def test_plex_recommendations_use_similar_tags_and_existing_album_path(
        self, taste_profile, get_public, resolve_album, musicbrainz_lookup
    ):
        taste_profile.return_value = (
            [{"id": "seed-mbid", "name": "Seed", "score": 3.0}],
            {"alternative": 1.0},
        )

        def public(method, _api_key, **_kwargs):
            if method == "tag.getsimilar":
                return {"similartags": {"tag": [{"name": "indie"}]}}
            if method == "artist.getsimilar":
                return {"similarartists": {"artist": [{
                    "mbid": "candidate-mbid",
                    "name": "Candidate",
                    "match": "0.8",
                }]}}
            if method == "artist.gettoptags":
                return {"toptags": {"tag": [{"name": "indie"}]}}
            if method == "artist.gettopalbums":
                return {"topalbums": {"album": [{
                    "mbid": "album-mbid",
                    "name": "Candidate Album",
                    "artist": {"name": "Candidate"},
                }]}}
            self.fail(f"Unexpected Last.fm method {method}")

        get_public.side_effect = public
        resolve_album.return_value = "release-group-mbid"
        musicbrainz_lookup.return_value = {
            "title": "Candidate Album",
            "first-release-date": "2025",
            "primary-type": "Album",
        }

        artists, albums = recommendation_engine.plex_history_recommendations(
            7,
            {"url": "http://plex"},
            "shared-key",
        )

        self.assertEqual([artist["id"] for artist in artists], ["candidate-mbid"])
        self.assertEqual([album["id"] for album in albums], ["release-group-mbid"])
        methods = [call.args[0] for call in get_public.call_args_list]
        self.assertIn("tag.getsimilar", methods)
        self.assertIn("artist.getsimilar", methods)
        self.assertIn("artist.gettopalbums", methods)
        self.assertNotIn("tag.gettopartists", methods)
        self.assertNotIn("tag.gettopalbums", methods)

    @patch("backend.recommendations._recommendation_exclusions")
    @patch("backend.recommendations.lastfm_top_tags")
    @patch("backend.recommendations.lastfm.get")
    @patch("backend.recommendations.lastfm_recommendations")
    @patch("backend.recommendations.plex_history_recommendations")
    @patch("backend.recommendations.get_lastfm_api_key")
    @patch("backend.recommendations.get_service")
    def test_linked_lastfm_and_plex_results_are_blended_then_deduplicated(
        self,
        get_service,
        get_api_key,
        plex_recommendations,
        lastfm_recommendations,
        lastfm_get,
        lastfm_top_tags,
        exclusions,
    ):
        get_service.side_effect = lambda name: (
            {"url": "http://plex", "machineIdentifier": "server-1"}
            if name == "plex"
            else None
        )
        get_api_key.return_value = "shared-key"
        exclusions.return_value = recommendation_engine._empty_exclusions()
        plex_recommendations.return_value = (
            [{"id": "same-artist", "name": "Shared Artist", "score": 0.8}],
            [{"id": "same-album", "name": "Shared Album", "score": 0.7}],
        )
        lastfm_recommendations.return_value = (
            [{"id": "same-artist", "name": "Shared Artist", "score": 0.9}],
            [{"id": "same-album", "name": "Shared Album", "score": 0.6}],
        )
        lastfm_get.return_value = {"artists": {"artist": []}}
        lastfm_top_tags.return_value = []

        payload = recommendation_engine.build_recommendation_cache({
            "id": 7,
            "username": "listener",
            "plex_id": "global-plex-id",
            "listenbrainz_username": None,
            "lastfm_username": "lastfm-listener",
        })

        self.assertEqual(len(payload["artists"]), 1)
        self.assertEqual(len(payload["albums"]), 1)
        self.assertEqual(payload["artists"][0]["score"], 0.9)
        self.assertEqual(
            payload["artists"][0]["recommendationSource"],
            "Plex history · Last.fm + Last.fm",
        )
        self.assertEqual(payload["providerStatus"], {
            "listenbrainz": "disabled",
            "lastfm": "ok",
            "plexHistory": "ok",
        })

    @patch("backend.recommendations.listenbrainz.recording_metadata")
    @patch("backend.recommendations.listenbrainz.recording_recommendations")
    def test_listenbrainz_deduplicates_using_highest_recording_score(
        self, recording_recommendations, recording_metadata
    ):
        recording_recommendations.return_value = [
            {"recording_mbid": "recording-1", "score": 0.4},
            {"recording_mbid": "recording-2", "score": 0.9},
        ]
        common_artist = {
            "artist_mbid": "artist-1",
            "name": "Test Artist",
            "type": "Person",
        }
        common_release = {
            "release_group_mbid": "group-1",
            "name": "Test Album",
            "album_artist_name": "Test Artist",
            "year": 2026,
            "type": "Album",
        }
        recording_metadata.return_value = {
            "recording-1": {
                "artist": {"artists": [common_artist]},
                "release": common_release,
            },
            "recording-2": {
                "artist": {"artists": [common_artist]},
                "release": common_release,
            },
        }

        artists, albums = recommendation_engine.listenbrainz_recommendations("listener")

        self.assertEqual(len(artists), 1)
        self.assertEqual(len(albums), 1)
        self.assertEqual(artists[0]["score"], 0.9)
        self.assertEqual(albums[0]["score"], 0.9)
        self.assertEqual(artists[0]["coverArt"], "/api/artwork/artist/artist-1?size=thumb")
        self.assertEqual(
            albums[0]["coverArt"], "/api/artwork/release-group/group-1?size=thumb"
        )

    @patch("backend.recommendations.listenbrainz.recording_metadata")
    @patch("backend.recommendations.listenbrainz.recording_recommendations")
    def test_listenbrainz_excludes_library_artists_and_requested_albums(
        self, recording_recommendations, recording_metadata
    ):
        recording_recommendations.return_value = [
            {"recording_mbid": "owned-recording", "score": 0.9},
            {"recording_mbid": "new-recording", "score": 0.8},
        ]
        recording_metadata.return_value = {
            "owned-recording": {
                "artist": {"artists": [{
                    "artist_mbid": "owned-artist",
                    "name": "Owned Artist",
                }]},
                "release": {
                    "release_group_mbid": "owned-artist-album",
                    "name": "Owned Artist Album",
                    "album_artist_name": "Owned Artist",
                },
            },
            "new-recording": {
                "artist": {"artists": [{
                    "artist_mbid": "new-artist",
                    "name": "New Artist",
                }]},
                "release": {
                    "release_group_mbid": "requested-album",
                    "name": "Requested Album",
                    "album_artist_name": "New Artist",
                },
            },
        }

        artists, albums = recommendation_engine.listenbrainz_recommendations(
            "listener",
            excluded_artist_ids={"owned-artist"},
            excluded_artist_names={"Owned Artist"},
            excluded_album_names={("New Artist", "Requested Album")},
        )

        self.assertEqual([artist["id"] for artist in artists], ["new-artist"])
        self.assertEqual([album["id"] for album in albums], ["owned-artist-album"])

    @patch("backend.recommendations._search_release_group")
    @patch("backend.recommendations._musicbrainz_lookup")
    def test_lastfm_release_mbid_is_normalized_to_release_group(
        self, musicbrainz_lookup, search_release_group
    ):
        musicbrainz_lookup.return_value = {
            "release-group": {"id": "release-group-id"}
        }
        result = recommendation_engine.resolve_lastfm_album_mbid(
            "release-id", "Album", "Artist"
        )
        self.assertEqual(result, "release-group-id")
        musicbrainz_lookup.assert_called_once_with(
            "/release/release-id", "release-groups"
        )
        search_release_group.assert_not_called()

    @patch("backend.recommendations.resolve_lastfm_album_mbid")
    @patch("backend.recommendations.lastfm.get")
    def test_failed_musicbrainz_album_lookup_skips_only_that_album(
        self, lastfm_get, resolve_album
    ):
        def get(method, *_args, **_kwargs):
            if method == "user.gettopartists":
                return {"topartists": {"artist": [{"mbid": "seed"}]}}
            if method == "artist.getsimilar":
                return {"similarartists": {"artist": [{
                    "mbid": "recommended-artist",
                    "name": "Recommended Artist",
                }]}}
            if method == "user.gettopalbums":
                return {"topalbums": {"album": []}}
            if method == "user.gettoptags":
                return {"toptags": {"tag": []}}
            if method == "artist.gettoptags":
                return {"toptags": {"tag": []}}
            if method == "artist.gettopalbums":
                return {"topalbums": {"album": [{
                    "mbid": "release-id",
                    "name": "Unavailable Album",
                    "artist": {"name": "Recommended Artist"},
                }]}}
            self.fail(f"Unexpected Last.fm method {method}")

        lastfm_get.side_effect = get
        resolve_album.side_effect = requests.HTTPError("MusicBrainz 503")
        with self.assertLogs("backend.recommendations", level="WARNING") as logs:
            artists, albums = recommendation_engine.lastfm_recommendations("user", "key")
        self.assertEqual(len(artists), 1)
        self.assertEqual(albums, [])
        self.assertIn("Skipping Last.fm album", logs.output[0])

    @patch("backend.recommendations._musicbrainz_lookup")
    @patch("backend.recommendations.resolve_lastfm_album_mbid")
    @patch("backend.recommendations.lastfm.get")
    def test_lastfm_weights_recent_taste_and_softly_boosts_new_releases(
        self, lastfm_get, resolve_album, musicbrainz_lookup
    ):
        def get(method, *_args, **kwargs):
            if method == "user.gettopartists":
                period = kwargs["period"]
                seed = (
                    {"mbid": "recent-seed", "name": "Recent Seed", "playcount": "100"}
                    if period == "1month"
                    else {"mbid": "old-seed", "name": "Old Seed", "playcount": "100"}
                )
                return {"topartists": {"artist": [seed]}}
            if method == "artist.getsimilar":
                if kwargs.get("mbid") == "recent-seed":
                    artists = [{
                        "mbid": "recent-match",
                        "name": "Recent Match",
                        "match": "0.8",
                    }]
                else:
                    artists = [{
                        "mbid": "owned-match",
                        "name": "Owned Match",
                        "match": "0.9",
                    }]
                return {"similarartists": {"artist": artists}}
            if method == "user.gettopalbums":
                return {"topalbums": {"album": []}}
            if method == "user.gettoptags":
                return {"toptags": {"tag": [{"name": "indie"}]}}
            if method == "artist.gettoptags":
                return {"toptags": {"tag": [{"name": "indie"}]}}
            if method == "artist.gettopalbums":
                return {"topalbums": {"album": [
                    {
                        "mbid": "old-album",
                        "name": "Old Album",
                        "artist": {"name": "Recent Match"},
                    },
                    {
                        "mbid": "new-album",
                        "name": "New Album",
                        "artist": {"name": "Recent Match"},
                    },
                ]}}
            self.fail(f"Unexpected Last.fm method {method}")

        lastfm_get.side_effect = get
        resolve_album.side_effect = lambda mbid, *_args: mbid
        musicbrainz_lookup.side_effect = lambda path: {
            "id": path.rsplit("/", 1)[-1],
            "title": "Old Album" if path.endswith("old-album") else "New Album",
            "first-release-date": "1980" if path.endswith("old-album") else "2025",
            "primary-type": "Album",
        }

        artists, albums = recommendation_engine.lastfm_recommendations(
            "user",
            "key",
            excluded_artist_ids={"owned-match"},
        )

        self.assertEqual([artist["id"] for artist in artists], ["recent-match"])
        self.assertEqual([album["id"] for album in albums], ["new-album", "old-album"])
        self.assertEqual(albums[0]["tasteTags"], ["indie"])

    @patch("backend.recommendations._musicbrainz_lookup")
    @patch("backend.recommendations.lastfm_album_mbid")
    @patch("backend.recommendations.lastfm.get")
    def test_tag_backfill_filters_library_and_reranks_for_recency(
        self, lastfm_get, album_mbid, musicbrainz_lookup
    ):
        def get(method, *_args, **_kwargs):
            if method == "user.gettopalbums":
                return {"topalbums": {"album": []}}
            if method == "tag.gettopalbums":
                return {"albums": {"album": [
                    {"mbid": "old", "name": "Old", "artist": {"name": "Candidate"}},
                    {"mbid": "new", "name": "New", "artist": {"name": "Candidate"}},
                    {"mbid": "owned", "name": "Owned", "artist": {"name": "Owned Artist"}},
                ]}}
            self.fail(f"Unexpected Last.fm method {method}")

        lastfm_get.side_effect = get
        album_mbid.side_effect = lambda album, *_args: album["mbid"]
        musicbrainz_lookup.side_effect = lambda path: {
            "title": path.rsplit("/", 1)[-1].title(),
            "first-release-date": "1980" if path.endswith("old") else "2025",
            "primary-type": "Album",
        }

        albums = recommendation_engine.lastfm_tag_recommendations(
            "pop",
            "user",
            "key",
            excluded_album_names={("Owned Artist", "Owned")},
        )

        self.assertEqual([album["id"] for album in albums], ["new", "old"])

    @patch("backend.recommendations.plex.library_snapshot")
    @patch("backend.recommendations.lidarr.library_albums")
    @patch("backend.recommendations.lidarr.library_artists")
    @patch("backend.recommendations.get_service")
    @patch("backend.recommendations.get_request_history")
    def test_exclusions_combine_requests_lidarr_and_plex(
        self,
        get_request_history,
        get_service,
        library_artists,
        library_albums,
        library_snapshot,
    ):
        get_request_history.return_value = [
            {"kind": "artist", "mbid": "requested-artist", "name": "Requested"},
            {"kind": "release-group", "mbid": "requested-album", "name": "Album"},
        ]
        get_service.side_effect = lambda service: {"service": service}
        library_artists.return_value = [{
            "foreignArtistId": "lidarr-artist",
            "artistName": "Lidarr Artist",
        }]
        library_albums.return_value = [{"foreignAlbumId": "lidarr-album", "statistics": {"totalTrackCount": 10, "trackFileCount": 10}}]
        library_snapshot.return_value = {
            "artists": [{
                "name": "Plex Artist",
                "musicbrainzId": "plex-artist",
            }],
            "releaseGroups": [{
                "name": "Plex Album",
                "artistName": "Plex Artist",
                "musicbrainzReleaseId": "plex-release",
                "musicbrainzReleaseGroupId": "plex-album",
            }],
        }

        exclusions = recommendation_engine._recommendation_exclusions({"id": 7})

        self.assertEqual(
            exclusions["artist_ids"],
            {"requested-artist", "lidarr-artist", "plex-artist"},
        )
        self.assertEqual(
            exclusions["album_ids"],
            {"requested-album", "lidarr-album", "plex-album"},
        )
        self.assertEqual(
            exclusions["album_names"], {("Plex Artist", "Plex Album")}
        )
        self.assertEqual(
            exclusions["artist_names"],
            {"Requested", "Lidarr Artist", "Plex Artist"},
        )

    @patch("backend.recommendations.lastfm_top_tags")
    @patch("backend.recommendations.lastfm.get")
    @patch("backend.recommendations.lastfm_recommendations")
    @patch("backend.recommendations.listenbrainz_recommendations")
    def test_combined_cache_labels_sources_and_builds_tag_rows(
        self,
        listenbrainz_recommendations,
        lastfm_recommendations,
        lastfm_get,
        lastfm_top_tags,
    ):
        save_service("lastfm", {"apiKey": "admin-shared-key"})
        listenbrainz_recommendations.return_value = (
            [{"id": "lb-artist", "name": "LB Artist"}],
            [{"id": "lb-album", "name": "LB Album"}],
        )
        personalized_albums = [
            {
                "id": f"lf-album-{index}",
                "name": f"LF Album {index}",
                "tasteTags": (
                    ["ambient", "country"] if index >= 12
                    else ["indie"]
                ),
            }
            for index in range(32)
        ]
        lastfm_recommendations.return_value = (
            [{"id": "lf-artist", "name": "LF Artist"}],
            personalized_albums,
        )
        lastfm_top_tags.return_value = [
            {"name": "ambient", "count": 100},
            {"name": "country", "count": 80},
        ]

        def get(method, *_args, **_kwargs):
            if method == "chart.gettopartists":
                return {"artists": {"artist": [{
                    "mbid": "chart-artist",
                    "name": "Chart Artist",
                }]}}
            self.fail(f"Unexpected Last.fm method {method}")

        lastfm_get.side_effect = get
        payload = recommendation_engine.build_recommendation_cache({
            "listenbrainz_username": "listener",
            "lastfm_username": "lastfm-user",
            "lastfm_api_key": "key",
        })

        self.assertEqual(
            [item["recommendationSource"] for item in payload["artists"]],
            ["ListenBrainz", "Last.fm"],
        )
        self.assertEqual(payload["chartArtists"][0]["recommendationSource"], "Popular on Last.fm")
        self.assertEqual(payload["tagRows"][0]["tag"], "ambient")
        self.assertEqual(payload["tagRows"][0]["albums"][0]["id"], "lf-album-12")
        self.assertEqual(
            payload["tagRows"][0]["albums"][0]["recommendationSource"],
            "Last.fm taste · ambient",
        )
        tag_album_ids = [
            album["id"]
            for row in payload["tagRows"]
            for album in row["albums"]
        ]
        self.assertEqual(len(payload["tagRows"][0]["albums"]), 10)
        self.assertEqual(len(payload["tagRows"][1]["albums"]), 10)
        self.assertEqual(len(tag_album_ids), 20)
        self.assertEqual(len(tag_album_ids), len(set(tag_album_ids)))
        self.assertEqual(payload["providerStatus"], {
            "listenbrainz": "ok",
            "lastfm": "ok",
            "plexHistory": "disabled",
        })

    @patch("backend.recommendations.lastfm_top_tags")
    @patch("backend.recommendations.lastfm.get")
    @patch("backend.recommendations.lastfm_recommendations")
    @patch("backend.recommendations.listenbrainz_recommendations")
    def test_listenbrainz_timeout_does_not_block_lastfm(
        self,
        listenbrainz_recommendations,
        lastfm_recommendations,
        lastfm_get,
        lastfm_top_tags,
    ):
        save_service("lastfm", {"apiKey": "admin-shared-key"})
        listenbrainz_recommendations.side_effect = requests.Timeout("timed out")
        lastfm_recommendations.return_value = (
            [{"id": "lf-artist", "name": "LF Artist"}],
            [{"id": "lf-album", "name": "LF Album"}],
        )
        lastfm_get.return_value = {"artists": {"artist": []}}
        lastfm_top_tags.return_value = []

        with self.assertLogs("backend.recommendations", level="WARNING") as logs:
            payload = recommendation_engine.build_recommendation_cache({
                "listenbrainz_username": "offline-listener",
                "lastfm_username": "lastfm-user",
                "lastfm_api_key": "key",
            })

        self.assertEqual(payload["artists"][0]["name"], "LF Artist")
        self.assertEqual(payload["albums"][0]["name"], "LF Album")
        self.assertEqual(payload["providerStatus"], {
            "listenbrainz": "unavailable",
            "lastfm": "ok",
            "plexHistory": "disabled",
        })
        rendered = "\n".join(logs.output)
        self.assertIn("ListenBrainz recommendations unavailable", rendered)
        self.assertIn("user id unknown", rendered)
        self.assertIn("Timeout", rendered)
        self.assertNotIn("offline-listener", rendered)
        self.assertNotIn("lastfm-user", rendered)

    @patch("backend.recommendations.save_recommendation_cache")
    @patch("backend.recommendations.build_recommendation_cache")
    @patch("backend.recommendations.recommendation_users")
    def test_refresh_continues_when_one_user_fails(
        self, recommendation_users, build_cache, save_cache
    ):
        api_key = "sentinel-lastfm-api-key"
        linked_username = "sentinel-lastfm-username"
        melodarr_username = "sentinel-melodarr-username"
        private_url = (
            "https://ws.audioscrobbler.example/2.0/?"
            f"api_key={api_key}&user={linked_username}"
        )
        error = requests.HTTPError(f"HTTP 503 for url: {private_url}")
        error.response = Response(503)
        recommendation_users.return_value = [
            {
                "id": 1,
                "username": melodarr_username,
                "lastfm_username": linked_username,
            },
            {"id": 2, "username": "working"},
        ]
        build_cache.side_effect = [error, {"artists": []}]
        with self.assertLogs("backend.recommendations", level="WARNING") as logs:
            retry_required = recommendation_engine.refresh_recommendation_cache()
        save_cache.assert_called_once_with(2, {"artists": []})
        self.assertTrue(retry_required)
        rendered = "\n".join(logs.output)
        self.assertIn("user id 1", rendered)
        self.assertIn("HTTPError HTTP 503", rendered)
        for private_value in (
            api_key,
            linked_username,
            melodarr_username,
            private_url,
            "HTTP 503 for url",
        ):
            self.assertNotIn(private_value, rendered)

    @patch("backend.recommendations.save_recommendation_cache")
    @patch("backend.recommendations.build_recommendation_cache")
    @patch("backend.recommendations._service_recommendation_exclusions")
    @patch("backend.recommendations.recommendation_users")
    def test_refresh_collects_service_exclusions_once_for_all_users(
        self,
        recommendation_users,
        service_exclusions,
        build_cache,
        save_cache,
    ):
        recommendation_users.return_value = [
            {
                "id": 1,
                "username": "first",
                "listenbrainz_username": "first-listener",
                "lastfm_username": None,
                "lastfm_api_key": None,
            },
            {
                "id": 2,
                "username": "second",
                "listenbrainz_username": "second-listener",
                "lastfm_username": None,
                "lastfm_api_key": None,
            },
        ]
        shared = recommendation_engine._empty_exclusions()
        service_exclusions.return_value = shared
        build_cache.return_value = {"providerStatus": {}}

        retry_required = recommendation_engine.refresh_recommendation_cache()

        self.assertFalse(retry_required)
        service_exclusions.assert_called_once_with()
        self.assertEqual(build_cache.call_count, 2)
        self.assertTrue(all(
            call.kwargs["shared_exclusions"] is shared
            for call in build_cache.call_args_list
        ))
        self.assertEqual(save_cache.call_count, 2)

    @patch("backend.recommendations.save_recommendation_cache")
    @patch("backend.recommendations.build_recommendation_cache")
    @patch("backend.recommendations.recommendation_users")
    def test_partial_provider_cache_is_saved_and_requests_retry(
        self, recommendation_users, build_cache, save_cache
    ):
        recommendation_users.return_value = [{"id": 1, "username": "listener"}]
        payload = {
            "artists": [{"id": "lastfm-artist"}],
            "providerStatus": {
                "listenbrainz": "unavailable",
                "lastfm": "ok",
            },
        }
        build_cache.return_value = payload

        retry_required = recommendation_engine.refresh_recommendation_cache()

        save_cache.assert_called_once_with(1, payload)
        self.assertTrue(retry_required)


class ArtworkCacheTests(DatabaseTestCase):
    @patch("backend.artwork_cache.requests.get")
    def test_oversized_authenticated_artwork_does_not_redirect_or_poison_cache(self, get):
        get.return_value = Response(
            headers={"Content-Type": "image/jpeg"}, chunks=(b"too-large",),
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(artwork_cache, "ARTWORK_CACHE_DIRECTORY", directory),
            patch.object(artwork_cache, "ARTWORK_MAX_DOWNLOAD_BYTES", 4),
            self.app.test_request_context(),
        ):
            result = self.app.make_response(artwork_cache.cached_artwork(
                "plex-artist-oversized", "http://plex:32400/thumbnail",
                headers={"X-Plex-Token": "private-token"},
            ))
            self.assertEqual(result.status_code, 502)
            self.assertNotIn("Location", result.headers)
            self.assertEqual(result.headers["Cache-Control"], "no-store")
            self.assertEqual(os.listdir(directory), [])

    @patch("backend.artwork_cache.requests.get")
    def test_authenticated_artwork_rejects_provider_redirect(self, get):
        get.return_value = Response(
            307,
            headers={"Location": "https://other.test/private-artwork"},
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(artwork_cache, "ARTWORK_CACHE_DIRECTORY", directory),
            self.app.test_request_context(),
        ):
            result = self.app.make_response(artwork_cache.cached_artwork(
                "plex-artist-redirect",
                "http://plex:32400/thumbnail",
                headers={"X-Plex-Token": "private-token"},
            ))

        self.assertEqual(result.status_code, 502)
        self.assertNotIn("Location", result.headers)
        get.assert_called_once()
        self.assertFalse(get.call_args.kwargs["allow_redirects"])
        self.assertEqual(
            get.call_args.kwargs["headers"],
            {"X-Plex-Token": "private-token"},
        )

    def artwork_url(self, mbid):
        return f"/api/artwork/release-group/{mbid}"

    @patch("backend.artwork_cache.trim_artwork_cache")
    @patch(
        "backend.artwork_cache._estimated_artwork_cache_size",
        return_value=artwork_cache.ARTWORK_CACHE_HIGH_WATER_BYTES + 1,
    )
    @patch("backend.artwork_cache.time.monotonic", return_value=100.0)
    def test_artwork_trim_runs_at_most_once_per_interval(
        self, monotonic, estimated_size, trim
    ):
        original_last_trim = artwork_cache._last_trim_at
        artwork_cache._last_trim_at = None
        try:
            self.assertTrue(artwork_cache.maybe_trim_artwork_cache())
            self.assertFalse(artwork_cache.maybe_trim_artwork_cache())
            trim.assert_called_once_with()

            monotonic.return_value += artwork_cache.ARTWORK_CACHE_TRIM_INTERVAL + 1
            self.assertTrue(artwork_cache.maybe_trim_artwork_cache())
            self.assertEqual(trim.call_count, 2)
        finally:
            artwork_cache._last_trim_at = original_last_trim

    @patch("backend.artwork_cache.trim_artwork_cache")
    @patch("backend.artwork_cache._estimated_artwork_cache_size")
    def test_artwork_trim_skips_directory_scan_below_high_water(
        self, estimated_size, trim
    ):
        estimated_size.return_value = artwork_cache.ARTWORK_CACHE_HIGH_WATER_BYTES

        self.assertFalse(artwork_cache.maybe_trim_artwork_cache())

        trim.assert_not_called()

    def test_artwork_size_estimate_updates_without_repeated_scans(self):
        cache_key = "release-group-size-accounting"
        os.makedirs(ARTWORK_CACHE_DIRECTORY, exist_ok=True)
        final_path = os.path.join(ARTWORK_CACHE_DIRECTORY, f"{cache_key}.jpg")

        with patch(
            "backend.artwork_cache._scan_evictable_entries",
            return_value=[(0, 100, "existing.jpg")],
        ) as scan:
            self.assertEqual(artwork_cache._estimated_artwork_cache_size(), 100)

            with tempfile.NamedTemporaryFile(
                "wb", dir=ARTWORK_CACHE_DIRECTORY, delete=False
            ) as file:
                first_temporary = file.name
                file.write(b"12345")
            artwork_cache._replace_cache_file(first_temporary, final_path)
            self.assertEqual(artwork_cache._estimated_artwork_cache_size(), 105)

            with tempfile.NamedTemporaryFile(
                "wb", dir=ARTWORK_CACHE_DIRECTORY, delete=False
            ) as file:
                second_temporary = file.name
                file.write(b"12")
            artwork_cache._replace_cache_file(second_temporary, final_path)
            self.assertEqual(artwork_cache._estimated_artwork_cache_size(), 102)

        scan.assert_called_once_with()

    @patch("backend.artwork_cache.requests.get")
    def test_concurrent_cache_misses_share_one_download(self, get):
        cache_key = "release-group-concurrent-download"
        download_started = Event()
        release_download = Event()

        def fetch(*args, **kwargs):
            download_started.set()
            self.assertTrue(release_download.wait(2))
            return Response(
                headers={"Content-Type": "image/jpeg"},
                chunks=(b"shared-image",),
            )

        get.side_effect = fetch
        responses = []

        def request_artwork():
            with self.app.test_request_context():
                response = artwork_cache.cached_artwork(
                    cache_key, "https://images.example/shared.jpg"
                )
                response.direct_passthrough = False
                responses.append((response.status_code, response.get_data()))
                response.close()

        first = Thread(target=request_artwork)
        second = Thread(target=request_artwork)
        first.start()
        self.assertTrue(download_started.wait(2))
        second.start()
        time.sleep(0.05)
        self.assertEqual(get.call_count, 1)
        release_download.set()
        first.join(2)
        second.join(2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(get.call_count, 1)
        self.assertEqual(responses, [(200, b"shared-image"), (200, b"shared-image")])
        self.assertNotIn(cache_key, artwork_cache._key_locks)

    def test_concurrent_variant_misses_share_one_resize(self):
        cache_key = "release-group-concurrent-resize"
        os.makedirs(ARTWORK_CACHE_DIRECTORY, exist_ok=True)
        original_path = os.path.join(ARTWORK_CACHE_DIRECTORY, f"{cache_key}.jpg")
        with open(original_path, "wb") as file:
            file.write(encoded_image(1000))

        resize_started = Event()
        release_resize = Event()
        actual_build = artwork_cache.build_artwork_variant

        def build_variant(*args, **kwargs):
            resize_started.set()
            self.assertTrue(release_resize.wait(2))
            return actual_build(*args, **kwargs)

        responses = []

        def request_artwork():
            with self.app.test_request_context():
                response = artwork_cache.cached_artwork(
                    cache_key, None, size="thumb"
                )
                response.direct_passthrough = False
                responses.append((response.status_code, response.get_data()))
                response.close()

        with patch(
            "backend.artwork_cache.build_artwork_variant",
            side_effect=build_variant,
        ) as build:
            first = Thread(target=request_artwork)
            second = Thread(target=request_artwork)
            first.start()
            self.assertTrue(resize_started.wait(2))
            second.start()
            time.sleep(0.05)
            self.assertEqual(build.call_count, 1)
            release_resize.set()
            first.join(2)
            second.join(2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(build.call_count, 1)
        self.assertEqual(len(responses), 2)
        self.assertEqual(responses[0][0], 200)
        self.assertEqual(responses[0], responses[1])
        self.assertNotIn(cache_key, artwork_cache._key_locks)

    @patch("backend.artwork_cache.requests.get")
    def test_downloaded_artwork_is_served_from_disk_on_next_request(self, get):
        mbid = "33333333-3333-3333-3333-333333333333"
        get.return_value = Response(
            headers={"Content-Type": "image/jpeg"},
            chunks=(b"cover-", b"bytes"),
        )
        self.register()

        first = self.client.get(self.artwork_url(mbid))
        second = self.client.get(self.artwork_url(mbid))

        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.data, b"cover-bytes")
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.data, b"cover-bytes")
        get.assert_called_once()
        self.assertTrue(os.path.isfile(os.path.join(
            ARTWORK_CACHE_DIRECTORY,
            f"release-group-{mbid}.jpg",
        )))
        first.close()
        second.close()

    @patch("backend.routes.artwork.lidarr.artist_image_url")
    @patch("backend.artwork_cache.requests.get")
    def test_large_artist_artwork_reuses_disk_cache_without_lidarr_lookup(
        self, get, artist_image_url
    ):
        mbid = "66666666-6666-6666-6666-666666666666"
        artist_image_url.return_value = "https://images.example/artist.jpg"
        get.return_value = Response(
            headers={"Content-Type": "image/jpeg"},
            chunks=(b"artist-image",),
        )
        self.register()

        thumbnail = self.client.get(f"/api/artwork/artist/{mbid}")
        large = self.client.get(f"/api/artwork/artist/{mbid}/large")

        self.assertEqual(thumbnail.status_code, 200)
        self.assertEqual(large.status_code, 200)
        artist_image_url.assert_called_once_with(mbid)
        get.assert_called_once()
        thumbnail.close()
        large.close()

    @patch("backend.artwork_cache.requests.get")
    def test_missing_artwork_uses_negative_cache(self, get):
        mbid = "44444444-4444-4444-4444-444444444444"
        get.return_value = Response(404)
        self.register()

        first = self.client.get(self.artwork_url(mbid))
        second = self.client.get(self.artwork_url(mbid))

        self.assertEqual(first.status_code, 404)
        self.assertEqual(second.status_code, 404)
        get.assert_called_once()
        self.assertTrue(os.path.isfile(os.path.join(
            ARTWORK_CACHE_DIRECTORY,
            f"release-group-{mbid}.miss",
        )))

    def test_negative_cache_removes_expired_markers_and_caps_fresh_entries(self):
        with tempfile.TemporaryDirectory(prefix="melodarr-artwork-misses-") as directory:
            timestamps = {
                "expired.miss": 899,
                "oldest.miss": 920,
                "middle.miss": 940,
                "newest.miss": 960,
            }
            for name, modified_at in timestamps.items():
                path = os.path.join(directory, name)
                with open(path, "w", encoding="utf-8"):
                    pass
                os.utime(path, (modified_at, modified_at))
            image_path = os.path.join(directory, "cover.jpg")
            with open(image_path, "wb") as file:
                file.write(b"image")

            with (
                patch.object(artwork_cache, "ARTWORK_CACHE_DIRECTORY", directory),
                patch.object(artwork_cache, "ARTWORK_MISS_TTL", 100),
                patch.object(artwork_cache, "ARTWORK_MISS_CACHE_MAX_ENTRIES", 2),
            ):
                removed = artwork_cache.trim_artwork_miss_cache(now=1000)

            self.assertEqual(removed, 2)
            self.assertEqual(
                sorted(name for name in os.listdir(directory) if name.endswith(".miss")),
                ["middle.miss", "newest.miss"],
            )
            self.assertTrue(os.path.isfile(image_path))

    @patch("backend.artwork_cache.requests.get")
    def test_expired_negative_cache_is_renewed_by_one_upstream_404(self, get):
        get.return_value = Response(404)
        cache_key = "release-group-renewed-miss"
        with tempfile.TemporaryDirectory(prefix="melodarr-artwork-renew-") as directory:
            miss_file = os.path.join(directory, f"{cache_key}.miss")
            with open(miss_file, "w", encoding="utf-8"):
                pass
            os.utime(miss_file, (800, 800))

            with (
                patch.object(artwork_cache, "ARTWORK_CACHE_DIRECTORY", directory),
                patch.object(artwork_cache, "ARTWORK_MISS_TTL", 100),
                patch.object(artwork_cache.time, "time", return_value=1000),
                self.app.test_request_context(),
            ):
                first = artwork_cache.cached_artwork(
                    cache_key, "https://images.example/renewed.jpg"
                )
                second = artwork_cache.cached_artwork(
                    cache_key, "https://images.example/renewed.jpg"
                )

            self.assertEqual(first, ("", 404))
            self.assertEqual(second, ("", 404))
            get.assert_called_once_with(
                "https://images.example/renewed.jpg",
                headers=None,
                stream=True,
                timeout=20,
            )
            self.assertEqual(os.path.getmtime(miss_file), 1000)

    @patch("backend.artwork_cache.requests.get")
    def test_successful_refresh_removes_expired_negative_cache_marker(self, get):
        get.return_value = Response(
            headers={"Content-Type": "image/jpeg"},
            chunks=(b"new-artwork",),
        )
        cache_key = "release-group-recovered-artwork"
        with tempfile.TemporaryDirectory(prefix="melodarr-artwork-success-") as directory:
            miss_file = os.path.join(directory, f"{cache_key}.miss")
            with open(miss_file, "w", encoding="utf-8"):
                pass
            os.utime(miss_file, (800, 800))

            with (
                patch.object(artwork_cache, "ARTWORK_CACHE_DIRECTORY", directory),
                patch.object(artwork_cache, "ARTWORK_MISS_TTL", 100),
                patch.object(artwork_cache.time, "time", return_value=1000),
                self.app.test_request_context(),
            ):
                response = artwork_cache.cached_artwork(
                    cache_key, "https://images.example/recovered.jpg"
                )
                response.direct_passthrough = False
                self.assertEqual(response.get_data(), b"new-artwork")
                response.close()

            self.assertFalse(os.path.exists(miss_file))
            self.assertTrue(
                os.path.isfile(os.path.join(directory, f"{cache_key}.jpg"))
            )

    @patch("backend.artwork_cache.ARTWORK_MAX_DOWNLOAD_BYTES", 5)
    @patch("backend.artwork_cache.requests.get")
    def test_oversized_artwork_falls_back_to_provider_redirect(self, get):
        mbid = "55555555-5555-5555-5555-555555555555"
        get.return_value = Response(
            headers={"Content-Type": "image/jpeg"},
            chunks=(b"123456",),
        )
        self.register()

        with self.assertLogs(level="WARNING") as logs:
            response = self.client.get(self.artwork_url(mbid))

        self.assertEqual(response.status_code, 302)
        self.assertIn(
            f"/release-group/{mbid}/front-500",
            response.headers["Location"],
        )
        self.assertFalse(os.path.exists(os.path.join(
            ARTWORK_CACHE_DIRECTORY,
            f"release-group-{mbid}.jpg",
        )))
        self.assertIn("too large to cache", logs.output[0])


def encoded_image(size, colour=(90, 30, 160)):
    """Return JPEG bytes for a square test image."""
    buffer = io.BytesIO()
    Image.new("RGB", (size, size), colour).save(buffer, format="JPEG")
    return buffer.getvalue()


class ArtworkVariantTests(DatabaseTestCase):
    def artwork_url(self, mbid, size=None):
        url = f"/api/artwork/release-group/{mbid}"
        return f"{url}?size={size}" if size else url

    @patch("backend.artwork_cache.requests.get")
    def test_requested_size_is_downscaled_to_webp_once(self, get):
        mbid = "77777777-7777-7777-7777-777777777777"
        original = encoded_image(1000)
        get.return_value = Response(
            headers={"Content-Type": "image/jpeg"},
            chunks=(original,),
        )
        self.register()

        first = self.client.get(self.artwork_url(mbid, "thumb"))
        second = self.client.get(self.artwork_url(mbid, "thumb"))

        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.data, second.data)
        # One upstream download serves every variant of the same artwork.
        get.assert_called_once()
        self.assertLess(len(first.data), len(original))
        with Image.open(io.BytesIO(first.data)) as image:
            self.assertEqual(image.format, "WEBP")
            self.assertEqual(max(image.size), 128)
        first.close()
        second.close()

    @patch("backend.artwork_cache.requests.get")
    def test_variants_and_original_use_separate_cache_files(self, get):
        mbid = "88888888-8888-8888-8888-888888888888"
        get.return_value = Response(
            headers={"Content-Type": "image/jpeg"},
            chunks=(encoded_image(1000),),
        )
        self.register()

        thumb = self.client.get(self.artwork_url(mbid, "thumb"))
        large = self.client.get(self.artwork_url(mbid, "large"))
        original = self.client.get(self.artwork_url(mbid))

        get.assert_called_once()
        for name in (
            f"release-group-{mbid}.jpg",
            f"release-group-{mbid}@thumb.webp",
            f"release-group-{mbid}@large.webp",
        ):
            self.assertTrue(
                os.path.isfile(os.path.join(ARTWORK_CACHE_DIRECTORY, name)), name
            )
        self.assertNotEqual(thumb.data, large.data)
        self.assertNotEqual(thumb.data, original.data)
        thumb.close()
        large.close()
        original.close()

    @patch("backend.artwork_cache.requests.get")
    def test_unsupported_size_serves_the_original_image(self, get):
        mbid = "99999999-9999-9999-9999-999999999999"
        original = encoded_image(300)
        get.return_value = Response(
            headers={"Content-Type": "image/jpeg"},
            chunks=(original,),
        )
        self.register()

        response = self.client.get(self.artwork_url(mbid, "enormous"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, original)
        response.close()

    def test_stale_cleanup_keeps_variants_of_current_plex_artists(self):
        with tempfile.TemporaryDirectory(
            prefix="melodarr-stale-artwork-"
        ) as artwork_directory, patch.object(
            artwork_cache,
            "ARTWORK_CACHE_DIRECTORY",
            artwork_directory,
        ):
            kept = artwork_cache.plex_artist_artwork_key(
                "server-1", "100", "/thumb/100/new"
            )
            old_version = artwork_cache.plex_artist_artwork_key(
                "server-1", "100", "/thumb/100/old"
            )
            removed = artwork_cache.plex_artist_artwork_key(
                "server-1", "200", "/thumb/200"
            )
            names = [
                f"{kept}.jpg",
                f"{kept}@thumb.webp",
                f"{kept}@card.webp",
                f"{old_version}.jpg",
                f"{old_version}@card.webp",
                f"{removed}.jpg",
                f"{removed}@thumb.webp",
            ]
            for name in names:
                with open(os.path.join(artwork_directory, name), "wb") as file:
                    file.write(b"image")

            deleted = artwork_cache.remove_stale_plex_artist_artwork({kept})

            self.assertEqual(deleted, 4)
            for name in names[:3]:
                self.assertTrue(
                    os.path.isfile(os.path.join(artwork_directory, name)), name
                )
            for name in names[3:]:
                self.assertFalse(
                    os.path.exists(os.path.join(artwork_directory, name)), name
                )

    @patch("backend.routes.artwork.cached_artwork")
    @patch("backend.routes.artwork.plex.cached_library_index")
    @patch("backend.routes.artwork.get_service")
    def test_plex_artist_route_uses_current_thumbnail_cache_key(
        self,
        get_service_mock,
        cached_library_index,
        cached_artwork,
    ):
        get_service_mock.return_value = {
            "url": "http://plex:32400",
            "token": "token",
            "machineIdentifier": "server-1",
        }
        cached_library_index.return_value = {
            "artistsByRatingKey": {
                "100": {"thumb": "/library/metadata/100/thumb/200"}
            }
        }
        cached_artwork.return_value = ("", 204)
        self.register()

        response = self.client.get(
            "/api/artwork/plex-artist/100?v=browser-version&size=card"
        )

        self.assertEqual(response.status_code, 204)
        cached_artwork.assert_called_once_with(
            artwork_cache.plex_artist_artwork_key(
                "server-1",
                "100",
                "/library/metadata/100/thumb/200",
            ),
            "http://plex:32400/photo/:/transcode?"
            "url=%2Flibrary%2Fmetadata%2F100%2Fthumb%2F200"
            "&width=640&height=640&minSize=0&upscale=0&format=jpeg",
            headers={"X-Plex-Token": "token"},
            size="card",
        )

    @patch("backend.routes.artwork.plex.cached_library_index")
    @patch("backend.routes.artwork.get_service")
    @patch("backend.artwork_cache.requests.get")
    def test_plex_artwork_failure_stays_on_proxy_and_next_request_recovers(
        self, get, get_service_mock, library_index
    ):
        get_service_mock.return_value = {
            "url": "http://plex:32400", "token": "private-token",
        }
        library_index.return_value = {
            "artistsByRatingKey": {
                "101": {"thumb": "/library/metadata/101/thumb/201"},
            },
        }
        get.side_effect = [
            requests.Timeout("Plex temporarily unavailable"),
            Response(headers={"Content-Type": "image/jpeg"}, chunks=(encoded_image(640),)),
        ]
        self.register()
        url = "/api/artwork/plex-artist/101?size=card"
        failed = self.client.get(url)
        self.assertEqual(failed.status_code, 502)
        self.assertNotIn("Location", failed.headers)
        self.assertEqual(failed.headers["Cache-Control"], "no-store")

        recovered = self.client.get(url)
        cached = self.client.get(url)
        self.assertEqual(recovered.status_code, 200)
        self.assertEqual(cached.data, recovered.data)
        self.assertEqual(get.call_count, 2)
        self.assertEqual(get.call_args.kwargs["headers"], {"X-Plex-Token": "private-token"})
        self.assertIn("/photo/:/transcode?", get.call_args.args[0])
        with Image.open(io.BytesIO(recovered.data)) as image:
            self.assertEqual(image.size, (384, 384))
        recovered.close()
        cached.close()


class StoragePerformanceTests(DatabaseTestCase):
    def test_request_history_queries_use_recency_indexes(self):
        with db() as connection:
            indexes = {
                row["name"] for row in connection.execute(
                    "PRAGMA index_list(request_history)"
                )
            }
            user_plan = [
                row["detail"] for row in connection.execute(
                    "EXPLAIN QUERY PLAN "
                    "SELECT id, use_for_recommendations, kind, mbid, name, "
                    "artist_name, release_type, release_date, anime_slug, "
                    "anime_name, theme_id, theme_label, song_id, song_title, "
                    "created_at FROM request_history WHERE user_id = ? "
                    "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                    (1, 100, 200),
                )
            ]
            admin_plan = [
                row["detail"] for row in connection.execute(
                    """
                    EXPLAIN QUERY PLAN
                    SELECT
                        request_history.id AS request_id,
                        request_history.user_id,
                        request_history.kind,
                        request_history.mbid,
                        request_history.name,
                        request_history.artist_name,
                        request_history.release_type,
                        request_history.release_date,
                        request_history.anime_slug,
                        request_history.anime_name,
                        request_history.theme_id,
                        request_history.theme_label,
                        request_history.song_id,
                        request_history.song_title,
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
                    (100, 200),
                )
            ]

        self.assertIn("request_history_user_recent", indexes)
        self.assertIn("request_history_recent", indexes)
        self.assertTrue(any(
            "request_history_user_recent" in detail for detail in user_plan
        ))
        self.assertTrue(any(
            "request_history_recent" in detail for detail in admin_plan
        ))
        self.assertFalse(any(
            detail == "SCAN request_history" for detail in (*user_plan, *admin_plan)
        ))
        self.assertFalse(any(
            "USE TEMP B-TREE" in detail for detail in (*user_plan, *admin_plan)
        ))

    def test_settings_file_is_parsed_once_and_results_are_isolated(self):
        storage_module.write_settings_file({
            "lidarr": {"url": "http://lidarr:8686"},
            "plex": {"url": "http://plex:32400"},
        })
        storage_module._settings_cache_signature = ("stale",)
        storage_module._settings_cache_value = None

        with patch("backend.storage.json.load", wraps=json.load) as load:
            first = storage_module.load_settings_file()
            first["lidarr"]["url"] = "mutated"
            second = storage_module.load_settings_file()

        self.assertEqual(load.call_count, 1)
        self.assertEqual(second["lidarr"]["url"], "http://lidarr:8686")

    def test_same_size_external_settings_replacement_invalidates_memo(self):
        original = {
            "lidarr": {"url": "http://one.test"},
            "plex": {"url": "http://plex.test"},
        }
        replacement = {
            "lidarr": {"url": "http://two.test"},
            "plex": {"url": "http://plex.test"},
        }
        write_settings_file(original)
        primed = storage_module.load_settings_file()
        original_signature = storage_module._settings_cache_signature
        original_size = os.stat(storage_module.SETTINGS_FILE).st_size

        replacement_json = json.dumps(replacement, indent=2) + "\n"
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=os.path.dirname(storage_module.SETTINGS_FILE),
                delete=False,
            ) as file:
                file.write(replacement_json)
                temporary_path = file.name
            self.assertEqual(os.stat(temporary_path).st_size, original_size)
            os.replace(temporary_path, storage_module.SETTINGS_FILE)
            temporary_path = None
        finally:
            if temporary_path and os.path.exists(temporary_path):
                os.unlink(temporary_path)

        replacement_signature = storage_module._settings_file_signature()
        self.assertEqual(replacement_signature[1], original_signature[1])
        self.assertNotEqual(replacement_signature, original_signature)

        reloaded = storage_module.load_settings_file()
        self.assertEqual(primed, original)
        self.assertEqual(reloaded, replacement)

        reloaded["lidarr"]["url"] = "mutated"
        self.assertEqual(
            storage_module.load_settings_file()["lidarr"]["url"],
            "http://two.test",
        )


class CompressionTests(DatabaseTestCase):
    def test_precompressed_static_asset_is_served_without_runtime_compression(self):
        source_path = os.path.join(TEST_DATA.name, "precompressed.js")
        with open(f"{source_path}.br", "wb") as file:
            file.write(b"build-time-brotli")

        with patch("backend.application.safe_join", return_value=source_path):
            response = self.client.get(
                "/static/app.js",
                headers={"Accept-Encoding": "br, gzip"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"build-time-brotli")
        self.assertEqual(response.headers["Content-Encoding"], "br")
        self.assertIn("immutable", response.headers["Cache-Control"])
        self.assertIn("Accept-Encoding", response.headers["Vary"])
        response.close()

    @patch("backend.routes.library.plex.cached_library_snapshot")
    @patch("backend.routes.library.get_service")
    def test_large_json_is_gzipped_only_when_the_client_accepts_it(
        self, get_service_mock, cached_snapshot
    ):
        get_service_mock.return_value = {"url": "http://plex:32400", "token": "token"}
        cached_snapshot.return_value = {
            "artists": [
                {"name": f"Artist {index}", "section": "Music", "url": "u"}
                for index in range(400)
            ],
            "releaseGroups": [],
            "scannedAt": 1,
        }
        self.register()

        compressed = self.client.get(
            "/api/library", headers={"Accept-Encoding": "gzip"}
        )
        plain = self.client.get("/api/library", headers={"Accept-Encoding": ""})

        self.assertEqual(compressed.headers.get("Content-Encoding"), "gzip")
        self.assertIsNone(plain.headers.get("Content-Encoding"))
        self.assertIn("Accept-Encoding", compressed.headers.get("Vary", ""))
        self.assertIn("Accept-Encoding", plain.headers.get("Vary", ""))
        self.assertEqual(
            gzip.decompress(compressed.data),
            plain.data,
        )
        self.assertLess(len(compressed.data), len(plain.data))

    def test_small_responses_are_not_compressed(self):
        response = self.client.get(
            "/api/auth/status", headers={"Accept-Encoding": "gzip"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.headers.get("Content-Encoding"))

    @patch("backend.routes.library.plex.cached_library_snapshot")
    @patch("backend.routes.library.get_service")
    def test_gzip_with_zero_quality_is_not_used(
        self, get_service_mock, cached_snapshot
    ):
        get_service_mock.return_value = {
            "url": "http://plex:32400",
            "token": "token",
        }
        cached_snapshot.return_value = {
            "artists": [{"name": "Artist"} for _ in range(400)],
            "releaseGroups": [],
        }
        self.register()

        response = self.client.get(
            "/api/library",
            headers={"Accept-Encoding": "gzip;q=0"},
        )

        self.assertIsNone(response.headers.get("Content-Encoding"))

    @patch("backend.artwork_cache.requests.get")
    def test_streamed_artwork_is_never_recompressed(self, get):
        mbid = "12121212-1212-1212-1212-121212121212"
        get.return_value = Response(
            headers={"Content-Type": "image/jpeg"},
            chunks=(encoded_image(600),),
        )
        self.register()

        response = self.client.get(
            f"/api/artwork/release-group/{mbid}?size=card",
            headers={"Accept-Encoding": "gzip"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.headers.get("Content-Encoding"))
        response.close()


class LibraryRouteTests(DatabaseTestCase):
    def sign_in_as_invited_user(self):
        """Register the owner, then join and sign in as a non-admin account."""
        csrf = self.register()
        invitation = self.client.post(
            "/api/account/invitations", headers={"X-CSRF-Token": csrf}
        )
        token = parse_qs(urlparse(invitation.get_json()["path"]).query)["invite"][0]
        self.client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})
        response = self.client.post(
            "/api/auth/register",
            json={
                "username": "invited-user",
                "password": "another-secure-password",
                "invitationToken": token,
            },
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["role"], "user")

    @patch("backend.routes.library.plex.library_snapshot")
    @patch("backend.routes.library.plex.cached_library_snapshot")
    @patch("backend.routes.library.get_service")
    def test_invited_users_read_the_cached_library_without_a_scan(
        self, get_service_mock, cached_snapshot, live_snapshot
    ):
        get_service_mock.return_value = {"url": "http://plex:32400", "token": "token"}
        cached_snapshot.return_value = {
            "artists": [{
                "name": "alpha",
                "sortName": "The Alpha",
                "section": "Music",
                "musicbrainzId": "11111111-1111-1111-1111-111111111111",
                "artwork": "/api/artwork/plex-artist/1",
                "url": "https://app.plex.tv/",
                "thumb": "/library/metadata/1/thumb",
                "guids": ["plex://artist/1"],
                "plexGuid": "plex://artist/1",
                "key": "/library/metadata/1/children",
            }],
            "releaseGroups": [{"name": "An album"}],
            "scannedAt": 1234,
        }
        self.sign_in_as_invited_user()

        response = self.client.get("/api/library")

        self.assertEqual(response.status_code, 200)
        live_snapshot.assert_not_called()
        payload = response.get_json()
        self.assertEqual(payload["artistCount"], 1)
        self.assertEqual(payload["releaseGroupCount"], 1)
        # Plex GUIDs and internal keys stay on the server.
        self.assertEqual(
            set(payload["artists"][0]),
            {"name", "sortName", "section", "musicbrainzId", "artwork", "url"},
        )
        self.assertEqual(payload["artists"][0]["sortName"], "The Alpha")
        self.assertTrue(response.headers.get("ETag"))

    @patch("backend.routes.library.plex.library_snapshot")
    @patch("backend.routes.library.plex.cached_library_snapshot")
    @patch("backend.routes.library.get_service")
    def test_stale_cache_falls_back_to_a_current_snapshot(
        self, get_service_mock, cached_snapshot, live_snapshot
    ):
        get_service_mock.return_value = {"url": "http://plex:32400", "token": "token"}
        cached_snapshot.return_value = {
            "snapshotVersion": plex.SNAPSHOT_VERSION - 1,
            "artists": [{"name": "Album incorrectly cached as an artist"}],
            "releaseGroups": [{"name": "Album"}],
        }
        live_snapshot.return_value = {
            "snapshotVersion": plex.SNAPSHOT_VERSION,
            "artists": [{"name": "Correct Artist"}],
            "releaseGroups": [{"name": "Album"}],
        }
        self.register()

        response = self.client.get("/api/library")

        self.assertEqual(response.status_code, 200)
        live_snapshot.assert_called_once()
        self.assertEqual(
            [artist["name"] for artist in response.get_json()["artists"]],
            ["Correct Artist"],
        )

    @patch("backend.routes.library.plex.library_snapshot")
    @patch("backend.routes.library.plex.cached_library_snapshot")
    @patch("backend.routes.library.get_service")
    def test_empty_cache_falls_back_to_a_live_scan(
        self, get_service_mock, cached_snapshot, live_snapshot
    ):
        get_service_mock.return_value = {"url": "http://plex:32400", "token": "token"}
        cached_snapshot.return_value = {"artists": [], "releaseGroups": []}
        live_snapshot.return_value = {
            "artists": [{"name": "alpha"}],
            "releaseGroups": [],
        }
        self.register()

        response = self.client.get("/api/library")

        self.assertEqual(response.status_code, 200)
        live_snapshot.assert_called_once()
        self.assertEqual(response.get_json()["artistCount"], 1)


class LibraryIndexMemoizationTests(DatabaseTestCase):
    def test_snapshot_is_parsed_once_per_request(self):
        config = {"url": "http://plex:32400", "token": "token"}
        snapshot = {
            "artists": [{
                "name": "alpha",
                "musicbrainzId": "11111111-1111-1111-1111-111111111111",
                "ratingKey": "10",
            }],
            "releaseGroups": [{
                "name": "An album",
                "ratingKey": "20",
                "musicbrainzReleaseGroupId": "22222222-2222-2222-2222-222222222222",
            }],
        }
        with patch(
            "backend.services.plex.get_cache_document", return_value=snapshot
        ) as read:
            with self.app.test_request_context("/"):
                first = plex.cached_library_index(config)
                second = plex.cached_library_index(config)

        self.assertIs(first, second)
        read.assert_called_once()
        self.assertEqual(
            set(first["artistsByMbid"]), {"11111111-1111-1111-1111-111111111111"}
        )
        self.assertEqual(
            set(first["releaseGroupsByMbid"]),
            {"22222222-2222-2222-2222-222222222222"},
        )
        self.assertEqual(set(first["artistsByRatingKey"]), {"10"})
        self.assertEqual(set(first["releaseGroupsByRatingKey"]), {"20"})


class PlexHistoryClientTests(unittest.TestCase):
    @patch("backend.services.plex_history.requests.get")
    def test_accounts_normalize_server_local_ids_and_aliases(self, get):
        get.return_value = Response(payload={"MediaContainer": {
            "Account": [
                {"id": 1, "name": "JRamperSaud123", "title": "Jeremy"},
                {"id": 2, "name": "Managed Listener"},
            ],
        }})

        result = plex_history.accounts({
            "url": "http://plex:32400",
            "token": "server-token",
        })

        self.assertEqual(result, [
            {
                "account_id": "1",
                "aliases": ("JRamperSaud123", "Jeremy"),
            },
            {
                "account_id": "2",
                "aliases": ("Managed Listener",),
            },
        ])
        self.assertEqual(
            get.call_args.kwargs["headers"]["X-Plex-Token"],
            "server-token",
        )

    @patch("backend.services.plex_history.requests.get")
    def test_accounts_reject_cross_origin_redirect(self, get):
        get.return_value = Response(
            302,
            headers={"Location": "https://other.test/accounts"},
        )

        with self.assertRaises(RedirectRejected):
            plex_history.accounts({
                "url": "http://plex:32400",
                "token": "server-token",
            })

        get.assert_called_once()
        self.assertFalse(get.call_args.kwargs["allow_redirects"])
        self.assertEqual(
            get.call_args.kwargs["headers"]["X-Plex-Token"],
            "server-token",
        )

    @patch(
        "backend.services.plex_history.plex.cached_library_index",
        return_value={},
    )
    @patch("backend.services.plex_history.requests.get")
    def test_history_is_paginated_and_normalized_without_track_metadata(
        self, get, _cached_library_index
    ):
        get.side_effect = [
            Response(payload={"MediaContainer": {
                "totalSize": 2,
                "offset": 0,
                "size": 1,
                "Metadata": [{
                    "type": "track",
                    "historyKey": "/status/sessions/history/100",
                    "grandparentRatingKey": "artist-10",
                    "parentRatingKey": "album-20",
                    "librarySectionID": "music",
                    "viewedAt": 1_000,
                    "User": {"id": 1, "title": "Listener"},
                    "title": "Intentionally not persisted",
                }],
            }}),
            Response(payload={"MediaContainer": {
                "totalSize": 2,
                "offset": 1,
                "size": 1,
                "Metadata": [{
                    "type": "track",
                    "historyKey": "/status/sessions/history/101",
                    "grandparentRatingKey": "artist-11",
                    "librarySectionID": "music",
                    "viewedAt": 2_000,
                    "accountID": 2,
                }],
            }}),
        ]

        events = list(plex_history.iter_history(
            {"url": "http://plex:32400", "token": "token"},
            since=500,
            until=2_500,
            section_ids=["music"],
            page_size=1,
        ))

        self.assertEqual(events, [
            {
                "history_key": "/status/sessions/history/100",
                "account_id": "1",
                "artist_rating_key": "artist-10",
                "album_rating_key": "album-20",
                "played_at": 1_000.0,
            },
            {
                "history_key": "/status/sessions/history/101",
                "account_id": "2",
                "artist_rating_key": "artist-11",
                "album_rating_key": None,
                "played_at": 2_000.0,
            },
        ])
        self.assertEqual(
            get.call_args_list[1].kwargs["params"]["X-Plex-Container-Start"],
            1,
        )
        history_params = get.call_args_list[0].kwargs["params"]
        self.assertEqual(history_params["sort"], "viewedAt:desc")
        self.assertNotIn("librarySectionID", history_params)
        self.assertNotIn("viewedAt>", history_params)
        self.assertNotIn("type", history_params)
        self.assertNotIn("viewedAt>=", history_params)
        self.assertNotIn("viewedAt<=", history_params)

    @patch(
        "backend.services.plex_history.plex.cached_library_index",
        return_value={},
    )
    @patch("backend.services.plex_history.requests.get")
    def test_history_enforces_exact_window_and_track_type_client_side(
        self, get, _cached_library_index
    ):
        get.return_value = Response(payload={"MediaContainer": {
            "totalSize": 4,
            "offset": 0,
            "size": 4,
            "Metadata": [
                {
                    "type": "track",
                    "historyKey": "too-old",
                    "grandparentRatingKey": "artist-1",
                    "librarySectionID": "music",
                    "viewedAt": 499,
                    "accountID": 1,
                },
                {
                    "type": "movie",
                    "historyKey": "movie",
                    "grandparentRatingKey": "artist-1",
                    "librarySectionID": "music",
                    "viewedAt": 1_000,
                    "accountID": 1,
                },
                {
                    "type": "track",
                    "historyKey": "in-window",
                    "grandparentRatingKey": "artist-1",
                    "librarySectionID": "music",
                    "viewedAt": 1_500,
                    "accountID": 1,
                },
                {
                    "type": "track",
                    "historyKey": "other-section",
                    "grandparentRatingKey": "artist-1",
                    "librarySectionID": "other-music",
                    "viewedAt": 1_600,
                    "accountID": 1,
                },
            ],
        }})

        events = list(plex_history.iter_history(
            {"url": "http://plex:32400", "token": "token"},
            since=500,
            until=2_000,
            section_ids=["music"],
        ))

        self.assertEqual([event["history_key"] for event in events], [
            "in-window",
        ])

    @patch(
        "backend.services.plex_history.plex.cached_library_index",
        return_value={},
    )
    @patch("backend.services.plex_history.requests.get")
    def test_mixed_age_page_does_not_end_global_pagination_early(
        self, get, _cached_library_index
    ):
        get.side_effect = [
            Response(payload={"MediaContainer": {
                "totalSize": 3,
                "offset": 0,
                "size": 2,
                "Metadata": [
                    {
                        "type": "track",
                        "historyKey": "current-1",
                        "grandparentRatingKey": "artist-1",
                        "librarySectionID": "music",
                        "viewedAt": 1_500,
                        "accountID": 1,
                    },
                    {
                        "type": "track",
                        "historyKey": "old-outlier",
                        "grandparentRatingKey": "artist-1",
                        "librarySectionID": "music",
                        "viewedAt": 400,
                        "accountID": 1,
                    },
                ],
            }}),
            Response(payload={"MediaContainer": {
                "totalSize": 3,
                "offset": 2,
                "size": 1,
                "Metadata": [{
                    "type": "track",
                    "historyKey": "current-2",
                    "grandparentRatingKey": "artist-1",
                    "librarySectionID": "music",
                    "viewedAt": 1_000,
                    "accountID": 1,
                }],
            }}),
        ]

        events = list(plex_history.iter_history(
            {"url": "http://plex:32400", "token": "token"},
            since=500,
            until=2_000,
            section_ids=["music"],
            page_size=2,
        ))

        self.assertEqual(
            [event["history_key"] for event in events],
            ["current-1", "current-2"],
        )
        self.assertEqual(get.call_count, 2)

    def test_history_event_ignores_untyped_generic_children_as_accounts(self):
        event = plex_history._history_event({
            "type": "track",
            "historyKey": "history-1",
            "grandparentRatingKey": "artist-1",
            "viewedAt": 1_000,
            "_children": [{"id": 99, "title": "A media child"}],
        })

        self.assertIsNone(event)

    @patch("backend.services.plex_history.plex.cached_library_index")
    @patch("backend.services.plex_history.requests.get")
    def test_history_accepts_key_paths_legacy_children_and_missing_section(
        self, get, cached_library_index
    ):
        cached_library_index.return_value = {
            "artistsByRatingKey": {"artist-42": {"ratingKey": "artist-42"}},
            "releaseGroupsByRatingKey": {
                "album-84": {"ratingKey": "album-84"},
            },
        }
        get.return_value = Response(payload={
            "size": 1,
            "_children": [{
                "type": "track",
                "historyKey": "/status/sessions/history/200",
                "grandparentKey": "/library/metadata/artist-42",
                "parentKey": "/library/metadata/album-84",
                "viewedAt": 1_500,
                "_children": [{
                    "_elementType": "User",
                    "id": 7,
                    "title": "Listener",
                }],
            }],
        })
        diagnostics = {}

        events = list(plex_history.iter_history(
            {"url": "http://plex:32400", "token": "token"},
            since=500,
            until=2_000,
            section_ids=["music"],
            diagnostics=diagnostics,
        ))

        self.assertEqual(events, [{
            "history_key": "/status/sessions/history/200",
            "account_id": "7",
            "artist_rating_key": "artist-42",
            "album_rating_key": "album-84",
            "played_at": 1_500.0,
        }])
        self.assertEqual(diagnostics, {
            "pages": 1,
            "scanned": 1,
            "tracks": 1,
            "normalized": 1,
            "selected": 1,
            "sections": 1,
            "cachedArtists": 1,
            "cachedAlbums": 1,
        })


class PlexHistoryWorkerTests(DatabaseTestCase):
    @patch("backend.workers.plex_history.plex_history.iter_history")
    @patch("backend.workers.plex_history.plex_history.accounts")
    def test_sync_maps_exact_plex_id_when_server_alias_differs(
        self, accounts, iter_history
    ):
        self.register()
        with db() as connection:
            connection.execute(
                "UPDATE users SET plex_username = ?, plex_id = ? "
                "WHERE username = 'test-user'",
                ("bitemyear", "50651486"),
            )
            user_id = connection.execute(
                "SELECT id FROM users WHERE username = 'test-user'"
            ).fetchone()["id"]
        accounts.return_value = [{
            "account_id": "50651486",
            "aliases": ("Family Account",),
        }]
        iter_history.return_value = iter([{
            "history_key": "family-history-1",
            "account_id": "50651486",
            "artist_rating_key": "artist-1",
            "album_rating_key": "album-1",
            "played_at": 2_000.0,
        }])

        result = plex_history_worker.synchronize(
            {
                "url": "http://plex:32400",
                "token": "token",
                "machineIdentifier": "server-1",
                "librarySectionIds": ["music"],
            },
            full=True,
            now=3_000.0,
        )

        self.assertEqual(result["fetched"], 1)
        self.assertEqual(result["mapped"], 1)
        self.assertEqual(result["inserted"], 1)
        rows = get_plex_listens(user_id, 0, server_id="server-1")
        self.assertEqual(
            [row["history_key"] for row in rows],
            ["family-history-1"],
        )

    @patch("backend.workers.plex_history.plex_history.iter_history")
    @patch("backend.workers.plex_history.plex_history.accounts")
    def test_sync_maps_accounts_case_insensitively_and_stores_only_play_keys(
        self, accounts, iter_history
    ):
        self.register()
        with db() as connection:
            connection.execute(
                "UPDATE users SET plex_username = ?, plex_id = ? "
                "WHERE username = 'test-user'",
                ("JRamperSaud123", "global-plex-id"),
            )
            user_id = connection.execute(
                "SELECT id FROM users WHERE username = 'test-user'"
            ).fetchone()["id"]
        accounts.return_value = [{
            "account_id": "7",
            "aliases": ("jrampersaud123",),
        }]
        iter_history.return_value = iter([
            {
                "history_key": "history-1",
                "account_id": "7",
                "artist_rating_key": "artist-1",
                "album_rating_key": "album-1",
                "played_at": 2_000.0,
            },
            {
                "history_key": "history-unmapped",
                "account_id": "8",
                "artist_rating_key": "artist-2",
                "album_rating_key": "album-2",
                "played_at": 2_100.0,
            },
        ])

        result = plex_history_worker.synchronize(
            {
                "url": "http://plex:32400",
                "token": "token",
                "machineIdentifier": "server-1",
                "librarySectionIds": ["music"],
            },
            full=True,
            now=3_000.0,
        )

        self.assertEqual(result["fetched"], 2)
        self.assertEqual(result["mapped"], 1)
        self.assertEqual(result["inserted"], 1)
        rows = get_plex_listens(user_id, 0, server_id="server-1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(set(rows[0].keys()), {
            "server_id",
            "history_key",
            "user_id",
            "artist_rating_key",
            "album_rating_key",
            "played_at",
        })

    @patch("backend.workers.plex_history.plex_history.iter_history")
    @patch("backend.workers.plex_history.plex_history.accounts")
    def test_failed_pagination_does_not_advance_durable_history_cursor(
        self, accounts, iter_history
    ):
        self.register()
        with db() as connection:
            connection.execute(
                "UPDATE users SET plex_username = ?, plex_id = ? "
                "WHERE username = 'test-user'",
                ("listener", "7"),
            )
        accounts.return_value = [{
            "account_id": "7",
            "aliases": ("listener",),
        }]

        def failing_history():
            for history_id in range(500):
                yield {
                    "history_key": f"history-{history_id}",
                    "account_id": "7",
                    "artist_rating_key": "artist-1",
                    "album_rating_key": "album-1",
                    "played_at": 2_000.0 + history_id,
                }
            raise requests.ConnectionError("later page failed")

        iter_history.return_value = failing_history()

        with self.assertRaises(requests.ConnectionError):
            plex_history_worker.synchronize(
                {
                    "url": "http://plex:32400",
                    "token": "token",
                    "machineIdentifier": "server-1",
                    "librarySectionIds": ["music"],
                },
                full=True,
                now=3_000.0,
            )

        self.assertEqual(
            plex_listen_stats(server_id="server-1")["count"],
            0,
        )

    @patch("backend.workers.plex_history._linked_user_indexes")
    @patch("backend.workers.plex_history.plex_history.accounts")
    def test_account_map_rejects_conflicting_id_and_alias_matches(
        self, accounts, linked_user_indexes
    ):
        accounts.return_value = [{
            "account_id": "50651486",
            "aliases": ("Family Account",),
        }]
        linked_user_indexes.return_value = (
            {"50651486": {8}},
            {"family account": {9}},
        )

        with self.assertLogs(
            "backend.workers.plex_history", level="WARNING"
        ) as logs:
            result = plex_history_worker._account_user_map({
                "url": "http://plex:32400",
                "token": "token",
            })

        self.assertEqual(result, {})
        self.assertIn(
            "account ID and aliases match different linked users",
            "\n".join(logs.output),
        )


class PlexAuthenticationClientTests(unittest.TestCase):
    @patch("backend.services.plex_auth.requests.post")
    def test_pin_creation_builds_the_plex_authorization_url(self, post):
        post.return_value = Response(payload={
            "id": 42,
            "code": "ABCD",
            "expiresAt": "2026-07-28T18:00:00Z",
        })

        result = plex_auth.create_pin("client-id")

        self.assertEqual(result["id"], 42)
        self.assertIn("https://app.plex.tv/auth/#!?", result["authorizationUrl"])
        self.assertIn("clientID=client-id", result["authorizationUrl"])
        self.assertIn("code=ABCD", result["authorizationUrl"])
        post.assert_called_once()

    @patch("backend.services.plex_auth.requests.get")
    def test_authenticated_account_request_rejects_redirect(self, get):
        get.return_value = Response(
            301,
            headers={"Location": "https://other.test/account"},
        )

        with self.assertRaises(RedirectRejected):
            plex_auth.get_account("account-token", "client-id")

        get.assert_called_once()
        self.assertFalse(get.call_args.kwargs["allow_redirects"])
        self.assertEqual(
            get.call_args.kwargs["headers"]["X-Plex-Token"],
            "account-token",
        )

    @patch("backend.services.plex_auth.requests.get")
    def test_poll_pin_treats_a_null_token_as_pending(self, get):
        get.return_value = Response(payload={"id": 42, "authToken": None})

        token = plex_auth.poll_pin(42, "client-id")

        self.assertEqual(token, "")

    @patch("backend.services.plex_auth.requests.get")
    def test_resource_discovery_normalizes_owned_server_connections(self, get):
        get.return_value = Response(
            content=(
                b'<MediaContainer>'
                b'<Device name="Music Plex" product="Plex Media Server" '
                b'clientIdentifier="server-1" provides="server" owned="1" '
                b'accessToken="server-token">'
                b'<Connection uri="https://server-1.plex.direct:32400" '
                b'protocol="https" address="server-1.plex.direct" port="32400" '
                b'local="1"/>'
                b'<Connection uri="http://192.168.1.10:32400" protocol="http" '
                b'address="192.168.1.10" port="32400" local="1"/>'
                b'</Device>'
                b'</MediaContainer>'
            ),
            headers={"Content-Type": "application/xml"},
        )

        servers = plex_auth.get_resources("account-token", "client-id")

        self.assertEqual(servers[0]["clientIdentifier"], "server-1")
        self.assertTrue(servers[0]["owned"])
        self.assertTrue(servers[0]["connections"][0]["secure"])
        self.assertFalse(servers[0]["connections"][1]["secure"])
        self.assertEqual(
            servers[0]["connections"][1]["uri"],
            "http://192.168.1.10:32400",
        )
        self.assertEqual(servers[0]["accessToken"], "server-token")
        self.assertEqual(
            get.call_args.args[0],
            "https://plex.tv/api/resources",
        )
        self.assertEqual(get.call_args.kwargs["params"], {"includeHttps": "1"})


class PlexClientTests(unittest.TestCase):
    @patch("backend.services.plex.requests.get")
    def test_identity_parses_machine_identifier(self, get):
        get.return_value = Response(
            content=b'<MediaContainer machineIdentifier="server-1"/>'
        )
        result = plex.machine_identifier({"url": "http://plex:32400", "token": "token"})
        self.assertEqual(result, "server-1")

    @patch("backend.services.plex.requests.get")
    def test_identity_rejects_same_origin_redirect(self, get):
        get.return_value = Response(
            307,
            headers={"Location": "http://plex:32400/identity/"},
        )

        with self.assertRaises(RedirectRejected):
            plex.machine_identifier({
                "url": "http://plex:32400",
                "token": "server-token",
            })

        get.assert_called_once()
        self.assertFalse(get.call_args.kwargs["allow_redirects"])
        self.assertEqual(
            get.call_args.kwargs["headers"]["X-Plex-Token"],
            "server-token",
        )

    @patch("backend.services.plex.requests.get")
    def test_music_library_filters_sorts_and_builds_links(self, get):
        clear_cache("plex-library")
        clear_cache("plex-guid")
        get.side_effect = [
            Response(payload={
                "MediaContainer": {
                    "Directory": [
                        {"key": "movies", "type": "movie", "title": "Movies"},
                        {"key": "music", "type": "artist", "title": "Music"},
                    ]
                }
            }),
            Response(payload={
                "MediaContainer": {
                    "Metadata": [
                        {"title": "Zulu", "key": "/library/metadata/2", "thumb": "/z"},
                        {
                            "title": "alpha",
                            "titleSort": "The Alpha",
                            "key": "/library/metadata/1/children",
                            "thumb": "/a",
                            "guid": "plex://artist/artist-1",
                            "Genre": [
                                {"tag": "Alternative"},
                                {"tag": " alternative "},
                            ],
                            "Style": {"tag": "Indie Rock"},
                            "Mood": [{"tag": "Energetic"}],
                        },
                    ]
                }
            }),
            Response(payload={
                "MediaContainer": {
                    "Metadata": [{
                        "title": "An EP",
                        "parentTitle": "alpha",
                        "subtype": "ep",
                        "key": "/library/metadata/3",
                        "guid": "plex://album/album-3",
                        "Guid": [{
                            "id": "mbid://11111111-1111-1111-1111-111111111111"
                        }],
                        "Genre": [{"tag": "Rock"}],
                        "Style": [{"tag": "Garage Rock"}],
                        "Mood": [{"tag": "Rowdy"}],
                    }]
                }
            }),
        ]
        config = {
            "url": "http://plex:32400",
            "token": "token",
            "machineIdentifier": "server-1",
        }
        artists = plex.music_library(config)
        releases = plex.library_release_groups(config)
        self.assertEqual([artist["name"] for artist in artists], ["alpha", "Zulu"])
        self.assertEqual(artists[0]["sortName"], "The Alpha")
        self.assertEqual(artists[0]["genres"], ["Alternative"])
        self.assertEqual(artists[0]["styles"], ["Indie Rock"])
        self.assertEqual(artists[0]["moods"], ["Energetic"])
        self.assertIn("key=%2Flibrary%2Fmetadata%2F1", artists[0]["url"])
        self.assertNotIn("%2Fchildren", artists[0]["url"])
        self.assertEqual(
            artists[0]["plexampUrl"],
            "https://listen.plex.tv/artist/artist-1?"
            "source=server-1&key=%2Flibrary%2Fmetadata%2F1",
        )
        self.assertEqual([release["name"] for release in releases], ["An EP"])
        self.assertEqual(releases[0]["releaseType"], "ep")
        self.assertEqual(releases[0]["genres"], ["Rock"])
        self.assertEqual(releases[0]["styles"], ["Garage Rock"])
        self.assertEqual(releases[0]["moods"], ["Rowdy"])
        self.assertEqual(
            releases[0]["plexampUrl"],
            "https://listen.plex.tv/album/album-3?"
            "source=server-1&key=%2Flibrary%2Fmetadata%2F3",
        )
        self.assertEqual(
            releases[0]["musicbrainzReleaseId"],
            "11111111-1111-1111-1111-111111111111",
        )
        plex.apply_release_group_mappings(config, {
            "11111111-1111-1111-1111-111111111111":
                "22222222-2222-2222-2222-222222222222",
        })
        enriched = plex.library_release_groups(config)[0]
        self.assertTrue(enriched["releaseGroupResolved"])
        self.assertEqual(
            enriched["musicbrainzReleaseGroupId"],
            "22222222-2222-2222-2222-222222222222",
        )
        self.assertEqual(get.call_count, 3)
        self.assertEqual(get.call_args_list[1].kwargs["params"]["type"], 8)
        self.assertEqual(get.call_args_list[2].kwargs["params"]["type"], 9)
        self.assertEqual(get.call_args_list[2].kwargs["params"]["includeGuids"], 1)

        with cache_db() as connection:
            guid_rows = connection.execute(
                "SELECT COUNT(*) AS count FROM api_cache "
                "WHERE cache_key LIKE 'plex-guid:%'"
            ).fetchone()["count"]
        self.assertEqual(guid_rows, 2)

    @patch("backend.services.plex.requests.get")
    def test_recent_scan_rejects_albums_returned_for_the_artist_query(self, get):
        get.side_effect = [
            Response(payload={"MediaContainer": {"Metadata": [{
                "type": "album",
                "title": "Album incorrectly returned as an artist",
                "ratingKey": "20",
            }]}}),
            Response(payload={"MediaContainer": {"Metadata": [{
                "type": "album",
                "title": "A New Album",
                "parentTitle": "A New Artist",
                "parentRatingKey": "10",
                "parentKey": "/library/metadata/10/children",
                "ratingKey": "20",
            }]}}),
            Response(payload={"MediaContainer": {"Metadata": [{
                "type": "artist",
                "title": "A New Artist",
                "ratingKey": "10",
                "key": "/library/metadata/10/children",
            }]}}),
        ]

        result = plex._scan_sections(
            {
                "url": "http://plex:32400",
                "token": "token",
                "machineIdentifier": "server-1",
            },
            [{"id": "music", "title": "Music"}],
            recently_added=True,
        )

        self.assertEqual(
            [artist["name"] for artist in result["artists"]],
            ["A New Artist"],
        )
        self.assertEqual(
            [release["name"] for release in result["releaseGroups"]],
            ["A New Album"],
        )

    @patch("backend.services.plex.requests.get")
    def test_recent_album_hydrates_parent_artist_missing_from_recent_feed(self, get):
        get.side_effect = [
            Response(payload={"MediaContainer": {"Metadata": []}}),
            Response(payload={"MediaContainer": {"Metadata": [{
                "title": "A New Album",
                "parentTitle": "A New Artist",
                "parentRatingKey": "10",
                "parentKey": "/library/metadata/10/children",
                "parentGuid": "plex://artist/new-artist",
                "ratingKey": "20",
                "guid": "plex://album/new-album",
                "Guid": [{
                    "id": "mbid://11111111-1111-1111-1111-111111111111",
                }],
            }]}}),
            Response(payload={"MediaContainer": {"Metadata": [{
                "title": "A New Artist",
                "ratingKey": "10",
                "key": "/library/metadata/10/children",
                "guid": "plex://artist/new-artist",
            }]}}),
        ]

        result = plex._scan_sections(
            {
                "url": "http://plex:32400",
                "token": "token",
                "machineIdentifier": "server-1",
            },
            [{"id": "music", "title": "Music"}],
            recently_added=True,
        )

        self.assertEqual([artist["name"] for artist in result["artists"]], [
            "A New Artist",
        ])
        self.assertEqual(result["artists"][0]["ratingKey"], "10")
        self.assertEqual(
            result["releaseGroups"][0]["artistRatingKey"],
            "10",
        )
        self.assertIn("/library/metadata/10", get.call_args_list[2].args[0])
        self.assertNotIn("/children", get.call_args_list[2].args[0])

    @patch("backend.services.plex.set_cache_document")
    @patch("backend.services.plex.upsert_cache_documents")
    @patch("backend.services.plex.get_cache_document")
    def test_release_mapping_also_matches_its_unmatched_parent_artist(
        self, get_document, upsert_documents, set_document
    ):
        payload = {
            "artists": [{
                "name": "A New Artist",
                "ratingKey": "10",
                "plexGuid": "plex://artist/new-artist",
                "guids": ["plex://artist/new-artist"],
                "musicbrainzId": "",
            }],
            "releaseGroups": [{
                "name": "A New Album",
                "artistName": "A New Artist",
                "artistRatingKey": "10",
                "ratingKey": "20",
                "musicbrainzReleaseId": "release-1",
            }],
        }
        get_document.return_value = payload

        changed = plex.apply_release_group_mappings(
            {"url": "http://plex", "machineIdentifier": "server-1"},
            {"release-1": "release-group-1"},
            artist_mappings={"release-1": "artist-1"},
        )

        self.assertEqual(changed, 1)
        self.assertEqual(payload["artists"][0]["musicbrainzId"], "artist-1")
        self.assertEqual(
            payload["releaseGroups"][0]["musicbrainzReleaseGroupId"],
            "release-group-1",
        )
        saved_guid_inventory = upsert_documents.call_args.args[1]
        self.assertEqual(
            saved_guid_inventory["server-1:artist:10"]["musicbrainzId"],
            "artist-1",
        )
        set_document.assert_called_once()

    def test_cached_plex_urls_are_repaired_without_rescanning(self):
        payload = plex._normalize_snapshot_urls(
            {"url": "http://plex", "machineIdentifier": "server-1"},
            {
                "artists": [{
                    "key": "/library/metadata/65537/children",
                    "ratingKey": "65537",
                    "thumb": "/library/metadata/65537/thumb/100",
                    "url": "https://app.plex.tv/old-link",
                    "artwork": "/api/artwork/plex-artist/65537",
                    "plexGuid": "",
                    "guids": ["plex://artist/artist-65537"],
                }],
                "releaseGroups": [],
            },
        )

        url = payload["artists"][0]["url"]
        self.assertIn("key=%2Flibrary%2Fmetadata%2F65537", url)
        self.assertNotIn("%2Fchildren", url)
        self.assertEqual(
            payload["artists"][0]["plexampUrl"],
            "https://listen.plex.tv/artist/artist-65537?"
            "source=server-1&key=%2Flibrary%2Fmetadata%2F65537",
        )
        artwork = payload["artists"][0]["artwork"]
        self.assertRegex(
            artwork,
            r"^/api/artwork/plex-artist/65537\?v=[0-9a-f]{16}$",
        )
        changed = plex._normalize_snapshot_urls(
            {"url": "http://plex", "machineIdentifier": "server-1"},
            {
                "artists": [{
                    "ratingKey": "65537",
                    "thumb": "/library/metadata/65537/thumb/200",
                }],
                "releaseGroups": [],
            },
        )
        self.assertNotEqual(artwork, changed["artists"][0]["artwork"])

    @patch("backend.services.plex.requests.get")
    def test_full_scan_only_reads_selected_music_sections(self, get):
        clear_cache("plex-library")
        clear_cache("plex-guid")
        get.side_effect = [
            Response(payload={
                "MediaContainer": {
                    "Directory": [
                        {"key": "1", "type": "artist", "title": "Music"},
                        {"key": "2", "type": "artist", "title": "Other Music"},
                    ]
                }
            }),
            Response(payload={
                "MediaContainer": {
                    "Metadata": [
                        {"title": "Selected Artist", "key": "/library/metadata/1"},
                    ]
                }
            }),
            Response(payload={"MediaContainer": {"Metadata": []}}),
        ]

        result = plex.full_library_scan({
            "url": "http://plex:32400",
            "token": "token",
            "machineIdentifier": "server-2",
            "librarySectionIds": ["2"],
        })

        self.assertEqual(
            [artist["name"] for artist in result["artists"]],
            ["Selected Artist"],
        )
        self.assertIn("/library/sections/2/all", get.call_args_list[1].args[0])
        self.assertIn("/library/sections/2/all", get.call_args_list[2].args[0])
        self.assertEqual(get.call_args_list[1].kwargs["params"]["type"], 8)
        self.assertEqual(get.call_args_list[2].kwargs["params"]["type"], 9)
        self.assertEqual(get.call_count, 3)

    def test_unchanged_recent_scan_skips_snapshot_and_guid_writes(self):
        artist = {
            "name": "Cached Artist",
            "ratingKey": "1",
            "musicbrainzId": "artist-1",
        }
        release = {
            "name": "Cached Album",
            "ratingKey": "2",
            "musicbrainzReleaseId": "release-1",
            "releaseGroupResolved": False,
        }
        cached = {
            "snapshotVersion": plex.SNAPSHOT_VERSION,
            "artists": [artist],
            "releaseGroups": [release],
            "sectionIds": ["music"],
            "scannedAt": 1,
        }
        with (
            patch("backend.services.plex.get_cache_document", return_value=cached),
            patch(
                "backend.services.plex.selected_music_sections",
                return_value=[{"id": "music", "title": "Music"}],
            ),
            patch(
                "backend.services.plex._scan_sections",
                return_value={"artists": [dict(artist)], "releaseGroups": [dict(release)]},
            ),
            patch("backend.services.plex._save_snapshot") as save_snapshot,
        ):
            result = plex.recently_added_scan({
                "url": "http://plex",
                "machineIdentifier": "server-1",
            })

        self.assertFalse(result["changed"])
        self.assertEqual(result["artistMbids"], [])
        self.assertEqual(result["releaseMbids"], ["release-1"])
        save_snapshot.assert_not_called()

    def test_recent_scan_writes_only_changed_guid_documents(self):
        cached_artist = {
            "name": "Cached Artist",
            "ratingKey": "1",
            "musicbrainzId": "artist-1",
            "thumb": "/old",
        }
        updated_artist = {**cached_artist, "thumb": "/new"}
        cached = {
            "snapshotVersion": plex.SNAPSHOT_VERSION,
            "artists": [cached_artist],
            "releaseGroups": [],
            "sectionIds": ["music"],
            "scannedAt": 1,
        }
        with (
            patch("backend.services.plex.get_cache_document", return_value=cached),
            patch(
                "backend.services.plex.selected_music_sections",
                return_value=[{"id": "music", "title": "Music"}],
            ),
            patch(
                "backend.services.plex._scan_sections",
                return_value={"artists": [updated_artist], "releaseGroups": []},
            ),
            patch("backend.services.plex._save_snapshot") as save_snapshot,
        ):
            result = plex.recently_added_scan({
                "url": "http://plex",
                "machineIdentifier": "server-1",
            })

        self.assertTrue(result["changed"])
        self.assertEqual(result["artistMbids"], ["artist-1"])
        self.assertEqual(result["releaseMbids"], [])
        self.assertEqual(
            save_snapshot.call_args.kwargs["guid_inventory"],
            {"artists": [updated_artist], "releaseGroups": []},
        )

    def test_unchanged_full_scan_returns_no_artist_enrichment_targets(self):
        artist = {
            "name": "Cached Artist",
            "ratingKey": "1",
            "musicbrainzId": "artist-1",
        }
        cached = {
            "snapshotVersion": plex.SNAPSHOT_VERSION,
            "artists": [artist],
            "releaseGroups": [],
            "sectionIds": ["music"],
            "scannedAt": 1,
        }
        with (
            patch("backend.services.plex.get_cache_document", return_value=cached),
            patch(
                "backend.services.plex.selected_music_sections",
                return_value=[{"id": "music", "title": "Music"}],
            ),
            patch(
                "backend.services.plex._scan_sections",
                return_value={"artists": [dict(artist)], "releaseGroups": []},
            ),
            patch("backend.services.plex._save_snapshot") as save_snapshot,
        ):
            result = plex.full_library_scan({
                "url": "http://plex",
                "machineIdentifier": "server-1",
            })

        self.assertFalse(result["changed"])
        self.assertEqual(result["artistMbids"], [])
        self.assertEqual(result["releaseMbids"], [])
        save_snapshot.assert_called_once()


if __name__ == "__main__":
    unittest.main()
