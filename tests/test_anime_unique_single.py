"""A resolved song with one Single needs no manual release choice."""

if __package__:
    from ._test_environment import TEST_ROOT as _TEST_ROOT  # noqa: F401
else:  # Support unittest discovery with tests/ as the top-level directory.
    from _test_environment import TEST_ROOT as _TEST_ROOT  # noqa: F401

import json
import unittest
from unittest.mock import patch
from . import test_anime_artist_links as fixtures
from backend import storage
from backend.services import anime_musicbrainz as resolver, anime_mapping_registry as registry, anime_theme_links

SINGLE = '11111111-1111-4111-8111-111111111111'
ALBUM = '22222222-2222-4222-8222-222222222222'
RECORDING = '33333333-3333-4333-8333-333333333333'


def automatic():
    return fixtures.mapping(matchMethod='recording-search', recordingId=RECORDING,
        releaseGroups=[{'id': ALBUM, 'title': 'Compilation', 'type': 'Album'},
                       {'id': SINGLE, 'title': 'BLIZZARD / Ginsekai', 'type': 'Single', 'date': '2021-05-19'}])


class UniqueSingleTests(unittest.TestCase):
    setUp = fixtures.ArtistLinksTests.setUp

    def test_confirmed_single_persists_and_artist_card_has_one_target(self):
        result = resolver.confirm_unique_single(fixtures.theme(), automatic())
        self.assertEqual(result['registryStatus'], 'confirmed')
        self.assertEqual(result['registryProvenance'], 'automatic-unique-single')
        self.assertEqual([g['id'] for g in result['releaseGroups']], [SINGLE])
        self.assertEqual(result['recordingId'], RECORDING)
        storage.init_db()
        self.assertEqual(resolver.stored_mapping(fixtures.theme()), result)
        targets = anime_theme_links.release_groups_for_performances([
            {'animeSlug': 'the_fable', 'themeId': 12830, 'songId': 12787}])
        self.assertEqual(len(targets[('the_fable', 12830)]), 1)
        self.assertEqual(targets[('the_fable', 12830)][0]['id'], SINGLE)
        with storage.db() as connection:
            evidence = json.loads(connection.execute('SELECT payload FROM anime_automatic_matches').fetchone()[0])
        self.assertEqual(len(evidence['releaseGroups']), 2)

    def test_existing_saved_match_promotes_without_network_or_rematching(self):
        with storage.db() as connection:
            connection.execute('INSERT INTO anime_automatic_matches VALUES (?, ?, NULL, 1)',
                               (resolver.theme_mapping_key(fixtures.theme()), json.dumps(automatic())))
        with patch.object(resolver, '_resolve_theme_live', side_effect=AssertionError('rematch')):
            result = resolver.resolve_theme(fixtures.theme())
        self.assertEqual(result['registryStatus'], 'confirmed')

    def test_fresh_match_promotes_and_preserves_alternatives(self):
        with patch.object(resolver, 'cached_mapping', return_value=None), patch.object(resolver, '_resolve_theme_live', return_value=automatic()):
            result = resolver.resolve_theme(fixtures.theme())
        self.assertEqual(result['registryStatus'], 'confirmed')
        resolver.cache_mapping(fixtures.theme(), result)
        with storage.db() as connection:
            evidence = json.loads(connection.execute('SELECT payload FROM anime_automatic_matches').fetchone()[0])
        self.assertEqual(len(evidence['releaseGroups']), 2)

    def test_multiple_singles_unknown_types_and_ambiguous_recordings_require_choice(self):
        for mode in ('multiple', 'unknown', 'ambiguous', 'missing-recording'):
            candidate = automatic()
            if mode == 'multiple':
                candidate['releaseGroups'][0]['type'] = 'Single'
            elif mode == 'unknown':
                candidate['releaseGroups'][0]['type'] = 'Other'
            elif mode == 'ambiguous':
                candidate['state'] = 'ambiguous'
            else:
                candidate.pop('recordingId')
            with self.subTest(mode=mode):
                self.assertEqual(resolver.confirm_unique_single(fixtures.theme(), candidate), candidate)
                self.assertIsNone(registry.get_mapping(12787))

    def test_existing_rejection_and_manual_album_choice_win(self):
        for status in ('rejected', 'confirmed', 'proposed'):
            registry.upsert_mapping(12787, title='Professionalism', artists=['ALI'],
                status=status, provenance='manual', targets=[] if status == 'rejected' else [{
                    'releaseGroupId': ALBUM, 'releaseGroupTitle': 'Manual album',
                    'artistName': 'ALI', 'artistIds': [fixtures.ALI]}])
            result = resolver.confirm_unique_single(fixtures.theme(), automatic())
            self.assertEqual(result['registryStatus'], status)
            self.assertEqual(result['registryProvenance'], 'manual')
            if status != 'rejected':
                self.assertEqual(result['releaseGroups'][0]['id'], ALBUM)
