"""High-value backend regression tests using Flask's built-in test client."""

import gzip
import io
import json
import os
import runpy
import sqlite3
import tempfile
import time
import unittest
from urllib.parse import parse_qs, urlparse
from unittest.mock import Mock, patch


# Application paths are resolved when backend.config is first imported. Keep
# every test artifact outside the repository and disable daemon workers before
# importing the application package.
TEST_DATA = tempfile.TemporaryDirectory(prefix="melodarr-tests-")
os.environ.update({
    "MELODARR_DATABASE": os.path.join(TEST_DATA.name, "melodarr.db"),
    "MELODARR_CACHE_DATABASE": os.path.join(TEST_DATA.name, "cache", "metadata.db"),
    "MELODARR_SETTINGS": os.path.join(TEST_DATA.name, "settings.json"),
    "MELODARR_SECRET_KEY_FILE": os.path.join(TEST_DATA.name, "session-secret.key"),
    "MELODARR_ARTWORK_CACHE": os.path.join(TEST_DATA.name, "artwork"),
})

import requests
from PIL import Image

from backend import api_cache
from backend import artwork_cache
from backend import recommendations as recommendation_engine
from backend.api_cache import (
    cache_db,
    cache_key,
    cached_json_get,
    clear_cache,
    get_cache_document,
    migrate_legacy_cache,
    upsert_cache_documents,
)
from backend.application import create_app
from backend.config import ARTWORK_CACHE_DIRECTORY
from backend.services import lidarr, musicbrainz, plex
from backend.storage import db, enqueue_lidarr_search, get_service, set_lidarr_refresh_command
from backend import worker
from backend.workers import lidarr_searches as lidarr_search_worker
from backend.workers import lidarr_library as lidarr_library_worker
from backend.workers import plex as plex_worker
from backend.workers import plex_metadata as plex_metadata_worker
from backend.workers import recommendations as recommendation_worker


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


class DatabaseTestCase(unittest.TestCase):
    """Create an isolated app and reset mutable database state per test."""

    def setUp(self):
        self.app = create_app({"TESTING": True, "SECRET_KEY": "test-secret"})
        self.client = self.app.test_client()
        with db() as connection:
            connection.execute("DELETE FROM pending_lidarr_searches")
            connection.execute("DELETE FROM recommendation_cache")
            connection.execute("DELETE FROM request_history")
            connection.execute("DELETE FROM account_invitations")
            connection.execute("DELETE FROM users")
        with cache_db() as connection:
            connection.execute("DELETE FROM api_cache")

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
        self.assertEqual(len(rules), 52)
        self.assertEqual(len(route_methods), 52)

    def test_factory_applies_test_configuration(self):
        self.assertTrue(self.app.config["TESTING"])
        self.assertEqual(self.app.config["SECRET_KEY"], "test-secret")


