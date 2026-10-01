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
from backend import chart_diagnostics
from backend.api_cache import set_cache_document
from backend.storage import save_recommendation_cache, record_request


def entry(title="Chart Album", artist="Artist"):
    return {"name": title, "artistName": artist, "releaseDate": "2026-08-01"}


def group(mbid="matched", title="Chart Album", artist="Artist", released="2026-08-01"):
    return {"id": mbid, "title": title, "primary-type": "Album", "first-release-date": released,
            "artist-credit": [{"name": artist, "artist": {"id": "artist-mbid"}}]}


class AlbumChartTests(DatabaseTestCase):
    @patch("backend.services.charts.chart_fallback.resolve")
    @patch("backend.services.charts.musicbrainz.search")
    @patch("backend.services.charts.cached_json_get")
    def test_diagnostic_rows_reconcile_resolution_without_changing_chart_items(self, get, search, fallback):
        get.return_value = {"feed": {"updated": "today", "results": [
            {**entry(), "id": "apple-1", "url": "https://music.apple.com/album/1"},
            entry(title="Missing"), entry(title="Ambiguous"), entry(title="Wrong type"), entry(),
        ]}}
        search.side_effect = [
            {"release-groups": [group()]}, {"release-groups": []},
            {"release-groups": [group("a", "Ambiguous"), group("b", "Ambiguous")]},
            {"release-groups": [{**group(title="Wrong type"), "primary-type": "Single"}]},
            {"release-groups": [group()]},
        ]
        fallback.side_effect = [(None, {}, "no_match"), (None, {}, "ambiguous"),
                                (None, {}, "invalid_release_type")]
        original_search = search.side_effect
        search.side_effect = list(original_search) + [{"release-groups": []}] * 3
        result = charts.popular_albums()
        report = chart_diagnostics.build_report("us")
        self.assertEqual([item["chartRank"] for item in result["items"]], [1])
        self.assertEqual([row["resolution"] for row in report["rows"]],
                         ["matched", "no_match", "ambiguous", "invalid_release_type", "duplicate_mbid"])
        self.assertEqual(report["rows"][0]["sourceId"], "apple-1")
        self.assertEqual(report["rows"][0]["sourceUrl"], "https://music.apple.com/album/1")
        self.assertEqual(report["summary"]["sourceRows"], 5)
        self.assertEqual(report["summary"]["musicBrainzResolutionLossPercent"], 80)

    @patch("backend.services.charts.musicbrainz.lookup_urls")
    @patch("backend.services.charts.musicbrainz.search")
    @patch("backend.services.charts.cached_json_get")
    def test_refresh_uses_exact_then_batched_url_then_release_title_then_scoring(self, get, search, lookup):
        def source(rank):
            return {**entry(f"Edition {rank}"), "id": str(rank),
                    "url": f"https://music.apple.com/us/album/edition-{rank}/{rank}"}
        get.return_value = {"feed": {"results": [source(rank) for rank in range(1, 5)]}}
        url_group = group("url-group", "Base 2")
        title_group = group("title-group", "Base 3")
        title_group["score"] = 100
        scored_group = group("scored-group", "Edition 4")
        scored_group["score"] = 100
        lookup.return_value = {"urls": [{
            "resource": "https://music.apple.com/us/album/2",
            "relations": [{"target-type": "release", "release": {"id": "release-2"}}],
        }]}

        def search_response(query, search_type, **_kwargs):
            if search_type == "release":
                return {"releases": [{"id": "release-2", "title": "Edition 2",
                                      "artist-credit": url_group["artist-credit"],
                                      "release-group": {"id": "url-group"}}]}
            if query.startswith("rgid:"):
                return {"release-groups": [url_group]}
            if query.startswith("releasegroup:"):
                return {"release-groups": [group("exact-group", "Edition 1")] if "Edition 1" in query else []}
            if query.startswith('release:"'):
                return {"release-groups": [title_group] if "Edition 3" in query else []}
            return {"release-groups": [scored_group] if "Edition 4" in query else []}

        search.side_effect = search_response
        result = charts.popular_albums(force_refresh=True)
        report = chart_diagnostics.build_report("us")
        self.assertEqual([item["id"] for item in result["items"]],
                         ["exact-group", "url-group", "title-group", "scored-group"])
        self.assertEqual([row["evidenceStage"] for row in report["rows"]], [
            "exact_release_group", "apple_url_release", "release_title", "scored_fallback"])
        self.assertEqual(lookup.call_count, 1)
        counts = report["summary"]["requestCounts"]
        self.assertEqual([counts[stage]["logical"] for stage in (
            "exactSearch", "urlLookup", "releaseMapping", "groupHydration",
            "releaseTitleSearch", "scoredFallbackSearch")], [4, 1, 1, 1, 2, 1])

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
        search.assert_called_once()

    @patch("backend.services.charts.musicbrainz.search")
    def test_known_edition_qualifiers_use_strong_fallback_without_changing_exact_path(self, search):
        cases = [
            ("SOUR (Video Version)", "SOUR", "Olivia Rodrigo"),
            ("GUTS (spilled)", "GUTS", "Olivia Rodrigo"),
            ("Midnights (The Til Dawn Edition)", "Midnights", "Taylor Swift"),
            ("Icon (Director's Cut)", "Icon", "Brent Faiyaz"),
        ]
        for source_title, mb_title, artist in cases:
            with self.subTest(source_title=source_title):
                candidate = group(mbid="base", title=mb_title, artist=artist)
                candidate["score"] = 100
                search.reset_mock()
                search.side_effect = [{"release-groups": []}, {"release-groups": []},
                                      {"release-groups": [candidate]}]
                diagnostic = {}
                item = charts.resolve_chart_album(entry(source_title, artist), 1, diagnostic=diagnostic)
                self.assertEqual(item["id"], "base")
                self.assertEqual(diagnostic["exactResolution"], "no_match")
                self.assertEqual(diagnostic["matchMethod"], "fallback")
                self.assertEqual(diagnostic["fallback"]["decision"], "auto_matched")
                self.assertEqual(search.call_count, 3)
                self.assertIn(mb_title, search.call_args.args[0])

    @patch("backend.services.charts.musicbrainz.search")
    def test_soundtrack_punctuation_and_complete_artist_credit(self, search):
        title = "KPop Demon Hunters (Soundtrack from the Netflix Film)"
        artist = "KPop Demon Hunters Cast, HUNTR/X & Saja Boys"
        candidate = group(mbid="soundtrack", title="K‐Pop Demon Hunters: Soundtrack From the Netflix Film",
                          artist=artist)
        candidate["score"] = 100
        search.side_effect = [{"release-groups": []}, {"release-groups": []},
                              {"release-groups": [candidate]}]
        diagnostic = {}
        item = charts.resolve_chart_album(entry(title, artist), 23, diagnostic=diagnostic)
        self.assertEqual(item["id"], "soundtrack")
        self.assertIn("K Pop Demon Hunters", search.call_args.args[0])
        self.assertEqual(diagnostic["fallback"]["candidates"][0]["titleScore"], 60)
        self.assertEqual(diagnostic["fallback"]["candidates"][0]["artistScore"], 25)

    @patch("backend.services.charts.musicbrainz.search")
    def test_cast_credit_can_match_primary_artist_only_with_known_base_title(self, search):
        artist = "Lin-Manuel Miranda, Leslie Odom, Jr., Phillipa Soo, Daveed Diggs & Christopher Jackson"
        candidate = group(mbid="hamilton", title="Hamilton: An American Musical",
                          artist="Lin‐Manuel Miranda")
        candidate["score"] = 100
        search.side_effect = [{"release-groups": []}, {"release-groups": []},
                              {"release-groups": [candidate]}]
        diagnostic = {}
        item = charts.resolve_chart_album(
            entry("Hamilton: An American Musical (Original Broadway Cast Recording)", artist),
            34, diagnostic=diagnostic)
        self.assertEqual(item["id"], "hamilton")
        self.assertEqual(diagnostic["fallback"]["candidates"][0]["artistScore"], 18)
        self.assertEqual(diagnostic["fallback"]["candidates"][0]["titleScore"], 50)

    @patch("backend.services.charts.musicbrainz.search")
    def test_apple_ep_label_can_resolve_only_to_typed_ep(self, search):
        candidate = group(mbid="alimony", title="Alimony", artist="Remy Ma")
        candidate.update({"score": 100, "primary-type": "EP"})
        search.side_effect = [{"release-groups": []}, {"release-groups": []},
                              {"release-groups": [candidate]}]
        diagnostic = {}
        item = charts.resolve_chart_album(entry("Alimony - EP", "Remy Ma"), 70,
                                          diagnostic=diagnostic)
        self.assertEqual(item["id"], "alimony")
        self.assertEqual(diagnostic["fallback"]["candidates"][0]["releaseType"], "EP")
        self.assertEqual(diagnostic["fallback"]["sourceTitle"], "Alimony - EP")
        self.assertEqual(diagnostic["fallback"]["exactResolution"], "no_match")

    @patch("backend.services.charts.musicbrainz.search")
    def test_fallback_never_strips_unknown_parentheses_or_accepts_wrong_artist(self, search):
        candidate = group(mbid="other", title="Blue", artist="Artist")
        candidate["score"] = 100
        search.side_effect = lambda query, *_args, **_kwargs: {
            "release-groups": [] if query.startswith('release:"') else [candidate]}
        self.assertIsNone(charts.resolve_chart_album(entry("Blue (Live)"), 1))
        self.assertIsNone(charts.resolve_chart_album(entry("Blue (Forever)"), 1))
        self.assertIsNone(charts.resolve_chart_album(entry("Blue", artist="Other Artist"), 1))

    @patch("backend.services.charts.musicbrainz.search")
    def test_ambiguous_exact_groups_remain_ambiguous_despite_a_date_bonus(self, search):
        first = group("first", "Big Mama", "Latto", "2026-05-29")
        second = group("second", "Big Mama", "Latto", "2026-05-26")
        first["score"] = second["score"] = 100
        search.return_value = {"release-groups": [first, second]}
        diagnostic = {}
        self.assertIsNone(charts.resolve_chart_album(
            {**entry("Big Mama", "Latto"), "releaseDate": "2026-05-29"}, 48,
            diagnostic=diagnostic))
        self.assertEqual(diagnostic["resolution"], "ambiguous")
        self.assertEqual(diagnostic["fallback"]["decision"], "ambiguous")
        self.assertLess(diagnostic["fallback"]["bestScore"]
                        - diagnostic["fallback"]["runnerUpScore"], 15)

    @patch("backend.services.charts.musicbrainz.search")
    def test_single_and_untyped_groups_remain_unresolved(self, search):
        for release_type in ("Single", None):
            with self.subTest(release_type=release_type):
                candidate = group("same")
                candidate["primary-type"] = release_type
                candidate["score"] = 100
                search.return_value = {"release-groups": [candidate]}
                diagnostic = {}
                self.assertIsNone(charts.resolve_chart_album(entry(), 1, diagnostic=diagnostic))
                self.assertEqual(diagnostic["resolution"], "invalid_release_type")
                self.assertEqual(diagnostic["fallback"]["decision"], "no_acceptable_candidate")

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
        set_cache_document(f"{charts.DIAGNOSTIC_NAMESPACE}:us", "current", {
            "country": "us", "updated": "today", "sourceUrl": charts.CHART_URL,
            "sourceRows": 3, "rows": [
                {"rank": rank, "sourceTitle": mbid, "sourceArtist": "Artist",
                 "sourceId": str(rank), "sourceUrl": "", "resolution": "matched", "matchedMbid": mbid}
                for rank, mbid in enumerate(["owned", "requested", "fresh"], start=1)
            ],
        }, charts.CHART_TTL)
        trace = {}
        feed.current_feed(user_id, charts.with_cached_charts({"candidates": []}), chart_trace=trace)
        self.assertEqual(trace[("us", 1)]["disposition"], "lidarr_available")
        self.assertEqual(trace[("us", 2)]["disposition"], "requested")
        self.assertEqual(trace[("us", 3)]["disposition"], "final_requestable")
        summary = chart_diagnostics.build_report("us", user_id)["summary"]
        self.assertEqual(summary["personallyExcluded"], 2)
        self.assertEqual(summary["finalRequestable"], 1)
        self.assertEqual(summary["primaryExclusionReasons"]["lidarr_available"], 1)
        self.assertEqual(summary["primaryExclusionReasons"]["requested"], 1)
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
        trace = {}
        result = feed.current_feed(user_id, payload, chart_trace=trace)
        self.assertEqual(result["sections"][0]["items"][0]["id"], "same")
        self.assertEqual(result["popularAlbums"], [])
        self.assertEqual(trace[("us", 2)]["disposition"], "featured_elsewhere")

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
