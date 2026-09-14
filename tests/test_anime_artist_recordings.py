"""Exact romaji matching against a verified artist's native recording catalog."""
import json
import unittest
from unittest.mock import patch
import requests
from . import test_anime_artist_links as fixtures
from .test_anime_resolver import recording, release
from backend import storage
from backend.services import anime_musicbrainz as resolver

RECORDING = '8215997d-9674-4679-9527-00885b1c1b99'
GROUP = '0900cca0-c3d6-45d7-9843-dde29880a09d'


class ArtistRecordingTests(unittest.TestCase):
    setUp = fixtures.ArtistLinksTests.setUp

    def source(self):
        return {**fixtures.theme(), 'song': {**fixtures.theme()['song'], 'title': 'Ginsekai'}}

    def native(self, **kwargs):
        return recording(recording_id=RECORDING, title='銀世界',
                         artist_name='ALI', artist_id=fixtures.ALI, releases=[], **kwargs)

    def resolve(self, pages, release_pages=None):
        def get(path, inc, **params):
            self.assertEqual(params['priority'], 'background')
            if path == '/recording':
                self.assertEqual(params['artist'], fixtures.ALI)
                self.assertEqual(params['cache_ttl'], 86400)
                return pages[params['offset'] // 100]
            if path == '/release':
                self.assertEqual(params['recording'], RECORDING)
                return {'releases': release_pages or [release(GROUP, 'BLIZZARD / 銀世界')]}
            return {'release-groups': []}
        with patch.object(resolver, 'cached_mapping', return_value=None), patch.object(resolver.musicbrainz, 'search', return_value={'recordings': []}), patch.object(resolver.musicbrainz, 'get', side_effect=get):
            return resolver.resolve_theme(self.source(), verified_artists={'ALI': fixtures.ALI})

    def test_native_title_finds_recording_and_confirms_single_persistently(self):
        result = self.resolve([{'recordings': [self.native(aliases=['Silver World'])]}])
        self.assertEqual(result['recordingId'], RECORDING)
        self.assertEqual(result['registryStatus'], 'confirmed')
        self.assertEqual(result['releaseGroups'][0]['id'], GROUP)
        with storage.db() as connection:
            saved = json.loads(connection.execute('SELECT payload FROM anime_automatic_matches').fetchone()[0])
        self.assertEqual(saved['recordingTitle'], '銀世界')
        self.assertEqual(saved['sourceSongTitle'], 'Ginsekai')
        self.assertEqual(saved['titleMatchMethod'], 'artist-recording-romanization')
        with patch.object(resolver.musicbrainz, 'get', side_effect=AssertionError('rematch')):
            self.assertEqual(resolver.resolve_theme(self.source()), result)

    def test_paginated_catalog_and_alias_match(self):
        unrelated = [recording(recording_id=f'other-{i}', title='Unrelated') for i in range(100)]
        match = self.native()
        match['title'] = 'Different spelling'
        match['aliases'] = [{'name': '銀世界'}]
        result = self.resolve([{'recordings': unrelated, 'recording-count': 101},
                               {'recordings': [match], 'recording-count': 101}])
        self.assertEqual(result['recordingId'], RECORDING)

    def test_wrong_artist_live_and_tv_versions_are_excluded(self):
        wrong = self.native()
        wrong['artist-credit'][0]['artist']['id'] = 'wrong-artist'
        result = self.resolve([{'recordings': [wrong, self.native(disambiguation='live'),
                                             self.native(disambiguation='TV size')]}])
        self.assertEqual(result['state'], 'unmatched')

    def test_duplicate_recording_ids_are_one_candidate(self):
        result = self.resolve([{'recordings': [self.native(), self.native()]}])
        self.assertEqual(result['registryStatus'], 'confirmed')

    def test_incomplete_catalog_never_confirms_a_partial_match(self):
        page = [self.native()] * 100
        result = self.resolve([{'recordings': page, 'recording-count': 1001}] * 10)
        self.assertEqual(result['state'], 'unmatched')
        self.assertEqual(result['reason'], 'artist-recording-catalog-incomplete')

    def test_catalog_resumes_after_1000_and_restart_then_is_reused(self):
        filler = [recording(recording_id=f"other-{i}", title="Unrelated") for i in range(100)]
        pages = [{"recordings": filler, "recording-count": 1001}] * 10
        pages.append({"recordings": [self.native()], "recording-count": 1001})
        first = self.resolve(pages)
        self.assertEqual(first["reason"], "artist-recording-catalog-incomplete")
        storage.init_db()
        with storage.db() as connection:
            self.assertEqual(connection.execute("SELECT next_offset FROM anime_recording_catalogs").fetchone()[0], 1000)
        # Any attempt to fetch already consumed pages now fails the test.
        pages[:10] = [None] * 10
        result = self.resolve(pages)
        self.assertEqual(result["registryStatus"], "confirmed")
        with patch.object(resolver.musicbrainz, "get", side_effect=AssertionError("catalog refetch")):
            records, complete = resolver._recording_catalog(fixtures.ALI)
        self.assertTrue(complete)
        self.assertIn(RECORDING, [r["id"] for r in records])

    def test_provider_failure_keeps_completed_pages_for_retry(self):
        filler = [recording(recording_id=f"other-{i}", title="Unrelated") for i in range(100)]
        with patch.object(resolver.musicbrainz, "get", side_effect=[
            {"recordings": filler, "recording-count": 101}, requests.Timeout]):
            with self.assertRaises(requests.Timeout):
                resolver._recording_catalog(fixtures.ALI)
        with patch.object(resolver.musicbrainz, "get", return_value={"recordings": [self.native()], "recording-count": 101}) as get:
            records, complete = resolver._recording_catalog(fixtures.ALI)
        self.assertEqual(get.call_args.kwargs["offset"], 100)
        self.assertTrue(complete)

    def test_catalog_progress_retries_after_five_minutes(self):
        with patch.object(resolver, "set_cache_document"):
            resolver.cache_mapping(self.source(), resolver._result(
                self.source(), "unmatched", "artist-recording-catalog-incomplete"))
        with storage.db() as connection:
            row = connection.execute("SELECT retry_at, updated_at FROM anime_automatic_matches").fetchone()
        self.assertAlmostEqual(row["retry_at"] - row["updated_at"], 300, delta=1)

    def test_old_limit_results_are_requeued_without_touching_successes(self):
        old = resolver._result(self.source(), "unmatched", "artist-recording-browse-limit")
        with storage.db() as connection:
            connection.execute("INSERT INTO anime_automatic_matches VALUES (?, ?, ?, 1)",
                (resolver.theme_mapping_key(self.source()), json.dumps(old), 9999999999))
            connection.execute("INSERT INTO anime_performance_jobs (anime_slug, theme_id, song_id, due_at) VALUES ('a', 1, 12787, 9999999999)")
            connection.execute("INSERT INTO anime_automatic_matches VALUES ('success', ?, NULL, 1)",
                               (json.dumps({"state": "resolved"}),))
        with patch.object(resolver, "get_cache_document", return_value=old):
            self.assertIsNone(resolver.cached_mapping(self.source()))
            self.assertIsNone(resolver.saved_automatic_mapping(self.source()))
        storage.init_db()
        with storage.db() as connection:
            self.assertEqual(connection.execute("SELECT due_at FROM anime_performance_jobs").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT mapping_key FROM anime_automatic_matches").fetchone()[0], "success")

    def test_two_native_recordings_with_same_reading_remain_ambiguous(self):
        other = {**self.native(), 'id': '44444444-4444-4444-8444-444444444444'}
        with patch.object(resolver, 'cached_mapping', return_value=None), patch.object(resolver.musicbrainz, 'search', return_value={'recordings': []}), patch.object(resolver.musicbrainz, 'get', side_effect=lambda path, inc, **kw: {'recordings': [self.native(), other]} if path == '/recording' else {'releases': [release(GROUP, 'Single')]}):
            result = resolver.resolve_theme(self.source(), verified_artists={'ALI': fixtures.ALI})
        self.assertEqual(result['state'], 'ambiguous')
        self.assertNotIn('registryStatus', result)

    def test_provider_failure_does_not_become_a_match(self):
        with patch.object(resolver, 'cached_mapping', return_value=None), patch.object(resolver.musicbrainz, 'search', return_value={'recordings': []}), patch.object(resolver.musicbrainz, 'get', side_effect=requests.Timeout):
            result = resolver.resolve_theme(self.source(), verified_artists={'ALI': fixtures.ALI})
        self.assertEqual(result['state'], 'failed')
