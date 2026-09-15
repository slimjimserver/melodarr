"""Durable artist enrichment, incremental discovery, and review precedence."""

if __package__:
    from ._test_environment import TEST_ROOT as _TEST_ROOT  # noqa: F401
else:  # Support unittest discovery with tests/ as the top-level directory.
    from _test_environment import TEST_ROOT as _TEST_ROOT  # noqa: F401

from . import test_anime_artist_links as fixtures
from .test_anime_artist_links import ALI, ANIME, theme, mapping
import unittest
from unittest.mock import patch
import time
from backend import storage
from backend.services import anime_musicbrainz as resolver, anime_theme_links
from backend.workers import anime_artist_enrichment as worker


class EnrichmentTests(unittest.TestCase):
    setUp = fixtures.ArtistLinksTests.setUp
    def seed(self):
        anime_theme_links.sync_anime_theme_mapping(ANIME, theme(), mapping())
        worker.discover()

    def snapshot(self, extra=False):
        performances = [{"themeId": 12830, "songId": 12787}]
        if extra:
            performances.append({"themeId": 12831, "songId": 12788})
        return {"id": 916, "slug": "ali", "name": "ALI", "anime": [
            {**ANIME, "performances": performances}]}

    def test_discovery_restart_and_lease(self):
        self.seed()
        job = worker._claim("anime_artist_refresh_jobs")
        self.assertEqual(job["artist_mbid"], ALI)
        storage.init_db()
        worker.discover()
        self.assertIsNone(worker._claim("anime_artist_refresh_jobs"))
        with storage.db() as connection:
            connection.execute("UPDATE anime_artist_refresh_jobs SET lease_until=0")
        self.assertIsNotNone(worker._claim("anime_artist_refresh_jobs"))

    def test_refresh_discovers_new_performances_without_duplicates(self):
        self.seed()
        job = worker._claim("anime_artist_refresh_jobs")
        with patch.object(worker.animethemes, "artist_detail", return_value=self.snapshot()):
            worker.refresh_artist(job)
        with patch.object(worker.animethemes, "artist_detail", return_value=self.snapshot(True)):
            worker.refresh_artist(job)
            worker.refresh_artist(job)
        with storage.db() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM anime_performance_jobs").fetchone()[0], 2)
            self.assertIn('12831', connection.execute("SELECT snapshot FROM anime_artist_refresh_jobs").fetchone()[0])

    def test_success_survives_cache_loss_and_is_not_rematched(self):
        self.seed()
        source = theme()
        job = {"anime_slug": ANIME["slug"], "theme_id": source["id"], "song_id": source["song"]["id"]}
        with patch.object(resolver, "registered_mapping", return_value=None), patch.object(resolver, "cached_mapping", return_value=None), patch.object(resolver, "set_cache_document"), patch.object(worker.animethemes, "detail", return_value={**ANIME, "themes": [source]}), patch.object(resolver, "_resolve_theme_live", return_value=mapping()) as match:
            worker.match_performance(job)
            storage.init_db()
            worker.match_performance(job)
            self.assertEqual(match.call_count, 1)
            self.assertEqual(match.call_args.kwargs["verified_artists"], {"ALI": ALI})
            with patch.object(resolver.time, "time", return_value=time.time() + 100000000):
                self.assertEqual(resolver.stored_mapping(source)["state"], "resolved")
        self.assertTrue(anime_theme_links.links_for_release_group("test-group"))

    def test_negative_results_have_retry_cooldown(self):
        with patch.object(resolver, "set_cache_document"), patch.object(resolver, "registered_mapping", return_value=None), patch.object(resolver, "cached_mapping", return_value=None):
            resolver.cache_mapping(theme(), mapping(state="unmatched"))
            self.assertEqual(resolver.stored_mapping(theme())["state"], "unmatched")
            with patch.object(resolver.time, "time", return_value=time.time() + 90000):
                self.assertIsNone(resolver.stored_mapping(theme()))

    def test_manual_decisions_override_durable_automatic_match(self):
        with patch.object(resolver, "set_cache_document"):
            resolver.cache_mapping(theme(), mapping())
        with patch.object(resolver, "registered_mapping", return_value={"state": "rejected"}):
            self.assertEqual(resolver.stored_mapping(theme()), {"state": "rejected"})

    def test_shared_song_cannot_be_claimed_concurrently(self):
        with storage.db() as connection:
            connection.executemany(
                "INSERT INTO anime_performance_jobs (anime_slug, theme_id, song_id) VALUES (?, ?, ?)",
                [("a", 1, 10), ("b", 2, 10), ("c", 3, 11)])
        first = worker._claim("anime_performance_jobs")
        second = worker._claim("anime_performance_jobs")
        self.assertEqual((first["song_id"], second["song_id"]), (10, 11))
        self.assertIsNone(worker._claim("anime_performance_jobs"))

    def test_artist_page_uses_saved_snapshot_without_provider_request(self):
        self.seed()
        job = worker._claim("anime_artist_refresh_jobs")
        with patch.object(worker.animethemes, "artist_detail", return_value=self.snapshot()):
            worker.refresh_artist(job)
        with patch.object(worker.animethemes, "artist_detail", side_effect=AssertionError("unexpected network")):
            result = worker.anime_artist_links.appearances(ALI)
        self.assertEqual(result["anime"][0]["performances"][0]["songId"], 12787)

    def test_preferred_single_is_saved_for_artist_cards(self):
        anime_theme_links.sync_anime_theme_mapping(
            ANIME, theme(), mapping(releaseGroups=[{"id": "single", "title": "Single"},
                                                  {"id": "album", "title": "Album"}],
                                   preferredReleaseGroupId="single"))
        targets = anime_theme_links.release_groups_for_performances([
            {"animeSlug": ANIME["slug"], "themeId": theme()["id"], "songId": theme()["song"]["id"]}])
        self.assertEqual([group["id"] for group in next(iter(targets.values())) if group["preferred"]], ["single"])

    def test_provider_failure_backoff_keeps_job(self):
        self.seed()
        with patch.object(worker.animethemes, "artist_detail", side_effect=ValueError("offline")), patch.object(worker.logger, "exception"):
            self.assertTrue(worker.tick())
        with storage.db() as connection:
            row = connection.execute("SELECT * FROM anime_artist_refresh_jobs").fetchone()
            self.assertEqual(row["failures"], 1)
            self.assertEqual(row["lease_until"], 0)
            self.assertGreater(row["due_at"], time.time())
