"""Scheduled cache of Lidarr release-group download statistics."""

import logging
import time
from threading import Event

import requests

if __package__ == "backend.workers":
    from ..config import LIDARR_LIBRARY_SCAN_INTERVAL
    from ..services import lidarr
    from ..storage import get_service
else:
    from config import LIDARR_LIBRARY_SCAN_INTERVAL
    from services import lidarr
    from storage import get_service


logger = logging.getLogger(__name__)
wake_requested = Event()
job_state = {"running": False, "lastCompletedAt": None, "nextExecutionAt": None}


def request_scan():
    job_state["nextExecutionAt"] = time.time()
    wake_requested.set()


def status():
    return dict(job_state)


def _run_scan():
    job_state["running"] = True
    try:
        config = get_service("lidarr")
        if config:
            lidarr.scan_library_availability(config)
    except (ValueError, requests.RequestException) as exc:
        logger.warning("Lidarr library scan failed: %s", exc)
    except Exception:
        logger.exception("Lidarr library scan failed")
    finally:
        completed_at = time.time()
        job_state.update({
            "running": False,
            "lastCompletedAt": completed_at,
            "nextExecutionAt": completed_at + LIDARR_LIBRARY_SCAN_INTERVAL,
        })


def run():
    job_state["nextExecutionAt"] = time.time()
    while True:
        if time.time() >= (job_state["nextExecutionAt"] or 0):
            _run_scan()
        timeout = max(0.1, (job_state["nextExecutionAt"] or time.time() + 60) - time.time())
        wake_requested.wait(timeout)
        wake_requested.clear()
