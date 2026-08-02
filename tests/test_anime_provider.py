"""Focused AnimeThemes provider and route tests."""

import unittest
from unittest.mock import Mock, patch

import requests
from flask import Flask

from backend.routes.anime import _release_group_mbid, blueprint
from backend.services import anime_musicbrainz, animethemes


ANIME_RESPONSE = {
    "anime": {
        "id": 2028,
        "name": " Naruto ",
        "media_format": "TV",
        "season": "Fall",
        "slug": "naruto",
        "synopsis": " A ninja story. ",
        "year": 2002,
        "images": [
            {"facet": "Small Cover", "link": "https://img/small.avif"},
            {"facet": "Large Cover", "link": "https://img/large.jpg"},
        ],
        "resources": [
            {
                "id": 495,
                "external_id": 20,
                "link": "https://myanimelist.net/anime/20",
                "site": "MyAnimeList",
                "animeresource": {"as": None},
            }
        ],
        "series": [{"id": 118, "name": "Naruto", "slug": "naruto"}],
        "animethemes": [
            {
                "id": 1477,
                "sequence": 2,
                "slug": "OP2",
                "type": "OP",
                "song": {
                    "id": 1477,
                    "title": "Haruka Kanata",
                    "artists": [
                        {
                            "id": 26,
                            "name": "Asian Kung-Fu Generation",
                            "slug": "akfg",
                            "artistsong": {"as": "AKFG"},
                        }
                    ],
                },
                "animethemeentries": [
                    {"episodes": "26-53", "notes": "Version one"},
                    {"episodes": "26-53", "notes": None},
                    {"episodes": None, "notes": "Version two"},
                ],
            }
        ],
    }
}

SERIES_RESPONSE = {
    "series": {
        "id": 312,
        "name": "Boku no Kokoro no Yabai Yatsu",
        "slug": "boku_no_kokoro_no_yabai_yatsu",
        "anime": [
            {
                "id": 4212,
                "name": "Boku no Kokoro no Yabai Yatsu Season 2",
                "media_format": "TV",
                "season": "Winter",
                "slug": "boku_no_kokoro_no_yabai_yatsu_season_2",
                "year": 2024,
                "images": [{
                    "facet": "Large Cover",
                    "link": "https://img/season-2.jpg",
                }],
                "animethemes": [{"id": 1}, {"id": 2}],
            },
            {
                "id": 4025,
                "name": "Boku no Kokoro no Yabai Yatsu",
                "media_format": "TV",
                "season": "Spring",
                "slug": "boku_no_kokoro_no_yabai_yatsu",
                "year": 2023,
                "images": [{
                    "facet": "Small Cover",
                    "link": "https://img/original.avif",
                }],
                "animethemes": [{"id": 3}, {"id": 4}],
            },
            {
                "id": 4939,
                "name": "Boku no Kokoro no Yabai Yatsu Movie",
                "media_format": "Movie",
                "season": "Winter",
                "slug": "boku_no_kokoro_no_yabai_yatsu_movie",
                "year": 2026,
                "images": [],
                "animethemes": [{"id": 5}],
            },
        ],
    }
}


