"""Regression tests for privacy-scoped Last.fm response retention."""

import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

import requests
from werkzeug.security import generate_password_hash


TEST_DATA = tempfile.TemporaryDirectory(prefix="melodarr-lastfm-cache-tests-")
os.environ.update({
    "MELODARR_DATABASE": os.path.join(TEST_DATA.name, "melodarr.db"),
    "MELODARR_CACHE_DATABASE": os.path.join(
        TEST_DATA.name,
        "cache",
        "metadata.db",
    ),
    "MELODARR_SETTINGS": os.path.join(TEST_DATA.name, "settings.json"),
    "MELODARR_SECRET_KEY_FILE": os.path.join(
        TEST_DATA.name,
        "session-secret.key",
    ),
    "MELODARR_ARTWORK_CACHE": os.path.join(TEST_DATA.name, "artwork"),
})

from backend.api_cache import cache_db, cache_key
from backend.application import create_app
from backend.config import LASTFM_URL
from backend.services import lastfm
from backend.storage import db, save_service, write_settings_file


class Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.headers = {}

    def raise_for_status(self):
        if self.status_code >= 400:
            error = requests.HTTPError(f"HTTP {self.status_code}")
            error.response = self
            raise error

    def json(self):
        return self._payload


class LastfmCachePrivacyTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app({"TESTING": True, "SECRET_KEY": "privacy-test"})
        self.client = self.app.test_client()
        with db() as connection:
            connection.execute("DELETE FROM plex_auth_flows")
            connection.execute("DELETE FROM pending_lidarr_searches")
            connection.execute("DELETE FROM recommendation_cache")
            connection.execute("DELETE FROM listening_profiles")
            connection.execute("DELETE FROM plex_listens")
            connection.execute("DELETE FROM request_history")
            connection.execute("DELETE FROM account_invitations")
            connection.execute("DELETE FROM users")
        with cache_db() as connection:
            connection.execute("DELETE FROM api_cache")
        write_settings_file({})

    def register_admin(self):
        response = self.client.post(
            "/api/auth/register",
            json={"username": "owner", "password": "a-secure-password"},
        )
        self.assertEqual(response.status_code, 201)
        return response.get_json()

    def create_user(self, username, *, lastfm_username=None, role="user"):
        with db() as connection:
            return connection.execute(
                "INSERT INTO users "
                "(username, password_hash, role, created_at, lastfm_username) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    username,
                    generate_password_hash(f"{username}-password"),
                    role,
                    time.time(),
                    lastfm_username,
                ),
            ).lastrowid

    def seed_user_cache(self, username, marker):
        with patch(
            "backend.api_cache.requests.get",
            return_value=Response({"marker": marker}),
        ):
            return lastfm.get(
                "user.gettopartists",
                username,
                "shared-lastfm-key",
                limit=10,
            )

    def seed_public_cache(self, marker):
        with patch(
            "backend.api_cache.requests.get",
            return_value=Response({"marker": marker}),
        ):
            return lastfm.get_public(
                "chart.gettopartists",
                "shared-lastfm-key",
                limit=10,
            )

    def cache_rows(self):
        with cache_db() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT cache_key, value, expires_at FROM api_cache "
                    "ORDER BY cache_key"
                )
            ]

    def cache_markers(self):
        return {
            json.loads(row["value"])["marker"]
            for row in self.cache_rows()
        }

    def insert_legacy_row(self, marker, *, expired=False):
        params = {
            "method": "user.gettopartists",
            "api_key": "legacy-shared-key",
            "format": "json",
            "user": "legacy-private-handle",
        }
        with cache_db() as connection:
            connection.execute(
                "INSERT INTO api_cache (cache_key, value, expires_at) "
                "VALUES (?, ?, ?)",
                (
                    cache_key("lastfm", LASTFM_URL, params),
                    json.dumps({"marker": marker}),
                    time.time() + (-60 if expired else 3600),
                ),
            )

    def test_user_namespace_is_case_stable_and_does_not_store_the_handle(self):
        first = lastfm.user_cache_namespace("Private.Listener")
        second = lastfm.user_cache_namespace(" private.listener ")

        self.assertEqual(first, second)
        self.assertTrue(first.startswith("lastfm:user:"))
        self.assertNotIn("private.listener", first)
        self.assertEqual(len(first.rsplit(":", 1)[1]), 64)

    def test_unlink_deletes_only_that_user_scope_and_unattributable_legacy_rows(self):
        admin = self.register_admin()
        admin_id = admin["id"]
        with db() as connection:
            connection.execute(
                "UPDATE users SET lastfm_username = ? WHERE id = ?",
                ("old-private-handle", admin_id),
            )
        self.seed_user_cache("old-private-handle", "old-user")
        self.seed_user_cache("unrelated-handle", "unrelated-user")
        self.seed_public_cache("public-data")
        self.insert_legacy_row("legacy-private-data", expired=True)

        response = self.client.post(
            "/api/account/lastfm",
            headers={"X-CSRF-Token": admin["csrfToken"]},
            json={"username": ""},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.cache_markers(),
            {"unrelated-user", "public-data"},
        )
        with db() as connection:
            user = connection.execute(
                "SELECT lastfm_username FROM users WHERE id = ?",
                (admin_id,),
            ).fetchone()
        self.assertIsNone(user["lastfm_username"])

    def test_relink_removes_the_old_scope_without_deleting_another_user(self):
        admin = self.register_admin()
        admin_id = admin["id"]
        save_service("lastfm", {"apiKey": "shared-lastfm-key"})
        with db() as connection:
            connection.execute(
                "UPDATE users SET lastfm_username = ? WHERE id = ?",
                ("old-handle", admin_id),
            )
        self.seed_user_cache("old-handle", "old-user")
        self.seed_user_cache("unrelated-handle", "unrelated-user")

        with patch(
            "backend.api_cache.requests.get",
            return_value=Response({"marker": "new-user-validation"}),
        ):
            response = self.client.post(
                "/api/account/lastfm",
                headers={"X-CSRF-Token": admin["csrfToken"]},
                json={"username": "new-handle"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.cache_markers(),
            {"unrelated-user", "new-user-validation"},
        )
        with db() as connection:
            user = connection.execute(
                "SELECT lastfm_username FROM users WHERE id = ?",
                (admin_id,),
            ).fetchone()
        self.assertEqual(user["lastfm_username"], "new-handle")

    def test_admin_username_change_clears_only_the_previous_user_scope(self):
        admin = self.register_admin()
        target_id = self.create_user(
            "target-user",
            lastfm_username="target-old-handle",
        )
        self.seed_user_cache("target-old-handle", "target-old")
        self.seed_user_cache("other-handle", "other-user")

        response = self.client.patch(
            f"/api/admin/users/{target_id}",
            headers={"X-CSRF-Token": admin["csrfToken"]},
            json={"lastfmUsername": "target-new-handle"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.cache_markers(), {"other-user"})
        with db() as connection:
            user = connection.execute(
                "SELECT lastfm_username FROM users WHERE id = ?",
                (target_id,),
            ).fetchone()
        self.assertEqual(user["lastfm_username"], "target-new-handle")

    def test_account_deletion_removes_only_the_deleted_users_scope(self):
        admin = self.register_admin()
        target_id = self.create_user(
            "target-user",
            lastfm_username="deleted-handle",
        )
        other_id = self.create_user(
            "other-user",
            lastfm_username="retained-handle",
        )
        self.seed_user_cache("deleted-handle", "deleted-user")
        self.seed_user_cache("retained-handle", "retained-user")
        self.seed_public_cache("public-data")

        response = self.client.delete(
            f"/api/admin/users/{target_id}",
            headers={"X-CSRF-Token": admin["csrfToken"]},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.cache_markers(),
            {"retained-user", "public-data"},
        )
        with db() as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT id FROM users WHERE id = ?",
                    (target_id,),
                ).fetchone()
            )
            self.assertIsNotNone(
                connection.execute(
                    "SELECT id FROM users WHERE id = ?",
                    (other_id,),
                ).fetchone()
            )


if __name__ == "__main__":
    unittest.main()
