"""Discovery, recommendations, charts, and search routes."""

import json
import logging
import re
import unicodedata

import requests
from flask import Blueprint, jsonify, request

if __package__ == "backend.routes":
    from .. import (
        recommendation_activity,
        recommendation_feed,
        recommendation_preferences,
        track_search_index,
    )
    from .. import recommendations as recommendation_engine
    from ..media_urls import artist_cover_art, release_group_cover_art
    from ..responses import api_error
    from ..security import current_user, login_required
    from ..services import animethemes, charts, lastfm, musicbrainz, plex
    from ..storage import (
        get_lastfm_api_key,
        get_recommendation_cache,
        get_service,
    )
    from ..workers import artist_metadata as artist_metadata_worker
    from ..workers import recommendations as recommendation_worker
else:  # Support the existing `python backend/app.py` entry point.
    import recommendation_activity
    import recommendation_feed
    import recommendation_preferences
    import recommendations as recommendation_engine
    import track_search_index
    from media_urls import artist_cover_art, release_group_cover_art
    from responses import api_error
    from security import current_user, login_required
    from services import animethemes, charts, lastfm, musicbrainz, plex
    from storage import get_lastfm_api_key, get_recommendation_cache, get_service
    from workers import artist_metadata as artist_metadata_worker
    from workers import recommendations as recommendation_worker


blueprint = Blueprint("discovery", __name__)
logger = logging.getLogger(__name__)

_SEARCH_RESULT_LIMIT = 25
_DUPLICATE_TITLE_LIMIT = 2
_INFERRED_ARTIST_QUERY_BOOST = 2
_MAX_ALBUM_SEARCH_CHARACTERS = 200
_MAX_ALBUM_QUERY_INTERPRETATIONS = 8
_GENERIC_ALBUM_TITLES = {
    "anthology",
    "best of",
    "collection",
    "greatest hits",
    "singles",
    "the best of",
    "the collection",
    "the singles",
}
_PRIMARY_RELEASE_TYPE_RANK = {
    "single": 0,
    "album": 1,
    "ep": 2,
    "broadcast": 3,
    "other": 4,
}
_TRACK_VERSION_INTENTS = {
    "radio edit": ("radio edit",),
    "instrumental": ("instrumental",),
    "acoustic": ("acoustic", "unplugged"),
    "remaster": ("remaster", "remastered"),
    "remix": ("remix",),
    "live": ("live",),
    "demo": ("demo",),
}
_ALBUM_INTENT_ALIASES = {
    "soundtrack": ("soundtrack", "ost"),
    "compilation": ("compilation",),
    "album": ("album", "lp"),
    "ep": ("ep",),
    "single": ("single",),
}
_ISRC_PATTERN = re.compile(
    r"^(?:ISRC[\s:-]*)?([A-Z]{2})[-\s]?([A-Z0-9]{3})[-\s]?(\d{2})[-\s]?(\d{5})$",
    re.IGNORECASE,
)


def _normalize_search_text(value):
    """Return a punctuation-insensitive string for deterministic comparisons."""
    decomposed = unicodedata.normalize("NFKD", str(value or "").casefold())
    without_marks = "".join(
        character
        for character in decomposed
        if not unicodedata.combining(character)
    )
    return " ".join(re.findall(r"[^\W_]+", without_marks, flags=re.UNICODE))


def _album_query_interpretations(query):
    """Return the literal query plus bounded, likely title/artist splits."""
    words = str(query or "").split()
    interpretations = [(str(query or "").strip(), "")]
    if len(words) < 2:
        return interpretations
    # Prefer short artist suffixes. They are the most common qualified-album
    # shape and keep pasted or garbage input from producing quadratic URLs.
    split_points = list(range(len(words) - 1, 0, -1))[
        :_MAX_ALBUM_QUERY_INTERPRETATIONS - 1
    ]
    for split_at in split_points:
        interpretations.append((
            " ".join(words[:split_at]),
            " ".join(words[split_at:]),
        ))
    return interpretations


def _lucene_phrase(value):
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _lucene_all_words(value):
    return "(" + " AND ".join(
        _lucene_phrase(word) for word in _normalize_search_text(value).split()
    ) + ")"


def _lucene_fuzzy_words(value):
    """Allow one edit on substantial title words without broad fuzzy matching."""
    return "(" + " AND ".join(
        f"{word}~1" if len(word) >= 4 else _lucene_phrase(word)
        for word in _normalize_search_text(value).split()
    ) + ")"


def _album_musicbrainz_query(query):
    """Build one fielded query when an artist suffix can be inferred safely."""
    interpretations = _album_query_interpretations(query)
    if len(interpretations) == 1:
        return query, True
    literal_query = interpretations[0][0]
    clauses = [
        f"(releasegroup:{_lucene_phrase(literal_query)} OR "
        f"releasegroup:{_lucene_all_words(literal_query)} OR "
        f"releasegroup:{_lucene_fuzzy_words(literal_query)})"
    ]
    clauses.extend(
        f"(releasegroup:{_lucene_phrase(title)} AND "
        f"artist:{_lucene_phrase(artist)})"
        for title, artist in interpretations[1:]
    )
    return " OR ".join(clauses), False


def _album_intents(query):
    words = set(_normalize_search_text(query).split())
    return tuple(
        intent
        for intent, aliases in _ALBUM_INTENT_ALIASES.items()
        if words.intersection(aliases)
    )


def _album_base_title(query, intents):
    words = _normalize_search_text(query).split()
    ignored = {
        alias
        for intent in intents
        for alias in _ALBUM_INTENT_ALIASES[intent]
    }
    base = " ".join(word for word in words if word not in ignored)
    return base or _normalize_search_text(query)


def _release_group_intents_satisfied(group, intents):
    if not intents:
        return True
    primary = _normalize_search_text(group.get("primary-type"))
    secondary = {
        _normalize_search_text(value)
        for value in group.get("secondary-types") or []
    }
    descriptive_words = set(_normalize_search_text(" ".join((
        str(group.get("title") or ""),
        str(group.get("disambiguation") or ""),
        " ".join(str(value) for value in group.get("secondary-types") or []),
    ))).split())
    checks = {
        "soundtrack": "soundtrack" in secondary
        or bool(descriptive_words.intersection({"soundtrack", "ost"})),
        "compilation": "compilation" in secondary or primary == "compilation",
        "album": primary == "album",
        "ep": primary == "ep",
        "single": primary == "single",
    }
    return all(checks[intent] for intent in intents)


def _normalized_isrc(query):
    match = _ISRC_PATTERN.fullmatch(str(query or "").strip())
    return "".join(match.groups()).upper() if match else ""


