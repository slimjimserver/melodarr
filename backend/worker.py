"""Dedicated recommendation-worker process entry point."""

from threading import Thread

if __package__:
    from .storage import init_db
    from .workers import lidarr_searches as lidarr_search_worker
    from .workers import lidarr_library as lidarr_library_worker
    from .workers import plex as plex_worker
    from .workers import plex_metadata as plex_metadata_worker
    from .workers import recommendations as recommendation_worker
else:  # Support `python backend/worker.py` for local development.
    from storage import init_db
    from workers import lidarr_searches as lidarr_search_worker
    from workers import lidarr_library as lidarr_library_worker
    from workers import plex as plex_worker
    from workers import plex_metadata as plex_metadata_worker
    from workers import recommendations as recommendation_worker


def main():
    """Initialize storage and run both background job loops."""
    init_db()
    lidarr_thread = Thread(
        target=lidarr_search_worker.run,
        name="lidarr-search-followups",
        daemon=True,
    )
    lidarr_thread.start()
    lidarr_library_thread = Thread(
        target=lidarr_library_worker.run,
        name="lidarr-library-scan",
        daemon=True,
    )
    lidarr_library_thread.start()
    plex_thread = Thread(
        target=plex_worker.run,
        name="plex-library-scans",
        daemon=True,
    )
    plex_thread.start()
    plex_metadata_thread = Thread(
        target=plex_metadata_worker.run,
        name="plex-musicbrainz-enrichment",
        daemon=True,
    )
    plex_metadata_thread.start()
    recommendation_worker.run()


def start_background_thread():
    """Run recommendation refreshes alongside a single web worker."""
    thread = Thread(
        target=main,
        name="recommendation-refresh",
        daemon=True,
    )
    thread.start()
    return thread


if __name__ == "__main__":
    main()
