"""Supplemental metadata must preserve canonical identity, ordering and availability."""

if __package__:
    from ._test_environment import TEST_ROOT
else:
    from _test_environment import TEST_ROOT

import time
from queue import Queue
from unittest.mock import patch

import requests

from backend import api_cache, track_search_index
from backend.services import artist_summary as summary, deezer, musicbrainz, wikipedia
from backend.workers import artist_summary as worker
from tests.test_backend import DatabaseTestCase, Response

ARTIST = "ceb3f5b7-9f1a-430f-afbe-976fdcf30482"
SWAE = "26e09d6c-9c5e-49b1-aa1f-b4b547c40b44"
SOLO = "b5c78faa-c96b-4a7e-969a-ae4939d99196"
SATIVA = "db111065-99aa-4ab1-8d55-95f988f48153"
NICE = "01326a6d-7dc9-4bf3-a9a5-93ce9ba08ac5"
GROUP = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"
RELEASE = "33333333-3333-3333-3333-333333333333"
BEATLES = "b10bbbfc-cf9e-42e0-be17-e2c3e1d2600d"
SUN = "440f60e8-0b25-4ec4-abb1-c6beec624ab0"
ABBEY = "9162580e-5df4-32de-80cc-f45a8d8a9b1d"
ABBEY_US = "2e0542d1-5c0b-4600-ab77-64870cc619de"
ABBEY_EUROPE = "d605cd91-5a6a-4bcb-89c6-e545dc313729"


def credit(name="Jhené Aiko", mbid=ARTIST):
    return {"name": name, "artist": {"id": mbid, "name": name}}


def track(track_id=408766392):
    return {"id": track_id, "title": "Sativa", "title_short": "Sativa", "title_version": "",
            "isrc": "USUM71710460", "duration": 276, "rank": 1,
            "artist": {"id": 3841221, "name": "Jhené Aiko"},
            "contributors": [{"name": "Jhené Aiko", "role": "Main"}, {"name": "Swae Lee", "role": "Featured"}],
            "album": {"id": 47175652, "title": "Trip"}}


def recording(mbid=SATIVA, featured=True, **changes):
    return {"id": mbid, "title": "Sativa", "length": 276000, "isrcs": ["USUM71710460"],
            "artist-credit": [credit(), *([credit("Swae Lee", SWAE)] if featured else [])], **changes}


def release(group_id=GROUP, recording_id=SATIVA, title="Trip", **changes):
    return {"id": RELEASE, "title": title, "status": "Official", "artist-credit": [credit()],
            "release-group": {"id": group_id, "title": title, "primary-type": "Album", "artist-credit": [credit()]},
            "media": [{"tracks": [{"recording": {"id": recording_id}}]}], **changes}


def beatles_track(album_title="Abbey Road (Remastered)"):
    return {"id": 116348464, "title": "Here Comes The Sun (Remastered 2009)",
            "title_short": "Here Comes The Sun", "title_version": "(Remastered 2009)",
            "isrc": "GBAYE0601696", "duration": 184, "artist": {"id": 1, "name": "The Beatles"},
            "contributors": [{"name": "The Beatles", "role": "Main"}],
            "album": {"id": 12047952, "title": album_title, "release_date": "2015-12-24"}}


def abbey_release(release_id=ABBEY_US, group_id=ABBEY, title="Abbey Road", disambiguation="2009 stereo remaster"):
    return {"id": release_id, "title": title, "disambiguation": disambiguation,
            "date": "2009-09-09", "status": "Official", "artist-credit": [credit("The Beatles", BEATLES)],
            "release-group": {"id": group_id, "title": title, "primary-type": "Album",
                              "artist-credit": [credit("The Beatles", BEATLES)], "secondary-types": []},
            "media": [{"tracks": [{"recording": {"id": SUN}}]}]}


