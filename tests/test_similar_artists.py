"""Focused regression tests for paged similar-artist enrichment."""

if __package__:
    from ._test_environment import TEST_ROOT
else:  # Support unittest discovery with tests/ as the top-level directory.
    from _test_environment import TEST_ROOT

from typing import ClassVar
from unittest.mock import patch

import requests

from backend.workers import similar_artists as similar_artist_worker
from tests.test_backend import DatabaseTestCase


class SimilarArtistWorkerTests(DatabaseTestCase):
    candidate: ClassVar[dict[str, str]] = {
        "name": "Exact Artist",
        "url": "https://www.last.fm/music/Exact+Artist",
    }
    artist_id = "22222222-2222-2222-2222-222222222222"

    @patch("backend.workers.similar_artists.musicbrainz.search")
    def test_exact_artist_is_persisted_at_background_priority(self, search):
        search.return_value = {
            "artists": [{"id": self.artist_id, "name": "Exact Artist"}],
        }

        self.assertEqual(
            similar_artist_worker.request_resolutions([self.candidate]),
            1,
        )
        self.assertTrue(similar_artist_worker.process_one())

        self.assertEqual(
            similar_artist_worker.cached_resolution(self.candidate),
            {"id": self.artist_id, "name": "Exact Artist"},
        )
        self.assertIsNone(
            similar_artist_worker.cached_resolution(
                {
                    **self.candidate,
                    "url": "https://www.last.fm/music/A+Different+Exact+Artist",
                }
            )
        )
        search.assert_called_once_with(
            'artist:"Exact Artist"',
            "artist",
            priority="background",
        )

    @patch("backend.workers.similar_artists.musicbrainz.search")
    def test_ambiguous_exact_name_is_negative_cached(self, search):
        search.return_value = {
            "artists": [
                {"id": self.artist_id, "name": "Exact Artist"},
                {
                    "id": "33333333-3333-3333-3333-333333333333",
                    "name": "Exact Artist",
                },
            ]
        }

        similar_artist_worker.request_resolutions([self.candidate])
        similar_artist_worker.process_one()

        self.assertEqual(
            similar_artist_worker.cached_resolution(self.candidate),
            {"id": "", "name": "Exact Artist"},
        )
        self.assertEqual(
            similar_artist_worker.request_resolutions([self.candidate]),
            0,
        )

    @patch("backend.workers.similar_artists.musicbrainz.search")
    def test_transport_failure_is_not_negative_cached(self, search):
        search.side_effect = requests.Timeout("offline")
        similar_artist_worker.request_resolutions([self.candidate])

        similar_artist_worker.process_one()

        self.assertIsNone(similar_artist_worker.cached_resolution(self.candidate))
        self.assertEqual(
            similar_artist_worker.request_resolutions([self.candidate]),
            1,
        )

    @patch(
        "backend.workers.similar_artists.cached_resolution",
        return_value=None,
    )
    def test_queue_has_fixed_backpressure(self, cached_resolution):
        candidates = [
            {"name": f"Artist {index}", "url": f"https://last.fm/{index}"}
            for index in range(300)
        ]

        queued = similar_artist_worker.request_resolutions(candidates)

        self.assertEqual(queued, similar_artist_worker.MAX_QUEUED_CANDIDATES)
        self.assertEqual(
            len(similar_artist_worker.queued_candidates),
            similar_artist_worker.MAX_QUEUED_CANDIDATES,
        )


class SimilarArtistPaginationTests(DatabaseTestCase):
    artist_id = "11111111-1111-1111-1111-111111111111"

    @patch("backend.routes.music.lidarr.cached_artist_availability", return_value={})
    @patch("backend.routes.music.similar_artist_worker.request_resolutions")
    @patch("backend.routes.music.similar_artist_worker.cached_resolution")
    @patch("backend.routes.music.lastfm.get_public")
    @patch(
        "backend.routes.music.get_lastfm_api_key",
        return_value="shared-lastfm-key",
    )
    def test_pending_raw_slice_keeps_stable_offset_and_rank(
        self,
        get_api_key,
        get_public,
        cached_resolution,
        request_resolutions,
        artist_availability,
    ):
        items = [
            {
                "mbid": f"{index + 2:08x}-1111-1111-1111-111111111111",
                "name": f"Artist {index}",
                "match": str(1 - index / 100),
                "url": f"https://last.fm/{index}",
            }
            for index in range(15)
        ]
        items[5]["mbid"] = ""
        get_public.return_value = {"similarartists": {"artist": items}}
        cached_resolution.return_value = None
        self.register()

        pending = self.client.get(
            f"/api/music/artist/{self.artist_id}/similar?offset=0&limit=12"
        ).get_json()

        self.assertEqual(pending["pending"], 1)
        self.assertTrue(pending["hasMore"])
        self.assertIsNone(pending["nextOffset"])
        self.assertEqual(
            [artist["rank"] for artist in pending["artists"]],
            [*range(5), *range(6, 12)],
        )

        cached_resolution.return_value = {
            "id": "99999999-1111-1111-1111-111111111111",
            "name": "Artist 5",
        }
        settled = self.client.get(
            f"/api/music/artist/{self.artist_id}/similar?offset=0&limit=12"
        ).get_json()

        self.assertEqual(settled["pending"], 0)
        self.assertEqual(settled["nextOffset"], 12)
        self.assertEqual(
            [artist["rank"] for artist in settled["artists"]],
            list(range(12)),
        )
        self.assertTrue(
            all(call.kwargs["limit"] == 50 for call in get_public.call_args_list)
        )
