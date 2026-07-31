"""Durable, compact listening profiles for grounded AI personalization.

Provider data is normalized once by the background worker. AI requests read the
stored prompt projection and novelty indexes; they never fetch listening
history synchronously or send raw play events and linked-account identities to
an inference provider.
"""

import json
import math
import time

import requests

if __package__:
    from . import recommendations as recommendation_engine
    from .services import lastfm, listenbrainz, plex
    from .storage import (
        get_lastfm_api_key,
        get_listening_profile,
        get_plex_listens,
        get_request_history,
        get_service,
        listening_profile_users,
        record_listening_profile_failure,
        save_listening_profile,
    )
else:  # Support the existing ``python backend/app.py`` entry point.
    import recommendations as recommendation_engine
    from services import lastfm, listenbrainz, plex
    from storage import (
        get_lastfm_api_key,
        get_listening_profile,
        get_plex_listens,
        get_request_history,
        get_service,
        listening_profile_users,
        record_listening_profile_failure,
        save_listening_profile,
    )


SCHEMA_VERSION = 1
PROFILE_INTERVAL = 24 * 60 * 60
SHORT_DAYS = 30
MEDIUM_DAYS = 180
LONG_DAYS = 365
MAX_AFFINITY_ARTISTS = 30
MAX_TAGS_PER_KIND = 18
MAX_REQUESTS = 30
MAX_NOVELTY_ITEMS = 1000
MAX_PROMPT_PROFILE_CHARS = 6000
# Provider tokenizers differ. Three UTF-8-ish characters per token is a
# deliberately conservative provider-neutral regression bound.
MAX_PROMPT_PROFILE_APPROX_TOKENS = 2000

PERIOD_KEYS = ("short", "medium", "long")
SOURCE_CONFIDENCE = {
    "plex": 0.90,
    "lastfm": 0.92,
    "listenbrainz": 0.88,
    "requests": 1.0,
}
SOURCE_CODES = {
    "plex": "px",
    "lastfm": "lf",
    "listenbrainz": "lb",
    "requests": "rq",
}
MOOD_WORDS = frozenset({
    "aggressive", "atmospheric", "bittersweet", "calm", "chill", "dark",
    "dreamy", "energetic", "epic", "happy", "melancholic", "mellow",
    "moody", "relaxing", "romantic", "sad", "uplifting",
})


def _text(value, maximum=120):
    return " ".join(str(value or "").split())[:maximum]


def _integer(value):
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _empty_source():
    return {
        "artists": [],
        "genres": [],
        "styles": [],
        "moods": [],
        "heardArtistIds": [],
        "heardArtistNames": [],
        "heardAlbumIds": [],
        "heardAlbumNames": [],
        "recentRequests": [],
    }


def _user_value(user, key, default=""):
    try:
        return user[key]
    except (KeyError, IndexError, TypeError):
        return default


def _deduplicated(values, limit, *, maximum=120):
    result = []
    seen = set()
    for value in values:
        cleaned = _text(value, maximum)
        key = cleaned.casefold()
        if not cleaned or key in seen:
            continue
        seen.add(key)
        result.append(cleaned)
        if len(result) >= limit:
            break
    return result


def _artist_rows(periods):
    """Merge period lists into cumulative compact artist evidence."""
    merged = {}
    for period in PERIOD_KEYS:
        rows = periods.get(period) or []
        maximum = max((_integer(row.get("count")) for row in rows), default=0)
        for rank, row in enumerate(rows[:100]):
            name = _text(row.get("name"))
            mbid = _text(row.get("id"), 40)
            if not name:
                continue
            key = mbid or name.casefold()
            entry = merged.setdefault(key, {
                "name": name,
                "mbid": mbid,
                "short": 0,
                "medium": 0,
                "long": 0,
                "count": 0,
            })
            count = _integer(row.get("count"))
            normalized_count = count / maximum if maximum else 0
            rank_weight = 1 / math.sqrt(rank + 1)
            entry[period] = max(
                entry[period],
                round(100 * (0.75 * normalized_count + 0.25 * rank_weight)),
            )
            entry["count"] = max(entry["count"], count)
    return sorted(
        merged.values(),
        key=lambda row: (
            row["short"] * 1.0 + row["medium"] * 0.7 + row["long"] * 0.45,
            row["count"],
            row["name"].casefold(),
        ),
        reverse=True,
    )


