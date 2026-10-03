"""Durable room intent and one serialized, recoverable PMS reconciler.

The existing recording lifecycle is the acquisition bridge. Guest acquisitions
are sponsored by the host; guests never call authenticated music APIs directly.
"""

import json
import logging
import secrets
import sqlite3
import time
from hashlib import sha256
from uuid import UUID, uuid4

if __package__ == "backend.services":
    from .. import storage, track_search_index
    from ..request_locks import request_lock
    from . import plex_rooms, recording_requests
else:
    import storage
    import track_search_index
    from request_locks import request_lock
    from services import plex_rooms, recording_requests


logger = logging.getLogger(__name__)
CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
MAX_ENTRIES = 200
CLOSED_ROOM_RETENTION_SECONDS = 7 * 24 * 60 * 60
CHOICE_TTL_SECONDS = 60 * 60
CLEANUP_ROOM_BATCH_SIZE = 25
CLEANUP_CHOICE_BATCH_SIZE = 500
CLEANUP_RATE_BATCH_SIZE = 500
RATE_LIMIT_RETENTION_WINDOWS = 2


class RoomError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def room_by_code(code, *, active=False):
    with storage.db() as connection:
        row = connection.execute(
            "SELECT * FROM rooms WHERE code=?", (code.upper(),)
        ).fetchone()
    if not row:
        raise RoomError("Room not found or unavailable.", 404)
    if active and row["status"] != "active":
        raise RoomError("This Room has ended.", 410)
    return dict(row)


def entries(room_id):
    with storage.db() as connection:
        return [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM room_entries WHERE room_id=? ORDER BY position,id",
                (room_id,),
            )
        ]


def host_room(user_id):
    with storage.db() as connection:
        row = connection.execute(
            "SELECT code FROM rooms WHERE host_user_id=? AND status='active'",
            (user_id,),
        ).fetchone()
    return snapshot(row["code"]) if row else None


def snapshot(code):
    # One consistent read transaction: revision and queue always describe the
    # same committed room state, even during concurrent guest requests.
    with storage.db() as connection:
        connection.execute("BEGIN")
        room = connection.execute(
            "SELECT * FROM rooms WHERE code=?", (code.upper(),)
        ).fetchone()
        if not room:
            raise RoomError("Room not found or unavailable.", 404)
        rows = connection.execute(
            "SELECT * FROM room_entries WHERE room_id=? AND removed=0 AND playback='upcoming' ORDER BY position,id",
            (room["id"],),
        ).fetchall()
        rows = sorted(
            rows, key=lambda row: row["queue_item_id"] != room["next_item_id"]
        )
        guest_count = connection.execute(
            "SELECT COUNT(*) FROM room_guests WHERE room_id=?", (room["id"],)
        ).fetchone()[0]
        return {
            "code": room["code"],
            "status": room["status"],
            "version": room["version"],
            "createdAt": room["created_at"],
            "closedAt": room["closed_at"],
            "joinPath": f"/rooms/{room['code']}",
            "guestCount": guest_count,
            "nowPlaying": json.loads(room["now_playing"]),
            "handoff": json.loads(room["handoff"]),
            "upNext": json.loads(room["up_next"]),
            "playbackState": room["playback_state"],
            "queueWarning": bool(room["warning"]),
            "syncError": room["sync_error"],
            "queue": [
                {
                    "id": row["id"],
                    "recordingMbid": row["recording_mbid"],
                    "title": row["title"],
                    "artist": row["artist"],
                    "album": row["album"],
                    "requester": row["requester"],
                    "state": row["state"],
                    "locked": row["queue_item_id"] == room["next_item_id"],
                    "error": row["error"],
                    "artwork": f"/api/rooms/{room['code']}/artwork/{row['release_group_mbid']}"
                    if row["release_group_mbid"]
                    else "",
                }
                for row in rows
            ],
        }


def bump(connection, room_id):
    connection.execute("UPDATE rooms SET version=version+1 WHERE id=?", (room_id,))


def project_snapshot(state, *, host):
    """Guest presentation never changes the authoritative snapshot or database."""
    if host:
        return state
    return {
        **state,
        "queue": [
            {**entry, "state": "ready" if entry["state"] == "ready" else "requested"}
            for entry in state["queue"]
        ],
    }


def _cleanup_choices(connection, now):
    return connection.execute(
        "DELETE FROM room_choices WHERE id IN (SELECT id FROM room_choices "
        "WHERE expires_at<=? ORDER BY expires_at,id LIMIT ?)",
        (now, CLEANUP_CHOICE_BATCH_SIZE),
    ).rowcount