class WorkerEntrypointTests(unittest.TestCase):
    def test_refresh_request_wakes_sleeping_worker(self):
        recommendation_worker.refresh_requested.clear()
        recommendation_worker.request_refresh()
        self.assertTrue(recommendation_worker.refresh_requested.is_set())
        recommendation_worker.refresh_requested.clear()

    @patch("backend.worker.Thread")
    @patch("backend.worker.recommendation_worker.run")
    @patch("backend.worker.init_db")
    def test_worker_initializes_storage_and_background_loops(self, init_db, run, thread_class):
        calls = []
        lidarr_thread = Mock()
        plex_thread = Mock()
        plex_metadata_thread = Mock()
        lidarr_library_thread = Mock()
        thread_class.side_effect = [
            lidarr_thread, lidarr_library_thread, plex_thread, plex_metadata_thread
        ]
        init_db.side_effect = lambda: calls.append("database")
        run.side_effect = lambda: calls.append("recommendations")
        worker.main()
        self.assertEqual(calls, ["database", "recommendations"])
        self.assertEqual(thread_class.call_count, 4)
        thread_class.assert_any_call(
            target=lidarr_search_worker.run, name="lidarr-search-followups", daemon=True
        )
        thread_class.assert_any_call(
            target=lidarr_library_worker.run, name="lidarr-library-scan", daemon=True
        )
        thread_class.assert_any_call(
            target=plex_worker.run, name="plex-library-scans", daemon=True
        )
        thread_class.assert_any_call(
            target=plex_metadata_worker.run,
            name="plex-musicbrainz-enrichment",
            daemon=True,
        )
        lidarr_thread.start.assert_called_once_with()
        lidarr_library_thread.start.assert_called_once_with()
        plex_thread.start.assert_called_once_with()
        plex_metadata_thread.start.assert_called_once_with()

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
    @patch("backend.workers.plex_metadata.plex.apply_release_group_mappings")
    @patch("backend.workers.plex_metadata.plex.unresolved_musicbrainz_releases")
    @patch("backend.workers.plex_metadata.musicbrainz.get")
    def test_release_ids_are_resolved_to_release_groups_in_background(
        self, musicbrainz_get, unresolved, apply_mappings
    ):
        unresolved.return_value = [{"musicbrainzReleaseId": "release-1"}]
        musicbrainz_get.return_value = {
            "release-group": {"id": "release-group-1"}
        }

        plex_metadata_worker._resolve_release_groups({"url": "http://plex"})

        musicbrainz_get.assert_called_once_with(
            "/release/release-1",
            "release-groups",
            priority="background",
        )
        apply_mappings.assert_called_once_with(
            {"url": "http://plex"},
            {"release-1": "release-group-1"},
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

    @patch("backend.workers.lidarr_searches.set_lidarr_search_command")
    @patch("backend.workers.lidarr_searches.lidarr.start_command")
    @patch("backend.workers.lidarr_searches.lidarr.command")
    def test_completed_refresh_queues_album_search(
        self, command, start_command, set_search
    ):
        command.return_value = Response(200, {"status": "completed"})
        start_command.return_value = Response(201, {"id": 66})
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
        start_command.assert_called_once_with({
            "name": "AlbumSearch",
            "albumIds": [33],
        })
        set_search.assert_called_once_with(1, 66)

    @patch("backend.workers.lidarr_searches.set_lidarr_search_command")
    @patch("backend.workers.lidarr_searches.lidarr.start_command")
    @patch("backend.workers.lidarr_searches.lidarr.command")
    def test_shared_refresh_is_polled_once_then_searches_every_album(
        self, command, start_command, set_search
    ):
        command.return_value = Response(200, {"status": "completed"})
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


class DeploymentConfigTests(unittest.TestCase):
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
        self.assertNotIn("<summary>Create an account</summary>", frontend)
        self.assertIn('name="remember"', frontend)
        self.assertIn('data-account-route="invitations"', frontend)
        self.assertIn("status.firstAccount", typescript)
        self.assertIn("status.invitationValid", typescript)

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
        self.assertIn('<img src="/icons/melodarr.svg" alt="">', frontend)
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
        self.assertIn('showAccountPage?.("profile")', typescript)
        # The header and the mobile tab bar both carry a button per view, and
        # detail/account views have none, so this must not use the strict
        # single-element helper that throws when a selector matches nothing.
        self.assertIn(
            'document.querySelectorAll<HTMLElement>(`[data-view="${view}"]`)',
            typescript,
        )
        self.assertNotIn('$(`[data-view=${view}]`)', typescript)

    def test_gunicorn_runs_one_process_with_threaded_concurrency(self):
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        config_path = os.path.join(project_root, "backend", "gunicorn.conf.py")
        config = runpy.run_path(config_path)
        self.assertEqual(config["workers"], 1)
        self.assertEqual(config["worker_class"], "gthread")
        self.assertEqual(config["threads"], 16)
        self.assertFalse(config["preload_app"])
        self.assertTrue(config["control_socket_disable"])

        with open(os.path.join(project_root, "Dockerfile"), encoding="utf-8") as file:
            dockerfile = file.read()
        self.assertIn('"gunicorn"', dockerfile)
        self.assertIn('"--config=backend/gunicorn.conf.py"', dockerfile)

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

    def test_csrf_protects_authenticated_writes(self):
        token = self.register()
        rejected = self.client.post("/api/auth/logout")
        self.assertEqual(rejected.status_code, 403)
        accepted = self.client.post(
            "/api/auth/logout",
            headers={"X-CSRF-Token": token},
        )
        self.assertEqual(accepted.status_code, 200)


class SettingsMaintenanceTests(DatabaseTestCase):
    @patch("backend.routes.settings.lidarr_library_worker.request_scan")
    @patch("backend.routes.settings.plex_metadata_worker.request_enrichment")
    @patch("backend.routes.settings.plex_worker.request_full_scan")
    @patch("backend.routes.settings.plex_worker.request_recent_scan")
    @patch("backend.routes.settings.lidarr_search_worker.request_work")
    @patch("backend.routes.settings.recommendation_worker.request_refresh")
    def test_jobs_are_listed_and_can_be_manually_queued(
        self, request_refresh, request_work, request_recent, request_full,
        request_enrichment, request_lidarr_scan,
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
            ],
        )

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
        self.assertEqual(recommendation.status_code, 200)
        self.assertEqual(lidarr.status_code, 200)
        self.assertEqual(lidarr_library.status_code, 200)
        self.assertEqual(recent.status_code, 200)
        self.assertEqual(full.status_code, 200)
        self.assertEqual(enrichment.status_code, 200)
        request_refresh.assert_called_once_with()
        request_work.assert_called_once_with()
        request_lidarr_scan.assert_called_once_with()
        request_recent.assert_called_once_with()
        request_full.assert_called_once_with()
        request_enrichment.assert_called_once_with()

    @patch("backend.routes.settings.plex_worker.request_full_scan")
    @patch("backend.routes.settings.plex.music_sections")
    @patch("backend.routes.settings.plex.machine_identifier")
    def test_plex_settings_save_only_selected_music_libraries(
        self, machine_identifier, music_sections, request_full_scan
    ):
        machine_identifier.return_value = "server-1"
        music_sections.return_value = [
            {"id": "1", "title": "Main Music"},
            {"id": "2", "title": "Audiobooks"},
        ]
        token = self.register()
        response = self.client.post(
            "/api/settings/plex",
            json={
                "url": "http://plex:32400",
                "token": "plex-token",
                "librarySectionIds": ["1"],
            },
            headers={"X-CSRF-Token": token},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(get_service("plex")["librarySectionIds"], ["1"])
        request_full_scan.assert_called_once_with()

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
        listen_count.side_effect = requests.Timeout("upstream timeout")
        with self.assertLogs(level="WARNING") as logs:
            response = self.link(self.register())
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["validationDeferred"])
        self.assertEqual(self.saved_username(), "bitemyear")
        self.assertIn("validation deferred", logs.output[0])


