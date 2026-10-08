"""Album-only number spacing must preserve identity guards and cache reuse."""

if __package__:
    from ._test_environment import TEST_ROOT
else:
    from _test_environment import TEST_ROOT

import time
import unittest
from copy import deepcopy
from queue import Queue
from unittest.mock import patch

from backend import api_cache, track_search_index
from backend.services import artist_summary as summary, deezer, musicbrainz
from backend.workers import artist_summary as worker
from tests.test_backend import DatabaseTestCase

EMINEM = "b95ce3ff-3d05-4e87-9e01-c97b66af13d4"
MONSTER = "7e9e29c6-c643-41bb-bd1c-f77e4a453719"
MONSTER_RELEASE = "5e056966-f47a-4051-b9e2-09097107c39e"
MARSHALL_LP2 = "b1fdc9cc-8680-44da-abab-59edca6b2ad3"
OTHER = "22222222-2222-2222-2222-222222222222"
MB_ALBUM_TITLE = "The Marshall Mathers LP 2"
SPACING_METHOD = "recording_album_number_spacing"


def monster_track():
    return {
        "id": 72160317, "title": "The Monster", "title_short": "The Monster",
        "title_version": "", "duration": 250, "isrc": "USUM71314082",
        "artist": {"id": 13, "name": "Eminem"},
        "contributors": [{"name": "Eminem", "role": "Main"}, {"name": "Rihanna", "role": "Featured"}],
        "album": {"id": 7090505, "title": "The Marshall Mathers LP2 (Deluxe)"},
    }


def monster_release(title=f"{MB_ALBUM_TITLE} (deluxe)", group_id=MARSHALL_LP2, release_id=MONSTER_RELEASE):
    # MusicBrainz displays '(deluxe)' but its API stores the base title and
    # disambiguation separately. Tests cover both representations of this release.
    artist_credit = [{"name": "Eminem", "artist": {"id": EMINEM, "name": "Eminem"}}]
    return {
        "id": release_id, "title": title, "disambiguation": "deluxe", "status": "Official",
        "date": "2013-11-05", "artist-credit": artist_credit,
        "release-group": {
            "id": group_id, "title": MB_ALBUM_TITLE, "primary-type": "Album", "secondary-types": [],
            "artist-credit": artist_credit, "first-release-date": "2013-11-05",
        },
        "media": [{"tracks": [{"recording": {"id": MONSTER, "title": "The Monster"}}]}],
    }


