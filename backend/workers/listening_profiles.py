"""Daily refresh loop for private, AI-ready listening profiles."""

import logging
import time
from threading import Event

if __package__ == "backend.workers":
    from ..config import LISTENING_PROFILE_REFRESH_INTERVAL
    from ..listening_profiles import refresh_all_profiles
else:  # Support the existing `python backend/worker.py` entry point.
    from config import LISTENING_PROFILE_REFRESH_INTERVAL
    from listening_profiles import refresh_all_profiles


logger = logging.getLogger(__name__)
RETRY_INTERVAL = 15 * 60
refresh_requested = Event()
running = Event()
last_completed_at = None
last_successful_at = None
last_error = None
next_execution_at = None


def request_refresh():
    """Wake the profile loop after an input account or service changes."""
    refresh_requested.set()


def status():
    return {
        "running": running.is_set(),
        "lastCompletedAt": last_completed_at,
        "lastSuccessfulAt": last_successful_at,
        "lastError": last_error,
        "nextExecutionAt": next_execution_at,
    }


def run(initial_delay=0):
    """Refresh at startup, every 24 hours, or on an explicit wake request."""
    global last_completed_at, last_successful_at, last_error, next_execution_at
    next_execution_at = time.time() + initial_delay
    while True:
        timeout = max(0.0, next_execution_at - time.time())
        refresh_requested.wait(timeout)
        refresh_requested.clear()
        running.set()
        interval = LISTENING_PROFILE_REFRESH_INTERVAL
        try:
            retry_required = refresh_all_profiles()
            if retry_required:
                interval = RETRY_INTERVAL
                last_error = (
                    "One or more listening sources were unavailable; "
                    "last-known source data was retained."
                )
            else:
                last_successful_at = time.time()
                last_error = None
        except Exception as exc:
            interval = RETRY_INTERVAL
            last_error = str(exc)[:500]
            logger.exception(
                "Listening-profile refresh failed; retaining stored profiles"
            )
        finally:
            last_completed_at = time.time()
            running.clear()
        next_execution_at = time.time() + interval

