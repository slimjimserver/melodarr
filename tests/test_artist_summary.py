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
LET_IT_BE = "0cdc9b5b-b16b-4ff1-9f16-5b4ba76f1c17"
DRAKE = "55555555-5555-5555-5555-555555555555"
HEADLINES = "263560ff-c9da-4498-8da5-1800241f8799"
HEADLINES_SHORT = "53e58043-b6aa-4dd5-b103-3e69f102efac"
LIFE_REMIX = "5e7a3a47-3df6-4ae7-a60d-fb212758ad81"
LIFE_ORIGINAL = "4670caff-59d1-4a26-b26c-35491059756d"
RUBBER_SOUL = "66666666-6666-6666-6666-666666666666"
ARIANA = "f4fdbb4c-e4b7-47a0-b83b-d91bbfcfa387"
PROBLEM = "051da081-084d-448d-aa84-8bfdfd2e3c27"
MY_EVERYTHING = "77777777-7777-7777-7777-777777777777"


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


def let_it_be_track():
    return {**beatles_track(), "id": 2, "title": "Let It Be (Remastered 2009)",
            "title_short": "Let It Be", "isrc": "GBAYE0601713", "duration": 243,
            "album": {"id": 2, "title": "Let It Be (Remastered)", "release_date": "1970-05-08"}}


def let_it_be_release(group_id=GROUP, primary_type="Album", group_date="1970-05-08", release_date="2009-09-09"):
    value = abbey_release(RELEASE, group_id, title="Let It Be")
    value["date"] = release_date
    value["release-group"].update({"primary-type": primary_type, "first-release-date": group_date})
    value["media"][0]["tracks"][0]["recording"]["id"] = LET_IT_BE
    return value


def headlines_track():
    return {"id": 3, "title": "Headlines (Explicit Version)", "title_short": "Headlines",
            "title_version": "(Explicit Version)", "duration": 236, "isrc": "USCM51100290",
            "artist": {"id": 3, "name": "Drake"}, "contributors": [{"name": "Drake", "role": "Main"}],
            "album": {"id": 3, "title": "Take Care (Deluxe)"}}


def headlines_recordings():
    return [{"id": mbid, "title": "Headlines", "disambiguation": comment, "length": length,
             "artist-credit": [credit("Drake", DRAKE)]}
            for mbid, comment, length in ((HEADLINES, "explicit", 235986), (HEADLINES_SHORT, "", 214746))]


def life_track():
    return {**beatles_track(), "id": 4, "title": "In My Life (Remastered 2009)", "title_short": "In My Life",
            "duration": 145, "isrc": "GBAYE0601489",
            "album": {"id": 4, "title": "Rubber Soul (Remastered 2009)", "release_date": "2015-12-24"}}


def life_recordings():
    return [{"id": mbid, "title": "In My Life", "disambiguation": comment, "length": length,
             "artist-credit": [credit("The Beatles", BEATLES)]}
            for mbid, comment, length in ((LIFE_REMIX, "1987 remix", 147000), (LIFE_ORIGINAL, "original stereo studio mix", 146000))]


def rubber_release(recording_id=LIFE_REMIX, comment="2009 stereo remaster", release_date="2015-12-24"):
    value = abbey_release(RELEASE if recording_id == LIFE_REMIX else ABBEY_EUROPE,
                          RUBBER_SOUL, title="Rubber Soul", disambiguation=comment)
    value["date"] = release_date
    value["release-group"]["first-release-date"] = "1965-12-03"
    value["media"][0]["tracks"][0]["recording"]["id"] = recording_id
    return value


def problem_track():
    return {"id": 6, "title": "Problem", "title_short": "Problem", "title_version": "",
            "duration": 194, "isrc": "USUM71405403", "artist": {"id": 6, "name": "Ariana Grande"},
            "contributors": [{"name": "Ariana Grande", "role": "Main"}, {"name": "Iggy Azalea", "role": "Featured"}],
            "album": {"id": 8435726, "title": "My Everything (Deluxe)", "release_date": "2014-08-25"}}


