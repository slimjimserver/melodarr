"""Retryable, local-only backfill and page-bounded Requests enrichment."""

if __package__:
    from ._test_environment import TEST_ROOT
    from .test_backend import DatabaseTestCase
else:
    from _test_environment import TEST_ROOT
    from test_backend import DatabaseTestCase

import json
import os
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from backend import api_cache, request_history_search as search, storage
from backend.routes import account
from backend.services import anime_theme_links, musicbrainz


class RequestHistoryFreshStartupTests(unittest.TestCase):
    def test_durable_database_can_initialize_before_fresh_cache(self):
        for empty_file in (False, True):
            with self.subTest(empty_file=empty_file), tempfile.TemporaryDirectory() as directory:
                database = os.path.join(directory, "melodarr.db")
                cache = os.path.join(directory, "cache", "metadata.db")
                if empty_file:
                    os.makedirs(os.path.dirname(cache))
                    sqlite3.connect(cache).close()
                with patch.object(storage, "DATABASE", database), \
                     patch.object(storage, "SETTINGS_FILE", os.path.join(directory, "settings.json")), \
                     patch.object(api_cache, "CACHE_DATABASE", cache), \
                     patch("requests.sessions.Session.request", side_effect=AssertionError("Startup attempted upstream I/O")) as network:
                    # Match the Docker runtime smoke check's initialization order.
                    storage.init_db()
                    api_cache.init_cache_db()
                    with storage.db() as connection:
                        self.assertEqual(connection.execute("SELECT COUNT(*) FROM request_history").fetchone()[0], 0)
                        self.assertEqual(connection.execute("SELECT COUNT(*) FROM request_history_search_migrations").fetchone()[0], 1)
                        before = list(connection.iterdump())
                    storage.init_db()
                    with storage.db() as connection:
                        self.assertEqual(list(connection.iterdump()), before)
                    with api_cache.cache_db() as connection:
                        self.assertEqual(connection.execute("SELECT COUNT(*) FROM api_cache").fetchone()[0], 0)
                    network.assert_not_called()


