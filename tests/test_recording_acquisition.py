"""Exact recording acquisition ranking, complete browsing, cache, and API."""

from tests._test_environment import TEST_ROOT

import copy
import json
import sqlite3
import time
from threading import Event, Thread
from unittest.mock import patch
from uuid import UUID

import requests

from tests.test_backend import DatabaseTestCase
from backend import api_cache, cache_memo, track_search_index
from backend.services import lidarr, musicbrainz, plex, recording_acquisition
from backend.storage import save_service


def mbid(number):
    return str(UUID(int=number))


RECORDING = mbid(1)
ARTIST = mbid(2)
OTHER_ARTIST = mbid(3)


def credit(artist=ARTIST, name="Example Artist"):
    return [{"name": name, "artist": {"id": artist, "name": name}, "joinphrase": ""}]


def release(number, *, group_number=None, primary="Single", count=2,
            status="Official", date="2000-01-01", artist=ARTIST,
            secondary=(), recording=RECORDING):
    group_number = number if group_number is None else group_number
    medium = {"position": 1, "tracks": [{
        "id": mbid(2000 + number), "title": "Same title",
        "recording": {"id": recording, "title": "Same title"},
    }]}
    if count is not None:
        medium["track-count"] = count
    return {
        "id": mbid(1000 + number), "title": f"Release {number}",
        "date": date, "status": status, "artist-credit": credit(artist),
        "release-group": {
            "id": mbid(100 + group_number), "title": f"Group {group_number}",
            "primary-type": primary, "secondary-types": list(secondary),
            "first-release-date": date, "artist-credit": credit(artist),
        },
        "media": [medium],
    }


def page(releases, *, total=None, offset=0):
    return {
        "release-count": len(releases) if total is None else total,
        "release-offset": offset, "releases": releases,
    }