class AlbumNumberSpacingTests(unittest.TestCase):
    def test_lp_vol_part_spacing_matches_in_both_directions(self):
        for joined, spaced in (("LP2", "LP 2"), ("Vol2", "Vol 2"), ("Part3", "Part 3"), ("2LP", "2 LP")):
            for provider_title, mb_title in ((joined, spaced), (spaced, joined)):
                with self.subTest(provider=provider_title, musicbrainz=mb_title):
                    self.assertEqual(summary._album_title_match(provider_title, {"title": mb_title}), 0.5)

    def test_spacing_key_leaves_all_other_word_boundaries_intact(self):
        self.assertEqual(summary._album_number_spacing("The Marshall Mathers LP 2"), "the marshall mathers lp2")
        self.assertEqual(summary._album_number_spacing("Vol 2 Part 3"), "vol2part3")
        for left, right in (("Marshall Mathers LP2", "MarshallMathers LP 2"),
                            ("Part3 Live", "Part 3Li ve"), ("LP2 0", "LP20"),
                            ("Vol2 Part3", "Vol2 Pa rt3"), ("LP2", "LP3")):
            with self.subTest(left=left, right=right):
                self.assertEqual(summary._album_title_match(left, {"title": right}), 0)

    def test_recording_titles_and_global_normalization_remain_strict(self):
        details = {**monster_track(), "title": "Part3", "title_short": "Part3"}
        self.assertNotEqual(track_search_index.normalize_text("Part3"), track_search_index.normalize_text("Part 3"))
        for allow_mastering in (False, True):
            self.assertFalse(summary._title_matches(details, {"title": "Part 3"}, allow_mastering=allow_mastering))

    def test_monster_exact_recording_release_fixture_resolves_canonical_album_group(self):
        for title in (f"{MB_ALBUM_TITLE} (deluxe)", MB_ALBUM_TITLE):
            with self.subTest(title=title):
                candidate = monster_release(title)
                self.assertEqual(candidate["id"], MONSTER_RELEASE)
                self.assertEqual(summary.select_release_group(MONSTER, monster_track(), [candidate], EMINEM),
                                 (MARSHALL_LP2, SPACING_METHOD))

    def test_spacing_can_use_release_group_title_without_changing_containment(self):
        candidate = monster_release("Different edition title")
        self.assertEqual(summary.select_release_group(MONSTER, monster_track(), [candidate], EMINEM),
                         (MARSHALL_LP2, SPACING_METHOD))

    def test_exact_title_outranks_spacing_even_with_weaker_date_evidence(self):
        details = monster_track()
        details["album"]["release_date"] = "2013-11-05"
        exact = monster_release(details["album"]["title"], OTHER)
        exact["date"] = exact["release-group"]["first-release-date"] = "2015-01-01"
        candidates = [monster_release(), exact]
        for ordered in (candidates, list(reversed(candidates))):
            self.assertEqual(summary.select_release_group(MONSTER, details, ordered, EMINEM),
                             (OTHER, "recording_album_title"))

    def test_existing_edition_equivalence_outranks_spacing(self):
        edition = monster_release("The Marshall Mathers LP2 (Deluxe Edition)", OTHER)
        candidates = [monster_release(), edition]
        for ordered in (candidates, list(reversed(candidates))):
            self.assertEqual(summary.select_release_group(MONSTER, monster_track(), ordered, EMINEM),
                             (OTHER, "recording_album_edition"))

    def test_direct_deezer_album_relationship_still_outranks_all_title_forms(self):
        direct = monster_release("Different album", OTHER)
        direct["release-group"]["title"] = "Different album"
        direct["relations"] = [{"url": {"resource": "https://www.deezer.com/album/7090505"}}]
        exact = monster_release(monster_track()["album"]["title"])
        self.assertEqual(summary.select_release_group(MONSTER, monster_track(), [monster_release(), exact, direct], EMINEM),
                         (OTHER, "recording_deezer_album"))

    def test_supported_deluxe_qualifiers_combine_with_base_number_spacing(self):
        for qualifier in ("Deluxe", "Deluxe Edition", "Deluxe Version"):
            details = monster_track()
            details["album"]["title"] = f"The Marshall Mathers LP2 ({qualifier})"
            for mb_qualifier in (None, "deluxe", "deluxe edition", "deluxe version"):
                title = MB_ALBUM_TITLE + (f" ({mb_qualifier})" if mb_qualifier else "")
                candidate = monster_release(title)
                candidate["release-group"]["title"] = title
                with self.subTest(deezer=qualifier, musicbrainz=mb_qualifier):
                    self.assertEqual(summary.select_release_group(MONSTER, details, [candidate], EMINEM),
                                     (MARSHALL_LP2, SPACING_METHOD))

    def test_distinct_group_spacing_ties_remain_unresolved(self):
        candidates = [monster_release(), monster_release(group_id=OTHER, release_id=OTHER)]
        for ordered in (candidates, list(reversed(candidates))):
            self.assertEqual(summary.select_release_group(MONSTER, monster_track(), ordered, EMINEM), (None, "unresolved"))

    def test_spacing_editions_deduplicate_by_release_group(self):
        candidates = [monster_release(), monster_release(MB_ALBUM_TITLE, release_id=OTHER)]
        for ordered in (candidates, list(reversed(candidates))):
            self.assertEqual(summary.select_release_group(MONSTER, monster_track(), ordered, EMINEM),
                             (MARSHALL_LP2, SPACING_METHOD))

    def test_spacing_preserves_exact_recording_status_artist_and_compilation_guards(self):
        wrong_recording = monster_release()
        wrong_recording["media"][0]["tracks"][0]["recording"]["id"] = OTHER
        wrong_artist = monster_release()
        wrong_artist["artist-credit"] = wrong_artist["release-group"]["artist-credit"] = [
            {"name": "Other", "artist": {"id": OTHER, "name": "Other"}},
        ]
        rejected = [wrong_recording, wrong_artist]
        for status in ("Bootleg", "Pseudo-release", "Withdrawn", "Cancelled"):
            candidate = monster_release()
            candidate["status"] = status
            rejected.append(candidate)
        for secondary in ("Compilation", "DJ-mix", "Mixtape/Street"):
            candidate = monster_release()
            candidate["release-group"]["secondary-types"] = [secondary]
            rejected.append(candidate)
        for candidate in rejected:
            with self.subTest(candidate=candidate):
                self.assertEqual(summary.select_release_group(MONSTER, monster_track(), [candidate], EMINEM), (None, "unresolved"))

    def test_spacing_does_not_expand_supported_edition_families(self):
        for provider_title, mb_title in (
            ("LP2 (Deluxe)", "LP 2 (Live)"), ("LP2 (Deluxe)", "LP 2 (Remastered 2009)"),
            ("LP2 (Remastered 2009)", "LP 2 (Deluxe)"), ("LP2 (Deluxe 2013)", "LP 2"),
            ("LP2 (Expanded Edition)", "LP 2"), ("LP2 (Live) (Deluxe)", "LP 2"),
            ("LP2 (Remastered2009)", "LP 2 (Remastered 2009)"),
        ):
            with self.subTest(provider=provider_title, musicbrainz=mb_title):
                self.assertEqual(summary._album_title_match(provider_title, {"title": mb_title}), 0)

    def test_spacing_retains_meaningful_live_qualifier_when_it_is_identical(self):
        self.assertEqual(summary._album_title_match("LP2 (Live)", {"title": "LP 2 (Live)"}), 0.5)
        self.assertEqual(summary._album_title_match("LP2 (Live)", {"title": "LP 2"}), 0)

    def test_spacing_preserves_remaster_year_evidence_and_conflicts(self):
        for mb_title in ("LP 2", "LP 2 (Remastered 2009)"):
            candidate = {"title": mb_title, "disambiguation": "2009 stereo remaster"}
            self.assertEqual(summary._album_title_match("LP2 (Remastered 2009)", candidate), 0.5)
            candidate["disambiguation"] = "2015 stereo remaster"
            self.assertEqual(summary._album_title_match("LP2 (Remastered 2009)", candidate), 0)
        self.assertEqual(summary._album_title_match("LP2 (Remastered 2009)", {"title": "LP 2 (Remastered 2015)"}), 0)


