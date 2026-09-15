"""Public chart mapping, shared caching, and personal availability checks."""

if __package__:
    from ._test_environment import TEST_ROOT
    from .test_backend import DatabaseTestCase
else:
    from _test_environment import TEST_ROOT
    from test_backend import DatabaseTestCase

from datetime import date
from unittest.mock import patch
import requests

from backend.services import charts
from backend import recommendation_feed as feed
from backend.api_cache import set_cache_document
from backend.storage import save_recommendation_cache, record_request


def entry(title="Chart Album", artist="Artist"):
    return {"name": title, "artistName": artist, "releaseDate": "2026-08-01"}


def group(mbid="matched", title="Chart Album", artist="Artist", released="2026-08-01"):
    return {"id": mbid, "title": title, "primary-type": "Album", "first-release-date": released,
            "artist-credit": [{"name": artist, "artist": {"id": "artist-mbid"}}]}


class AlbumChartTests(DatabaseTestCase):
    @patch("backend.services.charts.musicbrainz.search")
    def test_exact_identity_maps_album_and_preserves_original_chart_rank(self, search):
        search.return_value = {"release-groups": [group()]}
        item = charts.resolve_chart_album(entry(), 17, today=date(2026, 9, 6))
        self.assertEqual(item["id"], "matched")
        self.assertEqual(item["artistId"], "artist-mbid")
        self.assertEqual(item["chartRank"], 17)
        self.assertTrue(item["recentRelease"])
        self.assertIn("#17", item["reason"])
        self.assertIn("size=card", item["coverArt"])
        self.assertEqual(search.call_args.kwargs["priority"], "background")

    @patch("backend.services.charts.musicbrainz.search")
    def test_wrong_artist_or_ambiguous_album_is_never_requestable(self, search):
        for results in ([group(artist="Wrong Artist")], [group(), group(mbid="ambiguous")]):
            search.return_value = {"release-groups": results}
            self.assertIsNone(charts.resolve_chart_album(entry(), 1))

    @patch("backend.services.charts.musicbrainz.search")
    def test_deluxe_title_maps_without_making_an_old_album_a_new_release(self, search):
        search.return_value = {"release-groups": [group(released="2010-01-01")]}
        item = charts.resolve_chart_album(entry(title="Chart Album (Deluxe Edition)"), 1, today=date(2026, 9, 6))
        self.assertEqual(item["name"], "Chart Album")
        self.assertFalse(item["recentRelease"])

    @patch("backend.services.charts.musicbrainz.search")
    def test_typographic_apostrophes_match_and_future_dates_are_not_recent(self, search):
        search.return_value = {"release-groups": [group(title="I'm Here", released="2999-01-01")]}
        item = charts.resolve_chart_album(entry(title="I’m Here"), 1, today=date(2026, 9, 6))
        self.assertIsNotNone(item)
        self.assertFalse(item["recentRelease"])

    @patch("backend.services.charts.musicbrainz.search")
    def test_solo_artist_does_not_match_a_collaboration_with_same_title(self, search):
        candidate = group()
        candidate["artist-credit"] = [{"name": "Artist", "joinphrase": " & "}, {"name": "Guest"}]
        search.return_value = {"release-groups": [candidate]}
        self.assertIsNone(charts.resolve_chart_album(entry(), 1))

    @patch("backend.services.charts.resolve_chart_album")
    @patch("backend.services.charts.cached_json_get")
    def test_shared_chart_cache_resolves_once_and_deduplicates_editions(self, get, resolve):
        get.return_value = {"feed": {"updated": "Sun, 6 Sep 2026 16:00:00 +0000", "results": [entry(), entry()]}}
        resolve.side_effect = [dict(group(), kind="release-group", name="Chart Album", chartRank=1),
                               dict(group(), kind="release-group", name="Chart Album", chartRank=2)]
        first = charts.popular_albums()
        second = charts.popular_albums()
        self.assertEqual(first, second)
        self.assertEqual(len(first["items"]), 1)
        self.assertEqual(first["items"][0]["chartRank"], 1)
        get.assert_called_once()
        self.assertEqual(resolve.call_count, 2)

    @patch("backend.services.charts.cached_json_get", side_effect=requests.Timeout())
    def test_outage_preserves_dated_chart_and_does_not_retry_for_every_user(self, get):
        previous = {"items": [{"id": "old"}], "updated": "yesterday", "status": "ok"}
        set_cache_document(charts.CACHE_NAMESPACE, "last-success", previous, 1)
        result = charts.popular_albums()
        self.assertTrue(result["stale"])
        self.assertEqual(result["updated"], "yesterday")
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(charts.popular_albums(), result)
        get.assert_called_once()

    @patch("backend.services.charts.resolve_chart_album", side_effect=requests.Timeout())
    @patch("backend.services.charts.cached_json_get")
    def test_mapping_outage_stops_after_three_failed_requests(self, get, resolve):
        get.return_value = {"feed": {"results": [entry() for _ in range(100)]}}
        result = charts.popular_albums()
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(resolve.call_count, 3)

    @patch("backend.services.charts.cached_json_get", return_value={"feed": {"results": []}})
    def test_malformed_feed_is_not_presented_as_a_successful_empty_chart(self, get):
        self.assertEqual(charts.popular_albums()["status"], "unavailable")

    @patch("backend.recommendation_feed.lidarr.cached_library_availability")
    def test_chart_cards_share_availability_feedback_and_request_tracking(self, availability):
        csrf = self.register()
        with self.client.session_transaction() as session:
            user_id = session["user_id"]
        items = [{"id": mbid, "kind": "release-group", "name": mbid, "artist": "Artist", "lane": "popular", "chartRank": rank}
                 for rank, mbid in enumerate(["owned", "requested", "fresh"], start=1)]
        save_recommendation_cache(user_id, {"feedVersion": 3, "candidates": [], "popularCandidates": items})
        set_cache_document(charts.CACHE_NAMESPACE, "current", {"items": items, "status": "ok"}, charts.CHART_TTL)
        availability.return_value = {"owned": {"fullyAvailable": True}}
        record_request(user_id, "release-group", "requested", "requested")
        result = self.client.get("/api/discover").get_json()
        self.assertEqual([item["id"] for item in result["popularAlbums"]], ["fresh"])
        self.assertNotIn("popularCandidates", result)
        response = self.client.post("/api/discover/activity", headers={"X-CSRF-Token": csrf}, json={
            "events": [{"id": "fresh", "kind": "release-group", "action": "dismiss"}]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/api/discover").get_json()["popularAlbums"], [])

    @patch("backend.services.charts.cached_popular_albums")
    def test_users_without_linked_accounts_still_get_public_chart_picks(self, popular):
        self.register()
        with self.client.session_transaction() as session:
            user_id = session["user_id"]
        popular.return_value = {"items": [{"id": "chart", "name": "Chart", "kind": "release-group", "lane": "popular"}], "status": "ok"}
        payload = feed.build_personal_feed({"id": user_id}, {}, {"artist_ids": set(), "artist_names": set(), "album_ids": set(), "album_names": set()})
        result = self.client.get("/api/discover").get_json()
        self.assertEqual(result["popularAlbums"][0]["id"], "chart")
        self.assertEqual(payload["sections"], [])

    def test_an_album_already_featured_personally_is_not_repeated_in_the_chart(self):
        self.register()
        with self.client.session_transaction() as session:
            user_id = session["user_id"]
        item = {"id": "same", "kind": "release-group", "name": "Same", "artist": "Artist"}
        payload = {"candidates": [item], "popularCandidates": [{**item, "lane": "popular", "chartRank": 2}]}
        result = feed.current_feed(user_id, payload)
        self.assertEqual(result["sections"][0]["items"][0]["id"], "same")
        self.assertEqual(result["popularAlbums"], [])

    @patch("backend.services.charts.musicbrainz.search")
    @patch("backend.services.charts.cached_json_get")
    def test_japan_uses_its_own_feed_cache_and_rank(self, get, search):
        get.side_effect = [
            {"feed": {"results": [entry()], "updated": "US date"}},
            {"feed": {"results": [entry(title="光", artist="宇多田ヒカル")], "updated": "Japan date"}},
        ]
        search.side_effect = [{"release-groups": [group()]},
                              {"release-groups": [group("japanese", "光", "宇多田ヒカル")]}]
        us = charts.popular_albums("us")
        japan = charts.popular_albums("jp")
        self.assertEqual(japan["items"][0]["id"], "japanese")
        self.assertEqual(japan["items"][0]["chartCountry"], "jp")
        self.assertEqual(japan["items"][0]["reason"], "#1 on Apple Music’s Japan Top 100 albums")
        self.assertIn("/jp/", get.call_args_list[1].args[0])
        self.assertEqual(charts.popular_albums("us"), us)
        self.assertEqual(charts.popular_albums("jp"), japan)
        self.assertEqual(get.call_count, 2)

    @patch("backend.services.charts.cached_json_get", side_effect=requests.Timeout())
    def test_japan_outage_never_falls_back_to_us_chart(self, get):
        set_cache_document(charts.CACHE_NAMESPACE, "last-success", {"items": [{"id": "us"}]}, 1)
        result = charts.popular_albums("jp")
        self.assertEqual(result["items"], [])
        self.assertFalse(result["stale"])
        self.assertEqual(result["country"], "jp")
        self.assertIn("Japan", result["source"])
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(charts.popular_albums("jp"), result)
        get.assert_called_once()

    @patch("backend.services.charts.cached_json_get", side_effect=requests.Timeout())
    def test_japan_outage_keeps_its_own_dated_chart(self, get):
        set_cache_document(charts.CACHE_NAMESPACE + ":jp", "last-success",
                           {"items": [{"id": "jp"}], "updated": "Japan date"}, 1)
        result = charts.popular_albums("jp")
        self.assertEqual(result["items"][0]["id"], "jp")
        self.assertEqual(result["updated"], "Japan date")
        self.assertTrue(result["stale"])

    @patch("backend.services.charts.cached_json_get")
    def test_unsupported_country_is_rejected_before_fetch(self, get):
        with self.assertRaises(ValueError):
            charts.popular_albums("unknown")
        get.assert_not_called()

    @patch("backend.services.charts.cached_popular_albums")
    def test_both_charts_are_filtered_and_japanese_feedback_is_accepted(self, popular):
        csrf = self.register()
        with self.client.session_transaction() as session:
            user_id = session["user_id"]
        popular.side_effect = lambda country: {
            "items": [{"id": mbid, "name": mbid, "artist": "Artist", "kind": "release-group",
                       "lane": "popular", "chartRank": rank}
                      for rank, mbid in enumerate(["shared", country + "-only"], start=1)],
            "status": "ok", "country": country,
        }
        payload = feed.build_personal_feed({"id": user_id}, {},
            {"artist_ids": set(), "artist_names": set(), "album_ids": set(), "album_names": set()})
        self.assertNotIn("popularCharts", payload)
        popular.assert_not_called()
        save_recommendation_cache(user_id, payload)
        record_request(user_id, "release-group", "shared", "shared")
        data = self.client.get("/api/discover").get_json()
        self.assertEqual([item["id"] for item in data["popularAlbumsByCountry"]["us"]], ["us-only"])
        self.assertEqual([item["id"] for item in data["popularAlbumsByCountry"]["jp"]], ["jp-only"])
        self.assertNotIn("popularCandidates", data)
        response = self.client.post("/api/discover/activity", headers={"X-CSRF-Token": csrf}, json={
            "events": [{"id": "jp-only", "kind": "release-group", "action": "dismiss"}]})
        self.assertEqual(response.status_code, 200)
        data = self.client.get("/api/discover").get_json()
        self.assertEqual(data["popularAlbumsByCountry"]["jp"], [])
        self.assertEqual(len(data["popularAlbumsByCountry"]["us"]), 1)
