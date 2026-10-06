"""Room bootstrap secrets, short-code reuse and migration boundaries."""

# isort: skip_file
from ._test_environment import TEST_ROOT  # noqa: F401
from .test_rooms import RoomTestCase

import json
import logging
import runpy
import sqlite3
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from backend import room_storage, storage
from backend.logging_filters import RoomInviteFilter
from backend.routes import rooms as routes
from backend.services import rooms


class RoomInvitationTests(RoomTestCase):
    def test_request_response_cannot_switch_to_a_reused_room_code(self):
        room = self.start()
        saved_id = rooms.room_by_code(room["code"])["id"]
        choice = self.choice(room)
        original_snapshot = rooms.snapshot

        def reuse_before_response(code, **kwargs):
            with storage.db() as connection:
                connection.execute(
                    "UPDATE rooms SET status='closed',closed_at=0 WHERE id=?",
                    (saved_id,),
                )
            self.reserve_code(code)
            return original_snapshot(code, **kwargs)

        with patch.object(rooms, "snapshot", side_effect=reuse_before_response):
            response = rooms.add(room["code"], choice, "Host", room_id=saved_id)
        self.assertEqual(response["status"], "closed")
        self.assertNotEqual(rooms.room_by_code(room["code"])["id"], saved_id)
        self.assertEqual(original_snapshot(room["code"])["status"], "active")

    def test_stale_authorization_cannot_join_or_add_to_reused_code(self):
        room = self.start()
        saved_id = rooms.room_by_code(room["code"])["id"]
        rooms.end(room["code"], self.user["id"])
        self.reserve_code(room["code"])
        with self.assertRaises(rooms.RoomError):
            rooms.join(room["code"], host=True, room_id=saved_id)
        with self.assertRaises(rooms.RoomError):
            rooms.add(room["code"], "choice", "Host", room_id=saved_id)

    def anonymous_join(self, room, invite=None, client=None):
        payload = {} if invite is None else {"invite": invite}
        return self.post(
            f"/api/rooms/{room['code']}/join",
            payload,
            client or self.app.test_client(),
            {"X-Room-Request": "1"},
        )

    def reserve_code(self, code, status="active"):
        with storage.db() as connection:
            other = connection.execute(
                "INSERT INTO users(username,password_hash,role,created_at) VALUES (?,'unused','user',0)",
                (str(uuid4()),),
            ).lastrowid
            identity = str(uuid4())
            connection.execute(
                "INSERT INTO rooms(id,code,host_user_id,status,created_at,closed_at,server_id,client_id,session_key,queue_id,current_item_id,handoff_item_id) "
                "VALUES (?,?,?,?,0,0,'server','other','other',?,'1','2')",
                (identity, code, other, status, identity),
            )
        return identity

    def test_new_code_is_four_uppercase_unambiguous_characters(self):
        room = self.start()
        self.assertEqual(len(room["code"]), 4)
        self.assertTrue(set(room["code"]) <= set(rooms.CODE_ALPHABET))
        self.assertEqual(room["code"], room["code"].upper())
        self.assertEqual(rooms.CODE_ALPHABET, "23456789ABCDEFGHJKLMNPQRSTUVWXYZ")

    def test_active_collision_retries_without_overwriting(self):
        old_id = self.reserve_code("A7K3")
        with patch.object(
            rooms.secrets, "choice", side_effect=list("A7K3M42P")
        ) as choice:
            room = self.start()
        self.assertEqual(room["code"], "M42P")
        self.assertEqual(choice.call_count, 8)
        self.assertEqual(rooms.room_by_code("A7K3")["id"], old_id)

    def test_collision_retries_are_bounded_and_fail_cleanly(self):
        old_id = self.reserve_code("AAAA")
        with patch.object(rooms.secrets, "choice", return_value="A") as choice:
            response = self.post("/api/rooms")
        self.assertEqual(response.status_code, 503)
        self.assertIn("allocate a Room code", response.get_json()["error"])
        self.assertEqual(choice.call_count, rooms.CODE_ATTEMPTS * 4)
        with storage.db() as connection:
            self.assertEqual(
                connection.execute("SELECT id FROM rooms").fetchall()[0][0], old_id
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM rooms").fetchone()[0], 1
            )
        self.assertFalse(
            any(call[0] in {"add", "move", "remove"} for call in self.pms.calls)
        )

    def test_retained_closed_code_can_be_reused_and_lookup_prefers_active(self):
        old_id = self.reserve_code("A7K3", "closed")
        with patch.object(rooms.secrets, "choice", side_effect=list("A7K3")):
            room = self.start()
        self.assertEqual(room["code"], "A7K3")
        self.assertNotEqual(rooms.room_by_code("A7K3")["id"], old_id)
        self.assertEqual(rooms.snapshot("A7K3", room_id=old_id)["status"], "closed")

    def test_invite_is_separate_random_capability_with_no_raw_database_secret(self):
        room = self.start()
        token = self.invite_token(room)
        saved = rooms.room_by_code(room["code"])
        self.assertEqual(len(token), 43)
        self.assertEqual(len(saved["invite_nonce"]), 64)
        self.assertEqual(saved["invite_hash"], sha256(token.encode()).hexdigest())
        self.assertNotIn(token, json.dumps(saved))
        self.assertNotIn(token, json.dumps(room))
        self.assertNotEqual(token, room["code"])

    def test_code_alone_cannot_join_or_read_room(self):
        room = self.start()
        client = self.app.test_client()
        self.assertEqual(self.anonymous_join(room, client=client).status_code, 403)
        self.assertEqual(client.get(f"/api/rooms/{room['code']}").status_code, 403)
        self.assertEqual(
            client.get(f"/api/rooms/{room['code']}/events").status_code, 403
        )
        with storage.db() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM room_guests").fetchone()[0], 0
            )

    def test_valid_invite_joins_with_original_guest_cookie_and_csrf(self):
        room = self.start()
        client = self.app.test_client()
        joined = self.anonymous_join(room, self.invite_token(room), client)
        self.assertEqual(joined.status_code, 200)
        self.assertIn("HttpOnly", joined.headers["Set-Cookie"])
        self.assertIn("SameSite=Lax", joined.headers["Set-Cookie"])
        first = joined.get_json()["guest"]
        repeated = self.anonymous_join(room, client=client)
        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(repeated.get_json()["guest"], first)
        self.assertEqual(client.get(f"/api/rooms/{room['code']}").status_code, 200)
        self.add(
            room,
            client=client,
            headers={"X-Room-Request": "1", "X-Room-CSRF": first["csrfToken"]},
        )

    def test_invalid_invite_values_fail_without_echoing_secrets(self):
        room = self.start()
        token = self.invite_token(room)
        for value in [token[:-1], "wrong" * 9, "x" * 10000, 42, {"invite": token}]:
            with self.subTest(value_type=type(value)):
                response = self.anonymous_join(room, value)
                self.assertEqual(response.status_code, 403)
                self.assertNotIn(token, response.get_data(as_text=True))

    def test_authenticated_owner_can_join_without_invite(self):
        room = self.start()
        response = self.anonymous_join(room, client=self.client)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get(f"/api/rooms/{room['code']}").status_code, 200)

    def test_other_authenticated_user_cannot_bypass_invite(self):
        room = self.start()
        self.reserve_code("9HRD")
        with storage.db() as connection:
            other_id = connection.execute(
                "SELECT id FROM users WHERE id!=?", (self.user["id"],)
            ).fetchone()[0]
        client = self.app.test_client()
        with client.session_transaction() as browser:
            browser["user_id"] = other_id
        self.assertEqual(self.anonymous_join(room, client=client).status_code, 403)

    def test_invite_endpoint_is_owner_only_and_uncached(self):
        room = self.start()
        guest, _, _ = self.guest(room)
        path = f"/api/rooms/{room['code']}/invite"
        self.assertEqual(self.app.test_client().get(path).status_code, 401)
        self.assertEqual(guest.get(path).status_code, 401)
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
        self.assertTrue(
            response.get_json()["invitePath"].startswith(
                f"/rooms/{room['code']}?invite="
            )
        )

    def test_guest_json_sse_diagnostics_and_host_snapshot_exclude_invite_material(self):
        room = self.start()
        token = self.invite_token(room)
        saved = rooms.room_by_code(room["code"])
        client, joined, _ = self.guest(room)
        documents = [
            json.dumps(joined),
            json.dumps(room),
            json.dumps(rooms.host_room(self.user["id"])),
        ]
        documents.append(
            client.get(f"/api/rooms/{room['code']}").get_data(as_text=True)
        )
        documents.append(
            self.client.get(f"/api/rooms/{room['code']}/diagnostics").get_data(
                as_text=True
            )
        )
        response = client.get(f"/api/rooms/{room['code']}/events", buffered=False)
        documents.append(next(iter(response.response)).decode())
        response.close()
        for document in documents:
            for secret in (
                token,
                saved["invite_hash"],
                saved["invite_nonce"],
                "invitePath",
                "?invite=",
            ):
                self.assertNotIn(secret, document)

    def test_invite_identity_survives_init_db_and_sharing_again(self):
        room = self.start()
        first = self.invite_token(room)
        storage.init_db()
        self.assertEqual(self.invite_token(room), first)
        self.assertEqual(self.anonymous_join(room, first).status_code, 200)

    def test_legacy_active_room_keeps_code_bindings_and_joined_guest(self):
        room = self.start()
        guest, _, _ = self.guest(room)
        saved = rooms.room_by_code(room["code"])
        with storage.db() as connection:
            connection.execute(
                "UPDATE rooms SET code='ABCDEFGHJK',invite_nonce=NULL,invite_hash=NULL WHERE id=?",
                (saved["id"],),
            )
        legacy = {**room, "code": "ABCDEFGHJK"}
        # Existing credential predates the invite bootstrap; cookie path was the
        # former code only because this fixture simulates a legacy DB in place.
        cookie = guest.get_cookie("room_guest", path=f"/api/rooms/{room['code']}")
        guest.set_cookie("room_guest", cookie.value, path="/api/rooms/ABCDEFGHJK")
        storage.init_db()
        current = rooms.room_by_code("abcdefghjk")
        for field in (
            "id",
            "queue_id",
            "client_id",
            "current_item_id",
            "next_item_id",
            "write_intent",
        ):
            self.assertEqual(current[field], saved[field])
        self.assertEqual(guest.get("/api/rooms/ABCDEFGHJK").status_code, 200)
        self.assertEqual(self.anonymous_join(legacy).status_code, 403)
        token = self.invite_token(legacy)
        storage.init_db()
        self.assertEqual(self.invite_token(legacy), token)
        self.assertEqual(self.anonymous_join(legacy, token).status_code, 200)

    def test_old_guest_cannot_access_reused_code_and_stream_stays_on_original_uuid(
        self,
    ):
        room = self.start()
        token = self.invite_token(room)
        guest, _, _ = self.guest(room)
        stream = guest.get(f"/api/rooms/{room['code']}/events", buffered=False)
        iterator = iter(stream.response)
        next(iterator)
        old_id = rooms.room_by_code(room["code"])["id"]
        self.post(f"/api/rooms/{room['code']}/end")
        with patch.object(rooms.secrets, "choice", side_effect=list(room["code"])):
            new = self.start()
        self.assertNotEqual(rooms.room_by_code(new["code"])["id"], old_id)
        self.assertEqual(guest.get(f"/api/rooms/{new['code']}").status_code, 403)
        self.assertEqual(self.anonymous_join(new, token, guest).status_code, 403)
        with patch.object(routes.time, "sleep"):
            messages = b"".join(iterator).decode()
        stream.close()
        self.assertIn('"status": "closed"', messages)
        self.assertNotIn(f'"createdAt": {new["createdAt"]}', messages)

    def test_replacing_server_key_fails_sharing_without_rotating_identity(self):
        room = self.start()
        first = rooms.room_by_code(room["code"])
        with patch(
            "backend.services.room_invites.load_session_secret",
            return_value="other-key",
        ):
            response = self.client.get(f"/api/rooms/{room['code']}/invite")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            rooms.room_by_code(room["code"])["invite_hash"], first["invite_hash"]
        )