class AcquisitionRankingTests(DatabaseTestCase):
    def candidates(self, releases, recording_artists=(ARTIST,)):
        return recording_acquisition.rank_candidates(
            RECORDING,
            {"id": RECORDING, "artist-credit": [
                item for artist in recording_artists for item in credit(artist)
            ]},
            releases,
        )

    def test_single_beats_album(self):
        candidates = self.candidates([release(1, primary="Album", count=17), release(2)])
        self.assertEqual(candidates[0]["releaseGroupMbid"], mbid(102))

    def test_single_beats_ep_and_album(self):
        candidates = self.candidates([
            release(1, primary="Album"), release(2, primary="EP"), release(3),
        ])
        self.assertEqual([c["primaryType"] for c in candidates], ["Single", "EP", "Album"])

    def test_ep_and_album_fallbacks(self):
        self.assertEqual(self.candidates([
            release(1, primary="Album"), release(2, primary="EP"),
        ])[0]["primaryType"], "EP")
        self.assertEqual(self.candidates([release(1, primary="Album")])[0]["primaryType"], "Album")

    def test_other_broadcast_unknown_order(self):
        candidates = self.candidates([
            release(1, primary=None), release(2, primary="Broadcast"), release(3, primary="Other"),
        ])
        self.assertEqual([c["primaryType"] for c in candidates], ["Other", "Broadcast", None])

    def test_smaller_known_single_wins(self):
        candidates = self.candidates([release(1, count=8), release(2, count=2)])
        self.assertEqual(candidates[0]["releaseGroupMbid"], mbid(102))

    def test_missing_track_count_remains_eligible(self):
        candidates = self.candidates([release(1, count=None), release(2, count=8)])
        self.assertEqual(len(candidates), 2)
        self.assertEqual(candidates[0]["releaseGroupMbid"], mbid(102))
        self.assertIsNone(candidates[1]["minimumTrackCount"])
        self.assertIsNone(self.candidates([release(1, count=None)])[0]["minimumTrackCount"])

    def test_all_media_track_counts_are_summed(self):
        item = release(1, count=2)
        item["media"].append({"track-count": 3, "tracks": [], "position": 2})
        self.assertEqual(self.candidates([item])[0]["minimumTrackCount"], 5)

    def test_incomplete_or_invalid_medium_count_stays_unknown(self):
        for count in (None, True, -1, "garbage"):
            with self.subTest(count=count):
                item = release(1)
                item["media"].append({"track-count": count, "tracks": []})
                self.assertIsNone(self.candidates([item])[0]["minimumTrackCount"])

    def test_duplicate_editions_aggregate_into_one_group(self):
        items = [release(n, group_number=1, count=n + 1) for n in range(1, 6)]
        candidates = self.candidates(items)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["containingReleaseCount"], 5)
        self.assertEqual(candidates[0]["officialReleaseCount"], 5)
        self.assertEqual(candidates[0]["minimumTrackCount"], 2)
        self.assertEqual(len(candidates[0]["containingReleases"]), 5)

    def test_duplicate_release_does_not_inflate_evidence(self):
        item = release(1)
        self.assertEqual(self.candidates([item, copy.deepcopy(item)])[0]["containingReleaseCount"], 1)

    def test_same_title_and_artist_wrong_recording_is_excluded(self):
        candidates = self.candidates([release(1, recording=mbid(99)), release(2, primary="Album")])
        self.assertEqual([c["releaseGroupMbid"] for c in candidates], [mbid(102)])

    def test_missing_or_invalid_release_group_is_excluded(self):
        for group in ({}, {"id": "bad"}, None):
            with self.subTest(group=group):
                item = release(1)
                item["release-group"] = group
                self.assertEqual(self.candidates([item]), [])

    def test_artist_single_beats_various_artists_compilation(self):
        candidates = self.candidates([
            release(1, primary="Album", artist=musicbrainz.VARIOUS_ARTISTS_ID, secondary=("Compilation",)),
            release(2),
        ])
        self.assertEqual(candidates[0]["releaseGroupMbid"], mbid(102))

    def test_compilation_only_is_retained(self):
        candidates = self.candidates([
            release(1, primary="Album", artist=musicbrainz.VARIOUS_ARTISTS_ID, secondary=("Compilation",)),
        ])
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["secondaryTypes"], ["Compilation"])

    def test_broad_packaging_is_lower_priority_within_same_primary_type(self):
        candidates = self.candidates([
            release(1, primary="Album", count=2, secondary=("Compilation",)),
            release(2, primary="Album", count=12),
        ])
        self.assertEqual(candidates[0]["releaseGroupMbid"], mbid(102))

    def test_live_and_remix_recordings_remain_eligible(self):
        for secondary in ("Live", "Remix", "Soundtrack", "DJ-mix", "Mixtape/Street", "Demo"):
            with self.subTest(secondary=secondary):
                candidate = self.candidates([release(1, secondary=(secondary,))])[0]
                self.assertEqual(candidate["secondaryTypes"], [secondary])
                self.assertTrue(candidate["containsExactRecording"])

    def test_artist_relevance_uses_mbids_not_display_names(self):
        matching = release(2)
        matching["release-group"]["artist-credit"] = credit(ARTIST, "Different credited spelling")
        candidates = self.candidates([release(1, artist=OTHER_ARTIST), matching])
        self.assertEqual(candidates[0]["releaseGroupMbid"], mbid(102))
        self.assertEqual(candidates[0]["artistRelevance"], "same-credit")

    def test_overlap_credit_is_better_than_unrelated_credit(self):
        candidates = self.candidates([
            release(1, artist=OTHER_ARTIST), release(2),
        ], recording_artists=(ARTIST, mbid(4)))
        self.assertEqual(candidates[0]["artistRelevance"], "overlapping-credit")

    def test_missing_artist_mbids_are_unknown_not_inferred_from_recording(self):
        item = release(1)
        item["artist-credit"] = []
        item["release-group"]["artist-credit"] = []
        candidate = self.candidates([item])[0]
        self.assertEqual(candidate["artistMbids"], [])
        self.assertEqual(candidate["artistRelevance"], "unknown")

    def test_official_single_beats_bootleg_only_single(self):
        candidates = self.candidates([release(1, status="Bootleg", count=1), release(2, count=8)])
        self.assertEqual(candidates[0]["releaseGroupMbid"], mbid(102))

    def test_official_album_beats_known_non_official_only_single(self):
        for status in ("Bootleg", "Pseudo-Release", "Withdrawn", "Cancelled"):
            with self.subTest(status=status):
                candidates = self.candidates([
                    release(1, status=status), release(2, primary="Album"),
                ])
                self.assertEqual(candidates[0]["releaseGroupMbid"], mbid(102))

    def test_unusual_status_only_candidate_is_retained(self):
        candidate = self.candidates([release(1, status="Bootleg")])[0]
        self.assertEqual(candidate["officialReleaseCount"], 0)
        self.assertEqual(candidate["releaseStatusCounts"], {"Bootleg": 1})

    def test_unknown_status_does_not_override_single_preference(self):
        candidates = self.candidates([release(1, status=None), release(2, primary="Album")])
        self.assertEqual(candidates[0]["releaseGroupMbid"], mbid(101))

    def test_one_official_edition_removes_non_official_only_penalty(self):
        candidates = self.candidates([
            release(1, group_number=1, status="Bootleg"),
            release(2, group_number=1), release(3, primary="Album"),
        ])
        self.assertEqual(candidates[0]["releaseGroupMbid"], mbid(101))
        self.assertEqual(candidates[0]["officialReleaseCount"], 1)
        self.assertEqual(candidates[0]["ranking"]["nonOfficialOnlyPenalty"], 0)

    def test_earlier_group_date_then_mbid_break_ties_deterministically(self):
        first = release(1, date="2001-01-01")
        second = release(2, date="2000-01-01")
        self.assertEqual(self.candidates([first, second])[0]["releaseGroupMbid"], mbid(102))
        first["release-group"]["first-release-date"] = "2000-01-01"
        normal = self.candidates([first, second])
        reverse = self.candidates([second, first])
        self.assertEqual(normal, reverse)
        self.assertEqual(normal[0]["releaseGroupMbid"], mbid(101))

    def test_known_partial_date_beats_unknown_date(self):
        first = release(1, date=None)
        second = release(2, date="2000")
        self.assertEqual(self.candidates([first, second])[0]["releaseGroupMbid"], mbid(102))

    def test_reasons_and_rank_components_are_explainable(self):
        candidate = self.candidates([release(1)])[0]
        self.assertIn("contains exact recording", candidate["selectionReasons"])
        self.assertIn("preferred primary type: Single", candidate["selectionReasons"])
        self.assertEqual(candidate["ranking"]["minimumTrackCount"], 2)
        self.assertEqual(candidate["ranking"]["primaryTypeRank"], 0)
        self.assertEqual(candidate["rankTuple"], list(recording_acquisition.candidate_rank(candidate)))