class ArtistSummaryTests(DatabaseTestCase):
    def save_snapshot(self, source="top_tracks", age=0, **changes):
        value = {"fetched_at": time.time() - age, "resolver_version": summary.RESOLVER_VERSION,
                 "entries": [{"deezer_track_id": 7, "title": "Old"}], **changes}
        api_cache.set_cache_document(summary.SNAPSHOT_NAMESPACE, f"{source}:{ARTIST}", value, summary.RETENTION_TTL)
        return value

    def test_deezer_relationship_is_exact_and_conflicts_are_unresolved(self):
        relations = [{"url": {"resource": "https://www.deezer.com/us/artist/3841221"}}]
        self.assertEqual(deezer.relationship_id(relations), 3841221)
        self.assertIsNone(deezer.relationship_id(relations + [{"url": {"resource": "https://www.deezer.com/artist/42"}}]))
        for url in ("https://deezer.com.evil.test/artist/3841221", "https://www.deezer.com/album/3841221", "https://api.deezer.com/artist/3841221"):
            self.assertIsNone(deezer.relationship_id([{"url": {"resource": url}}]))

    @patch.object(deezer, "get", return_value={"data": [{"id": 9, "rank": 1}, {"id": 1, "rank": 999}]})
    def test_top_order_is_not_numeric_rank(self, get):
        self.assertEqual([row["id"] for row in deezer.top_tracks(3841221)], [9, 1])
        get.assert_called_once_with("/artist/3841221/top", limit=10)

    @patch.object(deezer.requests, "get")
    def test_public_only_bounded_requests_and_error_objects(self, get):
        get.return_value = Response(payload={"error": {"code": 4}})
        with self.assertRaises(requests.RequestException):
            deezer.track(408766392)
        self.assertEqual(get.call_args.args[0], "https://api.deezer.com/track/408766392")
        self.assertEqual(get.call_args.kwargs["timeout"], (3.05, 10))
        self.assertFalse(get.call_args.kwargs["allow_redirects"])
        self.assertEqual(set(get.call_args.kwargs["headers"]), {"User-Agent"})
        for path in ("/gw-light.php", "/graphql", "/user/me", "/artist/1/bio"):
            with self.assertRaises(ValueError):
                deezer.get(path)
        get.side_effect = requests.Timeout()
        with self.assertRaises(requests.Timeout):
            deezer.track(408766392)

    def test_unique_isrc_nothing_nice_to_say(self):
        self.assertEqual(summary.select_recording({"isrc": "USUM72608325"}, [recording(NICE, title="Nothing Nice to Say")], ARTIST), (NICE, "exact_isrc"))

    def test_sativa_exact_contributors_choose_featured_recording(self):
        self.assertEqual(summary.select_recording(track(), [recording(SOLO, False), recording()], ARTIST), (SATIVA, "isrc_artist_credit"))

    def test_identical_candidate_signals_never_guess(self):
        self.assertEqual(summary.select_recording(track(), [recording(SOLO), recording()], ARTIST), (None, "unresolved"))

    def test_duration_tolerance_inclusive_five_seconds(self):
        for delta in (-5000, 5000):
            self.assertEqual(summary.select_recording(track(), [recording(SOLO, length=310000), recording(length=276000 + delta)], ARTIST), (SATIVA, "isrc_title_duration"))
        self.assertIsNone(summary.select_recording(track(), [recording(SOLO, length=310000), recording(length=281001)], ARTIST)[0])

    def test_fallback_requires_credit_primary_artist_title_version_and_duration(self):
        self.assertEqual(summary.select_recording(track(), [recording()], ARTIST, fallback=True), (SATIVA, "fallback_search"))
        for candidate in (recording(title="Other"), recording(featured=False), recording(length=310000), recording(length=None)):
            self.assertIsNone(summary.select_recording(track(), [candidate], ARTIST, fallback=True)[0])
        self.assertIsNone(summary.select_recording({**track(), "title_version": "Live"}, [recording()], ARTIST, fallback=True)[0])
        self.assertIsNone(summary.select_recording(track(), [recording()], OTHER, fallback=True)[0])

    @patch.object(musicbrainz, "browse_releases_by_recording", return_value=[])
    @patch.object(musicbrainz, "get", return_value={"recordings": [recording(SOLO), recording()]})
    @patch.object(musicbrainz, "search")
    def test_ambiguous_isrc_does_not_fallback_search(self, search, get, browse):
        self.assertEqual(summary.resolve_recording(track(), ARTIST), (None, "unresolved"))
        search.assert_not_called()
        self.assertEqual(get.call_args.args[0], "/isrc/USUM71710460")
        self.assertEqual(get.call_args.kwargs["priority"], "background")

    @patch.object(musicbrainz, "get", side_effect=requests.Timeout())
    @patch.object(musicbrainz, "search")
    def test_isrc_transport_failure_never_becomes_search(self, search, get):
        with self.assertRaises(requests.Timeout):
            summary.resolve_recording(track(), ARTIST)
        search.assert_not_called()

    @patch.object(musicbrainz, "search", return_value={"recordings": [recording()]})
    def test_absent_isrc_conservative_search(self, search):
        self.assertEqual(summary.resolve_recording({**track(), "isrc": None}, ARTIST), (SATIVA, "fallback_search"))
        self.assertIn(f"arid:{ARTIST}", search.call_args.args[0])

    @patch.object(musicbrainz, "get", return_value={"recordings": []})
    @patch.object(musicbrainz, "search", return_value={"recordings": [recording(length=50000)]})
    def test_empty_isrc_fallback_rejects_weak_match(self, search, get):
        self.assertIsNone(summary.resolve_recording(track(), ARTIST)[0])
        search.assert_called_once()

    @patch.object(musicbrainz, "get")
    def test_complete_local_isrc_lookup_avoids_provider(self, get):
        path, inc = "/isrc/USUM71710460", "artist-credits"
        payload = {"isrc": "USUM71710460", "recordings": [recording(SOLO, False), recording()]}
        api_cache.commit_json_responses([musicbrainz.metadata_cache_record(path, inc, payload)])
        track_search_index.index_recording_document(payload, musicbrainz.metadata_cache_key(path, inc))
        self.assertEqual(summary.resolve_recording(track(), ARTIST), (SATIVA, "isrc_artist_credit"))
        get.assert_not_called()

    @patch.object(musicbrainz, "browse_releases_by_recording")
    @patch.object(musicbrainz, "get", return_value={"isrc": "USUM71710460", "recordings": [recording()]})
    def test_unique_isrc_uses_minimal_legal_includes_without_release_requests(self, get, browse):
        self.assertEqual(summary.resolve_recording(track(), ARTIST), (SATIVA, "exact_isrc"))
        get.assert_called_once_with("/isrc/USUM71710460", "artist-credits", priority="background")
        browse.assert_not_called()

    @patch.object(musicbrainz, "browse_releases_by_recording")
    @patch.object(musicbrainz, "get", return_value={"recordings": [recording(SOLO, False), recording()]})
    def test_sativa_resolver_uses_contributors_before_album_context(self, get, browse):
        self.assertEqual(summary.resolve_recording(track(), ARTIST), (SATIVA, "isrc_artist_credit"))
        get.assert_called_once_with("/isrc/USUM71710460", "artist-credits", priority="background")
        browse.assert_not_called()

    @patch.object(musicbrainz, "browse_releases_by_recording")
    @patch.object(musicbrainz, "get", return_value={"recordings": [recording(SOLO, length=310000), recording()]})
    def test_duration_resolves_before_album_context(self, get, browse):
        self.assertEqual(summary.resolve_recording(track(), ARTIST), (SATIVA, "isrc_title_duration"))
        browse.assert_not_called()

    @patch.object(musicbrainz, "get", return_value={"recordings": [recording(SOLO), recording(), recording(NICE, title="Other")]})
    def test_album_context_only_hydrates_remaining_candidates(self, get):
        def browse(mbid, **kwargs):
            if kwargs.get("cache_only"):
                return None
            return [release(recording_id=mbid, title="Trip" if mbid == SATIVA else "Other")]
        with patch.object(musicbrainz, "browse_releases_by_recording", side_effect=browse) as read:
            self.assertEqual(summary.resolve_recording(track(), ARTIST), (SATIVA, "isrc_album_context"))
        self.assertEqual({call.args[0] for call in read.call_args_list}, {SATIVA, SOLO})
        self.assertEqual([call.args[0] for call in read.call_args_list if not call.kwargs.get("cache_only")], [SOLO, SATIVA])
        # Check all candidates locally before the first remote hydration.
        self.assertTrue(all(call.kwargs.get("cache_only") for call in read.call_args_list[:4]))

    @patch.object(musicbrainz, "get", return_value={"recordings": [recording(SOLO), recording()]})
    def test_album_context_reuses_complete_cached_release_collections(self, get):
        def browse(mbid, **kwargs):
            self.assertTrue(kwargs["cache_only"])
            return [release(recording_id=mbid, title="Trip" if mbid == SATIVA else "Other")]
        with patch.object(musicbrainz, "browse_releases_by_recording", side_effect=browse) as read:
            self.assertEqual(summary.resolve_recording(track(), ARTIST), (SATIVA, "isrc_album_context"))
        self.assertEqual(read.call_count, 2)

    @patch.object(musicbrainz, "_http_get", side_effect=AssertionError("All metadata is already cached"))
    def test_minimal_cached_isrc_and_release_pages_resolve_album_and_group_without_network(self, http):
        candidates = [recording(SOLO), recording()]
        for candidate in candidates:
            candidate.pop("isrcs")
        payload = {"isrc": "USUM71710460", "recordings": candidates}
        records = [musicbrainz.metadata_cache_record("/isrc/USUM71710460", "artist-credits", payload)]
        for mbid in (SOLO, SATIVA):
            releases = [release(recording_id=mbid, title="Trip" if mbid == SATIVA else "Other")]
            records.append(musicbrainz.metadata_cache_record(
                "/release", musicbrainz.RECORDING_RELEASE_INCLUDES + "+url-rels",
                {"release-count": 1, "releases": releases}, recording=mbid, limit=100, offset=0,
            ))
        api_cache.commit_json_responses(records)
        track_search_index.index_recording_document(payload, musicbrainz.metadata_cache_key("/isrc/USUM71710460", "artist-credits"))
        value = summary.resolve_track(track(), ARTIST)
        self.assertEqual(value["recording_mbid"], SATIVA)
        self.assertEqual(value["recording_resolution_method"], "isrc_album_context")
        self.assertEqual(value["release_group_mbid"], GROUP)
        http.assert_not_called()

    @patch.object(musicbrainz, "get", return_value={"recordings": [recording(SOLO, False), recording(NICE, False)]})
    @patch.object(musicbrainz, "browse_releases_by_recording")
    def test_conflicting_contributors_do_not_trigger_album_requests(self, browse, get):
        self.assertEqual(summary.resolve_recording(track(), ARTIST), (None, "unresolved"))
        browse.assert_not_called()

    @patch.object(musicbrainz, "get", return_value=None)
    def test_cache_only_release_collection_miss_never_requests_remote_data(self, get):
        self.assertIsNone(musicbrainz.browse_releases_by_recording(SATIVA, cache_only=True))
        self.assertTrue(get.call_args.kwargs["cache_only"])

    @patch.object(musicbrainz, "get", side_effect=[{"release-count": 2, "releases": [release()]}, None])
    def test_partial_cached_release_collection_is_not_complete_album_evidence(self, get):
        self.assertIsNone(musicbrainz.browse_releases_by_recording(SATIVA, cache_only=True))
        self.assertEqual(get.call_args.kwargs["offset"], 1)
        self.assertTrue(all(call.kwargs["cache_only"] for call in get.call_args_list))

    @patch.object(musicbrainz, "get", return_value={"recordings": [recording(SOLO, False), recording()]})
    @patch.object(musicbrainz, "browse_releases_by_recording", return_value=[release()])
    def test_selected_recording_still_resolves_release_group_separately(self, browse, get):
        value = summary.resolve_track(track(), ARTIST)
        self.assertEqual(value["recording_mbid"], SATIVA)
        self.assertEqual(value["release_group_mbid"], GROUP)
        self.assertEqual(value["release_group_resolution_method"], "recording_album_title")
        get.assert_called_once_with("/isrc/USUM71710460", "artist-credits", priority="background")
        browse.assert_called_once_with(SATIVA, priority="background", include_url_relations=True)

    @patch.object(musicbrainz, "get", return_value={"recordings": [recording(SOLO, False), recording()]})
    def test_partial_local_album_is_not_proof_of_unique_isrc(self, get):
        payload = release(media=[{"tracks": [{"recording": recording(SOLO, False)}]}])
        record = musicbrainz.metadata_cache_record(f"/release/{RELEASE}", musicbrainz.RELEASE_TRACK_INCLUDES, payload)
        api_cache.commit_json_responses([record])
        track_search_index.index_release(payload, api_cache.cache_key(record["namespace"], record["url"], record["params"]))
        self.assertEqual(summary.resolve_recording(track(), ARTIST)[0], SATIVA)
        get.assert_called_once()

    def test_release_groups_only_from_exact_recording_and_album_context(self):
        releases = [release(OTHER, NICE), release(title="Compilation"), release()]
        self.assertEqual(summary.select_release_group(SATIVA, track(), releases, ARTIST), (GROUP, "recording_album_title"))

    def test_ambiguous_release_groups_do_not_guess(self):
        self.assertEqual(summary.select_release_group(SATIVA, track(), [release(), release(OTHER)], ARTIST), (None, "unresolved"))

    def test_compilation_and_unrelated_artist_are_rejected(self):
        compilation = release()
        compilation["release-group"]["secondary-types"] = ["Compilation"]
        unrelated = release(artist_credit=[])
        unrelated["release-group"]["artist-credit"] = [credit("Other", OTHER)]
        unrelated["artist-credit"] = [credit("Other", OTHER)]
        self.assertIsNone(summary.select_release_group(SATIVA, track(), [compilation, unrelated], ARTIST)[0])

    def test_direct_deezer_album_relation_is_strongest(self):
        direct = release(title="Trip Deluxe")
        direct["relations"] = [{"url": {"resource": "https://www.deezer.com/album/47175652"}}]
        self.assertEqual(summary.select_release_group(SATIVA, track(), [direct, release(OTHER)], ARTIST), (GROUP, "recording_deezer_album"))

    def test_remaster_release_group_exact_normalized_title_is_unchanged(self):
        self.assertEqual(summary.select_release_group(SUN, beatles_track("ÀBBEY ROAD"), [abbey_release()], BEATLES),
                         (ABBEY, "recording_album_title"))

    def test_deezer_remastered_album_matches_release_remaster_disambiguation(self):
        self.assertEqual(summary.select_release_group(SUN, beatles_track(), [abbey_release()], BEATLES),
                         (ABBEY, "recording_album_remaster"))

    def test_remaster_year_and_word_order_are_equivalent(self):
        for album in ("Abbey Road (Remastered 2009)", "Abbey Road (2009 Remaster)", "Abbey Road (Remaster 2009)"):
            for comment in ("2009 stereo remaster", "remastered 2009", "stereo remaster 2009", "2009 remastered stereo"):
                with self.subTest(album=album, comment=comment):
                    self.assertEqual(summary.select_release_group(SUN, beatles_track(album), [abbey_release(disambiguation=comment)], BEATLES),
                                     (ABBEY, "recording_album_remaster"))
        self.assertEqual(summary.select_release_group(SUN, beatles_track("Abbey Road (Remastered 2009)"),
                         [abbey_release(title="Abbey Road (2009 Remaster)")], BEATLES), (ABBEY, "recording_album_remaster"))

    def test_remaster_match_does_not_require_release_disambiguation(self):
        self.assertEqual(summary.select_release_group(SUN, beatles_track("Abbey Road (Remastered 2009)"),
                         [abbey_release(disambiguation="")], BEATLES), (ABBEY, "recording_album_remaster"))

    def test_multiple_remaster_editions_collapse_to_one_release_group(self):
        releases = [abbey_release(), abbey_release(ABBEY_EUROPE, disambiguation="")]
        for ordered in (releases, list(reversed(releases))):
            self.assertEqual(summary.select_release_group(SUN, beatles_track(), ordered, BEATLES), (ABBEY, "recording_album_remaster"))

    def test_equally_strong_remaster_matches_across_groups_remain_unresolved(self):
        releases = [abbey_release(), abbey_release(ABBEY_EUROPE, OTHER, disambiguation="")]
        for ordered in (releases, list(reversed(releases))):
            self.assertEqual(summary.select_release_group(SUN, beatles_track(), ordered, BEATLES), (None, "unresolved"))

    def test_exact_album_title_beats_remaster_fallback_even_with_later_date_match(self):
        exact = abbey_release(ABBEY_EUROPE, OTHER, title="Abbey Road (Remastered)")
        contextual = abbey_release()
        contextual["date"] = "2015-12-24"
        self.assertEqual(summary.select_release_group(SUN, beatles_track(), [contextual, exact], BEATLES), (OTHER, "recording_album_title"))

    def test_direct_album_relationship_beats_exact_and_remaster_title_matches(self):
        direct = abbey_release(ABBEY_EUROPE, OTHER, title="Different album title")
        direct["relations"] = [{"url": {"resource": "https://www.deezer.com/album/12047952"}}]
        exact = abbey_release(group_id=GROUP, title="Abbey Road (Remastered)")
        self.assertEqual(summary.select_release_group(SUN, beatles_track(), [abbey_release(), exact, direct], BEATLES),
                         (OTHER, "recording_deezer_album"))

    def test_non_remaster_album_parentheses_and_other_versions_are_preserved(self):
        for album in ("Abbey Road (Live)", "Abbey Road (Deluxe)", "Abbey Road (Remixed 2009)",
                      "Abbey Road (Remastered Deluxe)", "Abbey Road (50th Anniversary Edition)",
                      "Abbey Road (Live) (Remastered 2009)", "Abbey Road (Remastered 2009) (Live)",
                      "Abbey Road (2009 Remastered 2015)", "Abbey Road Remastered 2009", "Abbey Road (Mono Remastered)"):
            with self.subTest(album=album):
                self.assertEqual(summary.select_release_group(SUN, beatles_track(album), [abbey_release()], BEATLES), (None, "unresolved"))

    def test_explicit_remaster_year_conflicts_are_not_ignored(self):
        conflicting = [abbey_release(disambiguation="2015 stereo remaster"),
                       abbey_release(title="Abbey Road (Remastered 2015)", disambiguation="")]
        conflicting[1]["release-group"]["title"] = "Abbey Road"
        for candidate in conflicting:
            with self.subTest(candidate=candidate["title"]):
                self.assertEqual(summary.select_release_group(SUN, beatles_track("Abbey Road (Remastered 2009)"), [candidate], BEATLES),
                                 (None, "unresolved"))

    def test_remaster_fallback_preserves_recording_status_artist_and_compilation_guards(self):
        wrong_recording = abbey_release()
        wrong_recording["media"][0]["tracks"][0]["recording"]["id"] = NICE
        wrong_artist = abbey_release()
        wrong_artist["artist-credit"] = [credit("Other", OTHER)]
        wrong_artist["release-group"]["artist-credit"] = [credit("Other", OTHER)]
        rejected = [wrong_recording, wrong_artist]
        for status in ("Bootleg", "Pseudo-release", "Withdrawn", "Cancelled"):
            candidate = abbey_release()
            candidate["status"] = status
            rejected.append(candidate)
        for secondary in ("Compilation", "DJ-mix", "Mixtape/Street"):
            candidate = abbey_release()
            candidate["release-group"]["secondary-types"] = [secondary]
            rejected.append(candidate)
        for candidate in rejected:
            with self.subTest(status=candidate["status"], secondary=candidate["release-group"]["secondary-types"]):
                self.assertEqual(summary.select_release_group(SUN, beatles_track(), [candidate], BEATLES), (None, "unresolved"))

    def test_album_remaster_matching_does_not_weaken_recording_version_matching(self):
        self.assertFalse(summary._title_matches(beatles_track(), {"title": "Here Comes The Sun", "disambiguation": "2009 stereo remaster"}))

    @patch.object(summary, "resolve_recording", side_effect=AssertionError("Keep the successful recording identity"))
    @patch.object(musicbrainz, "browse_releases_by_recording", return_value=[abbey_release(), abbey_release(ABBEY_EUROPE, disambiguation="")])
    @patch.object(deezer, "top_tracks")
    @patch.object(deezer, "track")
    def test_v2_negative_group_and_snapshot_retry_while_recording_and_bio_are_preserved(self, details, top, browse, resolve):
        self.assertEqual(summary.RESOLVER_VERSION, 3)
        api_cache.set_cache_document(summary.IDENTITY_NAMESPACE, "track:116348464", {
            "complete": True, "resolver_version": 2, "isrc": "GBAYE0601696", "recording_mbid": SUN,
            "recording_resolution_method": "exact_isrc",
        }, summary.RETENTION_TTL)
        api_cache.set_cache_document(summary.IDENTITY_NAMESPACE, f"group:{SUN}:12047952:abbey road remastered", {
            "complete": False, "resolver_version": 2, "retry_at": time.time() + summary.UNRESOLVED_TTL,
            "recording_mbid": SUN, "release_group_mbid": None, "release_group_resolution_method": "unresolved",
        }, summary.RETENTION_TTL)
        old = {"fetched_at": time.time(), "resolver_version": 2, "entries": [{
            **beatles_track(), "deezer_track_id": 116348464, "deezer_artist_id": 1, "position": 1,
            "recording_mbid": SUN, "release_group_mbid": None,
        }]}
        bio = {"fetched_at": time.time(), "bio": {"text": "Beatles biography"}}
        api_cache.set_cache_document(summary.SNAPSHOT_NAMESPACE, f"top_tracks:{BEATLES}", old, summary.RETENTION_TTL)
        api_cache.set_cache_document(summary.SNAPSHOT_NAMESPACE, f"bio:{BEATLES}", bio, summary.RETENTION_TTL)
        api_cache.set_cache_document(summary.STATE_NAMESPACE, f"top_tracks:resolver-v2:{BEATLES}", {
            "status": "pending", "pending_until": time.time() + worker.LEASE_TTL,
        }, worker.LEASE_TTL)
        with patch.object(worker, "Thread"), patch.object(worker, "jobs", Queue(maxsize=32)), patch.object(worker, "_started", False):
            self.assertTrue(worker.request_summary(BEATLES)["pending"])
            self.assertEqual(worker.jobs.get_nowait(), (BEATLES, "top_tracks"))
        worker.process_job(BEATLES, "top_tracks")
        repaired = summary.snapshot(BEATLES, "top_tracks")
        self.assertEqual(repaired["entries"][0]["recording_mbid"], SUN)
        self.assertEqual(repaired["entries"][0]["release_group_mbid"], ABBEY)
        self.assertEqual(repaired["entries"][0]["release_group_resolution_method"], "recording_album_remaster")
        self.assertEqual(repaired["resolver_version"], 3)
        self.assertEqual(repaired["fetched_at"], old["fetched_at"])
        self.assertEqual(summary.snapshot(BEATLES, "bio"), bio)
        self.assertTrue(summary.fresh(repaired, "top_tracks"))
        resolve.assert_not_called()
        top.assert_not_called()
        details.assert_not_called()
        browse.assert_called_once_with(SUN, priority="background", include_url_relations=True)

    @patch.object(musicbrainz, "browse_releases_by_recording", return_value=[release()])
    @patch.object(summary, "resolve_recording", return_value=(SATIVA, "isrc_artist_credit"))
    def test_durable_identity_is_reused_and_provenance_saved(self, resolve, browse):
        first = summary.resolve_track(track(), ARTIST)
        self.assertEqual(first["recording_mbid"], SATIVA)
        self.assertEqual(first["release_group_mbid"], GROUP)
        self.assertEqual(summary.resolve_track(track(), ARTIST)["release_group_mbid"], GROUP)
        resolve.assert_called_once()
        browse.assert_called_once_with(SATIVA, priority="background", include_url_relations=True)
        self.assertEqual(first["recording_resolution_method"], "isrc_artist_credit")
        self.assertEqual(first["deezer_album_id"], 47175652)
        self.assertGreater(first["resolved_at"], 0)

    @patch.object(musicbrainz, "browse_releases_by_recording", side_effect=requests.Timeout())
    @patch.object(summary, "resolve_recording", return_value=(SATIVA, "exact_isrc"))
    def test_release_failure_keeps_resolved_recording(self, resolve, browse):
        value = summary.resolve_track(track(), ARTIST)
        self.assertEqual(value["recording_mbid"], SATIVA)
        self.assertIsNone(value["release_group_mbid"])

    @patch.object(musicbrainz, "browse_releases_by_recording", return_value=[release()])
    @patch.object(summary, "resolve_recording", return_value=(SATIVA, "exact_isrc"))
    @patch.object(deezer, "track", side_effect=lambda track_id: track(track_id))
    @patch.object(deezer, "top_tracks", side_effect=[[{"id": value} for value in range(1, 11)], [{"id": value} for value in [*range(1, 9), 11, 12]]])
    @patch.object(summary, "artist_relations", return_value={"relations": [{"url": {"resource": "https://deezer.com/artist/3841221"}}]})
    def test_next_day_eight_known_two_new_only_resolves_new_tracks(self, artist, top, details, resolve, browse):
        summary.refresh_top_tracks(ARTIST)
        self.assertEqual(resolve.call_count, 10)
        summary.refresh_top_tracks(ARTIST)
        self.assertEqual(resolve.call_count, 12)
        self.assertEqual(browse.call_count, 1)
        artist.assert_called_once()

    @patch.object(musicbrainz, "browse_releases_by_recording", return_value=[release()])
    @patch.object(summary, "resolve_recording", side_effect=[(SATIVA, "exact_isrc"), (NICE, "exact_isrc")])
    def test_changed_explicit_isrc_invalidates_old_recording_identity(self, resolve, browse):
        self.assertEqual(summary.resolve_track(track(), ARTIST)["recording_mbid"], SATIVA)
        changed = summary.resolve_track({**track(), "isrc": "USUM72608325"}, ARTIST)
        self.assertEqual(changed["recording_mbid"], NICE)
        self.assertEqual(resolve.call_count, 2)

    @patch.object(musicbrainz, "search")
    def test_local_artist_track_index_can_validate_fallback_without_search(self, search):
        payload = release(media=[{"tracks": [{"recording": recording(), "title": "Sativa"}]}])
        cache_record = musicbrainz.metadata_cache_record(f"/release/{RELEASE}", musicbrainz.RELEASE_TRACK_INCLUDES, payload)
        api_cache.commit_json_responses([cache_record])
        track_search_index.index_release(payload, api_cache.cache_key(cache_record["namespace"], cache_record["url"], cache_record["params"]))
        self.assertEqual(summary.resolve_recording({**track(), "isrc": None}, ARTIST), (SATIVA, "fallback_search"))
        search.assert_not_called()

    @patch.object(worker, "request_summary", side_effect=AssertionError("Must remain lazy"))
    @patch("backend.routes.music._artist_detail_payload", return_value={"id": ARTIST, "name": "Artist", "sections": {}})
    def test_normal_artist_page_does_not_use_supplements(self, payload, read):
        self.register()
        self.assertEqual(self.client.get(f"/api/music/artist/{ARTIST}").status_code, 200)
        read.assert_not_called()

    @patch.object(summary, "resolve_recording", return_value=(None, "unresolved"))
    def test_unresolved_mapping_waits_seven_days_then_retries(self, resolve):
        with patch.object(summary.time, "time", return_value=100):
            summary.resolve_track(track(), ARTIST)
        with patch.object(summary.time, "time", return_value=100 + summary.UNRESOLVED_TTL - 1):
            summary.resolve_track(track(), ARTIST)
        self.assertEqual(resolve.call_count, 1)
        with patch.object(summary.time, "time", return_value=100 + summary.UNRESOLVED_TTL):
            summary.resolve_track(track(), ARTIST)
        self.assertEqual(resolve.call_count, 2)

    def test_http_provider_failures_are_short_backoff_and_recover(self):
        for status in (400, 500):
            with self.subTest(status=status):
                details = track(status)
                response = requests.Response()
                response.status_code = status
                error = requests.HTTPError("https://user:secret@mirror.invalid/?token=secret", response=response)
                with patch.object(summary.time, "time", return_value=100), patch.object(musicbrainz, "get", side_effect=error):
                    with self.assertRaises(requests.HTTPError):
                        summary.resolve_track(details, ARTIST)
                    cached = api_cache.get_cache_document(summary.IDENTITY_NAMESPACE, f"track:{status}")
                    self.assertTrue(cached["provider_failure"])
                    self.assertFalse(cached["complete"])
                    self.assertEqual(cached["retry_at"], 100 + summary.RETRY_TTL)
                    with api_cache.cache_db() as connection:
                        expires_at = connection.execute("SELECT expires_at FROM api_cache WHERE cache_key = ?", (
                            api_cache.document_cache_key(summary.IDENTITY_NAMESPACE, f"track:{status}"),
                        )).fetchone()[0]
                    self.assertEqual(expires_at, 100 + summary.RETRY_TTL)
                with patch.object(summary.time, "time", return_value=100 + summary.RETRY_TTL - 1), patch.object(musicbrainz, "get") as get:
                    self.assertIsNone(summary.resolve_track(details, ARTIST)["recording_mbid"])
                    get.assert_not_called()
                with patch.object(summary.time, "time", return_value=100 + summary.RETRY_TTL), \
                        patch.object(musicbrainz, "get", return_value={"recordings": [recording()]}), \
                        patch.object(musicbrainz, "browse_releases_by_recording", return_value=[release()]):
                    recovered = summary.resolve_track(details, ARTIST)
                    self.assertEqual(recovered["recording_mbid"], SATIVA)
                    self.assertEqual(recovered["release_group_mbid"], GROUP)
                    self.assertFalse(recovered.get("provider_failure", False))

    @patch.object(musicbrainz, "get", return_value={"recording-count": 2, "recordings": [recording()]})
    def test_incomplete_isrc_response_is_provider_failure_not_metadata_unresolved(self, get):
        with self.assertRaises(requests.RequestException):
            summary.resolve_track(track(), ARTIST)
        value = api_cache.get_cache_document(summary.IDENTITY_NAMESPACE, "track:408766392")
        self.assertTrue(value["provider_failure"])
        self.assertLessEqual(value["retry_at"], time.time() + summary.RETRY_TTL)

    @patch.object(musicbrainz, "get", return_value={"recordings": [{"title": "Malformed response without identity"}]})
    def test_malformed_recording_candidate_is_provider_failure(self, get):
        with self.assertRaises(requests.RequestException) as caught:
            summary.resolve_track(track(), ARTIST)
        self.assertEqual(caught.exception.artist_summary_resource, "/isrc/USUM71710460")
        value = api_cache.get_cache_document(summary.IDENTITY_NAMESPACE, "track:408766392")
        self.assertTrue(value["provider_failure"])

    @patch.object(musicbrainz, "browse_releases_by_recording", return_value=[release()])
    @patch.object(musicbrainz, "get", return_value={"recordings": [recording(SOLO, False), recording()]})
    def test_legacy_negative_recording_and_group_mappings_retry_immediately(self, get, browse):
        negative = {"complete": False, "retry_at": time.time() + summary.UNRESOLVED_TTL,
                    "recording_mbid": None, "recording_resolution_method": "unresolved"}
        api_cache.set_cache_document(summary.IDENTITY_NAMESPACE, "track:408766392", negative, summary.RETENTION_TTL)
        api_cache.set_cache_document(summary.IDENTITY_NAMESPACE, f"group:{SATIVA}:47175652:trip", {
            **negative, "release_group_mbid": None,
        }, summary.RETENTION_TTL)
        value = summary.resolve_track(track(), ARTIST)
        self.assertEqual(value["recording_mbid"], SATIVA)
        self.assertEqual(value["release_group_mbid"], GROUP)
        get.assert_called_once()
        browse.assert_called_once()

    @patch.object(musicbrainz, "browse_releases_by_recording", return_value=[release()])
    @patch.object(summary, "resolve_recording")
    def test_legacy_successful_recording_mapping_remains_reusable(self, resolve, browse):
        api_cache.set_cache_document(summary.IDENTITY_NAMESPACE, "track:408766392", {
            "complete": True, "isrc": "USUM71710460", "recording_mbid": SATIVA, "recording_resolution_method": "exact_isrc",
        }, summary.RETENTION_TTL)
        self.assertEqual(summary.resolve_track(track(), ARTIST)["release_group_mbid"], GROUP)
        resolve.assert_not_called()

    @patch.object(musicbrainz, "browse_releases_by_recording", side_effect=requests.Timeout())
    @patch.object(summary, "resolve_recording", return_value=(SATIVA, "exact_isrc"))
    def test_release_provider_failure_has_short_backoff_and_preserves_recording(self, resolve, browse):
        with patch.object(summary.time, "time", return_value=100):
            value = summary.resolve_track(track(), ARTIST)
            self.assertEqual(value["recording_mbid"], SATIVA)
            self.assertTrue(value["provider_failure"])
            self.assertEqual(value["retry_at"], 100 + summary.RETRY_TTL)
            summary.resolve_track(track(), ARTIST)
            browse.assert_called_once()
        browse.side_effect = None
        browse.return_value = [release()]
        with patch.object(summary.time, "time", return_value=100 + summary.RETRY_TTL):
            recovered = summary.resolve_track(track(), ARTIST)
            self.assertEqual(recovered["release_group_mbid"], GROUP)
            self.assertFalse(recovered.get("provider_failure", False))
        resolve.assert_called_once()

    @patch.object(deezer, "top_tracks")
    @patch.object(deezer, "track")
    @patch.object(musicbrainz, "browse_releases_by_recording", return_value=[release()])
    @patch.object(musicbrainz, "get", return_value={"recordings": [recording(SOLO, False), recording()]})
    def test_failed_legacy_snapshot_and_refresh_state_recover_without_cache_clear(self, get, browse, details, top):
        entry = {**track(), "deezer_track_id": 408766392, "deezer_artist_id": 3841221,
                 "position": 1, "recording_mbid": None, "release_group_mbid": None}
        old = self.save_snapshot(resolver_version=None, entries=[entry])
        self.save_snapshot("bio", bio={"text": "Bio"})
        api_cache.set_cache_document(summary.STATE_NAMESPACE, f"top_tracks:{ARTIST}", {
            "status": "pending", "pending_until": time.time() + worker.LEASE_TTL,
        }, worker.LEASE_TTL)
        with patch.object(worker, "Thread"), patch.object(worker, "jobs", Queue(maxsize=32)), patch.object(worker, "_started", False):
            self.assertTrue(worker.request_summary(ARTIST)["pending"])
            self.assertEqual(worker.jobs.get_nowait(), (ARTIST, "top_tracks"))
        worker.process_job(ARTIST, "top_tracks")
        repaired = summary.snapshot(ARTIST, "top_tracks")
        self.assertEqual(repaired["entries"][0]["recording_mbid"], SATIVA)
        self.assertEqual(repaired["entries"][0]["release_group_mbid"], GROUP)
        self.assertEqual(repaired["fetched_at"], old["fetched_at"])
        self.assertTrue(summary.fresh(repaired, "top_tracks"))
        top.assert_not_called()
        details.assert_not_called()

    @patch.object(deezer, "top_tracks", return_value=[{"id": 408766392}])
    @patch.object(deezer, "track", return_value=track())
    @patch.object(summary, "artist_relations", return_value={"relations": [{"url": {"resource": "https://deezer.com/artist/3841221"}}]})
    def test_failed_snapshot_retries_after_backoff_without_refetching_daily_order(self, artist, details, top):
        response = requests.Response()
        response.status_code = 400
        error = requests.HTTPError("https://user:secret@mirror.invalid/?token=secret", response=response)
        with patch.object(summary.time, "time", return_value=100), patch.object(musicbrainz, "get", side_effect=error), \
                self.assertLogs(summary.logger, level="WARNING") as logs:
            failed = summary.refresh_top_tracks(ARTIST)
        line = " ".join(logs.output)
        for expected in ("deezer_track_id=408766392", "isrc=USUM71710460", "http_status=400", "musicbrainz_path=/isrc/USUM71710460"):
            self.assertIn(expected, line)
        self.assertNotIn("secret", line)
        self.assertNotIn("mirror.invalid", line)
        with patch.object(summary.time, "time", return_value=100 + summary.RETRY_TTL - 1):
            self.assertTrue(summary.fresh(failed, "top_tracks"))
        with patch.object(summary.time, "time", return_value=100 + summary.RETRY_TTL), \
                patch.object(musicbrainz, "get", return_value={"recordings": [recording()]}), \
                patch.object(musicbrainz, "browse_releases_by_recording", return_value=[release()]):
            self.assertFalse(summary.fresh(failed, "top_tracks"))
            repaired = summary.refresh_top_tracks(ARTIST)
            self.assertEqual(repaired["entries"][0]["recording_mbid"], SATIVA)
            self.assertEqual(repaired["entries"][0]["release_group_mbid"], GROUP)
            self.assertTrue(summary.fresh(repaired, "top_tracks"))
            self.assertEqual(repaired["fetched_at"], failed["fetched_at"])
        top.assert_called_once()
        details.assert_called_once()

    def test_exact_twenty_four_hour_and_thirty_day_boundaries(self):
        with patch.object(summary.time, "time", return_value=10000000):
            for source, ttl, data in [("top_tracks", 86400, {}), ("bio", 2592000, {"bio": {"text": "Bio"}})]:
                self.assertTrue(summary.fresh({"fetched_at": 10000000 - ttl + 1, **data}, source))
                self.assertFalse(summary.fresh({"fetched_at": 10000000 - ttl, **data}, source))

    @patch.object(worker, "_claim")
    def test_fresh_cache_never_queues_requests(self, claim):
        self.save_snapshot()
        self.save_snapshot("bio", bio={"text": "Bio"})
        self.assertFalse(worker.request_summary(ARTIST)["pending"])
        claim.assert_not_called()

    @patch.object(worker, "Thread")
    def test_stale_snapshot_is_immediate_and_refresh_coalesces(self, thread):
        self.save_snapshot(age=26 * 3600)
        self.save_snapshot("bio", bio={"text": "Bio"})
        with patch.object(worker, "jobs", Queue(maxsize=32)), patch.object(worker, "_started", False):
            value = worker.request_summary(ARTIST)
            self.assertEqual(value["topTracks"][0]["title"], "Old")
            self.assertTrue(value["pending"])
            self.assertTrue(value["sources"]["top_tracks"]["stale"])
            worker.request_summary(ARTIST)
            self.assertEqual(worker.jobs.qsize(), 1)
            self.assertEqual(thread.call_count, 2)

    @patch.object(summary, "refresh_top_tracks", side_effect=requests.Timeout())
    def test_failed_refresh_and_cache_cleanup_preserve_stale_success(self, refresh):
        old = self.save_snapshot(age=26 * 3600)
        worker.process_job(ARTIST, "top_tracks")
        with patch.object(api_cache, "_last_cleanup_at", None):
            api_cache.cleanup_expired_cache()
        self.assertEqual(summary.snapshot(ARTIST, "top_tracks"), old)
        with patch.object(worker, "Thread") as thread:
            self.save_snapshot("bio", bio={"text": "Bio"})
            self.assertFalse(worker.request_summary(ARTIST)["pending"])
            thread.assert_not_called()

    @patch.object(summary, "resolve_track", return_value={"recording_mbid": SATIVA, "release_group_mbid": GROUP})
    @patch.object(deezer, "track", side_effect=lambda track_id: track(track_id))
    @patch.object(deezer, "top_tracks", return_value=[{"id": 10, "rank": 1}, {"id": 9, "rank": 999}])
    @patch.object(summary, "artist_relations", return_value={"relations": [{"url": {"resource": "https://deezer.com/artist/3841221"}}]})
    def test_successful_refresh_replaces_ordered_snapshot(self, artist, top, details, resolve):
        self.save_snapshot(age=26 * 3600)
        worker.process_job(ARTIST, "top_tracks")
        entries = summary.snapshot(ARTIST, "top_tracks")["entries"]
        self.assertEqual([entry["deezer_track_id"] for entry in entries], [10, 9])
        self.assertEqual([entry["position"] for entry in entries], [1, 2])
        self.assertTrue(summary.fresh(summary.snapshot(ARTIST, "top_tracks"), "top_tracks"))

    @patch.object(deezer, "track", side_effect=[requests.Timeout(), track(9)])
    @patch.object(deezer, "top_tracks", return_value=[{"id": 10, "title": "Failed"}, {"id": 9}])
    @patch.object(summary, "artist_relations", return_value={"relations": [{"url": {"resource": "https://deezer.com/artist/3841221"}}]})
    @patch.object(summary, "resolve_track", side_effect=requests.Timeout())
    def test_partial_detail_and_mb_failures_remain_informational(self, resolve, artist, top, details):
        entries = summary.refresh_top_tracks(ARTIST)["entries"]
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["title"], "Failed")
        self.assertTrue(all(entry["release_group_mbid"] is None for entry in entries))

    @patch.object(summary, "artist_relations", return_value={"relations": []})
    @patch.object(wikipedia, "bio", return_value={"text": "Biography", "sourceUrl": "https://en.wikipedia.org/wiki/Artist"})
    def test_wikipedia_successful_cache_and_failure_preserve_bio(self, bio, relations):
        value = summary.refresh_bio(ARTIST)
        self.assertEqual(summary.snapshot(ARTIST, "bio"), value)
        with patch.object(worker, "Thread"):
            self.save_snapshot()
            worker.request_summary(ARTIST)
        self.assertEqual(bio.call_count, 1)
        self.save_snapshot("bio", age=summary.BIO_TTL + 1, bio=value["bio"])
        bio.side_effect = requests.Timeout()
        worker.process_job(ARTIST, "bio")
        self.assertEqual(summary.snapshot(ARTIST, "bio")["bio"], value["bio"])

    @patch.object(wikipedia, "_get")
    def test_wikipedia_direct_relationship_and_wikidata_api(self, get):
        get.return_value = {"query": {"pages": [{"title": "Jhené Aiko", "extract": "Bio text"}]}}
        bio = wikipedia.bio([{"url": {"resource": "https://en.wikipedia.org/wiki/Jhen%C3%A9_Aiko"}}])
        self.assertEqual(bio["text"], "Bio text")
        self.assertEqual(get.call_args.args[0], "https://en.wikipedia.org/w/api.php")
        self.assertEqual(get.call_args.kwargs["explaintext"], 1)
        get.side_effect = [{"entities": {"Q123": {"sitelinks": {"enwiki": {"title": "Artist"}}}}}, {"query": {"pages": [{"title": "Artist", "extract": "Text"}]}}]
        self.assertEqual(wikipedia.bio([{"url": {"resource": "https://www.wikidata.org/wiki/Q123"}}])["text"], "Text")
        self.assertEqual(get.call_args_list[-2].kwargs["action"], "wbgetentities")

    @patch.object(wikipedia, "_get")
    def test_wikipedia_no_fuzzy_search_or_disambiguation_bio(self, get):
        self.assertIsNone(wikipedia.bio([]))
        self.assertIsNone(wikipedia.bio([{"url": {"resource": "https://en.wikipedia.org.evil.test/wiki/Artist"}}]))
        get.assert_not_called()
        get.return_value = {"query": {"pages": [{"extract": "Wrong", "pageprops": {"disambiguation": ""}}]}}
        self.assertIsNone(wikipedia.bio([{"url": {"resource": "https://en.wikipedia.org/wiki/Artist"}}]))

    @patch.object(worker, "request_summary", return_value={"bio": {"text": "Bio"}, "topTracks": [{"release_group_mbid": GROUP}], "pending": False})
    @patch("backend.routes.music._release_group_availability", return_value={GROUP: {"requestStatus": "queued"}})
    def test_route_joins_canonical_context_and_live_state(self, availability, read):
        self.register()
        track_search_index.index_release_groups([release()["release-group"]])
        result = self.client.get(f"/api/music/artist/{ARTIST}/summary")
        self.assertEqual(result.status_code, 200)
        group = result.get_json()["releaseGroups"][GROUP]
        self.assertEqual(group["title"], "Trip")
        self.assertEqual(group["requestStatus"], "queued")
        self.assertIn(GROUP, group["coverArt"])

    @patch.object(worker, "request_summary", side_effect=RuntimeError())
    def test_supplemental_failure_is_not_an_artist_5xx(self, read):
        self.register()
        response = self.client.get(f"/api/music/artist/{ARTIST}/summary")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["topTracks"], [])
        self.assertEqual(self.client.get("/api/music/artist/invalid/summary").status_code, 400)