def _tag_rows(values, limit=MAX_TAGS_PER_KIND):
    merged = {}
    for name, weight in values:
        cleaned = _text(name, 80)
        if not cleaned:
            continue
        try:
            numeric = max(0.0, float(weight or 0))
        except (TypeError, ValueError):
            numeric = 0
        key = cleaned.casefold()
        previous = merged.get(key)
        if previous is None or numeric > previous[1]:
            merged[key] = (cleaned, numeric)
    maximum = max((row[1] for row in merged.values()), default=0)
    return [
        {"name": name, "weight": round(weight / maximum * 100)}
        for name, weight in sorted(
            merged.values(),
            key=lambda row: (-row[1], row[0].casefold()),
        )[:limit]
        if maximum
    ]


def _request_source(user_id):
    rows = get_request_history(user_id, limit=MAX_NOVELTY_ITEMS)
    recent = []
    artist_ids = []
    artist_names = []
    album_ids = []
    album_names = []
    for row in rows:
        name = _text(row["name"])
        artist = _text(row["artist_name"])
        if row["kind"] == "artist":
            artist_ids.append(_text(row["mbid"], 40))
            artist_names.append(name)
            if len(recent) < MAX_REQUESTS and name:
                recent.append(["a", name])
        else:
            album_ids.append(_text(row["mbid"], 40))
            if artist and name:
                album_names.append([artist, name])
            if len(recent) < MAX_REQUESTS and name:
                recent.append(["r", name, artist])
    source = _empty_source()
    source.update({
        "heardArtistIds": _deduplicated(artist_ids, MAX_NOVELTY_ITEMS, maximum=40),
        "heardArtistNames": _deduplicated(
            artist_names, MAX_NOVELTY_ITEMS
        ),
        "heardAlbumIds": _deduplicated(album_ids, MAX_NOVELTY_ITEMS, maximum=40),
        "heardAlbumNames": album_names[:MAX_NOVELTY_ITEMS],
        "recentRequests": recent,
    })
    return source


