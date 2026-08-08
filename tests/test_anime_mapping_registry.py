"""Durability and invariants for curated AnimeThemes song mappings."""

if __package__:
    from ._test_environment import TEST_ROOT
else:  # Support direct execution: python tests/test_anime_mapping_registry.py
    from _test_environment import TEST_ROOT

import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from backend import storage
from backend.services import anime_mapping_registry as registry
from backend.services import anime_theme_links


SHAYOU_GROUP = "6259b4f8-39b2-4b46-98e0-5dd433630abc"
ALTERNATE_GROUP = "11111111-2222-4333-8444-555555555555"
RECORDING_ONE = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
RECORDING_TWO = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"
ARTIST_ONE = "cccccccc-dddd-4eee-8fff-000000000000"


def target(release_group_id=SHAYOU_GROUP, **overrides):
    value = {
        "releaseGroupId": release_group_id,
        "recordingIds": [RECORDING_ONE],
        "artistIds": [ARTIST_ONE],
        "releaseGroupTitle": "斜陽",
        "artistName": "ヨルシカ",
        "primaryType": "Single",
        "firstReleaseDate": "2023-05-08",
    }
    value.update(overrides)
    if "recordingId" in overrides and "recordingIds" not in overrides:
        value.pop("recordingIds", None)
    if "artistId" in overrides and "artistIds" not in overrides:
        value.pop("artistIds", None)
    return value


class AnimeMappingRegistryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="melodarr-anime-registry-")
        self.addCleanup(self.directory.cleanup)
        database = os.path.join(self.directory.name, "melodarr.db")
        settings = os.path.join(self.directory.name, "settings.json")
        database_patch = patch.object(storage, "DATABASE", database)
        settings_patch = patch.object(storage, "SETTINGS_FILE", settings)
        database_patch.start()
        settings_patch.start()
        self.addCleanup(database_patch.stop)
        self.addCleanup(settings_patch.stop)
        storage.init_db()

    def _users(self):
        with storage.db() as connection:
            submitter_id = connection.execute(
                "INSERT INTO users "
                "(username, password_hash, role, created_at) "
                "VALUES ('listener', 'hash', 'user', 1)"
            ).lastrowid
            reviewer_id = connection.execute(
                "INSERT INTO users "
                "(username, password_hash, role, created_at) "
                "VALUES ('reviewer', 'hash', 'admin', 1)"
            ).lastrowid
        return submitter_id, reviewer_id

    def _proposal(self, submitter_id, release_group_id=ALTERNATE_GROUP):
        return registry.submit_mapping_proposal(
            submitter_id,
            anime_slug="boku_no_kokoro_no_yabai_yatsu",
            anime_name="The Dangers in My Heart",
            theme_id=1477,
            theme_label="Opening 1",
            song_id=2451,
            song_title="Shayou",
            artists=["Yorushika"],
            target=target(
                release_group_id,
                releaseGroupTitle="Proposed single",
            ),
        )

    def test_pending_proposal_does_not_replace_a_confirmed_mapping(self):
        submitter_id, _ = self._users()
        confirmed = registry.upsert_mapping(
            2451,
            title="Shayou",
            artists=["Yorushika"],
            status="confirmed",
            provenance="admin-review",
            targets=[target()],
        )

        proposal = self._proposal(submitter_id)

        self.assertEqual(proposal["status"], "pending")
        self.assertEqual(proposal["submittedBy"]["username"], "listener")
        self.assertEqual(proposal["releaseGroupMbid"], ALTERNATE_GROUP)
        self.assertEqual(
            proposal["musicBrainzUrl"],
            f"https://musicbrainz.org/release-group/{ALTERNATE_GROUP}",
        )
        self.assertEqual(registry.get_mapping(2451), confirmed)

        revised = self._proposal(submitter_id, SHAYOU_GROUP)
        self.assertEqual(revised["id"], proposal["id"])
        self.assertEqual(revised["releaseGroupMbid"], SHAYOU_GROUP)
        visible = registry.mapping_proposals_for_anime(
            "boku_no_kokoro_no_yabai_yatsu",
            submitter_user_id=submitter_id,
        )
        self.assertEqual([item["id"] for item in visible], [proposal["id"]])

    def test_approval_atomically_publishes_proposal(self):
        submitter_id, reviewer_id = self._users()
        registry.upsert_mapping(
            2451,
            title="Old title",
            artists=["Old artist"],
            status="confirmed",
            provenance="admin-review",
            targets=[target()],
        )
        proposal = self._proposal(submitter_id)

        approved = registry.approve_mapping_proposal(proposal["id"], reviewer_id)

        self.assertEqual(approved["status"], "approved")
        self.assertEqual(approved["reviewedByUserId"], reviewer_id)
        mapping = registry.get_mapping(2451)
        self.assertEqual(mapping["status"], "confirmed")
        self.assertEqual(mapping["provenance"], f"user-proposal:{proposal['id']}")
        self.assertEqual(
            mapping["preferredTarget"]["releaseGroupId"],
            ALTERNATE_GROUP,
        )

    def test_failed_approval_rolls_back_mapping_and_proposal_state(self):
        submitter_id, reviewer_id = self._users()
        original = registry.upsert_mapping(
            2451,
            title="Shayou",
            artists=["Yorushika"],
            status="confirmed",
            provenance="admin-review",
            targets=[target()],
        )
        proposal = self._proposal(submitter_id)

        with patch.object(
            registry,
            "_insert_targets",
            side_effect=sqlite3.IntegrityError("simulated approval failure"),
        ), self.assertRaises(sqlite3.IntegrityError):
            registry.approve_mapping_proposal(proposal["id"], reviewer_id)

        self.assertEqual(registry.get_mapping(2451), original)
        self.assertEqual(
            registry.get_mapping_proposal(proposal["id"])["status"],
            "pending",
        )

    def test_rejection_preserves_existing_confirmed_mapping(self):
        submitter_id, reviewer_id = self._users()
        confirmed = registry.upsert_mapping(
            2451,
            title="Shayou",
            artists=["Yorushika"],
            status="confirmed",
            provenance="admin-review",
            targets=[target()],
        )
        proposal = self._proposal(submitter_id)

        rejected = registry.reject_mapping_proposal(proposal["id"], reviewer_id)

        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(registry.get_mapping(2451), confirmed)

    def test_upsert_persists_multiple_targets_and_one_preferred_target(self):
        with patch.object(registry.time, "time", return_value=100.5):
            mapping = registry.upsert_mapping(
                2451,
                title="Shayou",
                artists=["Yorushika", "ヨルシカ"],
                status="proposed",
                provenance="musicbrainz-discography-match-v2",
                scope="song",
                preferred_release_group_mbid=SHAYOU_GROUP,
                targets=[
                    target(scope="commercial_full"),
                    target(
                        ALTERNATE_GROUP,
                        recordingIds=[RECORDING_TWO],
                        releaseGroupTitle="Alternate edition",
                        firstReleaseDate="2023-06-01",
                        scope="theme_edit",
                    ),
                ],
            )

        self.assertEqual(mapping, registry.get_mapping("2451"))
        self.assertEqual(mapping["songId"], 2451)
        self.assertEqual(mapping["title"], "Shayou")
        self.assertEqual(mapping["artists"], ["Yorushika", "ヨルシカ"])
        self.assertEqual(mapping["status"], "proposed")
        self.assertEqual(mapping["provenance"], "musicbrainz-discography-match-v2")
        self.assertEqual(mapping["scope"], "commercial_full")
        self.assertEqual(mapping["schemaVersion"], registry.SCHEMA_VERSION)
        self.assertEqual(mapping["createdAt"], 100.5)
        self.assertEqual(mapping["updatedAt"], 100.5)
        self.assertEqual(len(mapping["targets"]), 2)
        self.assertEqual(
            mapping["preferredTarget"]["releaseGroupId"],
            SHAYOU_GROUP,
        )
        self.assertEqual(mapping["preferredTarget"]["recordingIds"], [RECORDING_ONE])
        self.assertEqual(mapping["preferredTarget"]["artistIds"], [ARTIST_ONE])
        self.assertEqual(mapping["preferredTarget"]["releaseGroupTitle"], "斜陽")
        self.assertEqual(mapping["preferredTarget"]["artistName"], "ヨルシカ")
        self.assertEqual(mapping["preferredTarget"]["primaryType"], "Single")
        self.assertEqual(mapping["preferredTarget"]["scope"], "commercial_full")
        self.assertEqual(mapping["targets"][1]["scope"], "theme_edit")

    def test_upsert_replaces_targets_and_preserves_creation_timestamps(self):
        with patch.object(registry.time, "time", return_value=100):
            registry.upsert_mapping(
                2451,
                title="Suu Sentimental",
                artists="Kohana Lam",
                status="proposed",
                provenance="resolver",
                targets=[target()],
            )

        with patch.object(registry.time, "time", return_value=200):
            mapping = registry.upsert_mapping(
                2451,
                title="Suu Sentimental",
                artists=["Kohana Lam"],
                status="confirmed",
                provenance="manual-review",
                scope="anime-theme-song",
                targets=[target(recordingId=RECORDING_TWO)],
            )

        self.assertEqual(mapping["status"], "confirmed")
        self.assertEqual(mapping["provenance"], "manual-review")
        self.assertEqual(mapping["scope"], "anime-theme-song")
        self.assertEqual(mapping["createdAt"], 100)
        self.assertEqual(mapping["updatedAt"], 200)
        self.assertEqual(mapping["targets"][0]["createdAt"], 100)
        self.assertEqual(mapping["targets"][0]["updatedAt"], 200)
        # The singular compatibility alias is normalized to the durable list.
        self.assertEqual(mapping["targets"][0]["recordingIds"], [RECORDING_TWO])

    def test_delete_cascades_targets_and_reports_whether_mapping_existed(self):
        registry.upsert_mapping(
            2451,
            title="Shayou",
            artists=["Yorushika"],
            status="confirmed",
            provenance="manual-review",
            targets=[target()],
        )

        self.assertTrue(registry.delete_mapping(2451))
        self.assertFalse(registry.delete_mapping(2451))
        self.assertIsNone(registry.get_mapping(2451))
        with storage.db() as connection:
            target_count = connection.execute(
                "SELECT COUNT(*) FROM anime_song_mapping_targets"
            ).fetchone()[0]
        self.assertEqual(target_count, 0)

    def test_create_if_absent_never_overwrites_a_local_correction(self):
        seeded = registry.create_mapping_if_absent(
            2451,
            title="Shayou",
            artists=["Yorushika"],
            status="proposed",
            provenance="builtin-seed-v1",
            targets=[target()],
        )
        self.assertEqual(seeded["provenance"], "builtin-seed-v1")

        corrected = registry.upsert_mapping(
            2451,
            title="斜陽",
            artists=["ヨルシカ"],
            status="confirmed",
            provenance="admin-correction",
            targets=[target(
                ALTERNATE_GROUP,
                releaseGroupTitle="斜陽 (corrected edition)",
            )],
        )
        returned = registry.create_mapping_if_absent(
            2451,
            title="Stale seed title",
            artists=["Stale artist"],
            status="proposed",
            provenance="builtin-seed-v2",
            targets=[target()],
        )

        self.assertEqual(returned, corrected)
        self.assertEqual(returned["status"], "confirmed")
        self.assertEqual(returned["provenance"], "admin-correction")
        self.assertEqual(
            returned["preferredTarget"]["releaseGroupId"],
            ALTERNATE_GROUP,
        )

    def test_registry_rejects_invalid_status_ids_duplicates_and_preferences(self):
        cases = [
            {"song_id": 0},
            {"song_id": 1.9},
            {"title": {"unexpected": "object"}},
            {"status": "verified"},
            {"targets": [target("not-an-mbid")]},
            {"targets": [target(preferred="false")]},
            {"targets": [target(), target()]},
            {"targets": [target(), target(ALTERNATE_GROUP)]},
            {
                "targets": [
                    target(preferred=True),
                    target(ALTERNATE_GROUP, preferred=True),
                ]
            },
        ]
        defaults = {
            "song_id": 2451,
            "title": "Shayou",
            "artists": ["Yorushika"],
            "status": "proposed",
            "provenance": "resolver",
            "targets": [target()],
        }
        for override in cases:
            values = {**defaults, **override}
            song_id = values.pop("song_id")
            with self.subTest(override=override), self.assertRaises(ValueError):
                registry.upsert_mapping(song_id, **values)

    def test_rejection_is_a_durable_targetless_tombstone(self):
        rejected = registry.reject_mapping(
            2451,
            title="Shayou",
            artists=["Yorushika"],
            provenance="manual-rejection",
        )

        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(rejected["scope"], "unknown")
        self.assertEqual(rejected["targets"], [])
        self.assertIsNone(rejected["preferredTarget"])
        self.assertEqual(registry.get_mapping(2451), rejected)

    def test_failed_target_replacement_rolls_back_the_existing_mapping(self):
        original = registry.upsert_mapping(
            2451,
            title="Shayou",
            artists=["Yorushika"],
            status="confirmed",
            provenance="manual-review",
            targets=[target()],
        )

        with patch.object(
            registry,
            "_insert_targets",
            side_effect=sqlite3.IntegrityError("simulated target failure"),
        ), self.assertRaises(sqlite3.IntegrityError):
            registry.upsert_mapping(
                2451,
                title="Replacement",
                artists=["Different artist"],
                status="confirmed",
                provenance="manual-review",
                targets=[target(ALTERNATE_GROUP)],
            )

        self.assertEqual(registry.get_mapping(2451), original)

    def test_init_db_migrates_v1_registry_rows_and_rejection_constraint(self):
        with storage.db() as connection:
            connection.execute("DROP TABLE anime_song_mapping_targets")
            connection.execute("DROP TABLE anime_song_mappings")
            connection.execute("""
                CREATE TABLE anime_song_mappings (
                    song_id INTEGER PRIMARY KEY CHECK(song_id > 0),
                    title_snapshot TEXT NOT NULL,
                    artists_json TEXT NOT NULL,
                    status TEXT NOT NULL
                        CHECK(status IN ('proposed', 'confirmed')),
                    provenance TEXT NOT NULL,
                    mapping_scope TEXT NOT NULL,
                    schema_version INTEGER NOT NULL CHECK(schema_version > 0),
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
            """)
            connection.execute("""
                CREATE TABLE anime_song_mapping_targets (
                    song_id INTEGER NOT NULL REFERENCES anime_song_mappings(song_id)
                        ON DELETE CASCADE,
                    release_group_mbid TEXT NOT NULL,
                    recording_mbids_json TEXT NOT NULL DEFAULT '[]',
                    artist_mbids_json TEXT NOT NULL DEFAULT '[]',
                    release_group_title TEXT NOT NULL,
                    artist_name TEXT NOT NULL,
                    primary_type TEXT NOT NULL,
                    first_release_date TEXT NOT NULL,
                    is_preferred INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(song_id, release_group_mbid)
                )
            """)
            connection.execute(
                "INSERT INTO anime_song_mappings VALUES "
                "(2451, 'Shayou', '[\"Yorushika\"]', 'confirmed', "
                "'manual', 'commercial_full', 1, 10, 10)"
            )
            connection.execute(
                "INSERT INTO anime_song_mapping_targets VALUES "
                "(2451, ?, '[]', '[]', '斜陽', 'ヨルシカ', "
                "'Single', '2023-05-08', 1, 10, 10)",
                (SHAYOU_GROUP,),
            )

        storage.init_db()

        migrated = registry.get_mapping(2451)
        self.assertEqual(migrated["preferredTarget"]["scope"], "commercial_full")
        rejected = registry.reject_mapping(
            2451,
            title="Shayou",
            artists=["Yorushika"],
        )
        self.assertEqual(rejected["status"], "rejected")
        with storage.db() as connection:
            foreign_key = connection.execute(
                "PRAGMA foreign_key_list(anime_song_mapping_targets)"
            ).fetchone()
        self.assertEqual(foreign_key["table"], "anime_song_mappings")

    def test_database_prevents_two_preferred_targets_for_one_song(self):
        registry.upsert_mapping(
            2451,
            title="Shayou",
            artists=["Yorushika"],
            status="confirmed",
            provenance="manual-review",
            targets=[target()],
        )
        with self.assertRaises(sqlite3.IntegrityError), storage.db() as connection:
            connection.execute(
                "INSERT INTO anime_song_mapping_targets "
                "(song_id, release_group_mbid, recording_mbids_json, "
                "artist_mbids_json, release_group_title, artist_name, "
                "primary_type, first_release_date, mapping_scope, is_preferred, "
                "created_at, updated_at) "
                "VALUES (?, ?, '[]', '[]', ?, ?, '', '', 'unknown', 1, 1, 1)",
                (2451, ALTERNATE_GROUP, "Alternate", "Yorushika"),
            )


class AnimeThemeLinkTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="melodarr-anime-links-")
        self.addCleanup(self.directory.cleanup)
        database = os.path.join(self.directory.name, "melodarr.db")
        settings = os.path.join(self.directory.name, "settings.json")
        database_patch = patch.object(storage, "DATABASE", database)
        settings_patch = patch.object(storage, "SETTINGS_FILE", settings)
        database_patch.start()
        settings_patch.start()
        self.addCleanup(database_patch.stop)
        self.addCleanup(settings_patch.stop)
        storage.init_db()
        self.anime = {"slug": "naruto", "name": "Naruto"}
        self.theme = {
            "id": 1477,
            "label": "Opening 2",
            "type": "OP",
            "sequence": 2,
            "song": {"id": 1477, "title": "Haruka Kanata"},
        }

    def test_sync_dedupes_resolved_groups_and_supports_multiple_anime(self):
        mapping = {
            "state": "resolved",
            "releaseGroups": [
                {"id": SHAYOU_GROUP},
                {"id": SHAYOU_GROUP},
                {"id": ALTERNATE_GROUP},
            ],
        }
        with patch.object(anime_theme_links.detail_cache, "invalidate") as invalidate:
            changed = anime_theme_links.sync_anime_theme_mapping(
                self.anime, self.theme, mapping
            )
            unchanged = anime_theme_links.sync_anime_theme_mapping(
                self.anime, self.theme, mapping
            )
            anime_theme_links.sync_anime_theme_mapping(
                {"slug": "boruto", "name": "Boruto"},
                {**self.theme, "id": 2000, "label": "Opening 1", "sequence": 1},
                {"state": "resolved", "releaseGroups": [{"id": SHAYOU_GROUP}]},
            )

        self.assertTrue(changed)
        self.assertFalse(unchanged)
        links = anime_theme_links.links_for_release_group(SHAYOU_GROUP)
        self.assertEqual([link["animeSlug"] for link in links], ["boruto", "naruto"])
        naruto = links[1]
        self.assertEqual(naruto["animePath"], "/anime/naruto#theme-1477")
        self.assertEqual(naruto["themeType"], "OP")
        self.assertEqual(naruto["sequence"], 2)
        self.assertEqual(naruto["songTitle"], "Haruka Kanata")
        self.assertEqual(invalidate.call_count, 3)

    def test_unresolved_or_proposed_mapping_removes_stale_links(self):
        with patch.object(anime_theme_links.detail_cache, "invalidate") as invalidate:
            anime_theme_links.sync_anime_theme_mapping(
                self.anime,
                self.theme,
                {"state": "resolved", "releaseGroups": [{"id": SHAYOU_GROUP}]},
            )
            changed = anime_theme_links.sync_anime_theme_mapping(
                self.anime,
                self.theme,
                {
                    "state": "resolved",
                    "registryStatus": "proposed",
                    "releaseGroups": [{"id": ALTERNATE_GROUP}],
                },
            )

        self.assertTrue(changed)
        self.assertEqual(anime_theme_links.links_for_release_group(SHAYOU_GROUP), [])
        self.assertEqual(anime_theme_links.links_for_release_group(ALTERNATE_GROUP), [])
        invalidate.assert_any_call(("release-group", SHAYOU_GROUP))

    def test_confirmed_registry_targets_are_supported(self):
        changed = anime_theme_links.sync_anime_theme_mapping(
            self.anime,
            self.theme,
            {
                "status": "confirmed",
                "targets": [{"releaseGroupId": SHAYOU_GROUP}],
            },
        )

        self.assertTrue(changed)
        self.assertEqual(
            anime_theme_links.links_for_release_group(SHAYOU_GROUP)[0]["themeId"],
            1477,
        )


if __name__ == "__main__":
    unittest.main()
