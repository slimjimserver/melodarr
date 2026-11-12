"""Read-only, explicitly allowlisted host troubleshooting data."""

import json

if __package__ == "backend.services":
    from .. import storage
    from . import plex_rooms, recording_requests, rooms
else:
    import storage
    from services import plex_rooms, recording_requests, rooms


def _object(value, fallback):
    try:
        decoded = json.loads(value)
        return decoded if isinstance(decoded, type(fallback)) else fallback
    except (ValueError, TypeError):
        return fallback


def _item_id(value):
    value = str(value or "")
    return value if value.isdigit() and len(value) <= 30 else None


def _item_ids(value):
    return [
        identity
        for item in value[: plex_rooms.MAX_QUEUE_ITEMS]
        if (identity := _item_id(item))
    ]


def inspect(room):
    # Capture journals, entries and revision together. No reconciler/observe(),
    # request initiation or rate-limit writes belong in this read endpoint.
    with storage.db() as connection:
        connection.execute("BEGIN")
        saved = connection.execute(
            "SELECT * FROM rooms WHERE id=?", (room["id"],)
        ).fetchone()
        if not saved:
            raise rooms.RoomError("Room not found or unavailable.", 404)
        room = dict(saved)
        entries = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM room_entries WHERE room_id=? ORDER BY position,id",
                (room["id"],),
            )
        ]
    intent = _object(room["write_intent"], {})
    placements = intent.get("placements", {})
    placements = placements if isinstance(placements, dict) else {}
    result = {
        "room": {
            "id": room["id"],
            "code": room["code"],
            "version": room["version"],
            "status": room["status"],
            "dirty": bool(room["dirty"]),
            "serverId": room["server_id"],
            "clientId": room["client_id"],
            "sessionKey": room["session_key"],
            "queueId": room["queue_id"],
            "currentItemId": room["current_item_id"],
            "nextItemId": room["next_item_id"],
            "deviceName": room["device_name"],
            "product": room["device_product"],
            "platform": room["device_platform"],
            "playbackState": room["playback_state"],
            "syncError": "Room synchronization needs attention."
            if room["sync_error"]
            else None,
        },
        "entries": [
            {
                "id": row["id"],
                "position": row["position"],
                "recordingMbid": row["recording_mbid"],
                "releaseGroupMbid": row["release_group_mbid"],
                "ratingKey": row["rating_key"],
                "playQueueItemId": row["queue_item_id"],
                "title": row["title"],
                "artist": row["artist"],
                "album": row["album"],
                "requester": row["requester"],
                "state": row["state"],
                "playback": row["playback"],
                "removed": bool(row["removed"]),
                "locked": bool(
                    row["queue_item_id"]
                    and row["queue_item_id"]
                    in (room["current_item_id"], room["next_item_id"])
                ),
                "createdAt": row["created_at"],
                "deferredUntil": _item_id(row["deferred_until"]),
                "addBefore": _item_ids(_object(row["add_before"], []))
                if row["add_before"]
                else None,
                "error": "Acquisition or queue placement needs attention."
                if row["error"]
                else None,
            }
            for row in entries
        ],
        "pendingWrites": {
            "pending": bool(room["write_pending"]),
            "remove": _item_ids(intent.get("remove", []))
            if isinstance(intent.get("remove", []), list)
            else [],
            "order": _item_ids(intent.get("order", []))
            if isinstance(intent.get("order", []), list)
            else [],
            "placements": [
                {
                    "entryId": row["id"],
                    "before": _item_id(placements[row["id"]].get("before")),
                    "after": _item_id(placements[row["id"]].get("after")),
                }
                for row in entries
                if isinstance(placements.get(row["id"]), dict)
            ],
            "legacyTrimIds": _item_ids(_object(room["trim_ids"], [])),
        },
        "pms": {
            "reachable": False,
            "queueId": room["queue_id"],
            "currentItemId": None,
            "nextItemId": None,
            "items": [],
            "error": None,
        },
    }
    try:
        adapter = plex_rooms.PMSQueue(storage.get_service("plex"))
        if adapter.server_id != room["server_id"]:
            raise plex_rooms.QueueError("The configured Plex server changed.")
        queue = adapter.load(room["queue_id"])
        items = queue.get("Metadata", [])
        result["pms"].update(
            reachable=True,
            queueId=str(queue["playQueueID"]),
            items=[
                {
                    "playQueueItemId": str(item["playQueueItemID"]),
                    "ratingKey": str(item["ratingKey"]),
                    **plex_rooms.public_track(item),
                }
                for item in items
            ],
        )
        event, session = rooms.playback_event(room, adapter)
        ids = [str(item["playQueueItemID"]) for item in items]
        current = str(event["playQueueItemID"])
        index = ids.index(current)
        if str(items[index]["ratingKey"]) != session["rating_key"]:
            raise plex_rooms.QueueError("Playback advanced during inspection.")
        result["pms"].update(
            currentItemId=current,
            nextItemId=ids[index + 1] if index + 1 < len(ids) else None,
            sessionKey=session["session_key"],
            clientId=session["client_id"],
        )
    except Exception:  # noqa: BLE001 - even malformed provider replies must not leak raw errors.
        result["pms"]["error"] = (
            "Live Plex inspection failed. Persisted Room data is available."
        )
    try:
        states = recording_requests.recording_states(
            [row["recording_mbid"] for row in entries if row["recording_mbid"]]
        )
    except Exception:  # noqa: BLE001 - persisted diagnostics remain useful during local index failures.
        states = {}
        result["acquisitionError"] = "Recording lifecycle inspection is unavailable."
    result["acquisition"] = [
        {
            "entryId": row["id"],
            "recordingMbid": row["recording_mbid"],
            "roomState": row["state"],
            "recordingLifecycleStatus": states.get(row["recording_mbid"], {}).get(
                "status"
            ),
            "available": states.get(row["recording_mbid"], {}).get("available"),
            "plexCopyCount": states.get(row["recording_mbid"], {}).get(
                "plexCopyCount",
                len(states.get(row["recording_mbid"], {}).get("tracks", [])),
            )
            if row["recording_mbid"] in states
            else None,
        }
        for row in entries
    ]
    return result