class AnimeThemesServiceTests(unittest.TestCase):
    @patch("backend.services.animethemes.cached_json_get")
    def test_search_returns_normalized_anime_summaries(self, cached_get):
        cached_get.return_value = {
            "search": {
                "anime": [
                    {
                        "id": 2028,
                        "name": "Naruto",
                        "media_format": "TV",
                        "season": "Fall",
                        "slug": "naruto",
                        "synopsis": "A ninja story.",
                        "year": 2002,
                    },
                    None,
                    {"id": 1, "name": "Missing slug"},
                ]
            }
        }

        results = animethemes.search("  Naruto  ", limit=10)

        self.assertEqual(results, [{
            "id": 2028,
            "slug": "naruto",
            "name": "Naruto",
            "year": 2002,
            "season": "Fall",
            "format": "TV",
            "synopsis": "A ninja story.",
            "coverArt": "",
        }])
        cached_get.assert_called_once_with(
            "https://api.animethemes.moe/search",
            params={
                "q": "Naruto",
                "limit": 10,
                "include[anime]": "images",
            },
            headers={"User-Agent": animethemes.USER_AGENT},
            namespace="animethemes-search",
            ttl=animethemes._SEARCH_CACHE_TTL,
        )

    @patch("backend.services.animethemes.cached_json_get")
    def test_detail_normalizes_themes_artists_entries_and_relations(self, cached_get):
        cached_get.return_value = ANIME_RESPONSE

        result = animethemes.detail("naruto")

        self.assertEqual(result, {
            "id": 2028,
            "slug": "naruto",
            "name": "Naruto",
            "year": 2002,
            "season": "Fall",
            "format": "TV",
            "synopsis": "A ninja story.",
            "coverArt": "https://img/large.jpg",
            "resources": [{
                "id": 495,
                "site": "MyAnimeList",
                "link": "https://myanimelist.net/anime/20",
                "externalId": 20,
            }],
            "series": [{"id": 118, "name": "Naruto", "slug": "naruto"}],
            "themes": [{
                "id": 1477,
                "type": "OP",
                "sequence": 2,
                "label": "Opening 2",
                "slug": "OP2",
                "song": {
                    "id": 1477,
                    "title": "Haruka Kanata",
                    "artists": [{
                        "id": 26,
                        "name": "Asian Kung-Fu Generation",
                        "slug": "akfg",
                        "as": "AKFG",
                    }],
                },
                "episodes": ["26-53"],
                "notes": ["Version one", "Version two"],
            }],
        })
        cached_get.assert_called_once_with(
            "https://api.animethemes.moe/anime/naruto",
            params={"include": animethemes._DETAIL_INCLUDE},
            headers={"User-Agent": animethemes.USER_AGENT},
            namespace="animethemes-detail",
            ttl=animethemes._DETAIL_CACHE_TTL,
        )

    @patch("backend.services.animethemes.cached_json_get")
    def test_detail_returns_none_when_upstream_has_no_anime(self, cached_get):
        cached_get.return_value = {"anime": None}
        self.assertIsNone(animethemes.detail("not_here"))

    @patch("backend.services.animethemes.cached_json_get")
    def test_series_detail_normalizes_and_orders_related_anime(self, cached_get):
        cached_get.return_value = SERIES_RESPONSE

        result = animethemes.series_detail("boku_no_kokoro_no_yabai_yatsu")

        self.assertEqual(result["id"], 312)
        self.assertEqual(result["slug"], "boku_no_kokoro_no_yabai_yatsu")
        self.assertEqual(
            [anime["slug"] for anime in result["anime"]],
            [
                "boku_no_kokoro_no_yabai_yatsu",
                "boku_no_kokoro_no_yabai_yatsu_season_2",
                "boku_no_kokoro_no_yabai_yatsu_movie",
            ],
        )
        self.assertEqual(
            [anime["themeCount"] for anime in result["anime"]],
            [2, 2, 1],
        )
        self.assertEqual(result["anime"][0]["coverArt"], "https://img/original.avif")
        cached_get.assert_called_once_with(
            "https://api.animethemes.moe/series/boku_no_kokoro_no_yabai_yatsu",
            params={"include": animethemes._SERIES_INCLUDE},
            headers={"User-Agent": animethemes.USER_AGENT},
            namespace="animethemes-series",
            ttl=animethemes._SERIES_CACHE_TTL,
        )

    @patch("backend.services.animethemes.cached_json_get")
    def test_series_detail_returns_none_without_a_series(self, cached_get):
        cached_get.return_value = {"series": None}
        self.assertIsNone(animethemes.series_detail("not_here"))

    @patch("backend.services.animethemes.cached_json_get")
    def test_search_enforces_the_requested_limit_locally(self, cached_get):
        cached_get.return_value = {"search": {"anime": [
            {"id": index, "name": f"Anime {index}", "slug": f"anime_{index}"}
            for index in range(3)
        ]}}

        results = animethemes.search("Anime", limit=2)

        self.assertEqual([result["id"] for result in results], [0, 1])

    @patch("backend.services.animethemes.cached_json_get")
    def test_inputs_are_validated_before_an_external_request(self, cached_get):
        invalid_calls = (
            lambda: animethemes.search("x"),
            lambda: animethemes.search("Naruto", limit=0),
            lambda: animethemes.detail("../naruto"),
            lambda: animethemes.series_detail("../naruto"),
        )
        for call in invalid_calls:
            with self.subTest(call=call), self.assertRaises(ValueError):
                call()
        cached_get.assert_not_called()


class AnimeRouteTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True, SECRET_KEY="test-secret")
        self.app.register_blueprint(blueprint)
        self.client = self.app.test_client()
        library_availability = patch(
            "backend.routes.anime.lidarr.cached_library_availability",
            return_value={},
        )
        library_availability.start()
        self.addCleanup(library_availability.stop)
        registry_mapping = patch(
            "backend.routes.anime.anime_mapping_registry.get_mapping",
            return_value=None,
        )
        self.registry_mapping = registry_mapping.start()
        self.addCleanup(registry_mapping.stop)

    def _get(self, path):
        with patch("backend.security.current_user", return_value={"id": 1}):
            return self.client.get(path)

    def _admin_request(self, method, path, **kwargs):
        with patch(
            "backend.security.current_user",
            return_value={"id": 1, "role": "admin"},
        ):
            return self.client.open(path, method=method, **kwargs)

    def test_release_group_parser_accepts_a_url_or_raw_mbid(self):
        mbid = "6259b4f8-39b2-4b46-98e0-5dd433630abc"
        self.assertEqual(_release_group_mbid(mbid), mbid)
        self.assertEqual(
            _release_group_mbid(
                f"https://www.musicbrainz.org/release-group/{mbid}/"
            ),
            mbid,
        )

    def test_detail_requires_login(self):
        response = self.client.get("/api/anime/naruto")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json(), {"error": "Sign in is required."})

        series_response = self.client.get("/api/series/naruto")
        self.assertEqual(series_response.status_code, 401)

    @patch("backend.routes.anime.animethemes.detail")
    def test_detail_returns_normalized_provider_payload(self, detail):
        detail.return_value = {"id": 2028, "slug": "naruto", "themes": []}
        response = self._get("/api/anime/naruto")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json(),
            {"id": 2028, "slug": "naruto", "themes": []},
        )

    @patch("backend.routes.anime.animethemes.series_detail")
    def test_series_detail_returns_related_anime(self, series_detail):
        series_detail.return_value = {
            "id": 312,
            "name": "Boku no Kokoro no Yabai Yatsu",
            "slug": "boku_no_kokoro_no_yabai_yatsu",
            "anime": [{
                "slug": "boku_no_kokoro_no_yabai_yatsu",
                "name": "Boku no Kokoro no Yabai Yatsu",
                "themeCount": 2,
            }],
        }

        response = self._get("/api/series/boku_no_kokoro_no_yabai_yatsu")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["anime"][0]["themeCount"], 2)

    @patch("backend.routes.anime.animethemes.series_detail")
    def test_series_detail_returns_404_when_missing(self, series_detail):
        series_detail.return_value = None

        response = self._get("/api/series/unknown")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            response.get_json(),
            {"error": "Series was not found on AnimeThemes."},
        )

    def test_series_detail_rejects_an_invalid_slug(self):
        response = self._get("/api/series/bad.slug")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.get_json(),
            {"error": "Invalid AnimeThemes series slug."},
        )

    def test_manual_mapping_requires_an_administrator(self):
        path = "/api/anime/naruto/themes/1477/mapping"
        response = self.client.put(path, json={"releaseGroup": "group"})
        self.assertEqual(response.status_code, 401)

        with patch(
            "backend.security.current_user",
            return_value={"id": 2, "role": "user"},
        ):
            response = self.client.put(path, json={"releaseGroup": "group"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.get_json(),
            {"error": "Administrator access is required."},
        )

    @patch("backend.routes.anime.anime_musicbrainz.registered_mapping")
    @patch("backend.routes.anime.anime_mapping_registry.upsert_mapping")
    @patch("backend.routes.anime.musicbrainz.get")
    @patch("backend.routes.anime.animethemes.detail")
    def test_admin_can_link_a_hydrated_release_group(
        self, detail, musicbrainz_get, upsert_mapping, registered_mapping
    ):
        mbid = "6259b4f8-39b2-4b46-98e0-5dd433630abc"
        detail.return_value = {
            "slug": "boku_no_kokoro_no_yabai_yatsu",
            "themes": [{
                "id": 1477,
                "song": {
                    "id": 1477,
                    "title": "Shayou",
                    "artists": [{"name": "Yorushika", "as": None}],
                },
            }],
        }
        musicbrainz_get.return_value = {
            "id": mbid,
            "title": "斜陽",
            "primary-type": "Single",
            "first-release-date": "2023-05-08",
            "artist-credit": [{
                "name": "ヨルシカ",
                "artist": {"id": "ef4d3c4f-e2d3-4f11-bcdc-e47c601247bb"},
            }],
        }
        upsert_mapping.return_value = {
            "songId": 1477,
            "title": "Shayou",
            "artists": ["Yorushika"],
            "status": "confirmed",
            "provenance": "manual",
            "scope": "unknown",
            "targets": [{
                "releaseGroupId": mbid,
                "recordingIds": [],
                "artistIds": ["ef4d3c4f-e2d3-4f11-bcdc-e47c601247bb"],
                "releaseGroupTitle": "斜陽",
                "artistName": "ヨルシカ",
                "primaryType": "Single",
                "firstReleaseDate": "2023-05-08",
                "preferred": True,
            }],
            "preferredTarget": {"releaseGroupId": mbid},
        }
        registered_mapping.return_value = {
            "state": "resolved",
            "sourceSongId": "1477",
            "songTitle": "Shayou",
            "sourceArtists": ["Yorushika"],
            "matchMethod": "local-registry",
            "mappingSource": "local",
            "registryStatus": "confirmed",
            "registryProvenance": "manual",
            "mappingScope": "unknown",
            "releaseGroups": [{
                "id": mbid,
                "title": "斜陽",
                "artist": "ヨルシカ",
                "date": "2023-05-08",
                "type": "Single",
            }],
        }

        response = self._admin_request(
            "PUT",
            "/api/anime/boku_no_kokoro_no_yabai_yatsu/themes/1477/mapping",
            json={"releaseGroup": f"https://musicbrainz.org/release-group/{mbid}"},
        )

        self.assertEqual(response.status_code, 200)
        mapping = response.get_json()["mapping"]
        self.assertEqual(mapping["mappingSource"], "local")
        self.assertEqual(mapping["registryProvenance"], "manual")
        self.assertEqual(mapping["releaseGroups"][0]["id"], mbid)
        musicbrainz_get.assert_called_once_with(
            f"/release-group/{mbid}",
            "aliases+artist-credits",
            priority="critical",
        )
        upsert_mapping.assert_called_once_with(
            1477,
            title="Shayou",
            artists=["Yorushika"],
            status="confirmed",
            provenance="manual",
            scope="unknown",
            targets=[{
                "releaseGroupId": mbid,
                "recordingIds": [],
                "artistIds": ["ef4d3c4f-e2d3-4f11-bcdc-e47c601247bb"],
                "releaseGroupTitle": "斜陽",
                "artistName": "ヨルシカ",
                "primaryType": "Single",
                "firstReleaseDate": "2023-05-08",
                "preferred": True,
            }],
            preferred_release_group_mbid=mbid,
        )

    @patch("backend.routes.anime.anime_musicbrainz.registered_mapping")
    @patch("backend.routes.anime.anime_mapping_registry.upsert_mapping")
    @patch("backend.routes.anime.anime_musicbrainz.cached_mapping")
    @patch("backend.routes.anime.musicbrainz.get")
    @patch("backend.routes.anime.animethemes.detail")
    def test_admin_can_confirm_only_the_recommended_automatic_recording_match(
        self,
        detail,
        musicbrainz_get,
        cached_mapping,
        upsert_mapping,
        registered_mapping,
    ):
        group_id = "6259b4f8-39b2-4b46-98e0-5dd433630abc"
        recording_id = "1e9b7625-9184-493b-be13-b0b6d12040c9"
        artist_id = "cb9266b4-8537-4687-8763-5129c583be53"
        theme = {
            "id": 1477,
            "song": {
                "id": 1477,
                "title": "Redo",
                "artists": [{"name": "Konomi Suzuki"}],
            },
        }
        detail.return_value = {"slug": "rezero", "themes": [theme]}
        cached_mapping.return_value = {
            "state": "resolved",
            "matchMethod": "recording-search",
            "recordingId": recording_id,
            "artistIds": [artist_id],
            "releaseGroups": [
                {
                    "id": group_id,
                    "name": "Redo",
                    "artist": "鈴木このみ",
                    "type": "Single",
                    "date": "2016-05-11",
                },
                {
                    "id": "11111111-2222-4333-8444-555555555555",
                    "name": "lead",
                    "artist": "鈴木このみ",
                    "type": "Album",
                    "date": "2017-03-08",
                },
            ],
        }
        registered_mapping.return_value = {
            "state": "resolved",
            "mappingSource": "local",
            "registryStatus": "confirmed",
            "registryProvenance": "manual-confirmation",
            "recordingId": recording_id,
            "releaseGroups": [{"id": group_id, "name": "Redo"}],
        }

        response = self._admin_request(
            "PUT",
            "/api/anime/rezero/themes/1477/mapping",
            json={"confirmAutomatic": True, "releaseGroup": group_id},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json()["message"],
            "Recommended automatic match confirmed.",
        )
        self.assertEqual(
            [group["id"] for group in response.get_json()["mapping"]["releaseGroups"]],
            [group_id],
        )
        musicbrainz_get.assert_not_called()
        upsert_mapping.assert_called_once_with(
            1477,
            title="Redo",
            artists=["Konomi Suzuki"],
            status="confirmed",
            provenance="manual-confirmation",
            scope="unknown",
            targets=[{
                "releaseGroupId": group_id,
                "recordingIds": [recording_id],
                "artistIds": [artist_id],
                "releaseGroupTitle": "Redo",
                "artistName": "鈴木このみ",
                "primaryType": "Single",
                "firstReleaseDate": "2016-05-11",
                "scope": "unknown",
                "preferred": True,
            }],
            preferred_release_group_mbid=group_id,
        )

    @patch("backend.routes.anime.anime_musicbrainz.registered_mapping")
    @patch("backend.routes.anime.anime_mapping_registry.upsert_mapping")
    @patch("backend.routes.anime.anime_musicbrainz.cached_mapping")
    @patch("backend.routes.anime.musicbrainz.get")
    @patch("backend.routes.anime.animethemes.detail")
    def test_admin_can_confirm_an_automatic_artist_title_match_without_recording(
        self,
        detail,
        musicbrainz_get,
        cached_mapping,
        upsert_mapping,
        registered_mapping,
    ):
        group_id = "e8aa3f4b-4148-4af6-b380-928be9541e79"
        artist_id = "e449ea14-f6b3-4a81-9c38-4a31630503cc"
        theme = {
            "id": 921,
            "song": {
                "id": 3096,
                "title": "Gajumaru ~Heaven in the Rain~",
                "artists": [{"name": "ReoNa"}],
            },
        }
        detail.return_value = {"slug": "anime", "themes": [theme]}
        cached_mapping.return_value = {
            "state": "resolved",
            "matchMethod": "artist-discography-title",
            "recordingId": "",
            "artistIds": [artist_id],
            "releaseGroups": [{
                "id": group_id,
                "name": "ガジュマル～Heaven in the Rain～",
                "artist": "ReoNa",
                "type": "Single",
                "date": "2024-01-08",
                "mappingScope": "commercial_full",
            }],
        }
        registered_mapping.return_value = {
            "state": "resolved",
            "mappingSource": "local",
            "registryStatus": "confirmed",
            "registryProvenance": "manual-confirmation",
            "recordingId": "",
            "releaseGroups": [{"id": group_id, "name": "ガジュマル"}],
        }

        response = self._admin_request(
            "PUT",
            "/api/anime/anime/themes/921/mapping",
            json={"confirmAutomatic": True, "releaseGroup": group_id},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [group["id"] for group in response.get_json()["mapping"]["releaseGroups"]],
            [group_id],
        )
        musicbrainz_get.assert_not_called()
        upsert_mapping.assert_called_once_with(
            3096,
            title="Gajumaru ~Heaven in the Rain~",
            artists=["ReoNa"],
            status="confirmed",
            provenance="manual-confirmation",
            scope="unknown",
            targets=[{
                "releaseGroupId": group_id,
                "recordingIds": [],
                "artistIds": [artist_id],
                "releaseGroupTitle": "ガジュマル～Heaven in the Rain～",
                "artistName": "ReoNa",
                "primaryType": "Single",
                "firstReleaseDate": "2024-01-08",
                "scope": "commercial_full",
                "preferred": True,
            }],
            preferred_release_group_mbid=group_id,
        )

    @patch("backend.routes.anime.anime_mapping_registry.upsert_mapping")
    @patch("backend.routes.anime.anime_musicbrainz.cached_mapping")
    @patch("backend.routes.anime.animethemes.detail")
    def test_automatic_confirmation_requires_a_current_supported_match(
        self, detail, cached_mapping, upsert_mapping
    ):
        detail.return_value = {
            "slug": "anime",
            "themes": [{
                "id": 1,
                "song": {
                    "id": 9,
                    "title": "Song",
                    "artists": [{"name": "Artist"}],
                },
            }],
        }
        cached_mapping.return_value = None

        response = self._admin_request(
            "PUT",
            "/api/anime/anime/themes/1/mapping",
            json={"confirmAutomatic": True},
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("no longer available", response.get_json()["error"])
        upsert_mapping.assert_not_called()

    @patch("backend.routes.anime.anime_mapping_registry.upsert_mapping")
    @patch("backend.routes.anime.musicbrainz.get")
    @patch("backend.routes.anime.animethemes.detail")
    def test_manual_mapping_rejects_invalid_or_wrong_musicbrainz_links(
        self, detail, musicbrainz_get, upsert_mapping
    ):
        detail.return_value = {
            "slug": "anime",
            "themes": [{
                "id": 1,
                "song": {"id": 9, "title": "Song", "artists": [{"name": "Artist"}]},
            }],
        }
        bad_values = (
            "not-a-uuid",
            "https://example.com/release-group/6259b4f8-39b2-4b46-98e0-5dd433630abc",
            "http://musicbrainz.org/release-group/6259b4f8-39b2-4b46-98e0-5dd433630abc",
            "https://musicbrainz.org/release/6259b4f8-39b2-4b46-98e0-5dd433630abc",
        )
        for value in bad_values:
            with self.subTest(value=value):
                response = self._admin_request(
                    "PUT",
                    "/api/anime/anime/themes/1/mapping",
                    json={"releaseGroup": value},
                )
                self.assertEqual(response.status_code, 400)
        musicbrainz_get.assert_not_called()
        upsert_mapping.assert_not_called()

    @patch("backend.routes.anime.anime_mapping_registry.upsert_mapping")
    @patch("backend.routes.anime.musicbrainz.get")
    @patch("backend.routes.anime.animethemes.detail")
    def test_manual_mapping_returns_404_for_an_unknown_release_group(
        self, detail, musicbrainz_get, upsert_mapping
    ):
        detail.return_value = {
            "slug": "anime",
            "themes": [{
                "id": 1,
                "song": {"id": 9, "title": "Song", "artists": [{"name": "Artist"}]},
            }],
        }
        missing = Mock(status_code=404)
        musicbrainz_get.side_effect = requests.HTTPError(response=missing)

        response = self._admin_request(
            "PUT",
            "/api/anime/anime/themes/1/mapping",
            json={
                "releaseGroup": "6259b4f8-39b2-4b46-98e0-5dd433630abc"
            },
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            response.get_json(),
            {"error": "MusicBrainz release group was not found."},
        )
        upsert_mapping.assert_not_called()

    @patch("backend.routes.anime.anime_mapping_registry.get_mapping")
    @patch("backend.routes.anime.anime_mapping_registry.delete_mapping")
    @patch("backend.routes.anime.anime_metadata_worker.mappings_for")
    @patch("backend.routes.anime.animethemes.detail")
    def test_admin_can_unlink_a_local_mapping(
        self, detail, mappings_for, delete_mapping, get_mapping
    ):
        theme = {
            "id": 1477,
            "song": {"id": 1477, "title": "Shayou", "artists": [{"name": "Yorushika"}]},
        }
        detail.return_value = {"slug": "anime", "themes": [theme]}
        get_mapping.return_value = {"provenance": "manual"}
        mappings_for.return_value = {}

        response = self._admin_request(
            "DELETE",
            "/api/anime/anime/themes/1477/mapping",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["mapping"]["status"], "pending")
        delete_mapping.assert_called_once_with(1477)

    @patch("backend.routes.anime.anime_mapping_registry.get_mapping")
    @patch("backend.routes.anime.animethemes.detail")
    def test_unlink_returns_404_without_a_local_mapping(
        self, detail, get_mapping
    ):
        detail.return_value = {
            "slug": "anime",
            "themes": [{"id": 1, "song": {"id": 9, "title": "Song"}}],
        }
        get_mapping.return_value = None

        response = self._admin_request(
            "DELETE",
            "/api/anime/anime/themes/1/mapping",
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            response.get_json(),
            {"error": "This theme does not have a local mapping."},
        )

    @patch("backend.routes.anime.anime_mapping_registry.reject_mapping")
    @patch("backend.routes.anime.anime_mapping_registry.get_mapping")
    @patch("backend.routes.anime.anime_metadata_worker.mappings_for")
    @patch("backend.routes.anime.animethemes.detail")
    def test_unlinking_a_builtin_seed_creates_a_durable_rejection(
        self, detail, mappings_for, get_mapping, reject_mapping
    ):
        theme = {
            "id": 1477,
            "song": {
                "id": 1477,
                "title": "Shayou",
                "artists": [{"name": "Yorushika"}],
            },
        }
        detail.return_value = {"slug": "anime", "themes": [theme]}
        get_mapping.return_value = {"provenance": "builtin-seed"}
        mappings_for.return_value = {
            anime_musicbrainz.theme_mapping_key(theme): {
                "state": "unmatched",
                "reason": "registry-rejected",
                "releaseGroups": [],
            }
        }

        response = self._admin_request(
            "DELETE",
            "/api/anime/anime/themes/1477/mapping",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["mapping"]["status"], "unmatched")
        self.assertIn("suppressed", response.get_json()["message"].lower())
        reject_mapping.assert_called_once_with(
            1477,
            title="Shayou",
            artists=["Yorushika"],
            provenance="manual-rejection",
        )

    @patch("backend.routes.anime.anime_musicbrainz.theme_mapping_key")
    @patch("backend.routes.anime.anime_metadata_worker.mappings_for")
    @patch("backend.routes.anime.animethemes.detail")
    def test_detail_attaches_cached_theme_mappings(
        self, detail, mappings_for, mapping_key
    ):
        theme = {"id": 1477, "song": {"id": 1477, "title": "Haruka Kanata"}}
        detail.return_value = {
            "id": 2028,
            "slug": "naruto",
            "themes": [theme],
        }
        mapping_key.return_value = "theme-key"
        mappings_for.return_value = {
            "theme-key": {
                "state": "resolved",
                "releaseGroups": [{"id": "group-id", "title": "Single"}],
            }
        }

        response = self._get("/api/anime/naruto")

        mapping = response.get_json()["themes"][0]["mapping"]
        self.assertEqual(mapping["status"], "resolved")
        self.assertEqual(mapping["releaseGroups"][0]["name"], "Single")

    @patch("backend.routes.anime.lidarr.cached_library_availability")
    @patch("backend.routes.anime.anime_musicbrainz.theme_mapping_key")
    @patch("backend.routes.anime.anime_metadata_worker.mappings_for")
    @patch("backend.routes.anime.animethemes.detail")
    def test_detail_enriches_musicbrainz_groups_with_lidarr_availability(
        self, detail, mappings_for, mapping_key, library_availability
    ):
        theme = {"id": 1477, "song": {"id": 1477, "title": "Shayou"}}
        detail.return_value = {
            "id": 4025,
            "slug": "boku_no_kokoro_no_yabai_yatsu",
            "themes": [theme],
        }
        mapping_key.return_value = "theme-key"
        mappings_for.return_value = {
            "theme-key": {
                "state": "resolved",
                "releaseGroups": [{
                    "id": "6259b4f8-39b2-4b46-98e0-5dd433630abc",
                    "title": "斜陽",
                }],
            }
        }
        library_availability.return_value = {
            "6259b4f8-39b2-4b46-98e0-5dd433630abc": {
                "fullyAvailable": True,
                "trackFileCount": 3,
                "totalTrackCount": 3,
            }
        }

        response = self._get("/api/anime/boku_no_kokoro_no_yabai_yatsu")

        group = response.get_json()["themes"][0]["mapping"]["releaseGroups"][0]
        self.assertTrue(group["availableInLidarr"])
        self.assertTrue(group["fullyAvailableInLidarr"])

    @patch("backend.routes.anime.lidarr.cached_library_availability")
    @patch("backend.routes.anime.anime_musicbrainz.theme_mapping_key")
    @patch("backend.routes.anime.anime_metadata_worker.mappings_for")
    @patch("backend.routes.anime.animethemes.detail")
    def test_detail_marks_tracked_incomplete_groups_for_search_missing(
        self, detail, mappings_for, mapping_key, library_availability
    ):
        theme = {"id": 2, "song": {"id": 2, "title": "Ending"}}
        detail.return_value = {"slug": "anime", "themes": [theme]}
        mapping_key.return_value = "theme-key"
        mappings_for.return_value = {
            "theme-key": {
                "state": "ambiguous",
                "recordingCandidates": [{
                    "releaseGroups": [{"id": "group-id", "title": "Single"}],
                }],
            }
        }
        library_availability.return_value = {
            "group-id": {"fullyAvailable": False}
        }

        response = self._get("/api/anime/anime")

        candidate = response.get_json()["themes"][0]["mapping"]["candidates"][0]
        self.assertTrue(candidate["availableInLidarr"])
        self.assertFalse(candidate["fullyAvailableInLidarr"])
        nested = response.get_json()["themes"][0]["mapping"][
            "recordingCandidates"
        ][0]["releaseGroups"][0]
        self.assertTrue(nested["availableInLidarr"])
        self.assertFalse(nested["fullyAvailableInLidarr"])

    @patch("backend.routes.anime.anime_musicbrainz.theme_mapping_key")
    @patch("backend.routes.anime.anime_metadata_worker.request_resolution")
    @patch("backend.routes.anime.animethemes.detail")
    def test_resolution_request_returns_theme_id_keyed_progress(
        self, detail, request_resolution, mapping_key
    ):
        theme = {"id": 1477, "song": {"id": 1477, "title": "Haruka Kanata"}}
        detail.return_value = {"slug": "naruto", "themes": [theme]}
        mapping_key.return_value = "theme-key"
        request_resolution.return_value = {
            "status": "pending",
            "polling": True,
            "mappings": {"theme-key": {"state": "pending"}},
        }

        with patch("backend.security.current_user", return_value={"id": 1}):
            response = self.client.post("/api/anime/naruto/resolve", json={})

        self.assertEqual(response.status_code, 202)
        payload = response.get_json()
        self.assertEqual(payload["mappings"]["1477"]["status"], "pending")
        request_resolution.assert_called_once_with("naruto", [theme])

    @patch("backend.routes.anime.animethemes.detail")
    def test_detail_returns_404_for_missing_anime(self, detail):
        detail.return_value = None
        response = self._get("/api/anime/unknown")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            response.get_json(),
            {"error": "Anime was not found on AnimeThemes."},
        )

    @patch("backend.routes.anime.animethemes.detail")
    def test_detail_preserves_upstream_404(self, detail):
        upstream_response = Mock(status_code=404)
        detail.side_effect = requests.HTTPError(response=upstream_response)
        response = self._get("/api/anime/unknown")
        self.assertEqual(response.status_code, 404)

    @patch("backend.routes.anime.animethemes.detail")
    def test_detail_returns_502_for_upstream_failure(self, detail):
        detail.side_effect = requests.ConnectionError("offline")
        response = self._get("/api/anime/naruto")
        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.get_json(),
            {"error": "AnimeThemes could not load this anime."},
        )

    def test_detail_rejects_an_invalid_slug(self):
        response = self._get("/api/anime/bad.slug")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.get_json(),
            {"error": "Invalid AnimeThemes anime slug."},
        )


if __name__ == "__main__":
    unittest.main()