class AlbumNumberSpacingCacheTests(DatabaseTestCase):
    def save_v6_snapshot(self, **row_changes):
        known = {"deezer_track_id": 99, "position": 2, "title": "Known track",
                 "recording_mbid": OTHER, "release_group_mbid": OTHER}
        old = {"fetched_at": time.time(), "resolver_version": 6, "entries": [
            {**monster_track(), "deezer_track_id": 72160317, "deezer_artist_id": 13, "position": 1,
             "recording_mbid": MONSTER, "release_group_mbid": None, **row_changes}, known,
        ]}
        bio = {"fetched_at": time.time(), "bio": {"text": "Eminem biography"}}
        api_cache.set_cache_document(summary.SNAPSHOT_NAMESPACE, f"top_tracks:{EMINEM}", old, summary.RETENTION_TTL)
        api_cache.set_cache_document(summary.SNAPSHOT_NAMESPACE, f"bio:{EMINEM}", bio, summary.RETENTION_TTL)
        return old, bio

    def save_v6_identities(self, group_mbid=None):
        recording = {"complete": True, "resolver_version": 6, "isrc": "USUM71314082",
                     "recording_mbid": MONSTER, "recording_resolution_method": "isrc_artist_credit"}
        key = summary._group_identity_key(monster_track(), recording)
        self.assertEqual(key, f"group:{MONSTER}:7090505:the marshall mathers lp2 deluxe")
        group = {"complete": bool(group_mbid), "resolver_version": 6,
                 "retry_at": 0 if group_mbid else time.time() + summary.UNRESOLVED_TTL,
                 "recording_mbid": MONSTER, "release_group_mbid": group_mbid,
                 "release_group_resolution_method": "recording_deezer_album" if group_mbid else "unresolved"}
        api_cache.set_cache_document(summary.IDENTITY_NAMESPACE, "track:72160317", recording, summary.RETENTION_TTL)
        api_cache.set_cache_document(summary.IDENTITY_NAMESPACE, key, group, summary.RETENTION_TTL)
        return recording, key, group

    @patch.object(summary, "resolve_recording")
    @patch.object(musicbrainz, "browse_releases_by_recording", return_value=[monster_release(MB_ALBUM_TITLE)])
    @patch.object(deezer, "top_tracks")
    @patch.object(deezer, "track")
    def test_v6_negative_group_retries_immediately_preserving_recording_bio_and_order(self, details, top, browse, resolve):
        self.assertEqual(summary.RESOLVER_VERSION, 7)
        recording, key, negative = self.save_v6_identities()
        self.assertGreater(negative["retry_at"], time.time())
        self.assertIsNone(summary._identity(key))
        old, bio = self.save_v6_snapshot()
        original = deepcopy(old)
        api_cache.set_cache_document(summary.STATE_NAMESPACE, f"top_tracks:resolver-v6:{EMINEM}", {
            "status": "pending", "pending_until": time.time() + worker.LEASE_TTL,
        }, worker.LEASE_TTL)
        with patch.object(worker, "Thread"), patch.object(worker, "jobs", Queue(maxsize=32)), patch.object(worker, "_started", False):
            self.assertTrue(worker.request_summary(EMINEM)["pending"])
            self.assertEqual(worker.jobs.get_nowait(), (EMINEM, "top_tracks"))
            self.assertTrue(worker.jobs.empty())
        worker.process_job(EMINEM, "top_tracks")
        repaired = summary.snapshot(EMINEM, "top_tracks")
        self.assertEqual(repaired["resolver_version"], 7)
        self.assertEqual(repaired["fetched_at"], original["fetched_at"])
        self.assertEqual([row["deezer_track_id"] for row in repaired["entries"]], [72160317, 99])
        self.assertEqual(repaired["entries"][1], original["entries"][1])
        row = repaired["entries"][0]
        self.assertEqual(row["recording_mbid"], MONSTER)
        self.assertEqual(row["recording_resolution_method"], "isrc_artist_credit")
        self.assertEqual(row["release_group_mbid"], MARSHALL_LP2)
        self.assertEqual(row["release_group_resolution_method"], SPACING_METHOD)
        self.assertEqual(summary.snapshot(EMINEM, "bio"), bio)
        self.assertEqual(api_cache.get_cache_document(summary.IDENTITY_NAMESPACE, "track:72160317"), recording)
        mapping = api_cache.get_cache_document(summary.IDENTITY_NAMESPACE, key)
        self.assertTrue(mapping["complete"])
        self.assertEqual(mapping["resolver_version"], 7)
        self.assertEqual(mapping["retry_at"], 0)
        self.assertTrue(summary.fresh(repaired, "top_tracks"))
        resolve.assert_not_called()
        top.assert_not_called()
        details.assert_not_called()
        browse.assert_called_once_with(MONSTER, priority="background", include_url_relations=True)

    @patch.object(summary, "resolve_recording")
    @patch.object(musicbrainz, "browse_releases_by_recording")
    def test_v6_successful_recording_and_group_identities_are_reused(self, browse, resolve):
        recording, key, group = self.save_v6_identities(MARSHALL_LP2)
        result = summary.resolve_track(monster_track(), EMINEM)
        self.assertEqual(result["release_group_mbid"], MARSHALL_LP2)
        self.assertEqual(result["release_group_resolution_method"], "recording_deezer_album")
        self.assertEqual(api_cache.get_cache_document(summary.IDENTITY_NAMESPACE, "track:72160317"), recording)
        self.assertEqual(api_cache.get_cache_document(summary.IDENTITY_NAMESPACE, key), group)
        resolve.assert_not_called()
        browse.assert_not_called()

    def test_v6_fully_resolved_summary_stays_warm_without_scheduling_jobs(self):
        old, _ = self.save_v6_snapshot(release_group_mbid=MARSHALL_LP2)
        with patch.object(worker, "Thread"), patch.object(worker, "jobs", Queue(maxsize=32)), patch.object(worker, "_started", False):
            self.assertFalse(worker.request_summary(EMINEM)["pending"])
            self.assertTrue(worker.jobs.empty())
        self.assertEqual(summary.snapshot(EMINEM, "top_tracks"), old)

    def test_v6_provider_failure_snapshot_retains_its_existing_retry_backoff(self):
        retry_at = time.time() + summary.RETRY_TTL
        old, _ = self.save_v6_snapshot(provider_failure=True, retry_at=retry_at)
        self.assertTrue(summary.fresh(old, "top_tracks"))
        with patch.object(worker, "Thread"), patch.object(worker, "jobs", Queue(maxsize=32)), patch.object(worker, "_started", False):
            self.assertFalse(worker.request_summary(EMINEM)["pending"])
            self.assertTrue(worker.jobs.empty())
        with patch.object(summary.time, "time", return_value=retry_at + 1):
            self.assertFalse(summary.fresh(old, "top_tracks"))


if __name__ == "__main__":
    unittest.main()