def cleanup(*, now=None):
    """Bounded Room-local retention; active Rooms and global requests survive."""
    now = time.time() if now is None else now
    with storage.db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        closed = connection.execute(
            "DELETE FROM rooms WHERE id IN (SELECT id FROM rooms "
            "WHERE status='closed' AND closed_at<? ORDER BY closed_at,id LIMIT ?)",
            (now - CLOSED_ROOM_RETENTION_SECONDS, CLEANUP_ROOM_BATCH_SIZE),
        ).rowcount
        choices = _cleanup_choices(connection, now)
        rates = connection.execute(
            "DELETE FROM room_rate_limits WHERE rowid IN (SELECT rowid FROM room_rate_limits "
            "WHERE window<? ORDER BY window LIMIT ?)",
            (int(now // 60) - RATE_LIMIT_RETENTION_WINDOWS, CLEANUP_RATE_BATCH_SIZE),
        ).rowcount
    return {"rooms": closed, "choices": choices, "rateLimits": rates}


def queue_lock(room):
    return request_lock(
        "room-queue", f"{room['server_id']}:{room['queue_id']}", timeout=35
    )


def start(user, *, session_id=None):
    with request_lock("room-host", user["id"]):
        if host_room(user["id"]):
            raise RoomError("You already have an active Room.", 409)
        adapter = plex_rooms.PMSQueue(storage.get_service("plex"))
        discovered = (
            adapter.discover(user)
            if session_id is None
            else adapter.discover(user, session_id=session_id)
        )
        lock_identity = {"server_id": adapter.server_id, **discovered}
        with queue_lock(lock_identity):
            # Revalidate the chosen device under the queue lock. Never switch
            # to another client if playback disappeared or its queue changed.
            event, session = playback_event(
                {**lock_identity, "host_user_id": user["id"]}, adapter
            )
            discovered.update(**session, current_item_id=str(event["playQueueItemID"]))
            queue = adapter.load(discovered["queue_id"])
            items = queue.get("Metadata", [])
            index = next(
                (
                    i
                    for i, item in enumerate(items)
                    if str(item["playQueueItemID"]) == discovered["current_item_id"]
                ),
                None,
            )
            if index is None or index + 1 >= len(items):
                raise RoomError(
                    "Add at least one more song to Plexamp's Up Next, then retry starting your Room.",
                    409,
                )
            if str(items[index].get("ratingKey") or "") != session["rating_key"]:
                raise RoomError(
                    "Plexamp advanced during startup. Retry starting the Room.", 409
                )
            room_id = str(uuid4())
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(10))
            try:
                with storage.db() as connection:
                    connection.execute(
                        "INSERT INTO rooms(id,code,host_user_id,status,created_at,server_id,client_id,session_key,queue_id,"
                        "current_item_id,handoff_item_id,trim_ids,now_playing,handoff,device_name,device_product,device_platform) "
                        "VALUES (?,?,?,'active',?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            room_id,
                            code,
                            user["id"],
                            time.time(),
                            adapter.server_id,
                            discovered["client_id"],
                            discovered["session_key"],
                            discovered["queue_id"],
                            discovered["current_item_id"],
                            str(items[index + 1]["playQueueItemID"]),
                            "[]",
                            json.dumps(plex_rooms.public_track(items[index])),
                            json.dumps(plex_rooms.public_track(items[index + 1])),
                            discovered.get("device_name", ""),
                            discovered.get("product", ""),
                            discovered.get("platform", ""),
                        ),
                    )
            except sqlite3.IntegrityError:
                raise RoomError(
                    "This host or Plex queue already has an active Room. Reload Rooms.",
                    409,
                ) from None
            sync_locked(room_by_code(code), adapter)
            return snapshot(code)


def join(code, name="", token=None):
    room = room_by_code(code, active=True)
    with queue_lock(room):
        room = room_by_code(code, active=True)
        with storage.db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM room_guests WHERE room_id=? AND token_hash=?",
                (room["id"], sha256((token or "").encode()).hexdigest()),
            ).fetchone()
            if existing:
                return {
                    "id": existing["id"],
                    "name": existing["name"],
                    "csrfToken": existing["csrf_token"],
                }, token
            count = connection.execute(
                "SELECT COUNT(*) FROM room_guests WHERE room_id=?", (room["id"],)
            ).fetchone()[0]
            if count >= 500:
                raise RoomError("This Room has reached its guest limit.", 429)
            name = name.strip() or f"Guest #{count + 1}"
            token, csrf, guest_id = (
                secrets.token_urlsafe(32),
                secrets.token_urlsafe(32),
                str(uuid4()),
            )
            connection.execute(
                "INSERT INTO room_guests VALUES (?,?,?,?,?,?)",
                (
                    guest_id,
                    room["id"],
                    sha256(token.encode()).hexdigest(),
                    csrf,
                    name,
                    time.time(),
                ),
            )
            bump(connection, room["id"])
    return {"id": guest_id, "name": name, "csrfToken": csrf}, token


def guest(room, token):
    if not token or len(token) > 100:
        return None
    with storage.db() as connection:
        row = connection.execute(
            "SELECT * FROM room_guests WHERE room_id=? AND token_hash=?",
            (room["id"], sha256(token.encode()).hexdigest()),
        ).fetchone()
    return dict(row) if row else None


def save_choices(room, results):
    safe = []
    with storage.db() as connection:
        _cleanup_choices(connection, time.time())
        for result in results[:25]:
            try:
                mbid = str(UUID(str(result.get("recordingMbid"))))
                release = str(UUID(str(result.get("id"))))
            except (ValueError, TypeError, AttributeError):
                continue
            choice_id = secrets.token_urlsafe(18)
            title = str(
                result.get("matchedTrack") or result.get("name") or "Unknown track"
            )[:300]
            artist = str(
                result.get("matchedTrackArtist") or result.get("artist") or ""
            )[:300]
            connection.execute(
                "INSERT INTO room_choices VALUES (?,?,?,?,?,?,?)",
                (
                    choice_id,
                    room["id"],
                    mbid,
                    title,
                    artist,
                    release,
                    time.time() + CHOICE_TTL_SECONDS,
                ),
            )
            state = (result.get("recordingState") or {}).get("status", "not_requested")
            safe.append(
                {
                    "id": choice_id,
                    "title": title,
                    "artist": artist,
                    "album": str(result.get("name") or "")[:300],
                    "state": state,
                    "artwork": f"/api/rooms/{room['code']}/artwork/{release}",
                }
            )
    return safe