def _plex_source(user_id, config, *, now):
    server_id = str(config.get("machineIdentifier") or config.get("url") or "")
    listens = get_plex_listens(
        user_id,
        now - LONG_DAYS * 24 * 60 * 60,
        server_id=server_id,
    )
    index = plex.cached_library_index(config)
    artists_index = index.get("artistsByRatingKey") or {}
    albums_index = index.get("releaseGroupsByRatingKey") or {}
    artist_periods = {period: {} for period in PERIOD_KEYS}
    tags = {"genres": {}, "styles": {}, "moods": {}}
    heard_ids = set()
    heard_names = set()
    heard_album_ids = set()
    heard_album_names = set()
    for listen in listens:
        artist = artists_index.get(str(listen["artist_rating_key"])) or {}
        album_key = str(listen["album_rating_key"] or "")
        album = albums_index.get(album_key) if album_key else None
        album = album or {}
        artist_id = _text(artist.get("musicbrainzId"), 40)
        artist_name = _text(artist.get("name"))
        if not artist_name:
            continue
        age_days = max(0, (now - float(listen["played_at"] or 0)) / 86400)
        active_periods = ["long"]
        if age_days <= MEDIUM_DAYS:
            active_periods.append("medium")
        if age_days <= SHORT_DAYS:
            active_periods.append("short")
        identity = artist_id or artist_name.casefold()
        for period in active_periods:
            row = artist_periods[period].setdefault(
                identity,
                {"id": artist_id, "name": artist_name, "count": 0},
            )
            row["count"] += 1
        if artist_id:
            heard_ids.add(artist_id)
        heard_names.add(artist_name)
        album_id = _text(album.get("musicbrainzReleaseGroupId"), 40)
        album_name = _text(album.get("name"))
        album_artist = _text(album.get("artistName")) or artist_name
        if album_id:
            heard_album_ids.add(album_id)
        if album_name and album_artist:
            heard_album_names.add((album_artist, album_name))
        for kind in ("genres", "styles", "moods"):
            for item in (artist, album):
                for tag in item.get(kind, []) or []:
                    cleaned = _text(tag, 80)
                    if cleaned:
                        key = cleaned.casefold()
                        tags[kind][key] = (
                            tags[kind].get(key, [cleaned, 0])[0],
                            tags[kind].get(key, [cleaned, 0])[1] + 1,
                        )
    source = _empty_source()
    source.update({
        "artists": _artist_rows({
            period: list(rows.values()) for period, rows in artist_periods.items()
        }),
        "genres": _tag_rows(tags["genres"].values()),
        "styles": _tag_rows(tags["styles"].values()),
        "moods": _tag_rows(tags["moods"].values()),
        "heardArtistIds": sorted(heard_ids)[:MAX_NOVELTY_ITEMS],
        "heardArtistNames": sorted(
            heard_names, key=str.casefold
        )[:MAX_NOVELTY_ITEMS],
        "heardAlbumIds": sorted(heard_album_ids)[:MAX_NOVELTY_ITEMS],
        "heardAlbumNames": [
            list(value)
            for value in sorted(
                heard_album_names,
                key=lambda value: (value[0].casefold(), value[1].casefold()),
            )[:MAX_NOVELTY_ITEMS]
        ],
    })
    return source


def _lastfm_source(username, api_key):
    periods = {}
    for profile_period, provider_period, limit in (
        ("short", "1month", 50),
        ("medium", "6month", 75),
        ("long", "overall", MAX_NOVELTY_ITEMS),
    ):
        rows = lastfm.get(
            "user.gettopartists",
            username,
            api_key,
            period=provider_period,
            limit=limit,
        ).get("topartists", {}).get("artist", [])
        periods[profile_period] = [
            {
                "id": item.get("mbid"),
                "name": item.get("name"),
                "count": item.get("playcount"),
            }
            for item in rows
        ]
    albums = lastfm.get(
        "user.gettopalbums",
        username,
        api_key,
        period="overall",
        limit=MAX_NOVELTY_ITEMS,
    ).get("topalbums", {}).get("album", [])
    personal_tags = recommendation_engine.lastfm_top_tags(
        username,
        api_key,
        limit=MAX_TAGS_PER_KIND,
    )
    genre_values = []
    mood_values = []
    for rank, item in enumerate(personal_tags):
        name = _text(item.get("name"), 80)
        weight = item.get("count") or (MAX_TAGS_PER_KIND - rank)
        destination = mood_values if name.casefold() in MOOD_WORDS else genre_values
        destination.append((name, weight))
    long_artists = periods["long"]
    source = _empty_source()
    source.update({
        "artists": _artist_rows(periods),
        "genres": _tag_rows(genre_values),
        "moods": _tag_rows(mood_values),
        "heardArtistIds": _deduplicated(
            (item.get("id") for item in long_artists),
            MAX_NOVELTY_ITEMS,
            maximum=40,
        ),
        "heardArtistNames": _deduplicated(
            (item.get("name") for item in long_artists),
            MAX_NOVELTY_ITEMS,
        ),
        "heardAlbumNames": [
            [artist, title]
            for artist, title in (
                (
                    _text((item.get("artist") or {}).get("name")),
                    _text(item.get("name")),
                )
                for item in albums
            )
            if artist and title
        ][:MAX_NOVELTY_ITEMS],
    })
    return source


