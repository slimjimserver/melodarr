"""Request history searches all authorized local data before pagination."""

if __package__:
    from ._test_environment import TEST_ROOT
else:
    from _test_environment import TEST_ROOT

import json
from unittest.mock import patch

if __package__:
    from .test_backend import DatabaseTestCase
else:
    from test_backend import DatabaseTestCase
from backend import request_history_search, track_search_index
from backend.api_cache import cache_db, cache_key
from backend.services import animethemes, musicbrainz
from backend.storage import db, enqueue_lidarr_search, init_db, record_request, save_service


class RequestHistorySearchTests(DatabaseTestCase):
    artist_id = "11111111-1111-4111-8111-111111111111"
    album_id = "22222222-2222-4222-8222-222222222222"
    second_artist_id = "33333333-3333-4333-8333-333333333333"

    def setUp(self):
        super().setUp()
        self.csrf = self.register()
        with db() as connection:
            self.user_id = connection.execute("SELECT id FROM users").fetchone()[0]
            connection.execute("DELETE FROM request_history_search_aliases")
            connection.execute("DELETE FROM anime_theme_release_group_links")
        with cache_db() as connection:
            for table in (
                "track_search_artist_names", "track_search_release_groups",
                "track_search_release_group_artists", "track_search_release_group_refs",
            ):
                connection.execute(f"DELETE FROM {table}")
        self.network = self.enterContext(patch(
            "requests.sessions.Session.request", side_effect=AssertionError("History search attempted network I/O"),
        ))
        self.enterContext(patch("backend.storage._wake_recommendations"))

    def search(self, query, page=1, username=None):
        params = {"q": query, "page": page}
        if username:
            params["username"] = username
        response = self.client.get("/api/account/profile", query_string=params)
        self.assertEqual(response.status_code, 200, response.get_json())
        self.network.assert_not_called()
        return response.get_json()

    def ids(self, payload):
        return {item["mbid"] for items in payload["requests"].values() for item in items}

    def add_album(self, **metadata):
        record_request(self.user_id, "release-group", self.album_id, "So Wrong, It's Right", artist_name="All Time Low", **metadata)

    def cache_document(self, key, payload):
        with cache_db() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO api_cache (cache_key, value, expires_at) VALUES (?, ?, 0)",
                (key, json.dumps(payload)),
            )

    def test_artist_title_partial_and_case_search(self):
        record_request(self.user_id, "artist", self.artist_id, "All Time Low")
        self.add_album()
        record_request(self.user_id, "release-group", "unrelated", "Other album", artist_name="Other artist")
        for query in ("All Time Low", "ALL time LOW", "time l"):
            with self.subTest(query=query):
                payload = self.search(query)
                self.assertEqual(self.ids(payload), {self.artist_id, self.album_id})
                self.assertEqual(payload["matchCounts"], {"artist": 1, "release-group": 1})
        self.assertEqual(self.ids(self.search("So Wrong It's Right")), {self.album_id})

    def test_anime_song_theme_slug_and_unicode_normalization(self):
        self.add_album(
            anime_name="僕の心のヤバイやつ", anime_slug="boku_no_kokoro_no_yabai_yatsu",
            theme_id=1, theme_label="Opening 1", song_title="Café — Again",
        )
        for query in ("僕の心", "boku no kokoro no yabai yatsu", "BOKU-no--KOKORO", "opening_1", "cafe again", "Again"):
            with self.subTest(query=query):
                self.assertEqual(self.ids(self.search(query)), {self.album_id})

    def test_local_reverse_anime_associations_are_searchable(self):
        self.add_album()
        with db() as connection:
            connection.execute(
                "INSERT INTO anime_theme_release_group_links "
                "(anime_slug, anime_name, theme_id, theme_label, theme_type, song_title, release_group_mbid, created_at, updated_at) "
                "VALUES ('fullmetal_alchemist', 'Fullmetal Alchemist', 2, 'Opening 1', 'OP', 'again', ?, 0, 0)",
                (self.album_id,),
            )
            request_history_search.save_names(connection, "anime", "fullmetal_alchemist", ["Hagane no Renkinjutsushi"], catalog=True)
        for query in ("Fullmetal", "again", "opening", "hagane no renkinjutsushi"):
            with self.subTest(query=query):
                self.assertEqual(self.ids(self.search(query)), {self.album_id})

    def test_search_filters_entire_history_then_paginates_and_keeps_order(self):
        with db() as connection:
            connection.executemany(
                "INSERT INTO request_history (user_id, kind, mbid, name, created_at) VALUES (?, 'artist', ?, ?, ?)",
                [(self.user_id, f"request-{i}", "Match" if i < 105 else "Other", i) for i in range(505)],
            )
        self.assertNotIn("request-0", self.ids(self.search("")))
        first = self.search("match")
        second = self.search("match", page=2)
        self.assertEqual(first["pagination"], {"page": 1, "pageSize": 100, "total": 105, "totalPages": 2})
        self.assertEqual(first["matchCounts"], {"artist": 105, "release-group": 0})
        self.assertEqual([item["mbid"] for item in first["requests"]["artist"]], [f"request-{i}" for i in range(104, 4, -1)])
        self.assertEqual([item["mbid"] for item in second["requests"]["artist"]], [f"request-{i}" for i in range(4, -1, -1)])
        self.assertEqual(self.search("")["pagination"]["total"], 505)

    def test_artist_and_album_alias_snapshots_survive_cache_deletion(self):
        artist = {"id": self.artist_id, "name": "羊文学", "sort-name": "Hitsuji Bungaku",
                  "aliases": [{"name": "Hitsujibungaku"}, {"name": "Sheep Literature"}]}
        group = {"id": self.album_id, "title": "全色", "aliases": [{"name": "In Full Color", "locale": "en"}, {"name": "Zenshoku"}],
                 "artist-credit": [{"name": "羊文学", "artist": artist}]}
        track_search_index.index_artist(artist)
        track_search_index.index_release_groups([group])
        self.cache_document(musicbrainz.metadata_cache_key(f"/release-group/{self.album_id}", "aliases+artist-credits+url-rels"), group)
        record_request(self.user_id, "artist", self.artist_id, "羊文学")
        enqueue_lidarr_search(self.user_id, self.album_id, 1, 2, "全色", artist_name="羊文学")
        with cache_db() as connection:
            connection.execute("DELETE FROM api_cache")
            connection.execute("DELETE FROM track_search_artist_names")
            connection.execute("DELETE FROM track_search_release_groups")
            connection.execute("DELETE FROM track_search_release_group_artists")
        with patch("backend.services.musicbrainz.get", side_effect=AssertionError("MusicBrainz is unavailable")), \
             patch("backend.services.animethemes.detail", side_effect=AssertionError("AnimeThemes is unavailable")):
            for query in ("羊文学", "Hitsuji Bungaku", "Sheep Literature"):
                with self.subTest(query=query):
                    self.assertEqual(self.ids(self.search(query)), {self.artist_id, self.album_id})
            for query in ("全色", "In Full Color", "Zenshoku"):
                with self.subTest(query=query):
                    self.assertEqual(self.ids(self.search(query)), {self.album_id})

    def test_lidarr_metadata_preserves_known_artist_and_release_variants(self):
        record_request(self.user_id, "artist", self.artist_id, "羊文学", search_metadata=({
            "artistName": "羊文学", "romanizedName": "Hitsuji Bungaku", "aliases": ["Sheep Literature"],
        },))
        self.add_album(search_metadata=({
            "title": "So Wrong, It's Right", "romanizedTitle": "Alternate album title", "aliases": ["Another title"],
            "artist": {"foreignArtistId": self.artist_id, "artistName": "羊文学"},
        },))
        self.assertEqual(self.ids(self.search("Hitsuji Bungaku")), {self.artist_id, self.album_id})
        self.assertEqual(self.ids(self.search("Another title")), {self.album_id})
        self.assertEqual(self.ids(self.search("Alternate album title")), {self.album_id})

    def test_stored_artist_identity_links_alias_to_legacy_album_name(self):
        record_request(self.user_id, "artist", self.artist_id, "羊文学", search_metadata=({"romanizedName": "Hitsuji Bungaku"},))
        record_request(self.user_id, "release-group", self.album_id, "Album", artist_name="羊文学")
        self.assertEqual(self.ids(self.search("Hitsuji Bungaku")), {self.artist_id, self.album_id})

    def album_artist_entities(self):
        with db() as connection:
            return {row[0] for row in connection.execute(
                "SELECT entity_id FROM request_history_search_entities WHERE entity_kind = 'artist' "
                "AND request_id = (SELECT id FROM request_history WHERE mbid = ? ORDER BY id DESC LIMIT 1)",
                (self.album_id,),
            )}

    def add_shared_name_artist(self):
        record_request(self.user_id, "artist", self.artist_id, "Shared Name", search_metadata=({
            "aliases": [{"name": "OnlyArtistOneAlias"}],
        },))

    def test_explicit_artist_identity_overrides_name_fallback_and_conflicting_cached_credit(self):
        self.add_shared_name_artist()
        self.cache_document(musicbrainz.metadata_cache_key(f"/release-group/{self.album_id}", "aliases+artist-credits+url-rels"), {
            "id": self.album_id, "title": "Album", "artist-credit": [{"name": "Shared Name", "artist": {"id": self.artist_id}}],
        })
        for metadata in (
            {"artist": {"foreignArtistId": self.second_artist_id, "artistName": "Shared Name"}},
            {"foreignArtistId": self.second_artist_id, "artistName": "Shared Name"},
            {"artist-credit": [{"name": "Shared Name", "artist": {"id": self.second_artist_id}}]},
        ):
            with self.subTest(metadata=metadata):
                record_request(self.user_id, "release-group", self.album_id, "Album", artist_name="Shared Name", search_metadata=(metadata,))
                self.assertEqual(self.album_artist_entities(), {self.second_artist_id})
                self.assertEqual(self.ids(self.search("OnlyArtistOneAlias")), {self.artist_id})

    def test_cached_musicbrainz_artist_credit_precedes_name_fallback(self):
        self.add_shared_name_artist()
        self.cache_document(musicbrainz.metadata_cache_key(f"/release-group/{self.album_id}", "aliases+artist-credits+url-rels"), {
            "id": self.album_id, "title": "Album", "artist-credit": [{"name": "Shared Name", "artist": {"id": self.second_artist_id}}],
        })
        record_request(self.user_id, "release-group", self.album_id, "Album", artist_name="Shared Name")
        self.assertEqual(self.album_artist_entities(), {self.second_artist_id})
        self.assertEqual(self.ids(self.search("OnlyArtistOneAlias")), {self.artist_id})

    def test_local_index_artist_credit_precedes_name_fallback(self):
        self.add_shared_name_artist()
        track_search_index.index_release_groups([{
            "id": self.album_id, "title": "Album", "artist-credit": [{"name": "Shared Name", "artist": {"id": self.second_artist_id, "name": "Shared Name"}}],
        }])
        record_request(self.user_id, "release-group", self.album_id, "Album", artist_name="Shared Name")
        self.assertEqual(self.album_artist_entities(), {self.second_artist_id})
        self.assertEqual(self.ids(self.search("OnlyArtistOneAlias")), {self.artist_id})

    def test_ambiguous_artist_name_without_identity_evidence_is_unresolved(self):
        self.add_shared_name_artist()
        record_request(self.user_id, "artist", self.second_artist_id, "Shared Name")
        record_request(self.user_id, "release-group", self.album_id, "Album", artist_name="Shared Name")
        self.assertEqual(self.album_artist_entities(), set())
        self.assertEqual(self.ids(self.search("OnlyArtistOneAlias")), {self.artist_id})

    def test_unambiguous_legacy_fallback_captures_cached_artist_aliases(self):
        record_request(self.user_id, "artist", self.artist_id, "Shared Name")
        self.cache_document(musicbrainz.metadata_cache_key(f"/artist/{self.artist_id}", "aliases"), {
            "id": self.artist_id, "name": "Shared Name", "aliases": [{"name": "Known Later Alias"}],
        })
        record_request(self.user_id, "release-group", self.album_id, "Album", artist_name="Shared Name")
        self.assertEqual(self.album_artist_entities(), {self.artist_id})
        self.assertEqual(self.ids(self.search("Known Later Alias")), {self.artist_id, self.album_id})

    def test_cached_album_credit_captures_already_local_artist_aliases(self):
        self.cache_document(musicbrainz.metadata_cache_key(f"/release-group/{self.album_id}", "aliases+artist-credits+url-rels"), {
            "id": self.album_id, "title": "Album", "artist-credit": [{"name": "羊文学", "artist": {"id": self.artist_id, "name": "羊文学"}}],
        })
        self.cache_document(musicbrainz.metadata_cache_key(f"/artist/{self.artist_id}", "aliases+url-rels+genres"), {
            "id": self.artist_id, "name": "羊文学", "aliases": [{"name": "Hitsuji Bungaku"}],
        })
        record_request(self.user_id, "release-group", self.album_id, "Album", artist_name="羊文学")
        with cache_db() as connection:
            connection.execute("DELETE FROM api_cache")
        self.assertEqual(self.ids(self.search("Hitsuji Bungaku")), {self.album_id})

    def test_missing_aliases_do_not_trigger_romanization_or_network_at_search_time(self):
        record_request(self.user_id, "release-group", self.album_id, "雫")
        with patch("backend.services.musicbrainz.romanized_release_group_title", side_effect=AssertionError("Search attempted romanization")):
            self.assertEqual(self.ids(self.search("雫")), {self.album_id})
            self.assertEqual(self.ids(self.search("Shizuku")), set())

    def test_anime_aliases_known_before_or_after_request_survive_cache_cleanup(self):
        anime = {"slug": "boku_no_kokoro_no_yabai_yatsu", "name": "僕の心のヤバイやつ",
                 "synonyms": ["The Dangers in My Heart"]}
        with patch("backend.services.animethemes.cached_json_get", return_value={"anime": anime}):
            animethemes.detail(anime["slug"])
        self.add_album(anime_slug=anime["slug"], anime_name=anime["name"])
        self.assertEqual(self.ids(self.search("Dangers in My Heart")), {self.album_id})
        anime["synonyms"].append("Known later title")
        with patch("backend.services.animethemes.cached_json_get", return_value={"anime": anime}):
            animethemes.detail(anime["slug"])
        with cache_db() as connection:
            connection.execute("DELETE FROM api_cache")
        self.assertEqual(self.ids(self.search("Known later title")), {self.album_id})

    def test_client_anime_name_is_row_local_and_cannot_poison_another_users_search(self):
        self.add_album(anime_slug="shared-anime", anime_name="Canonical Anime", theme_id=1, theme_label="Opening 1")
        with db() as connection:
            other = connection.execute("INSERT INTO users (username, password_hash, role, created_at) VALUES ('other-user', 'unused', 'user', 0)").lastrowid
            connection.execute("UPDATE users SET role = 'user' WHERE id = ?", (self.user_id,))
        enqueue_lidarr_search(other, "other-album", 1, 2, "Other Album")
        other_client = self.app.test_client()
        with other_client.session_transaction() as session:
            session["user_id"] = other
            session["csrf_token"] = "other-csrf"
        with patch("backend.services.release_requests.notifications.queue_admin_request"), \
             patch("backend.services.musicbrainz.get", side_effect=AssertionError("Unexpected MusicBrainz lookup")), \
             patch("backend.services.animethemes.detail", side_effect=AssertionError("Unexpected AnimeThemes lookup")), \
             patch("backend.services.lidarr.lookup_album", side_effect=AssertionError("Unexpected Lidarr lookup")):
            response = other_client.post("/api/request/release-group", headers={"X-CSRF-Token": "other-csrf"}, json={
                "mbid": "other-album", "animeSlug": "shared-anime", "animeName": "PrivateAlias917", "themeId": 1, "themeLabel": "Opening 1",
            })
            self.assertEqual(response.status_code, 202, response.get_json())
            self.assertEqual(self.ids(self.search("PrivateAlias917")), set())
            own = other_client.get("/api/account/profile", query_string={"q": "PrivateAlias917"})
            self.assertEqual(own.status_code, 200)
            self.assertEqual(self.ids(own.get_json()), {"other-album"})
            self.assertEqual(own.get_json()["requests"]["release-group"][0]["anime_name"], "PrivateAlias917")
        with db() as connection:
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM request_history_search_aliases WHERE entity_kind = 'anime' AND search_key = ?",
                (request_history_search.search_key("PrivateAlias917"),),
            ).fetchone())
        self.network.assert_not_called()

    def test_shared_anime_alias_writer_requires_catalog_provenance(self):
        with db() as connection:
            with self.assertRaises(ValueError):
                request_history_search.save_names(connection, "anime", "shared-anime", ["PrivateAlias917"])

    def test_legacy_anime_alias_cleanup_rebuilds_only_local_catalog_names_once(self):
        self.add_album(anime_slug="shared-anime", anime_name="My Row Name")
        self.add_shared_name_artist()
        self.cache_document(cache_key(
            "animethemes-detail", f"{animethemes._ANIMETHEMES_URL}/anime/shared-anime", {"include": animethemes._DETAIL_INCLUDE},
        ), {"anime": {"slug": "shared-anime", "name": "Catalog Anime", "name_romaji": "Catalog Romaji", "synonyms": ["Trusted Alias"]}})
        self.cache_document("animethemes-detail:malformed", [])
        with db() as connection:
            # Simulate an installed pre-fix database with indistinguishable aliases.
            connection.execute("DELETE FROM request_history_search_migrations")
            connection.executemany(
                "INSERT OR IGNORE INTO request_history_search_aliases (entity_kind, entity_id, search_key) VALUES ('anime', ?, ?)",
                [("shared-anime", "privatealias917"), ("unrecoverable", "oldunverifiedalias")],
            )
            connection.execute(
                "INSERT INTO anime_theme_release_group_links "
                "(anime_slug, anime_name, theme_id, theme_label, theme_type, song_title, release_group_mbid, created_at, updated_at) "
                "VALUES ('mapped-anime', 'Trusted Mapping Name', 2, 'Opening 1', 'OP', 'Song', ?, 0, 0)", (self.album_id,),
            )
            other_aliases = {tuple(row) for row in connection.execute("SELECT * FROM request_history_search_aliases WHERE entity_kind != 'anime'")}
        with patch("backend.services.musicbrainz.get", side_effect=AssertionError("Migration attempted MusicBrainz")), \
             patch("backend.services.animethemes.detail", side_effect=AssertionError("Migration attempted AnimeThemes")):
            init_db()
            self.assertEqual(self.ids(self.search("PrivateAlias917")), set())
            self.assertEqual(self.ids(self.search("My Row Name")), {self.album_id})
            for name in ("Trusted Alias", "Catalog Romaji", "Trusted Mapping Name"):
                self.assertEqual(self.ids(self.search(name)), {self.album_id})
            with db() as connection:
                aliases = {tuple(row) for row in connection.execute("SELECT * FROM request_history_search_aliases")}
                self.assertEqual({row for row in aliases if row[0] != "anime"}, other_aliases)
                self.assertNotIn(("anime", "unrecoverable", "oldunverifiedalias"), aliases)
            with cache_db() as connection:
                connection.execute("DELETE FROM api_cache")
            request_history_search.remember_anime_names({"slug": "shared-anime", "synonyms": ["Later Trusted Alias"]})
            init_db()
            init_db()
            self.assertEqual(self.ids(self.search("Later Trusted Alias")), {self.album_id})
            with db() as connection:
                after = {tuple(row) for row in connection.execute("SELECT * FROM request_history_search_aliases")}
                self.assertEqual(after, aliases | {("anime", "shared-anime", "latertrustedalias"), ("anime", "shared-anime", "sharedanime")})
        self.network.assert_not_called()

    def test_anime_english_and_romanized_aliases_are_captured_without_http(self):
        slug = "boku_no_kokoro_no_yabai_yatsu"
        self.cache_document(cache_key(
            "animethemes-detail", f"{animethemes._ANIMETHEMES_URL}/anime/{slug}", {"include": animethemes._DETAIL_INCLUDE},
        ), {"anime": {"slug": slug, "name": "僕の心のヤバイやつ", "synonyms": [
            {"text": "The Dangers in My Heart"}, {"text": "Boku no Kokoro no Yabai Yatsu"},
        ]}})
        self.add_album(anime_slug=slug, anime_name="僕の心のヤバイやつ")
        with cache_db() as connection:
            connection.execute("DELETE FROM api_cache")
        for query in ("The Dangers in My Heart", "Boku no Kokoro no Yabai Yatsu", "僕の心のヤバイやつ"):
            with self.subTest(query=query):
                self.assertEqual(self.ids(self.search(query)), {self.album_id})

    def test_migration_backfills_only_local_names_and_is_idempotent(self):
        track_search_index.index_artist({"id": self.artist_id, "name": "羊文学", "aliases": [{"name": "Hitsuji Bungaku"}]})
        with db() as connection:
            connection.execute("DROP TABLE request_history_search_entities")
            connection.execute("DROP TABLE request_history_search_aliases")
            connection.execute("DROP TABLE request_history_search_migrations")
            connection.execute(
                "INSERT INTO request_history (user_id, kind, mbid, name, created_at) VALUES (?, 'artist', ?, '羊文学', 0)",
                (self.user_id, self.artist_id),
            )
        init_db()
        init_db()
        with cache_db() as connection:
            connection.execute("DELETE FROM track_search_artist_names")
        self.assertEqual(self.ids(self.search("Hitsuji Bungaku")), {self.artist_id})
        self.assertEqual(self.ids(self.search("羊文学")), {self.artist_id})
        self.network.assert_not_called()

    def test_search_cannot_leak_another_users_history_or_bypass_account_resolution(self):
        self.add_album()
        with db() as connection:
            other = connection.execute(
                "INSERT INTO users (username, password_hash, role, created_at) VALUES ('other-user', 'unused', 'user', 0)",
            ).lastrowid
        record_request(other, "artist", "private-id", "Private Artist")
        record_request(other, "artist", "shared-id", "All Time Low")
        self.assertEqual(self.ids(self.search("All Time Low")), {self.album_id})
        self.assertEqual(self.ids(self.search("Private")), set())
        self.assertEqual(self.ids(self.search("Private", username="other-user")), {"private-id"})
        with db() as connection:
            connection.execute("UPDATE users SET role = 'user' WHERE id = ?", (self.user_id,))
        response = self.client.get("/api/account/profile", query_string={"q": "Private", "username": "other-user"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.ids(self.search("Private")), set())

    def test_search_is_literal_parameterized_and_has_bounded_input(self):
        self.add_album()
        for query in ("%", "_", "' OR 1=1 --", "does not exist"):
            with self.subTest(query=query):
                self.assertEqual(self.search(query)["pagination"]["total"], 0)
        self.assertEqual(self.search("   ")["pagination"]["total"], 1)
        self.assertEqual(self.client.get("/api/account/profile", query_string={"q": "a" * 501}).status_code, 400)

    def test_normalization_and_unicode_character_length_contract(self):
        record_request(self.user_id, "artist", self.artist_id, "Café & O'Neil / A-B_🎵 日本語", search_metadata=({"aliases": ["Romaji Nihongo"]},))
        for query in ("cafe", "Café", "Cafe\u0301", "O'Neil", "o neil", "Café & O'Neil", "A-B", "A_B", "A/B", "a b", "日本語", "Romaji Nihongo", "  cafe   o neil  ", "cafe%", "cafe_", "🎵cafe"):
            with self.subTest(query=query):
                self.assertEqual(self.search(query)["pagination"]["total"], 1)
        for query in ("%", "_", "🎵", "///", "&&&", "---"):
            self.assertEqual(self.search(query)["pagination"]["total"], 0)
        # Python validates Unicode code points, including astral emoji; the
        # browser's native maxlength separately measures UTF-16 code units.
        for character in ("a", "日", "🎵"):
            self.assertEqual(self.client.get("/api/account/profile", query_string={"q": character * 500}).status_code, 200)
            response = self.client.get("/api/account/profile", query_string={"q": character * 501})
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.get_json()["error"], "Search must be 500 characters or fewer.")

    def test_search_uses_only_local_availability_even_with_services_configured(self):
        self.add_album()
        save_service("plex", {"url": "http://unavailable", "token": "token", "librarySectionIds": [1]})
        save_service("lidarr", {"url": "http://unavailable", "apiKey": "key"})
        with patch("backend.services.musicbrainz.get", side_effect=AssertionError("MusicBrainz is unavailable")), \
             patch("backend.services.animethemes.detail", side_effect=AssertionError("AnimeThemes is unavailable")):
            payload = self.search("wrong")
        self.assertEqual(self.ids(payload), {self.album_id})
        self.assertIn("requestStatus", payload["requests"]["release-group"][0])
        self.assertIn("use_for_recommendations", payload["requests"]["release-group"][0])
