"""Bridge existing acquisition snapshots and PMS playback into Room updates."""

import logging
import time
from threading import Event

if __package__ == "backend.workers":
    from .. import storage
    from ..services import rooms
else:
    import storage
    from services import rooms


logger = logging.getLogger(__name__)
MAINTENANCE_INTERVAL_SECONDS = 60 * 60
_next_cleanup_at = 0


def tick():
    global _next_cleanup_at
    if time.monotonic() >= _next_cleanup_at:
        try:
            rooms.cleanup()
            _next_cleanup_at = time.monotonic() + MAINTENANCE_INTERVAL_SECONDS
        except Exception as exc:  # noqa: BLE001 - maintenance failure must not stop active Rooms.
            logger.warning("Room cleanup failed (%s)", type(exc).__name__)
    with storage.db() as connection:
        codes = [
            row[0]
            for row in connection.execute(
                "SELECT code FROM rooms WHERE status='active' ORDER BY created_at"
            )
        ]
    for code in codes:
        try:
            rooms.reconcile(code, initiate=True)
        except (rooms.RoomError, TimeoutError):
            pass  # The reconciler persisted the safe error for browsers.
        except Exception as exc:  # noqa: BLE001 - isolate daemon jobs and redact provider exception text.
            # Never log raw provider exceptions: they may include credentials.
            logger.warning("Room reconciliation failed (%s)", type(exc).__name__)


def run():
    sleeper = Event()
    while True:
        tick()
        sleeper.wait(5)
