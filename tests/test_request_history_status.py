"""History status filters share local card availability and run before pagination."""

if __package__:
    from ._test_environment import TEST_ROOT
else:
    from _test_environment import TEST_ROOT

from unittest.mock import patch
from backend import request_history_search

if __package__:
    from .test_backend import DatabaseTestCase
else:
    from test_backend import DatabaseTestCase
from backend.storage import db, enqueue_lidarr_search, record_request, save_service


class RequestHistoryStatusTests(DatabaseTestCase):
    def test_mixed_history_boundary_matrix_and_stable_page_partitions(self):
        for size in (0, 1, 100, 101, 205):
            with db() as connection:
                connection.execute("DELETE FROM request_history")
                connection.executemany(
                    "INSERT INTO request_history (user_id, kind, mbid, name, created_at) VALUES (?, ?, ?, 'Same Name Café', 1)",
                    [(self.user_id, "artist" if i % 2 else "release-group", f"boundary-{size}-{i}") for i in range(size)],
                )
                ordered = list(connection.execute("SELECT id, kind, mbid FROM request_history ORDER BY created_at DESC, id DESC"))
            for regime in ("none", "all", "mixed"):
                available = {row["mbid"] for i, row in enumerate(ordered) if regime == "all" or (regime == "mixed" and i % 3 == 0)}
                self.plex["artistsByMbid"] = {row["mbid"]: {"url": "local"} for row in ordered if row["kind"] == "artist" and row["mbid"] in available}
                self.albums.clear()
                self.albums.update({row["mbid"]: {"fullyAvailable": True} for row in ordered if row["kind"] == "release-group" and row["mbid"] in available})
                for query in ("", "cafe", "Same Name", "Absent"):
                    for status in ("all", "requested", "available"):
                        with self.subTest(size=size, regime=regime, query=query, status=status):
                            expected = [row for row in ordered if query != "Absent" and (status == "all" or (row["mbid"] in available) == (status == "available"))]
                            pages = (len(expected) + 99) // 100
                            seen = []
                            for page in range(1, max(1, pages) + 2):
                                body = self.history(status, q=query, page=page)
                                self.assertEqual(body["pagination"], {"page": page, "pageSize": 100, "total": len(expected), "totalPages": pages})
                                selected = expected[(page - 1) * 100:page * 100]
                                for kind in ("artist", "release-group"):
                                    self.assertEqual([row["id"] for row in body["requests"][kind]], [row["id"] for row in selected if row["kind"] == kind])
                                if query or status != "all":
                                    self.assertEqual(body["matchCounts"], {kind: sum(row["kind"] == kind for row in expected) for kind in ("artist", "release-group")})
                                seen.extend(row["id"] for group in body["requests"].values() for row in group)
                            self.assertEqual(len(seen), len(set(seen)))
                            self.assertEqual(set(seen), {row["id"] for row in expected})

    def test_multiple_anime_associations_and_overlapping_aliases_do_not_duplicate_rows(self):
        record_request(self.user_id, "release-group", "multi-anime", "Album")
        with db() as connection:
            for theme, slug in enumerate(("anime-one", "anime-two"), 1):
                connection.execute(
                    "INSERT INTO anime_theme_release_group_links (anime_slug, anime_name, theme_id, theme_label, theme_type, song_title, release_group_mbid, created_at, updated_at) "
                    "VALUES (?, 'Shared Anime', ?, 'Opening', 'OP', 'Song', 'multi-anime', 0, 0)", (slug, theme),
                )
                request_history_search.save_names(connection, "anime", slug, ["Shared Alias", f"Alias {theme}"], catalog=True)
        for status in ("requested", "available"):
            if status == "available":
                self.albums["multi-anime"] = {"fullyAvailable": True}
            for query in ("Shared Anime", "Shared Alias", "Alias 1", "Alias 2"):
                body = self.history(status, q=query)
                self.assertEqual(body["pagination"]["total"], 1)
                self.assertEqual(body["matchCounts"], {"artist": 0, "release-group": 1})
                self.assertEqual(len(body["requests"]["release-group"]), 1)

    def test_legacy_row_without_alias_sources_remains_searchable_and_local(self):
        with db() as connection:
            connection.execute(
                "INSERT INTO request_history (user_id, kind, mbid, name, anime_name, created_at) VALUES (?, 'release-group', 'legacy-missing', '雫', 'Row Snapshot', 1)", (self.user_id,),
            )
        for query in ("雫", "Row Snapshot"):
            self.assertEqual(self.history("requested", q=query)["pagination"]["total"], 1)
        self.assertEqual(self.history("requested", q="Shizuku")["pagination"]["total"], 0)
    def setUp(self):
        super().setUp()
        self.csrf = self.register()
        with db() as connection:
            self.user_id = connection.execute("SELECT id FROM users").fetchone()[0]
            connection.execute("DELETE FROM request_history_search_aliases")
            connection.execute("DELETE FROM anime_theme_release_group_links")
        self.enterContext(patch("backend.storage._wake_recommendations"))
        self.network = self.enterContext(patch(
            "requests.sessions.Session.request", side_effect=AssertionError("History filter attempted network I/O"),
        ))
        self.enterContext(patch("backend.services.musicbrainz.get", side_effect=AssertionError("MusicBrainz lookup")))
        self.enterContext(patch("backend.services.animethemes.detail", side_effect=AssertionError("AnimeThemes lookup")))
        self.albums, self.downloads = {}, {}
        self.plex = {"artistsByMbid": {}, "releaseGroupsByMbid": {}}
        self.read_patches = [
            patch("backend.routes.account._profile_plex_index", return_value=self.plex),
            patch("backend.services.lidarr.cached_library_availability", return_value=self.albums),
            patch("backend.services.lidarr.cached_download_availability", return_value=self.downloads),
        ]
        self.local_readers = [reader.get_original()[0] for reader in self.read_patches]
        self.plex_read, self.album_read, self.download_read = [self.enterContext(reader) for reader in self.read_patches]

    def history(self, status="all", **params):
        response = self.client.get("/api/account/profile", query_string={"status": status, **params})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.network.assert_not_called()
        return response.get_json()

    def rows(self, payload):
        return {row["mbid"]: row for group in payload["requests"].values() for row in group}

    def seed_states(self):
        for mbid in ("artist-ready", "artist-missing", "artist-empty"):
            record_request(self.user_id, "artist", mbid, "Match Artist")
        self.plex["artistsByMbid"] = {"artist-ready": {"url": "https://app.plex.tv/artist"}, "artist-empty": {}}
        for mbid in ("requested", "downloading", "available", "partial", "plex-only"):
            record_request(self.user_id, "release-group", mbid, "Match Album", artist_name="Match Artist")
        enqueue_lidarr_search(self.user_id, "queued", 1, 2, "Match Album", artist_name="Match Artist")
        self.downloads.update({"downloading": {"progress": 25, "status": "downloading"}, "available": {"progress": 90}})
        self.albums.update({"available": {"fullyAvailable": True}, "partial": {"fullyAvailable": False}})
        self.plex["releaseGroupsByMbid"] = {"partial": [{"url": "https://app.plex.tv/partial"}], "plex-only": [{"url": "https://app.plex.tv/album"}]}

    def test_all_preserves_every_lifecycle_and_existing_card_fields(self):
        self.seed_states()
        rows = self.rows(self.history())
        self.assertEqual(len(rows), 9)
        for mbid, state in (("requested", "requested"), ("queued", "queued"), ("downloading", "downloading"), ("available", "available")):
            self.assertEqual(rows[mbid]["requestStatus"], state)
        self.assertIsNone(rows["available"]["downloadStatus"])
        self.assertEqual(rows["downloading"]["downloadStatus"]["progress"], 25)
        self.assertTrue(rows["artist-ready"]["availableInPlex"])
        self.assertEqual(rows["artist-ready"]["plexUrl"], "https://app.plex.tv/artist")
        for row in rows.values():
            self.assertIn("id", row)
            self.assertIn("created_at", row)
            self.assertEqual(row["use_for_recommendations"], 1)

    def test_requested_includes_all_in_progress_states_and_unavailable_artists(self):
        self.seed_states()
        payload = self.history("requested")
        rows = self.rows(payload)
        self.assertEqual(set(rows), {"artist-missing", "artist-empty", "requested", "queued", "downloading", "partial", "plex-only"})
        self.assertEqual(payload["matchCounts"], {"artist": 2, "release-group": 5})
        self.assertTrue(rows["plex-only"]["availableInPlex"])
        self.assertEqual(rows["plex-only"]["requestStatus"], "requested")
        self.plex_read.assert_called_once()
        self.album_read.assert_called_once()
        self.download_read.assert_called_once()

    def test_available_uses_fully_available_lidarr_albums_and_existing_plex_artist_state(self):
        self.seed_states()
        payload = self.history("available")
        rows = self.rows(payload)
        self.assertEqual(set(rows), {"artist-ready", "available"})
        self.assertEqual(payload["matchCounts"], {"artist": 1, "release-group": 1})
        self.assertEqual(rows["available"]["requestStatus"], "available")
        self.assertFalse(rows["available"]["availableInPlex"])

    def test_requested_keeps_a_request_through_queue_and_download_until_available(self):
        record_request(self.user_id, "release-group", "transition", "Transition")
        self.assertEqual(self.rows(self.history("requested"))["transition"]["requestStatus"], "requested")
        enqueue_lidarr_search(self.user_id, "transition", 1, 2, "Transition")
        self.assertEqual(self.rows(self.history("requested"))["transition"]["requestStatus"], "queued")
        self.downloads["transition"] = {"progress": 50}
        self.assertEqual(self.rows(self.history("requested"))["transition"]["requestStatus"], "downloading")
        self.albums["transition"] = {"fullyAvailable": True}
        self.assertEqual(self.rows(self.history("requested")), {})
        self.assertEqual(self.rows(self.history("available"))["transition"]["requestStatus"], "available")

    def test_status_and_text_filter_entire_history_before_counts_and_pagination(self):
        with db() as connection:
            connection.executemany(
                "INSERT INTO request_history (user_id, kind, mbid, name, created_at) VALUES (?, 'artist', ?, ?, ?)",
                [(self.user_id, f"row-{i}", "Match" if i < 205 else "Other", i) for i in range(505)],
            )
        self.plex["artistsByMbid"] = {f"row-{i}": {"url": "local"} for i in range(205, 505)}
        for status, query in (("requested", ""), ("requested", "MATCH"), ("all", "match")):
            first = self.history(status, q=query)
            last = self.history(status, q=query, page=3)
            self.assertEqual(first["pagination"], {"page": 1, "pageSize": 100, "total": 205, "totalPages": 3})
            self.assertEqual(first["matchCounts"], {"artist": 205, "release-group": 0})
            self.assertEqual([row["mbid"] for row in first["requests"]["artist"]], [f"row-{i}" for i in range(204, 104, -1)])
            self.assertEqual([row["mbid"] for row in last["requests"]["artist"]], [f"row-{i}" for i in range(4, -1, -1)])
        self.assertEqual(self.history("available")["pagination"]["total"], 300)
        empty = self.history("available", q="match")
        self.assertEqual(empty["pagination"]["total"], 0)
        self.assertEqual(empty["matchCounts"], {"artist": 0, "release-group": 0})
        self.assertEqual(self.history()["pagination"]["total"], 505)

    def test_query_and_status_compose_for_artist_album_anime_and_aliases(self):
        record_request(self.user_id, "artist", "match-artist", "All Time Low", search_metadata=({"name": "All Time Low", "aliases": [{"name": "Known Alias"}]},))
        record_request(self.user_id, "release-group", "match-album", "Album", artist_name="All Time Low", anime_name="Fullmetal Alchemist", song_title="Again")
        self.albums["match-album"] = {"fullyAvailable": True}
        self.assertEqual(set(self.rows(self.history("all", q="all time low"))), {"match-artist", "match-album"})
        self.assertEqual(set(self.rows(self.history("requested", q="all time low"))), {"match-artist"})
        for query in ("All Time Low", "Album", "Fullmetal", "again"):
            self.assertEqual(set(self.rows(self.history("available", q=query))), {"match-album"})
        self.assertEqual(set(self.rows(self.history("requested", q="Known Alias"))), {"match-artist"})

    def test_status_validation_and_authorization_precede_snapshot_reads(self):
        for status in ("queued", "Available", "", "' OR 1=1 --"):
            response = self.client.get("/api/account/profile", query_string={"status": status})
            self.assertEqual(response.status_code, 400)
            self.assertIn("Status must be", response.get_json()["error"])
        self.plex_read.assert_not_called()
        with db() as connection:
            other = connection.execute("INSERT INTO users (username, password_hash, role, created_at) VALUES ('other-user', 'unused', 'user', 0)").lastrowid
        record_request(other, "artist", "private", "Private")
        self.assertEqual(self.rows(self.history("requested")), {})
        self.assertEqual(set(self.rows(self.history("requested", username="other-user"))), {"private"})
        with db() as connection:
            connection.execute("UPDATE users SET role = 'user' WHERE id = ?", (self.user_id,))
        self.plex_read.reset_mock()
        for status in ("requested", "available", "invalid"):
            response = self.client.get("/api/account/profile", query_string={"status": status, "q": "private", "username": "other-user"})
            self.assertEqual(response.status_code, 403)
        self.plex_read.assert_not_called()
        self.client.post("/api/auth/logout", headers={"X-CSRF-Token": self.csrf})
        self.assertEqual(self.client.get("/api/account/profile?status=requested").status_code, 401)

    def test_configured_services_use_only_cached_availability_and_local_metadata(self):
        record_request(self.user_id, "release-group", "legacy", "Legacy Album")
        save_service("plex", {"url": "http://unavailable", "token": "token", "librarySectionIds": [1]})
        save_service("lidarr", {"url": "http://unavailable", "apiKey": "key"})
        # Exercise the real cached readers instead of the controlled classification fixtures.
        for reader, original in zip((self.plex_read, self.album_read, self.download_read), self.local_readers):
            reader.side_effect = original
        for status in ("all", "requested", "available"):
            payload = self.history(status)
            self.assertEqual(payload["pagination"]["total"], 0 if status == "available" else 1)
