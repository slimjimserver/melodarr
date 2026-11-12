"""Deezer artist selection is derived from MB relationships and complete fan evidence."""

from ._test_environment import TEST_ROOT

import time
import unittest
from copy import deepcopy
from itertools import permutations
from queue import Queue
from unittest.mock import call, patch

import requests

from backend import api_cache, detail_cache
from backend.services import artist_summary as summary, deezer, deezer_artist, musicbrainz
from backend.workers import artist_summary as worker
from tests.test_backend import DatabaseTestCase, Response

KANYE = "164f0d73-1234-4e2c-8743-d77bf2191051"
ED = "b8a7c51f-362c-4dcb-a259-bc6e0095f0a6"
KANYE_IDS = [230, 4099199, 6013562]
ED_IDS = [384236, 307678801]
FANS = {230: 4551583, 4099199: 22519, 6013562: 2589, 384236: 20930996, 307678801: 2443, 99: 1}
RECORDING = "11111111-1111-1111-1111-111111111111"
GROUP = "22222222-2222-2222-2222-222222222222"


def artist_data(mbid=KANYE, ids=KANYE_IDS):
    return {"id": mbid, "name": "Fixture Artist", "relations": [
        {"url": {"resource": f"https://www.deezer.com/artist/{artist_id}"}} for artist_id in ids
    ]}


class DeezerArtistClientTests(unittest.TestCase):
    def test_validated_country_urls_deduplicate_without_touching_album_selection(self):
        urls = ["https://www.deezer.com/artist/230", "https://www.deezer.com/us/artist/230",
                "http://deezer.com/artist/230/", "https://www.deezer.com:443/fr/artist/230?utm=test"]
        source = [{"url": {"resource": value}} for value in urls]
        original = deepcopy(source)
        self.assertEqual(deezer.artist_relationship_ids(source), [230])
        self.assertEqual(source, original)
        albums = [{"url": {"resource": "https://www.deezer.com/us/album/7090505"}}]
        self.assertEqual(deezer.relationship_id(albums, "album"), 7090505)
        self.assertEqual(deezer.artist_relationship_ids(albums), [])
        self.assertIsNone(deezer.relationship_id(albums + [{"url": {"resource": "https://deezer.com/album/2"}}], "album"))

    def test_invalid_artist_urls_never_become_candidates(self):
        urls = ["https://deezer.com.evil.test/artist/230", "https://api.deezer.com/artist/230",
                "https://user:password@deezer.com/artist/230", "https://deezer.com:444/artist/230",
                "https://deezer.com:bad/artist/230", "javascript:alert(230)", "https://deezer.com/album/230",
                "https://deezer.com/artist/0", "https://deezer.com/artist/-230", "https://deezer.com/artist/2.3",
                "https://deezer.com/artist/230\n", "https://attacker.test/?next=https://deezer.com/artist/230"]
        self.assertEqual(deezer.artist_relationship_ids([None, {}, {"url": "invalid"},
                         *[{"url": {"resource": url}} for url in urls]]), [])

    @patch.object(deezer.requests, "get", return_value=Response(payload={"id": 230, "nb_fan": 4551583}))
    def test_artist_uses_the_existing_public_client_pacing_timeouts_and_validation(self, get):
        with patch.object(deezer, "_next_request_at", 0), patch.object(deezer.time, "sleep") as sleep, \
                patch.object(deezer.time, "monotonic", return_value=10):
            self.assertEqual(deezer.artist(230)["nb_fan"], 4551583)
            deezer.artist(230)
            self.assertEqual(sleep.call_args_list, [call(0), call(0.25)])
        self.assertEqual(get.call_args.args[0], "https://api.deezer.com/artist/230")
        self.assertEqual(get.call_args.kwargs["timeout"], (3.05, 10))
        self.assertFalse(get.call_args.kwargs["allow_redirects"])
        self.assertEqual(set(get.call_args.kwargs["headers"]), {"User-Agent"})

    @patch.object(deezer, "get")
    def test_invalid_artist_ids_and_missing_or_invalid_counts_fail(self, get):
        for artist_id in (0, -1, True, 2.3, "01", "bad", None):
            with self.subTest(id=artist_id), self.assertRaises(ValueError):
                deezer.artist(artist_id)
        get.assert_not_called()
        for fans in (None, "123", "invalid", True, -1, float("nan"), float("inf")):
            get.return_value = {"id": 230, "nb_fan": fans}
            with self.subTest(fans=fans), self.assertRaises(requests.RequestException):
                deezer.artist(230)
        for bad_id in (None, False, 0, 230.0, 4099199):
            get.return_value = {"id": bad_id, "nb_fan": 123}
            with self.subTest(id=bad_id), self.assertRaises(deezer.ArtistUnavailable):
                deezer.artist(230)
        for fans in (0, 123, 123.0):
            get.return_value = {"id": 230, "nb_fan": fans}
            self.assertEqual(deezer.artist(230)["nb_fan"], fans)

    @patch.object(deezer.requests, "get")
    def test_missing_profiles_are_distinct_from_temporary_api_failures(self, get):
        with patch.object(deezer.time, "sleep"):
            for response in (Response(status_code=404), Response(status_code=410), Response(payload={"error": {"code": 800}})):
                get.return_value = response
                for read in (deezer.artist, deezer.top_tracks):
                    with self.assertRaises(deezer.ArtistUnavailable):
                        read(230)
            get.return_value = Response(payload={"error": {"code": 4}})
            with self.assertRaises(requests.RequestException) as failed:
                deezer.artist(230)
            self.assertNotIsInstance(failed.exception, deezer.ArtistUnavailable)
            get.return_value = Response(status_code=302)
            with self.assertRaises(requests.RequestException):
                deezer.artist(230)


class DeezerArtistSelectionTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.counts = dict(FANS)
        self.failures = {}
        self.lookup_patch = patch.object(deezer, "artist", side_effect=self.profile)
        self.lookup = self.lookup_patch.start()
        self.addCleanup(self.lookup_patch.stop)

    def profile(self, artist_id):
        if artist_id in self.failures:
            raise self.failures[artist_id]
        return {"id": artist_id, "name": "Names never decide selection", "nb_fan": self.counts[artist_id]}

    def cache_artist(self, source):
        api_cache.commit_json_responses([musicbrainz.metadata_cache_record(
            f"/artist/{source['id']}", "aliases+url-rels+genres", source,
        )])

    def save_top(self, mbid=KANYE, artist_id=230, age=0):
        row = {"id": 9, "deezer_track_id": 9, "deezer_artist_id": artist_id, "position": 1,
               "title": "Known track", "title_short": "Known track", "duration": 200, "isrc": "USABC2300001",
               "artist": {"id": artist_id, "name": "Fixture Artist"},
               "contributors": [{"name": "Fixture Artist", "role": "Main"}],
               "album": {"id": 2, "title": "Fixture Album"}, "recording_mbid": RECORDING, "release_group_mbid": GROUP}
        value = {"fetched_at": time.time() - age, "resolver_version": summary.RESOLVER_VERSION,
                 "artist_mbid": mbid, "deezer_artist_id": artist_id, "entries": [row]}
        api_cache.set_cache_document(summary.SNAPSHOT_NAMESPACE, f"top_tracks:{mbid}", value, summary.RETENTION_TTL)
        api_cache.set_cache_document(summary.SNAPSHOT_NAMESPACE, f"bio:{mbid}", {
            "fetched_at": time.time(), "bio": {"text": "Preserved biography"},
        }, summary.RETENTION_TTL)
        return value

    def test_kanye_and_ed_select_highest_fan_count(self):
        for mbid, ids, expected in ((KANYE, KANYE_IDS, 230), (ED, ED_IDS, 384236)):
            with self.subTest(mbid=mbid):
                selection = deezer_artist.select(mbid, artist_data(mbid, ids))
                self.assertEqual(selection["deezer_artist_id"], expected)
                self.assertEqual(selection["method"], "deezer_fan_count")
                self.assertTrue(selection["complete"])
        self.assertEqual(self.lookup.call_count, 5)

    def test_single_unique_relationship_is_immediate_without_fan_requests(self):
        source = artist_data(KANYE, [230, 230])
        source["relations"].append({"url": {"resource": "https://deezer.com/us/artist/230"}})
        original = deepcopy(source)
        selected = deezer_artist.select(KANYE, source)
        self.assertEqual(selected["deezer_artist_id"], 230)
        self.assertEqual(selected["method"], "musicbrainz_relationship")
        self.lookup.assert_not_called()
        self.assertEqual(source, original)

    def test_highest_fan_count_wins_independently_of_relationship_order(self):
        for index, ids in enumerate(permutations(KANYE_IDS)):
            source = artist_data(f"order-{index}", ids)
            self.assertEqual(deezer_artist.select(source["id"], source)["deezer_artist_id"], 230)
        self.assertEqual(self.lookup.call_count, 3)  # Fan data is shared, not duplicated per MBID.

    def test_all_candidates_must_have_valid_counts_including_zero(self):
        for index, invalid in enumerate((None, "123", True, -1, float("nan"), float("inf"))):
            self.counts[4099199] = invalid
            selected = deezer_artist.select(f"invalid-{index}", artist_data(f"invalid-{index}"))
            self.assertIsNone(selected["deezer_artist_id"])
            self.assertTrue(selected["provider_failure"])
        self.counts[4099199] = 0
        self.assertEqual(deezer_artist.select(KANYE, artist_data())["deezer_artist_id"], 230)

    def test_one_failed_candidate_never_selects_the_highest_known_count(self):
        self.failures[6013562] = requests.Timeout()
        selected = deezer_artist.select(KANYE, artist_data())
        self.assertIsNone(selected["deezer_artist_id"])
        self.assertTrue(selected["provider_failure"])
        self.assertEqual(self.lookup.call_args_list, [call(value) for value in sorted(KANYE_IDS)])
        self.lookup.reset_mock()
        deezer_artist.select(KANYE, artist_data())
        self.lookup.assert_not_called()  # Short, bounded retry instead of repeated failed comparisons.

    def test_tied_highest_fans_remain_unresolved_instead_of_reusing_previous_winner(self):
        previous = deezer_artist.select(KANYE, artist_data())
        self.counts[4099199] = self.counts[230]
        with patch.object(deezer_artist.time, "time", return_value=previous["expires_at"]):
            selected = deezer_artist.select(KANYE, artist_data())
        self.assertIsNone(selected["deezer_artist_id"])
        self.assertFalse(selected["provider_failure"])

    def test_previously_verified_choice_survives_outage_without_renewing_90_day_expiry(self):
        previous = deezer_artist.select(KANYE, artist_data())
        self.failures[4099199] = requests.Timeout()
        self.lookup.reset_mock()
        with patch.object(deezer_artist.time, "time", return_value=previous["expires_at"]):
            fallback = deezer_artist.select(KANYE, artist_data())
            self.assertEqual(fallback["deezer_artist_id"], 230)
            self.assertTrue(fallback["verified"])
            self.assertFalse(fallback["complete"])
            self.assertEqual(fallback["expires_at"], previous["expires_at"])
            self.lookup.reset_mock()
            deezer_artist.select(KANYE, artist_data())
            self.lookup.assert_not_called()
        self.failures.clear()
        with patch.object(deezer_artist.time, "time", return_value=fallback["retry_at"]):
            recovered = deezer_artist.select(KANYE, artist_data())
            self.assertTrue(recovered["complete"])
            self.assertEqual(self.lookup.call_args_list, [call(4099199)])

    def test_removed_previous_selection_cannot_be_outage_fallback(self):
        previous = deezer_artist.select(KANYE, artist_data())
        self.failures[4099199] = requests.Timeout()
        with patch.object(deezer_artist.time, "time", return_value=previous["expires_at"]):
            selected = deezer_artist.select(KANYE, artist_data(ids=[4099199, 6013562]))
        self.assertIsNone(selected["deezer_artist_id"])
        self.assertFalse(selected["verified"])

    def test_fan_counts_expire_after_exactly_seven_days(self):
        with patch.object(deezer_artist.time, "time", return_value=time.time()):
            previous = deezer_artist.select(KANYE, artist_data())
        self.lookup.reset_mock()
        with patch.object(deezer_artist.time, "time", return_value=previous["checked_at"] + deezer_artist.FAN_TTL - 1):
            deezer_artist.select(KANYE, artist_data(ids=[*KANYE_IDS, 99]))
        self.assertEqual(self.lookup.call_args_list, [call(99)])
        self.lookup.reset_mock()
        with patch.object(deezer_artist.time, "time", return_value=previous["checked_at"] + deezer_artist.FAN_TTL):
            deezer_artist.select(KANYE, artist_data())
        self.assertEqual(self.lookup.call_args_list, [call(value) for value in sorted(KANYE_IDS)])

    def test_selection_revalidates_at_90_days_not_at_fan_cache_expiry(self):
        previous = deezer_artist.select(KANYE, artist_data())
        self.lookup.reset_mock()
        for when in (previous["checked_at"] + deezer_artist.FAN_TTL, previous["expires_at"] - 1):
            with patch.object(deezer_artist.time, "time", return_value=when):
                self.assertEqual(deezer_artist.select(KANYE, artist_data())["deezer_artist_id"], 230)
        self.lookup.assert_not_called()
        with patch.object(deezer_artist.time, "time", return_value=previous["expires_at"]):
            reevaluated = deezer_artist.select(KANYE, artist_data())
        self.assertEqual(self.lookup.call_count, 3)
        self.assertEqual(reevaluated["expires_at"], previous["expires_at"] + 90 * 86400)

    def test_mb_relationship_change_reuses_cached_fans_and_immediately_selects_again(self):
        self.cache_artist(artist_data())
        first = deezer_artist.select(KANYE)
        self.lookup.reset_mock()
        self.cache_artist(artist_data(ids=[4099199, 6013562]))
        with patch.object(musicbrainz, "get", side_effect=AssertionError("Use the existing metadata cache")):
            changed = deezer_artist.select(KANYE)
        self.assertEqual(first["deezer_artist_id"], 230)
        self.assertEqual(changed["deezer_artist_id"], 4099199)
        self.lookup.assert_not_called()

    def test_selection_and_fan_cache_have_finite_storage_ttls(self):
        selected = deezer_artist.select(KANYE, artist_data())
        selection_expiry = api_cache.get_cache_expiry(api_cache.document_cache_key(deezer_artist.SELECTION_NAMESPACE, KANYE))
        fans_expiry = api_cache.get_cache_expiry(api_cache.document_cache_key(deezer_artist.FAN_NAMESPACE, 230))
        self.assertAlmostEqual(selection_expiry - selected["checked_at"], 90 * 86400, delta=1)
        self.assertAlmostEqual(fans_expiry - selected["checked_at"], 7 * 86400, delta=1)

    def test_known_missing_selection_is_reevaluated_and_never_used_as_fallback(self):
        deezer_artist.select(KANYE, artist_data())
        deezer_artist.invalidate(KANYE, 230)
        self.failures[230] = deezer.ArtistUnavailable()
        self.lookup.reset_mock()
        selected = deezer_artist.select(KANYE, artist_data())
        self.assertIsNone(selected["deezer_artist_id"])
        self.assertFalse(selected["verified"])
        self.assertEqual(self.lookup.call_args_list, [call(230)])

    def test_old_ambiguous_mapping_and_lease_do_not_block_deployment_recovery(self):
        self.cache_artist(artist_data())
        api_cache.set_cache_document(summary.IDENTITY_NAMESPACE, f"artist:{KANYE}", {
            "complete": False, "deezer_artist_id": None, "retry_at": time.time() + summary.UNRESOLVED_TTL,
        }, summary.RETENTION_TTL)
        self.save_top(artist_id=None)
        api_cache.set_cache_document(summary.STATE_NAMESPACE, f"top_tracks:resolver-v7:{KANYE}", {
            "status": "failed", "retry_at": time.time() + summary.RETRY_TTL,
        }, summary.RETRY_TTL)
        with patch.object(worker, "Thread"), patch.object(worker, "jobs", Queue(maxsize=32)), patch.object(worker, "_started", False):
            self.assertTrue(worker.request_summary(KANYE)["pending"])
            self.assertEqual(worker.jobs.get_nowait(), (KANYE, "top_tracks"))
        with patch.object(deezer, "top_tracks", return_value=[]) as top:
            worker.process_job(KANYE, "top_tracks")
        top.assert_called_once_with(230)
        self.assertEqual(summary.snapshot(KANYE, "top_tracks")["deezer_artist_id"], 230)
        self.assertTrue(summary.fresh(summary.snapshot(KANYE, "top_tracks"), "top_tracks"))

    def test_changed_selection_refreshes_top_tracks_reusing_successful_track_identities(self):
        self.cache_artist(artist_data())
        deezer_artist.select(KANYE)
        previous = self.save_top()
        details = previous["entries"][0]
        recording = summary._save_identity("track:9", {"recording_mbid": RECORDING, "isrc": details["isrc"]}, True)
        group_key = summary._group_identity_key(details, recording)
        mapping = summary._save_identity(group_key, {"recording_mbid": RECORDING, "release_group_mbid": GROUP}, True)
        self.cache_artist(artist_data(ids=[4099199, 6013562]))
        self.assertFalse(summary.fresh(previous, "top_tracks"))
        api_cache.set_cache_document(summary.STATE_NAMESPACE, summary.refresh_state_key(KANYE, "top_tracks"), {
            "status": "complete",
        }, summary.RETRY_TTL)
        with patch.object(worker, "Thread"), patch.object(worker, "jobs", Queue(maxsize=32)), patch.object(worker, "_started", False):
            self.assertTrue(worker.request_summary(KANYE)["pending"])
            self.assertEqual(worker.jobs.get_nowait(), (KANYE, "top_tracks"))
        with patch.object(deezer, "top_tracks", return_value=[{"id": 9}]) as top, \
                patch.object(deezer, "track", return_value=details), patch.object(summary, "resolve_recording") as resolve, \
                patch.object(musicbrainz, "browse_releases_by_recording") as browse:
            worker.process_job(KANYE, "top_tracks")
        top.assert_called_once_with(4099199)
        result = summary.snapshot(KANYE, "top_tracks")
        self.assertEqual(result["entries"][0]["release_group_mbid"], GROUP)
        self.assertEqual(result["entries"][0]["deezer_artist_id"], 4099199)
        self.assertEqual(api_cache.get_cache_document(summary.IDENTITY_NAMESPACE, "track:9"), recording)
        self.assertEqual(api_cache.get_cache_document(summary.IDENTITY_NAMESPACE, group_key), mapping)
        resolve.assert_not_called()
        browse.assert_not_called()

    def test_missing_single_profile_invalidates_selection_without_fan_lookup_or_losing_snapshot(self):
        self.cache_artist(artist_data(ids=[230]))
        deezer_artist.select(KANYE)
        previous = self.save_top(age=summary.TOP_TRACKS_TTL + 1)
        with patch.object(deezer, "top_tracks", side_effect=deezer.ArtistUnavailable()) as top:
            worker.process_job(KANYE, "top_tracks")
        self.assertIsNone(deezer_artist.cached_selection(KANYE)["deezer_artist_id"])
        self.assertEqual(summary.snapshot(KANYE, "top_tracks"), previous)
        self.assertEqual(worker._state(KANYE, "top_tracks")["status"], "failed")
        with patch.object(worker, "Thread"), patch.object(worker, "jobs", Queue(maxsize=32)), patch.object(worker, "_started", False):
            self.assertFalse(worker.request_summary(KANYE)["pending"])
            self.assertTrue(worker.jobs.empty())
        self.lookup.assert_not_called()
        top.assert_called_once_with(230)

    def test_artist_detail_exposes_selected_deezer_link_and_leaves_spotify_and_raw_relations_unchanged(self):
        source = artist_data()
        spotify = "https://open.spotify.com/artist/fixture-kanye"
        source["relations"].append({"url": {"resource": spotify}})
        original = deepcopy(source)
        self.register()
        with patch("backend.routes.music._musicbrainz_artist_discography", return_value=(source, [])):
            first = self.client.get(f"/api/music/artist/{KANYE}")
            self.assertEqual(first.status_code, 200)
            self.assertEqual(first.get_json()["deezer"], "https://www.deezer.com/artist/230")
            self.assertEqual(first.get_json()["spotify"], spotify)
            self.lookup.reset_mock()
            second = self.client.get(f"/api/music/artist/{KANYE}")
            self.assertEqual(second.get_json()["deezer"], "https://www.deezer.com/artist/230")
            self.lookup.assert_not_called()
        self.assertEqual(source, original)

    def test_changed_selection_invalidates_the_assembled_artist_link(self):
        self.register()
        source = artist_data()
        with patch("backend.routes.music._musicbrainz_artist_discography", return_value=(source, [])):
            self.client.get(f"/api/music/artist/{KANYE}")
            self.client.get(f"/api/music/artist/{KANYE}")  # Retained after the initial selection invalidation.
        self.assertIsNotNone(detail_cache.get(("artist", KANYE)))
        deezer_artist.select(KANYE, artist_data(ids=[4099199, 6013562]))
        self.assertIsNone(detail_cache.get(("artist", KANYE)))

    def test_artist_detail_still_loads_spotify_during_deezer_outage(self):
        source = artist_data()
        spotify = "https://open.spotify.com/artist/fixture-kanye"
        source["relations"].append({"url": {"resource": spotify}})
        self.failures[6013562] = requests.Timeout()
        self.register()
        with patch("backend.routes.music._musicbrainz_artist_discography", return_value=(source, [])):
            response = self.client.get(f"/api/music/artist/{KANYE}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["deezer"], "")
        self.assertEqual(response.get_json()["spotify"], spotify)

    def test_active_top_tracks_lease_is_not_replaced_when_selection_changes(self):
        self.cache_artist(artist_data())
        deezer_artist.select(KANYE)
        self.save_top()
        state = {"status": "pending", "refresh_id": "active-before-change", "pending_until": time.time() + worker.LEASE_TTL}
        api_cache.set_cache_document(summary.STATE_NAMESPACE, summary.refresh_state_key(KANYE, "top_tracks"), state, worker.LEASE_TTL)
        self.cache_artist(artist_data(ids=[4099199, 6013562]))
        with patch.object(worker, "Thread"), patch.object(worker, "jobs", Queue(maxsize=32)), patch.object(worker, "_started", False):
            self.assertTrue(worker.request_summary(KANYE)["pending"])
            self.assertTrue(worker.jobs.empty())
        self.assertEqual(worker._state(KANYE, "top_tracks"), state)

    def test_same_artist_after_revalidation_preserves_fresh_daily_order_without_top_fetch(self):
        selected = deezer_artist.select(KANYE, artist_data())
        self.lookup.reset_mock()
        with patch.object(deezer_artist.time, "time", return_value=selected["expires_at"]):
            self.cache_artist(artist_data())
            previous = self.save_top()
            with patch.object(deezer, "top_tracks") as top, patch.object(deezer, "track") as details:
                refreshed = summary.refresh_top_tracks(KANYE)
            self.assertEqual(refreshed["fetched_at"], previous["fetched_at"])
            self.assertEqual(refreshed["entries"], previous["entries"])
            self.assertTrue(summary.fresh(refreshed, "top_tracks"))
        self.assertEqual(self.lookup.call_count, 3)
        top.assert_not_called()
        details.assert_not_called()

    def test_known_missing_single_profile_cannot_be_trusted_again_until_top_request_proves_recovery(self):
        self.cache_artist(artist_data(ids=[230]))
        deezer_artist.select(KANYE)
        deezer_artist.invalidate(KANYE, 230)
        blocked = deezer_artist.select(KANYE)
        with patch.object(deezer_artist.time, "time", return_value=blocked["retry_at"]):
            self.assertIsNone(deezer_artist.select(KANYE)["deezer_artist_id"])
        # Retry again after the new backoff, using the usual Top Tracks request.
        retry = deezer_artist.cached_selection(KANYE)["retry_at"]
        with patch.object(deezer_artist.time, "time", return_value=retry), patch.object(deezer, "top_tracks", return_value=[]) as top:
            value = summary.refresh_top_tracks(KANYE)
            self.assertEqual(value["deezer_artist_id"], 230)
        top.assert_called_once_with(230)
        self.lookup.assert_not_called()