def add(code, choice_id, requester, guest_id=None):
    room = room_by_code(code, active=True)
    with queue_lock(room):
        room = room_by_code(code, active=True)
        with storage.db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            choice = connection.execute(
                "SELECT * FROM room_choices WHERE id=? AND room_id=? AND expires_at>?",
                (choice_id, room["id"], time.time()),
            ).fetchone()
            if not choice:
                raise RoomError(
                    "Choose a track from this Room's recent search results."
                )
            count = connection.execute(
                "SELECT COUNT(*) FROM room_entries WHERE room_id=? AND playback='upcoming' AND removed=0",
                (room["id"],),
            ).fetchone()[0]
            if count >= MAX_ENTRIES:
                raise RoomError(
                    "The Room queue is full. Ask the host to remove an upcoming entry.",
                    409,
                )
            position = connection.execute(
                "SELECT COALESCE(MAX(position),0)+1 FROM room_entries WHERE room_id=?",
                (room["id"],),
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO room_entries(id,room_id,position,recording_mbid,title,artist,release_group_mbid,requester,guest_id,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    str(uuid4()),
                    room["id"],
                    position,
                    choice["recording_mbid"],
                    choice["title"],
                    choice["artist"],
                    choice["release_group_mbid"],
                    requester,
                    guest_id,
                    time.time(),
                ),
            )
            connection.execute("UPDATE rooms SET dirty=1 WHERE id=?", (room["id"],))
            bump(connection, room["id"])
        sync_locked(room_by_code(code))
    return snapshot(code)


def edit(code, user_id, *, order=None, remove_id=None, version=None):
    room = room_by_code(code, active=True)
    if room["host_user_id"] != user_id:
        raise RoomError("Only this Room's host can manage its queue.", 403)
    with queue_lock(room):
        room = room_by_code(code, active=True)
        if version != room["version"]:
            raise RoomError("The Room changed. Reload its queue and retry.", 409)
        adapter = plex_rooms.PMSQueue(storage.get_service("plex"))
        # Observe fresh playback before deciding which entries are editable.
        try:
            queue = adapter.load(room["queue_id"])
            observe(room, adapter, queue)
            _recover_adds(room, queue)
            ids, index, _current = observe(room, adapter, queue)
            intent = _write_intent(room, ids, index)
            _adopt(room, queue, ids, index, intent)
            active = _active_entries(room)
            locked = _logical_boundary(room, active)
        except plex_rooms.QueueError as exc:
            _sync_error(room, exc)
        upcoming = {row["id"] for row in active}
        if order is not None and (
            len(order) != len(set(order)) or set(order) != upcoming
        ):
            raise RoomError("Reorder exactly the upcoming Room entry IDs.", 409)
        if remove_id is not None and remove_id not in upcoming:
            raise RoomError("Only an upcoming entry in this Room can be removed.", 409)
        if locked and (
            remove_id == locked["id"] or order is not None and order[0] != locked["id"]
        ):
            raise RoomError("Up Next is locked. Room edits must stay after it.", 409)
        with storage.db() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if order is not None:
                for index, identity in enumerate(order):
                    connection.execute(
                        "UPDATE room_entries SET position=? WHERE room_id=? AND id=?",
                        (index, room["id"], identity),
                    )
                by_id = {row["id"]: row for row in active}
                intent["order"] = [
                    by_id[identity]["queue_item_id"]
                    for identity in order
                    if by_id[identity]["queue_item_id"]
                    and by_id[identity]["queue_item_id"] != room["next_item_id"]
                ]
            if remove_id is not None:
                connection.execute(
                    "UPDATE room_entries SET removed=1 WHERE room_id=? AND id=?",
                    (room["id"], remove_id),
                )
                target = next(row for row in active if row["id"] == remove_id)
                if target["queue_item_id"]:
                    intent.setdefault("remove", []).append(target["queue_item_id"])
                else:
                    intent.get("placements", {}).pop(remove_id, None)
                    connection.execute(
                        "UPDATE room_entries SET add_before=NULL WHERE id=?",
                        (remove_id,),
                    )
            connection.execute(
                "UPDATE rooms SET dirty=1,write_pending=?,write_intent=? WHERE id=?",
                (bool(intent), json.dumps(intent), room["id"]),
            )
            bump(connection, room["id"])
        sync_locked(room_by_code(code), adapter)
    return snapshot(code)


def end(code, user_id):
    room = room_by_code(code)
    if room["host_user_id"] != user_id:
        raise RoomError("Only this Room's host can end it.", 403)
    with queue_lock(room), storage.db() as connection:
        connection.execute(
            "UPDATE rooms SET status='closed',closed_at=?,version=version+1 WHERE id=? AND status='active'",
            (time.time(), room["id"]),
        )
    return snapshot(code)


