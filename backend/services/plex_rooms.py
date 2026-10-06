"""PMS-only queue adapter. No player addresses or Companion commands are used.

REST operations follow plexapi.playqueue's PMS protocol, using Melodarr's
existing redirect-rejecting HTTP client. Notification tokens stay in headers.
"""

import json
import time
from hashlib import sha256
from threading import Condition, Event, Thread
from urllib.parse import urlsplit, urlunsplit

import requests
import websocket

MAX_QUEUE_ITEMS = 10000
NO_ACTIVE_PLAYBACK = "No compatible active Plex music playback found. Start playing music in Plex and make sure there is another song in Up Next, then retry."

if __package__ == "backend.services":
    from ..http_security import request_without_redirects
    from . import plex
else:
    from http_security import request_without_redirects
    from services import plex


class QueueError(Exception):
    """Safe, actionable error; never wrap provider exception strings."""


class SessionSelectionError(QueueError):
    def __init__(self, message, sessions):
        super().__init__(message)
        self.sessions = sessions


def notification_rows(message):
    try:
        container = json.loads(message).get("NotificationContainer", {})
        rows = container.get("PlaySessionStateNotification", [])
        return (
            [row for row in rows if isinstance(row, dict)]
            if isinstance(rows, list)
            else []
        )
    except (ValueError, TypeError, AttributeError):
        return []


def matches(row, session):
    return (
        isinstance(row, dict)
        and str(row.get("clientIdentifier") or "") == session["client_id"]
        and str(row.get("sessionKey") or "") == session["session_key"]
        and str(row.get("playQueueID") or "").isdigit()
        and str(row.get("playQueueItemID") or "").isdigit()
    )


class NotificationFeed:
    def __init__(self, config):
        self.config = config
        self.condition = Condition()
        self.rows = {}
        self.connected = False
        self.stopped = Event()
        self.socket = None
        self.thread = Thread(
            target=self.run, name="rooms-pms-notifications", daemon=True
        )
        self.thread.start()

    def run(self):
        parsed = urlsplit(self.config["url"])
        url = urlunsplit(
            (
                "wss" if parsed.scheme == "https" else "ws",
                parsed.netloc,
                parsed.path.rstrip("/") + "/:/websockets/notifications",
                "",
                "",
            )
        )
        while not self.stopped.is_set():
            socket = None
            try:
                # No redirect following, TLS verification stays enabled, and
                # tokens never appear in URLs or exception logs.
                socket = websocket.create_connection(
                    url,
                    header={"X-Plex-Token": self.config["token"]},
                    timeout=10,
                    redirect_limit=0,
                    suppress_origin=True,
                )
                self.socket = socket
                with self.condition:
                    self.rows.clear()
                    self.connected = True
                    self.condition.notify_all()
                while not self.stopped.is_set():
                    try:
                        message = socket.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    if not message:
                        break
                    with self.condition:
                        for row in notification_rows(message):
                            key = (
                                str(row.get("clientIdentifier")),
                                str(row.get("sessionKey")),
                            )
                            self.rows[key] = (time.monotonic(), row)
                        self.rows = dict(list(self.rows.items())[-256:])
                        self.condition.notify_all()
            except (websocket.WebSocketException, OSError, ValueError):
                pass
            finally:
                with self.condition:
                    self.connected = False
                if socket:
                    socket.close()
                self.socket = None
            self.stopped.wait(3)

    def stop(self):
        self.stopped.set()
        if self.socket:
            self.socket.close()

    def latest(self, session, *, wait=0):
        deadline = time.monotonic() + wait
        with self.condition:
            while True:
                item = self.rows.get((session["client_id"], session["session_key"]))
                if (
                    self.connected
                    and item
                    and time.monotonic() - item[0] < 45
                    and matches(item[1], session)
                ):
                    return dict(item[1])
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self.condition.wait(min(remaining, 1))


_feed_condition = Condition()
_feeds = {}


def feed(config):
    key = (config["url"], config["token"])
    with _feed_condition:
        if key not in _feeds:
            for previous in _feeds.values():
                previous.stop()
            _feeds.clear()
            _feeds[key] = NotificationFeed(config)
        return _feeds[key]


def public_track(item):
    return {
        "title": str(item.get("title") or "Unknown track")[:300],
        "artist": str(item.get("originalTitle") or item.get("grandparentTitle") or "")[
            :300
        ],
        "album": str(item.get("parentTitle") or "")[:300],
    }


