"""Application paths, service endpoints, and runtime configuration."""

import os
from tempfile import NamedTemporaryFile


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND_ROOT = os.path.join(PROJECT_ROOT, "frontend")

MUSICBRAINZ_URL = "https://musicbrainz.org/ws/2"
COVER_ART_ARCHIVE_URL = "https://coverartarchive.org"
LIDARR_METADATA_URL = "https://api.lidarr.audio/api/v0.4"
LISTENBRAINZ_URL = "https://api.listenbrainz.org/1"
LASTFM_URL = "https://ws.audioscrobbler.com/2.0/"
USER_AGENT = "Melodarr/0.1 (https://github.com/slimjimserver/melodarr)"

MUSICBRAINZ_SEARCH_CACHE_TTL = 10 * 60
MUSICBRAINZ_METADATA_CACHE_TTL = 90 * 24 * 60 * 60
LIDARR_OPTIONS_CACHE_TTL = 5 * 60
LIDARR_METADATA_CACHE_TTL = 30 * 24 * 60 * 60
LIDARR_LIBRARY_SCAN_INTERVAL = 5 * 60
LIDARR_LIBRARY_CACHE_TTL = 30 * 24 * 60 * 60
LISTENBRAINZ_METADATA_CACHE_TTL = 6 * 60 * 60
LASTFM_CACHE_TTL = 60 * 60
RECOMMENDATION_REFRESH_INTERVAL = 12 * 60 * 60
RECOMMENDATION_RETRY_INTERVAL = 5 * 60
PLEX_RECENT_SCAN_INTERVAL = 5 * 60
PLEX_FULL_SCAN_INTERVAL = 12 * 60 * 60
PLEX_LIBRARY_CACHE_TTL = 30 * 24 * 60 * 60

ARTWORK_CACHE_DIRECTORY = os.getenv(
    "MELODARR_ARTWORK_CACHE",
    os.path.join(PROJECT_ROOT, "data", "cache", "artwork"),
)
ARTWORK_CACHE_LIMIT_BYTES = 500 * 1024 * 1024
ARTWORK_MISS_TTL = 24 * 60 * 60
ARTWORK_BROWSER_CACHE_TTL = 7 * 24 * 60 * 60
ARTWORK_MAX_DOWNLOAD_BYTES = 5 * 1024 * 1024
# Longest-edge pixel budgets for the resized variants Melodarr serves. Providers
# return images far larger than any Melodarr layout: artist art arrives at
# 1000x1000 (~690 KB) for cards that render it at 44 CSS pixels.
ARTWORK_SIZES = {"thumb": 128, "card": 384, "large": 640}
ARTWORK_WEBP_QUALITY = 80

DATABASE = os.getenv("MELODARR_DATABASE", os.path.join(PROJECT_ROOT, "melodarr.db"))
CACHE_DATABASE = os.getenv(
    "MELODARR_CACHE_DATABASE",
    os.path.join(os.path.dirname(os.path.abspath(DATABASE)), "cache", "metadata.db"),
)
SETTINGS_FILE = os.getenv(
    "MELODARR_SETTINGS",
    os.path.join(os.path.dirname(os.path.abspath(DATABASE)), "settings.json"),
)
SECRET_KEY_FILE = os.getenv(
    "MELODARR_SECRET_KEY_FILE",
    os.path.join(os.path.dirname(os.path.abspath(DATABASE)), "session-secret.key"),
)


def load_session_secret():
    """Load or create the persistent key used to sign browser sessions."""
    configured_secret = os.getenv("MELODARR_SECRET_KEY")
    if configured_secret:
        return configured_secret
    if os.path.exists(SECRET_KEY_FILE):
        with open(SECRET_KEY_FILE, encoding="utf-8") as file:
            return file.read().strip()

    secret = os.urandom(48).hex()
    directory = os.path.dirname(os.path.abspath(SECRET_KEY_FILE))
    os.makedirs(directory, exist_ok=True)
    with NamedTemporaryFile("w", encoding="utf-8", dir=directory, delete=False) as file:
        file.write(secret)
        temporary_path = file.name
    try:
        os.replace(temporary_path, SECRET_KEY_FILE)
        try:
            os.chmod(SECRET_KEY_FILE, 0o600)
        except OSError:
            pass
    finally:
        if os.path.exists(temporary_path):
            os.unlink(temporary_path)
    return secret
