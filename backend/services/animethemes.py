"""Cached AnimeThemes API access and response normalization."""

from urllib.parse import quote

if __package__ == "backend.services":
    from ..api_cache import cached_json_get
    from ..config import USER_AGENT
else:  # Support the existing `python backend/app.py` entry point.
    from api_cache import cached_json_get
    from config import USER_AGENT


_ANIMETHEMES_URL = "https://api.animethemes.moe"
_SEARCH_CACHE_TTL = 6 * 60 * 60
_DETAIL_CACHE_TTL = 24 * 60 * 60
_SERIES_CACHE_TTL = 24 * 60 * 60
_DETAIL_INCLUDE = (
    "animethemes.song.artists,"
    "animethemes.animethemeentries,"
    "images,resources,series"
)
_SERIES_INCLUDE = "anime.images,anime.animethemes"
_MAX_SEARCH_LIMIT = 50
_MAX_QUERY_LENGTH = 200
_MAX_SLUG_LENGTH = 200


def _clean_text(value):
    return str(value or "").strip()


def _validate_query(query):
    if not isinstance(query, str):
        raise ValueError("Anime search query must be text.")
    query = query.strip()
    if len(query) < 2:
        raise ValueError("Enter at least two characters.")
    if len(query) > _MAX_QUERY_LENGTH:
        raise ValueError(
            f"Anime search query must be {_MAX_QUERY_LENGTH} characters or fewer."
        )
    return query


def _validate_limit(limit):
    if isinstance(limit, bool):
        raise ValueError("Anime search limit must be a whole number.")
    try:
        limit = int(limit)
    except (TypeError, ValueError) as exc:
        raise ValueError("Anime search limit must be a whole number.") from exc
    if not 1 <= limit <= _MAX_SEARCH_LIMIT:
        raise ValueError(
            f"Anime search limit must be between 1 and {_MAX_SEARCH_LIMIT}."
        )
    return limit


def _validate_slug(slug):
    if not isinstance(slug, str):
        raise ValueError("Anime slug must be text.")
    slug = slug.strip()
    if not slug or len(slug) > _MAX_SLUG_LENGTH:
        raise ValueError("Invalid AnimeThemes anime slug.")
    if not slug[0].isalnum() or any(
        not (character.isascii() and (character.isalnum() or character in "_-"))
        for character in slug
    ):
        raise ValueError("Invalid AnimeThemes anime slug.")
    return slug


def _validate_series_slug(slug):
    if not isinstance(slug, str):
        raise ValueError("Series slug must be text.")
    slug = slug.strip()
    if not slug or len(slug) > _MAX_SLUG_LENGTH:
        raise ValueError("Invalid AnimeThemes series slug.")
    if not slug[0].isalnum() or any(
        not (character.isascii() and (character.isalnum() or character in "_-"))
        for character in slug
    ):
        raise ValueError("Invalid AnimeThemes series slug.")
    return slug


def _cover_art(images):
    images = [image for image in images or [] if isinstance(image, dict)]
    for wanted_facet in ("large cover", "small cover"):
        match = next(
            (
                image
                for image in images
                if _clean_text(image.get("facet")).casefold() == wanted_facet
                and _clean_text(image.get("link"))
            ),
            None,
        )
        if match:
            return _clean_text(match["link"])
    return next(
        (
            _clean_text(image.get("link"))
            for image in images
            if _clean_text(image.get("link"))
        ),
        "",
    )


def _normalize_summary(anime):
    return {
        "id": anime.get("id"),
        "slug": _clean_text(anime.get("slug")),
        "name": _clean_text(anime.get("name")) or "Untitled anime",
        "year": anime.get("year"),
        "season": _clean_text(anime.get("season")),
        "format": _clean_text(anime.get("media_format")),
        "synopsis": _clean_text(anime.get("synopsis")),
        "coverArt": _cover_art(anime.get("images")),
    }


def _unique_strings(values):
    normalized = []
    seen = set()
    for value in values:
        if isinstance(value, (list, tuple)):
            candidates = value
        else:
            candidates = (value,)
        for candidate in candidates:
            if candidate is None or isinstance(candidate, (dict, list, tuple)):
                continue
            text = _clean_text(candidate)
            key = text.casefold()
            if text and key not in seen:
                seen.add(key)
                normalized.append(text)
    return normalized


def _normalize_artist(artist):
    artist_song = artist.get("artistsong")
    if not isinstance(artist_song, dict):
        artist_song = {}
    credited_as = artist_song.get("as", artist.get("as"))
    return {
        "id": artist.get("id"),
        "name": _clean_text(artist.get("name")) or "Unknown artist",
        "slug": _clean_text(artist.get("slug")),
        "as": _clean_text(credited_as) or None,
    }


