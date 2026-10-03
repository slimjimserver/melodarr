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
    from .. import storage
    from ..request_locks import request_lock
    from . import plex_rooms, recording_requests
else:
    import storage
    from request_locks import request_lock
    from services import plex_rooms, recording_requests


logger = logging.getLogger(__name__)
CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
MAX_ENTRIES = 200


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


def queue_lock(room):
    return request_lock(
        "room-queue", f"{room['server_id']}:{room['queue_id']}", timeout=35
    )


def start(user):
    with request_lock("room-host", user["id"]):
        if host_room(user["id"]):
            raise RoomError("You already have an active Room.", 409)
        adapter = plex_rooms.PMSQueue(storage.get_service("plex"))
        discovered = adapter.discover(user)
        lock_identity = {"server_id": adapter.server_id, **discovered}
        with queue_lock(lock_identity):
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
            room_id = str(uuid4())
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(10))
            try:
                with storage.db() as connection:
                    connection.execute(
                        "INSERT INTO rooms(id,code,host_user_id,status,created_at,server_id,client_id,session_key,queue_id,"
                        "current_item_id,handoff_item_id,trim_ids,now_playing,handoff) VALUES (?,?,?,'active',?,?,?,?,?,?,?,?,?,?)",
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
        connection.execute(
            "DELETE FROM room_choices WHERE expires_at<?", (time.time(),)
        )
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
                    time.time() + 3600,
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
            if not room["write_pending"]:
                _adopt(room, queue, ids, index)
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
            if remove_id is not None:
                connection.execute(
                    "UPDATE room_entries SET removed=1 WHERE room_id=? AND id=?",
                    (room["id"], remove_id),
                )
            connection.execute(
                "UPDATE rooms SET dirty=1,write_pending=1 WHERE id=?", (room["id"],)
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
    """Never reconcile a logical prefix across an already locked PMS item."""
    locked = next(
        (row for row in active if row["queue_item_id"] == room["next_item_id"]), None
    )
    if locked and active[0]["id"] != locked["id"]:
        raise plex_rooms.QueueError(
            "Room order disagrees with live Up Next. Wait for playback to advance or restart the Room."
        )
    return locked


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
    active = [
        row
        for row in entries(room["id"])
        if row["playback"] == "upcoming"
        and not row["removed"]
        and not row["queue_item_id"]
        and row["recording_mbid"]
    ]
    states = recording_requests.recording_states(
        [row["recording_mbid"] for row in active], include_tracks=True
    )
    for row in active:
        lifecycle = states[row["recording_mbid"]]
        if initiate and lifecycle["status"] == "not_requested" and not row["error"]:
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
        status = (
            lifecycle["status"]
            if lifecycle["status"] != "not_requested"
            else "requested"
        )
        if status == "ready" and (not row["queue_item_id"] or row["state"] != "ready"):
            status = "waiting_for_plex"
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
        states = acquisition_bridge(room, initiate=initiate)
        _sync(
            room,
            adapter or plex_rooms.PMSQueue(storage.get_service("plex")),
            states,
        )
    except (plex_rooms.QueueError, TimeoutError) as exc:
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


def _adopt(room, queue, ids, index):
    """Persist the live materialized queue without writing to PMS.

    Pending requests keep their position before the next surviving materialized
    entry (or at the tail). Reordering those anchors while placeholders exist is
    deliberately deferred: retain intent and stop rather than guess placement.
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
    active = _active_entries(room)
    future = ids[index + 1 :]
    future_set = set(future)
    pending = [row for row in active if not row["queue_item_id"]]
    old_common = [
        row["queue_item_id"] for row in active if row["queue_item_id"] in future_set
    ]
    common_set = set(old_common)
    if pending and old_common != [
        identity for identity in future if identity in common_set
    ]:
        raise plex_rooms.QueueError(
            "Plex queue order changed around pending requests. Pending intent is preserved; "
            "wait for playback to advance or end this Room before retrying."
        )
    before = {}
    tail = []
    for offset, row in enumerate(active):
        if row["queue_item_id"]:
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
    ordered = []
    changed = False
    with storage.db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        for track in tracks:
            identity = str(track["playQueueItemID"])
            ordered.extend(before.get(identity, []))
            row = by_item.get(identity)
            metadata = plex_rooms.public_track(track)
            if row is None:
                row = {"id": str(uuid4()), "position": -1}
                connection.execute(
                    "INSERT INTO room_entries(id,room_id,position,title,artist,album,created_at,"
                    "state,rating_key,queue_item_id) VALUES (?,?,0,?,?,?,?,'ready',?,?)",
                    (
                        row["id"],
                        room["id"],
                        metadata["title"],
                        metadata["artist"],
                        metadata["album"],
                        time.time(),
                        str(track["ratingKey"]),
                        identity,
                    ),
                )
                changed = True
            else:
                values = (
                    metadata["title"],
                    metadata["artist"],
                    metadata["album"],
                    str(track["ratingKey"]),
                    "ready",
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
                        "state",
                        "playback",
                        "removed",
                        "error",
                    )
                ):
                    connection.execute(
                        "UPDATE room_entries SET title=?,artist=?,album=?,rating_key=?,state=?,"
                        "playback=?,removed=?,error=? WHERE id=?",
                        (*values, row["id"]),
                    )
                    changed = True
            ordered.append(row)
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


def _finish_sync(room):
    with storage.db() as connection:
        connection.execute(
            "UPDATE rooms SET sync_error=NULL,dirty=0,write_pending=0,version=version+1 "
            "WHERE id=? AND (sync_error IS NOT NULL OR dirty=1 OR write_pending=1)",
            (room["id"],),
        )
    room.update(sync_error=None, dirty=0, write_pending=0)


def _sync(room, adapter, states):
    if adapter.server_id != room["server_id"]:
        raise plex_rooms.QueueError(
            "The configured Plex server changed. End this Room before starting another."
        )
    queue = adapter.load(room["queue_id"])
    ids, index, current = observe(room, adapter, queue)
    _recover_adds(room, queue)
    # Recovered appends may already be playing or Up Next.
    ids, index, current = observe(room, adapter, queue)
    if not room["write_pending"]:
        _adopt(room, queue, ids, index)
    active = _active_entries(room)
    ready = [
        row
        for row in active
        if not row["queue_item_id"]
        and states.get(row["recording_mbid"], {}).get("status") == "ready"
    ]
    if not room["write_pending"] and not ready:
        _finish_sync(room)
        return
    # Durable order/tombstones become write intent only for a host action or
    # newly playable request, never just because an observation failed.
    if not room["write_pending"]:
        with storage.db() as connection:
            connection.execute(
                "UPDATE rooms SET write_pending=1 WHERE id=?", (room["id"],)
            )
        room["write_pending"] = 1
    _logical_boundary(room, active)
    if any(
        row["queue_item_id"] and row["queue_item_id"] not in ids[index + 1 :]
        for row in active
    ):
        raise plex_rooms.QueueError(
            "The live queue changed during saved Room intent. Inspect Plexamp before retrying."
        )
    # Recovery does not treat an interrupted delete/reorder as a host edit.
    # Unknown instances during recovery are retained, but ambiguous changes
    # stop this write; the reconciler must never delete them to enforce ownership.
    known = {
        row["queue_item_id"] for row in entries(room["id"]) if row["queue_item_id"]
    }
    # Older Rooms stored their startup next only in handoff/up_next. Recovery
    # must retain that protected anchor before the first passive import.
    known.add(room["next_item_id"])
    if any(identity not in known for identity in ids[index + 1 :]):
        raise plex_rooms.QueueError(
            "Plex queue changed during saved Room intent. Inspect Plexamp before retrying."
        )
    for row in entries(room["id"]):
        identity = row["queue_item_id"]
        if not row["removed"] or identity not in ids:
            continue
        _protect(room, adapter, identity, expected_ids=ids)
        adapter.remove(room["queue_id"], identity)
        ids.remove(identity)
    for row in ready:
        tracks = states[row["recording_mbid"]].get("tracks", [])
        if not tracks:
            raise plex_rooms.QueueError(
                "Plex availability has no playable copy yet. Retry shortly."
            )
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
            item
            for item in queue.get("Metadata", [])
            if str(item["playQueueItemID"]) not in before
            and str(item.get("ratingKey")) == str(tracks[0]["ratingKey"])
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
    active = _active_entries(room)
    _logical_boundary(room, active)
    anchor = room["next_item_id"] or current
    expected = [
        row["queue_item_id"]
        for row in active
        if row["queue_item_id"] and row["queue_item_id"] != anchor
    ]
    for identity in expected:
        if identity not in ids or ids.index(identity) <= index:
            raise plex_rooms.QueueError(
                "The live queue changed during saved Room intent. Retry shortly."
            )
        if ids.index(identity) != ids.index(anchor) + 1:
            _protect(room, adapter, identity, after=anchor, expected_ids=ids)
            adapter.move(room["queue_id"], identity, anchor)
            ids.remove(identity)
            ids.insert(ids.index(anchor) + 1, identity)
        anchor = identity
    queue, actual, index = _read_stable(room, adapter)
    if actual != ids or actual[index + 1 + bool(room["next_item_id"]) :] != expected:
        raise plex_rooms.QueueError(
            "Plex did not confirm the Room queue order. Room intent is saved; retry shortly."
        )
    # PMS confirmation is the materialized truth, independent of acquisition
    # cache availability. Keep MBIDs/requesters on the original logical entries.
    _adopt(room, queue, actual, index)
    _finish_sync(room)


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
