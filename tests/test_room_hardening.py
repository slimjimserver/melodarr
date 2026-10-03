"""Room-local retention, private inspection, lifecycle projections and device binding."""

# isort: skip_file
from ._test_environment import TEST_ROOT  # noqa: F401
from .test_rooms import CONFIG, OTHER, RECORDING, RELEASE, RoomTestCase, item

import json
from copy import deepcopy
from unittest.mock import Mock, patch
from uuid import uuid4

import requests

from backend import storage
from backend.routes import rooms as routes
from backend.services import recording_requests, rooms
from backend.services.plex_rooms import PMSQueue as RealPMSQueue
from backend.workers import rooms as worker


class RoomHardeningTests(RoomTestCase):
    def clone_closed(self, original, *, closed_at, status="closed"):
        room_id, code = str(uuid4()), str(uuid4())[:10].upper()
        with storage.db() as connection:
            connection.execute(
                "INSERT INTO rooms(id,code,host_user_id,status,created_at,closed_at,server_id,"
                "client_id,session_key,queue_id,current_item_id,handoff_item_id) "
                "SELECT ?,?,host_user_id,?,0,?,server_id,client_id,session_key,queue_id,"
                "current_item_id,handoff_item_id FROM rooms WHERE code=?",
                (room_id, code, status, closed_at, original["code"]),
            )
        return room_id

    def database_dump(self):
        with storage.db() as connection:
            return list(connection.iterdump())

    def test_cleanup_cascades_old_closed_local_rows_and_preserves_global_requests(self):
        room = self.add(self.start(), OTHER)
        self.guest(room)
        room_id = rooms.room_by_code(room["code"])["id"]
        self.choice(room)
        storage.save_recording_acquisition(
            OTHER,
            {"releaseGroupMbid": RELEASE, "title": "Album"},
            "Song",
            self.user["id"],
        )
        storage.record_request(self.user["id"], "release-group", RELEASE, "Album")
        rooms.end(room["code"], self.user["id"])
        with storage.db() as connection:
            connection.execute(
                "UPDATE rooms SET closed_at=0,write_pending=1,write_intent=? WHERE id=?",
                (json.dumps({"remove": ["102"]}), room_id),
            )
            connection.execute(
                "UPDATE room_entries SET add_before='[101,102]',deferred_until='102' WHERE room_id=?",
                (room_id,),
            )
            global_before = {
                table: [
                    tuple(row) for row in connection.execute(f"SELECT * FROM {table}")
                ]
                for table in (
                    "recording_acquisitions",
                    "recording_acquisition_requesters",
                    "request_history",
                )
            }
        self.assertEqual(rooms.cleanup()["rooms"], 1)
        with storage.db() as connection:
            for table in ("rooms", "room_entries", "room_guests", "room_choices"):
                self.assertEqual(
                    connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0
                )
            self.assertEqual(
                connection.execute("PRAGMA foreign_key_check").fetchall(), []
            )
            for table, before in global_before.items():
                self.assertEqual(
                    [
                        tuple(row)
                        for row in connection.execute(f"SELECT * FROM {table}")
                    ],
                    before,
                )
        self.assertEqual(rooms.cleanup(), {"rooms": 0, "choices": 0, "rateLimits": 0})

    def test_cleanup_retains_recent_closed_old_active_and_active_recovery(self):
        room = self.add(self.start(), OTHER)
        recent = self.clone_closed(room, closed_at=2_000_000_000 - 60)
        unknown_closed = self.clone_closed(room, closed_at=None)
        with storage.db() as connection:
            connection.execute(
                "UPDATE rooms SET created_at=0,dirty=1,write_pending=1,write_intent=? WHERE code=?",
                (
                    json.dumps(
                        {
                            "placements": {
                                room["queue"][-1]["id"]: {
                                    "before": "102",
                                    "after": "101",
                                }
                            }
                        }
                    ),
                    room["code"],
                ),
            )
            connection.execute(
                "UPDATE room_entries SET add_before='[101,102]',deferred_until='102' WHERE recording_mbid=?",
                (OTHER,),
            )
        saved_room = rooms.room_by_code(room["code"])
        saved_entries = rooms.entries(saved_room["id"])
        self.assertEqual(rooms.cleanup(now=2_000_000_000)["rooms"], 0)
        self.assertEqual(rooms.room_by_code(room["code"]), saved_room)
        self.assertEqual(rooms.entries(saved_room["id"]), saved_entries)
        with storage.db() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM rooms WHERE id IN (?,?)",
                    (recent, unknown_closed),
                ).fetchone()[0],
                2,
            )

    def test_cleanup_choices_and_rates_are_bounded_and_idempotent(self):
        room = self.start()
        expired = self.choice(room)
        fresh = self.choice(room)
        with storage.db() as connection:
            connection.execute(
                "UPDATE room_choices SET expires_at=10 WHERE id=?", (expired,)
            )
            connection.execute(
                "INSERT INTO room_rate_limits VALUES ('old','search',0,1)"
            )
        result = rooms.cleanup(now=10)
        self.assertEqual(result["choices"], 1)
        self.assertEqual(result["rateLimits"], 0)
        with storage.db() as connection:
            self.assertEqual(
                connection.execute("SELECT id FROM room_choices").fetchone()[0], fresh
            )
        self.assertEqual(rooms.cleanup(now=600)["rateLimits"], 1)
        self.assertEqual(rooms.cleanup(now=600)["choices"], 0)

    def test_cleanup_closed_room_batches_are_bounded(self):
        room = self.start()
        for _ in range(rooms.CLEANUP_ROOM_BATCH_SIZE + 3):
            self.clone_closed(room, closed_at=0)
        self.assertEqual(rooms.cleanup()["rooms"], rooms.CLEANUP_ROOM_BATCH_SIZE)
        self.assertEqual(rooms.cleanup()["rooms"], 3)
        self.assertEqual(rooms.cleanup()["rooms"], 0)
        self.assertEqual(rooms.room_by_code(room["code"])["status"], "active")

    def test_cleanup_expired_choice_batch_limit(self):
        room = self.start()
        with storage.db() as connection:
            room_id = rooms.room_by_code(room["code"])["id"]
            connection.executemany(
                "INSERT INTO room_choices VALUES (?,?,?,?,?,?,?)",
                [
                    (str(i), room_id, RECORDING, "Song", "Artist", RELEASE, 0)
                    for i in range(rooms.CLEANUP_CHOICE_BATCH_SIZE + 2)
                ],
            )
        self.assertEqual(rooms.cleanup()["choices"], rooms.CLEANUP_CHOICE_BATCH_SIZE)
        self.assertEqual(rooms.cleanup()["choices"], 2)

    def test_worker_runs_maintenance_periodically_without_stopping_active_sync(self):
        room = self.start()
        with (
            patch.object(worker, "_next_cleanup_at", 0),
            patch.object(worker.time, "monotonic", return_value=100),
            patch.object(rooms, "cleanup") as cleanup,
            patch.object(rooms, "reconcile") as reconcile,
        ):
            worker.tick()
            worker.tick()
        cleanup.assert_called_once_with()
        self.assertEqual(reconcile.call_count, 2)
        reconcile.assert_called_with(room["code"], initiate=True)

    def test_diagnostics_host_allowed_and_all_other_origins_forbidden(self):
        room = self.start()
        path = f"/api/rooms/{room['code']}/diagnostics"
        guest, _, _ = self.guest(room)
        self.assertEqual(guest.get(path).status_code, 401)
        anonymous = self.app.test_client()
        self.assertEqual(anonymous.get(path).status_code, 401)
        self.assertEqual(
            anonymous.get(
                path, headers={"X-Api-Key": self.app.config["AUTOMATION_API_KEY"]}
            ).status_code,
            401,
        )
        with storage.db() as connection:
            other = connection.execute(
                "INSERT INTO users(username,password_hash,role,created_at) VALUES ('other-host','unused','admin',0)"
            ).lastrowid
        with anonymous.session_transaction() as session:
            session["user_id"] = other
        self.assertEqual(anonymous.get(path).status_code, 403)
        self.assertEqual(self.client.get(path).status_code, 200)

    def test_diagnostics_is_read_only_and_preserves_duplicate_and_null_identities(self):
        room = self.add(self.add(self.start()))
        room = self.add(room, OTHER)
        before = self.database_dump()
        calls = deepcopy(self.pms.calls)
        with (
            patch.object(
                rooms, "reconcile", side_effect=AssertionError("Reconciliation")
            ),
            patch.object(
                rooms, "observe", side_effect=AssertionError("Observation mutation")
            ),
        ):
            response = self.client.get(f"/api/rooms/{room['code']}/diagnostics")
        self.assertEqual(response.status_code, 200)
        state = response.get_json()
        self.assertEqual(self.database_dump(), before)
        self.assertEqual(self.pms.calls, calls)
        self.acquire.assert_not_called()
        duplicates = [
            entry for entry in state["entries"] if entry["recordingMbid"] == RECORDING
        ]
        self.assertEqual(len({entry["id"] for entry in duplicates}), 2)
        self.assertEqual(len({entry["playQueueItemId"] for entry in duplicates}), 2)
        imported = state["entries"][0]
        self.assertIsNone(imported["recordingMbid"])
        self.assertTrue(imported["locked"])
        pending = state["entries"][-1]
        self.assertIsNone(pending["ratingKey"])
        self.assertIsNone(pending["playQueueItemId"])
        self.assertTrue(state["pms"]["reachable"])
        self.assertEqual(state["pms"]["currentItemId"], "101")
        self.assertEqual(state["pms"]["nextItemId"], "102")
        self.assertEqual(
            state["acquisition"][-1]["recordingLifecycleStatus"], "not_requested"
        )

    def test_diagnostics_redacts_credentials_errors_and_unknown_journal_fields(self):
        room = self.add(self.start(), OTHER)
        _, guest, _ = self.guest(room)
        room_id = rooms.room_by_code(room["code"])["id"]
        entry_id = room["queue"][-1]["id"]
        with storage.db() as connection:
            guest_row = dict(
                connection.execute(
                    "SELECT * FROM room_guests WHERE room_id=?", (room_id,)
                ).fetchone()
            )
            intent = {
                "remove": ["102", "provider-secret"],
                "order": ["103"],
                "token": "provider-secret",
                "placements": {
                    entry_id: {
                        "before": "102",
                        "after": "101",
                        "token": "provider-secret",
                    },
                    "secret-uuid": {"before": "101"},
                },
            }
            connection.execute(
                "UPDATE rooms SET write_pending=1,write_intent=?,sync_error='provider-secret',trim_ids=? WHERE id=?",
                (json.dumps(intent), json.dumps(["104", "provider-secret"]), room_id),
            )
            connection.execute(
                "UPDATE room_entries SET error='provider-secret',add_before=? WHERE id=?",
                (json.dumps(["105", "provider-secret"]), entry_id),
            )
        with patch.object(
            self.pms,
            "load",
            side_effect=requests.RequestException(
                "http://private/?X-Plex-Token=provider-secret"
            ),
        ):
            response = self.client.get(f"/api/rooms/{room['code']}/diagnostics")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertFalse(data["pms"]["reachable"])
        self.assertTrue(data["pms"]["error"])
        self.assertEqual(
            data["pendingWrites"]["placements"],
            [{"entryId": entry_id, "before": "102", "after": "101"}],
        )
        self.assertEqual(data["entries"][-1]["addBefore"], ["105"])
        body = response.get_data(as_text=True)
        for secret in (
            CONFIG["token"],
            self.app.config["AUTOMATION_API_KEY"],
            guest_row["token_hash"],
            guest["guest"]["csrfToken"],
            "provider-secret",
            "secret-uuid",
            "http://private",
            "Traceback",
            "password_hash",
        ):
            self.assertNotIn(secret, body)

    def test_diagnostics_survives_bad_journal_json_and_acquisition_failure(self):
        room = self.add(self.start(), OTHER)
        with storage.db() as connection:
            connection.execute(
                "UPDATE rooms SET write_intent='broken',trim_ids='broken' WHERE code=?",
                (room["code"],),
            )
        with patch.object(
            recording_requests,
            "recording_states",
            side_effect=ValueError("provider-secret"),
        ):
            data = self.client.get(f"/api/rooms/{room['code']}/diagnostics").get_json()
        self.assertEqual(data["pendingWrites"]["placements"], [])
        self.assertIsNone(data["acquisition"][-1]["available"])
        self.assertTrue(data["acquisitionError"])
        self.assertNotIn("provider-secret", json.dumps(data))

    def test_guest_get_join_and_sse_project_all_intermediate_states_without_mutation(
        self,
    ):
        room = self.add(self.start(), OTHER)
        guest, _, headers = self.guest(room)
        entry_id = room["queue"][-1]["id"]
        for state in (
            "requested",
            "queued",
            "downloading",
            "waiting_for_plex",
            "waiting_for_queue",
            "ready",
        ):
            with self.subTest(state=state):
                with storage.db() as connection:
                    connection.execute(
                        "UPDATE room_entries SET state=? WHERE id=?", (state, entry_id)
                    )
                saved_room = rooms.room_by_code(room["code"])
                saved_entries = rooms.entries(saved_room["id"])
                host_data = self.client.get(f"/api/rooms/{room['code']}").get_json()[
                    "room"
                ]
                guest_data = guest.get(f"/api/rooms/{room['code']}").get_json()["room"]
                expected = "ready" if state == "ready" else "requested"
                self.assertEqual(host_data["queue"][-1]["state"], state)
                self.assertEqual(guest_data["queue"][-1]["state"], expected)
                for client, projected in ((self.client, state), (guest, expected)):
                    response = client.get(
                        f"/api/rooms/{room['code']}/events", buffered=False
                    )
                    try:
                        event = json.loads(
                            next(iter(response.response)).decode().split("data: ", 1)[1]
                        )
                        self.assertEqual(event["queue"][-1]["state"], projected)
                    finally:
                        response.close()
                stored = rooms.entries(rooms.room_by_code(room["code"])["id"])[-1]
                self.assertEqual(stored["state"], state)
                # HTTP rate buckets are the only ordinary GET/SSE side effects.
                self.assertEqual(
                    rooms.snapshot(room["code"])["queue"][-1]["state"], state
                )
                self.assertEqual(host_data["version"], guest_data["version"])
                diagnostics = self.client.get(
                    f"/api/rooms/{room['code']}/diagnostics"
                ).get_json()
                self.assertEqual(diagnostics["entries"][-1]["state"], state)
                self.assertEqual(rooms.room_by_code(room["code"]), saved_room)
                self.assertEqual(rooms.entries(saved_room["id"]), saved_entries)
        joined = self.post(
            f"/api/rooms/{room['code']}/join", {"name": ""}, guest, headers
        )
        self.assertEqual(joined.get_json()["room"]["queue"][-1]["state"], "ready")

    def test_guest_add_and_search_project_intermediate_states(self):
        room = self.start()
        guest, _, headers = self.guest(room)
        self.states[OTHER] = self.lifecycle("downloading", "501")
        result = self.add(room, OTHER, client=guest, headers=headers)
        self.assertEqual(self.request_queue(result)[0]["state"], "requested")
        self.assertEqual(
            self.request_queue(rooms.snapshot(room["code"]))[0]["state"], "downloading"
        )
        with patch.object(
            routes,
            "_search_response",
            return_value=(
                {
                    "results": [
                        {
                            "recordingMbid": OTHER,
                            "id": RELEASE,
                            "name": "Song",
                            "recordingState": {"status": "downloading"},
                        }
                    ]
                },
                200,
            ),
        ):
            result = guest.get(f"/api/rooms/{room['code']}/search?q=Song").get_json()
        self.assertEqual(result["results"][0]["state"], "requested")

    def test_lifecycle_transitions_require_queue_confirmation(self):
        room = self.add(self.start(), OTHER)
        stored = rooms.room_by_code(room["code"])
        for status in ("queued", "downloading", "waiting_for_plex", "ready"):
            with self.subTest(status=status):
                self.states[OTHER] = self.lifecycle(status, "501")
                rooms.acquisition_bridge(stored)
                row = rooms.entries(stored["id"])[-1]
                self.assertEqual(
                    row["state"], "waiting_for_queue" if status == "ready" else status
                )
                self.assertIsNone(row["queue_item_id"])
        worker.tick()
        row = rooms.entries(stored["id"])[-1]
        self.assertEqual(row["state"], "ready")
        self.assertIn(
            row["queue_item_id"], [track["playQueueItemID"] for track in self.pms.items]
        )

    def test_exact_availability_outranks_stale_lifecycle_status(self):
        room = self.add(self.start(), OTHER)
        for stale in ("not_requested", "requested", "waiting_for_plex"):
            with self.subTest(stale=stale):
                self.states[OTHER] = {**self.lifecycle("ready", "501"), "status": stale}
                states = rooms.acquisition_bridge(
                    rooms.room_by_code(room["code"]), initiate=True
                )
                self.assertEqual(states[OTHER]["status"], "ready")
                self.assertEqual(
                    self.request_queue(rooms.snapshot(room["code"]))[0]["state"],
                    "waiting_for_queue",
                )
        self.acquire.assert_not_called()
        worker.tick()
        self.assertEqual(
            self.request_queue(rooms.snapshot(room["code"]))[0]["state"], "ready"
        )

    def test_ready_waiting_entry_survives_transient_index_gap_without_reacquisition(
        self,
    ):
        room = self.add(self.start(), OTHER)
        self.states[OTHER] = {"status": "ready", "available": True, "tracks": []}
        worker.tick()
        self.states[OTHER] = self.lifecycle("not_requested", "501")
        worker.tick()
        self.assertEqual(
            self.request_queue(rooms.snapshot(room["code"]))[0]["state"],
            "waiting_for_queue",
        )
        self.acquire.assert_not_called()
        self.states[OTHER] = self.lifecycle("ready", "501")
        worker.tick()
        self.assertEqual(
            self.request_queue(rooms.snapshot(room["code"]))[0]["state"], "ready"
        )

    def test_authoritative_ready_remains_automatic_after_locked_boundary_advances(self):
        room = self.add(self.start(), OTHER)
        stored = rooms.room_by_code(room["code"])
        entry_id = self.request_queue(room)[0]["id"]
        with storage.db() as connection:
            connection.execute(
                "UPDATE room_entries SET position=-1 WHERE id=?", (entry_id,)
            )
        self.states[OTHER] = self.lifecycle("ready", "501")
        worker.tick()
        row = rooms.entries(stored["id"])[0]
        self.assertEqual(row["state"], "waiting_for_queue")
        self.assertIsNone(row["queue_item_id"])
        self.pms.items.append(item(900))
        worker.tick()
        self.assertEqual(rooms.snapshot(room["code"])["queue"][-1]["title"], "Song 900")
        self.pms.current = "102"
        worker.tick()
        row = next(row for row in rooms.entries(stored["id"]) if row["id"] == entry_id)
        self.assertEqual(row["state"], "ready")
        self.acquire.assert_not_called()

    def configure_devices(self):
        self.device_rows = []
        self.device_events = {}
        self.device_queues = {}
        for client, key, queue_id, current, name, platform in (
            ("phone", "801", "1001", 11, "Jeremy’s iPhone", "iOS"),
            ("pc", "802", "1002", 21, "Apollo", "Windows"),
        ):
            self.device_rows.append(
                {
                    "type": "track",
                    "sessionKey": key,
                    "ratingKey": str(current),
                    "title": f"Song {current}",
                    "grandparentTitle": "Artist",
                    "User": {"id": 999, "title": "Plex User"},
                    "Player": {
                        "machineIdentifier": client,
                        "title": name,
                        "product": "Plexamp",
                        "platform": platform,
                        "state": "playing",
                        "token": "provider-secret",
                    },
                }
            )
            self.device_events[client] = {
                "clientIdentifier": client,
                "sessionKey": key,
                "playQueueID": queue_id,
                "playQueueItemID": str(current),
                "ratingKey": str(current),
                "state": "playing",
            }
            self.device_queues[queue_id] = {
                "playQueueID": queue_id,
                "playQueueTotalCount": 2,
                "Metadata": [item(current), item(current + 1)],
            }
        adapter = RealPMSQueue(CONFIG)

        def call(method, path, **params):
            self.assertEqual(method, "GET")
            if path == "/status/sessions":
                return {"Metadata": deepcopy(self.device_rows)}
            return deepcopy(self.device_queues[path.rsplit("/", 1)[1]])

        adapter.call = Mock(side_effect=call)
        self.feed.latest.side_effect = lambda session, **kwargs: deepcopy(
            self.device_events.get(session["client_id"])
        )
        self.adapter.return_value = adapter
        return adapter

    def devices(self):
        response = self.client.get("/api/rooms/sessions")
        self.assertEqual(response.status_code, 200, response.get_json())
        return response.get_json()["sessions"]

    def test_zero_and_single_eligible_session_keep_easy_start_workflow(self):
        self.configure_devices()
        self.device_rows.clear()
        self.assertEqual(self.devices(), [])
        response = self.post("/api/rooms")
        self.assertEqual(response.status_code, 502)
        self.assertIn("Start playing", response.get_json()["error"])
        self.assertIsNone(rooms.host_room(self.user["id"]))
        self.configure_devices()
        self.device_rows = self.device_rows[:1]
        room = self.start()
        self.assertEqual(rooms.room_by_code(room["code"])["client_id"], "phone")

    def test_multiple_sessions_require_selection_and_each_device_binds_its_own_queue(
        self,
    ):
        self.configure_devices()
        response = self.post("/api/rooms")
        self.assertEqual(response.status_code, 409)
        self.assertTrue(response.get_json()["selectionRequired"])
        self.assertIsNone(rooms.host_room(self.user["id"]))
        choices = self.devices()
        self.assertEqual(
            [choice["deviceName"] for choice in choices], ["Jeremy’s iPhone", "Apollo"]
        )
        for selected in choices:
            response = self.post("/api/rooms", {"sessionId": selected["id"]})
            self.assertEqual(response.status_code, 201, response.get_json())
            room = response.get_json()["room"]
            stored = rooms.room_by_code(room["code"])
            self.assertEqual(stored["client_id"], selected["clientId"])
            self.assertEqual(stored["session_key"], selected["sessionKey"])
            self.assertEqual(stored["queue_id"], selected["queueId"])
            self.assertEqual(stored["current_item_id"], selected["currentItemId"])
            diagnostics = self.client.get(
                f"/api/rooms/{room['code']}/diagnostics"
            ).get_json()
            self.assertEqual(diagnostics["room"]["deviceName"], selected["deviceName"])
            self.assertEqual(diagnostics["room"]["platform"], selected["platform"])
            self.assertNotIn(CONFIG["token"], json.dumps(diagnostics))
            self.assertNotIn("provider-secret", json.dumps(diagnostics))
            rooms.end(room["code"], self.user["id"])

    def test_stale_selection_never_falls_back_to_other_device(self):
        self.configure_devices()
        phone = self.devices()[0]
        self.device_rows = self.device_rows[1:]
        response = self.post("/api/rooms", {"sessionId": phone["id"]})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["sessions"][0]["clientId"], "pc")
        self.assertIsNone(rooms.host_room(self.user["id"]))

    def test_session_discovery_and_selection_exclude_other_users_video_and_paused(self):
        adapter = self.configure_devices()
        phone = deepcopy(self.device_rows[0])
        for extra in (
            {
                **phone,
                "User": {"title": "Someone else"},
                "Player": {**phone["Player"], "machineIdentifier": "foreign"},
            },
            {**phone, "type": "movie"},
            {**phone, "Player": {**phone["Player"], "state": "paused"}},
        ):
            self.device_rows.append(extra)
        before = self.database_dump()
        choices = self.devices()
        self.assertEqual(self.database_dump(), before)
        self.assertEqual(len(choices), 2)
        foreign_id = adapter._selection_id({"client_id": "foreign"})
        response = self.post("/api/rooms", {"sessionId": foreign_id})
        self.assertEqual(response.status_code, 409)
        self.assertNotIn("foreign", json.dumps(response.get_json()["sessions"]))
        self.assertNotIn("provider-secret", json.dumps(choices))
        self.assertNotIn(CONFIG["token"], json.dumps(choices))

    def test_duplicate_session_observations_are_deduplicated_without_merging_devices(
        self,
    ):
        self.configure_devices()
        self.device_rows.append(deepcopy(self.device_rows[0]))
        self.assertEqual(len(self.devices()), 2)
        self.device_rows = [self.device_rows[0], self.device_rows[2]]
        room = self.start()
        self.assertEqual(rooms.room_by_code(room["code"])["client_id"], "phone")

    def test_selected_device_revalidates_advanced_track_and_rotated_session_key(self):
        self.configure_devices()
        phone = self.devices()[0]
        self.device_rows[0].update(sessionKey="803", ratingKey="13", title="New song")
        self.device_events["phone"].update(
            sessionKey="803", ratingKey="13", playQueueItemID="13"
        )
        self.device_queues["1001"].update(
            playQueueTotalCount=3, Metadata=[item(11), item(13), item(12)]
        )
        response = self.post("/api/rooms", {"sessionId": phone["id"]})
        self.assertEqual(response.status_code, 201, response.get_json())
        stored = rooms.room_by_code(response.get_json()["room"]["code"])
        self.assertEqual(stored["session_key"], "803")
        self.assertEqual(stored["current_item_id"], "13")
        self.assertEqual(stored["next_item_id"], "12")

    def test_selection_is_invalidated_by_server_change(self):
        adapter = self.configure_devices()
        selection = self.devices()[0]["id"]
        adapter.server_id = "replacement-server"
        self.assertEqual(
            self.post("/api/rooms", {"sessionId": selection}).status_code, 409
        )
        self.assertIsNone(rooms.host_room(self.user["id"]))

    def test_bound_device_cannot_jump_to_other_device_or_new_queue(self):
        adapter = self.configure_devices()
        phone = self.devices()[0]
        room = self.post("/api/rooms", {"sessionId": phone["id"]}).get_json()["room"]
        self.device_events["phone"]["playQueueID"] = "new-queue"
        response = self.post(f"/api/rooms/{room['code']}/sync")
        self.assertEqual(response.status_code, 502)
        self.assertEqual(rooms.room_by_code(room["code"])["queue_id"], "1001")
        self.device_rows = self.device_rows[1:]
        worker.tick()
        self.assertEqual(rooms.room_by_code(room["code"])["client_id"], "phone")
        self.assertTrue(
            all(call.args[0] == "GET" for call in adapter.call.call_args_list)
        )

    def test_session_discovery_rejects_guests_anonymous_and_api_keys(self):
        self.configure_devices()
        client = self.app.test_client()
        self.assertEqual(client.get("/api/rooms/sessions").status_code, 401)
        self.assertEqual(
            client.get(
                "/api/rooms/sessions",
                headers={"X-Api-Key": self.app.config["AUTOMATION_API_KEY"]},
            ).status_code,
            401,
        )

    def test_selected_device_without_next_item_creates_no_room(self):
        self.configure_devices()
        phone = self.devices()[0]
        self.device_queues["1001"].update(playQueueTotalCount=1, Metadata=[item(11)])
        response = self.post("/api/rooms", {"sessionId": phone["id"]})
        self.assertEqual(response.status_code, 409)
        self.assertIsNone(rooms.host_room(self.user["id"]))

    def test_selected_device_disappearing_during_startup_never_falls_back(self):
        self.configure_devices()
        phone = self.devices()[0]
        original_latest = self.feed.latest.side_effect

        def disappear(session, **kwargs):
            event = original_latest(session, **kwargs)
            if kwargs.get("wait"):
                self.device_rows = self.device_rows[1:]
            return event

        self.feed.latest.side_effect = disappear
        response = self.post("/api/rooms", {"sessionId": phone["id"]})
        self.assertEqual(response.status_code, 502)
        self.assertIsNone(rooms.host_room(self.user["id"]))

    def test_selected_device_incomplete_queue_creates_no_room(self):
        self.configure_devices()
        phone = self.devices()[0]
        self.device_queues["1001"]["playQueueTotalCount"] = 3
        response = self.post("/api/rooms", {"sessionId": phone["id"]})
        self.assertEqual(response.status_code, 502)
        self.assertIsNone(rooms.host_room(self.user["id"]))

    def test_sse_updates_project_guest_state_and_preserve_host_detail(self):
        room = self.add(self.start(), OTHER)
        guest, _, _ = self.guest(room)
        entry_id = self.request_queue(room)[0]["id"]
        for client, expected in (
            (self.client, "waiting_for_queue"),
            (guest, "requested"),
        ):
            with storage.db() as connection:
                connection.execute(
                    "UPDATE room_entries SET state='waiting_for_plex' WHERE id=?",
                    (entry_id,),
                )
                rooms.bump(connection, rooms.room_by_code(room["code"])["id"])
            response = client.get(f"/api/rooms/{room['code']}/events", buffered=False)
            try:
                iterator = iter(response.response)
                next(iterator)
                with storage.db() as connection:
                    connection.execute(
                        "UPDATE room_entries SET state='waiting_for_queue' WHERE id=?",
                        (entry_id,),
                    )
                    rooms.bump(connection, rooms.room_by_code(room["code"])["id"])
                self.assertIn(b": heartbeat", next(iterator))
                with patch.object(routes.time, "sleep"):
                    updated = json.loads(next(iterator).decode().split("data: ", 1)[1])
                self.assertEqual(updated["queue"][-1]["state"], expected)
                self.assertEqual(
                    rooms.snapshot(room["code"])["queue"][-1]["state"],
                    "waiting_for_queue",
                )
            finally:
                response.close()
