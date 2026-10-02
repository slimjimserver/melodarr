"""The Various Artists exception stays limited to local library inventory."""

from ._test_environment import TEST_DATA, TEST_ROOT

from contextlib import contextmanager
from unittest.mock import patch

import requests

from backend import detail_cache
from backend.routes import music as music_routes
from backend.services import musicbrainz
from backend.workers import artist_metadata
from .test_backend import DatabaseTestCase


VARIOUS = musicbrainz.VARIOUS_ARTISTS_ID
OTHER = "11111111-1111-4111-8111-111111111111"
OWNED = "22222222-2222-4222-8222-222222222222"
LIDARR_OWNED = "33333333-3333-4333-8333-333333333333"
UNOWNED = "44444444-4444-4444-8444-444444444444"


class LibraryOnlyArtistTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.csrf = self.register()
        self.snapshot = {
            "artists": [
                {"musicbrainzId": VARIOUS, "ratingKey": "va-1", "name": "Various Artists"},
                {"musicbrainzId": VARIOUS, "ratingKey": "va-2", "name": "Various Artists"},
                {"musicbrainzId": OTHER, "ratingKey": "other", "name": "Various Artists"},
            ],
            "releaseGroups": [
                {"artistRatingKey": "va-1", "ratingKey": "album-1", "name": "Owned compilation",
                 "musicbrainzReleaseGroupId": OWNED, "releaseType": "album", "year": 2020},
                {"artistRatingKey": "va-2", "ratingKey": "album-2", "name": "Owned compilation",
                 "musicbrainzReleaseGroupId": OWNED, "releaseType": "album", "year": 2020},
                {"artistRatingKey": "va-1", "ratingKey": "unmapped", "name": "Unmatched compilation",
                 "releaseType": "album", "url": "https://app.plex.tv/desktop/#!/album/unmapped"},
                {"artistRatingKey": "other", "ratingKey": "other-album", "name": "Different identity",
                 "musicbrainzReleaseGroupId": UNOWNED, "releaseType": "album"},
            ],
            "tracks": [
                {"title": "Intro", "artistRatingKey": "va-1", "musicbrainzReleaseGroupId": OWNED},
                {"title": "Local Intro", "artistRatingKey": "va-1", "albumRatingKey": "unmapped"},
                {"title": "Other Intro", "artistRatingKey": "other", "musicbrainzReleaseGroupId": UNOWNED},
            ],
        }
        self.lidarr_albums = {
            OWNED: {"artistMbid": VARIOUS, "title": "Owned compilation", "type": "Album",
                    "trackFileCount": 10, "fullyAvailable": True},
            LIDARR_OWNED: {"artistMbid": VARIOUS, "title": "Partly imported compilation", "type": "Album",
                           "trackFileCount": 1, "fullyAvailable": False},
            UNOWNED: {"artistMbid": VARIOUS, "title": "Missing compilation", "type": "Album",
                      "trackFileCount": 0, "fullyAvailable": False},
        }

    def plex_index(self):
        groups = {}
        for album in self.snapshot["releaseGroups"]:
            if album.get("musicbrainzReleaseGroupId"):
                groups.setdefault(album["musicbrainzReleaseGroupId"], []).append(album)
        return {
            "snapshot": self.snapshot,
            "artistsByMbid": {artist["musicbrainzId"]: artist for artist in self.snapshot["artists"]},
            "releaseGroupsByMbid": groups,
        }

    @contextmanager
    def libraries(self):
        with (
            patch.object(music_routes, "_plex_index", side_effect=self.plex_index),
            patch.object(music_routes.lidarr, "cached_library_availability", side_effect=lambda: self.lidarr_albums),
            patch.object(music_routes.lidarr, "cached_artist_availability", return_value={VARIOUS: {"name": "Various Artists"}}),
            patch.object(musicbrainz, "get", side_effect=AssertionError("Unexpected MusicBrainz catalogue lookup")),
            patch.object(musicbrainz, "search", side_effect=AssertionError("Unexpected MusicBrainz search")),
        ):
            yield

    def albums(self, response):
        return {album["id"]: album for section in response.get_json()["sections"].values() for album in section}

    def test_artist_page_only_lists_owned_albums_and_preserves_all_plex_copies(self):
        with self.libraries():
            response = self.client.get(f"/api/music/artist/{VARIOUS}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(self.albums(response)), {OWNED, LIDARR_OWNED, "plex:unmapped"})
        self.assertEqual(len(self.albums(response)[OWNED]["plexReleases"]), 2)
        self.assertTrue(response.get_json()["libraryOnly"])
        self.assertEqual(response.get_json()["metadataSource"], "Library")
        self.assertFalse(response.get_json()["provisional"])
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_old_global_assembled_cache_is_ignored_for_every_artist_page_variant(self):
        with self.app.app_context():
            detail_cache.store(("artist", VARIOUS), {"name": "Old global catalogue", "total": 100000})
        with self.libraries():
            for suffix in ("", "?complete=1", "?prefetch=1"):
                with self.subTest(suffix=suffix):
                    response = self.client.get(f"/api/music/artist/{VARIOUS.upper()}{suffix}")
                    self.assertEqual(response.get_json()["id"], VARIOUS)
                    self.assertEqual(response.get_json()["total"], 3)

    def test_library_only_page_follows_inventory_changes_without_an_assembled_cache(self):
        with self.libraries():
            self.assertEqual(self.client.get(f"/api/music/artist/{VARIOUS}").get_json()["total"], 3)
            self.snapshot["releaseGroups"] = []
            self.lidarr_albums.clear()
            response = self.client.get(f"/api/music/artist/{VARIOUS}")
        self.assertEqual(response.get_json()["total"], 0)
        self.assertEqual(response.get_json()["sections"], {})

    def test_refresh_returns_local_inventory_without_scheduling_global_work(self):
        with (
            self.libraries(),
            patch.object(artist_metadata, "refresh_artist_metadata") as refresh,
            patch.object(artist_metadata, "request_track_refresh") as tracks,
        ):
            response = self.client.post(f"/api/music/artist/{VARIOUS}/refresh", json={},
                                        headers={"X-CSRF-Token": self.csrf})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["libraryOnly"])
        refresh.assert_not_called()
        tracks.assert_not_called()

    def test_track_search_filters_legacy_global_results_and_finds_local_plex_tracks(self):
        with (
            self.libraries(),
            patch.object(music_routes.track_search_index, "search_artist_tracks", return_value=[
                {"release_group_mbid": UNOWNED, "normalized_title": "global intro"},
                {"release_group_mbid": LIDARR_OWNED, "normalized_title": "intro"},
            ]),
        ):
            response = self.client.get(f"/api/music/artist/{VARIOUS}/tracks?q=Intro")
        results = {group["id"]: group for group in response.get_json()["results"]}
        self.assertEqual(set(results), {OWNED, LIDARR_OWNED, "plex:unmapped"})
        self.assertEqual(results["plex:unmapped"]["matchedTracks"], ["Local Intro"])

    def test_empty_local_track_search_never_falls_back_to_musicbrainz(self):
        with self.libraries():
            response = self.client.get(f"/api/music/artist/{VARIOUS}/tracks?q=NoMatch")
        self.assertEqual(response.get_json(), {"results": [], "candidateCount": 0})

    def test_revalidation_and_all_worker_entry_points_skip_this_artist(self):
        with self.libraries():
            response = self.client.post(f"/api/music/artist/{VARIOUS}/revalidate", json={},
                                        headers={"X-CSRF-Token": self.csrf})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()["status"], "library-only")
            self.assertFalse(self.client.get(f"/api/music/artist/{VARIOUS}/revalidation").get_json()["polling"])
            artist_metadata.request_track_refresh(VARIOUS)
            artist_metadata._process_artist(VARIOUS, force_tracks=True)
            self.assertEqual(artist_metadata.refresh_artist_metadata(VARIOUS, "critical"), {"libraryOnly": True})
            self.assertEqual(artist_metadata._refresh_artist_tracks(VARIOUS, {}), 0)
        self.assertNotIn(VARIOUS, artist_metadata.queued_artist_ids)
        self.assertNotIn(VARIOUS, artist_metadata.forced_track_artist_ids)

    def test_no_plex_library_can_still_show_owned_lidarr_albums(self):
        self.snapshot = {"artists": [], "releaseGroups": [], "tracks": []}
        with self.libraries():
            response = self.client.get(f"/api/music/artist/{VARIOUS}")
        self.assertEqual(set(self.albums(response)), {OWNED, LIDARR_OWNED})

    def test_exception_matches_only_the_exact_id(self):
        self.assertTrue(musicbrainz.is_library_only_artist(VARIOUS.upper()))
        self.assertFalse(musicbrainz.is_library_only_artist(OTHER))
        self.assertFalse(musicbrainz.is_library_only_artist("Various Artists"))

    def test_musicbrainz_collection_guard_does_not_affect_owned_release_lookups(self):
        with patch.object(musicbrainz, "_cached_get", return_value={"id": OWNED}) as cached_get:
            for resource in ("/release-group", "/release", "/recording"):
                self.assertIsNone(musicbrainz.get(resource, "", artist=VARIOUS, cache_only=True))
                with self.assertRaises(requests.RequestException):
                    musicbrainz.get(resource, "", artist=VARIOUS.upper())
            cached_get.assert_not_called()
            self.assertEqual(musicbrainz.get(f"/release/{OWNED}", "recordings+isrcs"), {"id": OWNED})
            musicbrainz.get("/release-group", "", artist=OTHER)
            self.assertEqual(cached_get.call_count, 2)
