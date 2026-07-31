"""Periodic recommendation-cache refresh worker."""

import logging
import time
from threading import Event

if __package__ == "backend.workers":
    from ..config import RECOMMENDATION_REFRESH_INTERVAL, RECOMMENDATION_RETRY_INTERVAL
    from ..recommendations import refresh_recommendation_cache
else:  # Support the existing `python backend/app.py` entry point.
    from config import RECOMMENDATION_REFRESH_INTERVAL, RECOMMENDATION_RETRY_INTERVAL
    from recommendations import refresh_recommendation_cache


logger = logging.getLogger(__name__)
refresh_requested = Event()
running = Event()
last_completed_at = None
next_execution_at = None


def request_refresh():
    """Wake the cache loop when linked-account inputs change."""
    refresh_requested.set()


def status():
    return {
        "running": running.is_set(),
        "lastCompletedAt": last_completed_at,
        "nextExecutionAt": next_execution_at,
    }


def run(initial_delay=0):
    global last_completed_at, next_execution_at
    next_execution_at = time.time() + initial_delay
    while True:
        timeout = max(0.0, next_execution_at - time.time())
        refresh_requested.wait(timeout)
        refresh_requested.clear()
        running.set()
        interval = RECOMMENDATION_RETRY_INTERVAL
        try:
            retry_required = refresh_recommendation_cache()
            interval = (
                RECOMMENDATION_RETRY_INTERVAL
                if retry_required
                else RECOMMENDATION_REFRESH_INTERVAL
            )
        except Exception:
            # Database failures and unexpected provider shapes must not kill
            # the application's only recommendation-refresh loop.
            logger.exception(
                "Recommendation refresh failed; retrying on the short interval"
            )
        finally:
            last_completed_at = time.time()
            running.clear()
        next_execution_at = time.time() + interval