def playback_event(room, adapter):
    """Bind notifications to the host's current stream on the original device.

    PMS sessionKey identifies a stream, not the lifetime of a Room. A finished
    stream can still have cached notifications after the next track starts.
    Re-resolve ownership/device via PMS before trusting any notification.
    """
    with storage.db() as connection:
        host = connection.execute(
            "SELECT * FROM users WHERE id=?", (room["host_user_id"],)
        ).fetchone()
    session = adapter.active_session(
        host, client_id=room["client_id"], allow_paused=True
    )
    event = plex_rooms.feed(adapter.config).latest(session)
    if not event:
        raise plex_rooms.QueueError(
            "PMS playback notifications are unavailable. Keep Plexamp playing and retry shortly."
        )
    if str(event.get("playQueueID")) != room["queue_id"]:
        raise plex_rooms.QueueError(
            "Plexamp switched queues. End this Room and start another while playback is active."
        )
    if (
        not plex_rooms.matches(event, session)
        or str(event.get("ratingKey") or "") != session["rating_key"]
    ):
        raise plex_rooms.QueueError(
            "Plexamp playback changed while observing its queue. Retry shortly."
        )
    if event.get("state") not in {"playing", "paused"}:
        raise plex_rooms.QueueError(
            "Plexamp playback is no longer active. Resume it on the Room's original device and retry."
        )
    return event, session


def observe(room, adapter, queue):
    if adapter.server_id != room["server_id"]:
        raise plex_rooms.QueueError(
            "The configured Plex server changed. End this Room before starting another."
        )
    event, session = playback_event(room, adapter)
    current = str(event["playQueueItemID"])
    items = queue.get("Metadata", [])
    ids = [str(item["playQueueItemID"]) for item in items]
    if len(ids) != len(set(ids)) or str(queue.get("playQueueID")) != room["queue_id"]:
        raise plex_rooms.QueueError(
            "Plex returned an ambiguous or different queue. Retry shortly."
        )
    if current not in ids:
        raise plex_rooms.QueueError(
            "The playing item could not be found. Keep Plexamp playback active and retry."
        )
    index = ids.index(current)
    if str(items[index].get("ratingKey") or "") != session["rating_key"]:
        raise plex_rooms.QueueError(
            "Plexamp advanced while its queue was loading. Retry shortly."
        )
    state = str(event["state"])
    now = json.dumps(plex_rooms.public_track(items[index]))
    next_id = ids[index + 1] if index + 1 < len(ids) else ""
    up_next = json.dumps(plex_rooms.public_track(items[index + 1]) if next_id else {})
    # Consume the startup buffer once, including when it becomes the playing
    # item. Retained/reordered history must never resurrect this queue anchor.
    handoff_id = room["handoff_item_id"]
    if handoff_id not in ids[index + 1 :]:
        handoff_id = ""
    handoff = json.dumps(
        plex_rooms.public_track(items[ids.index(handoff_id)]) if handoff_id else {}
    )
    warning = int(len(items[index + 1 :]) <= 1 or state == "stopped")
    with storage.db() as connection:
        changed = False
        for row in connection.execute(
            "SELECT * FROM room_entries WHERE room_id=? AND queue_item_id IS NOT NULL",
            (room["id"],),
        ).fetchall():
            item_id = row["queue_item_id"]
            playback = (
                "playing"
                if item_id == current
                else "played"
                if item_id in ids[:index] or row["playback"] == "playing"
                else row["playback"]
            )
            if playback != row["playback"]:
                connection.execute(
                    "UPDATE room_entries SET playback=? WHERE id=?",
                    (playback, row["id"]),
                )
                changed = True
        if (
            room["session_key"],
            room["current_item_id"],
            room["now_playing"],
            room["next_item_id"],
            room["up_next"],
            room["handoff_item_id"],
            room["handoff"],
            room["warning"],
            room["playback_state"],
        ) != (
            session["session_key"],
            current,
            now,
            next_id,
            up_next,
            handoff_id,
            handoff,
            warning,
            state,
        ):
            connection.execute(
                "UPDATE rooms SET session_key=?,current_item_id=?,now_playing=?,next_item_id=?,up_next=?,handoff_item_id=?,handoff=?,warning=?,playback_state=? WHERE id=?",
                (
                    session["session_key"],
                    current,
                    now,
                    next_id,
                    up_next,
                    handoff_id,
                    handoff,
                    warning,
                    state,
                    room["id"],
                ),
            )
            changed = True
        if changed:
            bump(connection, room["id"])
    room.update(
        session_key=session["session_key"],
        current_item_id=current,
        now_playing=now,
        next_item_id=next_id,
        up_next=up_next,
        handoff_item_id=handoff_id,
        handoff=handoff,
        warning=warning,
        playback_state=state,
    )
    return ids, index, current


def _active_entries(room):
    return [
        row
        for row in entries(room["id"])
        if not row["removed"] and row["playback"] == "upcoming"
    ]


def _logical_boundary(room, active):
    """The live materialized boundary is independent of placeholder positions."""
    return next(
        (row for row in active if row["queue_item_id"] == room["next_item_id"]), None
    )


def _read_stable(room, adapter, *, appended=None):
    """Refresh both the complete queue and owned stream without moving the boundary."""
    queue = adapter.load(room["queue_id"])
    observed = dict(room)
    ids, index, current = observe(observed, adapter, queue)
    next_id = observed["next_item_id"]
    # An empty upcoming queue can acquire its first item by appending. That item
    # immediately becomes locked; otherwise any boundary drift stops this pass.
    allowed_next = (
        appended if not room["next_item_id"] and appended else room["next_item_id"]
    )
    if (observed["session_key"], current, next_id) != (
        room["session_key"],
        room["current_item_id"],
        allowed_next,
    ):
        raise plex_rooms.QueueError(
            "Playback or Up Next changed during the queue update. Room intent is saved; retry shortly."
        )
    room.update(observed)
    return queue, ids, index