class ApiCacheTests(DatabaseTestCase):
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

    @patch("backend.routes.requests.lidarr_search_worker.request_work")
    @patch("backend.routes.requests.enqueue_lidarr_search")
    @patch("backend.routes.requests.lidarr.start_command")
    @patch("backend.routes.requests.lidarr.add_album")
    @patch("backend.routes.requests.lidarr.lookup_album")
    @patch("backend.routes.requests.get_service")
    def test_new_album_persists_refresh_then_search_job(
        self, get_service, lookup_album, add_album, start_command,
        enqueue_search, request_work
    ):
        get_service.return_value = self.lidarr_config()
        lookup_album.return_value = Response(payload=[{
            "title": "Test Album",
            "foreignAlbumId": self.album_mbid,
            "artist": {"artistName": "Test Artist"},
        }])
        add_album.return_value = Response(201, {
            "id": 33,
            "artistId": 44,
            "title": "Test Album",
        })
        enqueue_search.return_value = True

        response = self.client.post(
            "/api/request/release-group",
            json={"mbid": self.album_mbid},
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

    @patch("backend.services.lidarr.requests.request")
    def test_artist_lookup_uses_authenticated_v1_endpoint(self, request):
        response = Mock()
        request.return_value = response
        result = lidarr.lookup_artist(
            "artist-id",
            {"url": "http://lidarr:8686", "apiKey": "key"},
        )
        self.assertIs(result, response)
        request.assert_called_once_with(
            "GET",
            "http://lidarr:8686/api/v1/artist/lookup",
            headers={"X-Api-Key": "key"},
            timeout=15,
            params={"term": "mbid:artist-id"},
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
        musicbrainz._wait_for_request_slot()
        musicbrainz._wait_for_request_slot()
        sleep.assert_called_once_with(0.9000000000000004)
        self.assertAlmostEqual(musicbrainz._next_request_at, 12.2)

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
        wait_for_slot.assert_called_once_with("background")

        wait_for_slot.reset_mock()
        musicbrainz.get("/release-group/group", "", priority="prefetch")
        cached_get.call_args.kwargs["before_request"]()
        wait_for_slot.assert_called_once_with("prefetch")

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
        self.assertEqual(kwargs["params"]["limit"], 25)

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


class DiscoveryRoutesTests(DatabaseTestCase):
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


class MusicRoutesTests(DatabaseTestCase):
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

        response = self.client.get(
            "/api/music/release-group/group-id",
            headers={"X-CSRF-Token": self.register()},
        )

        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["provisional"])
        self.assertEqual(payload["artistId"], "artist-id")
        self.assertEqual(payload["releases"][0]["id"], "release-id")
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

    @patch("backend.routes.music.musicbrainz.get")
    def test_refresh_artist_force_fetches_complete_discography(self, get):
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
        self.assertTrue(all(call.kwargs["force_refresh"] for call in get.call_args_list))
        self.assertTrue(all(call.kwargs["priority"] == "critical" for call in get.call_args_list))


class RecommendationAssemblyTests(unittest.TestCase):
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
        self.assertEqual(albums, [])

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
        library_albums.return_value = [{"foreignAlbumId": "lidarr-album"}]
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
        })
        self.assertIn("ListenBrainz recommendations unavailable", logs.output[0])

    @patch("backend.recommendations.save_recommendation_cache")
    @patch("backend.recommendations.build_recommendation_cache")
    @patch("backend.recommendations.recommendation_users")
    def test_refresh_continues_when_one_user_fails(
        self, recommendation_users, build_cache, save_cache
    ):
        recommendation_users.return_value = [
            {"id": 1, "username": "offline"},
            {"id": 2, "username": "working"},
        ]
        build_cache.side_effect = [requests.Timeout("timeout"), {"artists": []}]
        with self.assertLogs("backend.recommendations", level="WARNING") as logs:
            retry_required = recommendation_engine.refresh_recommendation_cache()
        save_cache.assert_called_once_with(2, {"artists": []})
        self.assertTrue(retry_required)
        self.assertIn("offline", logs.output[0])

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
    def artwork_url(self, mbid):
        return f"/api/artwork/release-group/{mbid}"

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
        os.makedirs(ARTWORK_CACHE_DIRECTORY, exist_ok=True)
        kept = artwork_cache.plex_artist_artwork_key("server-1", "100")
        removed = artwork_cache.plex_artist_artwork_key("server-1", "200")
        names = [
            f"{kept}.jpg",
            f"{kept}@thumb.webp",
            f"{kept}@card.webp",
            f"{removed}.jpg",
            f"{removed}@thumb.webp",
        ]
        for name in names:
            with open(os.path.join(ARTWORK_CACHE_DIRECTORY, name), "wb") as file:
                file.write(b"image")

        deleted = artwork_cache.remove_stale_plex_artist_artwork({kept})

        self.assertEqual(deleted, 2)
        for name in names[:3]:
            self.assertTrue(
                os.path.isfile(os.path.join(ARTWORK_CACHE_DIRECTORY, name)), name
            )
        for name in names[3:]:
            self.assertFalse(
                os.path.exists(os.path.join(ARTWORK_CACHE_DIRECTORY, name)), name
            )