class RoomCodeMigrationTests(RoomTestCase):
    def test_failed_parent_rebuild_rolls_back_and_restores_foreign_keys(self):
        class FailingConnection(sqlite3.Connection):
            def execute(self, sql, *args):
                if sql.startswith("ALTER TABLE rooms_code_migration"):
                    raise sqlite3.OperationalError("simulated migration interruption")
                return super().execute(sql, *args)

        with sqlite3.connect(":memory:", factory=FailingConnection) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(
                "CREATE TABLE rooms(id TEXT PRIMARY KEY,code TEXT NOT NULL UNIQUE,status TEXT)"
            )
            connection.execute(
                "CREATE TABLE guests(id TEXT PRIMARY KEY,room_id TEXT REFERENCES rooms(id) ON DELETE CASCADE)"
            )
            connection.execute(
                "INSERT INTO rooms VALUES ('original','ABCDEFGHJK','active')"
            )
            connection.execute("INSERT INTO guests VALUES ('guest','original')")
            connection.commit()
            with self.assertRaises(sqlite3.OperationalError):
                room_storage.prepare_code_migration(connection)
            self.assertEqual(
                connection.execute("SELECT * FROM rooms").fetchall(),
                [("original", "ABCDEFGHJK", "active")],
            )
            self.assertEqual(
                connection.execute("SELECT * FROM guests").fetchall(),
                [("guest", "original")],
            )
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(
                connection.execute("PRAGMA foreign_key_check").fetchall(), []
            )

    def test_legacy_global_unique_schema_rebuild_preserves_children_and_is_idempotent(
        self,
    ):
        room = self.add(self.start())
        self.guest(room)
        with storage.db() as connection:
            # Recreate the old parent definition in an isolated copy, never the
            # application's DB: simulate the real pre-upgrade global constraint.
            legacy = sqlite3.connect(":memory:")
            connection.backup(legacy)
        legacy.execute("DROP INDEX rooms_active_code")
        legacy.execute("PRAGMA foreign_keys=OFF")
        schema = legacy.execute(
            "SELECT sql FROM sqlite_master WHERE name='rooms'"
        ).fetchone()[0]
        legacy.execute(
            schema.replace('"rooms"', '"rooms_old"', 1)
            .replace("rooms (", "rooms_old (", 1)
            .replace("code TEXT NOT NULL", "code TEXT NOT NULL UNIQUE", 1)
        )
        legacy.execute("INSERT INTO rooms_old SELECT * FROM rooms")
        legacy.execute("DROP TABLE rooms")
        legacy.execute("ALTER TABLE rooms_old RENAME TO rooms")
        legacy.commit()
        legacy.execute("PRAGMA foreign_keys=ON")
        before = {
            name: legacy.execute(f"SELECT * FROM {name}").fetchall()
            for name in ("rooms", "room_guests", "room_entries", "room_choices")
        }
        room_storage.prepare_code_migration(legacy)
        room_storage.migrate(legacy)
        legacy.commit()
        room_storage.prepare_code_migration(legacy)
        self.assertEqual(legacy.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(legacy.execute("PRAGMA foreign_key_check").fetchall(), [])
        for name, rows in before.items():
            self.assertEqual(legacy.execute(f"SELECT * FROM {name}").fetchall(), rows)
        self.assertEqual(
            legacy.execute("PRAGMA foreign_key_list(room_guests)").fetchone()[2],
            "rooms",
        )
        self.assertIn(
            "WHERE status='active'",
            legacy.execute(
                "SELECT sql FROM sqlite_master WHERE name='rooms_active_code'"
            ).fetchone()[0],
        )
        legacy.close()

    def test_http_logs_redact_invite_and_production_logs_exclude_queries(self):
        token = "secret-invite"
        for url in (f"/rooms/A7K3?invite={token}", f"/rooms/A7K3?%69nvite={token}"):
            record = logging.LogRecord(
                "werkzeug", logging.INFO, "", 0, '"GET %s HTTP/1.1" 200', (url,), None
            )
            RoomInviteFilter().filter(record)
            self.assertNotIn(token, record.getMessage())
        config = runpy.run_path(
            str(Path(__file__).parents[1] / "backend/gunicorn.conf.py")
        )
        for field in ("%(r)s", "%(q)s", "%({referer}i)s"):
            self.assertNotIn(field, config["access_log_format"])
