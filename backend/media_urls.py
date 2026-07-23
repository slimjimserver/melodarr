"""Internal URLs for artwork served by Melodarr."""

from hashlib import sha256
from urllib.parse import quote


def _sized(path, size):
    return f"{path}?size={size}" if size else path


def release_group_cover_art(mbid, size="thumb"):
    return _sized(f"/api/artwork/release-group/{quote(mbid)}", size)


def artist_cover_art(mbid, size="thumb"):
    return _sized(f"/api/artwork/artist/{quote(mbid)}", size)


def artist_large_cover_art(mbid, size="large"):
    return _sized(f"/api/artwork/artist/{quote(mbid)}", size)


def plex_artist_artwork_version(thumb):
    """Return a short, opaque revision for a Plex thumbnail path."""
    return sha256(str(thumb or "").encode()).hexdigest()[:16]


def plex_artist_artwork(rating_key, thumb):
    """Build a browser-cache-busting URL for a Plex artist thumbnail."""
    if not rating_key or not thumb:
        return ""
    version = plex_artist_artwork_version(thumb)
    return (
        f"/api/artwork/plex-artist/{quote(str(rating_key), safe='')}"
        f"?v={version}"
    )