def _listenbrainz_source(username):
    periods = {}
    for profile_period, provider_range, count in (
        ("short", "month", 50),
        ("medium", "half_yearly", 75),
        ("long", "all_time", MAX_NOVELTY_ITEMS),
    ):
        rows = listenbrainz.top_artists(
            username,
            count=count,
            range_name=provider_range,
        )
        periods[profile_period] = [
            {
                "id": item.get("artist_mbid"),
                "name": item.get("artist_name"),
                "count": item.get("listen_count"),
            }
            for item in rows
        ]
    releases = listenbrainz.top_release_groups(
        username,
        count=MAX_NOVELTY_ITEMS,
        range_name="all_time",
    )
    long_artists = periods["long"]
    source = _empty_source()
    source.update({
        "artists": _artist_rows(periods),
        "heardArtistIds": _deduplicated(
            (item.get("id") for item in long_artists),
            MAX_NOVELTY_ITEMS,
            maximum=40,
        ),
        "heardArtistNames": _deduplicated(
            (item.get("name") for item in long_artists),
            MAX_NOVELTY_ITEMS,
        ),
        "heardAlbumIds": _deduplicated(
            (item.get("release_group_mbid") for item in releases),
            MAX_NOVELTY_ITEMS,
            maximum=40,
        ),
        "heardAlbumNames": [
            [artist, title]
            for artist, title in (
                (
                    _text(item.get("artist_name")),
                    _text(
                        item.get("release_group_name")
                        or item.get("release_name")
                    ),
                )
                for item in releases
            )
            if artist and title
        ][:MAX_NOVELTY_ITEMS],
    })
    return source


def _previous_profile(user_id):
    row = get_listening_profile(user_id)
    if not row:
        return None
    try:
        value = json.loads(row["value"])
    except (TypeError, json.JSONDecodeError):
        return None
    if (
        not isinstance(value, dict)
        or value.get("schemaVersion") != SCHEMA_VERSION
        or not isinstance(value.get("sourceData"), dict)
    ):
        return None
    return value


def _source_state(name, status, refreshed_at, *, now):
    age_days = max(0, (now - float(refreshed_at or now)) / 86400)
    confidence = SOURCE_CONFIDENCE[name]
    if status == "stale":
        confidence *= max(0.2, math.exp(-age_days / 45))
    if status in {"disabled", "unavailable"}:
        confidence = 0
    return {
        "status": status,
        "refreshedAt": round(float(refreshed_at or 0)),
        "confidence": round(confidence, 2),
    }


def _merge_affinities(source_data, sources):
    artists = {}
    tag_groups = {"genres": {}, "styles": {}, "moods": {}}
    for source_name in ("plex", "lastfm", "listenbrainz"):
        state = sources[source_name]
        data = source_data[source_name]
        confidence = float(state["confidence"])
        for item in data["artists"]:
            key = item.get("mbid") or _text(item.get("name")).casefold()
            if not key:
                continue
            row = artists.setdefault(key, {
                "name": _text(item.get("name")),
                "mbid": _text(item.get("mbid"), 40),
                "short": 0.0,
                "medium": 0.0,
                "long": 0.0,
                "count": 0,
                "sourceCount": 0,
            })
            for period in PERIOD_KEYS:
                row[period] += _integer(item.get(period)) * confidence
            row["count"] = max(row["count"], _integer(item.get("count")))
            row["sourceCount"] += 1
        for kind in tag_groups:
            for item in data[kind]:
                name = _text(item.get("name"), 80)
                key = name.casefold()
                if not key:
                    continue
                row = tag_groups[kind].setdefault(
                    key, {"name": name, "weight": 0.0, "sourceCount": 0}
                )
                row["weight"] += _integer(item.get("weight")) * confidence
                row["sourceCount"] += 1

    artist_rows = sorted(
        artists.values(),
        key=lambda row: (
            row["short"] + row["medium"] * 0.7 + row["long"] * 0.45,
            row["sourceCount"],
            row["name"].casefold(),
        ),
        reverse=True,
    )[:MAX_AFFINITY_ARTISTS]
    for row in artist_rows:
        for period in PERIOD_KEYS:
            row[period] = min(100, round(row[period]))
    merged_tags = {}
    for kind, rows in tag_groups.items():
        ranked = sorted(
            rows.values(),
            key=lambda row: (
                -row["weight"],
                -row["sourceCount"],
                row["name"].casefold(),
            ),
        )[:MAX_TAGS_PER_KIND]
        maximum = max((row["weight"] for row in ranked), default=0)
        merged_tags[kind] = [
            {
                "name": row["name"],
                "weight": round(row["weight"] / maximum * 100),
                "sourceCount": row["sourceCount"],
            }
            for row in ranked
            if maximum
        ]
    return artist_rows, merged_tags


