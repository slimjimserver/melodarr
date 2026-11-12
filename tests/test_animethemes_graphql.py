"""GraphQL wire-format, caching, pagination, and normalization regressions."""

if __package__:
    from ._test_environment import TEST_ROOT as _TEST_ROOT
else:
    from _test_environment import TEST_ROOT as _TEST_ROOT  # noqa: F401

import copy
import json
import unittest
from unittest.mock import Mock, patch

import requests
from flask import Flask

from backend.api_cache import cache_db, init_cache_db
from backend.routes.discovery import blueprint
from backend.services import _animethemes_graphql as graphql
from backend.services import animethemes


def connection(nodes, cursor=None):
    return {"nodes": nodes, "pageInfo": {"hasNextPage": bool(cursor), "endCursor": cursor}}


ANIME = {
    "id": 2028, "slug": "naruto", "title": {"romaji": "Naruto", "english": "Naruto EN", "native": "ナルト"},
    "year": 2002, "season": "Fall", "media_format": "TV", "synopsis": "A ninja story.",
    "images": connection([
        {"facet": "Small Cover", "link": "https://img/small.avif"},
        {"facet": "Large Cover", "link": "https://img/large.jpg"},
    ]),
}
THEME = {
    "id": 1477, "type": "OP", "sequence": 2, "slug": "OP2",
    "song": {
        "id": 1477, "title": {"romaji": "Haruka Kanata", "native": "遥か彼方"},
        "performances": [{"as": "AKFG", "alias": None, "artist": {
            "id": 26, "slug": "akfg", "name": {"main": "Asian Kung-Fu Generation"},
        }}],
    },
    "animethemeentries": [{"id": 1, "episodes": "26-53", "notes": "Version one"}],
}
DETAIL = {
    **ANIME,
    "resources": connection([{"id": 495, "site": "MyAnimeList", "link": "https://myanimelist.net/anime/20", "external_id": 20}]),
    "series": connection([{"id": 118, "name": "Naruto", "slug": "naruto"}]),
    "animethemes": [THEME],
}


