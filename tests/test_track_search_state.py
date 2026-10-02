"""Track-state contract A–Q, defined before the batch implementation.

Every fixture lives in the process-wide isolated test database. Search keeps
its existing release-group cards; state describes the matched recording only.
"""

from ._test_environment import TEST_ROOT  # noqa: F401
from .test_backend import DatabaseTestCase

import json
import statistics
import time
from contextlib import contextmanager
from unittest.mock import patch

from backend import api_cache, cache_memo, storage, track_search_index
from backend.routes import discovery
from backend.services import lidarr, musicbrainz, plex, recording_acquisition, recording_requests, release_requests
from backend.workers import lidarr_library, lidarr_searches, plex as plex_worker


def mbid(number):
    return f"{number:08x}-1111-4111-8111-111111111111"


RECORDING = mbid(1)
GROUP = mbid(1001)
ARTIST = mbid(2001)
TARGET = {"releaseGroupMbid": GROUP, "title": "Song Single", "artistName": "Artist", "primaryType": "Single"}
COMPACT_FIELDS = {"status", "available", "plexCopyCount", "downloadStatus", "target", "retrying"}


class TrackSearchStateTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        with api_cache.cache_db() as connection:
            connection.execute("DELETE FROM track_search_plex_tracks")
            connection.execute("DELETE FROM track_search_plex_isrcs")
        self.register()
        with storage.db() as connection:
            self.user_id = connection.execute("SELECT id FROM users").fetchone()[0]
        storage.save_service("plex", {"machineIdentifier": "server", "librarySectionIds": ["1"]})
        self.original_local_resolution = discovery._local_track_resolution
        self.local = self.enterContext(patch.object(discovery, "_local_track_resolution", side_effect=lambda plan: {"plan": plan, "results": []}))
        self.search_provider = self.enterContext(patch.object(musicbrainz, "search", side_effect=self.provider))
        self.enterContext(patch("requests.sessions.Session.request", side_effect=AssertionError("Live provider call")))
        self.enterContext(patch.object(recording_acquisition, "resolve", side_effect=AssertionError("Acquisition resolution")))
        self.enterContext(patch.object(recording_requests, "request_for_user", side_effect=AssertionError("Request creation")))
        self.enterContext(patch.object(release_requests, "request_release_group_for_user", side_effect=AssertionError("Release request")))
        for module, names in ((plex_worker, ("request_recent_scan", "request_full_scan")),
                              (lidarr_library, ("request_scan",)), (lidarr_searches, ("request_work",))):
            for name in names:
                self.enterContext(patch.object(module, name, side_effect=AssertionError("Worker side effect")))
        self.recordings = [self.recording(RECORDING, GROUP)]

    def recording(self, recording_id, group_id, title="Song", score=100):
        return {"id": recording_id, "title": title, "score": score,
                "artist-credit": [{"name": "Artist", "artist": {"id": ARTIST, "name": "Artist"}}],
                "releases": [{"id": mbid(3001), "title": "Song Single", "status": "Official",
                              "release-group": {"id": group_id, "primary-type": "Single"}}]}

    def provider(self, query, search_type, **options):
        if search_type == "track":
            return {"recordings": self.recordings}
        return {"release-groups": []}

    def search(self, query="Song", **options):
        response = self.client.get("/api/search", query_string={"type": "track", "q": query, **options})
        self.assertEqual(response.status_code, 200, response.get_json())
        return response.get_json()["results"]

    def state(self):
        result = self.search()[0]
        self.assertEqual(result["id"], GROUP)
        self.assertEqual(result["recordingMbid"], RECORDING)
        self.assertEqual(set(result["recordingState"]), COMPACT_FIELDS)
        return result["recordingState"]

    def intent(self, recording=RECORDING, group=GROUP, **metadata):
        storage.save_recording_acquisition(recording, {**TARGET, "releaseGroupMbid": group, **metadata}, "Song", self.user_id)

    def pending(self, group=GROUP, error=""):
        storage.enqueue_lidarr_search(self.user_id, group, 33, 44, "Song Single")
        if error:
            job = storage.pending_lidarr_search(group)["id"]
            storage.defer_lidarr_search(job, error)

    def snapshots(self, albums=None, downloads=None):
        api_cache.set_cache_document("lidarr-library", "albums", {"albums": albums or {}}, 600)
        cache_memo.invalidate_document(lidarr.LIBRARY_INDEX_KEY)
        api_cache.set_cache_document(lidarr.DOWNLOAD_SNAPSHOT_NAMESPACE, lidarr.DOWNLOAD_SNAPSHOT_KEY,
                                     {"albums": downloads or {}}, 600)

    def copies(self, count=1, recording=RECORDING, section="1", server="server", key=True):
        track_search_index.index_plex_library({"serverId": server, "tracks": [{
            "ratingKey": str(i), "key": f"/library/metadata/{i}" if key else "",
            "musicbrainzRecordingId": recording, "librarySectionId": section,
            "musicbrainzTrackId": mbid(4001), "isrcs": ["USRC17607839", "USRC17607840"],
        } for i in range(1, count + 1)]})

    def test_a_ready_recording(self):
        self.copies()
        state = self.state()
        self.assertEqual(state["status"], "ready")
        self.assertTrue(state["available"])
        self.assertEqual(state["plexCopyCount"], 1)

    def test_b_multiple_plex_copies_do_not_duplicate_cards_or_count_isrcs(self):
        self.copies(2)
        results = self.search()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["recordingState"]["plexCopyCount"], 2)

    def test_c_missing_unrequested_has_no_target(self):
        self.assertEqual(self.state(), {"status": "not_requested", "available": False,
                                      "plexCopyCount": 0, "downloadStatus": None, "target": None, "retrying": False})

    def test_d_requested_uses_persisted_target(self):
        self.intent()
        state = self.state()
        self.assertEqual(state["status"], "requested")
        self.assertEqual(state["target"], TARGET)

    def test_e_queued(self):
        self.intent()
        self.pending()
        state = self.state()
        self.assertEqual(state["status"], "queued")
        self.assertFalse(state["retrying"])

    def test_e_retrying_hides_raw_worker_error(self):
        self.intent()
        self.pending(error="private-api-key /private/path raw upstream response")
        state = self.state()
        self.assertEqual(state["status"], "queued")
        self.assertTrue(state["retrying"])
        self.assertNotIn("private", json.dumps(state))
        self.assertNotIn("lastError", state)

    def test_f_downloading_normalizes_progress(self):
        self.intent()
        self.pending()
        self.snapshots(downloads={GROUP: {"progress": 37, "status": "downloading", "downloadId": "secret", "path": "/private"}})
        state = self.state()
        self.assertEqual(state["status"], "downloading")
        self.assertEqual(state["downloadStatus"], {"progress": 37, "status": "downloading"})
        self.assertFalse(state["available"])

    def test_g_waiting_for_plex_beats_download_and_pending(self):
        self.intent()
        self.pending()
        self.snapshots(albums={GROUP: {"fullyAvailable": True}}, downloads={GROUP: {"progress": 90}})
        state = self.state()
        self.assertEqual(state["status"], "waiting_for_plex")
        self.assertFalse(state["available"])
        self.assertIsNone(state["downloadStatus"])

    def test_h_ready_overrides_stale_download_and_library(self):
        self.intent()
        self.pending()
        self.snapshots(albums={GROUP: {"fullyAvailable": True}}, downloads={GROUP: {"progress": 63}})
        self.copies(2)
        state = self.state()
        self.assertEqual(state["status"], "ready")
        self.assertEqual(state["plexCopyCount"], 2)
        self.assertIsNone(state["downloadStatus"])
        self.assertEqual(state["target"], TARGET)

    def test_i_duplicate_recording_is_evaluated_once(self):
        self.recordings[0]["releases"].append({"id": mbid(3002), "title": "Album", "release-group": {"id": mbid(1002), "primary-type": "Album"}})
        with patch.object(recording_requests, "recording_states", wraps=recording_requests.recording_states) as batch, \
             patch.object(recording_requests, "_lifecycle", wraps=recording_requests._lifecycle) as lifecycle:
            results = self.search()
        batch.assert_called_once_with([RECORDING])
        lifecycle.assert_called_once()
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["recordingState"], results[1]["recordingState"])

    def test_j_mixed_page_all_six_states(self):
        self.recordings = [self.recording(mbid(i), mbid(1000 + i)) for i in range(1, 7)]
        self.copies()
        for i in range(3, 7):
            self.intent(mbid(i), mbid(1000 + i))
        self.pending(mbid(1004))
        self.snapshots(albums={mbid(1006): {"fullyAvailable": True}}, downloads={mbid(1005): {"progress": 63}})
        results = self.search()
        self.assertEqual({r["recordingMbid"]: r["recordingState"]["status"] for r in results},
                         dict(zip((mbid(i) for i in range(1, 7)), ("ready", "not_requested", "requested", "queued", "downloading", "waiting_for_plex"))))

    def test_k_missing_or_invalid_recording_identity_preserves_card(self):
        for identity in (None, "not-a-recording-mbid"):
            with self.subTest(identity=identity):
                self.recordings = [self.recording(identity, GROUP)]
                result = self.search()[0]
                self.assertEqual(result["id"], GROUP)
                self.assertNotIn("recordingState", result)
                self.assertNotIn("recordingMbid", result)

    def test_k_release_group_alias_fallback_does_not_invent_recording(self):
        self.recordings = []
        with patch.object(discovery, "_track_release_group_alias_results", return_value=[{"id": GROUP, "name": "Song Single"}]):
            result = self.search()[0]
        self.assertNotIn("recordingMbid", result)
        self.assertNotIn("recordingState", result)

    def test_l_enrichment_does_not_mutate_local_rows_or_cache(self):
        self.intent()
        self.pending(error="raw private error")
        self.snapshots(downloads={GROUP: {"progress": 37}})
        with self.trace_reads() as statements:
            self.search()
        self.assertFalse(any(s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "REPLACE")) for s in statements))

    @contextmanager
    def trace_reads(self):
        statements = []
        original_db, original_cache = storage.db, api_cache.cache_db
        def traced(factory):
            @contextmanager
            def open_connection():
                with factory() as connection:
                    connection.set_trace_callback(statements.append)
                    yield connection
            return open_connection
        with patch.object(storage, "db", traced(original_db)), \
             patch.object(api_cache, "cache_db", traced(original_cache)), \
             patch.object(track_search_index, "cache_db", traced(original_cache)):
            yield statements

    def test_m_twenty_five_recordings_use_bounded_queries_and_snapshots(self):
        identities = [mbid(i) for i in range(1, 26)]
        for i in range(1, 26):
            self.intent(mbid(i), mbid(1000 + i))
        self.snapshots(downloads={mbid(1000 + i): {"progress": 63} for i in range(1, 26)})
        for count in (1, 25):
            cache_memo.invalidate_document(lidarr.LIBRARY_INDEX_KEY)
            with self.trace_reads() as statements, \
                 patch.object(lidarr, "cached_library_availability", wraps=lidarr.cached_library_availability) as library, \
                 patch.object(lidarr, "cached_download_availability", wraps=lidarr.cached_download_availability) as downloads:
                states = recording_requests.recording_states(identities[:count])
            reads = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
            self.assertEqual(len(reads), 4, reads)
            self.assertEqual(len(states), count)
            library.assert_called_once()
            downloads.assert_called_once()

    def test_m_empty_batch_uses_no_queries(self):
        with self.trace_reads() as statements:
            self.assertEqual(recording_requests.recording_states([]), {})
        self.assertEqual(statements, [])

    def test_m_large_batch_chunks_sql_without_per_recording_queries(self):
        identities = [mbid(i) for i in range(1, 502)]
        with self.trace_reads() as statements:
            states = recording_requests.recording_states(identities)
        reads = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
        self.assertEqual(len(reads), 4, reads)  # two SQL batches per database
        self.assertEqual(len(states), 501)
        self.assertTrue(all(state["status"] == "not_requested" for state in states.values()))

    def test_m_sql_plans_use_recording_and_pending_indexes(self):
        self.intent()
        self.pending()
        with self.trace_reads() as statements:
            recording_requests.recording_states([RECORDING])
        sql = next(s for s in statements if "COUNT(*) AS copies" in s)
        with api_cache.cache_db() as connection:
            plex_plan = " ".join(row[3] for row in connection.execute("EXPLAIN QUERY PLAN " + sql))
        sql = next(s for s in statements if "FROM recording_acquisitions intent" in s)
        with storage.db() as connection:
            intent_plan = " ".join(row[3] for row in connection.execute("EXPLAIN QUERY PLAN " + sql))
        self.assertIn("idx_track_search_plex_recording", plex_plan)
        self.assertIn("SEARCH intent USING INDEX", intent_plan)
        self.assertIn("SEARCH pending USING INDEX", intent_plan)
        self.assertNotIn("SCAN", intent_plan)

    def test_m_unrequested_batch_does_not_read_lidarr_snapshots(self):
        with patch.object(lidarr, "cached_library_availability", side_effect=AssertionError("Unneeded snapshot")), \
             patch.object(lidarr, "cached_download_availability", side_effect=AssertionError("Unneeded snapshot")):
            self.assertEqual(self.state()["status"], "not_requested")

    def test_n_single_get_matches_search_through_every_transition(self):
        def compare(expected):
            compact = self.state()
            response = self.client.get(f"/api/music/recording/{RECORDING}/request")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            full = response.get_json()
            self.assertEqual(compact, {field: full[field] for field in COMPACT_FIELDS})
            self.assertEqual(compact["status"], expected)
            self.assertEqual(compact["plexCopyCount"], len(full["tracks"]))
        compare("not_requested")
        self.intent()
        compare("requested")
        self.pending()
        compare("queued")
        self.snapshots(downloads={GROUP: {"progress": 37}})
        compare("downloading")
        self.snapshots(albums={GROUP: {"fullyAvailable": True}})
        compare("waiting_for_plex")
        self.copies(2)
        compare("ready")

    def test_o_ranking_and_card_fields_stay_unchanged(self):
        self.recordings = [self.recording(mbid(i), mbid(1000 + i), score=100 - i) for i in (3, 1, 2)]
        baseline = discovery._recording_release_group_results({"recordings": self.recordings}, discovery._track_search_plan("Song"))
        enriched = self.search()
        self.assertEqual([{k: v for k, v in r.items() if k != "recordingState"} for r in enriched], baseline)

    def test_p_query_formats_preserve_provider_plan_and_state(self):
        for query in ("Song", "Song Artist", "US-RC1-76-07839", "Song live"):
            with self.subTest(query=query):
                self.search_provider.reset_mock()
                result = self.search(query)[0]
                plan = discovery._track_search_plan(query)
                call = self.search_provider.call_args_list[0]
                self.assertEqual(call.args, (plan["query"], "track"))
                self.assertEqual(call.kwargs["plain_search"], plan["plainSearch"])
                self.assertEqual(result["recordingMbid"], RECORDING)

    def test_q_authentication_and_validation(self):
        anonymous = self.app.test_client()
        self.assertEqual(anonymous.get("/api/search?q=Song&type=track").status_code, 401)
        for query in ({"q": "x", "type": "track"}, {"q": "Song", "type": "invalid"}):
            self.assertEqual(self.client.get("/api/search", query_string=query).status_code, 400)
        self.search_provider.assert_not_called()

    def test_local_results_keep_recording_identity_without_provider_calls(self):
        group = {"id": GROUP, "title": "Song Single", "artist-credit": [{"name": "Artist"}], "primary-type": "Single"}
        self.local.side_effect = self.original_local_resolution
        with patch.object(track_search_index, "resolve_artist", return_value={"status": "unique", "mbid": ARTIST}), \
             patch.object(track_search_index, "exact_track_matches", return_value=[{"release_group_mbid": GROUP, "recording_mbid": RECORDING}]), \
             patch.object(track_search_index, "cached_release_groups", return_value={GROUP: group}):
            result = self.search("Song Artist")[0]
        self.assertEqual(result["recordingMbid"], RECORDING)
        self.assertEqual(result["recordingState"]["status"], "not_requested")
        self.search_provider.assert_not_called()

    def test_historical_target_keeps_missing_optional_metadata_local(self):
        self.intent(title="", artistName="", primaryType=None)
        self.assertEqual(self.state()["target"], {"releaseGroupMbid": GROUP, "title": "", "artistName": "", "primaryType": None})

    def test_local_multiple_recordings_select_deterministic_valid_identity(self):
        self.local.side_effect = self.original_local_resolution
        matches = [{"release_group_mbid": GROUP, "recording_mbid": identity} for identity in (mbid(2), "", RECORDING)]
        group = {"id": GROUP, "title": "Song Single", "artist-credit": [{"name": "Artist"}]}
        with patch.object(track_search_index, "resolve_artist", return_value={"status": "unique", "mbid": ARTIST}), \
             patch.object(track_search_index, "exact_track_matches", return_value=matches), \
             patch.object(track_search_index, "cached_release_groups", return_value={GROUP: group}):
            results = self.search("Song Artist")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["recordingMbid"], RECORDING)

    def test_plex_server_sections_playable_key_and_track_identity_are_respected(self):
        for options in ({"section": "2"}, {"server": "other"}, {"key": False}, {"recording": mbid(4001)}):
            with self.subTest(options=options):
                self.copies(**options)
                self.assertEqual(self.state()["status"], "not_requested")
        storage.save_service("plex", {"machineIdentifier": "server", "librarySectionIds": []})
        self.copies()
        self.assertEqual(self.state()["status"], "not_requested")

    def test_uuid_case_and_duplicate_batch_inputs_are_normalized(self):
        identity = mbid(10)
        self.intent(identity)
        self.copies(recording=identity)
        states = recording_requests.recording_states([identity.upper(), identity])
        self.assertEqual(list(states), [identity])
        self.assertEqual(states[identity]["status"], "ready")

    def test_controlled_twenty_five_result_formatting_benchmark(self):
        self.recordings = [self.recording(mbid(i), mbid(1000 + i)) for i in range(1, 26)]
        for i in range(1, 26):
            self.intent(mbid(i), mbid(1000 + i))
        self.snapshots(downloads={mbid(1000 + i): {"progress": 63} for i in range(1, 26)})
        response, plan = {"recordings": self.recordings}, discovery._track_search_plan("Song")
        def measure(enriched):
            timings = []
            for _ in range(30):
                started = time.perf_counter()
                results = discovery._recording_release_group_results(response, plan)
                if enriched:
                    discovery._enrich_track_recording_states(results)
                timings.append((time.perf_counter() - started) * 1000)
                self.assertEqual(len(results), 25)
            return statistics.median(timings)
        baseline, enriched = measure(False), measure(True)
        print(f"\n25-result local formatting median (30 runs): baseline={baseline:.3f}ms enriched={enriched:.3f}ms overhead={enriched - baseline:.3f}ms")