def _dynamics(artists, tags, recent_requests):
    short_mass = sum(row["short"] for row in artists)
    long_mass = sum(row["long"] for row in artists)
    recency = round(100 * short_mass / max(short_mass + long_mass, 1))
    enduring = [
        row["name"] for row in artists if row["long"] >= 55
    ][:8]
    rising = [
        row["name"]
        for row in artists
        if row["short"] >= 40 and row["short"] >= row["long"] * 1.2
    ][:8]
    known = {row["name"].casefold() for row in artists if row["long"] >= 20}
    requested_artists = {
        (_text(row[2]) if len(row) > 2 else _text(row[1])).casefold()
        for row in recent_requests
        if len(row) > 1
    }
    exploration = round(
        100 * len(requested_artists - known) / max(len(requested_artists), 1)
    )
    tag_count = sum(len(values) for values in tags.values())
    diversity = min(100, round(len(artists) * 2.5 + tag_count * 2))
    return {
        "recencyBias": recency,
        "exploration": exploration,
        "diversity": diversity,
        "enduringArtists": enduring,
        "risingArtists": rising,
    }


def _novelty(source_data):
    return {
        "heardArtistIds": _deduplicated(
            (
                value
                for source in source_data.values()
                for value in source["heardArtistIds"]
            ),
            MAX_NOVELTY_ITEMS,
            maximum=40,
        ),
        "heardArtistNames": _deduplicated(
            (
                value
                for source in source_data.values()
                for value in source["heardArtistNames"]
            ),
            MAX_NOVELTY_ITEMS,
        ),
        "heardAlbumIds": _deduplicated(
            (
                value
                for source in source_data.values()
                for value in source["heardAlbumIds"]
            ),
            MAX_NOVELTY_ITEMS,
            maximum=40,
        ),
        "heardAlbumNames": [
            list(value)
            for value in dict.fromkeys(
                tuple(pair)
                for source in source_data.values()
                for pair in source["heardAlbumNames"]
                if isinstance(pair, (list, tuple)) and len(pair) == 2
            )
        ][:MAX_NOVELTY_ITEMS],
        # Listening or requesting something means "familiar", never "disliked".
        # No current integration supplies explicit dislike events.
        "negativePreferences": [],
    }