class RequestHistoryEnrichmentTests(DatabaseTestCase):
    artist = "11111111-1111-4111-8111-111111111111"

    def setUp(self):
        super().setUp()
        self.register()
        with storage.db() as connection:
            self.user_id = connection.execute("SELECT id FROM users").fetchone()[0]
            connection.execute("DELETE FROM request_history_search_aliases")
            connection.execute("DELETE FROM anime_theme_release_group_links")
        with api_cache.cache_db() as connection:
            connection.execute("DELETE FROM api_cache")
            for table in ("track_search_artist_names", "track_search_release_groups", "track_search_release_group_artists", "track_search_release_group_refs"):
                connection.execute(f"DELETE FROM {table}")
        self.network = self.enterContext(patch("requests.sessions.Session.request", side_effect=AssertionError("Requests attempted upstream I/O")))
        self.enterContext(patch("backend.storage._wake_recommendations"))
        self.enterContext(patch.object(account, "_profile_plex_index", return_value={}))

    def cache_document(self, mbid, document, *, kind="release-group", expires_at=0):
        includes = "aliases+artist-credits+url-rels" if kind == "release-group" else "aliases"
        key = musicbrainz.metadata_cache_key(f"/{kind}/{mbid}", includes)
        with api_cache.cache_db() as connection:
            connection.execute("INSERT OR REPLACE INTO api_cache VALUES (?, ?, ?)", (key, json.dumps(document), expires_at))

    def seed(self, size, *, user_id=None, kind="release-group", mbid=None):
        with storage.db() as connection:
            connection.executemany(
                "INSERT INTO request_history(user_id,kind,mbid,name,created_at) VALUES (?, ?, ?, 'Canonical', ?)",
                ((user_id or self.user_id, kind, mbid or f"group-{i}", i) for i in range(size)),
            )

    def legacy_schema(self):
        with storage.db() as connection:
            for table in ("request_history_search_entities", "request_history_search_aliases", "request_history_search_migrations"):
                connection.execute("DROP TABLE " + table)

    def profile(self, **parameters):
        response = self.client.get("/api/account/profile", query_string=parameters)
        self.assertEqual(response.status_code, 200, response.get_json())
        self.network.assert_not_called()
        return response.get_json()

    def snapshot(self):
        with storage.db() as connection:
            return {table: [tuple(row) for row in connection.execute("SELECT * FROM " + table + " ORDER BY 1, 2")]
                    for table in ("request_history", "request_history_search_entities", "request_history_search_aliases")}

    def test_anime_alias_cleanup_with_missing_cache_preserves_local_data(self):
        storage.record_request(self.user_id, "artist", self.artist, "Artist", search_metadata=({"aliases": ["Artist Alias"]},))
        storage.record_request(self.user_id, "release-group", "group", "Album", anime_slug="shared-anime", anime_name="Row Local Name", search_metadata=({"aliases": ["Album Alias"]},))
        with storage.db() as connection:
            connection.execute("DELETE FROM request_history_search_migrations")
            connection.execute("INSERT INTO request_history_search_aliases VALUES ('anime', 'shared-anime', 'untrustedalias')")
            connection.execute(
                "INSERT INTO anime_theme_release_group_links "
                "(anime_slug, anime_name, theme_id, theme_label, theme_type, song_title, release_group_mbid, created_at, updated_at) "
                "VALUES ('shared-anime', 'Trusted Mapping Name', 1, 'OP', 'OP', 'Song', 'group', 0, 0)"
            )
        before = self.snapshot()
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(api_cache, "CACHE_DATABASE", os.path.join(directory, "cache", "metadata.db")):
            storage.init_db()
            first = self.snapshot()
            self.assertEqual(first["request_history"], before["request_history"])
            self.assertEqual(first["request_history_search_entities"], before["request_history_search_entities"])
            self.assertEqual(
                [row for row in first["request_history_search_aliases"] if row[0] != "anime"],
                [row for row in before["request_history_search_aliases"] if row[0] != "anime"],
            )
            self.assertEqual(
                {tuple(row) for row in first["request_history_search_aliases"] if row[0] == "anime"},
                {("anime", "shared-anime", "sharedanime"), ("anime", "shared-anime", "trustedmappingname")},
            )
            storage.init_db()
            self.assertEqual(self.snapshot(), first)
        for query in ("Trusted Mapping Name", "Row Local Name", "Album Alias"):
            self.assertEqual(self.profile(q=query)["pagination"]["total"], 1)
        self.assertEqual(self.profile(q="Artist Alias")["pagination"]["total"], 1)
        self.assertEqual(self.profile(q="Untrusted Alias")["pagination"]["total"], 0)
        self.network.assert_not_called()

    def test_transient_backfill_cache_failure_rolls_back_and_retries(self):
        self.seed(3, kind="artist", mbid=self.artist)
        self.cache_document(self.artist, {"id": self.artist, "name": "Canonical", "aliases": [{"name": "Trusted Alias"}]}, kind="artist")
        self.legacy_schema()
        with patch.object(search._CaptureCache, "_document", side_effect=sqlite3.OperationalError("database is locked")):
            with self.assertRaises(sqlite3.OperationalError):
                storage.init_db()
        with storage.db() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM request_history").fetchone()[0], 3)
            self.assertIsNone(connection.execute("SELECT 1 FROM sqlite_master WHERE name='request_history_search_migrations'").fetchone())
        storage.init_db()
        self.assertEqual(self.profile(q="Trusted Alias")["pagination"]["total"], 3)
        before = self.snapshot()
        with patch.object(search, "backfill", side_effect=AssertionError("Completed migration ran again")):
            storage.init_db()
        self.assertEqual(self.snapshot(), before)

    def test_ordinary_capture_remains_best_effort_on_cache_failure(self):
        with patch.object(search._CaptureCache, "_document", side_effect=sqlite3.OperationalError("database is locked")):
            storage.record_request(self.user_id, "artist", self.artist, "Canonical")
        self.assertEqual(self.profile(q="Canonical")["pagination"]["total"], 1)

    def test_streamed_backfill_reuses_catalog_work_and_deduplicates(self):
        self.seed(1050, kind="artist", mbid=self.artist)
        self.cache_document(self.artist, {"id": self.artist, "name": "Canonical", "aliases": [{"name": "Reusable Alias"}]}, kind="artist")
        self.legacy_schema()
        original = api_cache.cache_db
        reads = []
        from contextlib import contextmanager

        @contextmanager
        def traced_cache():
            with original() as connection:
                connection.set_trace_callback(reads.append)
                yield connection

        with patch.object(api_cache, "cache_db", side_effect=traced_cache) as opened, \
             patch.object(musicbrainz, "romanized_artist_name", wraps=musicbrainz.romanized_artist_name) as extracted:
            storage.init_db()
        self.assertEqual(opened.call_count, 2)  # Catalog cleanup plus shared capture.
        self.assertEqual(extracted.call_count, 1)
        self.assertEqual(sum("SELECT name FROM sqlite_master" in sql for sql in reads), 1)
        self.assertLessEqual(sum("SELECT value FROM api_cache WHERE cache_key =" in sql for sql in reads), 4)
        state = self.snapshot()
        self.assertEqual(len(state["request_history_search_entities"]), 1050)
        self.assertEqual(len(state["request_history_search_aliases"]), 2)
        self.assertEqual(self.profile(q="Reusable Alias")["pagination"]["total"], 1050)
        with patch.object(search, "backfill", side_effect=AssertionError("Repeated backfill")):
            storage.init_db()
        self.assertEqual(state, self.snapshot())

    def test_halfway_failure_rolls_back_every_row_and_retry_completes(self):
        self.seed(7, kind="artist", mbid=self.artist)
        self.legacy_schema()
        capture = search.capture
        calls = 0

        def fail_halfway(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 4:
                raise sqlite3.OperationalError("Injected hard failure")
            return capture(*args, **kwargs)

        with patch.object(search, "capture", side_effect=fail_halfway):
            with self.assertRaises(sqlite3.OperationalError):
                storage.init_db()
        with storage.db() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM request_history").fetchone()[0], 7)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'request_history_search_%' AND type='table'").fetchone()[0], 0)
        storage.init_db()
        self.assertEqual(len(self.snapshot()["request_history_search_entities"]), 7)

    def test_snapshot_failures_are_independent_and_preserve_membership(self):
        self.seed(1)
        for library_failed, download_failed, lifecycle in ((False, True, "available"), (True, False, "downloading"), (True, True, "requested")):
            with self.subTest(library_failed=library_failed, download_failed=download_failed):
                with patch.object(account.lidarr, "cached_library_availability", side_effect=sqlite3.DatabaseError("Library failure") if library_failed else None, return_value={"group-0": {"fullyAvailable": True}}), \
                     patch.object(account.lidarr, "cached_download_availability", side_effect=sqlite3.DatabaseError("Download failure") if download_failed else None, return_value={"group-0": {"progress": 25, "status": "downloading"}}):
                    body = self.profile(status="all")
                    self.assertEqual(body["requests"]["release-group"][0]["requestStatus"], lifecycle)
                    available = lifecycle == "available"
                    self.assertEqual(self.profile(status="available")["pagination"]["total"], int(available))
                    self.assertEqual(self.profile(status="requested")["pagination"]["total"], int(not available))
        storage.enqueue_lidarr_search(self.user_id, "group-0", 1, 2, "Canonical")
        with patch.object(account.lidarr, "cached_library_availability", side_effect=sqlite3.DatabaseError()), \
             patch.object(account.lidarr, "cached_download_availability", side_effect=sqlite3.DatabaseError()):
            body = self.profile(status="requested")
            self.assertTrue(all(row["requestStatus"] == "queued" for row in body["requests"]["release-group"]))

    def test_page_enrichment_uses_one_batch_and_only_selected_rows(self):
        for size in (3, 100, 2000):
            with self.subTest(size=size):
                with storage.db() as connection:
                    connection.execute("DELETE FROM request_history")
                self.seed(size)
                expected = {f"group-{i}" for i in range(max(0, size - 100), size)}
                with patch.object(anime_theme_links, "db", wraps=anime_theme_links.db) as links_db, \
                     patch.object(api_cache, "cache_db", wraps=api_cache.cache_db) as metadata_db, \
                     patch.object(account, "local_release_group_metadata_batch", wraps=account.local_release_group_metadata_batch) as metadata, \
                     patch.object(account, "_release_group_snapshots", return_value=({}, {})), \
                     patch.object(account, "_profile_history_item", wraps=account._profile_history_item) as decorated:
                    body = self.profile(status="all")
                self.assertEqual(links_db.call_count, 1)
                self.assertEqual(metadata_db.call_count, 1)
                self.assertEqual(metadata.call_args.args[0], expected)
                self.assertEqual(decorated.call_count, min(size, 100))
                self.assertEqual([row["mbid"] for row in body["requests"]["release-group"]], [f"group-{i}" for i in range(size - 1, max(-1, size - 101), -1)])
                self.assertEqual(body["pagination"]["total"], size)

    def test_batched_links_preserve_order_explicit_context_and_single_cards(self):
        self.seed(2)
        with storage.db() as connection:
            for mbid in ("group-0", "group-1"):
                for theme, name in ((1, "Zebra Anime"), (2, "Alpha Anime")):
                    connection.execute("INSERT INTO anime_theme_release_group_links(anime_slug,anime_name,theme_id,theme_label,theme_type,song_title,release_group_mbid,created_at,updated_at) VALUES (?, ?, ?, 'Opening', 'OP', 'Song', ?, 0, 0)", (f"anime-{theme}", name, theme, mbid))
            connection.execute("UPDATE request_history SET anime_slug='row-local',anime_name='My Snapshot',theme_id=7 WHERE mbid='group-1'")
        with patch.object(anime_theme_links, "links_for_release_groups", wraps=anime_theme_links.links_for_release_groups) as links:
            body = self.profile(status="all")
        self.assertEqual(links.call_args.args[0], {"group-0"})
        cards = body["requests"]["release-group"]
        self.assertEqual(len(cards), 2)
        self.assertEqual([link["animeName"] for link in cards[0]["animeThemes"]], ["My Snapshot"])
        self.assertEqual([link["animeName"] for link in cards[1]["animeThemes"]], ["Alpha Anime", "Zebra Anime"])
        self.assertEqual(cards[1]["animePath"], "/anime/anime-2#theme-2")

    def test_metadata_batch_preserves_expiry_and_plex_fallbacks(self):
        self.seed(1)
        self.cache_document("group-0", {"artist-credit": [{"name": "Cached Artist"}], "primary-type": "EP", "first-release-date": "2001-02-03"})
        self.assertEqual(self.profile(status="all")["requests"]["release-group"][0]["artist_name"], "Cached Artist")
        plex = {"releaseGroupsByMbid": {"group-0": [{"url": "local", "artistName": "Plex Artist", "releaseType": "Album", "year": 2002}]}}
        with patch.object(account, "_profile_plex_index", return_value=plex):
            row = self.profile()["requests"]["release-group"][0]
            self.assertEqual((row["artist_name"], row["release_type"], row["release_date"]), ("Plex Artist", "Album", "2002"))
        self.cache_document("group-0", {"artist-credit": [{"name": "Fresh Artist"}]}, expires_at=time.time() + 60)
        self.assertEqual(self.profile()["requests"]["release-group"][0]["artist_name"], "Fresh Artist")

    def test_small_account_search_work_ignores_large_unrelated_history(self):
        self.seed(3, kind="artist")
        with storage.db() as connection:
            other = connection.execute("INSERT INTO users(username,password_hash,role,created_at) VALUES ('unrelated','unused','user',0)").lastrowid
            connection.execute("UPDATE users SET role='user' WHERE id=?", (self.user_id,))
        self.seed(3000, user_id=other, kind="artist")
        with patch.object(search, "search_key", wraps=search.search_key) as normalized:
            body = self.profile(q="Absent", status="requested")
        self.assertEqual(body["pagination"]["total"], 0)
        self.assertEqual(body["matchCounts"], {"artist": 0, "release-group": 0})
        self.assertEqual(normalized.call_count, 38)  # Two query keys + 2 * 3 * 6 row fields.
        body = self.profile(q="Canonical", status="requested")
        self.assertEqual(body["pagination"], {"page": 1, "pageSize": 100, "total": 3, "totalPages": 1})
        self.assertEqual(len(body["requests"]["artist"]), 3)
        self.assertEqual(self.client.get("/api/account/profile?username=unrelated&q=Canonical").status_code, 403)
