"""Taste input privacy and independent chart refresh regressions."""
if __package__:
    from ._test_environment import TEST_ROOT
    from .test_backend import DatabaseTestCase
else:
    from _test_environment import TEST_ROOT
    from test_backend import DatabaseTestCase

import time
from unittest.mock import patch

from backend import recommendation_feed as feed, recommendation_preferences as prefs
from backend import recommendations as engine
from backend.services import charts
from backend.storage import db, record_request, get_request_history, save_recommendation_cache, init_db, save_service
from backend.api_cache import set_cache_document
from backend.workers import charts as chart_worker

ARTIST = {"id": "11111111-1111-4111-8111-111111111111", "name": "Favorite Artist"}


class TastePreferenceTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.csrf = self.register()
        with self.client.session_transaction() as session:
            self.user_id = session["user_id"]
        self.headers = {"X-CSRF-Token": self.csrf}
        network = patch("requests.sessions.Session.request", side_effect=AssertionError("Unexpected live network"))
        network.start()
        self.addCleanup(network.stop)

    def save(self, mode="balanced", artists=None):
        return self.client.post("/api/discover/preferences", json={"mode": mode, "starterArtists": artists or []}, headers=self.headers)

    def test_defaults_and_preferences_survive_database_reinitialization(self):
        self.assertEqual(self.client.get("/api/discover/preferences").get_json()["mode"], "balanced")
        self.assertEqual(self.save("discovery", [ARTIST]).status_code, 200)
        init_db()
        result = self.client.get("/api/discover/preferences").get_json()
        self.assertEqual(result["mode"], "discovery")
        self.assertEqual(result["starterArtists"], [ARTIST])
        self.assertEqual(result["revision"], 1)

    def test_invalid_preferences_do_not_replace_existing_values(self):
        self.save("familiar", [ARTIST])
        for body in ({"mode": []}, {"mode": "anything"}, {"mode": "balanced", "starterArtists": [ARTIST] * 6},
                     {"mode": "balanced", "starterArtists": [ARTIST, ARTIST]},
                     {"mode": "balanced", "starterArtists": [{"id": "not-an-id", "name": "Wrong"}]}):
            response = self.client.post("/api/discover/preferences", json=body, headers=self.headers)
            self.assertEqual(response.status_code, 400)
        self.assertEqual(prefs.preferences_for(self.user_id)["mode"], "familiar")

    def test_excluded_request_remains_in_history_and_availability_exclusions(self):
        record_request(self.user_id, "release-group", "gift", "Gift Album", artist_name="Gift Artist")
        request_id = get_request_history(self.user_id)[0]["id"]
        result = self.client.post("/api/discover/request-influence", headers=self.headers,
            json={"requestId": request_id, "useForRecommendations": False})
        self.assertEqual(result.status_code, 200)
        history = get_request_history(self.user_id)
        self.assertEqual(len(history), 1)
        self.assertEqual(feed.request_seeds(history), [])
        self.assertIn("gift", engine._recommendation_exclusions({"id": self.user_id}, engine._empty_exclusions())["album_ids"])
        profile = self.client.get("/api/account/profile").get_json()["requests"]["release-group"][0]
        self.assertEqual(profile["id"], request_id)
        self.assertEqual(profile["use_for_recommendations"], 0)
        self.client.post("/api/discover/request-influence", headers=self.headers,
            json={"requestId": request_id, "useForRecommendations": True})
        self.assertEqual(feed.request_seeds(get_request_history(self.user_id))[0]["name"], "Gift Artist")

    def test_only_selected_history_row_is_excluded(self):
        record_request(self.user_id, "artist", "one", "Same Artist")
        record_request(self.user_id, "artist", "one", "Same Artist")
        rows = get_request_history(self.user_id)
        prefs.set_request_influence(self.user_id, rows[0]["id"], False)
        self.assertEqual(len(feed.request_seeds(get_request_history(self.user_id))), 1)
        self.assertEqual(get_request_history(self.user_id)[1]["use_for_recommendations"], 1)

    def test_cannot_change_another_users_request_or_preferences(self):
        with db() as connection:
            other = connection.execute("INSERT INTO users(username,password_hash,role,created_at) VALUES ('other','unused','user',?)", (time.time(),)).lastrowid
        record_request(other, "artist", "other-artist", "Other Artist")
        request_id = get_request_history(other)[0]["id"]
        response = self.client.post("/api/discover/request-influence", headers=self.headers,
            json={"requestId": request_id, "useForRecommendations": False})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(get_request_history(other)[0]["use_for_recommendations"], 1)
        self.client.post("/api/discover/preferences?username=other", headers=self.headers,
            json={"mode": "discovery", "starterArtists": [ARTIST]})
        self.assertEqual(prefs.preferences_for(other)["mode"], "balanced")
        self.assertEqual(prefs.preferences_for(self.user_id)["mode"], "discovery")

    def test_preference_mutations_require_csrf_and_authentication(self):
        self.assertEqual(self.client.post("/api/discover/preferences", json={"mode": "discovery"}).status_code, 403)
        with self.client.session_transaction() as session:
            session.clear()
        self.assertEqual(self.client.get("/api/discover/preferences").status_code, 401)
        self.assertEqual(self.client.get("/api/discover/charts").status_code, 401)

    @patch("backend.recommendation_feed.familiar_albums", return_value=[])
    @patch("backend.recommendation_feed.engine.seeded_lastfm_recommendations", return_value=([], []))
    def test_favorite_artists_seed_catalogs_and_similarity_without_requests(self, similar, familiar):
        self.save("balanced", [ARTIST])
        save_service("lastfm", {"apiKey": "test"})
        payload = feed.build_personal_feed({"id": self.user_id}, {}, engine._empty_exclusions())
        self.assertEqual(familiar.call_args.args[0][0]["id"], ARTIST["id"])
        self.assertEqual(similar.call_args.args[0][0]["name"], ARTIST["name"])
        self.assertEqual(payload["tasteRevision"], 1)

    def test_mix_changes_section_order_and_distribution(self):
        items = [{"id": f"{lane}-{i}", "name": f"{lane}-{i}", "artist": f"{lane}-artist-{i}", "kind": "release-group", "lane": lane}
                 for lane in ("familiar", "requests", "discovery") for i in range(10)]
        familiar = feed.select_sections(items, "familiar")
        discovery = feed.select_sections(items, "discovery")
        self.assertEqual(familiar[0]["id"], "familiar")
        self.assertEqual(discovery[0]["id"], "discovery")
        self.assertEqual({s["id"]: len(s["items"]) for s in familiar}, {"familiar": 8, "requests": 6, "discovery": 4})
        self.assertEqual({s["id"]: len(s["items"]) for s in discovery}, {"familiar": 4, "requests": 6, "discovery": 8})

    def test_stale_worker_build_cannot_restore_old_taste_after_save(self):
        self.save("discovery", [ARTIST])
        save_recommendation_cache(self.user_id, {"feedVersion": feed.FEED_VERSION, "tasteRevision": 0,
            "candidates": [{"id": "old", "kind": "artist", "name": "Old Artist"}]})
        result = self.client.get("/api/discover").get_json()
        self.assertTrue(result["pending"])
        self.assertEqual(result["sections"], [])
        self.assertEqual(result["tastePreferences"]["mode"], "discovery")

    @patch("backend.services.charts.popular_albums", side_effect=AssertionError("Chart refresh must be independent"))
    def test_personal_build_and_discover_do_not_fetch_charts(self, popular):
        payload = feed.build_personal_feed({"id": self.user_id}, {}, engine._empty_exclusions())
        self.assertNotIn("popularCandidates", payload)
        save_recommendation_cache(self.user_id, payload)
        result = self.client.get("/api/discover").get_json()
        self.assertFalse(result["pending"])
        self.assertTrue(result["popularCharts"]["jp"]["pending"])
        self.assertEqual(self.client.get("/api/discover/charts").status_code, 200)
        popular.assert_not_called()

    def test_charts_arrive_without_rebuilding_personal_feed(self):
        payload = {"feedVersion": feed.FEED_VERSION, "candidates": [{"id": "personal", "name": "Personal", "kind": "artist"}]}
        save_recommendation_cache(self.user_id, payload)
        before = self.client.get("/api/discover").get_json()
        item = {"id": "jp", "name": "Japan Album", "kind": "release-group", "lane": "popular"}
        set_cache_document(charts.CACHE_NAMESPACE + ":jp", "current", {"items": [item], "status": "ok"}, 100)
        after = self.client.get("/api/discover").get_json()
        self.assertEqual(before["sections"], after["sections"])
        self.assertEqual(before["refreshedAt"], after["refreshedAt"])
        self.assertEqual(after["popularAlbumsByCountry"]["jp"][0]["id"], "jp")
        self.assertTrue(after["popularCharts"]["us"]["pending"])

    def test_charts_are_available_before_first_personal_build(self):
        item = {"id": "chart", "name": "Chart", "kind": "release-group", "lane": "popular"}
        set_cache_document(charts.CACHE_NAMESPACE, "current", {"items": [item], "status": "ok"}, 100)
        result = self.client.get("/api/discover").get_json()
        self.assertTrue(result["pending"])
        self.assertEqual(result["popularAlbums"][0]["id"], "chart")
        response = self.client.post("/api/discover/activity", headers=self.headers,
            json={"events": [{"id": "chart", "kind": "release-group", "action": "dismiss"}]})
        self.assertEqual(response.status_code, 200)

    @patch("backend.workers.charts.Event")
    @patch("backend.workers.charts.charts.popular_albums", side_effect=[RuntimeError("outage"), {"items": []}])
    def test_chart_loop_survives_failure_and_retries_same_country(self, popular, event):
        event.return_value.wait.side_effect = [None, None, KeyboardInterrupt]
        with self.assertRaises(KeyboardInterrupt), self.assertLogs("backend.workers.charts", level="ERROR"):
            chart_worker.run("jp")
        self.assertEqual([call.args for call in popular.call_args_list], [("jp",), ("jp",)])

    def test_deleting_account_removes_taste_preferences(self):
        self.save("discovery", [ARTIST])
        with db() as connection:
            connection.execute("DELETE FROM users WHERE id=?", (self.user_id,))
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM recommendation_preferences").fetchone()[0], 0)

    def test_legacy_personal_cache_still_filters_shared_charts(self):
        save_recommendation_cache(self.user_id, {"artists": [], "albums": []})
        item = {"id": "requested", "kind": "release-group", "name": "Requested", "lane": "popular"}
        record_request(self.user_id, "release-group", "requested", "Requested")
        set_cache_document(charts.CACHE_NAMESPACE, "current", {"items": [item], "status": "ok"}, 100)
        result = self.client.get("/api/discover").get_json()
        self.assertNotIn("popularCandidates", result)
        self.assertEqual(result["popularAlbums"], [])