def _prompt_projection(profile, *, now):
    affinities = profile["affinities"]
    novelty = profile["novelty"]
    dynamics = profile["tasteDynamics"]
    projection = {
        "v": SCHEMA_VERSION,
        # a: [artist, short-term, medium-term, long-term, bounded play count]
        "a": [
            [
                row["name"],
                row["short"],
                row["medium"],
                row["long"],
                row["count"],
            ]
            for row in affinities["artists"]
        ],
        # g/st/mo: [genre/style/mood, normalized weight]
        "g": [[row["name"], row["weight"]] for row in affinities["genres"]],
        "st": [[row["name"], row["weight"]] for row in affinities["styles"]],
        "mo": [[row["name"], row["weight"]] for row in affinities["moods"]],
        # d: recency, exploration, diversity, enduring and rising artists.
        "d": [
            dynamics["recencyBias"],
            dynamics["exploration"],
            dynamics["diversity"],
            dynamics["enduringArtists"],
            dynamics["risingArtists"],
        ],
        # rq contains only display names/types, never IDs or timestamps.
        "rq": profile["sourceData"]["requests"]["recentRequests"],
        # nx are familiar-item counts. They are exclusions, not dislikes.
        "nx": [
            max(
                len(novelty["heardArtistIds"]),
                len(novelty["heardArtistNames"]),
            ),
            max(
                len(novelty["heardAlbumIds"]),
                len(novelty["heardAlbumNames"]),
            ),
        ],
        # neg is reserved exclusively for explicit negative preferences.
        "neg": novelty["negativePreferences"],
        # src: [source code, state, age in days, confidence 0-100].
        "src": [
            [
                SOURCE_CODES[name],
                state["status"],
                round(max(0, now - state["refreshedAt"]) / 86400),
                round(state["confidence"] * 100),
            ]
            for name, state in profile["sources"].items()
        ],
    }
    while len(json.dumps(
        projection, ensure_ascii=False, separators=(",", ":")
    )) > MAX_PROMPT_PROFILE_CHARS:
        removable = [
            key for key in ("a", "g", "st", "mo", "rq")
            if projection[key]
        ]
        if not removable:
            break
        largest = max(removable, key=lambda key: len(projection[key]))
        projection[largest].pop()
    return projection


