"""Cold Summary publication must not wait for the slowest identity."""

from ._test_environment import TEST_ROOT

import io
import logging
import runpy
import time
from contextlib import contextmanager, nullcontext
from pathlib import Path
from queue import Queue
from threading import Event, Lock, Thread
from unittest.mock import Mock, patch

import requests

from backend import api_cache
from backend.services import artist_summary as summary, deezer, musicbrainz, wikipedia
from backend.workers import artist_summary as worker
from tests.test_artist_summary import ARTIST, GROUP, SATIVA, release, track
from tests.test_backend import DatabaseTestCase


class ArtistSummaryProgressTests(DatabaseTestCase):
    def save(self, source, **value):
        document = {"fetched_at": time.time(), "resolver_version": summary.RESOLVER_VERSION, **value}
        api_cache.set_cache_document(summary.SNAPSHOT_NAMESPACE, f"{source}:{ARTIST}", document, summary.RETENTION_TTL)
        return document

    def await_summary(self, predicate):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            value = worker.request_summary(ARTIST)
            if predicate(value):
                return value
            time.sleep(0.01)
        self.fail("Expected progressive Summary state did not arrive")

    @contextmanager
    def refresh(self, items, resolve=None, details=None, gates=(), bio=True):
        if bio:
            self.save("bio", bio={"text": "Biography"})
        api_cache.set_cache_document(summary.IDENTITY_NAMESPACE, f"artist:{ARTIST}", {
            "complete": True, "deezer_artist_id": 42,
        }, summary.RETENTION_TTL)
        with patch.object(worker, "jobs", Queue(maxsize=32)), patch.object(worker, "_started", True), \
                patch.object(deezer, "top_tracks", return_value=items), \
                patch.object(deezer, "track", side_effect=details or track):
            with patch.object(summary, "resolve_track", side_effect=resolve) if resolve else nullcontext():
                worker.request_summary(ARTIST)
                thread = Thread(target=worker.process_job, args=(ARTIST, "top_tracks"))
                thread.start()
                try:
                    yield
                finally:
                    for gate in gates:
                        gate.set()
                    thread.join(15)
                    self.assertFalse(thread.is_alive(), "Refresh leaked a running resolver")

    def test_ordered_rows_publish_before_even_the_first_detail_finishes(self):
        gate = Event()
        items = [{**track(9), "rank": 1}, {**track(1), "rank": 999}]

        def details(track_id):
            self.assertTrue(gate.wait(10))
            return track(track_id)

        with self.refresh(items, lambda *args, **kwargs: {"recording_mbid": None, "release_group_mbid": None}, details, [gate]):
            value = self.await_summary(lambda value: len(value["topTracks"]) == 2)
            self.assertEqual([row["deezer_track_id"] for row in value["topTracks"]], [9, 1])
            self.assertEqual([row["position"] for row in value["topTracks"]], [1, 2])
            self.assertTrue(all(row["pending"] and row["details_pending"] for row in value["topTracks"]))
            self.assertTrue(all(row["title"] and row["artist"] and row["album"]["title"] for row in value["topTracks"]))
            self.assertIsNone(value["sources"]["top_tracks"]["fetchedAt"])
            self.assertIsNone(summary.snapshot(ARTIST, "top_tracks"))

    def test_one_slow_unresolved_identity_does_not_block_nine_other_rows(self):
        gate, guard = Event(), Lock()
        active = maximum = 0
        order = [90, 8, 4, 7, 2, 10, 3, 6, 1, 5]

        def resolve(details, artist, **kwargs):
            nonlocal active, maximum
            with guard:
                active += 1
                maximum = max(maximum, active)
            try:
                if details["id"] == 90:
                    self.assertTrue(gate.wait(10))
                    return {"recording_mbid": None, "release_group_mbid": None}
                return {"recording_mbid": SATIVA, "release_group_mbid": GROUP}
            finally:
                with guard:
                    active -= 1

        with self.refresh([track(track_id) for track_id in order], resolve, gates=[gate]):
            value = self.await_summary(lambda value: len(value["topTracks"]) == 10
                                       and sum(not row["pending"] for row in value["topTracks"]) == 9)
            self.assertTrue(value["pending"])
            self.assertTrue(value["topTracks"][0]["pending"])
            self.assertTrue(all(row["release_group_mbid"] == GROUP for row in value["topTracks"][1:]))
            self.assertEqual([row["deezer_track_id"] for row in value["topTracks"]], order)
            gate.set()
            final = self.await_summary(lambda value: not value["pending"])
            self.assertEqual([row["deezer_track_id"] for row in final["topTracks"]], order)
            self.assertEqual([row["position"] for row in final["topTracks"]], list(range(1, 11)))
            self.assertFalse(any(row["pending"] for row in final["topTracks"]))
            self.assertIsNone(final["topTracks"][0]["release_group_mbid"])
            self.assertEqual(len(final["topTracks"]), len({row["deezer_track_id"] for row in final["topTracks"]}))
            self.assertEqual(maximum, 2)
            with patch.object(worker, "_claim") as claim:
                self.assertFalse(worker.request_summary(ARTIST)["pending"])
                claim.assert_not_called()

    def test_biography_can_publish_while_top_tracks_are_pending(self):
        gate = Event()

        def details(track_id):
            self.assertTrue(gate.wait(10))
            return track(track_id)

        with self.refresh([track(9)], lambda *args, **kwargs: {"recording_mbid": None, "release_group_mbid": None}, details, [gate], bio=False), \
                patch.object(summary, "artist_relations", return_value={"relations": []}), \
                patch.object(wikipedia, "bio", return_value={"text": "Ready biography"}):
            worker.process_job(ARTIST, "bio")
            value = worker.request_summary(ARTIST)
            self.assertEqual(value["bio"]["text"], "Ready biography")
            self.assertFalse(value["sources"]["bio"]["pending"])
            self.assertTrue(value["sources"]["top_tracks"]["pending"])

    def test_cached_identities_publish_even_when_both_resolution_workers_are_busy(self):
        gate = Event()
        recording = summary._save_identity("track:9", {
            "recording_mbid": SATIVA, "isrc": track(9)["isrc"], "recording_resolution_method": "exact_isrc",
        }, True)
        summary._save_identity(summary._group_identity_key(track(9), recording), {
            "recording_mbid": SATIVA, "release_group_mbid": GROUP, "release_group_resolution_method": "album_context",
        }, True)
        self.assertIsNone(summary._cached_summary_mapping({**track(9), "isrc": "USUM71710461"}))

        def resolve(*args, **kwargs):
            self.assertTrue(gate.wait(10))
            return {"recording_mbid": None, "release_group_mbid": None}

        with self.refresh([track(90), track(91), track(9)], resolve, gates=[gate]):
            value = self.await_summary(lambda value: len(value["topTracks"]) == 3 and not value["topTracks"][2]["pending"])
            self.assertTrue(all(row["pending"] for row in value["topTracks"][:2]))
            self.assertEqual(value["topTracks"][2]["release_group_mbid"], GROUP)
            self.assertEqual([row["deezer_track_id"] for row in value["topTracks"]], [90, 91, 9])

    def test_recording_identity_publishes_before_release_group_resolution(self):
        gate = Event()

        def browse(*args, **kwargs):
            self.assertTrue(gate.wait(10))
            return [release()]

        with patch.object(summary, "resolve_recording", return_value=(SATIVA, "exact_isrc")), \
                patch.object(musicbrainz, "browse_releases_by_recording", side_effect=browse), \
                self.refresh([track(9)], gates=[gate]):
            value = self.await_summary(lambda value: bool(value["topTracks"])
                                       and value["topTracks"][0].get("recording_mbid") == SATIVA)
            self.assertTrue(value["topTracks"][0]["pending"])
            self.assertIsNone(value["topTracks"][0]["release_group_mbid"])
            gate.set()
            final = self.await_summary(lambda value: not value["pending"])
            self.assertEqual(final["topTracks"][0]["release_group_mbid"], GROUP)

    def test_top_tracks_can_finish_while_biography_is_still_pending(self):
        gate = Event()

        def biography(*args):
            self.assertTrue(gate.wait(10))
            return {"text": "Later biography"}

        with patch.object(summary, "artist_relations", return_value={"relations": []}), \
                patch.object(wikipedia, "bio", side_effect=biography), \
                self.refresh([track(9)], lambda *args, **kwargs: {"recording_mbid": SATIVA, "release_group_mbid": GROUP}, bio=False):
            thread = Thread(target=worker.process_job, args=(ARTIST, "bio"))
            thread.start()
            try:
                value = self.await_summary(lambda value: not value["sources"]["top_tracks"]["pending"])
                self.assertTrue(value["pending"])
                self.assertTrue(value["sources"]["bio"]["pending"])
                self.assertIsNone(value["bio"])
                self.assertEqual(value["topTracks"][0]["release_group_mbid"], GROUP)
                gate.set()
                final = self.await_summary(lambda value: not value["pending"])
                self.assertEqual(final["bio"]["text"], "Later biography")
            finally:
                gate.set()
                thread.join(15)
                self.assertFalse(thread.is_alive())

    def test_failed_progressive_refresh_retains_completed_success_and_backoff(self):
        old = self.save("top_tracks", fetched_at=time.time() - summary.TOP_TRACKS_TTL - 1,
                        entries=[{"title": "Retained", "deezer_track_id": 1}])
        with patch.object(summary._TopTracksProgress, "finish", side_effect=requests.Timeout()), \
                self.refresh([track(9)], lambda *args, **kwargs: {"recording_mbid": None, "release_group_mbid": None}):
            value = self.await_summary(lambda value: not value["pending"]
                                       and worker._state(ARTIST, "top_tracks").get("status") == "failed")
            self.assertFalse(value["pending"])
            self.assertEqual(value["topTracks"], old["entries"])
            self.assertEqual(summary.snapshot(ARTIST, "top_tracks"), old)
            self.assertEqual(worker.jobs.qsize(), 1)

    def test_other_refresh_generation_cannot_expose_or_finish_old_progress(self):
        self.save("bio", bio={"text": "Biography"})
        with patch.object(worker, "_started", True), patch.object(worker, "jobs", Queue(maxsize=32)):
            worker.request_summary(ARTIST)
            progress = summary._TopTracksProgress(ARTIST, {}, [{"title": "Old progress", "pending": True}], time.perf_counter())
            api_cache.set_cache_document(summary.STATE_NAMESPACE, summary.refresh_state_key(ARTIST, "top_tracks"), {
                "status": "pending", "pending_until": time.time() + worker.LEASE_TTL, "refresh_id": "new-refresh",
            }, worker.LEASE_TTL)
            self.assertEqual(worker.request_summary(ARTIST)["topTracks"], [])
            progress.finish(fetched_at=time.time())
            self.assertIsNone(summary.snapshot(ARTIST, "top_tracks"))

    def test_partial_documents_are_never_fresh_and_only_known_public_cover_urls_are_used(self):
        self.assertFalse(summary.fresh({"fetched_at": time.time(), "pending": True}, "top_tracks"))
        cover = "https://cdn-images.dzcdn.net/images/cover/abc/56x56.jpg"
        self.assertEqual(summary._provider_cover({"album": {"cover_small": cover}}), cover)
        for source in ("https://user:secret@cdn-images.dzcdn.net/images/cover/abc", cover + "?token=secret",
                       "https://evil.test/images/cover/abc", "http://cdn-images.dzcdn.net/images/cover/abc"):
            self.assertIsNone(summary._provider_cover({"album": {"cover_small": source}}))

    def test_timing_logs_cover_first_final_and_total_publication_without_provider_secrets(self):
        with self.assertLogs(summary.logger, level="INFO") as logs, \
                self.refresh([track(9)], lambda *args, **kwargs: {"recording_mbid": None, "release_group_mbid": None}):
            self.await_summary(lambda value: not value["pending"])
        text = " ".join(logs.output)
        for stage in ("deezer_top_tracks", "deezer_track_detail", "first_top_tracks_snapshot", "final_top_tracks_snapshot", "refresh_top_tracks"):
            self.assertIn("stage=" + stage, text)
        self.assertNotIn("https://", text)
        self.assertNotIn("secret", text)

    def test_container_routes_summary_timing_to_existing_gunicorn_log(self):
        output = io.StringIO()
        destination = logging.StreamHandler(output)
        gunicorn_logger = logging.getLogger("gunicorn.error")
        root_level = logging.getLogger().level
        with patch.object(gunicorn_logger, "handlers", [destination]), \
                patch.object(gunicorn_logger, "level", logging.INFO), \
                patch.object(summary.logger, "handlers", []), \
                patch.object(summary.logger, "level", logging.NOTSET), \
                patch.object(summary.logger, "propagate", True), \
                patch("backend.worker.start_background_thread"):
            config = runpy.run_path(str(Path(__file__).parents[1] / "backend/gunicorn.conf.py"))
            config["post_worker_init"](Mock())
            summary.logger.info("Artist Summary timing stage=refresh_bio duration_ms=1.0")
            self.assertIn("stage=refresh_bio", output.getvalue())
            self.assertEqual(logging.getLogger().level, root_level)
