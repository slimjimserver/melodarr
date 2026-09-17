"""Persistent anime artist evidence and complete performance expansion."""
from ._test_environment import TEST_ROOT
import os
import tempfile
import unittest
from unittest.mock import patch
from backend import storage
from backend.services import anime_artist_links as links, anime_theme_links, animethemes

ALI = "a0bb95b8-9fd9-47dd-af28-d4210f7f2a11"
OTHER = "cccccccc-dddd-4eee-8fff-000000000000"
ANIME = {"slug": "the_fable", "name": "The Fable"}


def theme(artists=None):
    return {"id": 12830, "label": "Opening 1", "type": "OP", "sequence": 1,
            "song": {"id": 12787, "title": "Professionalism", "artists": artists or [
                {"id": 916, "slug": "ali", "name": "ALI"}]}}


def mapping(ids=None, **extra):
    return {"state": "resolved", "artistIds": ids or [ALI],
            "releaseGroups": [{"id": "test-group"}], **extra}


class ArtistLinksTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(dir=TEST_ROOT)
        self.addCleanup(directory.cleanup)
        for field, filename in [("DATABASE", "db.sqlite"), ("SETTINGS_FILE", "settings.json")]:
            patcher = patch.object(storage, field, os.path.join(directory.name, filename))
            patcher.start()
            self.addCleanup(patcher.stop)
        storage.init_db()

    def rows(self):
        with storage.db() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM anime_artist_links")]

    def test_single_credit_persists_and_rejection_removes(self):
        anime_theme_links.sync_anime_theme_mapping(ANIME, theme(), mapping())
        storage.init_db()
        self.assertEqual([(row["artist_mbid"], row["animethemes_artist_id"], row["verified"]) for row in self.rows()], [(ALI, 916, 1)])
        anime_theme_links.sync_anime_theme_mapping(ANIME, theme(), mapping(state="unmatched"))
        self.assertEqual(self.rows(), [])

    def test_collaboration_never_zips_sorted_ids_and_verifies_alias(self):
        source = theme([{"id": 916, "slug": "ali", "name": "ALI"},
                        {"id": 999, "slug": "hannya", "name": "Hannya"}])
        anime_theme_links.sync_anime_theme_mapping(ANIME, source, mapping([OTHER, ALI]))
        self.assertTrue(all(row["verified"] == 0 for row in self.rows()))
        with patch.object(links.musicbrainz, "get", return_value={"name": "Japanese name", "aliases": [{"name": "ALI"}]}), patch.object(animethemes, "artist_detail", return_value={"id": 916, "slug": "ali", "name": "ALI", "anime": []}) as provider:
            result = links.appearances(ALI)
        self.assertEqual(result["artistLinks"][0]["id"], 916)
        provider.assert_called_once_with("ali")
        self.assertEqual([row["animethemes_artist_id"] for row in self.rows() if row["verified"]], [916])

    def test_proposed_and_various_artists_do_not_link(self):
        anime_theme_links.sync_anime_theme_mapping(ANIME, theme(), mapping(registryStatus="proposed"))
        anime_theme_links.sync_anime_theme_mapping(ANIME, theme(), mapping([links._VARIOUS_ARTISTS]))
        self.assertEqual(self.rows(), [])

    def test_changed_mapping_and_multiple_sources(self):
        anime_theme_links.sync_anime_theme_mapping(ANIME, theme(), mapping())
        second = {**theme(), "id": 13057}
        anime_theme_links.sync_anime_theme_mapping(ANIME, second, mapping())
        anime_theme_links.sync_anime_theme_mapping(ANIME, theme(), mapping([OTHER]))
        self.assertEqual(len(self.rows()), 2)
        with patch.object(animethemes, "artist_detail") as provider:
            self.assertEqual(links.appearances(ALI)["artistLinks"], [])
            provider.assert_not_called()
        anime_theme_links.sync_anime_theme_mapping(ANIME, theme(), mapping(state="unmatched"))
        self.assertEqual(self.rows()[0]["artist_mbid"], ALI)

    def test_unknown_artist_needs_no_network(self):
        with patch.object(links.musicbrainz, "get") as mb, patch.object(animethemes, "artist_detail") as provider:
            self.assertEqual(links.appearances(ALI), {"anime": [], "artistLinks": []})
            mb.assert_not_called()
            provider.assert_not_called()

    def test_provider_groups_anime_preserving_op_ed_and_other_appearances(self):
        anime = {"id": 4274, **ANIME, "year": 2024}
        opening = {"id": 12830, "type": "OP", "sequence": 1, "anime": anime}
        ending = {"id": 13057, "type": "ED", "sequence": 2, "anime": anime}
        beastars = {"id": 8781, "type": "OP", "anime": {"id": 259, "slug": "beastars", "name": "Beastars"}}
        payload = {"artist": {"id": 916, "name": "ALI", "slug": "ali", "songs": [
            {"id": 1, "title": "Professionalism", "animethemes": [opening, opening]},
            {"id": 2, "title": "BEYOND", "animethemes": [ending]},
            {"id": 3, "title": "Wild Side", "animethemes": [beastars]}]}}
        with patch.object(animethemes, "cached_json_get", return_value=payload):
            result = animethemes.artist_detail("ali")
        self.assertEqual(len(result["anime"]), 2)
        self.assertEqual([p["themeLabel"] for p in result["anime"][0]["performances"]], ["Opening 1", "Ending 2"])
    def test_previously_confirmed_song_backfills_artist_identity(self):
        from backend.services import anime_mapping_registry as registry
        registry.upsert_mapping(
            12787, title="Professionalism", artists=["ALI"], status="confirmed",
            provenance="manual", targets=[{
                "releaseGroupId": OTHER, "artistIds": [ALI],
                "releaseGroupTitle": "Professionalism", "artistName": "ALI",
            }],
        )
        anime_theme_links.sync_anime_theme_mapping(ANIME, theme(), mapping())
        with storage.db() as connection:
            connection.execute("DELETE FROM anime_artist_links")
        with patch.object(animethemes, "detail", return_value={**ANIME, "themes": [theme()]}) as detail, patch.object(animethemes, "artist_detail", return_value={"id": 916, "name": "ALI", "slug": "ali", "anime": []}):
            result = links.appearances(ALI)
            self.assertEqual(result["artistLinks"][0]["id"], 916)
            links.appearances(ALI)
            detail.assert_called_once_with("the_fable")
        self.assertEqual(self.rows()[0]["artist_mbid"], ALI)

    def test_route_authentication_validation_and_provider_failure(self):
        import requests
        from flask import Flask
        from backend.routes.music import blueprint
        app = Flask(__name__)
        app.config.update(TESTING=True, SECRET_KEY="test-secret")
        app.register_blueprint(blueprint)
        client = app.test_client()
        with patch("backend.security.current_user", return_value=None):
            self.assertEqual(client.get(f"/api/music/artist/{ALI}/anime").status_code, 401)
        with patch("backend.security.current_user", return_value={"id": 1}):
            self.assertEqual(client.get("/api/music/artist/invalid/anime").status_code, 400)
            self.assertEqual(client.get(f"/api/music/artist/{ALI}/anime").get_json(), {"anime": [], "artistLinks": []})
            with patch.object(links, "appearances", side_effect=requests.ConnectionError()):
                self.assertEqual(client.get(f"/api/music/artist/{ALI}/anime").status_code, 502)
    def test_reverse_artist_links_are_verified_unique_and_shared_across_anime(self):
        source = theme()
        result = mapping()
        anime_theme_links.sync_anime_theme_mapping(ANIME, source, result)
        self.assertEqual(result["artistLinks"], {"916": ALI})
        other_anime = {"slug": "beastars", "name": "Beastars"}
        unresolved = mapping(state="unmatched")
        anime_theme_links.sync_anime_theme_mapping(other_anime, source, unresolved)
        self.assertEqual(unresolved["artistLinks"], {"916": ALI})
        # Unknown collaborators do not borrow the verified performer's ID.
        self.assertEqual(links.musicbrainz_links([
            {"id": 916}, {"id": 999}, "Legacy name",
        ]), {"916": ALI})
        anime_theme_links.sync_anime_theme_mapping(other_anime, source, mapping([OTHER]))
        self.assertEqual(links.musicbrainz_links(source["song"]["artists"]), {})
        anime_theme_links.sync_anime_theme_mapping(other_anime, source, mapping(state="unmatched"))
        anime_theme_links.sync_anime_theme_mapping(ANIME, source, mapping(state="unmatched"))
        self.assertEqual(links.musicbrainz_links(source["song"]["artists"]), {})
    def test_release_card_anime_names_are_deduplicated_and_removed(self):
        first = theme()
        anime_theme_links.sync_anime_theme_mapping(ANIME, first, mapping())
        anime_theme_links.sync_anime_theme_mapping(ANIME, {**first, "id": 13057}, mapping())
        self.assertEqual(anime_theme_links.anime_names_for_release_groups(["TEST-GROUP", "missing"]), {"test-group": ["The Fable"]})
        anime_theme_links.sync_anime_theme_mapping(ANIME, first, mapping(state="unmatched"))
        self.assertEqual(anime_theme_links.anime_names_for_release_groups(["test-group"]), {"test-group": ["The Fable"]})
        anime_theme_links.sync_anime_theme_mapping(ANIME, {**first, "id": 13057}, mapping(state="unmatched"))
        self.assertEqual(anime_theme_links.anime_names_for_release_groups(["test-group"]), {})
    def test_performance_targets_use_saved_matches_and_respect_rejection(self):
        from backend.services import anime_mapping_registry as registry
        performance = {"animeSlug": "the_fable", "themeId": 12830, "songId": 12787}
        anime_theme_links.sync_anime_theme_mapping(ANIME, theme(), mapping())
        result = anime_theme_links.release_groups_for_performances([performance])
        self.assertEqual(result[("the_fable", 12830)][0]["id"], "test-group")
        registry.upsert_mapping(12787, title="Professionalism", artists=["ALI"],
            status="confirmed", provenance="manual", targets=[{
                "releaseGroupId": OTHER, "artistIds": [ALI],
                "releaseGroupTitle": "Preferred single", "artistName": "ALI",
            }], preferred_release_group_mbid=OTHER)
        result = anime_theme_links.release_groups_for_performances([performance])
        self.assertEqual(result[("the_fable", 12830)], [{"id": OTHER, "title": "Preferred single", "preferred": True}])
        registry.reject_mapping(12787, title="Professionalism", artists=["ALI"])
        self.assertEqual(anime_theme_links.release_groups_for_performances([performance])[("the_fable", 12830)], [])

    def test_artist_anime_endpoint_adds_live_request_states(self):
        from flask import Flask
        from backend.routes import music
        app = Flask(__name__)
        app.config.update(TESTING=True, SECRET_KEY="test-secret")
        app.register_blueprint(music.blueprint)
        performance = {"animeSlug": "the_fable", "themeId": 12830, "songId": 12787}
        targets = {("the_fable", 12830): [{"id": ALI}, {"id": OTHER}, {"id": "new"}, {"id": "queued"}]}
        with patch("backend.security.current_user", return_value={"id": 1}), patch.object(links, "appearances", return_value={"anime": [{"performances": [performance]}]}), patch.object(anime_theme_links, "release_groups_for_performances", return_value=targets), patch.object(music.lidarr, "cached_library_availability", return_value={ALI: {"fullyAvailable": True}, OTHER: {"fullyAvailable": False}}), patch.object(music, "_download_snapshot", return_value={}), patch.object(music, "pending_lidarr_search_mbids", return_value={"queued"}):
            response = app.test_client().get(f"/api/music/artist/{ALI}/anime")
        self.assertEqual(response.status_code, 200)
        groups = response.get_json()["anime"][0]["performances"][0]["releaseGroups"]
        self.assertTrue(groups[0]["fullyAvailableInLidarr"])
        self.assertTrue(groups[1]["availableInLidarr"])
        self.assertFalse(groups[1]["fullyAvailableInLidarr"])
        self.assertFalse(groups[2]["availableInLidarr"])
        self.assertEqual(groups[3]["requestStatus"], "queued")
    def test_automatic_release_options_keep_distinct_release_titles(self):
        source = mapping()
        source["releaseGroups"] = [
            {"id": "single", "title": "Burning"},
            {"id": "album", "title": "D o n’t L a u g h I t O f f"},
        ]
        anime_theme_links.sync_anime_theme_mapping(ANIME, theme(), source)
        performance = {"animeSlug": "the_fable", "themeId": 12830, "songId": 12787}
        with patch.object(anime_theme_links.musicbrainz, "get") as get:
            options = anime_theme_links.release_groups_for_performances([performance])[("the_fable", 12830)]
            get.assert_not_called()
        self.assertEqual({item["id"]: item["title"] for item in options}, {
            "single": "Burning", "album": "D o n’t L a u g h I t O f f",
        })

    def test_legacy_release_titles_are_repaired_once_and_preserved(self):
        source = mapping()
        source["releaseGroups"] = [{"id": OTHER}]
        anime_theme_links.sync_anime_theme_mapping(ANIME, theme(), source)
        performance = {"animeSlug": "the_fable", "themeId": 12830, "songId": 12787}
        with patch.object(anime_theme_links.musicbrainz, "get", autospec=True, return_value={"id": OTHER, "title": "Album title"}) as get:
            first = anime_theme_links.release_groups_for_performances([performance])
            anime_theme_links.sync_anime_theme_mapping(ANIME, theme(), source)
            second = anime_theme_links.release_groups_for_performances([performance])
            self.assertEqual(first, second)
            self.assertEqual(first[("the_fable", 12830)][0]["title"], "Album title")
            get.assert_called_once_with(f"/release-group/{OTHER}", "")