def _protect(
    room, adapter, item_id=None, *, after=None, expected_ids=None, append=False
):
    # The shared file lock serializes Melodarr processes. PMS/player changes are
    # checked independently immediately before each write, including next drift
    # within the same stream and externally reordered suffixes.
    queue, ids, index = _read_stable(room, adapter)
    boundary = index + bool(room["next_item_id"])
    if (
        expected_ids is not None
        and ids != expected_ids
        or item_id is not None
        and (item_id not in ids or ids.index(item_id) <= boundary)
        or after is not None
        and (after not in ids or ids.index(after) < boundary)
    ):
        raise plex_rooms.QueueError(
            "The live protected queue changed. Room intent is saved; retry shortly."
        )
    if append and room["next_item_id"] and not _manual_buffer(queue, ids, index):
        # next=0 appends to PMS's manual region, which can be empty even with a
        # visible next song. Promote that same existing instance in place. This
        # is a metadata bootstrap, never a host reorder or a replacement item.
        adapter.move(room["queue_id"], room["next_item_id"], room["current_item_id"])
        queue, confirmed_ids, confirmed_index = _read_stable(room, adapter)
        if confirmed_ids != ids or not _manual_buffer(
            queue, confirmed_ids, confirmed_index
        ):
            raise plex_rooms.QueueError(
                "Plex did not confirm Room queue initialization. Room intent is saved; retry synchronization."
            )
        ids, index = confirmed_ids, confirmed_index
    return queue, ids, index


def _manual_buffer(queue, ids, index):
    last_added = str(queue.get("playQueueLastAddedItemID") or "")
    return last_added in ids and ids.index(last_added) > index


def acquisition_bridge(room, *, initiate=False):
    """Advance the existing local lifecycle even when PMS is unavailable."""
    placements = json.loads(room["write_intent"]).get("placements", {})
    active = [
        row
        for row in entries(room["id"])
        if row["playback"] == "upcoming"
        and not row["removed"]
        and (
            not row["queue_item_id"]
            or row["id"] in placements
            or row["state"] != "ready"
        )
        and row["recording_mbid"]
    ]
    states = recording_requests.recording_states(
        [row["recording_mbid"] for row in active], include_tracks=True
    )
    for row in active:
        lifecycle = states[row["recording_mbid"]]
        if (
            initiate
            and lifecycle["status"] == "not_requested"
            and not lifecycle.get("available")
            and row["state"] != "waiting_for_queue"
            and not row["error"]
        ):
            with storage.db() as connection:
                host = connection.execute(
                    "SELECT * FROM users WHERE id=?", (room["host_user_id"],)
                ).fetchone()
            try:
                _payload, status = recording_requests.request_for_user(
                    row["recording_mbid"], host
                )
            except Exception:  # noqa: BLE001 - persist a safe acquisition failure at the provider boundary.
                status = 502
            if status >= 400:
                with storage.db() as connection:
                    connection.execute(
                        "UPDATE room_entries SET error=? WHERE id=?",
                        (
                            "Acquisition request failed. The host can retry synchronization.",
                            row["id"],
                        ),
                    )
                    bump(connection, room["id"])
            else:
                lifecycle = recording_requests.status(row["recording_mbid"])
                states[row["recording_mbid"]] = lifecycle
        # Exact Plex availability outranks acquisition/cache status. Room Ready
        # still requires confirmation of this particular PlayQueue instance.
        if lifecycle.get("available") or lifecycle["status"] == "ready":
            lifecycle = {**lifecycle, "status": "ready"}
            states[row["recording_mbid"]] = lifecycle
            status = "waiting_for_queue"
        elif lifecycle["status"] == "not_requested":
            # A temporary local-index gap must not re-request a recording which
            # already reached Plex readiness. Live readiness is still required
            # before retrying its materialization.
            status = (
                "waiting_for_queue"
                if row["state"] == "waiting_for_queue"
                else "requested"
            )
        else:
            status = lifecycle["status"]
        if status != row["state"]:
            with storage.db() as connection:
                connection.execute(
                    "UPDATE room_entries SET state=? WHERE id=?", (status, row["id"])
                )
                bump(connection, room["id"])
    return states


def sync_locked(room, adapter=None, *, initiate=False):
    if room["status"] != "active":
        return
    try:
        _sync(
            room,
            adapter or plex_rooms.PMSQueue(storage.get_service("plex")),
            initiate=initiate,
        )
    except (plex_rooms.QueueError, TimeoutError) as exc:
        # Local acquisition must still advance during observation/PMS outages.
        acquisition_bridge(room, initiate=initiate)
        _sync_error(room, exc)


def _sync_error(room, exc):
    message = (
        str(exc)
        if isinstance(exc, plex_rooms.QueueError)
        else "Room synchronization is busy. Retry shortly."
    )
    with storage.db() as connection:
        changed = connection.execute(
            "UPDATE rooms SET sync_error=?,dirty=1,version=version+1 WHERE id=? AND (sync_error IS NOT ? OR dirty=0)",
            (message, room["id"], message),
        ).rowcount
    if changed:
        logger.warning(
            "Room %s queue synchronization failed (%s)", room["id"], type(exc).__name__
        )
    raise RoomError(message, 502) from None