def _explicit_track_artist(query):
    """Split only unambiguous song/artist separators."""
    value = str(query or "").strip()
    match = re.fullmatch(r"(.+?)\s+[-–—]\s+(.+?)", value)
    if not match:
        match = re.fullmatch(r"(.+?)\s+by\s+(.+?)", value, flags=re.IGNORECASE)
        if (
            match
            and len(match.group(2).split()) < 2
            and " by " not in value
        ):
            match = None
    if not match:
        return value, ""
    title, artist = (part.strip() for part in match.groups())
    return (title, artist) if title and artist else (value, "")


def _track_version_intents(query):
    normalized = _normalize_search_text(query)
    padded = f" {normalized} "
    return tuple(
        intent
        for intent, aliases in _TRACK_VERSION_INTENTS.items()
        if any(f" {alias} " in padded for alias in aliases)
    )


def _track_base_title(query, intents):
    normalized = _normalize_search_text(query)
    for intent in intents:
        for alias in _TRACK_VERSION_INTENTS[intent]:
            normalized = re.sub(
                rf"(?:^|\s){re.escape(alias)}(?:$|\s)",
                " ",
                normalized,
            )
    return " ".join(normalized.split()) or _normalize_search_text(query)


def _track_search_plan(query):
    """Describe one MusicBrainz query and the local ranking intent."""
    isrc = _normalized_isrc(query)
    if isrc:
        return {
            "query": f"isrc:{isrc}",
            "plainSearch": False,
            "title": "",
            "artist": "",
            "versions": (),
            "isrc": isrc,
            "literalTitle": "",
            "interpretations": (),
            "resolvedArtists": (),
        }

    title, artist = _explicit_track_artist(query)
    interpretations = [{
        "title": str(query).strip(),
        "artist": "",
        "source": "literal",
    }]
    if artist:
        interpretations.append({
            "title": title,
            "artist": artist,
            "source": "explicit",
        })
    else:
        words = str(query).split()
        interpretations.extend(
            {
                "title": " ".join(words[:split_at]),
                "artist": " ".join(words[split_at:]),
                "source": "inferred",
            }
            for split_at in range(1, len(words))
        )

    search_query = query
    plain_search = True
    artist_interpretations = [
        interpretation
        for interpretation in interpretations
        if interpretation["artist"]
    ]
    if artist_interpretations:
        artist_clauses = [
            f"((recording:{_lucene_phrase(interpretation['title'])} OR "
            f"release:{_lucene_phrase(interpretation['title'])}) AND "
            f"(artist:{_lucene_phrase(interpretation['artist'])} OR "
            f"artistname:{_lucene_phrase(interpretation['artist'])}))"
            for interpretation in artist_interpretations
        ]
        if artist:
            search_query = " OR ".join(artist_clauses)
        else:
            literal_clause = (
                f"(recording:{_lucene_phrase(query)} OR "
                f"recording:{_lucene_all_words(query)})"
            )
            boosted_artist_clauses = [
                f"({clause}^{_INFERRED_ARTIST_QUERY_BOOST})"
                for clause in artist_clauses
            ]
            search_query = " OR ".join([
                literal_clause,
                *boosted_artist_clauses,
            ])
        plain_search = False
    versions = _track_version_intents(title)
    return {
        "query": search_query,
        "plainSearch": plain_search,
        "title": title,
        "artist": artist,
        "versions": versions,
        "isrc": "",
        "literalTitle": str(query).strip(),
        "interpretations": tuple(interpretations),
        "resolvedArtists": (),
    }


def _text_match_quality(query, value):
    query = _normalize_search_text(query)
    value = _normalize_search_text(value)
    if not query or not value:
        return 0
    if value == query:
        return 4
    if value.startswith(f"{query} "):
        return 3
    if f" {query} " in f" {value} ":
        return 2
    query_words = query.split()
    value_words = set(value.split())
    if len(query_words) > 1 and all(word in value_words for word in query_words):
        return 1
    return 0


def _release_group_artist_names(group):
    names = []
    for credit in group.get("artist-credit") or []:
        artist = credit.get("artist") or {}
        names.extend((
            credit.get("name"),
            artist.get("name"),
            artist.get("sort-name"),
        ))
        names.extend(alias.get("name") for alias in artist.get("aliases") or [])
    return [name for name in names if name]


def _release_group_title_quality(group, query):
    titles = [group.get("title")]
    titles.extend(alias.get("name") for alias in group.get("aliases") or [])
    return max(
        (_text_match_quality(query, title) for title in titles if title),
        default=0,
    )


def _release_group_artist_quality(group, query):
    return max(
        (_text_match_quality(query, name) for name in _release_group_artist_names(group)),
        default=0,
    )


def _release_group_search_score(group):
    try:
        return max(0, min(100, int(group.get("score") or 0)))
    except (TypeError, ValueError):
        return 0


def _release_group_type_quality(group):
    primary_type = _normalize_search_text(group.get("primary-type"))
    secondary_types = {
        _normalize_search_text(value)
        for value in group.get("secondary-types") or []
    }
    quality = {
        "album": 3,
        "ep": 2,
        "single": 1,
    }.get(primary_type, 0)
    if "soundtrack" in secondary_types:
        quality += 1
    if "compilation" in secondary_types:
        quality -= 1
    return quality


def _release_group_year(group):
    match = re.match(r"\d{4}", str(group.get("first-release-date") or ""))
    return int(match.group()) if match else 0


def _release_group_rank(group, interpretations, position):
    """Sort by title, artist, MusicBrainz score, type, then release year."""
    match = max(
        (
            _release_group_title_quality(group, title_query),
            _release_group_artist_quality(group, artist_query) if artist_query else 0,
        )
        for title_query, artist_query in interpretations
    )
    return (
        -match[0],
        -match[1],
        -_release_group_search_score(group),
        -_release_group_type_quality(group),
        -_release_group_year(group),
        position,
        str(group.get("id") or ""),
    )


def _diversify_release_group_titles(groups):
    """Keep repeated normalized titles from consuming the first result page."""
    first_page_indices = []
    title_counts = {}
    for position, group in enumerate(groups):
        title_key = _normalize_search_text(group.get("title"))
        if title_key and title_counts.get(title_key, 0) >= _DUPLICATE_TITLE_LIMIT:
            continue
        first_page_indices.append(position)
        title_counts[title_key] = title_counts.get(title_key, 0) + 1
        if len(first_page_indices) == _SEARCH_RESULT_LIMIT:
            break

    target_size = min(_SEARCH_RESULT_LIMIT, len(groups))
    selected = set(first_page_indices)
    if len(selected) < target_size:
        for position in range(len(groups)):
            if position in selected:
                continue
            first_page_indices.append(position)
            selected.add(position)
            if len(selected) == target_size:
                break
    return [
        *(groups[position] for position in first_page_indices),
        *(group for position, group in enumerate(groups) if position not in selected),
    ]


