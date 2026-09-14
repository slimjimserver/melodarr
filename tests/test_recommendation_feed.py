"""Personal recommendations, privacy, availability and outcome regressions."""

if __package__:
    from ._test_environment import TEST_ROOT
    from .test_backend import DatabaseTestCase
else:
    from _test_environment import TEST_ROOT
    from test_backend import DatabaseTestCase

import json
import time
import unittest
from unittest.mock import patch
import requests

from backend import recommendations as engine, recommendation_feed as feed
from backend import recommendation_activity as activity
from backend.storage import (db, record_request, save_recommendation_cache, get_recommendation_cache,
                             save_service, init_db, enqueue_lidarr_search)
from backend.workers import recommendations as worker


def album(mbid="new-album", artist="New Artist", **extra):
    return {"id": mbid, "name": "An Album", "artist": artist, "kind": "release-group",
            "type": "Album", "score": 1, "reason": "Because you requested Favorite",
            "recommendationSource": "Your requests", **extra}


class RankingTests(unittest.TestCase):
    def test_discography_requests_do_not_dominate_and_recent_requests_win(self):
        now = time.time()
        bulk = [{"kind": "release-group", "mbid": str(i), "name": str(i),
                 "artist_name": "Bulk", "created_at": now - 180 * feed.DAY} for i in range(100)]
        recent = {"kind": "artist", "mbid": "favorite", "name": "Favorite", "created_at": now}
        seeds = feed.request_seeds(bulk + [recent], now=now)
        self.assertEqual(len(seeds), 2)
        self.assertEqual(seeds[0]["name"], "Favorite")
        self.assertAlmostEqual(seeds[1]["score"], 0.625)

    def test_positive_feedback_is_a_seed_without_claiming_it_was_requested(self):
        rows = [{"action": "more", "item_json": json.dumps(album(artistId="artist-id"))}]
        seeds = feed.request_seeds([], rows)
        self.assertEqual(seeds[0]["id"], "artist-id")
        self.assertEqual(seeds[0]["origin"], "feedback")

    def test_provider_scales_are_normalized_and_agreement_is_rewarded(self):
        items = [album("a", score=10000, recommendationSource="A"),
                 album("b", score=1, recommendationSource="A"),
                 album("b", score=0.9, recommendationSource="B")]
        ranked = feed.rank_candidates(items)
        self.assertEqual(ranked[0]["id"], "b")
        self.assertEqual(set(ranked[0]["sources"]), {"A", "B"})

    def test_merged_legacy_results_retain_independent_provider_ranks(self):
        items = engine._deduplicate_recommendations([
            album("a", score=20000, recommendationSource="A"),
            album("b", score=1, recommendationSource="A"),
            album("b", score=0.9, recommendationSource="B"),
        ])
        self.assertEqual(items[1]["providerRanks"], {"A": 1, "B": 0})
        self.assertEqual(feed.rank_candidates(items)[0]["id"], "b")

    def test_repeated_exposure_has_bounded_penalty_and_one_view_is_not_dislike(self):
        item = album()
        baseline = feed.rank_candidates([item])[0]["rankScore"]
        key = ("release-group", "new-album")
        self.assertEqual(feed.rank_candidates([item], exposures={key: 1})[0]["rankScore"], baseline)
        self.assertAlmostEqual(feed.rank_candidates([item], exposures={key: 100})[0]["rankScore"], baseline * 0.7)
        self.assertEqual(feed.rank_candidates([item], [{"kind": key[0], "mbid": key[1], "action": "dismiss"}]), [])

    def test_rows_have_six_picks_with_global_artist_and_duplicate_limits(self):
        items = [album(str(i), artist=f"Artist {i // 4}", lane="familiar" if i < 12 else "discovery")
                 for i in range(30)]
        sections = feed.select_sections(feed.rank_candidates(items))
        picked = [item for row in sections for item in row["items"]]
        self.assertTrue(all(len(row["items"]) <= 6 for row in sections))
        self.assertEqual(len({item["id"] for item in picked}), len(picked))
        for artist in {item["artist"] for item in picked}:
            self.assertLessEqual(sum(item["artist"] == artist for item in picked), 2)

    def test_unused_multi_anchor_request_picks_can_fill_discovery(self):
        items = [album(str(i), artist=f"Artist {i}", lane="requests", seedNames=["A", "B"])
                 for i in range(9)]
        sections = feed.select_sections(feed.rank_candidates(items))
        self.assertEqual([(row["id"], len(row["items"])) for row in sections], [("requests", 6), ("discovery", 3)])

    @patch("backend.recommendation_feed.musicbrainz.get")
    def test_familiar_albums_exclude_owned_live_and_future_but_keep_missing(self, get):
        get.return_value = {"release-groups": [
            {"id": "missing", "title": "Missing", "primary-type": "Album", "first-release-date": "2026-01-01"},
            {"id": "owned", "title": "Owned", "primary-type": "Album", "first-release-date": "2024"},
            {"id": "future", "title": "Future", "primary-type": "Album", "first-release-date": "2999"},
            {"id": "live", "title": "Live", "primary-type": "Album", "first-release-date": "2024", "secondary-types": ["Live"]},
        ]}
        exclusions = engine._empty_exclusions()
        exclusions["album_ids"].add("owned")
        seeds = [{"id": "favorite", "name": "Favorite", "score": 2, "origin": "request", "requestedName": "Favorite album"}]
        result = feed.familiar_albums(seeds, exclusions)
        self.assertEqual([item["id"] for item in result], ["missing"])
        self.assertIn("You requested Favorite album", result[0]["reason"])

    @patch("backend.recommendation_feed.musicbrainz.search")
    def test_ambiguous_artist_names_do_not_seed_unrelated_discographies(self, search):
        search.return_value = {"artists": [{"id": "one", "name": "Common"}, {"id": "two", "name": "Common"}]}
        self.assertFalse(feed.resolve_seed({"id": "", "name": "Common"})["id"])


class PersonalFeedTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.csrf = self.register()
        with self.client.session_transaction() as session:
            self.user_id = session["user_id"]
        self.user = {"id": self.user_id, "listenbrainz_username": None, "lastfm_username": None, "plex_id": None}
        network = patch("requests.sessions.Session.request", side_effect=AssertionError("Unexpected live network"))
        network.start()
        self.addCleanup(network.stop)
        worker.refresh_requested.clear()


    def cache(self, *items):
        payload = {"feedVersion": 3, "candidates": list(items), "sections": feed.select_sections(items)}
        save_recommendation_cache(self.user_id, payload)
        return payload

    def event(self, action, mbid="new-album", **extra):
        return self.client.post("/api/discover/activity", json={"events": [
            {"action": action, "kind": "release-group", "id": mbid, **extra}
        ]}, headers={"X-CSRF-Token": self.csrf})

    @patch("backend.recommendation_feed.familiar_albums")
    @patch("backend.recommendation_feed.engine.seeded_lastfm_recommendations")
    def test_request_only_user_gets_personal_rows_without_linked_accounts(self, similar, familiar):
        record_request(self.user_id, "artist", "favorite", "Favorite")
        save_service("lastfm", {"apiKey": "test-key"})
        similar.return_value = ([], [album(seedNames=["Favorite"])])
        familiar.return_value = [album("missing", artist="Favorite", lane="familiar")]
        payload = feed.build_personal_feed(self.user, {"artists": [], "albums": []}, engine._empty_exclusions())
        self.assertEqual([row["id"] for row in payload["sections"]], ["familiar", "requests"])
        self.assertEqual(similar.call_args.args[0][0]["name"], "Favorite")
        self.assertEqual(payload["sections"][1]["items"][0]["reason"], "Because you requested Favorite")

    @patch("backend.recommendation_feed.familiar_albums", return_value=[])
    @patch("backend.recommendation_feed.engine.seeded_lastfm_recommendations", side_effect=requests.Timeout())
    def test_provider_failure_preserves_other_personal_picks(self, similar, familiar):
        record_request(self.user_id, "artist", "favorite", "Favorite")
        save_service("lastfm", {"apiKey": "test-key"})
        payload = feed.build_personal_feed(self.user, {"albums": [album()]}, engine._empty_exclusions())
        self.assertEqual(payload["requestStatus"], "unavailable")
        self.assertEqual(payload["sections"][0]["items"][0]["id"], "new-album")

    def test_feedback_requires_auth_csrf_and_server_owned_candidate(self):
        self.cache(album())
        response = self.client.post("/api/discover/activity", json={"events": [{"action": "dismiss", "kind": "release-group", "id": "new-album"}]})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.event("dismiss", "someone-elses-album").status_code, 409)
        self.assertEqual(self.event("dismiss", name="Injected title").status_code, 200)
        stored = activity.feedback_for(self.user_id)[0]
        self.assertEqual(json.loads(stored["item_json"])["name"], "An Album")
        with self.client.session_transaction() as session:
            session.clear()
        self.assertEqual(self.client.get("/api/discover/metrics").status_code, 401)

    def test_malformed_batch_does_not_partially_write(self):
        self.cache(album())
        result = self.client.post("/api/discover/activity", headers={"X-CSRF-Token": self.csrf}, json={"events": [
            {"action": "dismiss", "kind": "release-group", "id": "new-album"},
            {"action": [], "kind": "release-group", "id": "new-album"},
        ]})
        self.assertEqual(result.status_code, 400)
        self.assertEqual(activity.feedback_for(self.user_id), [])

    def test_feedback_is_immediate_private_persistent_and_reversible_after_refresh(self):
        self.cache(album())
        self.assertEqual(self.event("dismiss").status_code, 200)
        self.assertTrue(worker.refresh_requested.is_set())
        self.assertEqual(self.client.get("/api/discover").get_json()["sections"], [])
        self.assertEqual(activity.feedback_for(self.user_id + 999), [])
        self.cache()  # Simulate the next refresh removing the dismissed item.
        self.assertEqual(self.event("undo").status_code, 200)
        self.assertEqual(activity.feedback_for(self.user_id), [])

    def test_more_like_this_survives_reload_and_undo(self):
        self.cache(album())
        self.event("more")
        item = self.client.get("/api/discover").get_json()["sections"][0]["items"][0]
        self.assertEqual(item["feedback"], "more")
        self.event("undo")
        item = self.client.get("/api/discover").get_json()["sections"][0]["items"][0]
        self.assertIsNone(item["feedback"])

    def test_daily_impressions_deduplicate_and_metrics_do_not_count_links_as_plays(self):
        self.cache(album())
        for action in ("impression", "impression", "open", "listen"):
            self.assertEqual(self.event(action).status_code, 200)
        result = self.client.get("/api/discover/metrics").get_json()
        self.assertEqual((result["shown"], result["opened"], result["listeningLinksOpened"]), (1, 1, 1))
        self.assertEqual(result["requested"], 0)
        self.assertIsNone(result["played"])
        self.assertEqual(activity.metrics_for(self.user_id + 999)["shown"], 0)

    def test_only_requests_after_exposure_within_seven_days_are_attributed(self):
        now = time.time()
        self.cache(album())
        with patch("backend.storage.time.time", return_value=now - 1):
            record_request(self.user_id, "release-group", "new-album", "An Album")
        with patch("backend.recommendation_activity.time.time", return_value=now):
            self.event("impression")
        self.assertEqual(activity.metrics_for(self.user_id)["requested"], 0)
        with patch("backend.storage.time.time", return_value=now + 8 * feed.DAY):
            record_request(self.user_id, "release-group", "new-album", "An Album")
        self.assertEqual(activity.metrics_for(self.user_id)["requested"], 0)
        with patch("backend.storage.time.time", return_value=now + 10):
            record_request(self.user_id, "release-group", "new-album", "An Album")
        self.assertEqual(activity.metrics_for(self.user_id)["requested"], 1)

    @patch("backend.recommendation_activity.get_plex_listens")
    @patch("backend.recommendation_activity.plex.cached_library_index")
    def test_playback_requires_matching_user_album_and_time_after_request(self, index, listens):
        now = time.time()
        self.cache(album())
        with patch("backend.recommendation_activity.time.time", return_value=now - 20):
            activity.record_activity(self.user_id, album(), "impression")
        with patch("backend.storage.time.time", return_value=now - 10):
            record_request(self.user_id, "release-group", "new-album", "An Album")
        save_service("plex", {"machineIdentifier": "server-1"})
        index.return_value = {"releaseGroupsByRatingKey": {"album-key": {"musicbrainzReleaseGroupId": "new-album"}}}
        listens.return_value = [{"artist_rating_key": "artist-key", "album_rating_key": "album-key", "played_at": now - 30}]
        self.assertEqual(activity.metrics_for(self.user_id)["played"], 0)
        listens.return_value[0]["played_at"] = now
        self.assertEqual(activity.metrics_for(self.user_id)["played"], 1)
        self.assertEqual(listens.call_args.args[0], self.user_id)
        self.assertEqual(listens.call_args.kwargs["server_id"], "server-1")

    @patch("backend.recommendation_feed.lidarr.cached_library_availability")
    def test_catalog_presence_is_requestable_but_downloaded_pending_and_requested_are_filtered(self, availability):
        availability.return_value = {"catalog": {"fullyAvailable": False}, "owned": {"fullyAvailable": True}}
        self.cache(*(album(mbid, artist=mbid) for mbid in ["catalog", "owned", "pending", "requested"]))
        enqueue_lidarr_search(self.user_id, "pending", 1, 1, "Pending")
        record_request(self.user_id, "release-group", "requested", "Requested")
        data = self.client.get("/api/discover").get_json()
        self.assertEqual([item["id"] for row in data["sections"] for item in row["items"]], ["catalog"])
        self.assertNotIn("candidates", data)

    @patch("backend.recommendation_feed.plex.cached_library_index")
    def test_newly_available_plex_album_is_removed_using_cached_snapshot(self, index):
        save_service("plex", {"machineIdentifier": "server-1"})
        index.return_value = {"releaseGroupsByMbid": {"new-album": [{}]}}
        self.cache(album())
        self.assertEqual(self.client.get("/api/discover").get_json()["sections"], [])

    @patch("backend.recommendation_feed.familiar_albums", return_value=[])
    def test_refresh_worker_enriches_and_saves_feed_for_user_without_linked_accounts(self, familiar):
        engine.refresh_recommendation_cache()
        payload = json.loads(get_recommendation_cache(self.user_id)["value"])
        self.assertEqual(payload["feedVersion"], feed.FEED_VERSION)
        self.assertEqual(payload["sections"], [])

    @patch("backend.recommendation_feed.lidarr.cached_download_availability")
    def test_another_users_active_download_is_not_recommended(self, downloads):
        downloads.return_value = {"new-album": {"progress": 25}}
        self.cache(album())
        self.assertEqual(self.client.get("/api/discover").get_json()["sections"], [])

    def test_old_exposures_are_pruned_by_background_refresh_even_without_new_activity(self):
        now = time.time()
        with patch("backend.recommendation_activity.time.time", return_value=now - 91 * feed.DAY):
            activity.record_activity(self.user_id, album(), "impression")
        feed.build_personal_feed(self.user, {}, engine._empty_exclusions())
        with db() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM recommendation_exposures").fetchone()[0], 0)

    @patch("backend.recommendation_feed.musicbrainz.get", side_effect=requests.Timeout())
    def test_familiar_catalog_outage_is_visible_and_scheduled_for_retry(self, get):
        record_request(self.user_id, "artist", "favorite", "Favorite")
        payload = feed.build_personal_feed(self.user, {}, engine._empty_exclusions())
        self.assertEqual(payload["catalogStatus"], "unavailable")
        with patch.object(engine, "build_recommendation_cache", return_value={}), patch(
            "backend.recommendation_feed.build_personal_feed", return_value=payload
        ):
            self.assertTrue(engine.refresh_recommendation_cache())

    def test_feedback_schema_is_idempotent_and_cascades_on_user_deletion(self):
        self.cache(album())
        self.event("more")
        self.event("impression")
        init_db()
        self.assertEqual(len(activity.feedback_for(self.user_id)), 1)
        with db() as connection:
            connection.execute("DELETE FROM users WHERE id = ?", (self.user_id,))
        self.assertEqual(activity.feedback_for(self.user_id), [])
        self.assertEqual(activity.metrics_for(self.user_id)["shown"], 0)


if __name__ == "__main__":
    unittest.main()