def _theme_label(theme_type, sequence):
    names = {"OP": "Opening", "ED": "Ending", "IN": "Insert Song"}
    base = names.get(theme_type, theme_type or "Theme")
    return f"{base} {sequence}" if sequence not in (None, "") else base


def _normalize_theme(theme):
    song = theme.get("song")
    if not isinstance(song, dict):
        song = {}
    artists = [
        _normalize_artist(artist)
        for artist in song.get("artists") or []
        if isinstance(artist, dict)
    ]
    entries = [
        entry
        for entry in theme.get("animethemeentries") or []
        if isinstance(entry, dict)
    ]
    theme_type = _clean_text(theme.get("type")).upper()
    sequence = theme.get("sequence")
    return {
        "id": theme.get("id"),
        "type": theme_type,
        "sequence": sequence,
        "label": _theme_label(theme_type, sequence),
        "slug": _clean_text(theme.get("slug")),
        "song": {
            "id": song.get("id"),
            "title": _clean_text(song.get("title")) or "Untitled song",
            "artists": artists,
        },
        "episodes": _unique_strings(entry.get("episodes") for entry in entries),
        "notes": _unique_strings(entry.get("notes") for entry in entries),
    }


def _normalize_resource(resource):
    return {
        "id": resource.get("id"),
        "site": _clean_text(resource.get("site")),
        "link": _clean_text(resource.get("link")),
        "externalId": resource.get("external_id"),
    }


def _normalize_series(series):
    return {
        "id": series.get("id"),
        "name": _clean_text(series.get("name")) or "Untitled series",
        "slug": _clean_text(series.get("slug")),
    }


def _normalize_series_detail(series):
    anime_items = []
    for source_index, anime in enumerate(series.get("anime") or []):
        if not isinstance(anime, dict) or not _clean_text(anime.get("slug")):
            continue
        summary = _normalize_summary(anime)
        summary["themeCount"] = len([
            theme
            for theme in anime.get("animethemes") or []
            if isinstance(theme, dict)
        ])
        anime_items.append((source_index, summary))

    # AnimeThemes does not guarantee chronology in its API relationship array,
    # while its series page presents productions in broadcast order.
    anime_items.sort(key=lambda item: (
        item[1].get("year") is None,
        item[1].get("year") or 0,
        item[0],
    ))
    return {
        "id": series.get("id"),
        "name": _clean_text(series.get("name")) or "Untitled series",
        "slug": _clean_text(series.get("slug")),
        "anime": [summary for _, summary in anime_items],
    }


def _normalize_detail(anime):
    summary = _normalize_summary(anime)
    return {
        **summary,
        "resources": [
            _normalize_resource(resource)
            for resource in anime.get("resources") or []
            if isinstance(resource, dict)
        ],
        "series": [
            _normalize_series(series)
            for series in anime.get("series") or []
            if isinstance(series, dict)
        ],
        "themes": [
            _normalize_theme(theme)
            for theme in anime.get("animethemes") or []
            if isinstance(theme, dict)
        ],
    }


def search(query, limit=25):
    """Search AnimeThemes and return normalized anime summaries."""
    query = _validate_query(query)
    limit = _validate_limit(limit)
    data = cached_json_get(
        f"{_ANIMETHEMES_URL}/search",
        params={"q": query, "limit": limit, "include[anime]": "images"},
        headers={"User-Agent": USER_AGENT},
        namespace="animethemes-search",
        ttl=_SEARCH_CACHE_TTL,
    )
    search_payload = data.get("search") if isinstance(data, dict) else None
    anime_items = (
        search_payload.get("anime") if isinstance(search_payload, dict) else []
    )
    return [
        _normalize_summary(anime)
        for anime in anime_items or []
        if isinstance(anime, dict) and _clean_text(anime.get("slug"))
    ][:limit]


def detail(slug):
    """Return one normalized anime and its opening/ending metadata."""
    slug = _validate_slug(slug)
    data = cached_json_get(
        f"{_ANIMETHEMES_URL}/anime/{quote(slug, safe='')}",
        params={"include": _DETAIL_INCLUDE},
        headers={"User-Agent": USER_AGENT},
        namespace="animethemes-detail",
        ttl=_DETAIL_CACHE_TTL,
    )
    anime = data.get("anime") if isinstance(data, dict) else None
    return _normalize_detail(anime) if isinstance(anime, dict) else None


def series_detail(slug):
    """Return one normalized AnimeThemes series and its related anime."""
    slug = _validate_series_slug(slug)
    data = cached_json_get(
        f"{_ANIMETHEMES_URL}/series/{quote(slug, safe='')}",
        params={"include": _SERIES_INCLUDE},
        headers={"User-Agent": USER_AGENT},
        namespace="animethemes-series",
        ttl=_SERIES_CACHE_TTL,
    )
    series = data.get("series") if isinstance(data, dict) else None
    return _normalize_series_detail(series) if isinstance(series, dict) else None