def _rank_release_groups(groups, query):
    """Rerank MusicBrainz candidates without discarding its relevance score."""
    interpretations = _album_query_interpretations(query)
    ranked = sorted(
        enumerate(groups),
        key=lambda item: _release_group_rank(item[1], interpretations, item[0]),
    )
    return _diversify_release_group_titles([group for _, group in ranked])


def _local_album_interpretations(query):
    """Return literal and locally validated title/artist interpretations."""
    literal_intents = _album_intents(query)
    interpretations = [{
        "title": str(query).strip(),
        "baseTitle": _album_base_title(query, literal_intents),
        "artist": "",
        "artistMbid": "",
        "intents": literal_intents,
        "source": "literal",
    }]
    for title, artist in _album_query_interpretations(query)[1:]:
        resolution = track_search_index.resolve_artist(artist)
        if resolution["status"] != "unique":
            continue
        intents = _album_intents(title)
        interpretations.append({
            "title": title,
            "baseTitle": _album_base_title(title, intents),
            "artist": artist,
            "artistMbid": resolution["mbid"],
            "intents": intents,
            "source": "artist-qualified",
        })
    return interpretations


def _local_album_match(group, interpretation):
    title_quality = max(
        _release_group_title_quality(group, interpretation["title"]),
        _release_group_title_quality(group, interpretation["baseTitle"]),
    )
    artist_quality = 0
    artist_mbid = interpretation["artistMbid"]
    if artist_mbid:
        if artist_mbid not in _artist_credit_ids(group):
            return None
        artist_quality = 4
    intents_satisfied = _release_group_intents_satisfied(
        group,
        interpretation["intents"],
    )
    return {
        "titleQuality": title_quality,
        "artistQuality": artist_quality,
        "intentsSatisfied": intents_satisfied,
        "intentCount": len(interpretation["intents"]),
    }


def _local_album_rank(group, match):
    return (
        -match["titleQuality"],
        -match["artistQuality"],
        -match["intentCount"],
        -_release_group_type_quality(group),
        -_release_group_year(group),
        str(group.get("id") or ""),
    )


def _confident_local_album_match(match):
    if not match or not match["intentsSatisfied"]:
        return False
    if match["titleQuality"] == 4:
        return True
    return bool(
        match["titleQuality"] >= 3
        and (match["artistQuality"] == 4 or match["intentCount"])
    )


def _local_album_resolution(query):
    """Return ranked local groups only when the strongest match is confident."""
    candidates = {}
    for interpretation in _local_album_interpretations(query):
        groups = track_search_index.search_release_groups(
            interpretation["baseTitle"],
            interpretation["artistMbid"],
        )
        for group in groups:
            match = _local_album_match(group, interpretation)
            if (
                match is None
                or not match["titleQuality"]
                or not match["intentsSatisfied"]
            ):
                continue
            rank = _local_album_rank(group, match)
            existing = candidates.get(group["id"])
            if existing is None or rank < existing["rank"]:
                candidates[group["id"]] = {
                    "group": group,
                    "match": match,
                    "rank": rank,
                    "interpretation": interpretation,
                }
    ranked = sorted(candidates.values(), key=lambda candidate: candidate["rank"])
    if not ranked or not _confident_local_album_match(ranked[0]["match"]):
        return None
    groups = _diversify_release_group_titles([
        candidate["group"] for candidate in ranked
    ])[:100]
    results = [
        {
            "id": group["id"],
            "name": group.get("title", "Untitled release"),
            "romanizedTitle": (
                group.get("romanizedTitle")
                or musicbrainz.romanized_release_group_title(group)
            ),
            "artist": _artist_credit_name(group),
            "date": group.get("first-release-date", ""),
            "type": group.get("primary-type") or "Album",
            "secondaryTypes": [
                name for name in group.get("secondary-types") or [] if name
            ],
            "disambiguation": group.get("disambiguation", ""),
            "score": 0,
            "coverArt": release_group_cover_art(group["id"]),
        }
        for group in groups
    ]
    top = ranked[0]
    return {
        "results": results,
        "matchCount": len(ranked),
        "artistMbid": top["interpretation"]["artistMbid"],
        "interpretedTitle": top["interpretation"]["baseTitle"],
        "sourceMask": int(top["group"].get("sourceMask") or 0),
        "discographyArtistIds": tuple(
            top["group"].get("discographyArtistIds") or ()
        ),
    }


def _generic_album_title(value):
    return _normalize_search_text(value) in _GENERIC_ALBUM_TITLES


def _local_album_can_short_circuit(resolution):
    """Use local results only when the query or source establishes confidence."""
    artist_mbid = resolution["artistMbid"]
    if artist_mbid:
        try:
            artist_metadata_worker.request_revalidation(artist_mbid)
        except (OSError, ValueError, requests.RequestException):
            # Search remains useful when optional background freshness state is
            # unavailable; the exact title/artist identity is already strong.
            pass
        return True
    if (
        resolution["matchCount"] != 1
        or _generic_album_title(resolution["interpretedTitle"])
    ):
        return False
    if resolution["sourceMask"] & (
        track_search_index.SOURCE_LIDARR | track_search_index.SOURCE_PLEX
    ):
        return True
    return any(
        artist_metadata_worker.discography_is_fresh(artist_mbid)
        for artist_mbid in resolution["discographyArtistIds"]
    )


def _plex_search_artists():
    """Use the same MBID-indexed Plex records as artist detail pages."""
    config = get_service("plex")
    if not config:
        return {}
    try:
        return plex.cached_library_index(config)["artistsByMbid"]
    except (ValueError, requests.RequestException):
        return {}


def _plex_search_link(artist):
    if not artist:
        return None
    return {
        "url": artist.get("url", ""),
        "plexampUrl": artist.get("plexampUrl", ""),
        "plexGuid": artist.get("plexGuid", ""),
        "guids": artist.get("guids", []),
        "key": artist.get("key", ""),
    }


def _artist_credit_name(entity):
    """Return a readable MusicBrainz artist credit from a search entity."""
    names = [
        str(
            credit.get("name")
            or (credit.get("artist") or {}).get("name")
            or ""
        ).strip()
        for credit in entity.get("artist-credit") or []
    ]
    return " · ".join(name for name in names if name)


def _artist_credit_ids(entity):
    return {
        str((credit.get("artist") or {}).get("id") or "").casefold()
        for credit in entity.get("artist-credit") or []
        if isinstance(credit, dict)
        and (credit.get("artist") or {}).get("id")
    }


def _resolved_artist_mbid(plan, artist_query):
    normalized = _normalize_search_text(artist_query)
    return next((
        resolved["mbid"]
        for resolved in plan.get("resolvedArtists") or ()
        if _normalize_search_text(resolved["artist"]) == normalized
    ), "")


