"""Focused tests for conservative anime-song MusicBrainz resolution."""

if __package__:
    from ._test_environment import TEST_ROOT
else:  # Support direct execution: python tests/test_anime_resolver.py
    from _test_environment import TEST_ROOT

import os
import tempfile
import unittest
from unittest.mock import patch

import requests

from backend import api_cache, storage
from backend.services import anime_mapping_registry, anime_musicbrainz
from backend.workers import anime_metadata


def theme(
    song_id="song-1",
    title="Haruka Kanata",
    artists=("ASIAN KUNG-FU GENERATION",),
):
    return {
        "id": "theme-1",
        "type": "OP",
        "sequence": 2,
        "song": {
            "id": song_id,
            "title": title,
            "artists": [{"name": name} for name in artists],
        },
    }


def artist_result(name="ASIAN KUNG-FU GENERATION", artist_id="artist-1", score=100):
    return {
        "id": artist_id,
        "name": name,
        "sort-name": name,
        "score": score,
        "aliases": [],
    }


def release(
    group_id,
    title,
    *,
    primary_type="Single",
    secondary_types=(),
    date="2003-01-01",
    status="Official",
):
    return {
        "id": f"release-{group_id}",
        "title": title,
        "date": date,
        "status": status,
        "release-group": {
            "id": group_id,
            "title": title,
            "primary-type": primary_type,
            "secondary-types": list(secondary_types),
        },
    }


def recording(
    recording_id="recording-1",
    title="Haruka Kanata",
    artist_name="ASIAN KUNG-FU GENERATION",
    artist_id="artist-1",
    score=100,
    releases=None,
    disambiguation="",
    aliases=(),
):
    return {
        "id": recording_id,
        "title": title,
        "score": score,
        "disambiguation": disambiguation,
        "aliases": [{"name": value} for value in aliases],
        "artist-credit": [
            {
                "name": artist_name,
                "artist": {
                    "id": artist_id,
                    "name": artist_name,
                    "sort-name": artist_name,
                },
            }
        ],
        "releases": releases
        if releases is not None
        else [release("group-1", title)],
    }


def release_group(
    group_id="6259b4f8-39b2-4b46-98e0-5dd433630abc",
    title="斜陽",
    *,
    artist_name="ヨルシカ",
    artist_id="dfc6a151-3792-4695-8fda-f64723eaa788",
    sort_name="Yorushika",
    date="2023-05-08",
    primary_type="Single",
):
    return {
        "id": group_id,
        "title": title,
        "first-release-date": date,
        "primary-type": primary_type,
        "secondary-types": [],
        "aliases": [],
        "artist-credit": [
            {
                "name": artist_name,
                "artist": {
                    "id": artist_id,
                    "name": artist_name,
                    "sort-name": sort_name,
                },
            }
        ],
    }


def registry_document(
    song_id=12183,
    *,
    status="confirmed",
    provenance="manual-review",
    group_id="6259b4f8-39b2-4b46-98e0-5dd433630abc",
    title="Shayou",
    source_artist="Yorushika",
    group_title="斜陽",
    group_artist="ヨルシカ",
):
    target = {
        "releaseGroupId": group_id,
        "recordingIds": [],
        "artistIds": ["dfc6a151-3792-4695-8fda-f64723eaa788"],
        "releaseGroupTitle": group_title,
        "artistName": group_artist,
        "primaryType": "Single",
        "firstReleaseDate": "2023-05-08",
        "preferred": True,
    }
    return {
        "songId": song_id,
        "title": title,
        "artists": [source_artist],
        "status": status,
        "provenance": provenance,
        "scope": "commercial_full",
        "targets": [target],
        "preferredTarget": target,
        "schemaVersion": 1,
    }


class AnimeMusicBrainzResolverTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="melodarr-anime-tests-")
        self.original_cache_database = api_cache.CACHE_DATABASE
        api_cache.CACHE_DATABASE = os.path.join(self.temporary.name, "metadata.db")
        api_cache.init_cache_db()
        with anime_metadata.queue_lock:
            anime_metadata.queued_themes.clear()
            anime_metadata.active_theme_keys.clear()
            anime_metadata.job_state.update(
                running=False,
                queued=0,
                completed=0,
                total=0,
                lastCompletedAt=None,
            )
        anime_metadata.wake_requested.clear()
        self.musicbrainz_get_patcher = patch(
            "backend.services.anime_musicbrainz.musicbrainz.get",
            return_value={"release-groups": [], "release-group-count": 0},
        )
        self.musicbrainz_get = self.musicbrainz_get_patcher.start()
        self.registry_get_patcher = patch(
            "backend.services.anime_musicbrainz.anime_mapping_registry.get_mapping",
            return_value=None,
        )
        self.registry_get = self.registry_get_patcher.start()
        self.registry_create_patcher = patch(
            "backend.services.anime_musicbrainz.anime_mapping_registry."
            "create_mapping_if_absent",
        )
        self.registry_create = self.registry_create_patcher.start()

    def tearDown(self):
        self.registry_create_patcher.stop()
        self.registry_get_patcher.stop()
        self.musicbrainz_get_patcher.stop()
        api_cache.CACHE_DATABASE = self.original_cache_database
        self.temporary.cleanup()

    def test_title_comparison_handles_explicit_hepburn_long_vowels_only(self):
        self.assertFalse(
            anime_musicbrainz._title_comparison_keys("Shayō").isdisjoint(
                anime_musicbrainz._title_comparison_keys("Shayou")
            )
        )
        self.assertFalse(
            anime_musicbrainz._title_comparison_keys("Tōkyō").isdisjoint(
                anime_musicbrainz._title_comparison_keys("Toukyou")
            )
        )
        self.assertTrue(
            anime_musicbrainz._title_comparison_keys("Shayo").isdisjoint(
                anime_musicbrainz._title_comparison_keys("Shayou")
            )
        )

    @patch("backend.services.anime_musicbrainz.musicbrainz.search")
    def test_verified_shayou_seed_is_promoted_without_live_or_cache_use(self, search):
        document = registry_document(provenance="builtin-seed")
        self.registry_create.return_value = document
        seeded_theme = theme(
            song_id=12183,
            title="Shayou",
            artists=("Yorushika",),
        )

        mapping = anime_musicbrainz.resolve_theme(seeded_theme)

        self.assertEqual(mapping["state"], "resolved")
        self.assertEqual(mapping["mappingSource"], "seed")
        self.assertEqual(mapping["matchMethod"], "verified-seed")
        self.assertEqual(mapping["registryStatus"], "confirmed")
        self.assertEqual(
            mapping["releaseGroups"][0]["id"],
            "6259b4f8-39b2-4b46-98e0-5dd433630abc",
        )
        self.registry_get.assert_called_once_with(12183)
        self.registry_create.assert_called_once_with(
            12183,
            title="Shayou",
            artists=["Yorushika"],
            status="confirmed",
            provenance="builtin-seed",
            scope="commercial_full",
            targets=[anime_musicbrainz._VERIFIED_SEEDS[0]["target"]],
            preferred_release_group_mbid=(
                "6259b4f8-39b2-4b46-98e0-5dd433630abc"
            ),
        )
        search.assert_not_called()
        self.assertIsNone(anime_musicbrainz.cached_mapping(seeded_theme))

    @patch("backend.services.anime_musicbrainz.musicbrainz.search")
    def test_verified_suu_sentimental_seed_uses_observed_song_id(self, search):
        document = registry_document(
            song_id=12237,
            provenance="builtin-seed",
            group_id="7dc293e7-192e-4762-9217-db117a2ea705",
            title="Suu Sentimental",
            source_artist="Lam Kohana",
            group_title="数センチメンタル",
            group_artist="こはならむ",
        )
        document["targets"][0]["artistIds"] = [
            "cb9266b4-8537-4687-8763-5129c583be53"
        ]
        self.registry_create.return_value = document

        mapping = anime_musicbrainz.resolve_theme(
            theme(
                song_id="12237",
                title="Suu Sentimental",
                artists=("Lam Kohana",),
            )
        )

        self.assertEqual(mapping["state"], "resolved")
        self.assertEqual(
            mapping["releaseGroups"][0]["id"],
            "7dc293e7-192e-4762-9217-db117a2ea705",
        )
        self.assertEqual(
            mapping["releaseGroups"][0]["romanizedTitle"],
            "Suu Sentimental",
        )
        self.registry_create.assert_called_once()
        create_call = self.registry_create.call_args
        self.assertEqual(create_call.args, (12237,))
        self.assertEqual(create_call.kwargs["title"], "Suu Sentimental")
        self.assertEqual(create_call.kwargs["artists"], ["Lam Kohana"])
        search.assert_not_called()

    @patch("backend.services.anime_musicbrainz.musicbrainz.search")
    def test_local_registry_precedes_cache_and_preserves_user_choice(self, search):
        source = theme(song_id=12183, title="Shayou", artists=("Yorushika",))
        anime_musicbrainz.cache_mapping(
            source,
            anime_musicbrainz._result(source, "unmatched", "old-cache-miss"),
        )
        local_group = "11111111-2222-4333-8444-555555555555"
        self.registry_get.return_value = registry_document(
            provenance="manual-review",
            group_id=local_group,
            group_title="User selected edition",
        )

        mapping = anime_musicbrainz.resolve_theme(source)

        self.assertEqual(mapping["mappingSource"], "local")
        self.assertEqual(mapping["matchMethod"], "local-registry")
        self.assertEqual(mapping["releaseGroups"][0]["id"], local_group)
        self.registry_create.assert_not_called()
        search.assert_not_called()

    @patch("backend.services.anime_musicbrainz.musicbrainz.search")
    def test_cached_automated_mapping_precedes_live_resolution(self, search):
        source = theme(song_id=4242)
        cached = anime_musicbrainz._result(
            source,
            "resolved",
            "",
            matchMethod="recording-search",
            releaseGroups=[{"id": "cached-group", "name": "Cached"}],
        )
        anime_musicbrainz.cache_mapping(source, cached)

        mapping = anime_musicbrainz.resolve_theme(source)

        self.assertEqual(mapping, cached)
        self.registry_get.assert_called_once_with(4242)
        search.assert_not_called()

    def test_proposed_registry_mapping_remains_ambiguous(self):
        self.registry_get.return_value = registry_document(status="proposed")

        mapping = anime_musicbrainz.registered_mapping(
            theme(song_id=12183, title="Shayou", artists=("Yorushika",))
        )

        self.assertEqual(mapping["state"], "ambiguous")
        self.assertEqual(mapping["reason"], "registry-proposed")
        self.assertEqual(mapping["confidence"], 0)
        self.assertEqual(
            mapping["releaseGroups"][0]["mappingScope"],
            "commercial_full",
        )
        self.registry_create.assert_not_called()

    def test_rejected_registry_mapping_suppresses_seed_and_automatic_lookup(self):
        self.registry_get.return_value = registry_document(
            status="rejected",
            provenance="manual-rejection",
        ) | {"targets": [], "preferredTarget": None}

        mapping = anime_musicbrainz.registered_mapping(
            theme(song_id=12183, title="Shayou", artists=("Yorushika",))
        )

        self.assertEqual(mapping["state"], "unmatched")
        self.assertEqual(mapping["reason"], "registry-rejected")
        self.assertEqual(mapping["releaseGroups"], [])
        self.registry_create.assert_not_called()

    def test_invalid_song_ids_bypass_registry_and_seed_promotion(self):
        for song_id in (None, "", "song-shayou", 0, -1, True):
            with self.subTest(song_id=song_id):
                mapping = anime_musicbrainz.registered_mapping(
                    theme(
                        song_id=song_id,
                        title="Shayou",
                        artists=("Yorushika",),
                    )
                )
                self.assertIsNone(mapping)
        self.registry_get.assert_not_called()
        self.registry_create.assert_not_called()

    @patch("backend.services.anime_musicbrainz.musicbrainz.search")
    def test_exact_recording_resolves_and_ranks_requestable_release_groups(self, search):
        search.side_effect = [
            {"artists": [artist_result()]},
            {
                "recordings": [
                    recording(
                        releases=[
                            release(
                                "group-compilation",
                                "Anime Hits",
                                primary_type="Album",
                                secondary_types=("Compilation",),
                                date="2008-01-01",
                            ),
                            release(
                                "group-single",
                                "Haruka Kanata",
                                primary_type="Single",
                            ),
                        ]
                    )
                ]
            },
        ]

        mapping = anime_musicbrainz.resolve_theme(theme())

        self.assertEqual(mapping["state"], "resolved")
        self.assertEqual(mapping["recordingId"], "recording-1")
        self.assertEqual(mapping["artistIds"], ["artist-1"])
        self.assertEqual(
            [item["id"] for item in mapping["releaseGroups"]],
            ["group-single", "group-compilation"],
        )
        first = mapping["releaseGroups"][0]
        self.assertEqual(first["name"], "Haruka Kanata")
        self.assertEqual(first["title"], first["name"])
        self.assertEqual(
            first["coverArt"],
            "/api/artwork/release-group/group-single?size=thumb",
        )
        self.assertEqual(search.call_count, 2)
        self.assertTrue(
            all(call.kwargs["priority"] == "background" for call in search.call_args_list)
        )
        self.assertIn("arid:artist-1", search.call_args_list[1].args[0])
        self.assertIn("alias:", search.call_args_list[0].args[0])
        self.musicbrainz_get.assert_not_called()

    @patch("backend.services.anime_musicbrainz.musicbrainz.search")
    def test_shayou_resolves_by_exact_artist_discography_romanization(self, search):
        artist_id = "dfc6a151-3792-4695-8fda-f64723eaa788"
        group_id = "6259b4f8-39b2-4b46-98e0-5dd433630abc"
        search.side_effect = [
            {
                "artists": [
                    {
                        "id": artist_id,
                        "name": "ヨルシカ",
                        "sort-name": "Yorushika",
                        "score": 100,
                        "aliases": [
                            {"name": "Yorushika", "locale": "en", "primary": True}
                        ],
                    }
                ]
            },
            {"recordings": []},
        ]
        self.musicbrainz_get.return_value = {
            "release-groups": [release_group()],
            "release-group-count": 43,
            "release-group-offset": 0,
        }

        mapping = anime_musicbrainz.resolve_theme(
            theme(song_id="song-shayou", title="Shayou", artists=("Yorushika",))
        )

        self.assertEqual(mapping["state"], "resolved")
        self.assertEqual(mapping["matchMethod"], "artist-discography-title")
        self.assertEqual(mapping["recordingId"], "")
        self.assertEqual(mapping["recordingTitle"], "斜陽")
        self.assertEqual(mapping["artistIds"], [artist_id])
        self.assertEqual(mapping["releaseGroups"][0]["id"], group_id)
        self.assertEqual(mapping["releaseGroups"][0]["romanizedTitle"], "Shayou")
        self.assertEqual(mapping["releaseGroups"][0]["artist"], "ヨルシカ")
        artist_query = search.call_args_list[0].args[0]
        self.assertIn('alias:"Yorushika"', artist_query)
        self.assertIn('sortname:"Yorushika"', artist_query)
        self.musicbrainz_get.assert_called_once_with(
            "/release-group",
            "artist-credits+aliases",
            priority="background",
            artist=artist_id,
            limit=anime_musicbrainz.RELEASE_GROUP_BROWSE_LIMIT,
            offset=0,
        )

    @patch("backend.services.anime_musicbrainz.musicbrainz.search")
    def test_multiple_exact_romanized_release_groups_remain_ambiguous(self, search):
        artist_id = "dfc6a151-3792-4695-8fda-f64723eaa788"
        search.side_effect = [
            {
                "artists": [
                    artist_result("ヨルシカ", artist_id, score=100)
                    | {"sort-name": "Yorushika"}
                ]
            },
            {"recordings": []},
        ]
        self.musicbrainz_get.return_value = {
            "release-groups": [
                release_group(group_id="group-single"),
                release_group(
                    group_id="group-album",
                    date="2024-01-01",
                    primary_type="Album",
                ),
            ],
            "release-group-count": 2,
        }

        mapping = anime_musicbrainz.resolve_theme(
            theme(title="Shayou", artists=("Yorushika",))
        )

        self.assertEqual(mapping["state"], "ambiguous")
        self.assertEqual(mapping["reason"], "multiple-exact-release-groups")
        self.assertEqual(
            [item["id"] for item in mapping["releaseGroups"]],
            ["group-single", "group-album"],
        )

    @patch("backend.services.anime_musicbrainz.musicbrainz.search")
    def test_romanized_alias_can_supply_an_exact_title_match(self, search):
        search.side_effect = [
            {"artists": [artist_result("LiSA", "artist-lisa")]},
            {
                "recordings": [
                    recording(
                        title="紅蓮華",
                        artist_name="LiSA",
                        artist_id="artist-lisa",
                        aliases=("Gurenge",),
                        releases=[release("group-gurenge", "紅蓮華")],
                    )
                ]
            },
        ]

        mapping = anime_musicbrainz.resolve_theme(
            theme(title="Gurenge", artists=("LiSA",))
        )

        self.assertEqual(mapping["state"], "resolved")
        self.assertEqual(mapping["recordingTitle"], "紅蓮華")

    @patch("backend.services.anime_musicbrainz.musicbrainz.search")
    def test_version_markers_are_not_auto_resolved(self, search):
        search.side_effect = [
            {"artists": [artist_result()]},
            {
                "recordings": [
                    recording(disambiguation="live at Budokan"),
                    recording(
                        recording_id="recording-cover",
                        disambiguation="cover version",
                        score=99,
                    ),
                ]
            },
        ]

        mapping = anime_musicbrainz.resolve_theme(theme())

        self.assertEqual(mapping["state"], "unmatched")
        self.assertEqual(mapping["reason"], "no-exact-recording")

    @patch("backend.services.anime_musicbrainz.musicbrainz.search")
    def test_multiple_equally_strong_exact_recordings_remain_ambiguous(self, search):
        search.side_effect = [
            {"artists": [artist_result()]},
            {
                "recordings": [
                    recording("recording-1", releases=[release("group-1", "Song")]),
                    recording("recording-2", releases=[release("group-2", "Song")]),
                ]
            },
        ]

        mapping = anime_musicbrainz.resolve_theme(theme())

        self.assertEqual(mapping["state"], "ambiguous")
        self.assertEqual(mapping["recordingId"], "")
        self.assertEqual(
            {item["recordingId"] for item in mapping["recordingCandidates"]},
            {"recording-1", "recording-2"},
        )
        self.assertEqual(
            [item["name"] for item in mapping["releaseGroups"]],
            ["Song", "Song"],
        )
        self.assertTrue(
            all(item["coverArt"] for item in mapping["releaseGroups"])
        )

    @patch("backend.services.anime_musicbrainz.musicbrainz.search")
    def test_wrong_artist_credit_is_rejected_even_with_exact_title(self, search):
        search.side_effect = [
            {"artists": [artist_result()]},
            {
                "recordings": [
                    recording(artist_name="Different Artist", artist_id="artist-other")
                ]
            },
        ]

        mapping = anime_musicbrainz.resolve_theme(theme())

        self.assertEqual(mapping["state"], "unmatched")
        self.assertEqual(mapping["reason"], "no-exact-recording")

    @patch("backend.services.anime_musicbrainz.musicbrainz.search")
    def test_missing_artist_uses_no_provider_requests(self, search):
        mapping = anime_musicbrainz.resolve_theme(theme(artists=()))

        self.assertEqual(mapping["state"], "unmatched")
        self.assertEqual(mapping["reason"], "missing-artist")
        search.assert_not_called()

    @patch("backend.services.anime_musicbrainz.musicbrainz.search")
    def test_provider_failure_has_a_safe_short_lived_state(self, search):
        search.side_effect = requests.Timeout("private upstream detail")

        mapping = anime_musicbrainz.resolve_theme(theme())

        self.assertEqual(mapping["state"], "failed")
        self.assertEqual(mapping["reason"], "provider-error")
        self.assertNotIn("private", str(mapping))

    def test_cache_identity_uses_source_song_and_normalized_credits(self):
        self.assertEqual(anime_musicbrainz.CACHE_SCHEMA_VERSION, 2)
        first = theme(artists=("ASIAN KUNG-FU GENERATION",))
        second = theme(artists=("asian kung fu generation",))
        self.assertEqual(
            anime_musicbrainz.theme_mapping_key(first),
            anime_musicbrainz.theme_mapping_key(second),
        )
        document = anime_musicbrainz.failed_mapping(first)

        anime_musicbrainz.cache_mapping(first, document)

        self.assertEqual(anime_musicbrainz.cached_mapping(second), document)
        with api_cache.cache_db() as connection:
            key = connection.execute("SELECT cache_key FROM api_cache").fetchone()[0]
        self.assertTrue(key.startswith(f"{anime_musicbrainz.CACHE_NAMESPACE}:"))

    def test_worker_rechecks_registry_before_live_resolution(self):
        source = theme(song_id=4242)
        queued = anime_metadata.request_resolution("naruto", [source])
        self.assertTrue(queued["polling"])
        self.registry_get.return_value = registry_document(
            song_id=4242,
            title="Haruka Kanata",
            source_artist="ASIAN KUNG-FU GENERATION",
            provenance="manual-review",
        )

        with patch.object(anime_musicbrainz, "resolve_theme") as resolve:
            self.assertEqual(anime_metadata._drain_queue(), 1)

        resolve.assert_not_called()
        self.assertIsNone(anime_musicbrainz.cached_mapping(source))
        completed = anime_metadata.status("naruto", [source])
        mapping = next(iter(completed["mappings"].values()))
        self.assertEqual(mapping["mappingSource"], "local")
        self.assertFalse(completed["polling"])

    def test_worker_deduplicates_shared_song_and_reuses_cached_result(self):
        first = theme()
        second = {
            **theme(),
            "id": "theme-another-anime",
            "type": "ED",
            "sequence": 1,
        }
        resolved = {
            **anime_musicbrainz.pending_mapping(first),
            "state": "resolved",
            "queued": False,
            "recordingId": "recording-1",
            "releaseGroups": [{"id": "group-1", "name": "Haruka Kanata"}],
        }

        first_status = anime_metadata.request_resolution("naruto", [first])
        second_status = anime_metadata.request_resolution("other-anime", [second])

        self.assertTrue(first_status["polling"])
        self.assertTrue(second_status["polling"])
        with anime_metadata.queue_lock:
            self.assertEqual(len(anime_metadata.queued_themes), 1)

        with patch.object(anime_musicbrainz, "resolve_theme", return_value=resolved) as resolve:
            self.assertEqual(anime_metadata._drain_queue(), 1)

        resolve.assert_called_once()
        completed = anime_metadata.status("naruto", [first])
        self.assertEqual(completed["status"], "complete")
        self.assertFalse(completed["polling"])
        self.assertEqual(
            next(iter(completed["mappings"].values()))["recordingId"],
            "recording-1",
        )

        anime_metadata.request_resolution("naruto", [first])
        with anime_metadata.queue_lock:
            self.assertFalse(anime_metadata.queued_themes)


class AnimeMusicBrainzRegistryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="melodarr-seed-tests-")
        self.addCleanup(self.temporary.cleanup)
        database = os.path.join(self.temporary.name, "melodarr.db")
        settings = os.path.join(self.temporary.name, "settings.json")
        database_patch = patch.object(storage, "DATABASE", database)
        settings_patch = patch.object(storage, "SETTINGS_FILE", settings)
        database_patch.start()
        settings_patch.start()
        self.addCleanup(database_patch.stop)
        self.addCleanup(settings_patch.stop)
        storage.init_db()

        self.original_cache_database = api_cache.CACHE_DATABASE
        api_cache.CACHE_DATABASE = os.path.join(self.temporary.name, "metadata.db")
        api_cache.init_cache_db()
        self.addCleanup(
            setattr,
            api_cache,
            "CACHE_DATABASE",
            self.original_cache_database,
        )

    @patch("backend.services.anime_musicbrainz.musicbrainz.search")
    def test_seed_is_durable_and_a_later_local_choice_has_precedence(self, search):
        source = theme(
            song_id=12183,
            title="Shayou",
            artists=("Yorushika",),
        )

        seeded = anime_musicbrainz.resolve_theme(source)
        stored = anime_mapping_registry.get_mapping(12183)

        self.assertEqual(seeded["mappingSource"], "seed")
        self.assertEqual(stored["status"], "confirmed")
        self.assertEqual(stored["provenance"], "builtin-seed")
        self.assertEqual(stored["scope"], "commercial_full")
        self.assertEqual(
            stored["preferredTarget"]["releaseGroupId"],
            "6259b4f8-39b2-4b46-98e0-5dd433630abc",
        )
        self.assertIsNone(anime_musicbrainz.cached_mapping(source))
        search.assert_not_called()

        local_group = "11111111-2222-4333-8444-555555555555"
        anime_mapping_registry.upsert_mapping(
            12183,
            title="Shayou",
            artists=["Yorushika"],
            status="confirmed",
            provenance="manual-review",
            scope="song",
            targets=[{
                "releaseGroupId": local_group,
                "releaseGroupTitle": "User selected edition",
                "artistName": "Yorushika",
                "recordingIds": [],
                "artistIds": [],
                "primaryType": "Single",
                "firstReleaseDate": "",
                "preferred": True,
            }],
        )

        local = anime_musicbrainz.resolve_theme(source)

        self.assertEqual(local["mappingSource"], "local")
        self.assertEqual(local["releaseGroups"][0]["id"], local_group)
        search.assert_not_called()


if __name__ == "__main__":
    unittest.main()