def _recover_adds(room, queue):
    """Claim interrupted appends before interpreting PMS as external input."""
    rows = entries(room["id"])
    claimed = {row["queue_item_id"] for row in rows if row["queue_item_id"]}
    recovered = []
    for row in rows:
        if not row["add_before"] or row["queue_item_id"]:
            continue
        before = set(json.loads(row["add_before"]))
        candidates = [
            str(item["playQueueItemID"])
            for item in queue.get("Metadata", [])
            if str(item["playQueueItemID"]) not in before
            and str(item.get("ratingKey")) == row["rating_key"]
        ]
        if len(candidates) > 1 or any(identity in claimed for identity in candidates):
            raise plex_rooms.QueueError(
                "An interrupted Plex addition is ambiguous. Inspect Plexamp and restart the Room."
            )
        if candidates or row["removed"]:
            identity = candidates[0] if candidates else None
            recovered.append((identity, row["id"]))
            claimed.add(identity)
    if recovered:
        with storage.db() as connection:
            connection.executemany(
                "UPDATE room_entries SET queue_item_id=?,add_before=NULL WHERE id=?",
                recovered,
            )
            bump(connection, room["id"])


def _adopt(room, queue, ids, index, intent=None):
    """Persist the live materialized queue without writing to PMS.

    Pending requests follow their next surviving anchor or the tail. A blocked
    placeholder waits for its protected boundary to advance, then rebases just
    below the new boundary. Neither pending positions nor write journals veto
    materialized adoption.
    """
    tracks = queue.get("Metadata", [])[index + 1 :]
    if any(
        item.get("type") != "track" or not str(item.get("ratingKey") or "").isdigit()
        for item in tracks
    ):
        raise plex_rooms.QueueError(
            "Plex returned an unplayable queue item. Retry shortly."
        )
    rows = entries(room["id"])
    by_item = {row["queue_item_id"]: row for row in rows if row["queue_item_id"]}
    if len(by_item) != sum(bool(row["queue_item_id"]) for row in rows):
        raise plex_rooms.QueueError(
            "Room queue item identities are ambiguous. Restart the Room."
        )
    missing_mbids = [
        str(track["ratingKey"])
        for track in tracks
        if not by_item.get(str(track["playQueueItemID"]), {}).get("recording_mbid")
    ]
    try:
        recording_mbids = track_search_index.plex_recording_mbids_by_rating_key(
            room["server_id"], missing_mbids
        )
    except (OSError, sqlite3.Error) as exc:
        # Optional identity metadata must not stop live queue adoption.
        logger.warning("Room recording metadata lookup failed (%s)", type(exc).__name__)
        recording_mbids = {}
    active = _active_entries(room)
    future = ids[index + 1 :]
    future_set = set(future)
    placements = (intent or {}).get("placements", {})
    before = {}
    tail = []
    rebased = []
    deferrals = []
    for offset, row in enumerate(active):
        if row["queue_item_id"]:
            continue
        if row["deferred_until"] and row["deferred_until"] != room["next_item_id"]:
            rebased.append(row)
            deferrals.append((None, row["id"]))
            continue
        anchor = next(
            (
                later["queue_item_id"]
                for later in active[offset + 1 :]
                if later["queue_item_id"] in future_set
            ),
            None,
        )
        (before.setdefault(anchor, []) if anchor else tail).append(row)
        if anchor == room["next_item_id"] and not row["deferred_until"]:
            deferrals.append((anchor, row["id"]))
    ordered = []
    changed = False
    with storage.db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if deferrals:
            connection.executemany(
                "UPDATE room_entries SET deferred_until=? WHERE id=?", deferrals
            )
            changed = True
        for track in tracks:
            identity = str(track["playQueueItemID"])
            ordered.extend(before.get(identity, []))
            row = by_item.get(identity)
            metadata = plex_rooms.public_track(track)
            if row is None:
                row = {"id": str(uuid4()), "position": -1}
                connection.execute(
                    "INSERT INTO room_entries(id,room_id,position,title,artist,album,created_at,"
                    "state,rating_key,queue_item_id,recording_mbid) VALUES (?,?,0,?,?,?,?,'ready',?,?,?)",
                    (
                        row["id"],
                        room["id"],
                        metadata["title"],
                        metadata["artist"],
                        metadata["album"],
                        time.time(),
                        str(track["ratingKey"]),
                        identity,
                        recording_mbids.get(str(track["ratingKey"])),
                    ),
                )
                changed = True
            else:
                values = (
                    metadata["title"],
                    metadata["artist"],
                    metadata["album"],
                    str(track["ratingKey"]),
                    row["recording_mbid"]
                    or recording_mbids.get(str(track["ratingKey"])),
                    "waiting_for_queue" if row["id"] in placements else "ready",
                    "upcoming",
                    0,
                    None,
                )
                if values != tuple(
                    row[name]
                    for name in (
                        "title",
                        "artist",
                        "album",
                        "rating_key",
                        "recording_mbid",
                        "state",
                        "playback",
                        "removed",
                        "error",
                    )
                ):
                    connection.execute(
                        "UPDATE room_entries SET title=?,artist=?,album=?,rating_key=?,recording_mbid=?,state=?,"
                        "playback=?,removed=?,error=? WHERE id=?",
                        (*values, row["id"]),
                    )
                    changed = True
            ordered.append(row)
            if identity == room["next_item_id"]:
                ordered.extend(rebased)
        if not room["next_item_id"]:
            ordered.extend(rebased)
        ordered.extend(tail)
        for position, row in enumerate(ordered):
            if position != row["position"]:
                connection.execute(
                    "UPDATE room_entries SET position=? WHERE id=?",
                    (position, row["id"]),
                )
                changed = True
        # observe() has already classified playing/history instances. Only
        # still-upcoming materialized entries can be external removals.
        for row in active:
            if row["queue_item_id"] and row["queue_item_id"] not in future_set:
                connection.execute(
                    "UPDATE room_entries SET removed=1 WHERE id=?", (row["id"],)
                )
                changed = True
        if changed:
            bump(connection, room["id"])
        # Retire the obsolete startup trimming journal without replaying it.
        if room["trim_ids"] != "[]":
            connection.execute(
                "UPDATE rooms SET trim_ids='[]' WHERE id=?", (room["id"],)
            )
    return _active_entries(room)