def _with_resolved_artist(plan, interpretation, artist_mbid):
    resolved = {
        "title": interpretation["title"],
        "artist": interpretation["artist"],
        "mbid": artist_mbid.casefold(),
    }
    return {
        **plan,
        "resolvedArtists": (
            *(plan.get("resolvedArtists") or ()),
            resolved,
        ),
    }


def _recording_score(recording):
    try:
        return int(recording.get("score") or 0)
    except (TypeError, ValueError):
        return 0


def _recording_title_quality(recording, title_query, versions=()):
    if not title_query:
        return 0
    base_query = _track_base_title(title_query, versions)
    titles = [recording.get("title")]
    titles.extend(alias.get("name") for alias in recording.get("aliases") or [])
    return max(
        (
            max(
                _text_match_quality(title_query, title),
                _text_match_quality(base_query, title),
            )
            for title in titles
            if title
        ),
        default=0,
    )


def _release_title_quality(release, title_query, versions=()):
    """Score a title against the specific release represented by this row."""
    if not title_query:
        return 0
    base_query = _track_base_title(title_query, versions)
    group = release.get("release-group") or {}
    titles = [release.get("title"), group.get("title")]
    return max(
        (
            max(
                _text_match_quality(title_query, title),
                _text_match_quality(base_query, title),
            )
            for title in titles
            if title
        ),
        default=0,
    )


def _strict_recording_title_match(recording, title_query):
    """Protect true literal titles without erasing meaningful punctuation."""
    query = " ".join(str(title_query or "").casefold().split())
    if not query:
        return False
    titles = [recording.get("title")]
    titles.extend(alias.get("name") for alias in recording.get("aliases") or [])
    return any(
        " ".join(str(title).casefold().split()) == query
        for title in titles
        if title
    )


def _recording_artist_quality(recording, artist_query):
    if not artist_query:
        return 0
    return max(
        (
            _text_match_quality(artist_query, name)
            for name in _release_group_artist_names(recording)
        ),
        default=0,
    )


def _recording_interpretation_quality(recording, release, plan):
    """Choose the strongest validated title/artist interpretation."""
    literal_quality = _recording_title_quality(
        recording,
        plan["literalTitle"],
    )
    best = (literal_quality, literal_quality, literal_quality, 0)
    for interpretation in plan["interpretations"]:
        artist_query = interpretation["artist"]
        if not artist_query:
            continue
        versions = _track_version_intents(interpretation["title"])
        recording_title_quality = _recording_title_quality(
            recording,
            interpretation["title"],
            versions,
        )
        title_quality = max(
            recording_title_quality,
            _release_title_quality(
                release,
                interpretation["title"],
                versions,
            ),
        )
        resolved_mbid = _resolved_artist_mbid(plan, artist_query)
        artist_quality = (
            4
            if resolved_mbid and resolved_mbid in _artist_credit_ids(recording)
            else _recording_artist_quality(recording, artist_query)
        )
        if not title_quality or artist_quality < 2:
            continue
        best = max(
            best,
            (
                title_quality + artist_quality,
                recording_title_quality,
                title_quality,
                artist_quality,
            ),
        )
    return (
        int(
            not plan["artist"]
            and _strict_recording_title_match(recording, plan["literalTitle"])
        ),
        *best,
    )


def _track_release_artist_quality(recording, release, plan):
    """Prefer release groups whose credit supports a validated artist split."""
    group = release.get("release-group") or {}
    release_artist_names = [
        *_release_group_artist_names(group),
        *_release_group_artist_names(release),
    ]
    if not release_artist_names:
        return 0

    quality = 0
    for interpretation in plan["interpretations"]:
        artist_query = interpretation["artist"]
        if not artist_query:
            continue
        versions = _track_version_intents(interpretation["title"])
        title_quality = max(
            _recording_title_quality(
                recording,
                interpretation["title"],
                versions,
            ),
            _release_title_quality(
                release,
                interpretation["title"],
                versions,
            ),
        )
        recording_artist_quality = _recording_artist_quality(
            recording,
            artist_query,
        )
        resolved_mbid = _resolved_artist_mbid(plan, artist_query)
        if resolved_mbid and resolved_mbid in _artist_credit_ids(recording):
            recording_artist_quality = 4
        if not title_quality or recording_artist_quality < 2:
            continue
        release_artist_quality = max(
            (
                _text_match_quality(artist_query, name)
                for name in release_artist_names
            ),
            default=0,
        )
        if resolved_mbid and resolved_mbid in {
            *_artist_credit_ids(group),
            *_artist_credit_ids(release),
        }:
            release_artist_quality = 4
        quality = max(quality, release_artist_quality)
    return quality


def _local_release_group_rank(group):
    secondary_types = {
        _normalize_search_text(value)
        for value in group.get("secondary-types") or []
    }
    if "compilation" in secondary_types:
        secondary_rank = 2
    elif secondary_types:
        secondary_rank = 1
    else:
        secondary_rank = 0
    primary_type = _normalize_search_text(group.get("primary-type") or "other")
    return (
        secondary_rank,
        _PRIMARY_RELEASE_TYPE_RANK.get(primary_type, 5),
        group.get("first-release-date") or "9999",
        str(group.get("id") or ""),
    )


def _local_track_resolution(plan):
    """Resolve exact local title+artist hits without making provider requests."""
    resolved = []
    for interpretation in plan["interpretations"]:
        artist_query = interpretation["artist"]
        if not artist_query:
            continue
        resolution = track_search_index.resolve_artist(artist_query)
        if resolution["status"] != "unique":
            continue
        resolved.append((interpretation, resolution["mbid"]))

    distinct_artists = {artist_mbid for _, artist_mbid in resolved}
    if len(distinct_artists) != 1:
        return {"plan": plan, "results": []}
    artist_mbid = next(iter(distinct_artists))
    matches = []
    chosen_interpretation = None
    for interpretation, _ in sorted(
        resolved,
        key=lambda item: -len(_normalize_search_text(item[0]["title"])),
    ):
        candidate_matches = track_search_index.exact_track_matches(
            artist_mbid,
            interpretation["title"],
        )
        if candidate_matches:
            chosen_interpretation = interpretation
            matches = candidate_matches
            break
    fallback_interpretation = max(
        (interpretation for interpretation, _ in resolved),
        key=lambda item: len(_normalize_search_text(item["title"])),
    )
    resolved_plan = _with_resolved_artist(
        plan,
        chosen_interpretation or fallback_interpretation,
        artist_mbid,
    )
    if not matches or chosen_interpretation is None:
        return {"plan": resolved_plan, "results": []}

    group_ids = {match["release_group_mbid"] for match in matches}
    groups = track_search_index.cached_release_groups(group_ids)
    results = []
    for group_id in sorted(group_ids):
        group = groups.get(group_id)
        if not group:
            continue
        artist_name = _artist_credit_name(group)
        if not group.get("title") or not artist_name:
            continue
        results.append({
            "id": group_id,
            "name": group["title"],
            "romanizedTitle": musicbrainz.romanized_release_group_title(group),
            "artist": artist_name,
            "date": group.get("first-release-date") or "",
            "type": group.get("primary-type") or "Other",
            "secondaryTypes": [
                name for name in group.get("secondary-types") or [] if name
            ],
            "disambiguation": group.get("disambiguation") or "",
            "score": 100,
            "matchedTrack": chosen_interpretation["title"],
            "matchedTrackArtist": artist_name,
            "_rank": _local_release_group_rank(group),
        })
    results.sort(key=lambda result: result["_rank"])
    for result in results:
        result.pop("_rank", None)
    return {"plan": resolved_plan, "results": results[:_SEARCH_RESULT_LIMIT]}