def my_everything_release(title="My Everything", group_id=MY_EVERYTHING, release_id=RELEASE):
    return {"id": release_id, "title": title, "status": "Official", "date": "2014-08-25",
            "artist-credit": [credit("Ariana Grande", ARIANA)],
            "release-group": {"id": group_id, "title": "My Everything", "primary-type": "Album",
                              "first-release-date": "2014-08-25", "secondary-types": [],
                              "artist-credit": [credit("Ariana Grande", ARIANA)]},
            "media": [{"tracks": [{"recording": {"id": PROBLEM}}]}]}


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

    @patch.object(musicbrainz, "browse_releases_by_recording")
    @patch.object(musicbrainz, "get", return_value={"recordings": headlines_recordings()})
    def test_headlines_explicit_base_title_and_duration_resolve_before_album_context(self, get, browse):
        self.assertEqual(summary.resolve_recording(headlines_track(), DRAKE), (HEADLINES, "isrc_title_duration"))
        get.assert_called_once_with("/isrc/USCM51100290", "artist-credits", priority="background")
        browse.assert_not_called()

    def test_explicit_version_and_explicit_have_narrow_semantic_equivalence(self):
        for version in ("(Explicit Version)", "Explicit"):
            for comment in ("explicit", "Explicit Version"):
                with self.subTest(version=version, comment=comment):
                    self.assertTrue(summary._title_matches({**headlines_track(), "title_version": version},
                                                          {**headlines_recordings()[0], "disambiguation": comment}))
        for comment in ("", "clean", "non explicit", "explicit remix", "explicitly edited"):
            self.assertFalse(summary._title_matches(headlines_track(), {**headlines_recordings()[0], "disambiguation": comment}))
        self.assertTrue(summary._title_matches({**headlines_track(), "title_short": None},
                                              {**headlines_recordings()[0], "title": "Headlines (Explicit Version)", "disambiguation": ""}))

    def test_headlines_short_recording_fails_existing_duration_tolerance(self):
        explicit, short = headlines_recordings()
        self.assertTrue(summary._duration_matches(headlines_track(), explicit))
        self.assertFalse(summary._duration_matches(headlines_track(), short))

    def test_arbitrary_recording_version_text_is_not_fuzzily_matched(self):
        details = {**headlines_track(), "title_version": "(Radio Version)"}
        self.assertFalse(summary._title_matches(details, {**headlines_recordings()[0], "disambiguation": "radio edit"}))
        self.assertFalse(summary._title_matches(headlines_track(), {**headlines_recordings()[0], "title": "Other", "disambiguation": "explicit"}))

    def test_multi_isrc_requires_exact_contributors_and_canonical_artist(self):
        for credits in ([], [credit("The Beatles", OTHER)], [credit("Other", BEATLES)]):
            candidates = life_recordings()
            for candidate in candidates:
                candidate["artist-credit"] = credits
            self.assertEqual(summary.select_recording(life_track(), candidates, BEATLES), (None, "unresolved"))

    def test_in_my_life_remaster_is_not_recording_mix_identity_and_duration_stays_tied(self):
        candidates = life_recordings()
        for candidate in candidates:
            self.assertTrue(summary._title_matches(life_track(), candidate, allow_mastering=True))
            self.assertTrue(summary._duration_matches(life_track(), candidate))
            self.assertFalse(summary._title_matches(life_track(), candidate))
        mbid, method, remaining = summary._recording_selection(life_track(), candidates, BEATLES)
        self.assertEqual((mbid, method), (None, "unresolved"))
        self.assertEqual({candidate["id"] for candidate in remaining}, {LIFE_REMIX, LIFE_ORIGINAL})

    @patch.object(musicbrainz, "get", return_value={"recordings": life_recordings()})
    def test_in_my_life_resolves_by_exact_recording_remaster_release_context(self, get):
        def browse(mbid, **kwargs):
            if kwargs.get("cache_only"):
                return None
            return [rubber_release()] if mbid == LIFE_REMIX else [rubber_release(LIFE_ORIGINAL, "", "1965-12-03")]
        with patch.object(musicbrainz, "browse_releases_by_recording", side_effect=browse) as read:
            self.assertEqual(summary.resolve_recording(life_track(), BEATLES), (LIFE_REMIX, "isrc_album_context"))
        self.assertEqual({call.args[0] for call in read.call_args_list}, {LIFE_REMIX, LIFE_ORIGINAL})
        self.assertTrue(all(call.kwargs.get("cache_only") for call in read.call_args_list[:4]))
        get.assert_called_once_with("/isrc/GBAYE0601489", "artist-credits", priority="background")

    def test_partial_embedded_isrc_releases_are_not_proof_of_unique_recording_context(self):
        candidates = life_recordings()
        candidates[0]["releases"] = [rubber_release()]
        def browse(mbid, **kwargs):
            self.assertTrue(kwargs["cache_only"])
            return [rubber_release(mbid)]
        with patch.object(musicbrainz, "get", return_value={"recordings": candidates}), patch.object(musicbrainz, "browse_releases_by_recording", side_effect=browse) as read:
            self.assertEqual(summary.resolve_recording(life_track(), BEATLES), (None, "unresolved"))
        self.assertEqual(read.call_count, 2)

    def test_remaster_release_evidence_is_stronger_than_date_alone(self):
        for comment in ("2009 stereo remaster", "remastered"):
            candidates = life_recordings()
            candidates[0]["releases"] = [rubber_release(comment=comment, release_date="2009-09-09")]
            candidates[1]["releases"] = [rubber_release(LIFE_ORIGINAL, "", "2015-12-24")]
            self.assertEqual(summary.select_recording(life_track(), candidates, BEATLES), (LIFE_REMIX, "isrc_album_context"))

    def test_album_date_can_break_equally_supported_recording_remaster_context(self):
        candidates = life_recordings()
        candidates[0]["releases"] = [rubber_release()]
        candidates[1]["releases"] = [rubber_release(LIFE_ORIGINAL, "2009 stereo remaster", "2009-09-09")]
        self.assertEqual(summary.select_recording(life_track(), candidates, BEATLES), (LIFE_REMIX, "isrc_album_context"))

    def test_equally_strong_recording_context_never_guesses_from_order_or_release_count(self):
        candidates = life_recordings()
        candidates[0]["releases"] = [rubber_release()]
        candidates[1]["releases"] = [rubber_release(LIFE_ORIGINAL)] * 12
        for ordered in (candidates, list(reversed(candidates))):
            self.assertEqual(summary.select_recording(life_track(), ordered, BEATLES), (None, "unresolved"))

    def test_recording_album_context_preserves_containment_status_artist_and_compilation_guards(self):
        bad = [rubber_release(LIFE_ORIGINAL)]
        for status in ("Bootleg", "Pseudo-release", "Withdrawn", "Cancelled"):
            candidate = rubber_release()
            candidate["status"] = status
            bad.append(candidate)
        for secondary in ("Compilation", "DJ-mix", "Mixtape/Street"):
            candidate = rubber_release()
            candidate["release-group"]["secondary-types"] = [secondary]
            bad.append(candidate)
        wrong_artist = rubber_release()
        wrong_artist["artist-credit"] = [credit("Other", OTHER)]
        wrong_artist["release-group"]["artist-credit"] = [credit("Other", OTHER)]
        bad.append(wrong_artist)
        bad.append(rubber_release(comment="2015 stereo remaster"))
        for candidate in bad:
            candidates = life_recordings()
            candidates[0]["releases"] = [candidate]
            self.assertEqual(summary.select_recording(life_track(), candidates, BEATLES), (None, "unresolved"))

    @patch.object(musicbrainz, "get", return_value={"recordings": life_recordings()})
    def test_recording_context_hydrates_missing_exact_track_identities(self, get):
        def browse(mbid, **kwargs):
            candidate = rubber_release(mbid, "2009 stereo remaster" if mbid == LIFE_REMIX else "", "2015-12-24" if mbid == LIFE_REMIX else "1965-12-03")
            candidate.pop("media")
            return [candidate]
        def detail(mbid, **kwargs):
            return rubber_release() if mbid == RELEASE else rubber_release(LIFE_ORIGINAL, "", "1965-12-03")
        with patch.object(musicbrainz, "browse_releases_by_recording", side_effect=browse), patch.object(musicbrainz, "release_track_metadata", side_effect=detail) as hydrate:
            self.assertEqual(summary.resolve_recording(life_track(), BEATLES), (LIFE_REMIX, "isrc_album_context"))
        self.assertEqual(hydrate.call_count, 2)

    @patch.object(musicbrainz, "get", return_value={"recordings": life_recordings()})
    @patch.object(musicbrainz, "release_track_metadata", side_effect=AssertionError("Exact recording containment is already proven"))
    def test_known_recording_containment_needs_no_unrelated_medium_hydration(self, hydrate, get):
        def browse(mbid, **kwargs):
            candidate = rubber_release(mbid, "2009 stereo remaster" if mbid == LIFE_REMIX else "", "2015-12-24" if mbid == LIFE_REMIX else "1965-12-03")
            candidate["media"].append({"tracks": []})
            return [candidate]
        with patch.object(musicbrainz, "browse_releases_by_recording", side_effect=browse):
            self.assertEqual(summary.resolve_recording(life_track(), BEATLES), (LIFE_REMIX, "isrc_album_context"))
        hydrate.assert_not_called()

    @patch.object(musicbrainz, "get", return_value={"recordings": life_recordings()})
    def test_recording_context_http_failures_use_short_retry_and_can_recover(self, get):
        for status in (400, 500):
            api_cache.delete_cache_namespace(summary.IDENTITY_NAMESPACE)
            response = requests.Response()
            response.status_code = status
            def failed_browse(mbid, **kwargs):
                if kwargs.get("cache_only"):
                    return None
                raise requests.HTTPError(response=response)
            with patch.object(musicbrainz, "browse_releases_by_recording", side_effect=failed_browse):
                with self.assertRaises(requests.HTTPError):
                    summary.resolve_track(life_track(), BEATLES)
            cached = api_cache.get_cache_document(summary.IDENTITY_NAMESPACE, "track:4")
            self.assertTrue(cached["provider_failure"])
            self.assertLessEqual(cached["retry_at"] - time.time(), summary.RETRY_TTL)
            def recovered_browse(mbid, **kwargs):
                return [rubber_release()] if mbid == LIFE_REMIX else [rubber_release(LIFE_ORIGINAL, "", "1965-12-03")]
            with patch.object(summary.time, "time", return_value=time.time() + summary.RETRY_TTL + 1), patch.object(musicbrainz, "browse_releases_by_recording", side_effect=recovered_browse):
                value = summary.resolve_track(life_track(), BEATLES)
            self.assertEqual(value["recording_mbid"], LIFE_REMIX)
            self.assertEqual(value["recording_resolution_method"], "isrc_album_context")
            self.assertEqual(value["release_group_mbid"], RUBBER_SOUL)

    @patch.object(musicbrainz, "get", return_value={"recordings": life_recordings()})
    def test_incomplete_recording_context_is_provider_failure_not_durable_unresolved(self, get):
        incomplete = rubber_release()
        incomplete.pop("media")
        with patch.object(musicbrainz, "browse_releases_by_recording", return_value=[incomplete]), patch.object(musicbrainz, "release_track_metadata", return_value=incomplete):
            with self.assertRaises(requests.RequestException):
                summary.resolve_track(life_track(), BEATLES)
        value = api_cache.get_cache_document(summary.IDENTITY_NAMESPACE, "track:4")
        self.assertTrue(value["provider_failure"])
        self.assertLessEqual(value["retry_at"] - time.time(), summary.RETRY_TTL)

    @patch.object(musicbrainz, "get", return_value={"recordings": life_recordings()})
    @patch.object(musicbrainz, "release_track_metadata")
    def test_oversized_incomplete_recording_context_uses_provider_backoff_without_unbounded_hydration(self, hydrate, get):
        incomplete = rubber_release()
        incomplete.pop("media")
        with patch.object(musicbrainz, "browse_releases_by_recording", return_value=[incomplete] * 51):
            with self.assertRaises(requests.RequestException):
                summary.resolve_track(life_track(), BEATLES)
        hydrate.assert_not_called()
        self.assertTrue(api_cache.get_cache_document(summary.IDENTITY_NAMESPACE, "track:4")["provider_failure"])

    @patch.object(musicbrainz, "get", return_value={"recordings": headlines_recordings()})
    @patch.object(deezer, "top_tracks")
    @patch.object(deezer, "track")
    def test_v4_negative_recording_retries_without_refetching_order_or_successful_identities(self, details, top, get):
        api_cache.set_cache_document(summary.IDENTITY_NAMESPACE, "track:3", {
            "complete": False, "resolver_version": 4, "retry_at": time.time() + summary.UNRESOLVED_TTL,
            "isrc": "USCM51100290", "recording_mbid": None, "recording_resolution_method": "unresolved",
        }, summary.RETENTION_TTL)
        success = {"complete": True, "resolver_version": 4, "isrc": "USCM51100290",
                   "recording_mbid": HEADLINES, "recording_resolution_method": "exact_isrc"}
        api_cache.set_cache_document(summary.IDENTITY_NAMESPACE, "track:5", success, summary.RETENTION_TTL)
        known = {**headlines_track(), "id": 5, "deezer_track_id": 5, "position": 2,
                 "recording_mbid": HEADLINES, "release_group_mbid": GROUP}
        old = {"fetched_at": time.time(), "resolver_version": 4, "entries": [
            {**headlines_track(), "deezer_track_id": 3, "deezer_artist_id": 3, "position": 1,
             "recording_mbid": None, "release_group_mbid": None}, known,
        ]}
        api_cache.set_cache_document(summary.SNAPSHOT_NAMESPACE, f"top_tracks:{DRAKE}", old, summary.RETENTION_TTL)
        api_cache.set_cache_document(summary.SNAPSHOT_NAMESPACE, f"bio:{DRAKE}", {
            "fetched_at": time.time(), "bio": {"text": "Drake biography"},
        }, summary.RETENTION_TTL)
        api_cache.set_cache_document(summary.STATE_NAMESPACE, f"top_tracks:resolver-v4:{DRAKE}", {
            "status": "pending", "pending_until": time.time() + worker.LEASE_TTL,
        }, worker.LEASE_TTL)
        album = release(recording_id=HEADLINES, title="Take Care (Deluxe)")
        album["artist-credit"] = [credit("Drake", DRAKE)]
        album["release-group"]["artist-credit"] = [credit("Drake", DRAKE)]
        with patch.object(worker, "Thread"), patch.object(worker, "jobs", Queue(maxsize=32)), patch.object(worker, "_started", False):
            self.assertTrue(worker.request_summary(DRAKE)["pending"])
            self.assertEqual(worker.jobs.get_nowait(), (DRAKE, "top_tracks"))
        with patch.object(musicbrainz, "browse_releases_by_recording", return_value=[album]) as browse:
            worker.process_job(DRAKE, "top_tracks")
        repaired = summary.snapshot(DRAKE, "top_tracks")
        self.assertEqual(repaired["resolver_version"], summary.RESOLVER_VERSION)
        self.assertEqual(repaired["fetched_at"], old["fetched_at"])
        self.assertEqual([entry["deezer_track_id"] for entry in repaired["entries"]], [3, 5])
        self.assertEqual(repaired["entries"][1], known)
        self.assertEqual(repaired["entries"][0]["recording_mbid"], HEADLINES)
        self.assertEqual(repaired["entries"][0]["recording_resolution_method"], "isrc_title_duration")
        self.assertEqual(api_cache.get_cache_document(summary.IDENTITY_NAMESPACE, "track:5"), success)
        cached = api_cache.get_cache_document(summary.IDENTITY_NAMESPACE, "track:3")
        self.assertTrue(cached["complete"])
        self.assertEqual(cached["resolver_version"], summary.RESOLVER_VERSION)
        self.assertEqual(cached["retry_at"], 0)
        get.assert_called_once_with("/isrc/USCM51100290", "artist-credits", priority="background")
        browse.assert_called_once_with(HEADLINES, priority="background", include_url_relations=True)
        top.assert_not_called()
        details.assert_not_called()

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

    def test_problem_deluxe_matches_unqualified_album_title(self):
        self.assertEqual(summary._album_title_match(problem_track()["album"]["title"], {"title": "My Everything"}), 1)
        self.assertEqual(summary.select_release_group(PROBLEM, problem_track(), [my_everything_release()], ARIANA),
                         (MY_EVERYTHING, "recording_album_edition"))

    def test_problem_deluxe_matches_deluxe_edition_album_title(self):
        self.assertEqual(summary._album_title_match(problem_track()["album"]["title"], {"title": "My Everything (deluxe edition)"}), 1)
        self.assertEqual(summary.select_release_group(PROBLEM, problem_track(), [my_everything_release("My Everything (deluxe edition)")], ARIANA),
                         (MY_EVERYTHING, "recording_album_edition"))

    def test_problem_deluxe_matches_deluxe_version_album_title(self):
        self.assertEqual(summary._album_title_match(problem_track()["album"]["title"], {"title": "My Everything (deluxe version)"}), 1)
        self.assertEqual(summary.select_release_group(PROBLEM, problem_track(), [my_everything_release("My Everything (deluxe version)")], ARIANA),
                         (MY_EVERYTHING, "recording_album_edition"))

    def test_supported_deluxe_qualifiers_are_equivalent_only_on_the_same_base_title(self):
        for qualifier in ("Deluxe", "Deluxe Edition", "Deluxe Version"):
            details = problem_track()
            details["album"]["title"] = f"My Everything ({qualifier})"
            for mb_qualifier in (None, "Deluxe", "deluxe edition", "deluxe version"):
                title = "My Everything" if mb_qualifier is None else f"My Everything ({mb_qualifier})"
                candidate = my_everything_release(title)
                candidate["release-group"]["title"] = title
                exact = track_search_index.normalize_text(title) == track_search_index.normalize_text(details["album"]["title"])
                with self.subTest(qualifier=qualifier, mb_qualifier=mb_qualifier):
                    self.assertEqual(summary.select_release_group(PROBLEM, details, [candidate], ARIANA),
                                     (MY_EVERYTHING, "recording_album_title" if exact else "recording_album_edition"))
        self.assertEqual(summary._album_title_match("My Everything (Deluxe)", {"title": "Other Album (Deluxe Edition)"}), 0)

    def test_exact_album_title_outranks_deluxe_equivalence_and_stronger_date_evidence(self):
        exact = my_everything_release("My Everything (Deluxe)", OTHER)
        exact["date"] = "2015-01-01"
        exact["release-group"]["first-release-date"] = "2014-08-22"
        self.assertEqual(summary.select_release_group(PROBLEM, problem_track(), [my_everything_release(), exact], ARIANA),
                         (OTHER, "recording_album_title"))

    def test_direct_album_relationship_outranks_exact_and_deluxe_equivalent_titles(self):
        direct = my_everything_release("Different Album", OTHER)
        direct["release-group"]["title"] = "Different Album"
        direct["relations"] = [{"url": {"resource": "https://www.deezer.com/album/8435726"}}]
        exact = my_everything_release("My Everything (Deluxe)", GROUP)
        self.assertEqual(summary.select_release_group(PROBLEM, problem_track(), [my_everything_release(), exact, direct], ARIANA),
                         (OTHER, "recording_deezer_album"))

    def test_unsupported_album_qualifiers_and_meaningful_base_parentheses_are_preserved(self):
        for title in ("My Everything (Live)", "My Everything (Acoustic)", "My Everything (Anniversary Edition)",
                      "My Everything (Expanded Edition)", "My Everything (Remix)", "My Everything (Deluxe Expanded Edition)",
                      "My Everything (Deluxe 2014)", "My Everything (Live) (Deluxe)", "My Everything (Deluxe) (Live)",
                      "My Everything (Deluxe (Edition))", "My Everything Deluxe", "My Everything (Deluxe Remastered 2009)"):
            details = problem_track()
            details["album"]["title"] = title
            with self.subTest(title=title):
                self.assertEqual(summary.select_release_group(PROBLEM, details, [my_everything_release()], ARIANA), (None, "unresolved"))

    def test_deluxe_does_not_strip_unsupported_musicbrainz_qualifiers_or_equate_remasters(self):
        for qualifier in ("Live", "Acoustic", "Anniversary Edition", "Expanded Edition", "Remix", "Remastered 2009"):
            self.assertEqual(summary._album_title_match("My Everything (Deluxe)", {"title": f"My Everything ({qualifier})"}), 0)
        self.assertEqual(summary._album_title_match("My Everything (Remastered 2009)", {"title": "My Everything (Deluxe)"}), 0)

    def test_multiple_deluxe_editions_collapse_to_one_release_group(self):
        releases = [my_everything_release(title, release_id=release_id) for title, release_id in
                    (("My Everything", RELEASE), ("My Everything (deluxe edition)", OTHER), ("My Everything (deluxe version)", GROUP))]
        for ordered in (releases, list(reversed(releases))):
            self.assertEqual(summary.select_release_group(PROBLEM, problem_track(), ordered, ARIANA), (MY_EVERYTHING, "recording_album_edition"))

    def test_equally_strong_deluxe_matches_across_release_groups_remain_unresolved(self):
        releases = [my_everything_release("My Everything (deluxe edition)"), my_everything_release("My Everything (deluxe version)", OTHER)]
        for ordered in (releases, list(reversed(releases))):
            self.assertEqual(summary.select_release_group(PROBLEM, problem_track(), ordered, ARIANA), (None, "unresolved"))

    def test_deluxe_equivalence_preserves_containment_status_artist_and_compilation_guards(self):
        wrong_recording = my_everything_release()
        wrong_recording["media"][0]["tracks"][0]["recording"]["id"] = NICE
        wrong_artist = my_everything_release()
        wrong_artist["artist-credit"] = [credit("Other", OTHER)]
        wrong_artist["release-group"]["artist-credit"] = [credit("Other", OTHER)]
        rejected = [wrong_recording, wrong_artist]
        for status in ("Bootleg", "Pseudo-release", "Withdrawn", "Cancelled"):
            candidate = my_everything_release()
            candidate["status"] = status
            rejected.append(candidate)
        for secondary in ("Compilation", "DJ-mix", "Mixtape/Street"):
            candidate = my_everything_release()
            candidate["release-group"]["secondary-types"] = [secondary]
            rejected.append(candidate)
        for candidate in rejected:
            self.assertEqual(summary.select_release_group(PROBLEM, problem_track(), [candidate], ARIANA), (None, "unresolved"))

    def test_deluxe_album_equivalence_does_not_relax_recording_version_matching(self):
        details = {**problem_track(), "title_version": "(Deluxe)"}
        candidate = {"title": "Problem", "disambiguation": ""}
        for allow_mastering in (False, True):
            self.assertFalse(summary._title_matches(details, candidate, allow_mastering=allow_mastering))
        self.assertEqual(summary._remaster_album_title("My Everything (Deluxe)"), ("my everything deluxe", None))

    def test_remaster_base_can_still_contain_meaningful_deluxe_text_without_year_conflicts(self):
        details = problem_track()
        details["album"]["title"] = "My Everything (Deluxe) (Remastered 2009)"
        candidate = my_everything_release("My Everything (Deluxe)")
        candidate["disambiguation"] = "2009 stereo remaster"
        self.assertEqual(summary.select_release_group(PROBLEM, details, [candidate], ARIANA), (MY_EVERYTHING, "recording_album_remaster"))
        candidate["disambiguation"] = "2015 stereo remaster"
        self.assertEqual(summary.select_release_group(PROBLEM, details, [candidate], ARIANA), (None, "unresolved"))

    @patch.object(summary, "resolve_recording", side_effect=AssertionError("Preserve the successful Problem recording"))
    @patch.object(musicbrainz, "browse_releases_by_recording", return_value=[my_everything_release(), my_everything_release("My Everything (deluxe edition)", release_id=OTHER)])
    @patch.object(deezer, "top_tracks")
    @patch.object(deezer, "track")
    def test_v5_negative_deluxe_group_retries_without_refetching_recording_bio_or_order(self, details, top, browse, resolve):
        self.assertEqual(summary.RESOLVER_VERSION, 6)
        recording_cache = {"complete": True, "resolver_version": 5, "isrc": "USUM71405403",
                           "recording_mbid": PROBLEM, "recording_resolution_method": "isrc_artist_credit"}
        api_cache.set_cache_document(summary.IDENTITY_NAMESPACE, "track:6", recording_cache, summary.RETENTION_TTL)
        group_key = f"group:{PROBLEM}:8435726:my everything deluxe"
        api_cache.set_cache_document(summary.IDENTITY_NAMESPACE, group_key, {
            "complete": False, "resolver_version": 5, "retry_at": time.time() + summary.UNRESOLVED_TTL,
            "recording_mbid": PROBLEM, "release_group_mbid": None, "release_group_resolution_method": "unresolved",
        }, summary.RETENTION_TTL)
        known = {"deezer_track_id": 7, "position": 2, "title": "Known track", "recording_mbid": PROBLEM, "release_group_mbid": MY_EVERYTHING}
        old = {"fetched_at": time.time(), "resolver_version": 5, "entries": [
            {**problem_track(), "deezer_track_id": 6, "deezer_artist_id": 6, "position": 1,
             "recording_mbid": PROBLEM, "release_group_mbid": None}, known,
        ]}
        bio = {"fetched_at": time.time(), "bio": {"text": "Ariana Grande biography"}}
        api_cache.set_cache_document(summary.SNAPSHOT_NAMESPACE, f"top_tracks:{ARIANA}", old, summary.RETENTION_TTL)
        api_cache.set_cache_document(summary.SNAPSHOT_NAMESPACE, f"bio:{ARIANA}", bio, summary.RETENTION_TTL)
        api_cache.set_cache_document(summary.STATE_NAMESPACE, f"top_tracks:resolver-v5:{ARIANA}", {
            "status": "pending", "pending_until": time.time() + worker.LEASE_TTL,
        }, worker.LEASE_TTL)
        with patch.object(worker, "Thread"), patch.object(worker, "jobs", Queue(maxsize=32)), patch.object(worker, "_started", False):
            self.assertTrue(worker.request_summary(ARIANA)["pending"])
            self.assertEqual(worker.jobs.get_nowait(), (ARIANA, "top_tracks"))
        worker.process_job(ARIANA, "top_tracks")
        repaired = summary.snapshot(ARIANA, "top_tracks")
        self.assertEqual(repaired["resolver_version"], 6)
        self.assertEqual(repaired["fetched_at"], old["fetched_at"])
        self.assertEqual([entry["deezer_track_id"] for entry in repaired["entries"]], [6, 7])
        self.assertEqual(repaired["entries"][1], known)
        self.assertEqual(repaired["entries"][0]["recording_mbid"], PROBLEM)
        self.assertEqual(repaired["entries"][0]["recording_resolution_method"], "isrc_artist_credit")
        self.assertEqual(repaired["entries"][0]["release_group_mbid"], MY_EVERYTHING)
        self.assertEqual(repaired["entries"][0]["release_group_resolution_method"], "recording_album_edition")
        self.assertEqual(summary.snapshot(ARIANA, "bio"), bio)
        self.assertEqual(api_cache.get_cache_document(summary.IDENTITY_NAMESPACE, "track:6"), recording_cache)
        mapping = api_cache.get_cache_document(summary.IDENTITY_NAMESPACE, group_key)
        self.assertTrue(mapping["complete"])
        self.assertEqual(mapping["resolver_version"], 6)
        self.assertEqual(mapping["retry_at"], 0)
        resolve.assert_not_called()
        top.assert_not_called()
        details.assert_not_called()
        browse.assert_called_once_with(PROBLEM, priority="background", include_url_relations=True)

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

    def test_unrecognized_album_parentheses_and_other_versions_are_preserved(self):
        for album in ("Abbey Road (Live)", "Abbey Road (Remixed 2009)",
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

    def test_let_it_be_same_title_year_and_recording_resolve_by_full_group_date(self):
        releases = [let_it_be_release(release_date="1970-05-08"),
                    let_it_be_release(OTHER, "Single", "1970-03-06", "1970-03-06")]
        for ordered in (releases, list(reversed(releases))):
            self.assertEqual(summary.select_release_group(LET_IT_BE, let_it_be_track(), ordered, BEATLES),
                             (GROUP, "recording_album_remaster"))

    def test_let_it_be_remaster_uses_original_group_date_instead_of_edition_year(self):
        releases = [let_it_be_release(), let_it_be_release(OTHER, "Single", "1970-03-06")]
        self.assertEqual(summary.select_release_group(LET_IT_BE, let_it_be_track(), releases, BEATLES),
                         (GROUP, "recording_album_remaster"))

    def test_let_it_be_year_only_tie_remains_unresolved(self):
        releases = [let_it_be_release(release_date="1970-05-08"),
                    let_it_be_release(OTHER, "Single", "1970-03-06", "1970-03-06")]
        for provider_date in ("1970", "1970-05", None):
            details = let_it_be_track()
            details["album"]["release_date"] = provider_date
            with self.subTest(provider_date=provider_date):
                self.assertEqual(summary.select_release_group(LET_IT_BE, details, releases, BEATLES), (None, "unresolved"))

    def test_exact_release_date_outranks_year_only_when_group_full_date_is_missing(self):
        details = let_it_be_track()
        details["album"]["title"] = "Let It Be"
        releases = [let_it_be_release(group_date="1970", release_date="1970-05-08"),
                    let_it_be_release(OTHER, "Single", "1970", "1970-03-06")]
        self.assertEqual(summary.select_release_group(LET_IT_BE, details, releases, BEATLES), (GROUP, "recording_album_title"))

    def test_original_group_full_date_outranks_release_date_and_year_agreement(self):
        releases = [let_it_be_release(), let_it_be_release(OTHER, "Single", "1970-03-06", "1970-05-08")]
        self.assertEqual(summary.select_release_group(LET_IT_BE, let_it_be_track(), releases, BEATLES),
                         (GROUP, "recording_album_remaster"))

    def test_distinct_groups_with_identical_full_date_evidence_remain_unresolved(self):
        for edition_date in ("2009-09-09", "1970-03-06"):
            releases = [let_it_be_release(), let_it_be_release(OTHER, "Single", release_date=edition_date)]
            for ordered in (releases, list(reversed(releases))):
                with self.subTest(edition_date=edition_date, reversed=ordered[0] is releases[1]):
                    self.assertEqual(summary.select_release_group(LET_IT_BE, let_it_be_track(), ordered, BEATLES), (None, "unresolved"))

    def test_full_date_can_select_single_without_primary_type_preference(self):
        details = let_it_be_track()
        details["album"]["release_date"] = "1970-03-06"
        releases = [let_it_be_release(), let_it_be_release(OTHER, "Single", "1970-03-06")]
        self.assertEqual(summary.select_release_group(LET_IT_BE, details, releases, BEATLES), (OTHER, "recording_album_remaster"))

    def test_exact_date_does_not_override_title_or_direct_relationship_precedence(self):
        exact = let_it_be_release(OTHER, "Single", "1970-03-06")
        exact["title"] = "Let It Be (Remastered)"
        direct = let_it_be_release(OTHER, "Single", "1970-03-06")
        direct["title"] = "Different title"
        direct["release-group"]["title"] = "Different title"
        direct["relations"] = [{"url": {"resource": "https://www.deezer.com/album/2"}}]
        for candidate, method in ((exact, "recording_album_title"), (direct, "recording_deezer_album")):
            with self.subTest(method=method):
                self.assertEqual(summary.select_release_group(LET_IT_BE, let_it_be_track(), [let_it_be_release(), candidate], BEATLES),
                                 (OTHER, method))

    def test_invalid_or_non_full_dates_do_not_add_exact_date_evidence(self):
        for provider_date in ("1970-02-30", "1970-13-08", "1970-5-08", "1970-05-08T00:00:00", "1970-05-08 "):
            details = let_it_be_track()
            details["album"]["release_date"] = provider_date
            releases = [let_it_be_release(group_date=provider_date, release_date=provider_date),
                        let_it_be_release(OTHER, "Single", "1970-03-06", "1970-03-06")]
            with self.subTest(provider_date=provider_date):
                self.assertEqual(summary.select_release_group(LET_IT_BE, details, releases, BEATLES), (None, "unresolved"))

    @patch.object(summary, "resolve_recording", side_effect=AssertionError("Reuse the successful v3 recording identity"))
    @patch.object(musicbrainz, "browse_releases_by_recording", return_value=[let_it_be_release(), let_it_be_release(OTHER, "Single", "1970-03-06")])
    @patch.object(deezer, "top_tracks")
    @patch.object(deezer, "track")
    def test_v3_negative_group_retries_with_date_evidence_without_refreshing_daily_order(self, details, top, browse, resolve):
        api_cache.set_cache_document(summary.IDENTITY_NAMESPACE, "track:2", {
            "complete": True, "resolver_version": 3, "isrc": "GBAYE0601713", "recording_mbid": LET_IT_BE,
            "recording_resolution_method": "exact_isrc",
        }, summary.RETENTION_TTL)
        group_key = f"group:{LET_IT_BE}:2:let it be remastered"
        api_cache.set_cache_document(summary.IDENTITY_NAMESPACE, group_key, {
            "complete": False, "resolver_version": 3, "retry_at": time.time() + summary.UNRESOLVED_TTL,
            "recording_mbid": LET_IT_BE, "release_group_mbid": None, "release_group_resolution_method": "unresolved",
        }, summary.RETENTION_TTL)
        old = {"fetched_at": time.time(), "resolver_version": 3, "entries": [{
            **let_it_be_track(), "deezer_track_id": 2, "deezer_artist_id": 1, "position": 7,
            "recording_mbid": LET_IT_BE, "release_group_mbid": None,
        }]}
        api_cache.set_cache_document(summary.SNAPSHOT_NAMESPACE, f"top_tracks:{BEATLES}", old, summary.RETENTION_TTL)
        api_cache.set_cache_document(summary.SNAPSHOT_NAMESPACE, f"bio:{BEATLES}", {
            "fetched_at": time.time(), "bio": {"text": "Beatles biography"},
        }, summary.RETENTION_TTL)
        api_cache.set_cache_document(summary.STATE_NAMESPACE, f"top_tracks:resolver-v3:{BEATLES}", {
            "status": "pending", "pending_until": time.time() + worker.LEASE_TTL,
        }, worker.LEASE_TTL)
        with patch.object(worker, "Thread"), patch.object(worker, "jobs", Queue(maxsize=32)), patch.object(worker, "_started", False):
            self.assertTrue(worker.request_summary(BEATLES)["pending"])
            self.assertEqual(worker.jobs.get_nowait(), (BEATLES, "top_tracks"))
        worker.process_job(BEATLES, "top_tracks")
        repaired = summary.snapshot(BEATLES, "top_tracks")
        self.assertEqual(repaired["resolver_version"], summary.RESOLVER_VERSION)
        self.assertEqual(repaired["fetched_at"], old["fetched_at"])
        self.assertEqual(repaired["entries"][0]["position"], 7)
        self.assertEqual(repaired["entries"][0]["recording_mbid"], LET_IT_BE)
        self.assertEqual(repaired["entries"][0]["release_group_mbid"], GROUP)
        self.assertEqual(repaired["entries"][0]["release_group_resolution_method"], "recording_album_remaster")
        mapping = api_cache.get_cache_document(summary.IDENTITY_NAMESPACE, group_key)
        self.assertTrue(mapping["complete"])
        self.assertEqual(mapping["resolver_version"], summary.RESOLVER_VERSION)
        self.assertEqual(mapping["retry_at"], 0)
        resolve.assert_not_called()
        top.assert_not_called()
        details.assert_not_called()
        browse.assert_called_once_with(LET_IT_BE, priority="background", include_url_relations=True)

    @patch.object(summary, "resolve_recording", side_effect=AssertionError("Keep the successful recording identity"))
    @patch.object(musicbrainz, "browse_releases_by_recording", return_value=[abbey_release(), abbey_release(ABBEY_EUROPE, disambiguation="")])
    @patch.object(deezer, "top_tracks")
    @patch.object(deezer, "track")
    def test_v2_negative_group_and_snapshot_retry_while_recording_and_bio_are_preserved(self, details, top, browse, resolve):
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
        self.assertEqual(repaired["resolver_version"], summary.RESOLVER_VERSION)
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