class RecordingBrowseTests(DatabaseTestCase):
    def test_more_than_25_releases_and_short_pages_use_actual_offsets(self):
        items = [release(n) for n in range(1, 39)]
        pages = [page(items[:17], total=38), page(items[17:34], total=38, offset=17),
                 page(items[34:], total=38, offset=34)]
        with patch.object(musicbrainz, "get", side_effect=pages) as get:
            result = musicbrainz.browse_releases_by_recording(RECORDING)
        self.assertEqual(len(result), 38)
        self.assertEqual([call.kwargs["offset"] for call in get.call_args_list], [0, 17, 34])
        self.assertTrue(all(call.kwargs["recording"] == RECORDING for call in get.call_args_list))

    def test_zero_releases_is_complete(self):
        with patch.object(musicbrainz, "get", return_value=page([])) as get:
            self.assertEqual(musicbrainz.browse_releases_by_recording(RECORDING), [])
        self.assertEqual(get.call_count, 1)

    def test_empty_page_before_total_is_not_silently_complete(self):
        with patch.object(musicbrainz, "get", side_effect=[
            page([release(1)], total=2), page([], total=2, offset=1),
        ]):
            with self.assertRaises(requests.RequestException):
                musicbrainz.browse_releases_by_recording(RECORDING)

    def test_repeated_release_page_is_rejected(self):
        with patch.object(musicbrainz, "get", side_effect=[
            page([release(1)], total=2), page([release(1)], total=2, offset=1),
        ]):
            with self.assertRaises(requests.RequestException):
                musicbrainz.browse_releases_by_recording(RECORDING)

    def test_wrong_offset_or_changing_total_is_rejected(self):
        for second in (page([release(2)], total=2, offset=0),
                       page([release(2)], total=3, offset=1)):
            with self.subTest(second=second):
                with patch.object(musicbrainz, "get", side_effect=[
                    page([release(1)], total=2), second,
                ]):
                    with self.assertRaises(requests.RequestException):
                        musicbrainz.browse_releases_by_recording(RECORDING)

    def test_invalid_count_list_and_release_ids_are_rejected(self):
        invalid = [
            None, {}, {"release-count": True, "releases": []},
            {"release-count": -1, "releases": []},
            {"release-count": "bad", "releases": []},
            {"release-count": 0, "release-offset": 0.0, "releases": []},
            {"release-count": 0, "release-offset": False, "releases": []},
            {"release-count": 1, "releases": None},
            page([{"id": "bad"}]), page([release(1), release(1)]),
            page([release(1)], total=0),
        ]
        for payload in invalid:
            with self.subTest(payload=payload):
                with patch.object(musicbrainz, "get", return_value=payload):
                    with self.assertRaises(requests.RequestException):
                        musicbrainz.browse_releases_by_recording(RECORDING)


