"""Small Rooms HTTP boundary: opaque choices, scoped guests, host-only controls."""

import json
import re
import time
from functools import wraps
from hashlib import sha256
from hmac import compare_digest
from threading import Lock, Semaphore
from urllib.parse import urlsplit
from uuid import UUID

from flask import (
    Blueprint,
    Response,
    current_app,
    jsonify,
    request,
    session,
    stream_with_context,
)

if __package__ == "backend.routes":
    from .. import storage
    from ..artwork_cache import cached_artwork, serve_cached_artwork
    from ..responses import api_error
    from ..security import current_user, login_required
    from ..services import musicbrainz, plex_rooms, room_diagnostics, rooms
    from .discovery import _search_response
else:
    import storage
    from artwork_cache import cached_artwork, serve_cached_artwork
    from responses import api_error
    from routes.discovery import _search_response
    from security import current_user, login_required
    from services import musicbrainz, plex_rooms, room_diagnostics, rooms


blueprint = Blueprint("rooms", __name__)
_streams = Semaphore(6)  # Reserve production request threads for ordinary APIs.
_stream_lock = Lock()
_stream_identities = set()


def guest_route(view):
    """This boundary performs its own room-scoped CSRF/capability validation."""
    view._melodarr_room_guest_route = True
    return view


def boundary(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        try:
            return view(*args, **kwargs)
        except rooms.RoomError as exc:
            return api_error(str(exc), exc.status)
        except plex_rooms.SessionSelectionError as exc:
            return jsonify(
                {"error": str(exc), "selectionRequired": True, "sessions": exc.sessions}
            ), 409
        except plex_rooms.QueueError as exc:
            return api_error(str(exc), 502)
        except TimeoutError:
            return api_error("The Room is busy. Retry shortly.", 503)

    return wrapped


def rate(action, maximum, *, identity=None):
    identity = sha256(
        str(identity or request.remote_addr or "unknown").encode()
    ).hexdigest()
    window = int(time.time() // 60)
    with storage.db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "DELETE FROM room_rate_limits WHERE window<?",
            (window - rooms.RATE_LIMIT_RETENTION_WINDOWS,),
        )
        connection.execute(
            "INSERT INTO room_rate_limits VALUES (?,?,?,1) ON CONFLICT(identity,action,window) DO UPDATE SET count=count+1",
            (identity, action, window),
        )
        count = connection.execute(
            "SELECT count FROM room_rate_limits WHERE identity=? AND action=? AND window=?",
            (identity, action, window),
        ).fetchone()[0]
    if count > maximum:
        raise rooms.RoomError("Too many Room requests. Retry in a minute.", 429)


def code_room(code, *, active=False):
    if not re.fullmatch(r"[23456789A-HJ-NP-Z]{10}", code.upper()):
        raise rooms.RoomError("Room not found or unavailable.", 404)
    return rooms.room_by_code(code, active=active)


def safe_json(fields):
    # Custom header and JSON reject cross-origin simple form submissions even
    # when Origin is absent. No CORS is enabled on these endpoints.
    if request.headers.get("X-Room-Request") != "1" or not request.is_json:
        raise rooms.RoomError(
            "Room requests require JSON and the Room request header.", 403
        )
    origin = request.headers.get("Origin")
    if origin:
        try:
            parsed = urlsplit(origin)
            public_url = str(
                (storage.get_service("melodarr") or {}).get("applicationUrl") or ""
            )
            hosts = {request.host, urlsplit(public_url).netloc}
        except ValueError:
            raise rooms.RoomError("Invalid Room request origin.", 403) from None
        # Upstream TLS termination can change request.scheme/Host. The JSON
        # custom header requires a browser CORS preflight (CORS is disabled),
        # while this check also permits the administrator's public app URL.
        if (
            not parsed.netloc
            or parsed.scheme not in {"http", "https"}
            or parsed.netloc not in hosts
        ):
            raise rooms.RoomError("Cross-origin Room requests are not allowed.", 403)
    if request.headers.get("Sec-Fetch-Site") == "cross-site":
        raise rooms.RoomError("Cross-origin Room requests are not allowed.", 403)
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or set(payload) - set(fields):
        raise rooms.RoomError("Invalid Room request fields.")
    return payload


def participant(room, *, mutation=False):
    user = current_user()
    if (
        user
        and room["host_user_id"] == user["id"]
        and not (mutation and request.headers.get("X-Room-CSRF"))
    ):
        if mutation:
            expected = session.get("csrf_token", "")
            if not expected or not compare_digest(
                expected.encode(), request.headers.get("X-CSRF-Token", "").encode()
            ):
                raise rooms.RoomError("Invalid or missing CSRF token.", 403)
        return {"id": f"host:{user['id']}", "name": str(user["username"]), "host": True}
    guest = rooms.guest(room, request.cookies.get("room_guest"))
    if not guest:
        raise rooms.RoomError("Join this Room before using it.", 403)
    if mutation and not compare_digest(
        guest["csrf_token"].encode(), request.headers.get("X-Room-CSRF", "").encode()
    ):
        raise rooms.RoomError("Invalid or missing Room CSRF token.", 403)
    return {"id": guest["id"], "name": guest["name"], "host": False}


def owner(room):
    user = current_user()
    if not user or user["id"] != room["host_user_id"]:
        raise rooms.RoomError("Only this Room's host can do that.", 403)
    return user


@blueprint.after_request
def private_response(response):
    if request.endpoint in {
        "rooms.artwork",
        "rooms.plex_artwork",
    } and response.status_code in {200, 304}:
        # Room capabilities stay scoped; only the participant's browser caches
        # image bytes. JSON, SSE and failed image requests remain uncached.
        response.cache_control.public = False
        response.cache_control.private = True
    else:
        response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    if response.status_code == 429:
        response.headers["Retry-After"] = "60"
    return response


@blueprint.get("/api/rooms/active")
@login_required
@boundary
def active():
    return jsonify({"room": rooms.host_room(current_user()["id"])})


@blueprint.post("/api/rooms")
@login_required
@boundary
def start():
    rate("start", 4, identity=f"host:{current_user()['id']}")
    payload = safe_json({"sessionId"}) if request.get_data() else {}
    selection = payload.get("sessionId")
    if selection is not None and (
        not isinstance(selection, str) or not re.fullmatch(r"[0-9a-f]{64}", selection)
    ):
        raise rooms.RoomError("Choose an active Plexamp device.")
    return jsonify({"room": rooms.start(current_user(), session_id=selection)}), 201


@blueprint.get("/api/rooms/sessions")
@login_required
@boundary
def sessions():
    return jsonify(
        {
            "sessions": plex_rooms.PMSQueue(storage.get_service("plex")).sessions(
                current_user()
            )
        }
    )


@blueprint.get("/api/rooms/<code>/diagnostics")
@login_required
@boundary
def diagnostics(code):
    room = code_room(code)
    owner(room)
    return jsonify(room_diagnostics.inspect(room))


@blueprint.post("/api/rooms/<code>/join")
@guest_route
@boundary
def join(code):
    rate("join", 10)
    room = code_room(code, active=True)
    payload = safe_json({"name"})
    name = payload.get("name", "")
    if (
        not isinstance(name, str)
        or len(name) > 60
        or any(ord(character) < 32 for character in name)
    ):
        raise rooms.RoomError("Guest names must be text of 60 characters or fewer.")
    guest, token = rooms.join(room["code"], name, request.cookies.get("room_guest"))
    response = jsonify(
        {
            "guest": guest,
            "room": rooms.project_snapshot(rooms.snapshot(room["code"]), host=False),
        }
    )
    response.set_cookie(
        "room_guest",
        token,
        httponly=True,
        secure=current_app.config["SESSION_COOKIE_SECURE"],
        samesite="Lax",
        path=f"/api/rooms/{room['code']}",
        max_age=7 * 24 * 60 * 60,
    )
    return response


@blueprint.get("/api/rooms/<code>")
@boundary
def state(code):
    rate("state", 90)
    room = code_room(code)
    person = participant(room)
    return jsonify(
        {
            "room": rooms.project_snapshot(
                rooms.snapshot(room["code"]), host=person["host"]
            )
        }
    )


@blueprint.get("/api/rooms/<code>/search")
@boundary
def search(code):
    rate("search-ip", 25)
    room = code_room(code, active=True)
    person = participant(room)
    rate("search-person", 12, identity=person["id"])
    query = request.args.get("q", "").strip()
    if not 2 <= len(query) <= 200:
        raise rooms.RoomError("Search for tracks using 2 to 200 characters.")
    # Same implementation as /api/v1/search?type=track, with an allowlist at
    # the public boundary. Plex availability details never reach guests.
    response = current_app.make_response(
        _search_response(query=query, search_type="track")
    )
    if response.status_code != 200:
        return api_error(
            "Track search could not be completed. Retry shortly.", response.status_code
        )
    choices = rooms.save_choices(room, response.get_json().get("results", []))
    if not person["host"]:
        choices = [
            {**choice, "state": "ready" if choice["state"] == "ready" else "requested"}
            for choice in choices
        ]
    return jsonify({"results": choices})


@blueprint.post("/api/rooms/<code>/entries")
@guest_route
@boundary
def add(code):
    rate("add-ip", 30)
    room = code_room(code, active=True)
    payload = safe_json({"choiceId"})
    person = participant(room, mutation=True)
    rate("add-person", 15, identity=person["id"])
    choice = payload.get("choiceId")
    if not isinstance(choice, str) or len(choice) > 100:
        raise rooms.RoomError("Choose a track from the Room search results.")
    return jsonify(
        {
            "room": rooms.project_snapshot(
                rooms.add(
                    room["code"],
                    choice,
                    person["name"],
                    None if person["host"] else person["id"],
                ),
                host=person["host"],
            )
        }
    ), 201


@blueprint.put("/api/rooms/<code>/order")
@login_required
@boundary
def reorder(code):
    room = code_room(code, active=True)
    user = owner(room)
    payload = safe_json({"entryIds", "version"})
    order = payload.get("entryIds")
    if (
        not isinstance(order, list)
        or len(order) > plex_rooms.MAX_QUEUE_ITEMS
        or any(not isinstance(item, str) or len(item) > 40 for item in order)
    ):
        raise rooms.RoomError("Provide the upcoming Room entry IDs.")
    return jsonify(
        {
            "room": rooms.edit(
                room["code"], user["id"], order=order, version=payload.get("version")
            )
        }
    )


@blueprint.delete("/api/rooms/<code>/entries/<entry_id>")
@login_required
@boundary
def remove(code, entry_id):
    room = code_room(code, active=True)
    user = owner(room)
    payload = safe_json({"version"})
    return jsonify(
        {
            "room": rooms.edit(
                room["code"],
                user["id"],
                remove_id=entry_id,
                version=payload.get("version"),
            )
        }
    )


@blueprint.post("/api/rooms/<code>/end")
@login_required
@boundary
def end(code):
    room = code_room(code)
    user = owner(room)
    return jsonify({"room": rooms.end(room["code"], user["id"])})


@blueprint.post("/api/rooms/<code>/sync")
@login_required
@boundary
def retry(code):
    room = code_room(code, active=True)
    owner(room)
    rate("sync", 6, identity=room["id"])
    return jsonify({"room": rooms.reconcile(room["code"], retry=True, initiate=True)})


@blueprint.get("/api/rooms/<code>/artwork/<mbid>")
@boundary
def artwork(code, mbid):
    room = code_room(code)
    participant(room)
    try:
        mbid = str(UUID(mbid))
    except ValueError:
        raise rooms.RoomError("Artwork unavailable.", 404) from None
    with storage.db() as connection:
        allowed = connection.execute(
            "SELECT 1 FROM room_choices WHERE room_id=? AND release_group_mbid=? AND expires_at>? "
            "UNION ALL SELECT 1 FROM room_entries WHERE room_id=? AND release_group_mbid=? LIMIT 1",
            (room["id"], mbid, time.time(), room["id"], mbid),
        ).fetchone()
    if not allowed and not _has_artwork(rooms.snapshot(room["code"]), request.path):
        raise rooms.RoomError("Artwork unavailable.", 404)
    return cached_artwork(
        f"release-group-{mbid}",
        musicbrainz.cover_art_url(mbid, size=500),
        size=request.args.get("size") or "thumb",
    )


def _has_artwork(state, path):
    tracks = [state["nowPlaying"], state["upNext"], state["handoff"], *state["queue"]]
    return any(
        path == track.get(field)
        for track in tracks
        for field in ("artwork", "artworkFallback")
    )


@blueprint.get("/api/rooms/<code>/plex-artwork/<cache_key>")
@boundary
def plex_artwork(code, cache_key):
    room = code_room(code)
    participant(room)
    if not re.fullmatch(r"plex-album-[0-9a-f]{64}", cache_key) or not _has_artwork(
        rooms.snapshot(room["code"]), request.path
    ):
        raise rooms.RoomError("Artwork unavailable.", 404)
    return serve_cached_artwork(cache_key, size=request.args.get("size") or "thumb")


@blueprint.get("/api/rooms/<code>/events")
@boundary
def events(code):
    rate("events", 90)
    room = code_room(code)
    person = participant(room)
    identity = (room["id"], person["id"])
    with _stream_lock:
        if identity in _stream_identities or not _streams.acquire(blocking=False):
            raise rooms.RoomError("Realtime connections are busy. Retry shortly.", 429)
        _stream_identities.add(identity)

    @stream_with_context
    def stream():
        version, deadline = None, time.monotonic() + 25
        try:
            while time.monotonic() < deadline:
                state = rooms.project_snapshot(
                    rooms.snapshot(room["code"]), host=person["host"]
                )
                # Artwork can arrive from the library worker without a queue
                # edit. Preserve queue versions while publishing additive art.
                revision = (
                    state["version"],
                    tuple(
                        (track.get("artwork"), track.get("artworkFallback"))
                        for track in [
                            state["nowPlaying"],
                            state["upNext"],
                            state["handoff"],
                            *state["queue"],
                        ]
                    ),
                )
                if revision != version:
                    yield f"event: room\ndata: {json.dumps(state)}\n\n"
                    version = revision
                if state["status"] == "closed":
                    break
                yield ": heartbeat\n\n"
                time.sleep(2)
        finally:
            with _stream_lock:
                _stream_identities.discard(identity)
                _streams.release()

    response = Response(stream(), mimetype="text/event-stream")
    response.headers["X-Accel-Buffering"] = "no"
    return response
