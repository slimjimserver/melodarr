"""Artwork presentation uses local identities and the shared cache, never PMS renders."""

# isort: skip_file
# RoomTestCase establishes storage isolation before backend imports.
from .test_rooms import CONFIG, OTHER, RELEASE, RoomTestCase, item
from .test_backend import Response

import json
import os
import sqlite3
import tempfile
from io import BytesIO
from unittest.mock import patch

from PIL import Image

from backend import api_cache, artwork_cache, storage, track_search_index
from backend.routes import rooms as routes
from backend.services import room_artwork, rooms
from backend.workers import plex as plex_worker


class RoomArtworkTests(RoomTestCase):
    def setUp(self):
        super().setUp()
        directory = self.enterContext(
            tempfile.TemporaryDirectory(prefix="room-artwork-")
        )
        self.enterContext(
            patch.object(artwork_cache, "ARTWORK_CACHE_DIRECTORY", directory)
        )
        self.album = {
            "ratingKey": "70",
            "name": "Album",
            "thumb": "/library/metadata/70/thumb/123",
            "musicbrainzReleaseGroupId": RELEASE,
        }
        tracks = [
            {
                "ratingKey": key,
                "key": f"/library/metadata/{key}",
                "albumRatingKey": "70",
                "librarySectionId": "1",
                "musicbrainzReleaseGroupId": RELEASE,
            }
            for key in ("101", "102", "500", "501")
        ]
        payload = {"artists": [], "releaseGroups": [self.album], "tracks": tracks}
        api_cache.set_cache_document("plex-library", "server", payload, 3600)
        track_search_index.index_plex_library(payload, server_id="server")
        self.key = room_artwork.album_cache_key("server", self.album)
        self.path = os.path.join(directory, f"{self.key}.png")

    def cache_cover(self):
        Image.new("RGB", (300, 300), "red").save(self.path)

    def test_materialized_now_next_and_adopted_entries_prefer_cached_plex_album(self):
        self.cache_cover()
        self.pms.items.append(item(103, 500))
        state = self.start()
        url = f"/api/rooms/{state['code']}/plex-artwork/{self.key}"
        for track in [state["nowPlaying"], state["upNext"], *state["queue"]]:
            self.assertEqual(track["artwork"], url)
            self.assertEqual(
                track["artworkFallback"],
                f"/api/rooms/{state['code']}/artwork/{RELEASE}",
            )
        self.assertIsNone(state["queue"][0]["requester"])
        self.assertIn("recordingMbid", state["queue"][0])
        encoded = json.dumps(state)
        for secret in (
            "ratingKey",
            "albumRatingKey",
            CONFIG["token"],
            CONFIG["url"],
            self.album["thumb"],
        ):
            self.assertNotIn(secret, encoded)
        self.assertEqual(self.pms.calls, [])

    def test_pending_request_keeps_release_art_until_materialized_then_switches(self):
        self.cache_cover()
        state = self.add(self.start(), OTHER)
        entry = self.request_queue(state)[0]
        fallback = f"/api/rooms/{state['code']}/artwork/{RELEASE}"
        self.assertEqual(entry["artwork"], fallback)
        self.states[OTHER] = self.lifecycle("ready", "501")
        state = rooms.reconcile(state["code"])
        materialized = self.request_queue(state)[0]
        self.assertEqual(materialized["id"], entry["id"])
        self.assertEqual(
            materialized["artwork"],
            f"/api/rooms/{state['code']}/plex-artwork/{self.key}",
        )
        self.assertEqual(materialized["recordingMbid"], OTHER)

    def test_materialized_cover_miss_uses_release_group_without_network(self):
        state = self.start()
        self.assertEqual(
            state["nowPlaying"]["artwork"],
            f"/api/rooms/{state['code']}/artwork/{RELEASE}",
        )
        self.assertEqual(state["queue"][0]["artworkFallback"], "")

    def test_missing_local_metadata_returns_empty_artwork_without_blocking_queue(self):
        with patch.object(
            track_search_index,
            "plex_tracks_by_rating_key",
            side_effect=sqlite3.OperationalError("index unavailable"),
        ):
            state = self.start()
        self.assertEqual(state["nowPlaying"]["artwork"], "")
        self.assertEqual(state["queue"][0]["artwork"], "")
        self.assertTrue(state["queue"][0]["locked"])

    def test_cached_route_is_participant_scoped_cacheable_and_conditional(self):
        self.cache_cover()
        state = self.start()
        url = state["nowPlaying"]["artwork"] + "?size=large"
        self.assertEqual(self.app.test_client().get(url).status_code, 403)
        client, joined, _ = self.guest(state)
        response = client.get(url)
        self.addCleanup(response.close)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content_type, "image/webp")
        self.assertIn("private", response.headers["Cache-Control"])
        self.assertIn("max-age=", response.headers["Cache-Control"])
        self.assertNotIn("no-store", response.headers["Cache-Control"])
        etag = response.headers["ETag"]
        response.close()
        conditional = client.get(url, headers={"If-None-Match": etag})
        self.addCleanup(conditional.close)
        self.assertEqual(conditional.status_code, 304)
        conditional.close()
        self.assertEqual(
            joined["room"]["nowPlaying"]["artwork"], state["nowPlaying"]["artwork"]
        )

    def test_artwork_route_rejects_unrelated_cache_keys_and_never_fills_a_miss(self):
        self.cache_cover()
        state = self.start()
        url = state["nowPlaying"]["artwork"]
        unrelated = artwork_cache.plex_album_artwork_key(
            "server", "99", "/library/metadata/99/thumb/1"
        )
        self.assertEqual(
            self.client.get(
                f"/api/rooms/{state['code']}/plex-artwork/{unrelated}"
            ).status_code,
            404,
        )
        os.unlink(self.path)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertFalse(os.path.exists(self.path))

    def test_release_group_fallback_for_current_and_adopted_track_is_authorized(self):
        state = self.start()
        with patch.object(
            routes, "cached_artwork", return_value=("cover", 200)
        ) as cached:
            response = self.client.get(state["nowPlaying"]["artwork"] + "?size=large")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(cached.call_args.args[0], f"release-group-{RELEASE}")
        self.assertEqual(cached.call_args.kwargs["size"], "large")

    def test_duplicate_songs_keep_distinct_queue_identity_and_shared_cover(self):
        self.cache_cover()
        self.pms.items.extend([item(103, 500), item(104, 500)])
        state = self.start()
        first, second = state["queue"][-2:]
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(first["artwork"], second["artwork"])

    def test_cached_artwork_never_uses_another_servers_identity(self):
        self.cache_cover()
        state = self.start()
        storage.save_service("plex", {**CONFIG, "machineIdentifier": "another-server"})
        state = rooms.snapshot(state["code"])
        self.assertEqual(state["nowPlaying"]["artwork"], "")

    def test_artwork_only_availability_is_published_on_sse_without_queue_version_change(
        self,
    ):
        state = self.start()
        response = self.client.get(f"/api/rooms/{state['code']}/events", buffered=False)
        self.addCleanup(response.close)
        iterator = iter(response.response)
        first = json.loads(next(iterator).decode().split("data: ", 1)[1])
        self.cache_cover()
        with patch.object(routes.time, "sleep"):
            self.assertEqual(next(iterator), b": heartbeat\n\n")
            second = json.loads(next(iterator).decode().split("data: ", 1)[1])
            # No art or queue change produces only the ordinary heartbeat.
            self.assertEqual(next(iterator), b": heartbeat\n\n")
            self.assertEqual(next(iterator), b": heartbeat\n\n")
        updates = [first, second]
        self.assertEqual(updates[0]["version"], updates[1]["version"])
        self.assertNotEqual(
            updates[0]["nowPlaying"]["artwork"], updates[1]["nowPlaying"]["artwork"]
        )
        self.assertEqual(
            updates[1]["nowPlaying"]["artwork"],
            f"/api/rooms/{state['code']}/plex-artwork/{self.key}",
        )

    def test_library_worker_warms_album_artwork_after_scan(self):
        with (
            patch.object(
                plex_worker.plex,
                "recently_added_scan",
                return_value={"artistMbids": [], "releaseMbids": []},
            ),
            patch.object(room_artwork, "warm_artwork") as warm,
        ):
            plex_worker._run_scan("recent")
        self.assertEqual(warm.call_count, 1)
        self.assertEqual(warm.call_args.args[0], self.key)
        self.assertNotIn(CONFIG["token"], warm.call_args.args[1])
        self.assertEqual(
            warm.call_args.kwargs["headers"], {"X-Plex-Token": CONFIG["token"]}
        )

    def test_worker_fill_and_room_render_share_the_existing_cache_and_variants(self):
        data = BytesIO()
        Image.new("RGB", (300, 300), "red").save(data, format="PNG")
        provider = Response(
            headers={"Content-Type": "image/png"}, chunks=[data.getvalue()]
        )
        with patch.object(artwork_cache.requests, "get", return_value=provider) as get:
            # There is deliberately no Flask context in the worker.
            room_artwork.warm_album_artwork(CONFIG)
            room_artwork.warm_album_artwork(CONFIG)
            state = self.start()
            response = self.client.get(state["nowPlaying"]["artwork"] + "?size=large")
            self.assertEqual(response.status_code, 200)
            response.close()
        self.assertEqual(get.call_count, 1)
        self.assertTrue(os.path.exists(self.path))
        self.assertIsNotNone(artwork_cache.artwork_cache_file(self.key, "large"))

    def test_untrusted_album_thumbnail_sources_are_not_used(self):
        for thumb in (
            "https://other.invalid/art",
            "/library/metadata/70/thumb?X-Plex-Token=secret",
            "../cover",
        ):
            self.assertEqual(
                room_artwork.album_cache_key("server", {**self.album, "thumb": thumb}),
                "",
            )

    def test_guest_projection_keeps_safe_artwork_and_simplified_state(self):
        self.cache_cover()
        state = self.add(self.start(), OTHER)
        state["queue"][-1]["state"] = "waiting_for_queue"
        guest = rooms.project_snapshot(state, host=False)
        self.assertEqual(guest["queue"][-1]["state"], "requested")
        self.assertEqual(guest["queue"][0]["artwork"], state["queue"][0]["artwork"])
        self.assertEqual(guest["queue"][-1]["recordingMbid"], OTHER)
        self.assertNotIn("ratingKey", json.dumps(guest))