def _artist_mbid_recording_query(title, artist_mbid):
    return (
        f"(recording:{_lucene_phrase(title)} OR "
        f"recording:{_lucene_all_words(title)} OR "
        f"release:{_lucene_phrase(title)}) AND arid:{artist_mbid}"
    )


def _has_strong_recording_match(response, plan):
    for recording in response.get("recordings") or []:
        for interpretation in plan["interpretations"]:
            if not interpretation["artist"]:
                continue
            versions = _track_version_intents(interpretation["title"])
            title_quality = max(
                _recording_title_quality(
                    recording,
                    interpretation["title"],
                    versions,
                ),
                max(
                    (
                        _release_title_quality(
                            release,
                            interpretation["title"],
                            versions,
                        )
                        for release in recording.get("releases") or []
                    ),
                    default=0,
                ),
            )
            artist_quality = _recording_artist_quality(
                recording,
                interpretation["artist"],
            )
            if title_quality >= 3 and artist_quality >= 2:
                return True
    return False


def _has_exact_literal_recording(response, plan):
    return any(
        _strict_recording_title_match(recording, plan["literalTitle"])
        for recording in response.get("recordings") or []
    )


def _romanized_release_group_title_quality(group, title_query):
    """Match common spaced Hepburn input to compact local romanization."""
    romanized = (
        group.get("romanizedTitle")
        or musicbrainz.romanized_release_group_title(group)
    )
    if not romanized:
        return 0
    query_tokens = _normalize_search_text(title_query).split()
    value_tokens = _normalize_search_text(romanized).split()
    if not query_tokens or not value_tokens:
        return 0
    query_forms = {
        "".join(query_tokens),
        "".join(
            {"wa": "ha", "e": "he", "o": "wo"}.get(token, token)
            for token in query_tokens
        ),
    }
    value = "".join(value_tokens)
    if value in query_forms:
        return 4
    if any(value.startswith(query) for query in query_forms):
        return 3
    if any(query in value for query in query_forms):
        return 2
    return 0


def _track_release_group_alias_results(plan):
    """Recover romanized single/EP titles absent from the recording index."""
    interpretations = [
        interpretation
        for interpretation in plan["interpretations"]
        if interpretation["artist"]
    ]
    if not interpretations:
        return []
    clauses = []
    for interpretation in interpretations:
        resolved_mbid = _resolved_artist_mbid(plan, interpretation["artist"])
        artist_clause = (
            f"arid:{resolved_mbid}"
            if resolved_mbid
            else (
                f"(artist:{_lucene_phrase(interpretation['artist'])} OR "
                f"artistname:{_lucene_phrase(interpretation['artist'])})"
            )
        )
        clauses.append(
            f"((alias:{_lucene_phrase(interpretation['title'])} AND "
            f"{artist_clause}) OR ({artist_clause} AND "
            f"(primarytype:single OR primarytype:ep)))"
        )
    response = musicbrainz.search(
        " OR ".join(clauses),
        "album",
        plain_search=False,
        limit=100,
    )
    results = []
    for group in response.get("release-groups") or []:
        primary_type = _normalize_search_text(group.get("primary-type"))
        if primary_type not in {"single", "ep"}:
            continue
        validated = []
        for interpretation in interpretations:
            title_quality = max(
                _release_group_title_quality(group, interpretation["title"]),
                _romanized_release_group_title_quality(
                    group,
                    interpretation["title"],
                ),
            )
            resolved_mbid = _resolved_artist_mbid(
                plan,
                interpretation["artist"],
            )
            artist_quality = (
                4
                if resolved_mbid and resolved_mbid in _artist_credit_ids(group)
                else _release_group_artist_quality(
                    group,
                    interpretation["artist"],
                )
            )
            if title_quality < 3 or artist_quality < 2:
                continue
            validated.append((
                title_quality,
                artist_quality,
                len(_normalize_search_text(interpretation["title"])),
                interpretation,
            ))
        if not validated:
            continue
        title_quality, artist_quality, _, interpretation = max(
            validated,
            key=lambda item: item[:3],
        )
        results.append({
            "id": group["id"],
            "name": group.get("title") or interpretation["title"],
            "romanizedTitle": musicbrainz.romanized_release_group_title(group),
            "artist": _artist_credit_name(group),
            "date": group.get("first-release-date") or "",
            "type": group.get("primary-type") or "Single",
            "secondaryTypes": [
                name for name in group.get("secondary-types") or [] if name
            ],
            "disambiguation": group.get("disambiguation") or "",
            "score": _release_group_search_score(group),
            "matchedTrack": interpretation["title"],
            "matchedTrackArtist": _artist_credit_name(group),
            "_rank": (
                -title_quality,
                -artist_quality,
                -_release_group_search_score(group),
                0 if primary_type == "single" else 1,
                group.get("first-release-date") or "9999",
                group["id"],
            ),
        })
    results.sort(key=lambda result: result["_rank"])
    for result in results:
        result.pop("_rank", None)
    return results[:_SEARCH_RESULT_LIMIT]


def _alias_fallback_interpretation(plan):
    explicit = next((
        interpretation
        for interpretation in plan["interpretations"]
        if interpretation["source"] == "explicit"
    ), None)
    if explicit:
        return explicit
    words = _normalize_search_text(plan["literalTitle"]).split()
    if len(words) < 3:
        return None
    return next((
        interpretation
        for interpretation in reversed(plan["interpretations"])
        if interpretation["source"] == "inferred"
    ), None)


