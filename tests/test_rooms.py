"""Rooms contract, concurrency, PMS protocol, acquisition, and guest boundaries."""

# isort: skip_file
# Test storage isolation must precede every backend import.

from ._test_environment import TEST_ROOT  # noqa: F401
from .test_backend import DatabaseTestCase, Response

import json
import sqlite3
from copy import deepcopy
from threading import Barrier, Thread
from unittest.mock import Mock, patch

import requests

from backend import api_cache, cache_memo, room_storage, storage, track_search_index
from backend.routes import rooms as routes
from backend.services import (
    lidarr,
    plex_rooms,
    recording_acquisition,
    recording_requests,
    release_requests,
    rooms,
)
from backend.workers import rooms as worker

RECORDING = "11111111-1111-4111-8111-111111111111"
OTHER = "11111111-1111-4111-8111-111111111112"
MISSING_RECORDING = "aa9cdaf8-b8f4-42ad-92a3-d0c3d66bfcb1"
RELEASE = "22222222-2222-4222-8222-222222222222"
CONFIG = {
    "url": "http://pms.invalid",
    "token": "private-plex-token",
    "machineIdentifier": "server",
}


def item(identity, rating=None):
    return {
        "playQueueItemID": str(identity),
        "ratingKey": str(rating or identity),
        "title": f"Song {identity}",
        "grandparentTitle": "Artist",
        "type": "track",
    }


class FakePMS:
    config = CONFIG
    server_id = "server"

    def __init__(self):
        self.items = [item(i) for i in range(101, 103)]
        self.current = "101"
        self.session_key = "9117"
        self.calls = []
        self.fail = None
        self.sequence = 200
        self.manual_end = None

    def discover(self, user):
        return {
            "client_id": "client",
            "session_key": self.session_key,
            "queue_id": "82606",
            "current_item_id": self.current,
        }

    def active_session(self, user, *, client_id=None, allow_paused=False):
        return {
            "client_id": "client",
            "session_key": self.session_key,
            "rating_key": next(
                (
                    track["ratingKey"]
                    for track in self.items
                    if track["playQueueItemID"] == self.current
                ),
                self.current,
            ),
        }

    def load(self, _queue):
        return {
            "playQueueID": "82606",
            "Metadata": deepcopy(self.items),
            "playQueueTotalCount": len(self.items),
            "playQueueLastAddedItemID": self.manual_end
            if self.manual_end is not None
            else self.items[-1]["playQueueItemID"]
            if self.items
            else 0,
        }

    def add(self, queue, track):
        self.calls.append(("add", queue, track["ratingKey"]))
        if self.fail == "add":
            raise plex_rooms.QueueError("Safe Plex failure")
        self.sequence += 1
        added = item(self.sequence, track["ratingKey"])
        if self.manual_end is None:
            self.items.append(added)
        else:
            anchor = (
                self.manual_end
                if any(i["playQueueItemID"] == self.manual_end for i in self.items)
                else self.current
            )
            index = next(
                n for n, i in enumerate(self.items) if i["playQueueItemID"] == anchor
            )
            self.items.insert(index + 1, added)
            self.manual_end = added["playQueueItemID"]
        if self.fail == "after-add":
            self.fail = None
            raise plex_rooms.QueueError("Safe interrupted append")

    def remove(self, queue, identity):
        self.calls.append(("remove", queue, identity))
        if self.fail == "remove":
            raise plex_rooms.QueueError("Safe Plex failure")
        self.items = [i for i in self.items if i["playQueueItemID"] != identity]

    def move(self, queue, identity, after):
        self.calls.append(("move", queue, identity, after))
        if self.fail == "move":
            raise plex_rooms.QueueError("Safe Plex failure")
        target = next(i for i in self.items if i["playQueueItemID"] == identity)
        self.items.remove(target)
        index = next(
            n for n, i in enumerate(self.items) if i["playQueueItemID"] == after
        )
        self.items.insert(index + 1, target)
        if after == self.current and self.manual_end is not None:
            self.manual_end = identity


class RoomTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        with storage.db() as connection:
            connection.execute("DELETE FROM room_rate_limits")
        self.csrf = self.register()
        with storage.db() as connection:
            connection.execute(
                "UPDATE users SET plex_id='123',plex_username='Plex User'"
            )
            self.user = dict(connection.execute("SELECT * FROM users").fetchone())
        storage.save_service("plex", CONFIG)
        self.pms = FakePMS()
        self.adapter = self.enterContext(
            patch.object(plex_rooms, "PMSQueue", return_value=self.pms)
        )
        self.event = {
            "clientIdentifier": "client",
            "sessionKey": "9117",
            "playQueueID": "82606",
            "state": "playing",
        }
        self.feed = Mock()
        self.feed.latest.side_effect = lambda session, **kwargs: {
            **self.event,
            "playQueueItemID": self.pms.current,
            "ratingKey": self.pms.active_session(self.user)["rating_key"],
        }
        self.enterContext(patch.object(plex_rooms, "feed", return_value=self.feed))
        self.states = {
            RECORDING: self.lifecycle("ready"),
            OTHER: self.lifecycle("not_requested", "501"),
        }
        self.real_states, self.real_status = (
            recording_requests.recording_states,
            recording_requests.status,
        )
        self.real_request = recording_requests.request_for_user
        self.enterContext(
            patch.object(
                recording_requests,
                "recording_states",
                side_effect=lambda ids, **kw: {
                    i: deepcopy(self.states[i]) for i in set(ids)
                },
            )
        )
        self.enterContext(
            patch.object(
                recording_requests,
                "status",
                side_effect=lambda i: deepcopy(self.states[i]),
            )
        )
        self.acquire = self.enterContext(
            patch.object(recording_requests, "request_for_user", return_value=({}, 202))
        )
        self.enterContext(
            patch(
                "requests.sessions.Session.request",
                side_effect=AssertionError("Live provider call"),
            )
        )

    def lifecycle(self, status, rating="500"):
        return {
            "status": status,
            "available": status == "ready",
            "tracks": [{"ratingKey": rating, "librarySectionId": "1"}]
            if status == "ready"
            else [],
        }

    def request_queue(self, state):
        """Acquisition regressions assert the requested subset of the shared queue."""
        return [entry for entry in state["queue"] if entry["requester"]]

    def request_entries(self, room_id):
        return [row for row in rooms.entries(room_id) if row["recording_mbid"]]

    def post(self, path, payload=None, client=None, headers=None):
        return (client or self.client).post(
            path,
            json=payload,
            headers=headers or {"X-CSRF-Token": self.csrf, "X-Room-Request": "1"},
        )

    def start(self):
        response = self.post("/api/rooms")
        self.assertEqual(response.status_code, 201, response.get_json())
        return response.get_json()["room"]

    def choice(self, room, recording=RECORDING):
        result = {
            "recordingMbid": recording,
            "id": RELEASE,
            "matchedTrack": "Chosen song",
            "matchedTrackArtist": "Artist",
            "name": "Album",
        }
        return rooms.save_choices(rooms.room_by_code(room["code"]), [result])[0]["id"]

    def add(self, room, recording=RECORDING, client=None, headers=None):
        response = self.post(
            f"/api/rooms/{room['code']}/entries",
            {"choiceId": self.choice(room, recording)},
            client,
            headers,
        )
        self.assertEqual(response.status_code, 201, response.get_json())
        return response.get_json()["room"]

    def guest(self, room, name=""):
        client = self.app.test_client()
        response = self.post(
            f"/api/rooms/{room['code']}/join",
            {"name": name},
            client,
            {"X-Room-Request": "1"},
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        return (
            client,
            response.get_json(),
            {
                "X-Room-Request": "1",
                "X-Room-CSRF": response.get_json()["guest"]["csrfToken"],
            },
        )

    def edit(self, room, path, payload, method="PUT"):
        # The imported startup next is now a logical entry too. Requests-only
        # reorder tests include that immutable prefix in the API's full order.
        if "entryIds" in payload:
            locked = [
                entry["id"]
                for entry in rooms.snapshot(room["code"])["queue"]
                if entry["locked"] and not entry["requester"]
            ]
            payload = {
                **payload,
                "entryIds": [
                    identity
                    for identity in locked
                    if identity not in payload["entryIds"]
                ]
                + payload["entryIds"],
            }
        return self.client.open(
            f"/api/rooms/{room['code']}/{path}",
            method=method,
            json=payload,
            headers={"X-CSRF-Token": self.csrf, "X-Room-Request": "1"},
        )

    def test_no_active_playback_creates_no_room(self):
        self.pms.discover = Mock(
            side_effect=plex_rooms.QueueError(
                "No active Plexamp playback found. Start playback first."
            )
        )
        response = self.post("/api/rooms")
        self.assertEqual(response.status_code, 502)
        self.assertIn("Start playback", response.get_json()["error"])
        self.assertIsNone(rooms.host_room(self.user["id"]))

    def test_start_imports_entire_queue_and_locks_immediate_next(self):
        self.pms.items = [item(i) for i in range(101, 106)]
        original = deepcopy(self.pms.items)
        room = self.start()
        self.assertEqual(self.pms.items, original)
        self.assertEqual(self.pms.calls, [])
        self.assertEqual(room["nowPlaying"]["title"], "Song 101")
        self.assertEqual(room["upNext"]["title"], "Song 102")
        self.assertEqual(
            [entry["title"] for entry in room["queue"]],
            [f"Song {i}" for i in range(102, 106)],
        )
        self.assertEqual(
            [entry["locked"] for entry in room["queue"]], [True, False, False, False]
        )
        rows = rooms.entries(rooms.room_by_code(room["code"])["id"])
        self.assertEqual(
            [row["queue_item_id"] for row in rows], [str(i) for i in range(102, 106)]
        )
        self.assertTrue(
            all(
                row["state"] == "ready"
                and row["recording_mbid"] is None
                and row["requester"] is None
                for row in rows
            )
        )
        self.acquire.assert_not_called()

    def test_no_next_track_prevents_takeover(self):
        self.pms.items = [item(101)]
        response = self.post("/api/rooms")
        self.assertEqual(response.status_code, 409)
        self.assertIn("Up Next", response.get_json()["error"])
        self.assertIsNone(rooms.host_room(self.user["id"]))

    def test_one_room_per_host_and_idempotent_migration(self):
        room = self.start()
        storage.init_db()
        self.assertEqual(self.post("/api/rooms").status_code, 409)
        self.assertEqual(rooms.host_room(self.user["id"])["code"], room["code"])

    def test_ready_materializes_and_duplicates_stay_distinct(self):
        room = self.add(self.start())
        room = self.add(room)
        rows = self.request_entries(rooms.room_by_code(room["code"])["id"])
        self.assertEqual(len({r["id"] for r in rows}), 2)
        self.assertEqual(len({r["queue_item_id"] for r in rows}), 2)
        self.assertEqual({r["rating_key"] for r in rows}, {"500"})
        self.assertEqual(
            [r["state"] for r in self.request_queue(room)], ["ready", "ready"]
        )
        self.acquire.assert_not_called()

    def test_reorder_duplicates_uses_queue_item_ids(self):
        room = self.add(self.add(self.start()))
        rows = self.request_entries(rooms.room_by_code(room["code"])["id"])
        response = self.edit(
            room,
            "order",
            {"entryIds": [r["id"] for r in reversed(rows)], "version": room["version"]},
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertIn(
            ("move", "82606", rows[1]["queue_item_id"], "102"), self.pms.calls
        )
        self.assertEqual(
            [r["id"] for r in self.request_queue(response.get_json()["room"])],
            [r["id"] for r in reversed(rows)],
        )

    def test_remove_materialized_and_pending_does_not_cancel_acquisition(self):
        room = self.add(self.start())
        identity = self.request_queue(room)[0]["id"]
        response = self.edit(
            room, f"entries/{identity}", {"version": room["version"]}, "DELETE"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.request_queue(response.get_json()["room"]), [])
        self.assertEqual(len(self.pms.items), 2)
        room = self.add(response.get_json()["room"], OTHER)
        response = self.edit(
            room,
            f"entries/{self.request_queue(room)[0]['id']}",
            {"version": room["version"]},
            "DELETE",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.pms.items), 2)

    def test_missing_placeholder_and_existing_acquisition_bridge(self):
        room = self.add(self.start(), OTHER)
        self.assertEqual(self.request_queue(room)[0]["state"], "requested")
        self.acquire.assert_not_called()
        self.states[OTHER] = self.lifecycle("queued", "501")

        def acquire(identity, host):
            self.assertEqual(identity, OTHER)
            self.assertEqual(host["id"], self.user["id"])
            self.states[OTHER] = self.lifecycle("queued", "501")
            return {}, 202

        self.states[OTHER] = self.lifecycle("not_requested", "501")
        self.acquire.side_effect = acquire
        worker.tick()
        self.acquire.assert_called_once()
        self.assertEqual(
            self.request_queue(rooms.snapshot(room["code"]))[0]["state"], "queued"
        )
        self.states[OTHER] = self.lifecycle("downloading", "501")
        worker.tick()
        self.assertEqual(
            self.request_queue(rooms.snapshot(room["code"]))[0]["state"], "downloading"
        )
        self.states[OTHER] = self.lifecycle("waiting_for_plex", "501")
        worker.tick()
        self.states[OTHER] = self.lifecycle("ready", "501")
        worker.tick()
        self.assertEqual(
            self.request_queue(rooms.snapshot(room["code"]))[0]["state"], "ready"
        )
        self.assertEqual(self.pms.items[-1]["ratingKey"], "501")

    def test_pending_position_materializes_before_later_ready_track(self):
        room = self.add(self.start(), OTHER)
        room = self.add(room)
        self.states[OTHER] = self.lifecycle("ready", "501")
        worker.tick()
        self.assertEqual(
            [i["ratingKey"] for i in self.pms.items], ["101", "102", "501", "500"]
        )

    def pending_indexed_recording_room(self, copies=1):
        """Use the real availability API/lifecycle, mocking only PMS transport."""
        self.enterContext(
            patch.object(
                recording_requests, "recording_states", side_effect=self.real_states
            )
        )
        self.enterContext(
            patch.object(recording_requests, "status", side_effect=self.real_status)
        )
        with api_cache.cache_db() as connection:
            connection.execute("DELETE FROM track_search_plex_tracks")
            connection.execute("DELETE FROM track_search_plex_isrcs")
        storage.save_recording_acquisition(
            MISSING_RECORDING,
            {
                "releaseGroupMbid": RELEASE,
                "title": "Single",
                "artistName": "Lana Del Rey",
                "primaryType": "Single",
            },
            "Young & Beautiful",
            self.user["id"],
        )
        api_cache.set_cache_document(
            "lidarr-library",
            "albums",
            {"albums": {RELEASE: {"fullyAvailable": True}}},
            600,
        )
        cache_memo.invalidate_document(lidarr.LIBRARY_INDEX_KEY)
        track_search_index.index_plex_library(
            {
                "serverId": "server",
                "tracks": [
                    {
                        "ratingKey": "500",
                        "key": "/library/metadata/500",
                        "librarySectionId": "1",
                        "musicbrainzRecordingId": RECORDING,
                        "title": "the one",
                    }
                ],
            }
        )
        room = self.start()
        for _ in range(copies):
            room = self.add(room, MISSING_RECORDING)
        room = self.add(room)
        self.assertEqual(
            [entry["state"] for entry in self.request_queue(room)],
            ["waiting_for_plex"] * copies + ["ready"],
        )
        availability = self.client.get(
            f"/api/music/recording/{MISSING_RECORDING}/availability"
        ).get_json()
        self.assertFalse(availability["available"])
        self.assertEqual(availability["tracks"], [])
        rows = self.request_entries(rooms.room_by_code(room["code"])["id"])
        for row in rows[:copies]:
            self.assertEqual(row["recording_mbid"], MISSING_RECORDING)
            self.assertIsNone(row["queue_item_id"])
        return room

    def index_pending_recording(self):
        track = {
            "ratingKey": "270814",
            "key": "/library/metadata/270814",
            "librarySectionId": "1",
            "musicbrainzRecordingId": MISSING_RECORDING,
            "title": "Young & Beautiful",
            "trackArtist": "Lana Del Rey",
        }
        track_search_index.index_plex_library(
            {"serverId": "server", "tracks": [track]}, track_inventory=[track]
        )
        lifecycle = self.client.get(
            f"/api/music/recording/{MISSING_RECORDING}/request"
        ).get_json()
        self.assertTrue(lifecycle["available"])
        self.assertEqual(lifecycle["status"], "ready")
        self.assertEqual(lifecycle["tracks"][0]["ratingKey"], "270814")

    def test_manual_retry_pending_recording_materializes_in_order_and_emits_sse(self):
        room = self.pending_indexed_recording_room()
        client, _, _ = self.guest(room)
        response = client.get(f"/api/rooms/{room['code']}/events", buffered=False)
        self.addCleanup(response.close)
        iterator = iter(response.response)
        initial = json.loads(next(iterator).decode().split("data: ", 1)[1])
        self.assertEqual(self.request_queue(initial)[0]["state"], "waiting_for_plex")
        self.index_pending_recording()
        retry = self.post(f"/api/rooms/{room['code']}/sync")
        self.assertEqual(retry.status_code, 200, retry.get_json())
        state = retry.get_json()["room"]
        self.assertEqual(
            [entry["state"] for entry in self.request_queue(state)], ["ready"] * 2
        )
        self.assertEqual(
            [entry["id"] for entry in self.request_queue(state)],
            [entry["id"] for entry in self.request_queue(initial)],
        )
        rows = self.request_entries(rooms.room_by_code(room["code"])["id"])
        self.assertEqual(rows[0]["rating_key"], "270814")
        self.assertIsNotNone(rows[0]["queue_item_id"])
        self.assertIsNone(rows[0]["add_before"])
        self.assertEqual(
            [track["playQueueItemID"] for track in self.pms.items[2:]],
            [row["queue_item_id"] for row in rows],
        )
        self.assertEqual(
            [track["ratingKey"] for track in self.pms.items],
            ["101", "102", "270814", "500"],
        )
        with patch.object(routes.time, "sleep"):
            self.assertEqual(next(iterator), b": heartbeat\n\n")
            update = json.loads(next(iterator).decode().split("data: ", 1)[1])
        self.assertGreater(update["version"], initial["version"])
        self.assertEqual(self.request_queue(update), self.request_queue(state))
        calls = deepcopy(self.pms.calls)
        retry = self.post(f"/api/rooms/{room['code']}/sync")
        self.assertEqual(retry.status_code, 200, retry.get_json())
        self.assertEqual(self.pms.calls, calls)

    def test_worker_reconsiders_pending_recording_with_same_reconciliation_helper(self):
        room = self.pending_indexed_recording_room()
        worker.tick()
        self.assertEqual(
            self.request_queue(rooms.snapshot(room["code"]))[0]["state"],
            "waiting_for_plex",
        )
        self.index_pending_recording()
        with patch.object(rooms, "reconcile", wraps=rooms.reconcile) as reconcile:
            worker.tick()
        reconcile.assert_called_once_with(room["code"], initiate=True)
        self.assertEqual(
            self.request_queue(rooms.snapshot(room["code"]))[0]["state"], "ready"
        )
        self.assertEqual(
            [track["ratingKey"] for track in self.pms.items],
            ["101", "102", "270814", "500"],
        )
        calls = deepcopy(self.pms.calls)
        worker.tick()
        self.assertEqual(self.pms.calls, calls)

    def test_manual_retry_duplicate_pending_recordings_materialize_independently(self):
        room = self.pending_indexed_recording_room(copies=2)
        self.index_pending_recording()
        retry = self.post(f"/api/rooms/{room['code']}/sync")
        self.assertEqual(retry.status_code, 200, retry.get_json())
        rows = self.request_entries(rooms.room_by_code(room["code"])["id"])
        self.assertEqual(len({row["id"] for row in rows}), 3)
        self.assertEqual(len({row["queue_item_id"] for row in rows}), 3)
        self.assertEqual([row["state"] for row in rows], ["ready"] * 3)
        self.assertEqual(
            [track["playQueueItemID"] for track in self.pms.items[2:]],
            [row["queue_item_id"] for row in rows],
        )
        self.assertEqual(
            [track["ratingKey"] for track in self.pms.items],
            ["101", "102", "270814", "270814", "500"],
        )
        calls = deepcopy(self.pms.calls)
        retry = self.post(f"/api/rooms/{room['code']}/sync")
        self.assertEqual(retry.status_code, 200, retry.get_json())
        self.assertEqual(self.pms.calls, calls)

    def test_manual_retry_pending_recording_add_failure_retains_recoverable_intent(
        self,
    ):
        room = self.pending_indexed_recording_room()
        self.index_pending_recording()
        self.pms.fail = "add"
        retry = self.post(f"/api/rooms/{room['code']}/sync")
        self.assertEqual(retry.status_code, 502)
        row = self.request_entries(rooms.room_by_code(room["code"])["id"])[0]
        self.assertEqual(row["state"], "waiting_for_queue")
        self.assertIsNone(row["queue_item_id"])
        self.assertTrue(row["add_before"])
        self.assertTrue(rooms.snapshot(room["code"])["syncError"])
        self.pms.fail = None
        retry = self.post(f"/api/rooms/{room['code']}/sync")
        self.assertEqual(retry.status_code, 200, retry.get_json())
        self.assertEqual(
            self.request_queue(retry.get_json()["room"])[0]["state"], "ready"
        )
        self.assertEqual(
            [track["ratingKey"] for track in self.pms.items],
            ["101", "102", "270814", "500"],
        )

    def test_manual_retry_pending_recording_interrupted_add_recovers_exact_instance(
        self,
    ):
        room = self.pending_indexed_recording_room()
        self.index_pending_recording()
        self.pms.fail = "after-add"
        retry = self.post(f"/api/rooms/{room['code']}/sync")
        self.assertEqual(retry.status_code, 502)
        row = self.request_entries(rooms.room_by_code(room["code"])["id"])[0]
        self.assertEqual(row["state"], "waiting_for_queue")
        self.assertIsNone(row["queue_item_id"])
        retry = self.post(f"/api/rooms/{room['code']}/sync")
        self.assertEqual(retry.status_code, 200, retry.get_json())
        rows = self.request_entries(rooms.room_by_code(room["code"])["id"])
        self.assertEqual(rows[0]["state"], "ready")
        self.assertEqual(
            [track["playQueueItemID"] for track in self.pms.items[2:]],
            [row["queue_item_id"] for row in rows],
        )
        self.assertEqual(
            len(
                [call for call in self.pms.calls if call == ("add", "82606", "270814")]
            ),
            1,
        )

    def test_guest_security_payload_and_cookie(self):
        room = self.start()
        client, payload, headers = self.guest(room)
        self.assertEqual(payload["guest"]["name"], "Guest #1")
        self.assertEqual(self.guest(room)[1]["guest"]["name"], "Guest #2")
        response = client.get(f"/api/rooms/{room['code']}")
        body = response.get_data(as_text=True)
        for secret in (
            "private-plex-token",
            "client_id",
            "session_key",
            "queue_id",
            "queue_item_id",
            "playQueue",
            "ratingKey",
            "9117",
            "82606",
            "/private/",
        ):
            self.assertNotIn(secret, body)
        cookie = client.get_cookie("room_guest", path=f"/api/rooms/{room['code']}")
        self.assertTrue(cookie.http_only)
        self.assertEqual(cookie.same_site, "Lax")
        self.add(room, client=client, headers=headers)

    def test_guest_csrf_and_cross_origin_rejected(self):
        room = self.start()
        client, _, headers = self.guest(room)
        path = f"/api/rooms/{room['code']}/entries"
        choice = self.choice(room)
        self.assertEqual(
            self.post(
                path, {"choiceId": choice}, client, {"X-Room-Request": "1"}
            ).status_code,
            403,
        )
        self.assertEqual(
            self.post(
                path,
                {"choiceId": choice},
                client,
                {**headers, "Origin": "https://evil.invalid"},
            ).status_code,
            403,
        )
        self.assertEqual(
            self.post(
                f"/api/rooms/{room['code']}/join",
                {"name": ""},
                client,
                {"Content-Type": "application/json"},
            ).status_code,
            403,
        )

    def test_mass_assignment_arbitrary_urls_and_queue_ids_rejected(self):
        room = self.start()
        client, _, headers = self.guest(room)
        for field in (
            "queueId",
            "playQueueItemID",
            "url",
            "ip",
            "requester",
            "guestId",
            "ratingKey",
        ):
            response = self.post(
                f"/api/rooms/{room['code']}/entries",
                {"choiceId": self.choice(room), field: "internal"},
                client,
                headers,
            )
            self.assertEqual(response.status_code, 400)
        self.assertEqual(
            self.post(
                f"/api/rooms/{room['code']}/entries",
                {"choiceId": "arbitrary"},
                client,
                headers,
            ).status_code,
            400,
        )

    def test_proxy_tls_origin_and_configured_public_host(self):
        room = self.start()
        client = self.app.test_client()
        path = f"/api/rooms/{room['code']}/join"
        headers = {"X-Room-Request": "1", "Origin": "https://localhost"}
        self.assertEqual(self.post(path, {}, client, headers).status_code, 200)
        headers["Origin"] = "https://public.example"
        self.assertEqual(self.post(path, {}, client, headers).status_code, 403)
        storage.save_service("melodarr", {"applicationUrl": "https://public.example"})
        self.assertEqual(self.post(path, {}, client, headers).status_code, 200)
        self.assertEqual(
            self.post(
                path, {}, client, {**headers, "Sec-Fetch-Site": "cross-site"}
            ).status_code,
            403,
        )
        self.assertEqual(
            self.post(path, {}, client, {**headers, "Origin": "http:evil"}).status_code,
            403,
        )

    def test_guest_cannot_cross_rooms_or_spoof_choice(self):
        room = self.start()
        client, _, headers = self.guest(room)
        original = rooms.room_by_code(room["code"])
        with storage.db() as connection:
            connection.execute(
                "INSERT INTO users(username,password_hash,role,created_at) VALUES ('other','unused','user',0)"
            )
            user_id = connection.execute(
                "SELECT id FROM users WHERE username='other'"
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO rooms(id,code,host_user_id,status,created_at,server_id,client_id,session_key,queue_id,current_item_id,handoff_item_id) "
                "VALUES ('other-room','ABCDEFGHJK',?,'active',0,'server','other','other','other','1','2')",
                (user_id,),
            )
        token = client.get_cookie("room_guest", path=f"/api/rooms/{room['code']}").value
        client.set_cookie("room_guest", token, path="/api/rooms/ABCDEFGHJK")
        self.assertEqual(client.get("/api/rooms/ABCDEFGHJK").status_code, 403)
        self.assertEqual(
            self.post(
                "/api/rooms/ABCDEFGHJK/entries",
                {"choiceId": self.choice(room)},
                client,
                headers,
            ).status_code,
            403,
        )
        other_choice = rooms.save_choices(
            rooms.room_by_code("ABCDEFGHJK"),
            [{"recordingMbid": RECORDING, "id": RELEASE}],
        )[0]["id"]
        self.assertEqual(
            self.post(
                f"/api/rooms/{original['code']}/entries",
                {"choiceId": other_choice},
                client,
                headers,
            ).status_code,
            400,
        )

    def test_only_host_ends_and_closed_room_rejects_mutations(self):
        room = self.start()
        client, _, headers = self.guest(room)
        self.assertNotEqual(
            self.post(
                f"/api/rooms/{room['code']}/end", client=client, headers=headers
            ).status_code,
            200,
        )
        before = deepcopy(self.pms.items)
        self.assertEqual(self.post(f"/api/rooms/{room['code']}/end").status_code, 200)
        self.assertEqual(self.pms.items, before)
        self.assertEqual(
            self.post(
                f"/api/rooms/{room['code']}/entries",
                {"choiceId": self.choice(room)},
                client,
                headers,
            ).status_code,
            410,
        )
        self.assertEqual(
            self.post(
                f"/api/rooms/{room['code']}/join", {}, client, {"X-Room-Request": "1"}
            ).status_code,
            410,
        )
        self.assertIsNone(rooms.host_room(self.user["id"]))

    def test_simultaneous_requests_keep_unique_order(self):
        room = self.start()
        guests = [self.guest(room), self.guest(room)]
        choice = self.choice(room)
        barrier, responses, errors = Barrier(2), [], []

        def request(client, _, headers):
            try:
                barrier.wait(timeout=5)
                responses.append(
                    self.post(
                        f"/api/rooms/{room['code']}/entries",
                        {"choiceId": choice},
                        client,
                        headers,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - collect thread failures in the test thread.
                errors.append(exc)

        threads = [Thread(target=request, args=guest) for guest in guests]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual([r.status_code for r in responses], [201, 201])
        rows = self.request_entries(rooms.room_by_code(room["code"])["id"])
        self.assertEqual([r["position"] for r in rows], [1, 2])
        self.assertEqual(
            [i["playQueueItemID"] for i in self.pms.items[2:]],
            [r["queue_item_id"] for r in rows],
        )

    def test_failed_append_keeps_intent_and_retry_recovers_without_duplicate(self):
        room = self.start()
        self.pms.fail = "after-add"
        response = self.post(
            f"/api/rooms/{room['code']}/entries", {"choiceId": self.choice(room)}
        )
        self.assertEqual(response.status_code, 502)
        state = rooms.snapshot(room["code"])
        self.assertTrue(state["syncError"])
        self.assertEqual(len(self.request_queue(state)), 1)
        self.assertEqual(self.request_queue(state)[0]["state"], "waiting_for_queue")
        worker.tick()
        self.assertIsNone(rooms.snapshot(room["code"])["syncError"])
        self.assertEqual(len([c for c in self.pms.calls if c[0] == "add"]), 1)
        self.assertEqual(len(self.pms.items), 3)

    def test_failed_remove_and_reorder_keep_intent_for_reconciliation(self):
        room = self.add(self.add(self.start()))
        self.pms.fail = "move"
        order = [r["id"] for r in reversed(self.request_queue(room))]
        response = self.edit(
            room, "order", {"entryIds": order, "version": room["version"]}
        )
        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            json.loads(rooms.room_by_code(room["code"])["write_intent"])["order"],
            [
                row["queue_item_id"]
                for row in reversed(
                    self.request_entries(rooms.room_by_code(room["code"])["id"])
                )
            ],
        )
        self.pms.fail = None
        room = rooms.reconcile(room["code"])
        self.pms.fail = "remove"
        self.assertEqual(
            self.edit(
                room, f"entries/{order[0]}", {"version": room["version"]}, "DELETE"
            ).status_code,
            502,
        )
        self.pms.fail = None
        worker.tick()
        self.assertEqual(len(self.pms.items), 3)

    def test_playback_transition_and_current_track_protection(self):
        room = self.add(self.add(self.start()))
        self.pms.current = self.pms.items[2]["playQueueItemID"]
        worker.tick()
        state = rooms.snapshot(room["code"])
        self.assertEqual(len(self.request_queue(state)), 1)
        self.assertEqual(state["handoff"], {})
        self.assertEqual(
            self.edit(
                state,
                f"entries/{self.request_queue(room)[0]['id']}",
                {"version": state["version"]},
                "DELETE",
            ).status_code,
            409,
        )

    def test_rotated_stream_session_clears_handoff_and_materializes_after_current(self):
        room = self.add(self.start(), OTHER)
        room = self.add(room)
        ready_id = self.request_entries(rooms.room_by_code(room["code"])["id"])[1][
            "queue_item_id"
        ]
        original_event = {
            **self.event,
            "playQueueItemID": "101",
            "ratingKey": "101",
            "state": "stopped",
        }
        self.pms.current = ready_id
        self.pms.session_key = "9119"
        current_event = {
            **self.event,
            "sessionKey": "9119",
            "playQueueItemID": ready_id,
            "ratingKey": "500",
        }
        self.feed.latest.side_effect = lambda session, **kwargs: (
            current_event if session["session_key"] == "9119" else original_event
        )
        self.states[OTHER] = self.lifecycle("ready", "501")
        response = self.post(f"/api/rooms/{room['code']}/sync")
        self.assertEqual(response.status_code, 200, response.get_json())
        state = response.get_json()["room"]
        self.assertEqual(state["handoff"], {})
        self.assertEqual(state["nowPlaying"]["title"], f"Song {ready_id}")
        self.assertEqual(len(self.request_queue(state)), 1)
        rows = self.request_entries(rooms.room_by_code(room["code"])["id"])
        self.assertEqual(rows[1]["playback"], "playing")
        self.assertEqual(rows[0]["state"], "ready")
        self.assertEqual(
            self.pms.items[-1]["playQueueItemID"], rows[0]["queue_item_id"]
        )
        self.assertNotIn(
            ("move", "82606", rows[0]["queue_item_id"], "102"), self.pms.calls
        )
        stored = rooms.room_by_code(room["code"])
        self.assertEqual(stored["session_key"], "9119")
        self.assertEqual(stored["handoff_item_id"], "")
        calls = deepcopy(self.pms.calls)
        worker.tick()
        self.assertEqual(self.pms.calls, calls)

    def test_consumed_handoff_cannot_reappear_when_queue_history_changes(self):
        room = self.add(self.start())
        self.pms.current = "102"
        worker.tick()
        self.assertEqual(rooms.snapshot(room["code"])["handoff"], {})
        ready_id = self.request_entries(rooms.room_by_code(room["code"])["id"])[0][
            "queue_item_id"
        ]
        self.pms.current = ready_id
        # A later PMS snapshot can retain/reposition historical items. The
        # consumed startup buffer must never become an upcoming anchor again.
        handoff = next(
            track for track in self.pms.items if track["playQueueItemID"] == "102"
        )
        self.pms.items.remove(handoff)
        self.pms.items.append(handoff)
        worker.tick()
        self.assertEqual(rooms.snapshot(room["code"])["handoff"], {})
        room = self.add(rooms.snapshot(room["code"]))
        rows = self.request_entries(rooms.room_by_code(room["code"])["id"])
        self.assertEqual(
            [track["playQueueItemID"] for track in self.pms.items],
            ["101", ready_id, "102", rows[1]["queue_item_id"]],
        )
        self.assertEqual(room["handoff"], {})

    def protected_queue(self):
        room = self.start()
        for _ in range(4):
            room = self.add(room)
        self.pms.current = "102"
        room = rooms.reconcile(room["code"])
        self.pms.calls.clear()
        return room, self.request_entries(rooms.room_by_code(room["code"])["id"])

    def test_live_current_next_and_three_future_entries(self):
        room, rows = self.protected_queue()
        self.assertEqual(room["nowPlaying"]["title"], "Song 102")
        self.assertEqual(room["upNext"]["title"], f"Song {rows[0]['queue_item_id']}")
        self.assertEqual(
            [row["locked"] for row in self.request_queue(room)],
            [True, False, False, False],
        )
        self.assertEqual(room["handoff"], {})

    def test_locked_up_next_cannot_move_down_via_api(self):
        room, rows = self.protected_queue()
        order = [rows[1]["id"], rows[0]["id"], rows[2]["id"], rows[3]["id"]]
        response = self.edit(
            room, "order", {"entryIds": order, "version": room["version"]}
        )
        self.assertEqual(response.status_code, 409)
        self.assertIn("Up Next is locked", response.get_json()["error"])
        self.assertEqual(self.pms.calls, [])
        self.assertEqual(
            [r["id"] for r in self.request_queue(rooms.snapshot(room["code"]))],
            [r["id"] for r in rows],
        )

    def test_locked_up_next_cannot_be_removed_via_api(self):
        room, rows = self.protected_queue()
        response = self.edit(
            room, f"entries/{rows[0]['id']}", {"version": room["version"]}, "DELETE"
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.pms.calls, [])
        self.assertFalse(
            self.request_entries(rooms.room_by_code(room["code"])["id"])[0]["removed"]
        )

    def test_service_cannot_move_future_entry_ahead_of_next(self):
        room, rows = self.protected_queue()
        with self.assertRaises(rooms.RoomError) as raised:
            rooms.edit(
                room["code"],
                self.user["id"],
                order=[rows[3]["id"], *[r["id"] for r in rows[:3]]],
                version=room["version"],
            )
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(self.pms.calls, [])

    def test_future_entry_can_move_directly_after_locked_next(self):
        room, rows = self.protected_queue()
        order = [rows[0]["id"], rows[3]["id"], rows[1]["id"], rows[2]["id"]]
        state = rooms.edit(
            room["code"], self.user["id"], order=order, version=room["version"]
        )
        self.assertEqual([r["id"] for r in self.request_queue(state)], order)
        self.assertEqual(
            [i["playQueueItemID"] for i in self.pms.items][2:],
            [rows[n]["queue_item_id"] for n in (0, 3, 1, 2)],
        )
        self.assertEqual(
            self.pms.calls,
            [("move", "82606", rows[3]["queue_item_id"], rows[0]["queue_item_id"])],
        )

    def test_transition_automatically_locks_new_next_on_edit(self):
        room, rows = self.protected_queue()
        self.pms.current = rows[0]["queue_item_id"]
        # No worker tick: the mutation must discover the new boundary itself.
        response = self.edit(
            room, f"entries/{rows[1]['id']}", {"version": room["version"]}, "DELETE"
        )
        self.assertEqual(response.status_code, 409)
        state = rooms.snapshot(room["code"])
        self.assertEqual(self.request_queue(state)[0]["id"], rows[1]["id"])
        self.assertTrue(self.request_queue(state)[0]["locked"])
        self.assertEqual(self.pms.calls, [])

    def test_consumed_handoff_repositioned_in_future_is_adopted_without_special_protection(
        self,
    ):
        room, rows = self.protected_queue()
        self.pms.current = rows[0]["queue_item_id"]
        handoff = next(i for i in self.pms.items if i["playQueueItemID"] == "102")
        self.pms.items.remove(handoff)
        self.pms.items.append(handoff)
        state = rooms.reconcile(room["code"])
        self.assertEqual(state["handoff"], {})
        self.assertEqual(state["queue"][-1]["title"], "Song 102")
        self.assertFalse(state["queue"][-1]["locked"])
        self.assertEqual(self.pms.calls, [])

    def test_external_tail_and_autoplay_style_batch_are_adopted_in_order(self):
        room, _rows = self.protected_queue()
        self.pms.items.extend([item(900), item(901), item(902)])
        state = rooms.reconcile(room["code"])
        self.assertEqual(
            [entry["title"] for entry in state["queue"]][-3:],
            ["Song 900", "Song 901", "Song 902"],
        )
        self.assertEqual(self.pms.calls, [])
        rows = rooms.entries(rooms.room_by_code(room["code"])["id"])
        self.assertEqual(
            [row["queue_item_id"] for row in rows][-3:], ["900", "901", "902"]
        )

    def test_interspersed_external_duplicate_is_adopted_by_instance(self):
        room, rows = self.protected_queue()
        self.pms.items.insert(4, item(900, "500"))
        state = rooms.reconcile(room["code"])
        self.assertEqual(self.pms.calls, [])
        expected = [
            rows[0]["queue_item_id"],
            rows[1]["queue_item_id"],
            "900",
            rows[2]["queue_item_id"],
            rows[3]["queue_item_id"],
        ]
        active = rooms._active_entries(rooms.room_by_code(room["code"]))
        self.assertEqual([row["queue_item_id"] for row in active], expected)
        self.assertEqual(len({row["id"] for row in active}), 5)
        self.assertEqual([entry["title"] for entry in state["queue"]][2], "Song 900")

    def test_external_new_next_and_interspersed_song_are_adopted(self):
        room, _rows = self.protected_queue()
        self.pms.items.insert(2, item(900))
        self.pms.items.insert(4, item(901))
        original = deepcopy(self.pms.items)
        state = rooms.reconcile(room["code"])
        self.assertEqual(state["upNext"]["title"], "Song 900")
        self.assertEqual(state["queue"][0]["title"], "Song 900")
        self.assertTrue(state["queue"][0]["locked"])
        self.assertEqual(self.pms.items, original)
        self.assertEqual(self.pms.calls, [])

    def test_correct_protected_queue_is_idempotent(self):
        room, _rows = self.protected_queue()
        version = room["version"]
        for _ in range(3):
            state = rooms.reconcile(room["code"])
        self.assertEqual(self.pms.calls, [])
        self.assertEqual(state["version"], version)

    def test_duplicate_instances_remain_distinct_across_protected_boundary(self):
        room, rows = self.protected_queue()
        self.assertEqual(len({r["queue_item_id"] for r in rows}), 4)
        state = rooms.edit(
            room["code"],
            self.user["id"],
            remove_id=rows[2]["id"],
            version=room["version"],
        )
        self.assertEqual(
            self.pms.calls, [("remove", "82606", rows[2]["queue_item_id"])]
        )
        self.assertEqual(
            [r["id"] for r in self.request_queue(state)],
            [rows[n]["id"] for n in (0, 1, 3)],
        )

    def test_concurrent_edit_and_reconcile_share_protected_boundary(self):
        room, rows = self.protected_queue()
        barrier, errors = Barrier(2), []
        order = [rows[n]["id"] for n in (0, 3, 1, 2)]

        def run(edit):
            try:
                barrier.wait(timeout=5)
                if edit:
                    rooms.edit(
                        room["code"],
                        self.user["id"],
                        order=order,
                        version=room["version"],
                    )
                else:
                    rooms.reconcile(room["code"])
            except Exception as exc:  # noqa: BLE001 - surface thread failures in the parent assertion.
                errors.append(exc)

        threads = [Thread(target=run, args=(value,)) for value in (True, False)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(
            [r["id"] for r in self.request_queue(rooms.snapshot(room["code"]))], order
        )
        self.assertEqual(
            self.pms.calls,
            [("move", "82606", rows[3]["queue_item_id"], rows[0]["queue_item_id"])],
        )

    def test_next_drift_with_same_stream_stops_before_host_write(self):
        room, rows = self.protected_queue()
        original_load = self.pms.load
        calls = 0

        def load(queue):
            nonlocal calls
            calls += 1
            if calls == 3:
                self.pms.items[2], self.pms.items[3] = (
                    self.pms.items[3],
                    self.pms.items[2],
                )
            return original_load(queue)

        with patch.object(self.pms, "load", side_effect=load):
            response = self.edit(
                room,
                "order",
                {
                    "entryIds": [rows[n]["id"] for n in (0, 3, 1, 2)],
                    "version": room["version"],
                },
            )
        self.assertEqual(response.status_code, 502)
        self.assertIn("Up Next changed", rooms.snapshot(room["code"])["syncError"])
        self.assertEqual(self.pms.calls, [])

    def test_external_reorder_adopts_new_up_next_without_writes(self):
        room, rows = self.protected_queue()
        self.pms.items[2], self.pms.items[3] = self.pms.items[3], self.pms.items[2]
        state = rooms.reconcile(room["code"])
        self.assertIsNone(state["syncError"])
        self.assertEqual(
            [entry["id"] for entry in state["queue"]],
            [rows[n]["id"] for n in (1, 0, 2, 3)],
        )
        self.assertTrue(state["queue"][0]["locked"])
        self.assertEqual(self.pms.calls, [])

    def test_stale_queue_snapshot_never_changes_pms(self):
        room, _rows = self.protected_queue()
        self.pms.items.append(item(900))
        queue = self.pms.load("82606")
        queue["playQueueID"] = "99999"
        with patch.object(self.pms, "load", return_value=queue):
            response = self.post(f"/api/rooms/{room['code']}/sync")
        self.assertEqual(response.status_code, 502)
        self.assertTrue(rooms.snapshot(room["code"])["syncError"])
        self.assertEqual(self.pms.calls, [])

    def test_service_rejects_locked_removal_and_current_mutation(self):
        room, rows = self.protected_queue()
        with self.assertRaises(rooms.RoomError):
            rooms.edit(
                room["code"],
                self.user["id"],
                remove_id=rows[0]["id"],
                version=room["version"],
            )
        stored = rooms.room_by_code(room["code"])
        for identity in (stored["current_item_id"], stored["next_item_id"]):
            with (
                self.subTest(identity=identity),
                self.assertRaises(plex_rooms.QueueError),
            ):
                rooms._protect(stored, self.pms, identity)
        with self.assertRaises(plex_rooms.QueueError):
            rooms._protect(
                stored,
                self.pms,
                rows[1]["queue_item_id"],
                after=stored["current_item_id"],
            )
        self.assertEqual(self.pms.calls, [])

    def test_stream_transition_during_host_move_stops_before_write(self):
        room, rows = self.protected_queue()
        load = self.pms.load
        calls = 0

        def advance(queue):
            nonlocal calls
            calls += 1
            if calls == 3:
                self.pms.current = rows[0]["queue_item_id"]
                self.pms.session_key = "9120"
                self.event["sessionKey"] = "9120"
            return load(queue)

        with patch.object(self.pms, "load", side_effect=advance):
            response = self.edit(
                room,
                "order",
                {
                    "entryIds": [rows[n]["id"] for n in (0, 3, 1, 2)],
                    "version": room["version"],
                },
            )
        self.assertEqual(response.status_code, 502)
        self.assertTrue(rooms.snapshot(room["code"])["syncError"])
        self.assertEqual(self.pms.calls, [])

    def test_append_that_changes_existing_next_is_not_reordered_or_confirmed(self):
        room = self.start()
        original_add = self.pms.add

        def add(queue, track):
            original_add(queue, track)
            self.pms.items.insert(1, self.pms.items.pop())

        with patch.object(self.pms, "add", side_effect=add):
            response = self.post(
                f"/api/rooms/{room['code']}/entries", {"choiceId": self.choice(room)}
            )
        self.assertEqual(response.status_code, 502)
        state = rooms.snapshot(room["code"])
        self.assertIn("Up Next changed", state["syncError"])
        self.assertEqual(self.request_queue(state)[0]["state"], "waiting_for_queue")
        self.assertEqual(self.pms.calls[-1], ("add", "82606", "500"))
        self.assertFalse(any(call[0] == "move" for call in self.pms.calls))

    def test_first_request_initializes_empty_manual_region_without_changing_next(self):
        self.pms.manual_end = "0"
        room = self.start()
        self.pms.calls.clear()
        state = self.add(room)
        self.assertIsNone(state["syncError"])
        self.assertEqual(self.request_queue(state)[0]["state"], "ready")
        self.assertEqual(
            self.pms.calls, [("move", "82606", "102", "101"), ("add", "82606", "500")]
        )
        self.assertEqual(
            [i["playQueueItemID"] for i in self.pms.items], ["101", "102", "201"]
        )
        calls = deepcopy(self.pms.calls)
        rooms.reconcile(room["code"])
        self.add(rooms.snapshot(room["code"]))
        self.assertEqual(self.pms.calls, [*calls, ("add", "82606", "500")])

    def test_consumed_manual_region_reinitializes_same_live_next_instance(self):
        room, rows = self.protected_queue()
        self.pms.manual_end = self.pms.current
        state = self.add(room)
        self.assertIsNone(state["syncError"])
        self.assertEqual(
            self.pms.calls[:2],
            [
                ("move", "82606", rows[0]["queue_item_id"], self.pms.current),
                ("add", "82606", "500"),
            ],
        )
        self.assertTrue(self.request_queue(state)[0]["locked"])
        self.assertEqual(self.pms.items[2]["playQueueItemID"], rows[0]["queue_item_id"])
        self.assertEqual(
            [i["playQueueItemID"] for i in self.pms.items][2:],
            [*[row["queue_item_id"] for row in rows], "205"],
        )
        self.assertTrue(
            all(call[2] != rows[0]["queue_item_id"] for call in self.pms.calls[2:])
        )

    def test_bootstrap_without_confirmed_manual_region_never_appends(self):
        self.pms.manual_end = "0"
        room = self.start()
        self.pms.calls.clear()
        with patch.object(self.pms, "move") as move:
            response = self.post(
                f"/api/rooms/{room['code']}/entries", {"choiceId": self.choice(room)}
            )
        self.assertEqual(response.status_code, 502)
        move.assert_called_once_with("82606", "102", "101")
        self.assertEqual(self.pms.calls, [])
        state = rooms.snapshot(room["code"])
        self.assertIn("did not confirm Room queue initialization", state["syncError"])
        self.assertEqual(self.request_queue(state)[0]["state"], "waiting_for_queue")

    def test_bootstrap_failure_retains_first_request_for_retry(self):
        self.pms.manual_end = "0"
        room = self.start()
        self.pms.calls.clear()
        self.pms.fail = "move"
        response = self.post(
            f"/api/rooms/{room['code']}/entries", {"choiceId": self.choice(room)}
        )
        self.assertEqual(response.status_code, 502)
        self.assertFalse(any(call[0] == "add" for call in self.pms.calls))
        self.pms.fail = None
        state = rooms.reconcile(room["code"])
        self.assertEqual(self.request_queue(state)[0]["state"], "ready")
        self.assertEqual(len([call for call in self.pms.calls if call[0] == "add"]), 1)
        self.assertEqual(
            [i["playQueueItemID"] for i in self.pms.items], ["101", "102", "201"]
        )

    def test_bootstrap_requires_entire_queue_order_to_stay_unchanged(self):
        room, _rows = self.protected_queue()
        self.pms.manual_end = "0"
        move = self.pms.move

        def reorder(queue, identity, after):
            move(queue, identity, after)
            self.pms.items[-1], self.pms.items[-2] = (
                self.pms.items[-2],
                self.pms.items[-1],
            )

        with patch.object(self.pms, "move", side_effect=reorder):
            response = self.post(
                f"/api/rooms/{room['code']}/entries", {"choiceId": self.choice(room)}
            )
        self.assertEqual(response.status_code, 502)
        self.assertIn("queue initialization", rooms.snapshot(room["code"])["syncError"])
        self.assertFalse(any(call[0] == "add" for call in self.pms.calls))

    def test_interrupted_bootstrap_is_recovered_without_repeating_promotion(self):
        self.pms.manual_end = "0"
        room = self.start()
        self.pms.calls.clear()
        move = self.pms.move

        def interrupted(queue, identity, after):
            move(queue, identity, after)
            raise plex_rooms.QueueError("Interrupted queue initialization")

        with patch.object(self.pms, "move", side_effect=interrupted):
            response = self.post(
                f"/api/rooms/{room['code']}/entries", {"choiceId": self.choice(room)}
            )
        self.assertEqual(response.status_code, 502)
        state = rooms.reconcile(room["code"])
        self.assertIsNone(state["syncError"])
        self.assertEqual(
            self.pms.calls, [("move", "82606", "102", "101"), ("add", "82606", "500")]
        )

    def test_bootstrap_stops_if_playback_advances_during_promotion(self):
        self.pms.manual_end = "0"
        room = self.start()
        self.pms.calls.clear()
        move = self.pms.move

        def advance(queue, identity, after):
            move(queue, identity, after)
            self.pms.current = "102"

        with patch.object(self.pms, "move", side_effect=advance):
            response = self.post(
                f"/api/rooms/{room['code']}/entries", {"choiceId": self.choice(room)}
            )
        self.assertEqual(response.status_code, 502)
        self.assertIn("changed during", rooms.snapshot(room["code"])["syncError"])
        self.assertFalse(any(call[0] == "add" for call in self.pms.calls))

    def test_concurrent_first_requests_initialize_manual_region_once(self):
        self.pms.manual_end = "0"
        room = self.start()
        self.pms.calls.clear()
        choice = self.choice(room)
        barrier, errors = Barrier(2), []

        def add():
            try:
                barrier.wait(timeout=5)
                rooms.add(room["code"], choice, self.user["username"])
            except Exception as exc:  # noqa: BLE001 - surface concurrent failures in the parent assertion.
                errors.append(exc)

        threads = [Thread(target=add) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len([call for call in self.pms.calls if call[0] == "move"]), 1)
        self.assertEqual(
            [i["playQueueItemID"] for i in self.pms.items], ["101", "102", "201", "202"]
        )

    def test_migration_adds_live_boundary_to_existing_rooms_idempotently(self):
        with sqlite3.connect(":memory:") as connection:
            room_storage.migrate(connection)
            connection.execute("ALTER TABLE rooms DROP COLUMN next_item_id")
            connection.execute("ALTER TABLE rooms DROP COLUMN up_next")
            connection.execute(
                "INSERT INTO rooms(id,code,host_user_id,status,created_at,server_id,client_id,session_key,queue_id,current_item_id,handoff_item_id) VALUES ('old','OLD',1,'active',0,'s','c','stream','q','a','b')"
            )
            room_storage.migrate(connection)
            room_storage.migrate(connection)
            self.assertEqual(
                connection.execute(
                    "SELECT current_item_id,handoff_item_id,next_item_id,up_next FROM rooms"
                ).fetchone(),
                ("a", "b", "", "{}"),
            )

    def test_new_stream_for_different_queue_never_writes_saved_queue(self):
        room = self.add(self.start(), OTHER)
        calls = deepcopy(self.pms.calls)
        self.pms.session_key = "9120"
        old_event = {**self.event, "ratingKey": "101", "playQueueItemID": "101"}
        new_event = {**old_event, "sessionKey": "9120", "playQueueID": "99999"}
        self.feed.latest.side_effect = lambda session, **kwargs: (
            new_event if session["session_key"] == "9120" else old_event
        )
        self.states[OTHER] = self.lifecycle("ready", "501")
        response = self.post(f"/api/rooms/{room['code']}/sync")
        self.assertEqual(response.status_code, 502)
        self.assertIn("switched queues", rooms.snapshot(room["code"])["syncError"])
        self.assertEqual(self.pms.calls, calls)
        row = self.request_entries(rooms.room_by_code(room["code"])["id"])[0]
        self.assertEqual(row["state"], "waiting_for_queue")
        self.assertIsNone(row["queue_item_id"])

    def test_notification_for_previous_recording_cannot_drive_queue_writes(self):
        room = self.add(self.start(), OTHER)
        calls = deepcopy(self.pms.calls)
        self.pms.current = "102"
        self.feed.latest.side_effect = lambda session, **kwargs: {
            **self.event,
            "playQueueItemID": "101",
            "ratingKey": "101",
        }
        self.states[OTHER] = self.lifecycle("ready", "501")
        response = self.post(f"/api/rooms/{room['code']}/sync")
        self.assertEqual(response.status_code, 502)
        self.assertEqual(self.pms.calls, calls)
        self.assertEqual(
            self.request_queue(rooms.snapshot(room["code"]))[0]["state"],
            "waiting_for_queue",
        )

    def test_late_materialization_is_not_ready_until_pms_confirms_its_position(self):
        room = self.add(self.add(self.start(), OTHER))
        self.states[OTHER] = self.lifecycle("ready", "501")
        self.pms.fail = "move"
        response = self.post(f"/api/rooms/{room['code']}/sync")
        self.assertEqual(response.status_code, 502)
        row = self.request_entries(rooms.room_by_code(room["code"])["id"])[0]
        self.assertIsNotNone(row["queue_item_id"])
        self.assertEqual(row["state"], "waiting_for_queue")
        # Even a repeated lifecycle read must not prematurely advertise Ready.
        response = self.post(f"/api/rooms/{room['code']}/sync")
        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            next(
                entry
                for entry in rooms.snapshot(room["code"])["queue"]
                if entry["id"] == row["id"]
            )["state"],
            "waiting_for_queue",
        )
        self.pms.fail = None
        response = self.post(f"/api/rooms/{room['code']}/sync")
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(
            self.request_queue(response.get_json()["room"])[0]["state"], "ready"
        )
        self.assertEqual(
            [track["ratingKey"] for track in self.pms.items],
            ["101", "102", "501", "500"],
        )
        self.assertEqual(
            len([call for call in self.pms.calls if call == ("add", "82606", "501")]), 1
        )

    def test_pms_move_without_effect_does_not_falsely_confirm_ready(self):
        room = self.add(self.add(self.start(), OTHER))
        self.states[OTHER] = self.lifecycle("ready", "501")
        with patch.object(self.pms, "move"):
            response = self.post(f"/api/rooms/{room['code']}/sync")
        self.assertEqual(response.status_code, 502)
        state = rooms.snapshot(room["code"])
        self.assertIn("did not confirm", state["syncError"])
        self.assertEqual(self.request_queue(state)[0]["state"], "waiting_for_queue")
        response = self.post(f"/api/rooms/{room['code']}/sync")
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(
            self.request_queue(response.get_json()["room"])[0]["state"], "ready"
        )

    def test_exhaustion_warning_and_no_repeated_queue_writes(self):
        room = self.start()
        self.assertTrue(room["queueWarning"])
        room = self.add(room)
        self.assertFalse(room["queueWarning"])
        calls = deepcopy(self.pms.calls)
        worker.tick()
        worker.tick()
        self.assertEqual(self.pms.calls, calls)
        self.pms.current = "102"
        worker.tick()
        self.assertTrue(rooms.snapshot(room["code"])["queueWarning"])

    def test_host_csrf_and_version_checks(self):
        room = self.add(self.start())
        self.assertEqual(
            self.client.post(f"/api/rooms/{room['code']}/end").status_code, 403
        )
        self.assertEqual(
            self.edit(
                room,
                "order",
                {
                    "entryIds": [r["id"] for r in self.request_queue(room)],
                    "version": -1,
                },
            ).status_code,
            409,
        )

    def test_guest_rate_limit_and_search_allowlist(self):
        room = self.start()
        client, _, _ = self.guest(room)
        payload = {
            "results": [
                {
                    "id": RELEASE,
                    "recordingMbid": RECORDING,
                    "name": "Album",
                    "matchedTrack": "Track",
                    "plex": {"token": "secret", "ratingKey": "1"},
                    "recordingState": {
                        "status": "ready",
                        "tracks": [{"path": "/private/music"}],
                    },
                }
            ]
        }
        with patch.object(
            routes,
            "_search_response",
            side_effect=lambda **kwargs: self.app.json.response(payload),
        ) as search:
            response = client.get(
                f"/api/rooms/{room['code']}/search?q=song&type=artist"
            )
            self.assertEqual(response.status_code, 200)
            search.assert_called_once_with(query="song", search_type="track")
            self.assertNotIn("secret", response.get_data(as_text=True))
            self.assertNotIn("/private/music", response.get_data(as_text=True))
            for _ in range(12):
                response = client.get(f"/api/rooms/{room['code']}/search?q=song")
            self.assertEqual(response.status_code, 429)
            self.assertEqual(response.headers["Retry-After"], "60")

    def test_sse_authorization_update_and_clean_closure(self):
        room = self.start()
        self.assertEqual(
            self.app.test_client().get(f"/api/rooms/{room['code']}/events").status_code,
            403,
        )
        client, _, _ = self.guest(room)
        response = client.get(f"/api/rooms/{room['code']}/events", buffered=False)
        iterator = iter(response.response)
        self.assertIn(b"event: room", next(iterator))
        self.post(f"/api/rooms/{room['code']}/end")
        remainder = b"".join(iterator)
        self.assertIn(b'"status": "closed"', remainder)
        response.close()
        self.assertEqual(routes._stream_identities, set())

    def test_guest_document_omits_auth_and_admin(self):
        body = self.client.get("/rooms/ABCDEFGHJK").get_data(as_text=True)
        self.assertIn("rooms.js", body)
        for text in ("app.js", "login-form", "Settings", "tab-bar", "apiKey"):
            self.assertNotIn(text, body)

    def test_real_shared_lifecycle_materializes_after_plex_index_update(self):
        with api_cache.cache_db() as connection:
            connection.execute("DELETE FROM track_search_plex_tracks")
            connection.execute("DELETE FROM track_search_plex_isrcs")
        target = {
            "releaseGroupMbid": RELEASE,
            "title": "Single",
            "artistName": "Artist",
            "primaryType": "Single",
        }
        with (
            patch.object(
                recording_requests, "recording_states", side_effect=self.real_states
            ),
            patch.object(recording_requests, "status", side_effect=self.real_status),
            patch.object(
                recording_requests, "request_for_user", side_effect=self.real_request
            ),
            patch.object(
                recording_acquisition,
                "resolve",
                return_value={
                    "state": "resolved",
                    "target": target,
                    "recordingTitle": "Song",
                },
            ),
            patch.object(
                release_requests,
                "request_release_group_for_user",
                return_value=release_requests.ReleaseGroupRequestResult({}, 202),
            ) as release,
        ):
            room = self.add(self.start(), OTHER)
            self.assertEqual(self.request_queue(room)[0]["state"], "requested")
            worker.tick()
            release.assert_called_once()
            self.assertEqual(
                storage.recording_acquisition(OTHER)["release_group_mbid"], RELEASE
            )
            track_search_index.index_plex_library(
                {
                    "serverId": "server",
                    "tracks": [
                        {
                            "ratingKey": "501",
                            "librarySectionId": "1",
                            "musicbrainzRecordingId": OTHER,
                            "title": "Song",
                            "albumTitle": "Single",
                            "key": "/library/metadata/501",
                        }
                    ],
                }
            )
            worker.tick()
            self.assertEqual(
                self.request_queue(rooms.snapshot(room["code"]))[0]["state"], "ready"
            )
            self.assertEqual(self.pms.items[-1]["ratingKey"], "501")

    def test_interrupted_add_can_be_removed_without_leaving_orphan(self):
        room = self.start()
        self.pms.fail = "after-add"
        self.assertEqual(
            self.post(
                f"/api/rooms/{room['code']}/entries", {"choiceId": self.choice(room)}
            ).status_code,
            502,
        )
        state = rooms.snapshot(room["code"])
        response = self.edit(
            state,
            f"entries/{self.request_queue(state)[0]['id']}",
            {"version": state["version"]},
            "DELETE",
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(len(self.pms.items), 2)

    def test_start_requires_no_pms_mutation_even_if_writes_are_unavailable(self):
        self.pms.fail = "remove"
        room = self.start()
        self.assertIsNone(room["syncError"])
        self.assertEqual(self.pms.calls, [])
        self.assertEqual([entry["title"] for entry in room["queue"]], ["Song 102"])

    def test_acquisition_continues_while_pms_is_unavailable(self):
        room = self.start()

        def acquire(recording, _host):
            self.states[recording] = self.lifecycle("queued", "501")
            return {}, 202

        self.acquire.side_effect = acquire
        with patch.object(
            self.pms, "load", side_effect=plex_rooms.QueueError("Safe PMS outage")
        ):
            response = self.post(
                f"/api/rooms/{room['code']}/entries",
                {"choiceId": self.choice(room, OTHER)},
            )
            self.assertEqual(response.status_code, 502)
            worker.tick()
            self.acquire.assert_called_once()
            state = rooms.snapshot(room["code"])
            self.assertEqual(self.request_queue(state)[0]["state"], "queued")
            self.assertTrue(state["syncError"])
            self.states[OTHER] = self.lifecycle("ready", "501")
            worker.tick()
            self.assertEqual(
                self.request_queue(rooms.snapshot(room["code"]))[0]["state"],
                "waiting_for_queue",
            )
        worker.tick()
        self.assertEqual(
            self.request_queue(rooms.snapshot(room["code"]))[0]["state"], "ready"
        )
        self.assertEqual(self.pms.items[-1]["ratingKey"], "501")

    def test_legacy_startup_trim_journal_is_retired_without_deleting_queue(self):
        room = self.start()
        self.pms.items.extend([item(103), item(104)])
        with storage.db() as connection:
            connection.execute(
                "UPDATE rooms SET trim_ids=? WHERE code=?",
                (json.dumps({"current": "101", "items": ["103", "104"]}), room["code"]),
            )
        self.pms.current = "102"
        state = rooms.reconcile(room["code"])
        self.assertIsNone(state["syncError"])
        self.assertEqual(state["handoff"], {})
        self.assertEqual(
            [entry["title"] for entry in state["queue"]], ["Song 103", "Song 104"]
        )
        self.assertEqual(self.pms.calls, [])
        self.assertEqual(rooms.room_by_code(room["code"])["trim_ids"], "[]")

    def test_acquisition_failure_is_visible_and_retry_uses_shared_workflow(self):
        room = self.add(self.start(), OTHER)
        self.acquire.return_value = ({"error": "secret upstream data"}, 502)
        worker.tick()
        state = rooms.snapshot(room["code"])
        self.assertIn(
            "Acquisition request failed", self.request_queue(state)[0]["error"]
        )
        self.assertNotIn("secret", json.dumps(state))
        worker.tick()
        self.acquire.assert_called_once()
        self.acquire.return_value = ({}, 202)
        self.post(f"/api/rooms/{room['code']}/sync")
        self.assertEqual(self.acquire.call_count, 2)

    def test_queue_switch_or_lost_notifications_never_mutates_other_queue(self):
        room = self.add(self.start())
        calls = list(self.pms.calls)
        self.event["playQueueID"] = "unrelated-queue"
        worker.tick()
        self.assertEqual(self.pms.calls, calls)
        self.assertIn("switched queues", rooms.snapshot(room["code"])["syncError"])
        self.feed.latest.side_effect = None
        self.feed.latest.return_value = None
        worker.tick()
        self.assertEqual(self.pms.calls, calls)
        version = rooms.snapshot(room["code"])["version"]
        worker.tick()
        self.assertEqual(rooms.snapshot(room["code"])["version"], version)

    def test_signed_in_non_owner_and_api_key_cannot_end_or_host(self):
        room = self.start()
        with storage.db() as connection:
            other = connection.execute(
                "INSERT INTO users(username,password_hash,role,created_at) VALUES ('other','unused','admin',0)"
            ).lastrowid
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["user_id"], session["csrf_token"] = other, "other-csrf"
        self.assertEqual(
            self.post(
                f"/api/rooms/{room['code']}/end",
                client=client,
                headers={"X-CSRF-Token": "other-csrf"},
            ).status_code,
            403,
        )
        self.assertEqual(
            self.post(
                "/api/rooms",
                client=self.app.test_client(),
                headers={"X-Api-Key": self.app.config["AUTOMATION_API_KEY"]},
            ).status_code,
            401,
        )

    def test_start_with_current_and_one_next_imports_locked_entry(self):
        room = self.start()
        self.assertEqual([entry["title"] for entry in room["queue"]], ["Song 102"])
        self.assertTrue(room["queue"][0]["locked"])
        self.assertEqual(self.pms.calls, [])

    def test_single_external_append_persists_safe_metadata_without_acquisition_identity(
        self,
    ):
        room = self.start()
        self.pms.items.append(
            {
                **item(900),
                "title": "T" * 400,
                "originalTitle": "Track artist",
                "parentTitle": "Album",
                "thumb": "http://private/token=secret",
                "secret": "secret",
            }
        )
        worker.tick()
        state = rooms.snapshot(room["code"])
        self.assertGreater(state["version"], room["version"])
        entry = state["queue"][-1]
        self.assertEqual(
            (
                entry["title"],
                entry["artist"],
                entry["album"],
                entry["requester"],
                entry["state"],
            ),
            ("T" * 300, "Track artist", "Album", None, "ready"),
        )
        row = rooms.entries(rooms.room_by_code(room["code"])["id"])[-1]
        self.assertEqual(
            (row["queue_item_id"], row["rating_key"], row["recording_mbid"]),
            ("900", "900", None),
        )
        self.assertNotIn("secret", json.dumps(state))
        self.assertEqual(self.pms.calls, [])
        self.acquire.assert_not_called()

    def test_external_removal_of_imported_entry_is_tombstoned(self):
        self.pms.items.extend([item(103), item(104)])
        room = self.start()
        original = rooms.entries(rooms.room_by_code(room["code"])["id"])
        self.pms.items = [
            track for track in self.pms.items if track["playQueueItemID"] != "103"
        ]
        state = rooms.reconcile(room["code"])
        self.assertEqual(
            [entry["title"] for entry in state["queue"]], ["Song 102", "Song 104"]
        )
        rows = {
            row["id"]: row
            for row in rooms.entries(rooms.room_by_code(room["code"])["id"])
        }
        self.assertTrue(rows[original[1]["id"]]["removed"])
        self.assertEqual(rows[original[1]["id"]]["playback"], "upcoming")
        self.assertEqual(self.pms.calls, [])

    def test_external_removal_of_one_requested_duplicate_preserves_other_and_requester(
        self,
    ):
        room, rows = self.protected_queue()
        self.pms.items = [
            track
            for track in self.pms.items
            if track["playQueueItemID"] != rows[2]["queue_item_id"]
        ]
        state = rooms.reconcile(room["code"])
        self.assertEqual(
            [entry["id"] for entry in state["queue"]],
            [rows[n]["id"] for n in (0, 1, 3)],
        )
        stored = {
            row["id"]: row
            for row in rooms.entries(rooms.room_by_code(room["code"])["id"])
        }
        self.assertTrue(stored[rows[2]["id"]]["removed"])
        self.assertFalse(stored[rows[3]["id"]]["removed"])
        self.assertEqual(stored[rows[3]["id"]]["recording_mbid"], RECORDING)
        self.assertEqual(stored[rows[3]["id"]]["requester"], self.user["username"])
        self.assertEqual(self.pms.calls, [])

    def test_external_future_reorder_matches_pms_and_is_idempotent(self):
        room, rows = self.protected_queue()
        self.pms.items.insert(3, self.pms.items.pop())
        state = rooms.reconcile(room["code"])
        self.assertEqual(
            [entry["id"] for entry in state["queue"]],
            [rows[n]["id"] for n in (0, 3, 1, 2)],
        )
        saved = rooms.entries(rooms.room_by_code(room["code"])["id"])
        for _ in range(3):
            repeated = rooms.reconcile(room["code"])
            self.assertEqual(repeated, state)
            self.assertEqual(
                rooms.entries(rooms.room_by_code(room["code"])["id"]), saved
            )
        self.assertEqual(self.pms.calls, [])

    def test_external_boundary_is_adopted_before_host_controls_validate(self):
        room, rows = self.protected_queue()
        self.pms.items[2], self.pms.items[3] = self.pms.items[3], self.pms.items[2]
        response = self.edit(
            room, f"entries/{rows[1]['id']}", {"version": room["version"]}, "DELETE"
        )
        self.assertEqual(response.status_code, 409)
        self.assertIn("Up Next is locked", response.get_json()["error"])
        state = rooms.snapshot(room["code"])
        self.assertEqual(state["queue"][0]["id"], rows[1]["id"])
        self.assertTrue(state["queue"][0]["locked"])
        self.assertEqual(self.pms.calls, [])

    def test_normal_advancement_and_pruned_playing_item_are_not_external_removals(self):
        room, rows = self.protected_queue()
        self.pms.current = rows[0]["queue_item_id"]
        rooms.reconcile(room["code"])
        self.pms.items = [
            track
            for track in self.pms.items
            if track["playQueueItemID"] != rows[0]["queue_item_id"]
        ]
        self.pms.current = rows[1]["queue_item_id"]
        rooms.reconcile(room["code"])
        saved = {
            row["id"]: row
            for row in rooms.entries(rooms.room_by_code(room["code"])["id"])
        }
        self.assertEqual(saved[rows[0]["id"]]["playback"], "played")
        self.assertFalse(saved[rows[0]["id"]]["removed"])
        self.assertEqual(saved[rows[1]["id"]]["playback"], "playing")
        self.assertFalse(saved[rows[1]["id"]]["removed"])
        self.assertEqual(self.pms.calls, [])

    def test_pending_requests_survive_external_add_and_removal_then_materialize(self):
        room = self.add(self.start(), OTHER)
        pending_id = self.request_queue(room)[0]["id"]
        self.pms.items.extend([item(900), item(901)])
        state = rooms.reconcile(room["code"])
        self.assertEqual(state["queue"][-1]["id"], pending_id)
        self.pms.items = [
            track for track in self.pms.items if track["playQueueItemID"] != "900"
        ]
        state = rooms.reconcile(room["code"])
        self.assertEqual(state["queue"][-1]["id"], pending_id)
        self.assertEqual(state["queue"][-1]["state"], "requested")
        self.assertEqual(self.pms.calls, [])
        self.states[OTHER] = self.lifecycle("ready", "501")
        worker.tick()
        state = rooms.snapshot(room["code"])
        self.assertEqual(state["queue"][-1]["id"], pending_id)
        self.assertEqual(state["queue"][-1]["state"], "ready")
        self.assertEqual(
            [track["ratingKey"] for track in self.pms.items],
            ["101", "102", "901", "501"],
        )

    def test_external_reorder_with_pending_requests_is_adopted_and_retains_intent(
        self,
    ):
        self.pms.items.extend([item(103), item(104)])
        room = self.add(self.start(), OTHER)
        before = rooms.entries(rooms.room_by_code(room["code"])["id"])
        self.pms.items[-2], self.pms.items[-1] = self.pms.items[-1], self.pms.items[-2]
        response = self.post(f"/api/rooms/{room['code']}/sync")
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertIsNone(response.get_json()["room"]["syncError"])
        after = rooms.entries(rooms.room_by_code(room["code"])["id"])
        self.assertEqual({row["id"] for row in after}, {row["id"] for row in before})
        self.assertEqual(
            [row["queue_item_id"] for row in after if row["queue_item_id"]],
            ["102", "104", "103"],
        )
        self.assertEqual(self.pms.calls, [])
        self.assertFalse(rooms.room_by_code(room["code"])["write_pending"])

    def test_observation_error_does_not_turn_external_reorder_into_write_intent(self):
        room, rows = self.protected_queue()
        self.feed.latest.side_effect = None
        self.feed.latest.return_value = None
        worker.tick()
        self.assertFalse(rooms.room_by_code(room["code"])["write_pending"])
        self.feed.latest.side_effect = lambda session, **kwargs: {
            **self.event,
            "playQueueItemID": self.pms.current,
            "ratingKey": self.pms.active_session(self.user)["rating_key"],
        }
        self.pms.items.insert(3, self.pms.items.pop())
        state = rooms.reconcile(room["code"])
        self.assertIsNone(state["syncError"])
        self.assertEqual(
            [entry["id"] for entry in state["queue"]],
            [rows[n]["id"] for n in (0, 3, 1, 2)],
        )
        self.assertEqual(self.pms.calls, [])

    def test_external_add_remove_reorder_emit_sse_and_unchanged_tick_has_no_event(self):
        self.pms.items.extend([item(103), item(104)])
        room = self.start()
        client, _, _ = self.guest(room)
        response = client.get(f"/api/rooms/{room['code']}/events", buffered=False)
        self.addCleanup(response.close)
        iterator = iter(response.response)
        previous = json.loads(next(iterator).decode().split("data: ", 1)[1])
        changes = (
            lambda: self.pms.items.append(item(900)),
            lambda: self.pms.items.pop(-2),
            lambda: self.pms.items.insert(2, self.pms.items.pop()),
        )
        with patch.object(routes.time, "sleep"):
            for change in changes:
                change()
                worker.tick()
                self.assertEqual(next(iterator), b": heartbeat\n\n")
                update = json.loads(next(iterator).decode().split("data: ", 1)[1])
                self.assertGreater(update["version"], previous["version"])
                self.assertEqual(update, rooms.snapshot(room["code"]))
                previous = update
            worker.tick()
            self.assertEqual(next(iterator), b": heartbeat\n\n")
            self.assertEqual(next(iterator), b": heartbeat\n\n")
        self.assertEqual(self.pms.calls, [])

    def test_external_suffix_change_during_host_write_stops_remaining_operations(self):
        room, rows = self.protected_queue()
        move = self.pms.move

        def external_append(queue, identity, after):
            move(queue, identity, after)
            self.pms.items.append(item(900))

        with patch.object(self.pms, "move", side_effect=external_append):
            response = self.edit(
                room,
                "order",
                {
                    "entryIds": [rows[n]["id"] for n in (0, 3, 2, 1)],
                    "version": room["version"],
                },
            )
        self.assertEqual(response.status_code, 502)
        self.assertEqual(len(self.pms.calls), 1)
        self.assertEqual(self.pms.items[-1]["playQueueItemID"], "900")
        self.assertTrue(rooms.room_by_code(room["code"])["write_pending"])

    def test_migration_relaxes_legacy_identity_and_preserves_journals_and_foreign_keys(
        self,
    ):
        with sqlite3.connect(":memory:") as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("CREATE TABLE users(id INTEGER PRIMARY KEY)")
            connection.execute("INSERT INTO users VALUES (1)")
            room_storage.migrate(connection)
            connection.execute("ALTER TABLE rooms DROP COLUMN write_pending")
            connection.execute("ALTER TABLE rooms DROP COLUMN write_intent")
            entry_sql = connection.execute(
                "SELECT sql FROM sqlite_master WHERE name='room_entries'"
            ).fetchone()[0]
            connection.execute("DROP TABLE room_entries")
            connection.execute(
                entry_sql.replace(
                    "recording_mbid TEXT", "recording_mbid TEXT NOT NULL"
                ).replace("requester TEXT", "requester TEXT NOT NULL")
            )
            connection.execute("ALTER TABLE room_entries DROP COLUMN album")
            connection.execute("ALTER TABLE room_entries DROP COLUMN deferred_until")
            connection.execute(
                "INSERT INTO rooms(id,code,host_user_id,status,created_at,server_id,client_id,session_key,queue_id,current_item_id,handoff_item_id) VALUES ('old','OLD',1,'active',0,'s','c','stream','q','1','2')"
            )
            connection.execute(
                "INSERT INTO room_guests VALUES ('guest','old','token','csrf','Name',0)"
            )
            connection.execute(
                "INSERT INTO room_entries(id,room_id,position,recording_mbid,title,artist,requester,guest_id,created_at,rating_key,add_before,removed) VALUES ('request','old',1,?,'Song','Artist','Name','guest',0,'500','[1,2]',1)",
                (RECORDING,),
            )
            before = connection.execute("SELECT * FROM room_entries").fetchone()
            room_storage.migrate(connection)
            room_storage.migrate(connection)
            self.assertEqual(
                connection.execute("SELECT * FROM room_entries").fetchone()[:6],
                before[:6],
            )
            self.assertEqual(
                connection.execute(
                    "SELECT recording_mbid,requester,guest_id,rating_key,add_before,removed FROM room_entries"
                ).fetchone(),
                (RECORDING, "Name", "guest", "500", "[1,2]", 1),
            )
            self.assertEqual(
                connection.execute("SELECT write_pending FROM rooms").fetchone(), (1,)
            )
            self.assertEqual(
                connection.execute("SELECT write_intent FROM rooms").fetchone(), ("{}",)
            )
            self.assertEqual(
                connection.execute(
                    "SELECT deferred_until FROM room_entries"
                ).fetchone(),
                (None,),
            )
            connection.execute(
                "INSERT INTO room_entries(id,room_id,position,title,artist,created_at,state,rating_key,queue_item_id) VALUES ('adopted','old',2,'Plex song','Artist',0,'ready','900','900')"
            )
            self.assertEqual(
                connection.execute("PRAGMA foreign_key_check").fetchall(), []
            )
            self.assertIn(
                "room_entries_order",
                [
                    row[1]
                    for row in connection.execute("PRAGMA index_list(room_entries)")
                ],
            )
            connection.execute("DELETE FROM rooms WHERE id='old'")
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM room_entries").fetchone()[0], 0
            )

    def test_host_edit_uses_latest_observation_if_playback_advances_during_recovery_read(
        self,
    ):
        room, rows = self.protected_queue()
        original_event = self.feed.latest.side_effect
        observations = 0
        self.pms.current = rows[0]["queue_item_id"]

        def advance_duplicate(session, **kwargs):
            nonlocal observations
            observations += 1
            if observations == 2:
                self.pms.current = rows[1]["queue_item_id"]
            return original_event(session, **kwargs)

        # Duplicate tracks retain the same rating key while the playing queue
        # instance advances between the two recovery observations.
        with patch.object(self.feed, "latest", side_effect=advance_duplicate):
            response = self.edit(
                room, f"entries/{rows[1]['id']}", {"version": room["version"]}, "DELETE"
            )
        self.assertEqual(response.status_code, 409)
        saved = {
            row["id"]: row
            for row in rooms.entries(rooms.room_by_code(room["code"])["id"])
        }
        self.assertEqual(saved[rows[1]["id"]]["playback"], "playing")
        self.assertFalse(saved[rows[1]["id"]]["removed"])
        self.assertEqual(self.pms.calls, [])

    def test_imported_queue_larger_than_guest_request_limit_can_still_be_reordered(
        self,
    ):
        self.pms.items = [item(identity) for identity in range(101, 304)]
        room = self.start()
        self.assertEqual(len(room["queue"]), 202)
        order = [entry["id"] for entry in room["queue"]]
        order[-1], order[-2] = order[-2], order[-1]
        response = self.edit(
            room, "order", {"entryIds": order, "version": room["version"]}
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(
            [entry["id"] for entry in response.get_json()["room"]["queue"]], order
        )
        self.assertEqual(len(self.pms.calls), 1)
        self.assertEqual(self.pms.calls[0][0], "move")

    def test_legacy_interrupted_append_recovers_with_unimported_startup_next(self):
        room = self.start()
        self.pms.fail = "after-add"
        response = self.post(
            f"/api/rooms/{room['code']}/entries", {"choiceId": self.choice(room)}
        )
        self.assertEqual(response.status_code, 502)
        with storage.db() as connection:
            # Previous versions kept the startup next only in Room fields.
            connection.execute(
                "DELETE FROM room_entries WHERE room_id=? AND queue_item_id='102'",
                (rooms.room_by_code(room["code"])["id"],),
            )
        state = rooms.reconcile(room["code"])
        self.assertIsNone(state["syncError"])
        self.assertEqual(
            [entry["title"] for entry in state["queue"]], ["Song 102", "Song 201"]
        )
        self.assertTrue(state["queue"][0]["locked"])
        self.assertEqual(state["queue"][1]["requester"], self.user["username"])
        self.assertEqual(self.pms.calls, [("add", "82606", "500")])
        self.assertEqual(rooms.reconcile(room["code"]), state)

    def pending_mix(self):
        """A playing, B locked, requested X, downloading Y, materialized C."""
        self.states[MISSING_RECORDING] = self.lifecycle("downloading", "270909")
        room = self.add(self.add(self.add(self.start(), OTHER), MISSING_RECORDING))
        rows = self.request_entries(rooms.room_by_code(room["code"])["id"])
        self.pms.calls.clear()
        self.assertEqual(
            [row["state"] for row in rows], ["requested", "downloading", "ready"]
        )
        return room, rows

    def test_pending_mix_adopts_external_add_remove_reorder_play_next_and_sse(self):
        room, pending = self.pending_mix()
        client, _, _ = self.guest(room)
        response = client.get(f"/api/rooms/{room['code']}/events", buffered=False)
        self.addCleanup(response.close)
        iterator = iter(response.response)
        next(iterator)
        c_id = pending[2]["queue_item_id"]
        stages = [
            [item(101), item(102), item(c_id, 500), item(900, 500), item(901)],
            [item(101), item(102), item(c_id, 500), item(900, 500)],
            [item(101), item(102), item(900, 500), item(c_id, 500)],
            [item(101), item(900, 500), item(102), item(c_id, 500)],
        ]
        version = room["version"]
        for tracks in stages:
            self.pms.items = tracks
            state = rooms.reconcile(room["code"])
            self.assertIsNone(state["syncError"])
            saved = rooms.entries(rooms.room_by_code(room["code"])["id"])
            live = [
                row["queue_item_id"]
                for row in saved
                if not row["removed"] and row["queue_item_id"]
            ]
            self.assertEqual(live, [track["playQueueItemID"] for track in tracks[1:]])
            self.assertEqual(
                {row["id"] for row in saved if not row["queue_item_id"]},
                {row["id"] for row in pending[:2]},
            )
            self.assertEqual(
                [entry["state"] for entry in self.request_queue(state)[:2]],
                ["requested", "downloading"],
            )
            self.assertGreater(state["version"], version)
            version = state["version"]
            with patch.object(routes.time, "sleep"):
                self.assertEqual(next(iterator), b": heartbeat\n\n")
                update = json.loads(next(iterator).decode().split("data: ", 1)[1])
            self.assertEqual(update, state)
            self.assertEqual(rooms.reconcile(room["code"]), state)
        self.assertEqual(state["queue"][0]["title"], "Song 900")
        self.assertTrue(state["queue"][0]["locked"])
        self.assertEqual(self.pms.calls, [])

    def test_pending_positions_before_locked_row_defer_only_placement_then_worker_retries(
        self,
    ):
        room, rows = self.pending_mix()
        room_id = rooms.room_by_code(room["code"])["id"]
        with storage.db() as connection:
            connection.execute(
                "UPDATE room_entries SET position=position+2 WHERE room_id=?",
                (room_id,),
            )
            connection.execute(
                "UPDATE room_entries SET position=0 WHERE id=?", (rows[0]["id"],)
            )
            connection.execute(
                "UPDATE room_entries SET position=1 WHERE id=?", (rows[1]["id"],)
            )
        self.assertTrue(rooms.snapshot(room["code"])["queue"][0]["locked"])
        self.assertFalse(rooms.entries(room_id)[0]["queue_item_id"])
        state = rooms.reconcile(room["code"])
        self.assertIsNone(state["syncError"])
        self.pms.items.append(item(900))
        self.states[MISSING_RECORDING] = self.lifecycle("waiting_for_plex", "270909")
        worker.tick()
        self.assertEqual(
            next(
                entry
                for entry in rooms.snapshot(room["code"])["queue"]
                if entry["id"] == rows[1]["id"]
            )["state"],
            "waiting_for_plex",
        )
        self.states[MISSING_RECORDING] = self.lifecycle("ready", "270909")
        worker.tick()
        state = rooms.snapshot(room["code"])
        y = next(entry for entry in state["queue"] if entry["id"] == rows[1]["id"])
        self.assertEqual(y["state"], "waiting_for_queue")
        self.assertIsNone(state["syncError"])
        self.assertEqual(state["queue"][-1]["title"], "Song 900")
        self.assertEqual(self.pms.calls, [])
        self.assertEqual(rooms.reconcile(room["code"]), state)
        self.pms.current = "102"
        worker.tick()
        row = next(row for row in rooms.entries(room_id) if row["id"] == rows[1]["id"])
        self.assertEqual(row["state"], "ready")
        self.assertIsNotNone(row["queue_item_id"])
        self.assertIsNone(row["add_before"])
        self.assertIsNone(row["deferred_until"])
        self.assertEqual(
            [track["ratingKey"] for track in self.pms.items],
            ["101", "102", "500", "270909", "900"],
        )
        self.assertEqual(rooms.room_by_code(room["code"])["write_intent"], "{}")
        calls = deepcopy(self.pms.calls)
        worker.tick()
        self.assertEqual(self.pms.calls, calls)

    def test_365_real_authoritative_ready_corrects_stale_requested_while_deferred(self):
        recording = "bd9fd6a1-d41b-4b82-9ead-a4f958749a77"
        self.states[recording] = self.lifecycle("downloading", "270909")
        room = self.add(self.start(), recording)
        row = self.request_entries(rooms.room_by_code(room["code"])["id"])[0]
        track = {
            "ratingKey": "270909",
            "key": "/library/metadata/270909",
            "librarySectionId": "1",
            "musicbrainzRecordingId": recording,
            "title": "365",
        }
        track_search_index.index_plex_library(
            {"serverId": "server", "tracks": [track]}, track_inventory=[track]
        )
        self.enterContext(
            patch.object(
                recording_requests, "recording_states", side_effect=self.real_states
            )
        )
        self.enterContext(
            patch.object(recording_requests, "status", side_effect=self.real_status)
        )
        lifecycle = self.client.get(
            f"/api/music/recording/{recording}/request"
        ).get_json()
        self.assertEqual(lifecycle["status"], "ready")
        self.assertTrue(lifecycle["available"])
        self.assertEqual(lifecycle["tracks"][0]["ratingKey"], "270909")
        self.assertEqual(lifecycle["tracks"][0]["key"], "/library/metadata/270909")
        with storage.db() as connection:
            connection.execute(
                "UPDATE room_entries SET state='requested',position=-1 WHERE id=?",
                (row["id"],),
            )
        worker.tick()
        state = rooms.snapshot(room["code"])
        self.assertIsNone(state["syncError"])
        self.assertEqual(self.request_queue(state)[0]["state"], "waiting_for_queue")
        self.assertEqual(rooms.reconcile(room["code"]), state)
        self.pms.current = "102"
        worker.tick()
        self.assertEqual(
            self.request_queue(rooms.snapshot(room["code"]))[0]["state"], "ready"
        )

    def test_saved_interrupted_add_adopts_unrelated_additions_and_new_up_next(self):
        room = self.start()
        self.pms.fail = "after-add"
        response = self.post(
            f"/api/rooms/{room['code']}/entries", {"choiceId": self.choice(room)}
        )
        self.assertEqual(response.status_code, 502)
        self.pms.items.insert(1, item(900))
        self.pms.items.append(item(901))
        state = rooms.reconcile(room["code"])
        self.assertIsNone(state["syncError"])
        self.assertTrue(state["queue"][0]["locked"])
        self.assertEqual(state["queue"][0]["title"], "Song 900")
        self.assertEqual(
            {entry["title"] for entry in state["queue"]},
            {"Song 900", "Song 102", "Song 201", "Song 901"},
        )
        self.assertEqual(len([call for call in self.pms.calls if call[0] == "add"]), 1)
        self.assertFalse(rooms.room_by_code(room["code"])["write_pending"])

    def test_saved_failed_add_adopts_external_queue_then_materializes_once(self):
        room = self.start()
        self.pms.fail = "add"
        response = self.post(
            f"/api/rooms/{room['code']}/entries", {"choiceId": self.choice(room)}
        )
        self.assertEqual(response.status_code, 502)
        self.pms.items.append(item(900))
        self.pms.fail = None
        state = rooms.reconcile(room["code"])
        self.assertIsNone(state["syncError"])
        self.assertEqual(
            {entry["title"] for entry in state["queue"]},
            {"Song 102", "Song 900", "Song 201"},
        )
        self.assertEqual(
            len([track for track in self.pms.items if track["ratingKey"] == "500"]), 1
        )
        self.assertFalse(rooms.room_by_code(room["code"])["write_pending"])

    def test_saved_remove_already_satisfied_externally_clears_journal(self):
        room = self.add(self.start())
        entry = self.request_queue(room)[0]
        identity = self.request_entries(rooms.room_by_code(room["code"])["id"])[0][
            "queue_item_id"
        ]
        self.pms.fail = "remove"
        self.assertEqual(
            self.edit(
                room, f"entries/{entry['id']}", {"version": room["version"]}, "DELETE"
            ).status_code,
            502,
        )
        self.pms.items = [
            track for track in self.pms.items if track["playQueueItemID"] != identity
        ]
        self.pms.items.append(item(900))
        self.pms.fail = None
        calls = deepcopy(self.pms.calls)
        state = rooms.reconcile(room["code"])
        self.assertIsNone(state["syncError"])
        self.assertEqual(
            [entry["title"] for entry in state["queue"]], ["Song 102", "Song 900"]
        )
        self.assertEqual(self.pms.calls, calls)
        self.assertEqual(rooms.room_by_code(room["code"])["write_intent"], "{}")

    def test_saved_remove_newly_locked_is_deferred_while_other_changes_adopt(self):
        room = self.add(self.start())
        entry = self.request_queue(room)[0]
        identity = self.request_entries(rooms.room_by_code(room["code"])["id"])[0][
            "queue_item_id"
        ]
        self.pms.fail = "remove"
        self.assertEqual(
            self.edit(
                room, f"entries/{entry['id']}", {"version": room["version"]}, "DELETE"
            ).status_code,
            502,
        )
        self.pms.items.insert(1, self.pms.items.pop())
        self.pms.items.append(item(900))
        self.pms.fail = None
        calls = deepcopy(self.pms.calls)
        state = rooms.reconcile(room["code"])
        self.assertIsNone(state["syncError"])
        self.assertEqual(state["queue"][0]["id"], entry["id"])
        self.assertTrue(state["queue"][0]["locked"])
        self.assertEqual(self.pms.calls, calls)
        self.assertEqual(
            json.loads(rooms.room_by_code(room["code"])["write_intent"])["remove"],
            [identity],
        )
        self.assertEqual(rooms.reconcile(room["code"]), state)
        self.pms.current = identity
        worker.tick()
        self.assertFalse(rooms.room_by_code(room["code"])["write_pending"])
        self.assertEqual(self.pms.calls, calls)

    def test_saved_reorder_rebases_known_subset_preserving_external_items_and_boundary(
        self,
    ):
        room, rows = self.protected_queue()
        self.pms.fail = "move"
        response = self.edit(
            room,
            "order",
            {
                "entryIds": [rows[n]["id"] for n in (0, 3, 2, 1)],
                "version": room["version"],
            },
        )
        self.assertEqual(response.status_code, 502)
        self.pms.items.insert(4, item(900))
        self.pms.items.insert(2, item(901))
        self.pms.fail = None
        self.pms.calls.clear()
        state = rooms.reconcile(room["code"])
        self.assertIsNone(state["syncError"])
        self.assertTrue(state["queue"][0]["locked"])
        self.assertEqual(
            [track["playQueueItemID"] for track in self.pms.items],
            [
                "101",
                "102",
                "901",
                rows[0]["queue_item_id"],
                rows[3]["queue_item_id"],
                "900",
                rows[2]["queue_item_id"],
                rows[1]["queue_item_id"],
            ],
        )
        self.assertTrue(
            all(call[2] not in {"900", "901", "102"} for call in self.pms.calls)
        )
        self.assertFalse(rooms.room_by_code(room["code"])["write_pending"])

    def test_legacy_boundary_only_write_flag_does_not_block_external_import(self):
        room, rows = self.pending_mix()
        with storage.db() as connection:
            connection.execute(
                "UPDATE room_entries SET position=-1 WHERE id=?", (rows[0]["id"],)
            )
            connection.execute(
                "UPDATE rooms SET write_pending=1,write_intent='{}' WHERE code=?",
                (room["code"],),
            )
        self.pms.items.append(item(900))
        state = rooms.reconcile(room["code"])
        self.assertIsNone(state["syncError"])
        self.assertIn("Song 900", [entry["title"] for entry in state["queue"]])
        self.assertFalse(rooms.room_by_code(room["code"])["write_pending"])
        self.assertEqual(self.pms.calls, [])

    def test_ready_lifecycle_without_playable_copy_defers_only_that_entry(self):
        room = self.add(self.start(), OTHER)
        self.states[OTHER] = {"status": "ready", "available": True, "tracks": []}
        self.pms.items.append(item(900))
        state = rooms.reconcile(room["code"])
        self.assertIsNone(state["syncError"])
        self.assertEqual(self.request_queue(state)[0]["state"], "waiting_for_queue")
        self.assertIn("Song 900", [entry["title"] for entry in state["queue"]])
        self.assertEqual(self.pms.calls, [])
        self.assertEqual(rooms.reconcile(room["code"]), state)
        self.states[OTHER] = self.lifecycle("ready", "501")
        worker.tick()
        self.assertEqual(
            self.request_queue(rooms.snapshot(room["code"]))[0]["state"], "ready"
        )


class PMSProtocolTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.pms = plex_rooms.PMSQueue(CONFIG)
        self.session = {
            "type": "track",
            "sessionKey": "9117",
            "ratingKey": "77",
            "User": {"id": 999, "title": "Plex User"},
            "Player": {
                "machineIdentifier": "dynamic-client",
                "product": "Plexamp",
                "state": "playing",
            },
        }
        self.user = {"plex_id": "123", "plex_username": "plex user"}

    def test_session_uses_username_not_server_local_user_id(self):
        other = {**self.session, "User": {"id": 123, "title": "Someone else"}}
        with patch.object(
            self.pms, "call", return_value={"Metadata": [other, self.session]}
        ):
            self.assertEqual(
                self.pms.active_session(self.user)["client_id"], "dynamic-client"
            )

    def test_idle_unlinked_and_ambiguous_sessions_fail(self):
        for sessions in (
            [],
            [{**self.session, "Player": {**self.session["Player"], "state": "paused"}}],
            [self.session, self.session],
        ):
            with (
                patch.object(self.pms, "call", return_value={"Metadata": sessions}),
                self.assertRaises(plex_rooms.QueueError),
            ):
                self.pms.active_session(self.user)

    def test_room_stream_refresh_keeps_original_device_and_accepts_pause(self):
        other_device = {
            **self.session,
            "Player": {**self.session["Player"], "machineIdentifier": "other-client"},
        }
        renewed = {
            **self.session,
            "sessionKey": "9118",
            "ratingKey": "78",
            "Player": {**self.session["Player"], "state": "paused"},
        }
        other_user = {**renewed, "User": {"title": "Someone else"}}
        with patch.object(
            self.pms,
            "call",
            return_value={"Metadata": [other_device, renewed, other_user]},
        ):
            session = self.pms.active_session(
                self.user, client_id="dynamic-client", allow_paused=True
            )
        self.assertEqual(
            session,
            {"client_id": "dynamic-client", "session_key": "9118", "rating_key": "78"},
        )

    def test_room_stream_refresh_rejects_other_devices_and_other_users(self):
        for candidate in (
            {
                **self.session,
                "Player": {
                    **self.session["Player"],
                    "machineIdentifier": "other-client",
                },
            },
            {**self.session, "User": {"id": 123, "title": "Someone else"}},
        ):
            with (
                patch.object(self.pms, "call", return_value={"Metadata": [candidate]}),
                self.assertRaises(plex_rooms.QueueError),
            ):
                self.pms.active_session(
                    self.user, client_id="dynamic-client", allow_paused=True
                )

    def test_notification_matching_extracts_queue_and_current(self):
        event = {
            "clientIdentifier": "dynamic-client",
            "sessionKey": "9117",
            "ratingKey": "77",
            "playQueueID": "82606",
            "playQueueItemID": "56423286",
            "state": "playing",
        }
        parsed = plex_rooms.notification_rows(
            json.dumps(
                {"NotificationContainer": {"PlaySessionStateNotification": [event]}}
            )
        )
        session = {"client_id": "dynamic-client", "session_key": "9117"}
        self.assertTrue(plex_rooms.matches(parsed[0], session))
        self.assertFalse(plex_rooms.matches({**event, "sessionKey": "wrong"}, session))
        self.assertFalse(
            plex_rooms.matches({**event, "clientIdentifier": "other-client"}, session)
        )
        with (
            patch.object(self.pms, "call", return_value={"Metadata": [self.session]}),
            patch.object(
                plex_rooms, "feed", return_value=Mock(latest=Mock(return_value=event))
            ),
        ):
            result = self.pms.discover(self.user)
        self.assertEqual(result["queue_id"], "82606")
        self.assertEqual(result["current_item_id"], "56423286")

    def test_missing_notifications_and_track_change_fail_closed(self):
        for event in (None, {"state": "playing", "ratingKey": "other"}):
            with (
                patch.object(
                    self.pms, "call", return_value={"Metadata": [self.session]}
                ),
                patch.object(
                    plex_rooms,
                    "feed",
                    return_value=Mock(latest=Mock(return_value=event)),
                ),
                self.assertRaises(plex_rooms.QueueError),
            ):
                self.pms.discover(self.user)

    def test_full_queue_required(self):
        with (
            patch.object(
                self.pms,
                "call",
                return_value={
                    "playQueueID": "1",
                    "playQueueTotalCount": 200,
                    "Metadata": [item(1)],
                },
            ),
            self.assertRaises(plex_rooms.QueueError),
        ):
            self.pms.load("1")

    def test_duplicate_queue_item_ids_fail_closed(self):
        with (
            patch.object(
                self.pms,
                "call",
                return_value={"playQueueID": "1", "Metadata": [item(1), item(1)]},
            ),
            self.assertRaises(plex_rooms.QueueError),
        ):
            self.pms.load("1")

    def test_rest_uri_and_item_operations(self):
        responses = [
            Response(
                payload={
                    "MediaContainer": {
                        "Directory": [
                            {"type": "artist", "key": "1", "uuid": "section-uuid"}
                        ]
                    }
                }
            )
        ] + [Response(payload={"MediaContainer": {}})] * 3
        with patch.object(requests, "request", side_effect=responses) as http:
            self.pms.add("82606", {"ratingKey": "500", "librarySectionId": "1"})
            self.pms.move("82606", "200", "102")
            self.pms.remove("82606", "200")
        self.assertEqual(
            http.call_args_list[1].kwargs["params"],
            {"uri": "library://section-uuid/item/library/metadata/500", "next": 0},
        )
        self.assertEqual(
            http.call_args_list[2].args,
            ("PUT", "http://pms.invalid/playQueues/82606/items/200/move"),
        )
        self.assertEqual(http.call_args_list[2].kwargs["params"], {"after": "102"})
        for call in http.call_args_list:
            self.assertFalse(call.kwargs["allow_redirects"])
            self.assertNotIn("private-plex-token", call.args[1])

    def test_provider_exception_never_exposes_token_or_url(self):
        with (
            patch.object(
                requests,
                "request",
                side_effect=requests.RequestException(
                    "http://private/?X-Plex-Token=secret"
                ),
            ),
            self.assertRaises(plex_rooms.QueueError) as raised,
        ):
            self.pms.call("GET", "/status/sessions")
        self.assertNotIn("private", str(raised.exception))

    def test_notification_socket_uses_headers_and_rejects_disconnected_cache(self):
        event = {
            "clientIdentifier": "dynamic-client",
            "sessionKey": "9117",
            "ratingKey": "77",
            "playQueueID": "82606",
            "playQueueItemID": "56423286",
            "state": "playing",
        }
        with patch.object(plex_rooms, "Thread"):
            notifications = plex_rooms.NotificationFeed(CONFIG)
        socket = Mock()

        def receive():
            notifications.stopped.set()
            return json.dumps(
                {
                    "NotificationContainer": {
                        "PlaySessionStateNotification": [None, event]
                    }
                }
            )

        socket.recv.side_effect = receive
        with patch.object(
            plex_rooms.websocket, "create_connection", return_value=socket
        ) as connect:
            notifications.run()
        self.assertEqual(
            connect.call_args.args[0], "ws://pms.invalid/:/websockets/notifications"
        )
        self.assertEqual(
            connect.call_args.kwargs["header"], {"X-Plex-Token": CONFIG["token"]}
        )
        self.assertEqual(connect.call_args.kwargs["redirect_limit"], 0)
        session = {"client_id": "dynamic-client", "session_key": "9117"}
        self.assertIsNone(notifications.latest(session))
        notifications.connected = True
        self.assertEqual(notifications.latest(session)["playQueueID"], "82606")
