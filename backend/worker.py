"""Dedicated background-worker process entry point."""
from threading import Thread

if __package__:
    from .api_cache import init_cache_db
    from .storage import init_db
    from .workers import anime_metadata as anime_metadata_worker
    from .workers import artist_metadata as artist_metadata_worker
    from .workers import lidarr_searches as lidarr_search_worker
    from .workers import lidarr_library as lidarr_library_worker
    from .workers import lidarr_downloads as lidarr_download_worker
    from .workers import plex as plex_worker
    from .workers import plex_history as plex_history_worker
    from .workers import plex_metadata as plex_metadata_worker
    from .workers import recommendations as recommendation_worker
    from .workers import notifications as notification_worker
else:  # Support `python backend/worker.py` for local development.
    from api_cache import init_cache_db
    from storage import init_db
    from workers import anime_metadata as anime_metadata_worker
    from workers import artist_metadata as artist_metadata_worker
    from workers import lidarr_searches as lidarr_search_worker
    from workers import lidarr_library as lidarr_library_worker
    from workers import lidarr_downloads as lidarr_download_worker
    from workers import plex as plex_worker
    from workers import plex_history as plex_history_worker
    from workers import plex_metadata as plex_metadata_worker
    from workers import recommendations as recommendation_worker
    from workers import notifications as notification_worker


LIDARR_LIBRARY_STARTUP_DELAY = 10
PLEX_LIBRARY_STARTUP_DELAY = 30
PLEX_HISTORY_STARTUP_DELAY = 60
RECOMMENDATION_STARTUP_DEADLINE = 120


def main():
    """Initialize storage and start background jobs in a controlled sequence."""
    init_cache_db()
    init_db()
    anime_metadata_thread = Thread(
        target=anime_metadata_worker.run,
        name="anime-musicbrainz-resolution",
        daemon=True,
    )
    anime_metadata_thread.start()
    artist_metadata_thread = Thread(
        target=artist_metadata_worker.run,
        name="musicbrainz-artist-revalidation",
        daemon=True,
    )
    artist_metadata_thread.start()
    lidarr_thread = Thread(
        target=lidarr_search_worker.run,
        name="lidarr-search-followups",
        daemon=True,
    )
    lidarr_thread.start()
    lidarr_download_thread = Thread(
        target=lidarr_download_worker.run,
        name="lidarr-download-status",
        daemon=True,
    )
    lidarr_download_thread.start()
    lidarr_library_thread = Thread(
        target=lidarr_library_worker.run,
        args=(LIDARR_LIBRARY_STARTUP_DELAY,),
        name="lidarr-library-scan",
        daemon=True,
    )
    lidarr_library_thread.start()
    plex_thread = Thread(
        target=plex_worker.run,
        args=(PLEX_LIBRARY_STARTUP_DELAY,),
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
    plex_history_thread = Thread(
        target=plex_history_worker.run,
        args=(PLEX_HISTORY_STARTUP_DELAY,),
        name="plex-listening-history",
        daemon=True,
    )
    plex_history_thread.start()
    notification_thread = Thread(
        target=notification_worker.run,
        name="notification-deliveries",
        daemon=True,
    )
    notification_thread.start()
    recommendation_worker.run(RECOMMENDATION_STARTUP_DEADLINE)


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
