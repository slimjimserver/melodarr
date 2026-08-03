"""Application paths, service endpoints, and runtime configuration."""

import os
import tempfile
from tempfile import NamedTemporaryFile


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND_ROOT = os.path.join(PROJECT_ROOT, "frontend")


def _path_from_environment(name, default):
    """Return a configured filesystem path, rejecting explicit blank values."""
    value = os.getenv(name)
    if value is None:
        return default
    if not value.strip():
        raise RuntimeError(f"{name} must not be blank.")
    return value

MUSICBRAINZ_URL = "https://musicbrainz.org/ws/2"
COVER_ART_ARCHIVE_URL = "https://coverartarchive.org"
LIDARR_METADATA_URL = "https://api.lidarr.audio/api/v0.4"
LISTENBRAINZ_URL = "https://api.listenbrainz.org/1"
LASTFM_URL = "https://ws.audioscrobbler.com/2.0/"
USER_AGENT = "Melodarr/0.1 (https://github.com/slimjimserver/melodarr)"

MUSICBRAINZ_SEARCH_CACHE_TTL = 10 * 60
MUSICBRAINZ_METADATA_CACHE_TTL = 90 * 24 * 60 * 60
MUSICBRAINZ_ARTIST_REVALIDATION_INTERVAL = 24 * 60 * 60
MUSICBRAINZ_ARTIST_REVALIDATION_RETRY_INTERVAL = 60 * 60
LIDARR_OPTIONS_CACHE_TTL = 5 * 60
LIDARR_METADATA_CACHE_TTL = 30 * 24 * 60 * 60
LIDARR_LIBRARY_SCAN_INTERVAL = 4 * 60
LIDARR_LIBRARY_CACHE_TTL = 30 * 24 * 60 * 60
LISTENBRAINZ_METADATA_CACHE_TTL = 6 * 60 * 60
LASTFM_CACHE_TTL = 60 * 60
RECOMMENDATION_REFRESH_INTERVAL = 12 * 60 * 60
RECOMMENDATION_RETRY_INTERVAL = 5 * 60
PLEX_RECENT_SCAN_INTERVAL = 5 * 60
PLEX_FULL_SCAN_INTERVAL = 12 * 60 * 60
PLEX_LIBRARY_CACHE_TTL = 30 * 24 * 60 * 60
API_CACHE_CLEANUP_INTERVAL = 60 * 60
DETAIL_PAYLOAD_CACHE_TTL = 5 * 60
DETAIL_PAYLOAD_BROWSER_TTL = 60
DETAIL_PAYLOAD_CACHE_MAX_ENTRIES = 128

ARTWORK_CACHE_DIRECTORY = _path_from_environment(
    "MELODARR_ARTWORK_CACHE",
    os.path.join(PROJECT_ROOT, "data", "cache", "artwork"),
)
ARTWORK_CACHE_LIMIT_BYTES = 500 * 1024 * 1024
# Let writes briefly exceed the target before paying for a directory-wide LRU
# pass. A trim still evicts back to ARTWORK_CACHE_LIMIT_BYTES.
ARTWORK_CACHE_HIGH_WATER_BYTES = int(ARTWORK_CACHE_LIMIT_BYTES * 1.05)
ARTWORK_CACHE_TRIM_INTERVAL = 5 * 60
ARTWORK_MISS_TTL = 24 * 60 * 60
ARTWORK_MISS_CACHE_MAX_ENTRIES = 4096
ARTWORK_BROWSER_CACHE_TTL = 7 * 24 * 60 * 60
ARTWORK_MAX_DOWNLOAD_BYTES = 5 * 1024 * 1024
# Longest-edge pixel budgets for the resized variants Melodarr serves. Providers
# return images far larger than any Melodarr layout: artist art arrives at
# 1000x1000 (~690 KB) for cards that render it at 44 CSS pixels.
ARTWORK_SIZES = {"thumb": 128, "card": 384, "large": 640}
ARTWORK_WEBP_QUALITY = 80

DATABASE = _path_from_environment(
    "MELODARR_DATABASE",
    os.path.join(PROJECT_ROOT, "melodarr.db"),
)
CACHE_DATABASE = _path_from_environment(
    "MELODARR_CACHE_DATABASE",
    os.path.join(os.path.dirname(os.path.abspath(DATABASE)), "cache", "metadata.db"),
)
SETTINGS_FILE = _path_from_environment(
    "MELODARR_SETTINGS",
    os.path.join(os.path.dirname(os.path.abspath(DATABASE)), "settings.json"),
)
SECRET_KEY_FILE = _path_from_environment(
    "MELODARR_SECRET_KEY_FILE",
    os.path.join(os.path.dirname(os.path.abspath(DATABASE)), "session-secret.key"),
)


def assert_test_storage_isolation():
    """Fail closed unless all mutable test paths use a registered temp root."""
    registered_root = os.getenv("MELODARR_TEST_ROOT")
    if not registered_root or not registered_root.strip():
        raise RuntimeError(
            "TESTING requires MELODARR_TEST_ROOT to register an isolated "
            "temporary directory."
        )

    root = os.path.realpath(os.path.abspath(registered_root))
    temporary_root = os.path.realpath(os.path.abspath(tempfile.gettempdir()))
    try:
        root_is_temporary = (
            root != temporary_root
            and os.path.commonpath((temporary_root, root)) == temporary_root
        )
    except ValueError:
        root_is_temporary = False
    if not root_is_temporary:
        raise RuntimeError(
            "MELODARR_TEST_ROOT must be a dedicated directory beneath the "
            "operating system temporary directory."
        )

    paths = {
        "DATABASE": DATABASE,
        "CACHE_DATABASE": CACHE_DATABASE,
        "SETTINGS_FILE": SETTINGS_FILE,
        "ARTWORK_CACHE_DIRECTORY": ARTWORK_CACHE_DIRECTORY,
        "SECRET_KEY_FILE": SECRET_KEY_FILE,
    }
    unsafe = {}
    for name, path in paths.items():
        resolved = os.path.realpath(os.path.abspath(path))
        try:
            contained = resolved != root and os.path.commonpath((root, resolved)) == root
        except ValueError:
            contained = False
        if not contained:
            unsafe[name] = path
    if unsafe:
        details = ", ".join(f"{name}={path!r}" for name, path in unsafe.items())
        raise RuntimeError(
            "TESTING refused to use mutable paths outside MELODARR_TEST_ROOT: "
            f"{details}"
        )


def load_session_secret():
    """Load or create the persistent key used to sign browser sessions."""
    configured_secret = os.getenv("MELODARR_SECRET_KEY")
    if configured_secret:
        return configured_secret
    if os.path.exists(SECRET_KEY_FILE):
        with open(SECRET_KEY_FILE, encoding="utf-8") as file:
            stored_secret = file.read().strip()
        if stored_secret:
            return stored_secret

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
