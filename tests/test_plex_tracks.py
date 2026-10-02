"""Exact Plex track identity, cache, scan, and availability regressions."""

if __package__:
    from ._test_environment import TEST_ROOT
else:
    from _test_environment import TEST_ROOT

from tests.test_backend import DatabaseTestCase, Response

from unittest.mock import patch

import requests

from backend import api_cache, track_search_index
from backend.services import musicbrainz, plex
from backend.storage import save_service
from backend.workers import plex as plex_worker, plex_metadata


TRACK = "61e427a8-62f1-4000-b24b-35ecf1b6ce18"
TRACK2 = "11111111-1111-1111-1111-111111111111"
RECORDING = "22222222-2222-2222-2222-222222222222"
RELEASE = "33333333-3333-3333-3333-333333333333"
GROUP = "44444444-4444-4444-4444-444444444444"
ISRC = ["USIR10300005", "GBUM71029604"]
SECTION = {"id": "1", "title": "Music"}
CONFIG = {"url": "http://plex", "token": "secret-token", "machineIdentifier": "server-1"}


def track_item(rating_key="265485", track_id=TRACK):
    return {
        "type": "track", "ratingKey": rating_key, "key": f"/library/metadata/{rating_key}",
        "parentRatingKey": "265483", "grandparentRatingKey": "265482",
        "guid": "plex://track/example", "title": "21 Questions",
        "grandparentTitle": "50 Cent", "parentTitle": "Best of 50 Cent",
        "originalTitle": "50 Cent feat. Nate Dogg", "index": "2", "parentIndex": "1",
        "parentYear": "2017", "duration": "224200",
        "Guid": [{"id": f"mbid://{track_id}"}] if track_id else [],
    }


def album_item():
    return {
        "type": "album", "ratingKey": "265483", "parentRatingKey": "265482",
        "title": "Best of 50 Cent", "parentTitle": "50 Cent",
        "Guid": [{"id": f"mbid://{RELEASE}"}],
    }


def release_metadata():
    return {
        "id": RELEASE, "release-group": {"id": GROUP, "title": "Best of 50 Cent"},
        "media": [{"position": 1, "tracks": [
            {"id": track_id, "title": "A different title", "position": 99,
             "recording": {"id": RECORDING, "isrcs": ISRC}}
            for track_id in (TRACK, TRACK2)
        ]}],
    }


class PlexTrackTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        with api_cache.cache_db() as connection:
            for table in ("track_search_plex_tracks", "track_search_plex_isrcs", "track_search_release_refs"):
                connection.execute(f"DELETE FROM {table}")
        with plex_metadata.queue_lock:
            plex_metadata.queued_artist_ids.clear()
            plex_metadata.queued_release_ids.clear()
            plex_metadata.queued_track_ids.clear()
            plex_metadata.full_enrichment_requested = False
        plex_metadata.wake_requested.clear()

    def snapshot(self, tracks=None, albums=None):
        inventory = {
            "artists": [],
            "releaseGroups": [plex._normalize_release_group(CONFIG, SECTION, album_item())] if albums is None else albums,
            "tracks": [plex._normalize_track(CONFIG, SECTION, track_item())] if tracks is None else tracks,
            "snapshotVersion": plex.SNAPSHOT_VERSION, "sectionIds": ["1"], "scannedAt": 1,
        }
        plex._attach_track_releases(inventory)
        plex._save_snapshot(CONFIG, inventory, replace_guids=True)
        return inventory

    def enrich(self):
        metadata = release_metadata()
        plex.apply_release_group_mappings(CONFIG, {RELEASE: GROUP}, release_metadata={RELEASE: metadata})

    def test_normalization_distinguishes_track_identity_and_artist_credits(self):
        track = plex._normalize_track(CONFIG, SECTION, track_item())
        self.assertEqual(track["musicbrainzTrackId"], TRACK)
        self.assertEqual(track["musicbrainzRecordingId"], "")
        self.assertNotIn("musicbrainzId", track)
        self.assertEqual(track["trackArtist"], "50 Cent feat. Nate Dogg")
        self.assertEqual(track["albumArtist"], "50 Cent")
        self.assertEqual((track["durationMs"], track["trackNumber"], track["discNumber"], track["year"]), (224200, 2, 1, 2017))
        self.assertEqual(track["librarySectionId"], "1")
        self.assertEqual(track["plexGuid"], "plex://track/example")

    def test_normalization_tolerates_missing_optional_numbers(self):
        track = plex._normalize_track(CONFIG, SECTION, {"ratingKey": 1, "index": "bad"})
        self.assertEqual(track["key"], "/library/metadata/1")
        self.assertIsNone(track["trackNumber"])
        self.assertEqual(track["isrcs"], [])

    def test_exact_release_track_mapping_ignores_titles_and_positions(self):
        self.snapshot()
        self.enrich()
        track = plex.cached_library_snapshot(CONFIG)["tracks"][0]
        self.assertEqual(track["musicbrainzRecordingId"], RECORDING)
        self.assertEqual(track["musicbrainzReleaseId"], RELEASE)
        self.assertEqual(track["musicbrainzReleaseGroupId"], GROUP)
        self.assertEqual(track["mappingSource"], "release_track")
        self.assertEqual(track["mappingConfidence"], "exact")
        self.assertEqual(set(track["isrcs"]), set(ISRC))

    def test_multiple_plex_copies_and_multiple_isrcs_are_retained(self):
        tracks = [plex._normalize_track(CONFIG, SECTION, track_item(key, tid)) for key, tid in (("12345", TRACK2), ("265485", TRACK))]
        self.snapshot(tracks)
        self.enrich()
        available = plex.recording_availability(CONFIG, RECORDING)
        self.assertTrue(available["available"])
        self.assertEqual([track["ratingKey"] for track in available["tracks"]], ["12345", "265485"])
        for isrc in ISRC:
            self.assertEqual(len(track_search_index.plex_isrc_tracks("server-1", isrc.lower())), 2)

    def test_no_guid_track_is_retained_as_playable_inventory(self):
        self.snapshot([plex._normalize_track(CONFIG, SECTION, track_item(track_id=""))])
        self.enrich()
        self.assertEqual(len(plex.cached_library_snapshot(CONFIG)["tracks"]), 1)
        with api_cache.cache_db() as connection:
            row = connection.execute("SELECT * FROM track_search_plex_tracks").fetchone()
        self.assertEqual(row["rating_key"], "265485")
        self.assertEqual(row["recording_mbid"], "")

    def test_unmatched_track_is_never_fuzzily_assigned_or_fallback_searched(self):
        self.snapshot([plex._normalize_track(CONFIG, SECTION, track_item(track_id=GROUP))])
        self.enrich()
        self.assertEqual(plex.cached_library_snapshot(CONFIG)["tracks"][0]["musicbrainzRecordingId"], "")
        with patch.object(musicbrainz, "recordings_by_track_ids") as search:
            plex_metadata._resolve_tracks(CONFIG)
        search.assert_not_called()

    @patch.object(musicbrainz, "_http_get")
    @patch.object(musicbrainz, "_wait_for_request_slot")
    def test_release_fetch_is_once_per_unique_release_and_reused(self, wait, get):
        self.snapshot([plex._normalize_track(CONFIG, SECTION, track_item(key, tid)) for key, tid in (("1", TRACK), ("2", TRACK2))])
        get.return_value = Response(payload=release_metadata())
        plex_metadata._resolve_release_groups(CONFIG, [RELEASE, RELEASE])
        plex_metadata._resolve_release_groups(CONFIG, [RELEASE])
        self.assertEqual(get.call_count, 1)
        self.assertEqual(get.call_args.kwargs["params"]["inc"], musicbrainz.RELEASE_TRACK_INCLUDES)
        self.assertEqual(len(plex.recording_availability(CONFIG, RECORDING)["tracks"]), 2)

    def test_rich_cache_with_different_includes_is_reused_without_network(self):
        path = f"/release/{RELEASE}"
        inc = "isrcs+recordings+artist-credits+release-groups+media"
        api_cache.commit_json_responses([musicbrainz.metadata_cache_record(path, inc, release_metadata())])
        track_search_index.index_release(release_metadata(), musicbrainz.metadata_cache_key(path, inc))
        with patch.object(musicbrainz, "get") as get:
            metadata = musicbrainz.release_track_metadata(RELEASE)
        self.assertEqual(metadata, release_metadata())
        get.assert_not_called()

    def test_legacy_cache_is_upgraded_once_to_include_isrcs(self):
        old = release_metadata()
        for medium in old["media"]:
            for track in medium["tracks"]:
                track["recording"].pop("isrcs")
        inc = "recordings+artist-credits+release-groups"
        api_cache.commit_json_responses([musicbrainz.metadata_cache_record(f"/release/{RELEASE}", inc, old)])
        track_search_index.index_release(old, musicbrainz.metadata_cache_key(f"/release/{RELEASE}", inc))
        with patch.object(musicbrainz, "get", return_value=release_metadata()) as get:
            self.assertEqual(musicbrainz.release_track_metadata(RELEASE), release_metadata())
        get.assert_called_once_with(f"/release/{RELEASE}", musicbrainz.RELEASE_TRACK_INCLUDES, priority="background")

    def test_release_failure_preserves_inventory_and_does_not_trigger_outage_fallback(self):
        self.snapshot()
        with patch.object(musicbrainz, "release_track_metadata", side_effect=requests.ConnectionError("offline")):
            with self.assertLogs("backend.workers.plex_metadata", level="WARNING"):
                plex_metadata._resolve_release_groups(CONFIG, [RELEASE])
        self.assertEqual(len(plex.cached_library_snapshot(CONFIG)["tracks"]), 1)
        self.assertFalse(plex.recording_availability(CONFIG, RECORDING)["available"])
        self.assertEqual(plex.fallback_track_ids(CONFIG), [])

    def test_release_404_enables_exact_fallback(self):
        self.snapshot()
        error = requests.HTTPError("missing", response=Response(status_code=404))
        with patch.object(musicbrainz, "release_track_metadata", side_effect=error):
            plex_metadata._resolve_release_groups(CONFIG, [RELEASE])
        self.assertEqual(plex.fallback_track_ids(CONFIG), [TRACK])

    def test_single_tid_fallback_maps_recording_without_inventing_release(self):
        self.snapshot(albums=[])
        response = {"recording-count": 1, "recordings": [{"id": RECORDING, "isrcs": ISRC}]}
        with patch.object(musicbrainz, "search", return_value=response) as search:
            plex_metadata._resolve_tracks(CONFIG)
        search.assert_called_once_with(f"tid:{TRACK}", "recording", priority="background", limit=100)
        track = plex.recording_availability(CONFIG, RECORDING)["tracks"][0]
        self.assertEqual(track["mappingSource"], "track_search")
        self.assertEqual(track["musicbrainzReleaseId"], "")
        self.assertEqual(set(track["isrcs"]), set(ISRC))

    def test_tid_fallback_rejects_ambiguous_results(self):
        self.snapshot(albums=[])
        response = {"recording-count": 2, "recordings": [{"id": RECORDING}, {"id": GROUP}]}
        with patch.object(musicbrainz, "search", return_value=response):
            plex_metadata._resolve_tracks(CONFIG)
        self.assertEqual(plex.cached_library_snapshot(CONFIG)["tracks"][0]["musicbrainzRecordingId"], "")

    def test_tid_fallback_rejects_truncated_results(self):
        with patch.object(musicbrainz, "search", return_value={"recording-count": 101, "recordings": [{"id": RECORDING}]}):
            self.assertEqual(musicbrainz.recordings_by_track_ids([TRACK]), {})

    def test_batched_tid_search_attributes_each_track_and_reuses_recording_isrc_lookup(self):
        recording = {"id": RECORDING, "releases": [{"media": [{"track": [{"id": TRACK}, {"id": TRACK2}]}]}]}
        with (patch.object(musicbrainz, "search", return_value={"recordings": [recording]}) as search,
              patch.object(musicbrainz, "get", return_value={"id": RECORDING, "isrcs": ISRC}) as get):
            mappings = musicbrainz.recordings_by_track_ids([TRACK, TRACK2])
        self.assertEqual(set(mappings), {TRACK, TRACK2})
        self.assertEqual(get.call_count, 1)
        self.assertIn(" OR ", search.call_args.args[0])

    def test_fallback_worker_bounds_each_pass_and_defers_remaining_ids(self):
        targets = [f"{index:08x}-0000-0000-0000-000000000000" for index in range(130)]
        with (patch.object(plex, "fallback_track_ids", return_value=targets),
              patch.object(musicbrainz, "recordings_by_track_ids", return_value={}) as search):
            plex_metadata._resolve_tracks(CONFIG)
        self.assertEqual(search.call_count, 4)
        self.assertTrue(all(len(call.args[0]) == 25 for call in search.call_args_list))
        self.assertEqual(plex_metadata.queued_track_ids, set(targets[100:]))

    def test_recent_scan_upserts_only_new_tracks_and_keeps_existing_copies(self):
        self.snapshot()
        self.enrich()
        new = plex._normalize_track(CONFIG, SECTION, track_item("12345", TRACK2))
        with (patch.object(plex, "selected_music_sections", return_value=[SECTION]),
              patch.object(plex, "_scan_sections", return_value={"artists": [], "releaseGroups": [], "tracks": [new]}),
              patch.object(plex, "full_library_scan") as full,
              patch.object(track_search_index, "index_plex_library", wraps=track_search_index.index_plex_library) as index):
            result = plex.recently_added_scan(CONFIG)
        full.assert_not_called()
        self.assertEqual(result["releaseMbids"], [RELEASE])
        self.assertEqual([track["ratingKey"] for track in index.call_args.kwargs["track_inventory"]], ["12345"])
        self.enrich()
        self.assertEqual(len(plex.recording_availability(CONFIG, RECORDING)["tracks"]), 2)

    def test_recent_scan_preserves_mapping_and_skips_unchanged_writes(self):
        self.snapshot()
        self.enrich()
        with (patch.object(plex, "selected_music_sections", return_value=[SECTION]),
              patch.object(plex, "_scan_sections", return_value={"artists": [], "releaseGroups": [], "tracks": [plex._normalize_track(CONFIG, SECTION, track_item())]}),
              patch.object(plex, "_save_snapshot") as save):
            result = plex.recently_added_scan(CONFIG)
        self.assertFalse(result["changed"])
        save.assert_not_called()
        self.assertEqual(result["trackMbids"], [])

    def test_full_scan_removes_deleted_tracks_and_retains_exact_mapping(self):
        self.snapshot([plex._normalize_track(CONFIG, SECTION, track_item(key, tid)) for key, tid in (("1", TRACK), ("2", TRACK2))])
        self.enrich()
        inventory = {"artists": [], "releaseGroups": [plex._normalize_release_group(CONFIG, SECTION, album_item())], "tracks": [plex._normalize_track(CONFIG, SECTION, track_item("1"))]}
        with (patch.object(plex, "selected_music_sections", return_value=[SECTION]),
              patch.object(plex, "_scan_sections", return_value=inventory)):
            plex.full_library_scan(CONFIG)
        self.assertEqual([track["ratingKey"] for track in plex.recording_availability(CONFIG, RECORDING)["tracks"]], ["1"])

    def test_changed_track_identity_clears_previous_recording_and_isrcs(self):
        self.snapshot()
        self.enrich()
        new = plex._normalize_track(CONFIG, SECTION, track_item(track_id=GROUP))
        with (patch.object(plex, "selected_music_sections", return_value=[SECTION]),
              patch.object(plex, "_scan_sections", return_value={"artists": [], "releaseGroups": [], "tracks": [new]})):
            plex.recently_added_scan(CONFIG)
        self.assertFalse(plex.recording_availability(CONFIG, RECORDING)["available"])
        self.assertEqual(track_search_index.plex_isrc_tracks("server-1", ISRC[0]), [])

    def test_v7_index_migration_preserves_search_and_backfills_shared_cache(self):
        self.snapshot()
        self.enrich()
        api_cache.commit_json_responses([musicbrainz.metadata_cache_record(f"/release/{RELEASE}", musicbrainz.RELEASE_TRACK_INCLUDES, release_metadata())])
        artist_id = "55555555-5555-5555-5555-555555555555"
        track_search_index.index_artist({"id": artist_id, "name": "Migration Artist"})
        with api_cache.cache_db() as connection:
            connection.execute("DELETE FROM track_search_plex_tracks")
            connection.execute("DELETE FROM track_search_plex_isrcs")
            connection.execute("DELETE FROM track_search_release_refs")
            connection.execute("UPDATE track_search_meta SET value = '7' WHERE key = 'schema-version'")
        track_search_index._initialized = False
        track_search_index.initialize()
        self.assertTrue(plex.recording_availability(CONFIG, RECORDING)["available"])
        self.assertIsNotNone(track_search_index.resolve_artist("Migration Artist"))
        self.assertEqual(track_search_index.cached_musicbrainz_release(RELEASE), release_metadata())

    def test_cache_rebuild_restores_recording_and_isrc_lookups(self):
        self.snapshot()
        self.enrich()
        track_search_index.rebuild_from_cache()
        self.assertTrue(plex.recording_availability(CONFIG, RECORDING)["available"])
        self.assertEqual(len(track_search_index.plex_isrc_tracks("server-1", ISRC[0])), 1)

    def test_flush_removes_stale_availability(self):
        self.snapshot()
        self.enrich()
        api_cache.clear_cache("plex-library")
        self.assertFalse(plex.recording_availability(CONFIG, RECORDING)["available"])

    def test_server_and_selected_section_scope_availability(self):
        self.snapshot()
        self.enrich()
        self.assertFalse(plex.recording_availability({**CONFIG, "machineIdentifier": "other-server"}, RECORDING)["available"])
        self.assertFalse(plex.recording_availability({**CONFIG, "librarySectionIds": ["2"]}, RECORDING)["available"])
        self.assertFalse(plex.recording_availability({**CONFIG, "librarySectionIds": []}, RECORDING)["available"])

    def test_recording_lookup_uses_sql_index_without_snapshot_or_provider_calls(self):
        self.snapshot()
        self.enrich()
        with (patch.object(plex, "get_cache_document", side_effect=AssertionError("snapshot read")),
              patch.object(musicbrainz, "get", side_effect=AssertionError("MB request")),
              patch.object(plex.requests, "get", side_effect=AssertionError("Plex request"))):
            self.assertTrue(plex.recording_availability(CONFIG, RECORDING)["available"])
        with api_cache.cache_db() as connection:
            plan = connection.execute("EXPLAIN QUERY PLAN SELECT * FROM track_search_plex_tracks INDEXED BY idx_track_search_plex_recording WHERE server_id = ? AND recording_mbid = ?", ("server-1", RECORDING)).fetchall()
        self.assertIn("idx_track_search_plex_recording", " ".join(row[3] for row in plan))

    def test_recording_api_requires_auth_validates_uuid_and_hides_credentials(self):
        self.snapshot()
        self.enrich()
        save_service("plex", CONFIG)
        url = f"/api/music/recording/{RECORDING}/availability"
        self.assertEqual(self.client.get(url).status_code, 401)
        self.register()
        self.assertEqual(self.client.get("/api/music/recording/bad/availability").status_code, 400)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertTrue(response.get_json()["available"])
        self.assertEqual(response.get_json()["recordingMbid"], RECORDING)
        self.assertNotIn("secret-token", response.get_data(as_text=True))

    def test_unconfigured_recording_api_reports_unavailable(self):
        self.register()
        response = self.client.get(f"/api/music/recording/{RECORDING}/availability")
        self.assertEqual(response.get_json(), {"available": False, "recordingMbid": RECORDING, "tracks": []})

    def test_scan_worker_queues_track_only_changes(self):
        with (patch.object(plex_worker, "get_service", return_value=CONFIG),
              patch.object(plex, "recently_added_scan", return_value={"artistMbids": [], "releaseMbids": [], "trackMbids": [TRACK]}),
              patch.object(plex_metadata, "request_enrichment") as queue):
            plex_worker._run_scan("recent")
        queue.assert_called_once_with(artist_ids=[], release_ids=[], track_ids=[TRACK])

    def test_full_scan_paginates_plex_tracks(self):
        def get(url, **kwargs):
            params = kwargs["params"]
            if params["type"] != 10:
                return Response(payload={"MediaContainer": {"Metadata": []}})
            start = params["X-Plex-Container-Start"]
            return Response(payload={"MediaContainer": {"totalSize": 2, "Metadata": [track_item(str(start + 1))]}})
        with patch.object(plex.requests, "get", side_effect=get) as http:
            inventory = plex._scan_sections(CONFIG, [SECTION])
        self.assertEqual([track["ratingKey"] for track in inventory["tracks"]], ["1", "2"])
        self.assertEqual(http.call_count, 4)

    def test_recent_album_children_cover_tracks_when_recent_endpoint_returns_albums(self):
        def get(url, **kwargs):
            if url.endswith("/children"):
                return Response(payload={"MediaContainer": {"Metadata": [track_item(), track_item("2", TRACK2)]}})
            if url.endswith("/265482"):
                return Response(payload={"MediaContainer": {"Metadata": [{"title": "50 Cent", "ratingKey": "265482"}]}})
            params = kwargs["params"]
            return Response(payload={"MediaContainer": {"Metadata": [album_item()] if params["type"] in (9, 10) else []}})
        with patch.object(plex.requests, "get", side_effect=get):
            inventory = plex._scan_sections(CONFIG, [SECTION], recently_added=True)
        self.assertEqual(len(inventory["tracks"]), 2)
        self.assertEqual(len(inventory["artists"]), 1)

    def test_recent_track_hydrates_unknown_parent_album_once(self):
        def get(url, **kwargs):
            if url.endswith("/265483"):
                return Response(payload={"MediaContainer": {"Metadata": [album_item()]}})
            if url.endswith("/children"):
                return Response(payload={"MediaContainer": {"Metadata": [track_item(), track_item("2", TRACK2)]}})
            if url.endswith("/265482"):
                return Response(payload={"MediaContainer": {"Metadata": [{"title": "50 Cent", "ratingKey": "265482"}]}})
            return Response(payload={"MediaContainer": {"Metadata": [track_item(), track_item("2", TRACK2)] if kwargs["params"]["type"] == 10 else []}})
        with patch.object(plex.requests, "get", side_effect=get) as http:
            inventory = plex._scan_sections(CONFIG, [SECTION], recently_added=True)
        self.assertEqual(len(inventory["releaseGroups"]), 1)
        plex._attach_track_releases(inventory)
        self.assertTrue(all(track["musicbrainzReleaseId"] == RELEASE for track in inventory["tracks"]))
        self.assertEqual(sum(call.args[0].endswith("/265483") for call in http.call_args_list), 1)

    def test_recording_api_index_failure_has_safe_error(self):
        self.register()
        save_service("plex", CONFIG)
        with patch.object(plex, "recording_availability", side_effect=OSError("private-path")):
            response = self.client.get(f"/api/music/recording/{RECORDING}/availability")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private-path", response.get_data(as_text=True))

    def test_isrc_lookup_returns_all_copies_of_the_associated_recording(self):
        self.snapshot()
        self.enrich()
        snapshot = plex.cached_library_snapshot(CONFIG)
        other = {**snapshot["tracks"][0], "ratingKey": "other-copy", "isrcs": []}
        snapshot["tracks"].append(other)
        plex._save_snapshot(CONFIG, snapshot)
        self.assertEqual(len(track_search_index.plex_isrc_tracks("server-1", ISRC[0])), 2)

    def test_track_only_enrichment_preserves_unrelated_detail_cache(self):
        self.snapshot(albums=[])
        with patch.object(plex, "invalidate_detail_payloads") as invalidate:
            plex.apply_track_recording_mappings(CONFIG, {TRACK: {"id": RECORDING, "isrcs": ISRC}})
        invalidate.assert_not_called()

    def test_batched_fallback_handles_servers_omitting_nested_track_ids(self):
        response = {"recording-count": 1, "recordings": [{"id": RECORDING, "isrcs": []}]}
        with patch.object(musicbrainz, "search", return_value=response) as search:
            mappings = musicbrainz.recordings_by_track_ids([TRACK, TRACK2])
        self.assertEqual(set(mappings), {TRACK, TRACK2})
        self.assertEqual(search.call_count, 3)

    def test_zero_isrc_recording_is_available_and_isrc_values_are_deduplicated(self):
        self.snapshot(albums=[])
        plex.apply_track_recording_mappings(CONFIG, {TRACK: {"id": RECORDING, "isrcs": []}})
        self.assertEqual(plex.recording_availability(CONFIG, RECORDING)["tracks"][0]["isrcs"], [])
        self.assertEqual(track_search_index.normalize_isrcs([ISRC[0].lower(), ISRC[0], " "]), [ISRC[0]])
