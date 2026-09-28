"""Evidence gates for Apple URL and contained-release chart resolution."""

if __package__:
    from ._test_environment import TEST_ROOT
else:
    from _test_environment import TEST_ROOT

import unittest
from unittest.mock import Mock

from backend.services import chart_evidence, chart_fallback


def credit(name, mbid="artist-id", *, canonical=None):
    return [{"name": name, "artist": {"id": mbid, "name": canonical or name}}]


def group(mbid, title, name="Artist", kind="Album", score=100):
    return {"id": mbid, "title": title, "artist-credit": credit(name),
            "primary-type": kind, "score": score}


class ChartEvidenceTests(unittest.TestCase):
    def test_canonical_resource_requires_apple_id_and_original_url_to_agree(self):
        item = {"id": "12345", "url": "https://music.apple.com/us/album/a-slug/12345"}
        self.assertEqual(chart_evidence.canonical_apple_resource(item, "us"),
                         "https://music.apple.com/us/album/12345")
        self.assertEqual(chart_evidence.canonical_apple_resource({**item, "id": "67890"}, "us"), "")
        self.assertEqual(chart_evidence.canonical_apple_resource({**item, "url": item["url"].replace("/us/", "/jp/")}, "us"), "")

    def test_apple_urls_are_batched_and_require_unique_parent(self):
        items = [(1, {"id": "123", "url": "https://music.apple.com/us/album/one/123",
                      "name": "Edition", "artistName": "Artist"}),
                 (2, {"id": "456", "url": "https://music.apple.com/us/album/two/456",
                      "name": "Edition", "artistName": "Artist"})]
        lookup = Mock(return_value={"urls": [
            {"resource": "https://music.apple.com/us/album/123", "relations": [
                {"target-type": "release", "release": {"id": "r1"}}]},
            {"resource": "https://music.apple.com/us/album/456", "relations": [
                {"target-type": "release", "release": {"id": "r2"}},
                {"target-type": "release", "release": {"id": "r3"}}]},
        ]})
        releases = [
            {"id": "r1", "title": "Edition", "artist-credit": credit("Artist"), "release-group": {"id": "g1"}},
            {"id": "r2", "title": "Edition", "artist-credit": credit("Artist"), "release-group": {"id": "g2"}},
            {"id": "r3", "title": "Edition", "artist-credit": credit("Artist"), "release-group": {"id": "g3"}},
        ]
        def search(query, search_type, **_kwargs):
            if search_type == "release":
                return {"releases": releases}
            return {"release-groups": [group(mbid, "Edition") for mbid in ("g1", "g2", "g3")]}
        searched = Mock(side_effect=search)
        result = chart_evidence.collect_apple_url_evidence(items, "us", lookup, searched)
        self.assertEqual(lookup.call_count, 1)
        self.assertEqual(searched.call_count, 2)
        self.assertEqual(chart_evidence.select_apple_url(items[0][1], result[1])["id"], "g1")
        self.assertIsNone(chart_evidence.select_apple_url(items[1][1], result[2]))
        self.assertEqual(result[2]["trace"]["decision"], "multiple_parent_groups")

    def test_url_rejects_title_artist_and_type_conflicts(self):
        item = {"name": "Full Apple Title", "artistName": "Artist"}
        release = {"id": "r", "title": "Other Title", "artist-credit": credit("Artist")}
        evidence = {"trace": {"converged": True}, "group": group("g", "Other Title"),
                    "releases": [release]}
        self.assertIsNone(chart_evidence.select_apple_url(item, evidence))
        release["title"] = item["name"]
        release["artist-credit"] = credit("Someone Else", "other-id")
        self.assertIsNone(chart_evidence.select_apple_url(item, evidence))
        evidence["group"]["primary-type"] = None
        self.assertIsNone(chart_evidence.select_apple_url(item, evidence))

    def test_single_artist_canonical_identity_does_not_weaken_multi_credit(self):
        solo = group("g", "Something", "MILEY")
        solo["artist-credit"] = credit("MILEY", "miley-id", canonical="Miley Cyrus")
        self.assertEqual(chart_fallback.artist_identity(solo, "Miley Cyrus"), (25, "canonical_name"))
        solo["artist-credit"].append({"name": "Guest", "artist": {"id": "guest-id", "name": "Guest"}})
        self.assertEqual(chart_fallback.artist_identity(solo, "Miley Cyrus"), (0, "none"))

    def test_release_title_requires_unique_typed_full_credit_and_high_relevance(self):
        item = {"name": "Album: Edition", "artistName": "Artist"}
        candidate = group("g", "Album", score=100)
        selected, trace = chart_evidence.resolve_release_title(item, Mock(return_value={"release-groups": [candidate]}))
        self.assertEqual(selected["id"], "g")
        self.assertIn('release:"Album: Edition"', trace["query"])
        selected, trace = chart_evidence.resolve_release_title(item, Mock(return_value={"release-groups": [candidate, group("h", "Album", score=98)]}))
        self.assertIsNone(selected)
        self.assertEqual(trace["decision"], "ambiguous")
        selected, trace = chart_evidence.resolve_release_title(item, Mock(return_value={"release-groups": [group("u", "Album", kind=None)]}))
        self.assertIsNone(selected)
        self.assertEqual(trace["decision"], "type_rejected")

    def test_stick_season_edition_uses_release_title_without_a_title_exception(self):
        item = {"name": "Stick Season (Forever)", "artistName": "Noah Kahan"}
        candidate = group("stick-season", "Stick Season", "Noah Kahan")
        search = Mock(return_value={"release-groups": [candidate]})
        selected, trace = chart_evidence.resolve_release_title(item, search)
        self.assertEqual(selected["id"], "stick-season")
        self.assertIn('release:"Stick Season (Forever)"', trace["query"])
        self.assertEqual(chart_fallback._base_title(item["name"]), item["name"])


if __name__ == "__main__":
    unittest.main()
