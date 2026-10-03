"""PMS-only queue adapter. No player addresses or Companion commands are used.

REST operations follow plexapi.playqueue's PMS protocol, using Melodarr's
existing redirect-rejecting HTTP client. Notification tokens stay in headers.
"""

import json
import time
from threading import Condition, Event, Thread
from urllib.parse import urlsplit, urlunsplit

import requests
import websocket

MAX_QUEUE_ITEMS = 10000

if __package__ == "backend.services":
    from ..http_security import request_without_redirects
    from . import plex
else:
    from http_security import request_without_redirects
    from services import plex


class QueueError(Exception):
    """Safe, actionable error; never wrap provider exception strings."""


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

    def active_session(self, user, *, client_id=None, allow_paused=False):
        """Resolve the current stream; stream session keys are not device IDs."""
        username = str(user["plex_username"] or "").strip().casefold()
        if not user["plex_id"] or not username:
            raise QueueError(
                "Link your Plex account in Account settings before starting a Room."
            )
        sessions = self.call("GET", "/status/sessions").get("Metadata", [])
        candidates = []
        for item in sessions:
            player = item.get("Player") or {}
            if (
                str((item.get("User") or {}).get("title") or "").strip().casefold()
                == username
                and str(player.get("product") or "").casefold() == "plexamp"
                and player.get("state")
                in ({"playing", "paused"} if allow_paused else {"playing"})
                and item.get("type") == "track"
                and player.get("machineIdentifier")
                and (client_id is None or str(player["machineIdentifier"]) == client_id)
                and item.get("sessionKey") is not None
            ):
                candidates.append(
                    {
                        "client_id": str(player["machineIdentifier"]),
                        "session_key": str(item["sessionKey"]),
                        "rating_key": str(item.get("ratingKey") or ""),
                    }
                )
        if not candidates:
            if client_id is not None:
                raise QueueError(
                    "This Room's Plexamp playback is no longer active. Resume playback on its original device and retry."
                )
            raise QueueError(
                "No active Plexamp playback found. Start playing music in Plexamp, make sure there is another song in Up Next, then try again."
            )
        if len(candidates) != 1:
            raise QueueError(
                "Multiple active Plexamp sessions found. Stop playback on your other devices and retry."
            )
        return candidates[0]

    def discover(self, user):
        notifications = feed(self.config)
        session = self.active_session(user)
        event = notifications.latest(session, wait=20)
        if not event or event.get("state") != "playing":
            raise QueueError(
                "Plexamp's active queue could not be detected. Keep playback running and retry shortly."
            )
        # Recheck ownership/session after the bounded notification wait.
        if self.active_session(user) != session:
            raise QueueError(
                "Plexamp playback changed during startup. Retry the Room handoff."
            )
        if str(event.get("ratingKey") or "") != session["rating_key"]:
            raise QueueError("Plexamp advanced during startup. Retry the Room handoff.")
        return {
            **session,
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
                "Plex returned an incomplete queue. Use a smaller Plexamp queue and retry."
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
