"""Focused AnimeThemes provider and route tests."""

if __package__:
    from ._test_environment import TEST_ROOT
else:  # Support direct execution: python tests/test_anime_provider.py
    from _test_environment import TEST_ROOT

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
        theme_link_sync = patch(
            "backend.routes.anime.anime_theme_links.sync_anime_theme_mapping"
        )
        self.theme_link_sync = theme_link_sync.start()
        self.addCleanup(theme_link_sync.stop)

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

    @patch("backend.routes.anime.anime_mapping_registry.submit_mapping_proposal")
    @patch("backend.routes.anime.musicbrainz.get")
    @patch("backend.routes.anime.animethemes.detail")
    def test_user_can_submit_a_verified_override_without_updating_registry(
        self, detail, musicbrainz_get, submit_proposal
    ):
        mbid = "6259b4f8-39b2-4b46-98e0-5dd433630abc"
        theme = {
            "id": 1477,
            "label": "Opening 1",
            "song": {
                "id": 2451,
                "title": "Shayou",
                "artists": [{"name": "Yorushika"}],
            },
        }
        detail.return_value = {
            "slug": "anime",
            "name": "Anime",
            "themes": [theme],
        }
        musicbrainz_get.return_value = {
            "id": mbid,
            "title": "斜陽",
            "primary-type": "Single",
            "first-release-date": "2023-05-08",
            "artist-credit": [{
                "name": "ヨルシカ",
                "artist": {"id": "cb9266b4-8537-4687-8763-5129c583be53"},
            }],
        }
        submit_proposal.return_value = {
            "id": 7,
            "status": "pending",
            "releaseGroupMbid": mbid,
        }
        user = {"id": 2, "role": "user"}

        with patch("backend.security.current_user", return_value=user), patch(
            "backend.routes.anime.current_user", return_value=user
        ):
            response = self.client.post(
                "/api/anime/anime/themes/1477/mapping-proposals",
                json={"releaseGroup": f"https://musicbrainz.org/release-group/{mbid}"},
            )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["proposal"]["status"], "pending")
        submit_proposal.assert_called_once()
        call = submit_proposal.call_args
        self.assertEqual(call.args, (2,))
        self.assertEqual(call.kwargs["anime_slug"], "anime")
        self.assertEqual(call.kwargs["theme_id"], 1477)
        self.assertEqual(call.kwargs["song_id"], 2451)
        self.assertEqual(call.kwargs["target"]["primaryType"], "Single")

    @patch("backend.routes.anime.anime_mapping_registry.mapping_proposals_for_anime")
    @patch("backend.routes.anime.anime_musicbrainz.registered_mapping")
    @patch("backend.routes.anime.anime_mapping_registry.approve_mapping_proposal")
    @patch("backend.routes.anime.anime_mapping_registry.get_mapping_proposal")
    def test_admin_approval_returns_public_mapping_and_cleared_review_queue(
        self, get_proposal, approve_proposal, registered_mapping, proposals_for_anime
    ):
        group_id = "6259b4f8-39b2-4b46-98e0-5dd433630abc"
        proposal = {
            "id": 7,
            "userId": 2,
            "animeSlug": "anime",
            "animeName": "Anime",
            "themeId": 1477,
            "themeLabel": "Opening 1",
            "songId": 2451,
            "songTitle": "Shayou",
            "artists": ["Yorushika"],
            "status": "pending",
        }
        get_proposal.return_value = proposal
        approve_proposal.return_value = {**proposal, "status": "approved"}
        registered_mapping.return_value = {
            "state": "resolved",
            "mappingSource": "local",
            "releaseGroups": [{"id": group_id, "name": "斜陽"}],
        }
        proposals_for_anime.return_value = []
        admin = {"id": 1, "role": "admin"}

        with patch("backend.routes.anime.current_user", return_value=admin):
            response = self._admin_request(
                "POST",
                "/api/anime/anime/themes/1477/mapping-proposals/7/approve",
            )

        self.assertEqual(response.status_code, 200)
        mapping = response.get_json()["mapping"]
        self.assertEqual(mapping["status"], "resolved")
        self.assertEqual(mapping["releaseGroups"][0]["id"], group_id)
        self.assertEqual(mapping["proposals"], [])
        self.assertEqual(response.get_json()["proposals"], [])
        approve_proposal.assert_called_once_with(7, 1)
        self.theme_link_sync.assert_called_once()
        self.assertEqual(self.theme_link_sync.call_args.args[0]["slug"], "anime")
        self.assertEqual(self.theme_link_sync.call_args.args[1]["id"], 1477)
        self.assertEqual(
            self.theme_link_sync.call_args.args[2]["releaseGroups"][0]["id"],
            group_id,
        )

    @patch("backend.routes.anime.anime_mapping_registry.mapping_proposals_for_anime")
    @patch("backend.routes.anime.anime_metadata_worker.mappings_for")
    @patch("backend.routes.anime.anime_mapping_registry.reject_mapping_proposal")
    @patch("backend.routes.anime.anime_mapping_registry.get_mapping_proposal")
    def test_admin_rejection_returns_mapping_with_cleared_review_queue(
        self, get_proposal, reject_proposal, mappings_for, proposals_for_anime
    ):
        proposal = {
            "id": 7,
            "userId": 2,
            "animeSlug": "anime",
            "animeName": "Anime",
            "themeId": 1477,
            "themeLabel": "Opening 1",
            "songId": 2451,
            "songTitle": "Shayou",
            "artists": ["Yorushika"],
            "status": "pending",
        }
        get_proposal.return_value = proposal
        reject_proposal.return_value = {**proposal, "status": "rejected"}
        mappings_for.return_value = {}
        proposals_for_anime.return_value = []
        admin = {"id": 1, "role": "admin"}

        with patch("backend.routes.anime.current_user", return_value=admin):
            response = self._admin_request(
                "DELETE",
                "/api/anime/anime/themes/1477/mapping-proposals/7",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["mapping"]["proposals"], [])
        self.assertEqual(response.get_json()["proposals"], [])
        self.assertEqual(response.get_json()["proposal"]["status"], "rejected")
        reject_proposal.assert_called_once_with(7, 1)
        self.theme_link_sync.assert_called_once()
        self.assertEqual(self.theme_link_sync.call_args.args[0]["slug"], "anime")
        self.assertEqual(self.theme_link_sync.call_args.args[1]["id"], 1477)

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

    @patch("backend.routes.anime.anime_theme_links.sync_anime_theme_mapping")
    @patch("backend.routes.anime.anime_musicbrainz.registered_mapping")
    @patch("backend.routes.anime.anime_mapping_registry.upsert_mapping")
    @patch("backend.routes.anime.anime_musicbrainz.cached_mapping")
    @patch("backend.routes.anime.musicbrainz.get")
    @patch("backend.routes.anime.animethemes.detail")
    def test_admin_can_confirm_an_explicit_ambiguous_automatic_candidate(
        self,
        detail,
        musicbrainz_get,
        cached_mapping,
        upsert_mapping,
        registered_mapping,
        sync_mapping,
    ):
        first_group = "6259b4f8-39b2-4b46-98e0-5dd433630abc"
        selected_group = "11111111-2222-4333-8444-555555555555"
        selected_recording = "1e9b7625-9184-493b-be13-b0b6d12040c9"
        selected_artist = "cb9266b4-8537-4687-8763-5129c583be53"
        theme = {
            "id": 1477,
            "label": "Opening 4",
            "type": "OP",
            "sequence": 4,
            "song": {
                "id": 2451,
                "title": "GO!!!",
                "artists": [{"name": "Unknown Artist"}],
            },
        }
        anime = {"slug": "naruto", "name": "Naruto", "themes": [theme]}
        detail.return_value = anime
        cached_mapping.return_value = {
            "state": "ambiguous",
            "reason": "missing-artist",
            "matchMethod": "title-only-recording-search",
            "recordingId": "",
            "artistIds": [],
            "releaseGroups": [
                {
                    "id": first_group,
                    "name": "GO!!!",
                    "artist": "Other artist",
                    "type": "Album",
                    "date": "2005-01-01",
                },
                {
                    "id": selected_group,
                    "name": "GO!!!",
                    "artist": "FLOW",
                    "type": "Single",
                    "date": "2004-04-28",
                    "mappingScope": "commercial_full",
                },
            ],
            "recordingCandidates": [{
                "recordingId": selected_recording,
                "recordingTitle": "GO!!!",
                "artist": "FLOW",
                "artistIds": [selected_artist],
                "releaseGroups": [{"id": selected_group, "name": "GO!!!"}],
            }],
        }
        registered_mapping.return_value = {
            "state": "resolved",
            "mappingSource": "local",
            "registryStatus": "confirmed",
            "registryProvenance": "manual-confirmation",
            "releaseGroups": [{"id": selected_group, "name": "GO!!!"}],
        }

        response = self._admin_request(
            "PUT",
            "/api/anime/naruto/themes/1477/mapping",
            json={"confirmAutomatic": True, "releaseGroup": selected_group},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json()["message"],
            "Automatic match candidate confirmed.",
        )
        self.assertEqual(
            response.get_json()["mapping"]["releaseGroups"][0]["id"],
            selected_group,
        )
        musicbrainz_get.assert_not_called()
        upsert_mapping.assert_called_once_with(
            2451,
            title="GO!!!",
            artists=["Unknown Artist"],
            status="confirmed",
            provenance="manual-confirmation",
            scope="unknown",
            targets=[{
                "releaseGroupId": selected_group,
                "recordingIds": [selected_recording],
                "artistIds": [selected_artist],
                "releaseGroupTitle": "GO!!!",
                "artistName": "FLOW",
                "primaryType": "Single",
                "firstReleaseDate": "2004-04-28",
                "scope": "commercial_full",
                "preferred": True,
            }],
            preferred_release_group_mbid=selected_group,
        )
        sync_mapping.assert_called_once()
        self.assertEqual(sync_mapping.call_args.args[0], anime)
        self.assertEqual(sync_mapping.call_args.args[1], theme)
        self.assertEqual(
            sync_mapping.call_args.args[2]["releaseGroups"][0]["id"],
            selected_group,
        )

    @patch("backend.routes.anime.anime_mapping_registry.upsert_mapping")
    @patch("backend.routes.anime.anime_musicbrainz.cached_mapping")
    @patch("backend.routes.anime.animethemes.detail")
    def test_ambiguous_confirmation_rejects_an_arbitrary_release_group(
        self,
        detail,
        cached_mapping,
        upsert_mapping,
    ):
        candidate_group = "6259b4f8-39b2-4b46-98e0-5dd433630abc"
        arbitrary_group = "11111111-2222-4333-8444-555555555555"
        detail.return_value = {
            "slug": "anime",
            "themes": [{"id": 1, "song": {"id": 9, "title": "Song"}}],
        }
        cached_mapping.return_value = {
            "state": "ambiguous",
            "reason": "multiple-exact-release-groups",
            "matchMethod": "artist-discography-title",
            "releaseGroups": [{"id": candidate_group, "name": "Song"}],
        }

        response = self._admin_request(
            "PUT",
            "/api/anime/anime/themes/1/mapping",
            json={"confirmAutomatic": True, "releaseGroup": arbitrary_group},
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("not part", response.get_json()["error"])
        upsert_mapping.assert_not_called()

    @patch("backend.routes.anime.anime_mapping_registry.upsert_mapping")
    @patch("backend.routes.anime.anime_musicbrainz.cached_mapping")
    @patch("backend.routes.anime.animethemes.detail")
    def test_ambiguous_confirmation_requires_an_explicit_release_group(
        self,
        detail,
        cached_mapping,
        upsert_mapping,
    ):
        candidate_group = "6259b4f8-39b2-4b46-98e0-5dd433630abc"
        detail.return_value = {
            "slug": "anime",
            "themes": [{"id": 1, "song": {"id": 9, "title": "Song"}}],
        }
        cached_mapping.return_value = {
            "state": "ambiguous",
            "reason": "missing-artist",
            "matchMethod": "title-only-recording-search",
            "releaseGroups": [{"id": candidate_group, "name": "Song"}],
        }

        response = self._admin_request(
            "PUT",
            "/api/anime/anime/themes/1/mapping",
            json={"confirmAutomatic": True},
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("Choose", response.get_json()["error"])
        upsert_mapping.assert_not_called()

    @patch("backend.routes.anime.anime_musicbrainz.registered_mapping")
    @patch("backend.routes.anime.anime_mapping_registry.upsert_mapping")
    @patch("backend.routes.anime.anime_musicbrainz.cached_mapping")
    @patch("backend.routes.anime.animethemes.detail")
    def test_ambiguous_confirmation_allows_a_legacy_cached_candidate(
        self,
        detail,
        cached_mapping,
        upsert_mapping,
        registered_mapping,
    ):
        candidate_group = "6259b4f8-39b2-4b46-98e0-5dd433630abc"
        detail.return_value = {
            "slug": "anime",
            "themes": [{"id": 1, "song": {"id": 9, "title": "Song"}}],
        }
        cached_mapping.return_value = {
            "state": "ambiguous",
            "releaseGroups": [{"id": candidate_group, "name": "Song"}],
        }
        registered_mapping.return_value = {
            "state": "resolved",
            "mappingSource": "local",
            "releaseGroups": [{"id": candidate_group, "name": "Song"}],
        }

        response = self._admin_request(
            "PUT",
            "/api/anime/anime/themes/1/mapping",
            json={"confirmAutomatic": True, "releaseGroup": candidate_group},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json()["message"],
            "Automatic match candidate confirmed.",
        )
        upsert_mapping.assert_called_once()

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
        self.theme_link_sync.assert_called_once_with(
            detail.return_value,
            theme,
            response.get_json()["mapping"],
        )

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
        self.theme_link_sync.assert_called_once()
        self.assertEqual(self.theme_link_sync.call_args.args[0], response.get_json())
        self.assertEqual(self.theme_link_sync.call_args.args[1]["id"], 1477)

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

    @patch("backend.routes.anime._plex_index")
    @patch("backend.routes.anime.anime_musicbrainz.theme_mapping_key")
    @patch("backend.routes.anime.anime_metadata_worker.mappings_for")
    @patch("backend.routes.anime.animethemes.detail")
    def test_detail_enriches_candidates_with_cached_plex_links(
        self, detail, mappings_for, mapping_key, plex_index
    ):
        group_id = "6259b4f8-39b2-4b46-98e0-5dd433630abc"
        theme = {"id": 1477, "song": {"id": 1477, "title": "Shayou"}}
        detail.return_value = {"slug": "anime", "themes": [theme]}
        mapping_key.return_value = "theme-key"
        mappings_for.return_value = {
            "theme-key": {
                "state": "resolved",
                "releaseGroups": [{"id": group_id, "title": "斜陽"}],
            }
        }
        plex_index.return_value = {
            "releaseGroupsByMbid": {
                group_id: [{
                    "name": "斜陽",
                    "releaseType": "Single",
                    "musicbrainzReleaseId": "release-id",
                    "url": "https://app.plex.tv/album",
                    "plexampUrl": "plexamp://album",
                }]
            }
        }

        response = self._get("/api/anime/anime")

        group = response.get_json()["themes"][0]["mapping"]["releaseGroups"][0]
        self.assertTrue(group["availableInPlex"])
        self.assertEqual(group["plexUrl"], "https://app.plex.tv/album")
        self.assertEqual(group["plexampUrl"], "plexamp://album")
        self.assertEqual(group["plexReleases"][0]["releaseId"], "release-id")

    @patch("backend.routes.anime.anime_mapping_registry.mapping_proposals_for_anime")
    @patch("backend.routes.anime.anime_musicbrainz.theme_mapping_key")
    @patch("backend.routes.anime.anime_metadata_worker.mappings_for")
    @patch("backend.routes.anime.animethemes.detail")
    def test_detail_exposes_personal_proposal_and_admin_review_queue(
        self, detail, mappings_for, mapping_key, proposals_for_anime
    ):
        theme = {"id": 1477, "song": {"id": 2451, "title": "Shayou"}}
        detail.return_value = {"slug": "anime", "themes": [theme]}
        mapping_key.return_value = "theme-key"
        mappings_for.return_value = {"theme-key": {"state": "pending"}}
        proposals_for_anime.return_value = [{
            "id": 7,
            "userId": 1,
            "themeId": 1477,
            "status": "pending",
        }]
        admin = {"id": 1, "role": "admin"}

        with patch("backend.security.current_user", return_value=admin), patch(
            "backend.routes.anime.current_user", return_value=admin
        ):
            response = self.client.get("/api/anime/anime")

        mapping = response.get_json()["themes"][0]["mapping"]
        self.assertEqual(mapping["myProposal"]["id"], 7)
        self.assertEqual([item["id"] for item in mapping["proposals"]], [7])
        proposals_for_anime.assert_called_once_with(
            "anime",
            submitter_user_id=1,
            include_all_pending=True,
        )

    @patch("backend.routes.anime.anime_mapping_registry.mapping_proposals_for_anime")
    @patch("backend.routes.anime.anime_metadata_worker.mappings_for", return_value={})
    @patch("backend.routes.anime.animethemes.detail")
    def test_detail_scopes_non_admin_proposals_to_the_current_user(
        self, detail, mappings_for, proposals_for_anime
    ):
        detail.return_value = {
            "slug": "anime",
            "themes": [{"id": 1477, "song": {"id": 2451, "title": "Shayou"}}],
        }
        proposals_for_anime.return_value = [{
            "id": 7,
            "userId": 2,
            "themeId": 1477,
            "status": "pending",
        }]
        user = {"id": 2, "role": "user"}

        with patch("backend.security.current_user", return_value=user), patch(
            "backend.routes.anime.current_user", return_value=user
        ):
            response = self.client.get("/api/anime/anime")

        mapping = response.get_json()["themes"][0]["mapping"]
        self.assertEqual(mapping["myProposal"]["id"], 7)
        self.assertNotIn("proposals", mapping)
        proposals_for_anime.assert_called_once_with(
            "anime",
            submitter_user_id=2,
            include_all_pending=False,
        )

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
        request_resolution.assert_called_once_with(
            "naruto",
            [theme],
            requested_theme_ids=None,
        )

    @patch("backend.routes.anime.anime_musicbrainz.theme_mapping_key")
    @patch("backend.routes.anime.anime_metadata_worker.request_resolution")
    @patch("backend.routes.anime.animethemes.detail")
    def test_resolution_request_can_target_visible_themes_only(
        self, detail, request_resolution, mapping_key
    ):
        themes = [
            {"id": 10, "song": {"id": 110, "title": "Opening"}},
            {"id": 20, "song": {"id": 120, "title": "Ending"}},
        ]
        detail.return_value = {"slug": "anime", "themes": themes}
        mapping_key.side_effect = ["opening", "ending"]
        request_resolution.return_value = {
            "status": "idle",
            "polling": False,
            "mappings": {
                "opening": {"state": "pending"},
                "ending": {"state": "pending"},
            },
        }

        with patch("backend.security.current_user", return_value={"id": 1}):
            response = self.client.post(
                "/api/anime/anime/resolve",
                json={"themeIds": [20]},
            )

        self.assertEqual(response.status_code, 200)
        request_resolution.assert_called_once_with(
            "anime",
            themes,
            requested_theme_ids=["20"],
        )

    @patch("backend.routes.anime.anime_musicbrainz.theme_mapping_key")
    @patch("backend.routes.anime.anime_metadata_worker.request_resolution")
    @patch("backend.routes.anime.animethemes.detail")
    def test_resolution_request_preserves_explicit_empty_theme_list(
        self, detail, request_resolution, mapping_key
    ):
        theme = {"id": 10, "song": {"id": 110, "title": "Opening"}}
        detail.return_value = {"slug": "anime", "themes": [theme]}
        mapping_key.return_value = "opening"
        request_resolution.return_value = {
            "status": "idle",
            "polling": False,
            "mappings": {"opening": {"state": "pending"}},
        }

        with patch("backend.security.current_user", return_value={"id": 1}):
            response = self.client.post(
                "/api/anime/anime/resolve",
                json={"themeIds": []},
            )

        self.assertEqual(response.status_code, 200)
        request_resolution.assert_called_once_with(
            "anime",
            [theme],
            requested_theme_ids=[],
        )

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
