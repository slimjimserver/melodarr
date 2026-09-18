"""MusicBrainz-to-AnimeThemes series resolver endpoint tests."""

# Test storage isolation must be installed before Flask or backend imports.
from ._test_environment import TEST_ROOT  # noqa: I001

import os
import tempfile
import unittest
from unittest.mock import patch

from flask import Flask

from backend import storage
from backend.routes.anime import blueprint
from backend.security import verify_csrf_token
from backend.services import (
    anime_mapping_registry,
    anime_musicbrainz,
    anime_theme_links,
)

GROUP = "d65f7448-6d69-48b9-bc11-fb8a0b6f6e5f"
OTHER_GROUP = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
RECORDING = "2b31e1c4-561b-305a-a94c-0c47f6378447"
OTHER_RECORDING = "11111111-2222-4333-8444-555555555555"
API_KEY = "test-automation-api-key-0123456789"


def anime(slug="sword_art_online", anime_id=1234, series=None):
    return {
        "id": anime_id,
        "slug": slug,
        "name": "Sword Art Online Season 1",
        "series": series
        if series is not None
        else [{"id": 123, "name": "Sword Art Online", "slug": "sword_art_online"}],
    }


def theme(theme_id=10, song_id=20):
    return {
        "id": theme_id,
        "label": "Opening 1",
        "type": "OP",
        "sequence": 1,
        "song": {"id": song_id, "title": "crossing field", "artists": []},
    }


class AnimeThemesSeriesResolverTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(dir=TEST_ROOT)
        self.addCleanup(directory.cleanup)
        patcher = patch.object(
            storage, "DATABASE", os.path.join(directory.name, "db.sqlite")
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        storage.init_db()

        app = Flask(__name__)
        app.config.update(
            AUTOMATION_API_KEY=API_KEY,
            TESTING=True,
            SECRET_KEY="test-secret",
        )
        app.before_request(verify_csrf_token)
        app.register_blueprint(blueprint)
        self.client = app.test_client()
        with self.client.session_transaction() as session:
            session["csrf_token"] = "test-csrf-token"
        user = patch("backend.security.current_user", return_value={"id": 1})
        user.start()
        self.addCleanup(user.stop)

    def seed(
        self,
        *,
        source_anime=None,
        source_theme=None,
        group_id=GROUP,
        recording_ids=None,
    ):
        source_anime = source_anime or anime()
        source_theme = source_theme or theme()
        anime_mapping_registry.upsert_mapping(
            source_theme["song"]["id"],
            title=source_theme["song"]["title"],
            artists=["LiSA"],
            status="confirmed",
            provenance="manual-confirmation",
            targets=[
                {
                    "releaseGroupId": group_id,
                    "recordingIds": recording_ids or [],
                    "releaseGroupTitle": "crossing field",
                    "artistName": "LiSA",
                    "preferred": True,
                }
            ],
            preferred_release_group_mbid=group_id,
        )
        mapping = anime_musicbrainz.registered_mapping(source_theme)
        anime_theme_links.sync_anime_theme_mapping(source_anime, source_theme, mapping)

    def post(self, payload, *, headers=None, include_csrf=True):
        request_headers = {}
        if include_csrf:
            request_headers["X-CSRF-Token"] = "test-csrf-token"
        request_headers.update(headers or {})
        return self.client.post(
            "/api/v1/animethemes/resolve",
            headers=request_headers,
            json=payload,
        )

    def clear_session(self):
        with self.client.session_transaction() as session:
            session.clear()

    def test_release_group_match(self):
        self.seed()
        response = self.post({"releaseGroupId": GROUP})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json(),
            {
                "series": [
                    {
                        "animeThemesSeriesId": 123,
                        "matchedBy": "releaseGroup",
                        "name": "Sword Art Online",
                        "slug": "sword_art_online",
                    }
                ]
            },
        )

    def test_recording_only_match(self):
        self.seed(recording_ids=[RECORDING])
        response = self.post({"recordingIds": [RECORDING]})
        self.assertEqual(response.get_json()["series"][0]["matchedBy"], "recording")

    def test_release_group_takes_precedence_for_the_same_series(self):
        self.seed(recording_ids=[RECORDING])
        response = self.post(
            {
                "releaseGroupId": GROUP,
                "recordingIds": [RECORDING],
            }
        )
        self.assertEqual(len(response.get_json()["series"]), 1)
        self.assertEqual(response.get_json()["series"][0]["matchedBy"], "releaseGroup")

    def test_multiple_recordings_for_the_same_series_are_deduplicated(self):
        self.seed(recording_ids=[RECORDING, OTHER_RECORDING])
        response = self.post({"recordingIds": [RECORDING, OTHER_RECORDING, RECORDING]})
        self.assertEqual(len(response.get_json()["series"]), 1)

    def test_multiple_legitimate_series_are_returned(self):
        self.seed(recording_ids=[RECORDING])
        self.seed(
            source_anime=anime(
                "fate_zero",
                4321,
                [{"id": 456, "name": "Fate", "slug": "fate"}],
            ),
            source_theme=theme(11, 21),
            group_id=OTHER_GROUP,
            recording_ids=[OTHER_RECORDING],
        )
        response = self.post({"recordingIds": [RECORDING, OTHER_RECORDING]})
        self.assertEqual(
            {item["animeThemesSeriesId"] for item in response.get_json()["series"]},
            {123, 456},
        )

    def test_unknown_ids_return_an_empty_list(self):
        response = self.post({"releaseGroupId": GROUP, "recordingIds": [RECORDING]})
        self.assertEqual(response.get_json(), {"series": []})

    def test_missing_and_malformed_ids_are_validation_errors(self):
        self.assertEqual(self.post({}).status_code, 400)
        self.assertEqual(self.post({"releaseGroupId": "not-an-mbid"}).status_code, 400)
        self.assertEqual(self.post({"recordingIds": "not-a-list"}).status_code, 400)

    def test_endpoint_requires_authentication(self):
        self.clear_session()
        with patch("backend.security.current_user", return_value=None):
            response = self.post({"releaseGroupId": GROUP}, include_csrf=False)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json(), {"error": "Sign in is required."})

    def test_valid_api_key_succeeds_without_session_or_csrf(self):
        self.seed()
        self.clear_session()
        with patch("backend.security.current_user", return_value=None):
            response = self.post(
                {"releaseGroupId": GROUP},
                headers={"X-Api-Key": API_KEY},
                include_csrf=False,
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["series"][0]["animeThemesSeriesId"], 123)

    def test_invalid_api_key_fails_without_session(self):
        self.clear_session()
        with patch("backend.security.current_user", return_value=None):
            response = self.post(
                {"releaseGroupId": GROUP},
                headers={"X-Api-Key": "invalid-key"},
                include_csrf=False,
            )
        self.assertEqual(response.status_code, 401)

    def test_browser_session_authentication_still_requires_and_accepts_csrf(self):
        self.seed()
        accepted = self.post({"releaseGroupId": GROUP})
        rejected = self.post({"releaseGroupId": GROUP}, include_csrf=False)
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(rejected.status_code, 403)

    def test_ordinary_session_post_route_remains_csrf_protected(self):
        response = self.client.post("/api/anime/example/resolve", json={})
        self.assertEqual(response.status_code, 403)

    def test_anime_without_series_is_an_explicit_fallback(self):
        self.seed(source_anime=anime(series=[]))
        result = self.post({"releaseGroupId": GROUP}).get_json()["series"][0]
        self.assertEqual(result["animeThemesSeriesId"], None)
        self.assertEqual(result["animeThemesAnimeId"], 1234)
        self.assertEqual(result["fallback"], "anime")


if __name__ == "__main__":
    unittest.main()