class AnimeThemesGraphQLTests(unittest.TestCase):
    def setUp(self):
        init_cache_db()
        with cache_db() as connection:
            connection.execute("DELETE FROM api_cache WHERE cache_key LIKE 'animethemes-%'")
        self.post = self.enterContext(patch.object(graphql.requests, "post"))
        self.enterContext(patch.object(graphql.requests, "get", side_effect=AssertionError("Unexpected REST request")))

    def response(self, data=None, *, errors=None):
        payload = {"data": data}
        if errors:
            payload["errors"] = errors
        response = Mock(status_code=200)
        response.json.return_value = payload
        return response

    def test_search_uses_post_variables_and_caches_by_query_and_limit(self):
        self.post.return_value = self.response({"search": {"anime": [ANIME]}})
        query = 'Naruto "quoted" \\ 日本語'
        results = animethemes.search(query, limit=3)
        self.assertEqual(results[0]["name"], "Naruto")
        self.assertEqual(results[0]["coverArt"], "https://img/large.jpg")
        self.assertEqual(results[0]["format"], "TV")
        self.assertEqual(results[0]["season"], "Fall")
        self.assertEqual(animethemes.search(query, limit=3), results)
        self.post.assert_called_once()
        args, kwargs = self.post.call_args
        self.assertEqual(args, (graphql.URL,))
        self.assertEqual(kwargs["json"]["variables"], {"query": query, "limit": 3})
        self.assertNotIn(query, kwargs["json"]["query"])
        self.assertEqual(kwargs["headers"]["User-Agent"], graphql.USER_AGENT)
        self.assertEqual(kwargs["timeout"], 15)
        animethemes.search(query, limit=4)
        animethemes.search("Bleach", limit=3)
        self.assertEqual(self.post.call_count, 3)

    def test_detail_preserves_ids_credits_external_links_and_history_aliases(self):
        self.post.return_value = self.response({"entity": DETAIL})
        with patch("backend.request_history_search.remember_anime_names") as remember:
            result = animethemes.detail("naruto")
        self.assertEqual((result["id"], result["themes"][0]["id"], result["themes"][0]["song"]["id"]), (2028, 1477, 1477))
        self.assertEqual(result["themes"][0]["song"]["artists"][0]["as"], "AKFG")
        self.assertEqual(result["themes"][0]["episodes"], ["26-53"])
        self.assertEqual(result["resources"][0]["site"], "MyAnimeList")
        self.assertEqual(result["resources"][0]["externalId"], 20)
        self.assertEqual(result["series"], [{"id": 118, "name": "Naruto", "slug": "naruto"}])
        self.assertIn("Naruto EN", remember.call_args.args[0]["synonyms"])
        with cache_db() as connection:
            cached = connection.execute("SELECT value FROM api_cache WHERE cache_key = ?", (graphql.anime_cache_key("naruto"),)).fetchone()
        self.assertEqual(graphql.cached_anime(json.loads(cached[0]))["name"], "Naruto")

    def test_graphql_errors_with_partial_data_are_not_cached(self):
        self.post.side_effect = [
            self.response({"search": {"anime": [ANIME]}}, errors=[{"message": "Upstream failure"}]),
            self.response({"search": {"anime": [ANIME]}}),
        ]
        with self.assertRaises(requests.RequestException):
            animethemes.search("Naruto")
        self.assertEqual(animethemes.search("Naruto")[0]["id"], 2028)
        self.assertEqual(self.post.call_count, 2)

    def test_search_route_returns_results_and_handles_graphql_failure(self):
        app = Flask(__name__)
        app.config.update(TESTING=True, SECRET_KEY="test-secret")
        app.register_blueprint(blueprint)
        self.post.side_effect = [
            self.response({"search": {"anime": [ANIME]}}),
            self.response(None, errors=[{"message": "Internal server error"}]),
        ]
        with patch("backend.security.current_user", return_value={"id": 1}), app.test_client() as client:
            result = client.get("/api/search?type=anime&q=Naruto")
            failure = client.get("/api/search?type=anime&q=Bleach")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.get_json()["results"][0]["name"], "Naruto")
        self.assertEqual(failure.status_code, 502)
        self.assertEqual(failure.get_json(), {"error": "AnimeThemes could not be reached. Try again shortly."})

    def test_invalid_json_missing_data_and_http_errors_propagate(self):
        invalid_json = self.response()
        invalid_json.json.side_effect = ValueError("Invalid JSON")
        http_error = self.response()
        http_error.raise_for_status.side_effect = requests.HTTPError("503")
        for response in (invalid_json, self.response(None), self.response([], errors=None), http_error):
            self.post.return_value = response
            with self.subTest(response=response), self.assertRaises(requests.RequestException):
                animethemes.search("Naruto")

    def test_empty_search_and_missing_entities_are_valid(self):
        self.post.side_effect = [
            self.response({"search": {"anime": []}}),
            self.response({"entity": None}), self.response({"entity": None}), self.response({"entity": None}),
        ]
        self.assertEqual(animethemes.search("Missing"), [])
        self.assertIsNone(animethemes.detail("missing"))
        self.assertIsNone(animethemes.series_detail("missing"))
        self.assertIsNone(animethemes.artist_detail("missing"))

    def test_series_follows_cursor_pages_and_keeps_chronology(self):
        later = {**ANIME, "id": 2, "slug": "later", "year": 2007, "animethemes": [{"id": 2}]}
        earlier = {**ANIME, "animethemes": [{"id": 1}]}
        self.post.side_effect = [
            self.response({"entity": {"id": 118, "name": "Naruto", "slug": "naruto", "anime": connection([later], "next")}}),
            self.response({"entity": {"anime": connection([earlier])}}),
        ]
        result = animethemes.series_detail("naruto")
        self.assertEqual([item["slug"] for item in result["anime"]], ["naruto", "later"])
        self.assertEqual([item["themeCount"] for item in result["anime"]], [1, 1])
        self.assertEqual(self.post.call_args.kwargs["json"]["variables"]["after"], "next")

    def test_detail_completes_theme_song_credit_and_entry_pages(self):
        detail = copy.deepcopy(DETAIL)
        # Two results per page exercise the same production paging logic cheaply.
        detail["animethemes"].append({**copy.deepcopy(THEME), "id": 1478})
        detail["animethemes"][0]["song"]["performances"] *= 2
        detail["animethemes"][0]["animethemeentries"] *= 2
        credit = {"as": None, "alias": "Guest", "artist": {"id": 999, "slug": "guest", "name": {"main": "Guest"}}}
        self.post.side_effect = [
            self.response({"entity": detail}),
            self.response({"entity": {"animethemes": []}}),
            self.response({"entity": {"data": [{"performances": [credit]}]}}),
            self.response({"entity": {"data": [{"animethemeentries": [{"id": 2, "episodes": "54", "notes": None}]}]}}),
        ]
        with patch.object(graphql, "PAGE_SIZE", 2):
            result = animethemes.detail("naruto")
        self.assertEqual(len(result["themes"]), 2)
        self.assertEqual(result["themes"][0]["song"]["artists"][-1]["id"], 999)
        self.assertEqual(result["themes"][0]["episodes"], ["26-53", "54"])
        continuation_queries = [call.kwargs["json"] for call in self.post.call_args_list[1:]]
        self.assertTrue(all(item["variables"]["page"] == 2 for item in continuation_queries))

    def test_artist_completes_pages_deduplicates_songs_and_honors_refresh(self):
        appearance = {"id": 1477, "type": "OP", "sequence": 2, "anime": ANIME}
        song = {"id": 1477, "title": {"romaji": "Haruka Kanata", "native": None}, "animethemes": [appearance]}
        artist = {"id": 26, "slug": "akfg", "name": {"main": "Asian Kung-Fu Generation"}, "performances": [{"song": song}, {"song": song}]}
        self.post.side_effect = [
            self.response({"entity": artist}),
            self.response({"entity": {"performances": []}}),
            self.response({"entity": {**artist, "performances": []}}),
        ]
        with patch.object(graphql, "PAGE_SIZE", 2):
            result = animethemes.artist_detail("akfg")
            self.assertEqual(len(result["anime"][0]["performances"]), 1)
            self.assertEqual(animethemes.artist_detail("akfg"), result)
            self.assertEqual(animethemes.artist_detail("akfg", force_refresh=True)["anime"], [])
        self.assertEqual(self.post.call_count, 3)

    def test_repeating_pages_raise_instead_of_looping_or_silently_truncating(self):
        series = {"id": 118, "name": "Naruto", "slug": "naruto", "anime": connection([], "stuck")}
        self.post.return_value = self.response({"entity": series})
        with self.assertRaises(requests.RequestException):
            animethemes.series_detail("naruto")
        self.assertEqual(self.post.call_count, 2)


if __name__ == "__main__":
    unittest.main()