def _save_intent(room, intent):
    """Journal exact operations, never use dirty/order as an ownership claim."""
    intent = {key: value for key, value in intent.items() if value}
    encoded = json.dumps(intent)
    pending = bool(intent)
    if (room["write_intent"], bool(room["write_pending"])) != (encoded, pending):
        with storage.db() as connection:
            connection.execute(
                "UPDATE rooms SET write_intent=?,write_pending=? WHERE id=?",
                (encoded, pending, room["id"]),
            )
    room.update(write_intent=encoded, write_pending=int(pending))


def _reservation(row, active, ids, index):
    """Queue anchors for one pending placement, independent of other placeholders."""
    offset = next(n for n, entry in enumerate(active) if entry["id"] == row["id"])
    future = set(ids[index + 1 :])
    before = next(
        (
            entry["queue_item_id"]
            for entry in active[offset + 1 :]
            if entry["queue_item_id"] in future
        ),
        None,
    )
    after = next(
        (
            entry["queue_item_id"]
            for entry in reversed(active[:offset])
            if entry["queue_item_id"] in future
        ),
        None,
    )
    return {"before": before, "after": after}


def _write_intent(room, ids, index):
    intent = json.loads(room["write_intent"])
    if intent or not room["write_pending"]:
        return intent
    # Upgrade legacy interrupted writes once, before passive adoption changes
    # their DB positions. The old flag also represented boundary-only failures;
    # retain only identifiable operations over known instances, never freeze PMS.
    active = _active_entries(room)
    intent = {
        "remove": [
            row["queue_item_id"]
            for row in entries(room["id"])
            if row["removed"] and row["queue_item_id"]
        ],
        "order": [
            row["queue_item_id"]
            for row in active
            if row["queue_item_id"] and row["queue_item_id"] != room["next_item_id"]
        ],
        "placements": {
            row["id"]: _reservation(row, active, ids, index)
            for row in active
            if row["add_before"] or row["queue_item_id"] and row["state"] != "ready"
        },
    }
    _save_intent(room, intent)
    return intent


def _finish_sync(room, intent):
    _save_intent(room, intent)
    with storage.db() as connection:
        connection.execute(
            "UPDATE rooms SET sync_error=NULL,dirty=0,version=version+1 "
            "WHERE id=? AND (sync_error IS NOT NULL OR dirty=1)",
            (room["id"],),
        )
    room.update(sync_error=None, dirty=0)


def _confirm(room, adapter, ids):
    queue, actual, index = _read_stable(room, adapter)
    if actual != ids:
        raise plex_rooms.QueueError(
            "Plex did not confirm the Room queue order. Room intent is saved; retry shortly."
        )
    return queue, index


def _placement_anchor(room, placement, ids, index, identity=None):
    """Return a legal insertion anchor, or defer only this operation."""
    remaining = [value for value in ids if value != identity]
    boundary = room["next_item_id"] or room["current_item_id"]
    before, after = placement["before"], placement["after"]
    if before == room["next_item_id"] and before:
        return None
    if before in remaining and remaining.index(before) > remaining.index(boundary):
        return remaining[remaining.index(before) - 1]
    if after in remaining and remaining.index(after) >= remaining.index(boundary):
        return after
    # Both old anchors may have played or been removed. Rebase below the live
    # boundary instead of trying to restore consumed history or the old Up Next.
    return boundary


