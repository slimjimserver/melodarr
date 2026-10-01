"""History status filters share local card availability and run before pagination."""

if __package__:
    from ._test_environment import TEST_ROOT
else:
    from _test_environment import TEST_ROOT

from unittest.mock import patch

if __package__:
    from .test_backend import DatabaseTestCase
else:
    from test_backend import DatabaseTestCase
from backend.storage import db, enqueue_lidarr_search, record_request, save_service


class RequestHistoryStatusTests(DatabaseTestCase):
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
