"""Short-interval Lidarr queue snapshots shared with Flask through SQLite."""

import logging
import time
from threading import Event

import requests

if __package__ == "backend.workers":
    from ..config import LIDARR_DOWNLOAD_POLL_INTERVAL
    from ..services import lidarr
    from ..storage import get_service
else:
    from config import LIDARR_DOWNLOAD_POLL_INTERVAL
    from services import lidarr
    from storage import get_service


logger = logging.getLogger(__name__)
wake_requested = Event()
running = Event()
last_completed_at = None
next_execution_at = None


def request_poll():
    """Wake promptly once AlbumSearch has been accepted by Lidarr."""
    wake_requested.set()


def status():
    return {"running": running.is_set(), "lastCompletedAt": last_completed_at,
            "nextExecutionAt": next_execution_at}


def poll_once():
    config = get_service("lidarr")
    if config:
        lidarr.refresh_download_snapshot(config)


def run():
    global last_completed_at, next_execution_at
    while True:
        running.set()
        try:
            poll_once()
        except (ValueError, requests.RequestException, OSError):
            logger.warning("Lidarr download queue poll failed; preserving prior snapshot")
        except Exception:
            logger.exception("Lidarr download queue poll failed; preserving prior snapshot")
        finally:
            last_completed_at = time.time()
            next_execution_at = last_completed_at + LIDARR_DOWNLOAD_POLL_INTERVAL
            running.clear()
        wake_requested.wait(LIDARR_DOWNLOAD_POLL_INTERVAL)
        wake_requested.clear()