def _musicbrainz_alias_resolution(plan):
    interpretation = _alias_fallback_interpretation(plan)
    if interpretation is None:
        return None
    artist_query = interpretation["artist"]
    response = musicbrainz.search(
        artist_query,
        "artist",
        plain_search=True,
        limit=10,
    )
    query_keys = {
        track_search_index.normalize_text(artist_query),
        track_search_index.normalize_text(artist_query).replace(" ", ""),
    }
    matches = []
    for artist in response.get("artists") or []:
        names = [artist.get("name"), artist.get("sort-name")]
        names.extend(
            alias.get("name")
            for alias in artist.get("aliases") or []
            if isinstance(alias, dict)
        )
        romanized = musicbrainz.romanized_artist_name(artist)
        if romanized:
            names.append(romanized)
        artist_keys = {
            value
            for name in names
            if name
            for normalized in [track_search_index.normalize_text(name)]
            for value in (normalized, normalized.replace(" ", ""))
            if value
        }
        if query_keys & artist_keys and artist.get("id"):
            matches.append(artist)
    unique = {artist["id"]: artist for artist in matches}
    if len(unique) != 1:
        return None
    artist = next(iter(unique.values()))
    track_search_index.cache_aliases(artist, artist_query)
    return interpretation, artist["id"]


def _track_version_quality(recording, release, intents):
    if not intents:
        return 0
    group = release.get("release-group") or {}
    version_text = _normalize_search_text(" ".join((
        str(recording.get("title") or ""),
        str(recording.get("disambiguation") or ""),
        str(release.get("title") or ""),
        str(release.get("disambiguation") or ""),
        " ".join(str(value) for value in group.get("secondary-types") or []),
    )))
    padded = f" {version_text} "
    return sum(
        any(
            f" {_normalize_search_text(alias)} " in padded
            for alias in _TRACK_VERSION_INTENTS[intent]
        )
        for intent in intents
    )


def _track_release_rank(
    recording,
    release,
    recording_position,
    release_position,
    plan,
):
    """Prefer title/artist intent before score, then useful official editions."""
    group = release.get("release-group") or {}
    secondary_types = [
        str(name).casefold() for name in group.get("secondary-types") or []
    ]
    if "compilation" in secondary_types:
        secondary_type_rank = 2
    elif secondary_types:
        secondary_type_rank = 1
    else:
        secondary_type_rank = 0

    status = str(release.get("status") or "").casefold()
    status_rank = 0 if status == "official" else (1 if not status else 2)
    primary_type = str(group.get("primary-type") or "other").casefold()
    interpretation_quality = _recording_interpretation_quality(
        recording,
        release,
        plan,
    )
    return (
        *(-value for value in interpretation_quality),
        -_track_release_artist_quality(recording, release, plan),
        -_track_version_quality(recording, release, plan["versions"]),
        -_recording_score(recording),
        secondary_type_rank,
        status_rank,
        _PRIMARY_RELEASE_TYPE_RANK.get(primary_type, 5),
        release.get("date") or recording.get("first-release-date") or "9999",
        recording_position,
        release_position,
    )


def _recording_release_group_candidates(response, plan):
    """Flatten recording releases and retain the best edition per release group."""
    candidates = {}
    for recording_position, recording in enumerate(response.get("recordings") or []):
        for release_position, release in enumerate(recording.get("releases") or []):
            group = release.get("release-group") or {}
            group_id = str(group.get("id") or "").strip()
            if not group_id:
                continue

            rank = _track_release_rank(
                recording,
                release,
                recording_position,
                release_position,
                plan,
            )
            existing = candidates.get(group_id)
            if existing and existing["rank"] <= rank:
                continue

            title = str(
                group.get("title")
                or release.get("title")
                or recording.get("title")
                or "Untitled release"
            ).strip()
            candidates[group_id] = {
                "id": group_id,
                "name": title,
                "artist": (
                    _artist_credit_name(group)
                    or _artist_credit_name(release)
                    or _artist_credit_name(recording)
                ),
                "date": (
                    release.get("date")
                    or recording.get("first-release-date")
                    or ""
                ),
                "type": group.get("primary-type") or "Other",
                "secondaryTypes": [
                    name for name in group.get("secondary-types") or [] if name
                ],
                "disambiguation": "",
                "score": _recording_score(recording),
                "matchedTrack": recording.get("title") or "Untitled track",
                "matchedTrackArtist": _artist_credit_name(recording),
                "rank": rank,
            }

    return sorted(candidates.values(), key=lambda item: item["rank"])[
        :_SEARCH_RESULT_LIMIT
    ]


def _recording_release_group_results(response, plan):
    """Return canonical, ranked release groups reached through recording matches."""
    candidates = _recording_release_group_candidates(response, plan)
    canonical_groups = {}
    if candidates:
        query = " OR ".join(f"rgid:{candidate['id']}" for candidate in candidates)
        try:
            group_response = musicbrainz.search(query, "album")
            canonical_groups = {
                group["id"]: group
                for group in group_response.get("release-groups") or []
                if group.get("id")
            }
        except requests.RequestException as exc:
            # The recording response still contains enough release metadata to
            # offer a useful result if canonical enrichment is temporarily down.
            logger.warning(
                "Could not enrich track search release groups: %s", exc
            )

    results = []
    for candidate in candidates:
        group = canonical_groups.get(candidate["id"], {})
        title = group.get("title") or candidate["name"]
        results.append({
            "id": candidate["id"],
            "name": title,
            "romanizedTitle": musicbrainz.romanized_release_group_title(
                group or {"title": title}
            ),
            "artist": _artist_credit_name(group) or candidate["artist"],
            "date": group.get("first-release-date") or candidate["date"],
            "type": group.get("primary-type") or candidate["type"],
            "secondaryTypes": [
                name
                for name in (
                    group.get("secondary-types")
                    if group.get("secondary-types") is not None
                    else candidate["secondaryTypes"]
                )
                if name
            ],
            "disambiguation": (
                group.get("disambiguation") or candidate["disambiguation"]
            ),
            "score": candidate["score"],
            "matchedTrack": candidate["matchedTrack"],
            "matchedTrackArtist": candidate["matchedTrackArtist"],
        })
    return results


@blueprint.get("/api/recommendations")
@login_required
def recommendations():
    user = current_user()
    username = user["listenbrainz_username"]
    if not username:
        return api_error(
            "Add your ListenBrainz username in the account menu to get recommendations.",
            503,
        )
    try:
        artists, albums = recommendation_engine.listenbrainz_recommendations(username)
        return jsonify({"username": username, "artists": artists, "albums": albums})
    except requests.RequestException:
        return api_error(
            "ListenBrainz recommendations could not be loaded. Try again shortly.",
            502,
        )