def compact_prompt_json(profile):
    """Return the deterministic low-token projection saved with a profile."""
    projection = profile.get("promptProfile") if isinstance(profile, dict) else None
    if not isinstance(projection, dict):
        return "{}"
    serialized = json.dumps(
        projection,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(serialized) > MAX_PROMPT_PROFILE_CHARS:
        raise ValueError("Stored listening profile exceeds the AI prompt budget")
    return serialized


def build_user_profile(user, *, now=None):
    """Refresh one profile, retaining stale slices when a provider is offline."""
    now = time.time() if now is None else float(now)
    user_id = int(_user_value(user, "id"))
    previous = _previous_profile(user_id) or {}
    previous_data = previous.get("sourceData") or {}
    previous_states = previous.get("sources") or {}
    source_data = {}
    sources = {}
    errors = []

    configurations = {
        "plex": (
            bool(_user_value(user, "plex_id") and get_service("plex")),
            lambda: _plex_source(user_id, get_service("plex"), now=now),
        ),
        "lastfm": (
            bool(_user_value(user, "lastfm_username") and get_lastfm_api_key()),
            lambda: _lastfm_source(
                _text(_user_value(user, "lastfm_username"), 120),
                get_lastfm_api_key(),
            ),
        ),
        "listenbrainz": (
            bool(_user_value(user, "listenbrainz_username")),
            lambda: _listenbrainz_source(
                _text(_user_value(user, "listenbrainz_username"), 120)
            ),
        ),
    }
    for name, (enabled, loader) in configurations.items():
        if not enabled:
            source_data[name] = _empty_source()
            sources[name] = _source_state(name, "disabled", 0, now=now)
            continue
        try:
            source_data[name] = loader()
            sources[name] = _source_state(name, "fresh", now, now=now)
        except (KeyError, TypeError, ValueError, requests.RequestException) as exc:
            errors.append(f"{name}: {type(exc).__name__}")
            prior = previous_data.get(name)
            if isinstance(prior, dict):
                source_data[name] = prior
                prior_refreshed = (
                    (previous_states.get(name) or {}).get("refreshedAt") or 0
                )
                sources[name] = _source_state(
                    name, "stale", prior_refreshed, now=now
                )
            else:
                source_data[name] = _empty_source()
                sources[name] = _source_state(name, "unavailable", 0, now=now)

    source_data["requests"] = _request_source(user_id)
    sources["requests"] = _source_state("requests", "fresh", now, now=now)
    artists, tags = _merge_affinities(source_data, sources)
    recent_requests = source_data["requests"]["recentRequests"]
    profile = {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": round(now),
        "sources": sources,
        "sourceData": source_data,
        "affinities": {
            "artists": artists,
            **tags,
        },
        "tasteDynamics": _dynamics(artists, tags, recent_requests),
        "novelty": _novelty(source_data),
    }
    profile["promptProfile"] = _prompt_projection(profile, now=now)
    return profile, errors


def refresh_user_profile(user, *, now=None):
    """Build and atomically publish a user's profile."""
    now = time.time() if now is None else float(now)
    user_id = int(_user_value(user, "id"))
    try:
        profile, errors = build_user_profile(user, now=now)
        save_listening_profile(
            user_id,
            profile,
            refreshed_at=now,
            last_attempted_at=now,
            last_error="; ".join(errors) or None,
        )
        return bool(errors)
    except Exception as exc:
        record_listening_profile_failure(user_id, exc, attempted_at=now)
        raise


def refresh_all_profiles(*, now=None):
    """Refresh every user independently; one failure cannot mix or block users."""
    retry_required = False
    for user in listening_profile_users():
        try:
            retry_required = refresh_user_profile(user, now=now) or retry_required
        except Exception:
            retry_required = True
    return retry_required


def stored_profile_context(user_id):
    """Return prompt and exclusion context for exactly one user."""
    profile = _previous_profile(user_id)
    if not profile:
        return None
    novelty = profile.get("novelty") or {}
    affinities = profile.get("affinities") or {}
    artists = [
        {
            "name": row.get("name", ""),
            "playCount": _integer(row.get("count")),
        }
        for row in affinities.get("artists", [])[:MAX_AFFINITY_ARTISTS]
        if row.get("name")
    ]
    tags = [
        row.get("name")
        for kind in ("genres", "styles", "moods")
        for row in affinities.get(kind, [])
        if row.get("name")
    ][:MAX_TAGS_PER_KIND]
    return {
        "artists": artists,
        "tags": tags,
        "heardArtistIds": set(novelty.get("heardArtistIds") or []),
        "heardArtistNames": {
            _text(value).casefold()
            for value in novelty.get("heardArtistNames") or []
            if _text(value)
        },
        "heardAlbumIds": set(novelty.get("heardAlbumIds") or []),
        "heardAlbumNames": {
            (_text(value[0]).casefold(), _text(value[1]).casefold())
            for value in novelty.get("heardAlbumNames") or []
            if isinstance(value, (list, tuple))
            and len(value) == 2
            and _text(value[0])
            and _text(value[1])
        },
        "promptProfile": profile["promptProfile"],
        "profileGeneratedAt": profile.get("generatedAt"),
        "profileStatus": (
            "stale"
            if any(
                state.get("status") in {"stale", "unavailable"}
                for state in (profile.get("sources") or {}).values()
            )
            else "ready"
        ),
    }


def fallback_profile_context(user_id):
    """Build a request-only, no-network fallback for a first-run account."""
    requests_source = _request_source(user_id)
    return {
        "artists": [],
        "tags": [],
        "heardArtistIds": set(requests_source["heardArtistIds"]),
        "heardArtistNames": {
            value.casefold() for value in requests_source["heardArtistNames"]
        },
        "heardAlbumIds": set(requests_source["heardAlbumIds"]),
        "heardAlbumNames": {
            (value[0].casefold(), value[1].casefold())
            for value in requests_source["heardAlbumNames"]
        },
        "promptProfile": {
            "v": SCHEMA_VERSION,
            "a": [],
            "g": [],
            "st": [],
            "mo": [],
            "d": [0, 0, 0, [], []],
            "rq": requests_source["recentRequests"],
            "nx": [
                len(requests_source["heardArtistIds"]),
                len(requests_source["heardAlbumIds"]),
            ],
            "neg": [],
            "src": [["rq", "fresh", 0, 100]],
        },
        "profileGeneratedAt": None,
        "profileStatus": "pending",
    }