class AcquisitionServiceTests(DatabaseTestCase):
    def provider(self, releases):
        recording = {"id": RECORDING, "title": "Same title", "artist-credit": credit()}

        def get(path, inc, **kwargs):
            if path == f"/recording/{RECORDING}":
                return copy.deepcopy(recording)
            if path == "/release":
                offset = kwargs["offset"]
                return page(copy.deepcopy(releases[offset:offset + 17]), total=len(releases), offset=offset)
            raise AssertionError(f"Unexpected resource {path}")
        return get

    def test_beyond_first_page_candidate_can_win(self):
        items = [release(n, primary="Album") for n in range(1, 33)] + [release(33)]
        with patch.object(musicbrainz, "get", side_effect=self.provider(items)):
            result = recording_acquisition.resolve(RECORDING)
        self.assertEqual(result["state"], "resolved")
        self.assertEqual(result["target"]["releaseGroupMbid"], mbid(133))
        self.assertEqual(result["enumeratedReleaseCount"], 33)

    def test_empty_releases_and_missing_groups_have_distinct_states(self):
        for items, state in [([], "no_releases"), ([{**release(1), "release-group": {}}], "no_release_groups")]:
            with self.subTest(state=state):
                api_cache.delete_cache_namespace("musicbrainz-metadata")
                with patch.object(musicbrainz, "get", side_effect=self.provider(items)):
                    result = recording_acquisition.resolve(RECORDING)
                self.assertEqual(result["state"], state)
                self.assertIsNone(result["target"])
                self.assertEqual(result["alternatives"], [])

    def test_network_failure_does_not_cache_failure_or_partial_target(self):
        for error in (requests.Timeout("private-token"), requests.ConnectionError("private-url")):
            with self.subTest(error=error):
                with patch.object(musicbrainz, "get", side_effect=error):
                    result = recording_acquisition.resolve(RECORDING)
                self.assertEqual(result["state"], "musicbrainz_unavailable")
                self.assertIsNone(result["target"])
                self.assertNotIn("private", json.dumps(result))
        with patch.object(musicbrainz, "get", side_effect=self.provider([release(1)])):
            self.assertEqual(recording_acquisition.resolve(RECORDING)["state"], "resolved")

    def test_invalid_provider_configuration_returns_safe_failure(self):
        with patch.object(musicbrainz, "configuration", side_effect=musicbrainz.ConfigurationError("private-settings")):
            result = recording_acquisition.resolve(RECORDING)
        self.assertEqual(result["state"], "musicbrainz_unavailable")
        self.assertIsNone(result["target"])
        self.assertNotIn("private-settings", json.dumps(result))

    def test_partial_browse_failure_never_selects_partial_best(self):
        def get(path, inc, **kwargs):
            if path.startswith("/recording/"):
                return {"id": RECORDING, "artist-credit": credit()}
            if kwargs["offset"] == 0:
                return page([release(1)], total=2)
            raise requests.Timeout("private")
        with patch.object(musicbrainz, "get", side_effect=get):
            result = recording_acquisition.resolve(RECORDING)
        self.assertEqual(result["state"], "musicbrainz_unavailable")
        self.assertIsNone(result["target"])

    def test_fresh_normalized_cache_avoids_all_provider_calls(self):
        with patch.object(musicbrainz, "get", side_effect=self.provider([release(1)])) as get:
            first = recording_acquisition.resolve(RECORDING)
            second = recording_acquisition.resolve(RECORDING)
        self.assertEqual(first, second)
        self.assertEqual(get.call_count, 2)

    def test_fresh_raw_provider_documents_are_reused_without_http(self):
        recording = {"id": RECORDING, "artist-credit": credit()}
        documents = [
            musicbrainz.metadata_cache_record(f"/recording/{RECORDING}", "artist-credits", recording),
            musicbrainz.metadata_cache_record("/release", musicbrainz.RECORDING_RELEASE_INCLUDES,
                                              page([release(1)]), recording=RECORDING, limit=100, offset=0),
        ]
        api_cache.commit_json_responses(documents)
        with patch.object(musicbrainz, "_http_get", side_effect=AssertionError("live request")):
            result = recording_acquisition.resolve(RECORDING)
        self.assertEqual(result["state"], "resolved")

    def test_cache_expiry_refreshes_underlying_provider_documents(self):
        with patch.object(musicbrainz, "get", side_effect=self.provider([release(1)])):
            recording_acquisition.resolve(RECORDING)
        with api_cache.cache_db() as connection:
            connection.execute("UPDATE api_cache SET expires_at=? WHERE cache_key LIKE ?",
                               (time.time() - 1, recording_acquisition.CACHE_NAMESPACE + ":%"))
        with patch.object(musicbrainz, "get", side_effect=self.provider([release(2)])) as get:
            result = recording_acquisition.resolve(RECORDING)
        self.assertEqual(result["target"]["releaseGroupMbid"], mbid(102))
        self.assertTrue(all(call.kwargs.get("force_refresh") for call in get.call_args_list))

    def test_musicbrainz_namespace_clear_invalidates_normalized_resolution(self):
        with patch.object(musicbrainz, "get", side_effect=self.provider([release(1)])):
            recording_acquisition.resolve(RECORDING)
        api_cache.delete_cache_namespace("musicbrainz-metadata")
        with patch.object(musicbrainz, "get", side_effect=self.provider([release(2)])):
            result = recording_acquisition.resolve(RECORDING)
        self.assertEqual(result["target"]["releaseGroupMbid"], mbid(102))

    def test_different_musicbrainz_base_url_has_separate_cache_identity(self):
        with patch.object(musicbrainz, "get", side_effect=self.provider([release(1)])):
            recording_acquisition.resolve(RECORDING)
        save_service("musicbrainz", {"baseUrl": "https://different.example/ws/2"})
        with patch.object(musicbrainz, "get", side_effect=self.provider([release(2)])):
            result = recording_acquisition.resolve(RECORDING)
        self.assertEqual(result["target"]["releaseGroupMbid"], mbid(102))

    def test_recording_response_identity_must_match_input(self):
        with patch.object(musicbrainz, "get", return_value={"id": mbid(99), "artist-credit": credit()}):
            result = recording_acquisition.resolve(RECORDING)
        self.assertEqual(result["state"], "musicbrainz_unavailable")
        self.assertIsNone(result["target"])

    def test_concurrent_normalized_cache_misses_share_one_provider_browse(self):
        entered, finish = Event(), Event()
        outcomes, errors = [], []
        base = self.provider([release(1)])

        def get(path, inc, **kwargs):
            if path.startswith("/recording/"):
                entered.set()
                self.assertTrue(finish.wait(5))
            return base(path, inc, **kwargs)

        def run():
            try:
                outcomes.append(recording_acquisition.resolve(RECORDING))
            except BaseException as exc:
                errors.append(exc)

        with patch.object(musicbrainz, "get", side_effect=get) as get_mock:
            first, second = Thread(target=run), Thread(target=run)
            first.start()
            self.assertTrue(entered.wait(5))
            second.start()
            finish.set()
            first.join(5)
            second.join(5)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(outcomes), 2)
        self.assertEqual(outcomes[0], outcomes[1])
        self.assertEqual(get_mock.call_count, 2)

    def test_malformed_provider_tracklist_returns_safe_failure(self):
        item = release(1)
        item["media"][0]["tracks"] = 9
        with patch.object(musicbrainz, "get", side_effect=self.provider([item])):
            result = recording_acquisition.resolve(RECORDING)
        self.assertEqual(result["state"], "musicbrainz_unavailable")
        self.assertIsNone(result["target"])

    def test_release_detail_response_identity_must_match_requested_release(self):
        item = release(1)
        item.pop("media")
        base = self.provider([item])

        def get(path, inc, **kwargs):
            return release(2) if path.startswith("/release/") else base(path, inc, **kwargs)

        with patch.object(musicbrainz, "get", side_effect=get):
            result = recording_acquisition.resolve(RECORDING)
        self.assertEqual(result["state"], "musicbrainz_unavailable")
        self.assertIsNone(result["target"])

    def test_missing_tracklist_uses_one_exact_release_lookup(self):
        item = release(1)
        browse_item = copy.deepcopy(item)
        browse_item.pop("media")
        def get(path, inc, **kwargs):
            if path.startswith("/recording/"):
                return {"id": RECORDING, "artist-credit": credit()}
            if path == "/release":
                return page([browse_item])
            if path == f"/release/{item['id']}":
                return item
            raise AssertionError(path)
        with patch.object(musicbrainz, "get", side_effect=get) as get_mock:
            result = recording_acquisition.resolve(RECORDING)
        self.assertEqual(result["state"], "resolved")
        self.assertEqual(get_mock.call_count, 3)

    def test_missing_group_metadata_fetched_once_for_multiple_editions(self):
        items = [release(n, group_number=1) for n in range(1, 6)]
        group = copy.deepcopy(items[0]["release-group"])
        for item in items:
            item["release-group"] = {"id": group["id"]}
        base = self.provider(items)
        def get(path, inc, **kwargs):
            return group if path == f"/release-group/{group['id']}" else base(path, inc, **kwargs)
        with patch.object(musicbrainz, "get", side_effect=get) as get_mock:
            result = recording_acquisition.resolve(RECORDING)
        self.assertEqual(result["target"]["primaryType"], "Single")
        self.assertEqual(result["target"]["containingReleaseCount"], 5)
        self.assertEqual(sum(c.args[0].startswith("/release-group/") for c in get_mock.call_args_list), 1)

    def test_group_detail_response_id_cannot_substitute_another_group(self):
        item = release(1)
        item["release-group"] = {"id": mbid(101)}
        base = self.provider([item])
        def get(path, inc, **kwargs):
            return {"id": mbid(102)} if path.startswith("/release-group/") else base(path, inc, **kwargs)
        with patch.object(musicbrainz, "get", side_effect=get):
            result = recording_acquisition.resolve(RECORDING)
        self.assertEqual(result["state"], "musicbrainz_unavailable")
        self.assertIsNone(result["target"])


