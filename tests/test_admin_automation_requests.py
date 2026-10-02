"""Admin history includes machine-origin audits without inventing an account."""

from ._test_environment import TEST_ROOT  # noqa: F401
from .test_backend import DatabaseTestCase

from unittest.mock import patch

from backend import storage


class AdminAutomationRequestTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.csrf = self.register()
        with storage.db() as connection:
            self.user_id = connection.execute("SELECT id FROM users").fetchone()[0]
        self.plex = self.enterContext(patch(
            "backend.routes.admin._profile_plex_index",
            return_value={"artistsByMbid": {}, "releaseGroupsByMbid": {}},
        ))
        self.enterContext(patch("backend.routes.account._profile_plex_index", return_value={}))
        self.enterContext(patch("backend.routes.account._cached_release_group_metadata", return_value={}))
        self.library = self.enterContext(patch(
            "backend.routes.account.lidarr.cached_library_availability", return_value={},
        ))
        self.downloads = self.enterContext(patch(
            "backend.routes.account.lidarr.cached_download_availability", return_value={},
        ))
        self.enterContext(patch(
            "requests.sessions.Session.request", side_effect=AssertionError("Live provider call"),
        ))

    def automation_history(self, mbid="automation-group", *, created_at=300):
        storage.record_request(
            None, "release-group", mbid, "Automation Album",
            artist_name="Machine Artist", release_type="Album", release_date="2026-10-02",
        )
        with storage.db() as connection:
            connection.execute(
                "UPDATE automation_request_history SET created_at = ? WHERE mbid = ?",
                (created_at, mbid),
            )
            return connection.execute(
                "SELECT id FROM automation_request_history WHERE mbid = ?", (mbid,),
            ).fetchone()[0]

    def user_history(self, mbid="user-group", *, created_at=100):
        with storage.db() as connection:
            cursor = connection.execute(
                "INSERT INTO request_history "
                "(user_id, kind, mbid, name, artist_name, release_type, release_date, created_at) "
                "VALUES (?, 'release-group', ?, 'User Album', 'User Artist', 'Album', '2026', ?)",
                (self.user_id, mbid, created_at),
            )
            return cursor.lastrowid

    def test_existing_automation_audit_is_visible_without_user_or_new_writes(self):
        audit_id = self.automation_history()
        with storage.db() as connection:
            before = dict(connection.execute("SELECT * FROM automation_request_history").fetchone())

        response = self.client.get("/api/admin/requests")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["pagination"]["total"], 1)
        item = payload["requests"][0]
        self.assertEqual(item["id"], f"automation:{audit_id}")
        self.assertEqual(item["source"], "automation")
        self.assertEqual(item["requester"], {
            "id": None, "username": "Automation API", "userType": "automation",
        })
        self.assertEqual(item["artist_name"], "Machine Artist")
        self.assertEqual(item["release_type"], "Album")
        self.assertEqual(item["created_at"], 300)
        with storage.db() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM request_history").fetchone()[0], 0)
            self.assertEqual(dict(connection.execute("SELECT * FROM automation_request_history").fetchone()), before)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM recording_acquisitions").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM pending_lidarr_searches").fetchone()[0], 0)

    def test_mixed_origins_keep_unique_ids_and_stable_ties(self):
        with storage.db() as connection:
            connection.execute(
                "INSERT INTO request_history (id, user_id, kind, mbid, name, created_at) "
                "VALUES (10000, ?, 'artist', 'same-artist', 'Same Artist', 100)",
                (self.user_id,),
            )
            connection.execute(
                "INSERT INTO automation_request_history "
                "(id, source, kind, mbid, name, created_at) "
                "VALUES (10000, 'automation', 'artist', 'same-artist', 'Same Artist', 100)",
            )
        first = self.client.get("/api/admin/requests").get_json()
        second = self.client.get("/api/admin/requests").get_json()
        self.assertEqual(first, second)
        self.assertEqual(first["pagination"]["total"], 2)
        self.assertEqual([item["id"] for item in first["requests"]], [10000, "automation:10000"])
        self.assertEqual([item["source"] for item in first["requests"]], ["user", "automation"])
        self.assertEqual(first["requests"][0]["requester"]["id"], self.user_id)
        self.assertIsNone(first["requests"][1]["requester"]["id"])

    def test_combined_pagination_orders_both_sources_before_slicing(self):
        with storage.db() as connection:
            connection.executemany(
                "INSERT INTO request_history (user_id, kind, mbid, name, created_at) "
                "VALUES (?, 'artist', ?, ?, ?)",
                [(self.user_id, f"user-{i}", f"User {i}", i * 2) for i in range(100)],
            )
            connection.executemany(
                "INSERT INTO automation_request_history (source, kind, mbid, name, created_at) "
                "VALUES ('automation', 'artist', ?, ?, ?)",
                [(f"automation-{i}", f"Automation {i}", i * 2 + 1) for i in range(101)],
            )
        pages = [self.client.get(f"/api/admin/requests?page={page}").get_json() for page in (1, 2, 3)]
        self.assertEqual([len(page["requests"]) for page in pages], [100, 100, 1])
        self.assertTrue(all(page["pagination"]["total"] == 201 for page in pages))
        self.assertTrue(all(page["pagination"]["totalPages"] == 3 for page in pages))
        rows = [item for page in pages for item in page["requests"]]
        self.assertEqual([item["created_at"] for item in rows], sorted([i * 2 for i in range(100)] + [i * 2 + 1 for i in range(101)], reverse=True))
        self.assertEqual(len({item["id"] for item in rows}), 201)
        self.assertEqual(sum(item["source"] == "automation" for item in rows), 101)
        self.assertEqual(self.client.get("/api/admin/requests?page=4").get_json()["requests"], [])

    def test_automation_uses_shared_safe_lifecycle_and_plex_badges(self):
        self.automation_history()
        self.downloads.return_value = {
            "automation-group": {
                "progress": 70, "status": "downloading", "downloadClient": "private-client",
            },
        }
        item = self.client.get("/api/admin/requests").get_json()["requests"][0]
        self.assertEqual(item["requestStatus"], "downloading")
        self.assertEqual(item["downloadStatus"], {"progress": 70, "status": "downloading"})
        self.assertNotIn("private-client", str(item))
        self.downloads.return_value = {}
        self.library.return_value = {"automation-group": {"fullyAvailable": True}}
        self.plex.return_value = {"releaseGroupsByMbid": {
            "automation-group": [{"url": "https://app.plex.tv/album", "plexampUrl": "https://listen.plex.tv/album"}],
        }}
        item = self.client.get("/api/admin/requests").get_json()["requests"][0]
        self.assertEqual(item["requestStatus"], "available")
        self.assertTrue(item["availableInPlex"])
        self.assertEqual(item["plexUrl"], "https://app.plex.tv/album")
        self.assertEqual(item["plexampUrl"], "https://listen.plex.tv/album")

    def test_automation_stays_out_of_personal_history_and_user_counts(self):
        self.user_history()
        self.automation_history()
        profile = self.client.get("/api/account/profile").get_json()
        self.assertEqual([item["mbid"] for item in profile["requests"]["release-group"]], ["user-group"])
        users = self.client.get("/api/admin/users").get_json()["users"]
        self.assertEqual(len(users), 1)
        self.assertEqual(users[0]["requestCount"], 1)
        with storage.db() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM request_history").fetchone()[0], 1)

    def test_automation_audit_remains_admin_only(self):
        self.automation_history()
        self.app.config["AUTOMATION_API_KEY"] = "isolated-admin-access-test-key"
        machine = self.app.test_client()
        self.assertEqual(machine.get("/api/admin/requests").status_code, 401)
        self.assertEqual(machine.get("/api/admin/requests", headers={
            "X-Api-Key": "isolated-admin-access-test-key",
        }).status_code, 401)
        with storage.db() as connection:
            connection.execute("UPDATE users SET role = 'user' WHERE id = ?", (self.user_id,))
        self.assertEqual(self.client.get("/api/admin/requests").status_code, 403)