class CompressionTests(DatabaseTestCase):
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
            {"name", "section", "musicbrainzId", "artwork", "url"},
        )
        self.assertTrue(response.headers.get("ETag"))

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


class PlexClientTests(unittest.TestCase):
    @patch("backend.services.plex.requests.get")
    def test_identity_parses_machine_identifier(self, get):
        get.return_value = Response(
            content=b'<MediaContainer machineIdentifier="server-1"/>'
        )
        result = plex.machine_identifier({"url": "http://plex:32400", "token": "token"})
        self.assertEqual(result, "server-1")

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
                            "key": "/library/metadata/1/children",
                            "thumb": "/a",
                            "guid": "plex://artist/artist-1",
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
        self.assertIn("key=%2Flibrary%2Fmetadata%2F1", artists[0]["url"])
        self.assertNotIn("%2Fchildren", artists[0]["url"])
        self.assertEqual(
            artists[0]["plexampUrl"],
            "https://listen.plex.tv/artist/artist-1?"
            "source=server-1&key=%2Flibrary%2Fmetadata%2F1",
        )
        self.assertEqual([release["name"] for release in releases], ["An EP"])
        self.assertEqual(releases[0]["releaseType"], "ep")
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

    def test_cached_plex_urls_are_repaired_without_rescanning(self):
        payload = plex._normalize_snapshot_urls(
            {"url": "http://plex", "machineIdentifier": "server-1"},
            {
                "artists": [{
                    "key": "/library/metadata/65537/children",
                    "url": "https://app.plex.tv/old-link",
                }],
                "releaseGroups": [],
            },
        )

        url = payload["artists"][0]["url"]
        self.assertIn("key=%2Flibrary%2Fmetadata%2F65537", url)
        self.assertNotIn("%2Fchildren", url)

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

        artists = plex.full_library_scan({
            "url": "http://plex:32400",
            "token": "token",
            "machineIdentifier": "server-2",
            "librarySectionIds": ["2"],
        })

        self.assertEqual([artist["name"] for artist in artists], ["Selected Artist"])
        self.assertIn("/library/sections/2/all", get.call_args_list[1].args[0])
        self.assertIn("/library/sections/2/all", get.call_args_list[2].args[0])
        self.assertEqual(get.call_args_list[1].kwargs["params"]["type"], 8)
        self.assertEqual(get.call_args_list[2].kwargs["params"]["type"], 9)
        self.assertEqual(get.call_count, 3)


if __name__ == "__main__":
    unittest.main()