def _sync(room, adapter, *, initiate=False):
    if adapter.server_id != room["server_id"]:
        raise plex_rooms.QueueError(
            "The configured Plex server changed. End this Room before starting another."
        )
    queue = adapter.load(room["queue_id"])
    ids, index, _current = observe(room, adapter, queue)
    _recover_adds(room, queue)
    ids, index, _current = observe(room, adapter, queue)
    intent = _write_intent(room, ids, index)
    _adopt(room, queue, ids, index, intent)
    states = acquisition_bridge(room, initiate=initiate)

    for identity in list(intent.get("remove", [])):
        # A removal already done externally, or consumed by playback, is satisfied.
        if identity not in ids or ids.index(identity) <= index:
            intent["remove"].remove(identity)
            _save_intent(room, intent)
            continue
        if identity == room["next_item_id"]:
            continue
        _protect(room, adapter, identity, expected_ids=ids)
        adapter.remove(room["queue_id"], identity)
        ids.remove(identity)
        queue, index = _confirm(room, adapter, ids)
        intent["remove"].remove(identity)
        _save_intent(room, intent)

    if "order" in intent:
        # Rebase the saved relative order into only its editable live slots.
        # New external instances keep their slots; current/new Up Next stay put.
        boundary = index + bool(room["next_item_id"])
        desired = [
            identity for identity in intent["order"] if identity in ids[boundary + 1 :]
        ]
        subset = set(desired)
        replacements = iter(desired)
        expected = [
            next(replacements) if identity in subset else identity for identity in ids
        ]
        owns_suffix = set(ids[boundary + 1 :]) == subset
        offsets = (
            range(boundary + 1, len(expected))
            if owns_suffix
            else range(len(expected) - 1, boundary, -1)
        )
        for offset in offsets:
            identity = expected[offset]
            if identity not in subset:
                continue
            if owns_suffix:
                anchor = ids[offset - 1]
            else:
                remaining = [value for value in ids if value != identity]
                following = expected[offset + 1] if offset + 1 < len(expected) else None
                anchor = (
                    remaining[remaining.index(following) - 1]
                    if following
                    else remaining[-1]
                )
            if ids.index(identity) != ids.index(anchor) + 1:
                _protect(room, adapter, identity, after=anchor, expected_ids=ids)
                adapter.move(room["queue_id"], identity, anchor)
                ids.remove(identity)
                ids.insert(ids.index(anchor) + 1, identity)
        if ids != [queue_item["playQueueItemID"] for queue_item in queue["Metadata"]]:
            queue, index = _confirm(room, adapter, ids)
        intent.pop("order")
        _save_intent(room, intent)

    # A placement journal survives an interrupted append or move. Unmaterialized
    # entries without a journal retain their own logical reservation.
    _adopt(room, queue, ids, index, intent)
    active = _active_entries(room)
    active_ids = {row["id"] for row in active}
    for identity in list(intent.get("placements", {})):
        if identity not in active_ids:
            intent["placements"].pop(identity)
    _save_intent(room, intent)
    for row in active:
        placements = intent.setdefault("placements", {})
        placement = placements.get(row["id"])
        lifecycle = states.get(row["recording_mbid"], {})
        if placement is None:
            if row["queue_item_id"] or lifecycle.get("status") != "ready":
                continue
            if row["deferred_until"] == room["next_item_id"] and row["deferred_until"]:
                continue
            placement = _reservation(row, _active_entries(room), ids, index)
        identity = row["queue_item_id"]
        # An interrupted addition that has since become locked is confirmed by
        # its exact PMS identity; never move it across the boundary to restore intent.
        if identity in ids and ids.index(identity) <= index + bool(
            room["next_item_id"]
        ):
            placements.pop(row["id"], None)
            _save_intent(room, intent)
            continue
        anchor = _placement_anchor(room, placement, ids, index, identity)
        if anchor is None:
            if not identity and row["deferred_until"] != room["next_item_id"]:
                with storage.db() as connection:
                    connection.execute(
                        "UPDATE room_entries SET deferred_until=? WHERE id=?",
                        (room["next_item_id"], row["id"]),
                    )
                    bump(connection, room["id"])
            continue
        if not identity:
            if lifecycle.get("status") != "ready":
                continue
            tracks = lifecycle.get("tracks", [])
            if not tracks:
                # Availability can be between indexing steps. Retry this entry,
                # while the confirmed materialized projection remains usable.
                continue
            placements[row["id"]] = placement
            _save_intent(room, intent)
            before = list(ids)
            with storage.db() as connection:
                connection.execute(
                    "UPDATE room_entries SET rating_key=?,add_before=? WHERE id=?",
                    (str(tracks[0]["ratingKey"]), json.dumps(before), row["id"]),
                )
            _protect(room, adapter, expected_ids=ids, append=True)
            adapter.add(room["queue_id"], tracks[0])
            queue = adapter.load(room["queue_id"])
            added = [
                track
                for track in queue.get("Metadata", [])
                if str(track["playQueueItemID"]) not in before
                and str(track.get("ratingKey")) == str(tracks[0]["ratingKey"])
            ]
            if len(added) != 1:
                raise plex_rooms.QueueError(
                    "Plex did not confirm the new queue entry. Room intent is saved; retry shortly."
                )
            identity = str(added[0]["playQueueItemID"])
            with storage.db() as connection:
                connection.execute(
                    "UPDATE room_entries SET queue_item_id=?,add_before=NULL,error=NULL WHERE id=?",
                    (identity, row["id"]),
                )
                bump(connection, room["id"])
            queue, actual, index = _read_stable(room, adapter, appended=identity)
            if [value for value in actual if value != identity] != ids:
                raise plex_rooms.QueueError(
                    "The live protected queue changed during the addition. Room intent is saved; retry shortly."
                )
            ids = actual
        if identity != room["next_item_id"]:
            anchor = _placement_anchor(room, placement, ids, index, identity)
            if anchor is None:
                continue
            if ids.index(identity) != ids.index(anchor) + 1:
                _protect(room, adapter, identity, after=anchor, expected_ids=ids)
                adapter.move(room["queue_id"], identity, anchor)
                ids.remove(identity)
                ids.insert(ids.index(anchor) + 1, identity)
            queue, index = _confirm(room, adapter, ids)
        placements.pop(row["id"], None)
        _save_intent(room, intent)

    _adopt(room, queue, ids, index, intent)
    _finish_sync(room, intent)


def reconcile(code, *, retry=False, initiate=False):
    room = room_by_code(code, active=True)
    with queue_lock(room):
        room = room_by_code(code, active=True)
        if retry:
            with storage.db() as connection:
                connection.execute(
                    "UPDATE room_entries SET error=NULL WHERE room_id=? AND removed=0",
                    (room["id"],),
                )
        sync_locked(room, initiate=initiate)
    return snapshot(code)
