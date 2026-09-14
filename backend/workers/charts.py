"""Independent country chart loops; never assemble or wait for personal feeds."""
import logging
from threading import Event

if __package__ == "backend.workers":
    from ..services import charts
else:
    from services import charts

logger = logging.getLogger(__name__)


def run(country, initial_delay=0):
    # Periodic cache checks work in the dedicated worker process as well as the
    # development server; web-process Events are not needed to start chart work.
    sleeper = Event()
    sleeper.wait(initial_delay)
    while True:
        try:
            charts.popular_albums(country)
        except Exception:
            logger.exception("Country chart refresh failed for %s; retrying", country)
        sleeper.wait(charts.RETRY_TTL)