# Keep API tests independent without duplicating the provider setup.
class AcquisitionEndpointTests(DatabaseTestCase):
    provider = AcquisitionServiceTests.provider

    def setUp(self):
        super().setUp()
        with api_cache.cache_db() as connection:
            connection.execute("DELETE FROM track_search_plex_tracks")
            connection.execute("DELETE FROM track_search_plex_isrcs")

    def call(self, items=None):
        with patch.object(musicbrainz, "get", side_effect=self.provider([release(1)] if items is None else items)):
            return self.client.get(f"/api/music/recording/{RECORDING}/acquisition")

    def test_requires_session_and_validates_recording_uuid(self):
        url = f"/api/music/recording/{RECORDING}/acquisition"
        self.assertEqual(self.client.get(url).status_code, 401)
        self.register()
        self.assertEqual(self.client.get("/api/music/recording/bad/acquisition").status_code, 400)
        response = self.call()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_missing_recording_has_target_without_triggering_acquisition(self):
        self.register()
        with (patch.object(lidarr, "add_album", side_effect=AssertionError("download")),
              patch.object(lidarr, "start_command", side_effect=AssertionError("search")),
              patch.object(plex, "full_library_scan", side_effect=AssertionError("scan")),
              patch.object(track_search_index, "rebuild_from_cache", side_effect=AssertionError("rebuild"))):
            result = self.call([release(1), release(2, primary="Album")]).get_json()
        self.assertFalse(result["available"])
        self.assertTrue(result["needsAcquisition"])
        self.assertEqual(result["target"]["primaryType"], "Single")
        self.assertEqual(len(result["alternatives"]), 1)
        with api_cache.cache_db() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM track_search_plex_tracks").fetchone()[0], 0)

    def test_existing_plex_recording_still_resolves_diagnostic_target(self):
        self.register()
        save_service("plex", {"machineIdentifier": "real-server", "token": "never-expose"})
        track_search_index.index_plex_library({
            "serverId": "real-server", "tracks": [{
                "ratingKey": "265485", "key": "/library/metadata/265485",
                "title": "Same title", "musicbrainzRecordingId": RECORDING,
            }],
        })
        result = self.call().get_json()
        self.assertTrue(result["available"])
        self.assertFalse(result["needsAcquisition"])
        self.assertIsNotNone(result["target"])
        self.assertNotIn("never-expose", json.dumps(result))

    def test_local_plex_and_lidarr_state_remain_fresh_on_resolution_cache_hit(self):
        self.register()
        first = self.call().get_json()
        self.assertFalse(first["available"])
        save_service("plex", {"machineIdentifier": "real-server"})
        track_search_index.index_plex_library({
            "serverId": "real-server", "tracks": [{
                "ratingKey": "1", "key": "/library/metadata/1", "musicbrainzRecordingId": RECORDING,
            }],
        })
        api_cache.set_cache_document("lidarr-library", "albums", {"albums": {
            mbid(101): {"fullyAvailable": True, "trackFileCount": 2, "totalTrackCount": 2},
        }}, 600)
        cache_memo.invalidate_document(lidarr.LIBRARY_INDEX_KEY)
        with patch.object(musicbrainz, "get", side_effect=AssertionError("cached resolution")):
            second = self.client.get(f"/api/music/recording/{RECORDING}/acquisition").get_json()
        self.assertTrue(second["available"])
        self.assertFalse(second["needsAcquisition"])
        self.assertTrue(second["target"]["availableInLidarr"])
        self.assertTrue(second["target"]["fullyAvailableInLidarr"])

    def test_lidarr_snapshot_is_read_once_not_per_candidate(self):
        self.register()
        with patch.object(lidarr, "cached_library_availability", return_value={}) as cached:
            self.call([release(n) for n in range(1, 20)])
        self.assertEqual(cached.call_count, 1)

    def test_complete_empty_collection_has_no_target(self):
        self.register()
        result = self.call([]).get_json()
        self.assertEqual(result["state"], "no_releases")
        self.assertIsNone(result["target"])
        self.assertEqual(result["alternatives"], [])
        self.assertFalse(result["available"])
        self.assertTrue(result["needsAcquisition"])

    def test_wrong_recording_is_not_an_acquisition_target(self):
        self.register()
        result = self.call([release(1, recording=mbid(99)), release(2, primary="Album")]).get_json()
        self.assertEqual(result["target"]["releaseGroupMbid"], mbid(102))
        self.assertEqual(result["alternatives"], [])

    def test_lidarr_state_does_not_reorder_acquisition_candidates(self):
        self.register()
        api_cache.set_cache_document("lidarr-library", "albums", {"albums": {
            mbid(102): {"fullyAvailable": True, "trackFileCount": 17, "totalTrackCount": 17},
        }}, 600)
        cache_memo.invalidate_document(lidarr.LIBRARY_INDEX_KEY)
        result = self.call([release(1), release(2, primary="Album")]).get_json()
        self.assertEqual(result["target"]["primaryType"], "Single")
        self.assertFalse(result["target"]["availableInLidarr"])
        self.assertTrue(result["alternatives"][0]["fullyAvailableInLidarr"])

    def test_provider_failure_is_safe_and_has_no_target(self):
        self.register()
        with patch.object(musicbrainz, "get", side_effect=requests.Timeout("token=private")):
            response = self.client.get(f"/api/music/recording/{RECORDING}/acquisition")
        self.assertEqual(response.status_code, 502)
        result = response.get_json()
        self.assertEqual(result["state"], "musicbrainz_unavailable")
        self.assertIsNone(result["target"])
        self.assertNotIn("private", response.get_data(as_text=True))
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_local_index_failure_returns_safe_503(self):
        self.register()
        with patch.object(plex, "recording_availability", side_effect=sqlite3.OperationalError("private-path")):
            save_service("plex", {"machineIdentifier": "server"})
            response = self.call()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private-path", response.get_data(as_text=True))
