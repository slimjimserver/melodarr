"""Internal URLs for artwork served by Melodarr."""

from urllib.parse import quote


def release_group_cover_art(mbid):
    return f"/api/artwork/release-group/{quote(mbid)}"


def artist_cover_art(mbid):
    return f"/api/artwork/artist/{quote(mbid)}"


def artist_large_cover_art(mbid):
    return f"/api/artwork/artist/{quote(mbid)}/large"