class PMSQueue:
    def __init__(self, config):
        if not config or not config.get("url") or not config.get("token"):
            raise QueueError("Configure Plex Media Server before starting a Room.")
        self.config = config
        try:
            self.server_id = config.get("machineIdentifier") or plex.machine_identifier(
                config
            )
        except (requests.RequestException, ValueError):
            raise QueueError(
                "Plex Media Server could not be identified. Check its configuration and retry."
            ) from None
        self.section_uuids = None

    def call(self, method, path, **params):
        try:
            response = request_without_redirects(
                requests.request,
                method,
                self.config["url"].rstrip("/") + path,
                headers=plex._headers(self.config, accept_json=True),
                params=params,
                timeout=12,
            )
            response.raise_for_status()
            result = response.json()["MediaContainer"]
            if not isinstance(result, dict):
                raise TypeError()
            return result
        except (requests.RequestException, ValueError, KeyError, TypeError):
            raise QueueError(
                "Plex queue synchronization failed. Room intent is saved; retry shortly."
            ) from None

    def _session_candidates(self, user, *, client_id=None, allow_paused=False):
        """Read owned music streams; ignore server-local numeric user IDs."""
        username = str(user["plex_username"] or "").strip().casefold()
        if not user["plex_id"] or not username:
            raise QueueError(
                "Link your Plex account in Account settings before starting a Room."
            )
        sessions = self.call("GET", "/status/sessions").get("Metadata", [])
        if not isinstance(sessions, list):
            raise QueueError("Plex returned invalid playback sessions. Retry shortly.")
        candidates = {}
        for item in sessions:
            if not isinstance(item, dict):
                continue
            player = item.get("Player") or {}
            owner = item.get("User") or {}
            if not isinstance(player, dict) or not isinstance(owner, dict):
                continue
            if (
                str(owner.get("title") or "").strip().casefold() == username
                and player.get("state")
                in ({"playing", "paused"} if allow_paused else {"playing"})
                and item.get("type") == "track"
                and isinstance(player.get("machineIdentifier"), str)
                and player["machineIdentifier"].strip()
                and (client_id is None or str(player["machineIdentifier"]) == client_id)
                and item.get("sessionKey") is not None
                and str(item["sessionKey"]).strip()
                and str(item.get("ratingKey") or "").isdigit()
            ):
                identity = (
                    str(player["machineIdentifier"]),
                    str(item["sessionKey"]),
                    str(item.get("ratingKey") or ""),
                )
                candidates.setdefault(
                    identity,
                    {
                        "client_id": str(player["machineIdentifier"]),
                        "session_key": str(item["sessionKey"]),
                        "rating_key": str(item.get("ratingKey") or ""),
                        "device_name": str(player.get("title") or "")[:300],
                        "product": str(player.get("product") or "")[:100],
                        "platform": str(player.get("platform") or "")[:100],
                        "state": player["state"],
                        **public_track(item),
                    },
                )
        return list(candidates.values())

    def active_session(self, user, *, client_id=None, allow_paused=False):
        """Resolve the current stream; stream session keys are not device IDs."""
        candidates = self._session_candidates(
            user, client_id=client_id, allow_paused=allow_paused
        )
        if not candidates:
            if client_id is not None:
                raise QueueError(
                    "This Room's Plex music playback is no longer active. Resume playback on its original player and retry."
                )
            raise QueueError(NO_ACTIVE_PLAYBACK)
        if len(candidates) != 1:
            raise QueueError(
                "Multiple active Plex music sessions found. Stop playback on your other players and retry."
            )
        return {
            key: candidates[0][key]
            for key in ("client_id", "session_key", "rating_key")
        }

    def _selection_id(self, session):
        # Device binding survives stream-key rotations and track advancement;
        # changing the configured server invalidates the selection token.
        return sha256(
            json.dumps([self.server_id, session["client_id"]]).encode()
        ).hexdigest()

    def _compatible_sessions(self, candidates, *, wait=0):
        """Prove queue capability through PMS, within one notification wait budget."""
        notifications = feed(self.config)
        deadline = time.monotonic() + wait
        compatible = []
        for session in candidates:
            event = notifications.latest(
                session, wait=max(0, deadline - time.monotonic())
            )
            if not (
                event
                and matches(event, session)
                and str(event.get("ratingKey") or "") == session["rating_key"]
                and event.get("state") == session["state"]
            ):
                continue
            try:
                queue = self.load(str(event["playQueueID"]))
            except QueueError:
                # Unsupported/unreachable queues never become selectable, and
                # provider errors are not part of the session response.
                continue
            if not any(
                str(item["playQueueItemID"]) == str(event["playQueueItemID"])
                and str(item["ratingKey"]) == session["rating_key"]
                for item in queue.get("Metadata", [])
            ):
                continue
            compatible.append((session, event))
        return compatible

    def _session_choices(self, compatible):
        return [
            {
                "id": self._selection_id(session),
                "clientId": session["client_id"],
                "sessionKey": session["session_key"],
                "deviceName": session["device_name"],
                "product": session["product"],
                "platform": session["platform"],
                "state": session["state"],
                "title": session["title"],
                "artist": session["artist"],
                "album": session["album"],
                "queueId": str(event["playQueueID"]),
                "currentItemId": str(event["playQueueItemID"]),
            }
            for session, event in compatible
        ]

    def sessions(self, user):
        return self._session_choices(
            self._compatible_sessions(self._session_candidates(user))
        )

    def discover(self, user, *, session_id=None):
        candidates = self._session_candidates(user)
        compatible = self._compatible_sessions(candidates, wait=20)
        if session_id is not None:
            selected = [
                (candidate, event)
                for candidate, event in compatible
                if self._selection_id(candidate) == session_id
            ]
            if len(selected) != 1:
                raise SessionSelectionError(
                    "The selected Plex player is no longer available or has no usable music queue. Refresh players and retry.",
                    self._session_choices(compatible),
                )
            candidate, event = selected[0]
        elif len(compatible) > 1:
            raise SessionSelectionError(
                "Multiple compatible Plex music sessions were found. Choose a player for this Room.",
                self._session_choices(compatible),
            )
        elif compatible:
            candidate, event = compatible[0]
        else:
            # Keep the existing helpful zero-session error.
            raise QueueError(NO_ACTIVE_PLAYBACK)
        session = {
            key: candidate[key] for key in ("client_id", "session_key", "rating_key")
        }
        # Recheck ownership/session after the bounded notification wait.
        if self.active_session(user, client_id=session["client_id"]) != session:
            raise QueueError(
                "Plex music playback changed during startup. Retry the Room handoff."
            )
        if str(event.get("ratingKey") or "") != session["rating_key"]:
            raise QueueError(
                "Plex playback advanced during startup. Retry the Room handoff."
            )
        return {
            **session,
            "device_name": candidate["device_name"],
            "product": candidate["product"],
            "platform": candidate["platform"],
            "queue_id": str(event["playQueueID"]),
            "current_item_id": str(event["playQueueItemID"]),
        }

    def load(self, queue_id):
        result = self.call(
            "GET",
            f"/playQueues/{queue_id}",
            own=0,
            window=MAX_QUEUE_ITEMS,
            includeBefore=1,
            includeAfter=1,
        )
        items = result.get("Metadata", [])
        try:
            complete = (
                isinstance(items, list)
                and str(result.get("playQueueID")) == str(queue_id)
                and int(result.get("playQueueTotalCount", len(items))) == len(items)
                and all(
                    isinstance(item, dict)
                    and str(item.get("playQueueItemID") or "").isdigit()
                    and item.get("type") == "track"
                    and str(item.get("ratingKey") or "").isdigit()
                    for item in items
                )
                and len({str(item["playQueueItemID"]) for item in items}) == len(items)
            )
        except (ValueError, TypeError):
            complete = False
        if not complete:
            raise QueueError(
                "Plex returned an incomplete queue. Use a smaller Plex music queue and retry."
            )
        return result

    def add(self, queue_id, track):
        section_id = str(track.get("librarySectionId") or "")
        if self.section_uuids is None:
            self.section_uuids = {
                str(item["key"]): item.get("uuid")
                for item in self.call("GET", "/library/sections").get("Directory", [])
                if item.get("type") == "artist"
            }
        uuid = self.section_uuids.get(section_id)
        key = str(track.get("ratingKey") or "")
        if not uuid or not key.isdigit():
            raise QueueError(
                "The recording's selected Plex music library could not be resolved."
            )
        return self.call(
            "PUT",
            f"/playQueues/{queue_id}",
            uri=f"library://{uuid}/item/library/metadata/{key}",
            next=0,
        )

    def remove(self, queue_id, item_id):
        self.call("DELETE", f"/playQueues/{queue_id}/items/{item_id}")

    def move(self, queue_id, item_id, after):
        self.call("PUT", f"/playQueues/{queue_id}/items/{item_id}/move", after=after)
