"""Internal URLs for artwork served by Melodarr."""

from urllib.parse import quote


def _sized(path, size):
    return f"{path}?size={size}" if size else path


def release_group_cover_art(mbid, size="thumb"):
    return _sized(f"/api/artwork/release-group/{quote(mbid)}", size)


def artist_cover_art(mbid, size="thumb"):
    return _sized(f"/api/artwork/artist/{quote(mbid)}", size)


def artist_large_cover_art(mbid, size="large"):
    return _sized(f"/api/artwork/artist/{quote(mbid)}", size)