@blueprint.get("/api/recommendations/lastfm")
@login_required
def lastfm_recommendations():
    user = current_user()
    api_key = get_lastfm_api_key()
    if not api_key:
        return api_error(
            "Last.fm is not configured. Ask an administrator to add the API key.",
            503,
        )
    if not user["lastfm_username"]:
        return api_error(
            "Add your Last.fm username in Linked accounts to get recommendations.",
            503,
        )
    try:
        artists, albums = recommendation_engine.lastfm_recommendations(
            user["lastfm_username"], api_key
        )
        return jsonify({
            "username": user["lastfm_username"],
            "artists": artists,
            "albums": albums,
        })
    except ValueError as exc:
        return api_error(str(exc), 502)
    except requests.RequestException:
        return api_error(
            "Last.fm recommendations could not be loaded. Try again shortly.",
            502,
        )


@blueprint.get("/api/charts/lastfm")
@login_required
def lastfm_charts():
    api_key = get_lastfm_api_key()
    if not api_key:
        return api_error(
            "Last.fm is not configured. Ask an administrator to add the API key.",
            503,
        )
    try:
        artists_data = lastfm.get(
            "chart.gettopartists",
            "melodarr",
            api_key,
            limit=20,
        )
        artists = [
            {
                "id": artist.get("mbid"),
                "name": artist.get("name"),
                "type": "Last.fm global chart",
                "coverArt": artist_cover_art(artist["mbid"]),
                "url": artist.get("url", ""),
            }
            for artist in artists_data.get("artists", {}).get("artist", [])
            if artist.get("mbid") and artist.get("name")
        ]
        return jsonify({"artists": artists})
    except (ValueError, requests.RequestException):
        return api_error("Last.fm charts could not be loaded. Try again shortly.", 502)


@blueprint.get("/api/charts/lastfm/tags")
@login_required
def lastfm_tags():
    user = current_user()
    api_key = get_lastfm_api_key()
    if not api_key:
        return api_error(
            "Last.fm is not configured. Ask an administrator to add the API key.",
            503,
        )
    if not user["lastfm_username"]:
        return api_error(
            "Add your Last.fm username in Linked accounts to load tag charts.",
            503,
        )
    try:
        tags = recommendation_engine.lastfm_top_tags(
            user["lastfm_username"], api_key, limit=10
        )
        return jsonify({"tags": [tag["name"] for tag in tags[:10] if tag.get("name")]})
    except (ValueError, requests.RequestException):
        return api_error("Last.fm tag charts could not be loaded. Try again shortly.", 502)


@blueprint.get("/api/charts/lastfm/tag-albums")
@login_required
def lastfm_tag_albums():
    user = current_user()
    tag_name = request.args.get("tag", "").strip()
    api_key = get_lastfm_api_key()
    if not api_key:
        return api_error(
            "Last.fm is not configured. Ask an administrator to add the API key.",
            503,
        )
    if not user["lastfm_username"]:
        return api_error(
            "Add your Last.fm username in Linked accounts to load tag charts.",
            503,
        )
    if not tag_name or len(tag_name) > 100:
        return api_error("A valid Last.fm tag is required.")
    try:
        albums = lastfm.get(
            "tag.gettopalbums",
            user["lastfm_username"],
            api_key,
            tag=tag_name,
            limit=10,
        ).get("albums", {}).get("album", [])
        mapped = []
        for album in albums:
            mbid = recommendation_engine.lastfm_album_mbid(
                album, user["lastfm_username"], api_key
            )
            if mbid and album.get("name"):
                mapped.append({
                    "id": mbid,
                    "name": album["name"],
                    "artist": (album.get("artist") or {}).get("name", ""),
                    "type": f"Top {tag_name} album",
                    "date": "",
                    "coverArt": release_group_cover_art(mbid),
                })
        return jsonify({"tag": tag_name, "albums": mapped})
    except (ValueError, requests.RequestException):
        return api_error(f"Last.fm albums for {tag_name} could not be loaded.", 502)


def _personal_snapshot(user_id):
    row = get_recommendation_cache(user_id)
    preferences = recommendation_preferences.preferences_for(user_id)
    payload = json.loads(row["value"]) if row else {}
    # A worker may finish an older build after a taste setting was saved. Never
    # display that build as if it reflects the user's new inputs.
    pending = row is None or payload.get("tasteRevision", 0) != preferences["revision"]
    if pending:
        payload = {"feedVersion": recommendation_feed.FEED_VERSION, "candidates": [], "sections": []}
    if pending or payload.get("feedVersion") != recommendation_feed.FEED_VERSION:
        if not recommendation_worker.running.is_set():
            recommendation_worker.request_refresh()
    return row, preferences, payload, pending


@blueprint.get("/api/discover")
@login_required
def cached_discover():
    user_id = current_user()["id"]
    row, preferences, payload, pending = _personal_snapshot(user_id)
    payload = charts.with_cached_charts(payload)
    payload = recommendation_feed.current_feed(user_id, payload)
    return jsonify({**payload, "pending": pending, "tastePreferences": preferences,
                    "refreshedAt": row["refreshed_at"] if row else 0})


@blueprint.get("/api/discover/charts")
@login_required
def cached_discover_charts():
    user_id = current_user()["id"]
    _, _, payload, _ = _personal_snapshot(user_id)
    result = recommendation_feed.current_feed(user_id, charts.with_cached_charts(payload))
    return jsonify({key: result[key] for key in
                    ("popularChart", "popularCharts", "popularAlbums", "popularAlbumsByCountry")})


@blueprint.get("/api/discover/preferences")
@login_required
def discover_preferences():
    return jsonify(recommendation_preferences.preferences_for(current_user()["id"]))


@blueprint.post("/api/discover/preferences")
@login_required
def save_discover_preferences():
    try:
        result = recommendation_preferences.save_preferences(current_user()["id"], request.get_json(silent=True))
        return jsonify({**result, "message": "Taste preferences saved. Your picks are being refreshed."})
    except ValueError as exc:
        return api_error(str(exc))


@blueprint.post("/api/discover/request-influence")
@login_required
def request_influence():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return api_error("A request preference is required.")
    try:
        found = recommendation_preferences.set_request_influence(
            current_user()["id"], body.get("requestId"), body.get("useForRecommendations"))
    except ValueError as exc:
        return api_error(str(exc))
    if not found:
        return api_error("Request not found.", 404)
    return jsonify({"message": "Request preference saved. Your picks are being refreshed."})


@blueprint.post("/api/discover/refresh")
@login_required
def refresh_discover():
    recommendation_worker.request_refresh()
    return jsonify({"message": "Refreshing your picks. New suggestions will appear when ready."}), 202


@blueprint.post("/api/discover/activity")
@login_required
def discover_activity():
    body = request.get_json(silent=True)
    events = body.get("events") if isinstance(body, dict) else None
    if not isinstance(events, list) or not 1 <= len(events) <= 24:
        return api_error("Send between 1 and 24 recommendation events.")
    user_id = current_user()["id"]
    row = get_recommendation_cache(user_id)
    payload = charts.with_cached_charts(json.loads(row["value"]) if row else {})
    candidates = {(item["kind"], item["id"]): item for item in [
        *payload.get("candidates", []), *payload.get("popularCandidates", []),
    ]}
    resolved = []
    for event in events:
        if not isinstance(event, dict):
            return api_error("Invalid recommendation event.")
        action, kind, mbid = event.get("action"), event.get("kind"), event.get("id")
        if (not isinstance(action, str) or action not in {"impression", "open", "listen", "dismiss", "more", "undo"}
                or not isinstance(kind, str) or kind not in {"artist", "release-group"}
                or not isinstance(mbid, str) or not 1 <= len(mbid) <= 100):
            return api_error("Invalid recommendation event.")
        item = candidates.get((kind, mbid))
        if item is None and action == "undo":
            item = recommendation_activity.prior_item(user_id, kind, mbid)
        if item is None:
            return api_error("This suggestion has expired. Refresh your recommendations.", 409)
        resolved.append((item, action))
    for item, action in resolved:
        recommendation_activity.record_activity(user_id, item, action)
    if any(action in {"dismiss", "more", "undo"} for _, action in resolved):
        recommendation_worker.request_refresh()
    return jsonify({"ok": True})


@blueprint.get("/api/discover/metrics")
@login_required
def discover_metrics():
    return jsonify(recommendation_activity.metrics_for(current_user()["id"]))


@blueprint.get("/api/search")
@login_required
def search():
    query = request.args.get("q", "").strip()
    search_type = request.args.get("type", "artist")
    if len(query) < 2:
        return api_error("Enter at least two characters.")
    if search_type not in {"artist", "album", "track", "anime"}:
        return api_error("Search type must be artist, album, track, or anime.")
    if search_type == "anime":
        try:
            return jsonify({"results": animethemes.search(query), "type": search_type})
        except ValueError as exc:
            return api_error(str(exc))
        except requests.RequestException:
            return api_error(
                "AnimeThemes could not be reached. Try again shortly.", 502
            )
    if search_type == "album" and (
        len(query) > _MAX_ALBUM_SEARCH_CHARACTERS
        or len(_normalize_search_text(query)) > _MAX_ALBUM_SEARCH_CHARACTERS
    ):
        return api_error(
            f"Album searches must be {_MAX_ALBUM_SEARCH_CHARACTERS} "
            "characters or fewer."
        )
    search_query = query
    plain_search = True
    track_plan = None
    if search_type == "album":
        if request.args.get("musicbrainz") != "1":
            local_resolution = _local_album_resolution(query)
            if (
                local_resolution
                and _local_album_can_short_circuit(local_resolution)
            ):
                return jsonify({
                    "results": local_resolution["results"],
                    "type": search_type,
                    "candidateCount": len(local_resolution["results"]),
                    "source": "local",
                })
        search_query, plain_search = _album_musicbrainz_query(query)
    elif search_type == "track":
        track_plan = _track_search_plan(query)
        local_resolution = _local_track_resolution(track_plan)
        track_plan = local_resolution["plan"]
        if (
            local_resolution["results"]
            and request.args.get("musicbrainz") != "1"
        ):
            return jsonify({
                "results": local_resolution["results"],
                "type": search_type,
                "candidateCount": len(local_resolution["results"]),
                "source": "local",
            })
        search_query = track_plan["query"]
        plain_search = track_plan["plainSearch"]
        if track_plan["resolvedArtists"]:
            resolved = track_plan["resolvedArtists"][-1]
            search_query = _artist_mbid_recording_query(
                resolved["title"],
                resolved["mbid"],
            )
            plain_search = False
    search_options = {"plain_search": plain_search}
    if track_plan and any(
        interpretation["artist"]
        for interpretation in track_plan["interpretations"]
    ):
        search_options["limit"] = 50
    try:
        response = musicbrainz.search(
            search_query,
            search_type,
            **search_options,
        )
    except requests.RequestException as exc:
        message = musicbrainz.search_error_message(exc)
        return api_error(
            message or "MusicBrainz could not be reached. Try again shortly.",
            502,
        )

    if (
        search_type == "track"
        and not _has_strong_recording_match(response, track_plan)
        and not _has_exact_literal_recording(response, track_plan)
    ):
        try:
            alias_results = _track_release_group_alias_results(track_plan)
            if alias_results:
                return jsonify({
                    "results": alias_results,
                    "type": search_type,
                    "candidateCount": len(alias_results),
                    "source": "musicbrainz",
                })
        except requests.RequestException:
            # Release-group aliases are an optional recovery path. Continue
            # with the existing artist-alias resolution when it is unavailable.
            pass
        if not track_plan["resolvedArtists"]:
            try:
                alias_resolution = _musicbrainz_alias_resolution(track_plan)
                if alias_resolution:
                    interpretation, artist_mbid = alias_resolution
                    track_plan = _with_resolved_artist(
                        track_plan,
                        interpretation,
                        artist_mbid,
                    )
                    response = musicbrainz.search(
                        _artist_mbid_recording_query(
                            interpretation["title"],
                            artist_mbid,
                        ),
                        "track",
                        plain_search=False,
                        limit=50,
                    )
            except requests.RequestException:
                # Alias resolution is an optional recovery path. Preserve the
                # original recording results when either fallback request fails.
                pass

    if search_type == "artist":
        plex_artists = _plex_search_artists()
        results = [
            {
                "id": artist["id"],
                "name": artist.get("name", "Unknown artist"),
                "romanizedName": musicbrainz.romanized_artist_name(artist),
                "disambiguation": artist.get("disambiguation", ""),
                "country": artist.get("country", ""),
                "type": artist.get("type", ""),
                "score": artist.get("score", 0),
                "coverArt": artist_cover_art(artist["id"]),
                "plex": _plex_search_link(plex_artists.get(artist["id"])),
            }
            for artist in response.get("artists", [])
        ]
    elif search_type == "album":
        candidates = response.get("release-groups", [])
        track_search_index.index_release_groups(candidates)
        ranked_albums = _rank_release_groups(candidates, query)
        results = [
            {
                "id": album["id"],
                "name": album.get("title", "Untitled release"),
                "romanizedTitle": musicbrainz.romanized_release_group_title(album),
                "artist": _artist_credit_name(album),
                "date": album.get("first-release-date", ""),
                "type": album.get("primary-type", "Album"),
                "secondaryTypes": [
                    name for name in album.get("secondary-types") or [] if name
                ],
                "disambiguation": album.get("disambiguation", ""),
                "score": album.get("score", 0),
            }
            for album in ranked_albums
        ]
    else:
        results = _recording_release_group_results(response, track_plan)
    payload = {
        "results": results,
        "type": search_type,
        "candidateCount": len(results),
    }
    if search_type in {"album", "track"}:
        payload["source"] = "musicbrainz"
    return jsonify(payload)
