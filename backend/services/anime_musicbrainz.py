"""Conservative AnimeThemes song-to-MusicBrainz mapping.

The resolver deliberately favors a missing mapping over a plausible but wrong
one.  AnimeThemes song identifiers and normalized artist credits form the
stable cache identity, so one resolved theme can be reused by every anime that
features the same song.
"""

import json
import re
import time
import unicodedata
from uuid import UUID

import requests

if __package__ == "backend.services":
    from ..api_cache import get_cache_document, set_cache_document
    from ..media_urls import release_group_cover_art
    from ..storage import db
    from . import anime_mapping_registry, musicbrainz
else:  # Support the existing ``python backend/app.py`` entry point.
    from api_cache import get_cache_document, set_cache_document
    from media_urls import release_group_cover_art
    from services import anime_mapping_registry, musicbrainz
    from storage import db


CACHE_SCHEMA_VERSION = 2
CACHE_NAMESPACE = f"anime-musicbrainz-mapping-v{CACHE_SCHEMA_VERSION}"
RESOLVED_CACHE_TTL = 30 * 24 * 60 * 60
AMBIGUOUS_CACHE_TTL = 7 * 24 * 60 * 60
UNMATCHED_CACHE_TTL = 24 * 60 * 60
FAILED_CACHE_TTL = 60 * 60
MAX_SOURCE_ARTISTS = 3
MAX_ARTIST_CANDIDATES = 3
MAX_RECORDING_SEARCHES = 3
MAX_RECORDING_CANDIDATES = 5
MAX_RELEASE_GROUP_CANDIDATES = 8
RELEASE_GROUP_BROWSE_LIMIT = 100
MAX_RELEASE_GROUP_BROWSE_PAGES = 3

_PRIMARY_TYPE_RANK = {
    "single": 0,
    "ep": 1,
    "album": 2,
    "broadcast": 3,
    "other": 4,
}
_LOW_PRIORITY_SECONDARY_TYPES = {"compilation", "live", "djmix"}
_UNKNOWN_ARTIST_NAMES = {"unknown", "unknownartist"}
_VERSION_MARKERS = (
    "live",
    "cover",
    "remix",
    "karaoke",
    "instrumental",
    "rerecorded",
    "rerecording",
    "acoustic",
    "tvsize",
    "tvedit",
    "tvversion",
    "shortversion",
)
_HEPBURN_LONG_VOWELS = {
    "ā": ("aa",),
    "ī": ("ii",),
    "ū": ("uu",),
    "ē": ("ei", "ee"),
    "ō": ("ou", "oo"),
}

# These two mappings were verified against the corresponding MusicBrainz
# release-group pages. They intentionally match the observed AnimeThemes title
# and complete artist credit instead of guessing or depending on numeric song
# IDs. A match is promoted under the numeric ID supplied by AnimeThemes at
# runtime using the registry's insert-if-absent path, so local choices win.
_VERIFIED_SEEDS = (
    {
        "title": "Shayou",
        "artists": ("Yorushika",),
        "target": {
            "releaseGroupId": "6259b4f8-39b2-4b46-98e0-5dd433630abc",
            "releaseGroupTitle": "斜陽",
            "artistName": "ヨルシカ",
            "primaryType": "Single",
            "firstReleaseDate": "2023-05-08",
            "recordingIds": [],
            "artistIds": ["dfc6a151-3792-4695-8fda-f64723eaa788"],
            "preferred": True,
        },
    },
    {
        "title": "Suu Sentimental",
        "artists": ("Lam Kohana",),
        "target": {
            "releaseGroupId": "7dc293e7-192e-4762-9217-db117a2ea705",
            "releaseGroupTitle": "数センチメンタル",
            "artistName": "こはならむ",
            "primaryType": "Single",
            "firstReleaseDate": "2023-04-10",
            "recordingIds": [],
            "artistIds": ["cb9266b4-8537-4687-8763-5129c583be53"],
            "preferred": True,
        },
    },
)


def normalize_text(value):
    """Return a comparison-only Unicode, case, and punctuation-normalized key."""
    value = unicodedata.normalize("NFKC", str(value or "")).casefold()
    value = "".join(
        character
        for character in unicodedata.normalize("NFKD", value)
        if not unicodedata.combining(character)
    )
    return "".join(character for character in value if character.isalnum())


def _is_latin_text(value):
    letters = [character for character in str(value or "") if character.isalpha()]
    return bool(letters) and all(
        "LATIN" in unicodedata.name(character, "") for character in letters
    )


def _title_comparison_keys(value):
    """Return exact title keys plus standard Hepburn long-vowel spellings."""
    folded = unicodedata.normalize("NFKC", str(value or "")).casefold()
    variants = {folded}
    for character, replacements in _HEPBURN_LONG_VOWELS.items():
        if character not in folded:
            continue
        variants.update(
            variant.replace(character, replacement)
            for variant in tuple(variants)
            for replacement in replacements
        )
    return {normalize_text(variant) for variant in variants if normalize_text(variant)}


def _song(theme):
    song = theme.get("song") if isinstance(theme, dict) else None
    return song if isinstance(song, dict) else {}


def _artist_names(theme):
    names = []
    for artist in _song(theme).get("artists") or []:
        if not isinstance(artist, dict):
            continue
        name = str(artist.get("name") or "").strip()
        if name and normalize_text(name) not in {normalize_text(item) for item in names}:
            names.append(name)
    return names


def _has_missing_artist_credit(artists):
    """Return whether artist credits are absent or explicitly unknown."""
    return not artists or any(
        normalize_text(name) in _UNKNOWN_ARTIST_NAMES for name in artists
    )


def theme_mapping_key(theme):
    """Build the version-independent document id for a normalized theme."""
    song = _song(theme)
    source_song_id = str(song.get("id") or "").strip()
    if not source_song_id:
        source_song_id = f"title:{normalize_text(song.get('title'))}"
    credits = sorted({normalize_text(name) for name in _artist_names(theme) if name})
    return json.dumps(
        {"sourceSongId": source_song_id, "credits": credits},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def cached_mapping(theme):
    """Read a fresh cached mapping for one theme, if present."""
    return _usable_automatic_mapping(get_cache_document(CACHE_NAMESPACE, theme_mapping_key(theme)))


def cache_mapping(theme, mapping):
    """Persist a mapping with a state-appropriate refresh interval."""
    if mapping.get("mappingSource") in {"local", "seed"}:
        return
    ttl = {
        "resolved": RESOLVED_CACHE_TTL,
        "ambiguous": AMBIGUOUS_CACHE_TTL,
        "unmatched": UNMATCHED_CACHE_TTL,
        "failed": FAILED_CACHE_TTL,
    }.get(mapping.get("state"), FAILED_CACHE_TTL)
    if mapping.get("reason") == "artist-recording-catalog-incomplete":
        ttl = 5 * 60
    with db() as connection:
        connection.execute(
            "INSERT INTO anime_automatic_matches VALUES (?, ?, ?, ?) "
            "ON CONFLICT(mapping_key) DO UPDATE SET payload=excluded.payload, "
            "retry_at=excluded.retry_at, updated_at=excluded.updated_at "
            "WHERE anime_automatic_matches.retry_at IS NOT NULL OR excluded.retry_at IS NULL",
            (theme_mapping_key(theme), json.dumps(mapping),
             None if mapping.get("state") == "resolved" else time.time() + ttl, time.time()),
        )
    set_cache_document(CACHE_NAMESPACE, theme_mapping_key(theme), mapping, ttl)
    confirm_unique_single(theme, mapping)


def _mapping_base(theme):
    song = _song(theme)
    return {
        "schemaVersion": CACHE_SCHEMA_VERSION,
        "themeKey": theme_mapping_key(theme),
        "sourceSongId": str(song.get("id") or "").strip(),
        "songTitle": str(song.get("title") or "").strip(),
        "sourceArtists": _artist_names(theme),
        "recordingId": "",
        "recordingTitle": "",
        "artistIds": [],
        "confidence": 0,
        "matchMethod": "",
        "releaseGroups": [],
    }


def _result(theme, state, reason="", **changes):
    result = {**_mapping_base(theme), "state": state, "reason": reason}
    result.update(changes)
    return result


def _document_value(document, *names, default=None):
    if not isinstance(document, dict):
        return default
    for name in names:
        if name in document:
            return document[name]
    return default


def _registry_song_id(theme):
    """Return a valid observed AnimeThemes song ID, without guessing one."""
    value = _song(theme).get("id")
    if isinstance(value, bool):
        return None
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _verified_seed_for(theme):
    title = normalize_text(_song(theme).get("title"))
    artists = tuple(sorted(normalize_text(name) for name in _artist_names(theme)))
    for seed in _VERIFIED_SEEDS:
        seed_artists = tuple(
            sorted(normalize_text(name) for name in seed["artists"])
        )
        if title == normalize_text(seed["title"]) and artists == seed_artists:
            return seed
    return None


def _registry_target_card(target, source_title="", default_scope="unknown"):
    group_id = str(
        _document_value(
            target,
            "releaseGroupId",
            "releaseGroupMbid",
            "release_group_id",
            "release_group_mbid",
            default="",
        )
        or ""
    ).strip()
    title = str(
        _document_value(
            target,
            "releaseGroupTitle",
            "release_group_title",
            default=source_title,
        )
        or source_title
    ).strip()
    source_romanization = (
        str(source_title).strip()
        if source_title
        and _is_latin_text(source_title)
        and not _is_latin_text(title)
        and normalize_text(source_title) != normalize_text(title)
        else ""
    )
    return {
        "id": group_id,
        "name": title,
        "title": title,
        "romanizedTitle": source_romanization
        or musicbrainz.romanized_release_group_title({"title": title}),
        "artist": str(
            _document_value(
                target,
                "artistName",
                "artist_name",
                default="",
            )
            or ""
        ).strip(),
        "date": str(
            _document_value(
                target,
                "firstReleaseDate",
                "first_release_date",
                default="",
            )
            or ""
        ),
        "type": str(
            _document_value(
                target,
                "primaryType",
                "primary_type",
                default="Other",
            )
            or "Other"
        ),
        "mappingScope": str(
            _document_value(
                target,
                "scope",
                "mappingScope",
                "mapping_scope",
                default=default_scope,
            )
            or default_scope
        ),
        "secondaryTypes": [],
        "releaseId": "",
        "coverArt": release_group_cover_art(group_id),
        "preferred": bool(
            _document_value(target, "preferred", "is_preferred", default=False)
        ),
    }


def _mapping_from_registry(theme, document):
    """Project durable registry state into the existing resolver response."""
    provenance = str(document.get("provenance") or "").strip()
    mapping_source = "seed" if provenance == "builtin-seed" else "local"
    match_method = "verified-seed" if mapping_source == "seed" else "local-registry"
    source_title = str(document.get("title") or _song(theme).get("title") or "")
    preferred_document = document.get("preferredTarget") or {}
    preferred_id = str(
        _document_value(
            preferred_document,
            "releaseGroupId",
            "releaseGroupMbid",
            "release_group_id",
            "release_group_mbid",
            default="",
        )
        or ""
    )
    target_documents = [
        target
        for target in document.get("targets") or []
        if isinstance(target, dict)
    ]
    target_documents.sort(
        key=lambda target: (
            0
            if str(
                _document_value(
                    target,
                    "releaseGroupId",
                    "releaseGroupMbid",
                    "release_group_id",
                    "release_group_mbid",
                    default="",
                )
            )
            == preferred_id
            else 1,
            str(
                _document_value(
                    target,
                    "releaseGroupId",
                    "releaseGroupMbid",
                    "release_group_id",
                    "release_group_mbid",
                    default="",
                )
            ),
        )
    )
    groups = [
        _registry_target_card(
            target,
            source_title,
            str(document.get("scope") or "unknown"),
        )
        for target in target_documents
        if _document_value(
            target,
            "releaseGroupId",
            "releaseGroupMbid",
            "release_group_id",
            "release_group_mbid",
        )
    ]
    status = str(document.get("status") or "").casefold()
    if status == "rejected":
        state = "unmatched"
        reason = "registry-rejected"
    elif status == "confirmed" and groups:
        state = "resolved"
        reason = ""
    else:
        state = "ambiguous"
        reason = "registry-proposed"
    preferred = target_documents[0] if target_documents else {}
    recording_ids = _document_value(
        preferred,
        "recordingIds",
        "recordingMbids",
        "recording_ids",
        "recording_mbids",
        default=[],
    )
    if isinstance(recording_ids, str):
        recording_ids = [recording_ids]
    elif not isinstance(recording_ids, (list, tuple)):
        recording_ids = []
    all_recording_ids = set()
    for target in target_documents:
        target_recording_ids = _document_value(
            target,
            "recordingIds",
            "recordingMbids",
            "recording_ids",
            "recording_mbids",
            default=[],
        )
        if isinstance(target_recording_ids, str):
            target_recording_ids = [target_recording_ids]
        if isinstance(target_recording_ids, (list, tuple)):
            all_recording_ids.update(
                str(value) for value in target_recording_ids if value
            )
    artist_ids = _document_value(
        preferred,
        "artistIds",
        "artistMbids",
        "artist_ids",
        "artist_mbids",
        default=[],
    )
    if isinstance(artist_ids, str):
        artist_ids = [artist_ids]
    elif not isinstance(artist_ids, (list, tuple)):
        artist_ids = []
    return _result(
        theme,
        state,
        reason,
        recordingId=(str(recording_ids[0]) if len(recording_ids) == 1 else ""),
        recordingIds=sorted(all_recording_ids),
        recordingTitle=source_title,
        artistIds=sorted({str(value) for value in artist_ids if value}),
        confidence=100 if state == "resolved" else 0,
        matchMethod=match_method,
        mappingSource=mapping_source,
        registryStatus=status,
        registryProvenance=provenance,
        mappingScope=str(document.get("scope") or ""),
        releaseGroups=groups,
    )


def registered_mapping(theme):
    """Return a permanent local mapping, lazily promoting verified seeds."""
    song_id = _registry_song_id(theme)
    if song_id is None:
        return None
    document = anime_mapping_registry.get_mapping(song_id)
    if document is None:
        seed = _verified_seed_for(theme)
        if seed is not None:
            target = seed["target"]
            document = anime_mapping_registry.create_mapping_if_absent(
                song_id,
                title=seed["title"],
                artists=list(seed["artists"]),
                status="confirmed",
                provenance="builtin-seed",
                scope="commercial_full",
                targets=[target],
                preferred_release_group_mbid=target["releaseGroupId"],
            )
    return _mapping_from_registry(theme, document) if document is not None else None


def confirm_unique_single(theme, mapping):
    """Apply the single-release policy without overwriting any reviewed decision."""
    if not mapping or mapping.get("state") != "resolved" or mapping.get("reason"):
        return mapping
    if mapping.get("mappingSource") or mapping.get("registryStatus"):
        return mapping
    if mapping.get("matchMethod") not in {"recording-search", "artist-discography-title"}:
        return mapping
    song_id = _registry_song_id(theme)
    groups = mapping.get("releaseGroups") or []
    if song_id is None or not groups or any(
        not isinstance(group, dict) or str(group.get("type") or "").casefold() not in {"single", "album", "ep"}
        for group in groups
    ):
        return mapping
    singles = {group.get("id"): group for group in groups if str(group.get("type")).casefold() == "single"}
    if len(singles) != 1:
        return mapping
    group = next(iter(singles.values()))
    recording_id = mapping.get("recordingId")
    if mapping.get("matchMethod") == "recording-search" and not recording_id:
        return mapping
    try:
        group_id = str(UUID(str(group.get("id"))))
        recording_ids = [str(UUID(str(recording_id)))] if recording_id else []
        artist_ids = [str(UUID(str(value))) for value in mapping.get("artistIds") or []]
    except ValueError:
        return mapping
    if not artist_ids:
        return mapping
    document = anime_mapping_registry.create_mapping_if_absent(
        song_id, title=_song(theme).get("title") or "Untitled song",
        artists=_artist_names(theme), status="confirmed", provenance="automatic-unique-single",
        scope="commercial_full", preferred_release_group_mbid=group_id,
        targets=[{"releaseGroupId": group_id, "recordingIds": recording_ids,
                  "artistIds": artist_ids, "releaseGroupTitle": group.get("title") or group.get("name") or "",
                  "artistName": group.get("artist") or " · ".join(_artist_names(theme)),
                  "primaryType": "Single", "firstReleaseDate": group.get("date") or "",
                  "scope": "commercial_full", "preferred": True}],
    )
    if document.get("provenance") == "automatic-unique-single":
        # Keep the original recording evidence and alternatives for inspection.
        with db() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO anime_automatic_matches VALUES (?, ?, NULL, ?)",
                (theme_mapping_key(theme), json.dumps(mapping), time.time()),
            )
    return _mapping_from_registry(theme, document)


def stored_mapping(theme):
    """Read permanent local state before the expiring automated cache."""
    manual = registered_mapping(theme)
    if manual:
        return manual
    mapping = saved_automatic_mapping(theme) or cached_mapping(theme)
    return confirm_unique_single(theme, mapping)


def saved_automatic_mapping(theme):
    """Read original automatic evidence without promoting or changing review state."""
    with db() as connection:
        row = connection.execute(
            "SELECT payload FROM anime_automatic_matches WHERE mapping_key=? "
            "AND (retry_at IS NULL OR retry_at>?)",
            (theme_mapping_key(theme), time.time()),
        ).fetchone()
    return _usable_automatic_mapping(json.loads(row["payload"])) if row else None


def _usable_automatic_mapping(mapping):
    # The former hard cap could never succeed on a retry; discard only that
    # obsolete negative result, in both durable and disposable caches.
    if mapping and mapping.get("reason") == "artist-recording-browse-limit":
        return None
    return mapping


def failed_mapping(theme, reason="provider-error"):
    """Return a safe failure document without serializing provider details."""
    return _result(theme, "failed", reason)


def pending_mapping(theme, *, queued=False):
    """Return a transient public mapping for work not yet persisted."""
    return _result(theme, "pending", "", queued=bool(queued))


def _entity_names(entity):
    names = {
        normalize_text(entity.get("name")),
        normalize_text(entity.get("sort-name") or entity.get("sortName")),
    }
    for alias in entity.get("aliases") or []:
        if isinstance(alias, dict):
            names.add(normalize_text(alias.get("name")))
    romanized = musicbrainz.romanized_artist_name(entity)
    if romanized:
        names.add(normalize_text(romanized))
    names.discard("")
    return names


def _title_names(recording):
    names = set(_title_comparison_keys(recording.get("title")))
    for alias in recording.get("aliases") or []:
        if isinstance(alias, dict):
            names.update(_title_comparison_keys(alias.get("name")))
    romanized = musicbrainz.romanized_release_group_title(recording)
    if romanized:
        names.update(_title_comparison_keys(romanized))
    names.discard("")
    return names


def _score(entity):
    try:
        return max(0, min(100, int(entity.get("score") or 0)))
    except (TypeError, ValueError):
        return 0


def _lucene_phrase(value):
    value = re.sub(r"([\\\"])", r"\\\1", str(value or ""))
    return f'"{value}"'


def _search(query, search_type):
    response = musicbrainz.search(query, search_type, priority="background")
    if not isinstance(response, dict):
        raise ValueError("MusicBrainz returned an invalid search response")
    return response


def _artist_candidates(name):
    target = normalize_text(name)
    phrase = _lucene_phrase(name)
    # AnimeThemes commonly uses a Latin artist name while MusicBrainz stores
    # the native-script name as canonical (for example Yorushika / ヨルシカ).
    # Search every exact-name index in one request, then retain only candidates
    # whose returned canonical name, sort name, or alias is an exact match.
    response = _search(
        f"(artist:{phrase} OR alias:{phrase} OR sortname:{phrase})",
        "artist",
    )
    matches = []
    for artist in response.get("artists") or []:
        if not isinstance(artist, dict) or not artist.get("id"):
            continue
        if target not in _entity_names(artist):
            continue
        matches.append({
            "id": str(artist["id"]),
            "name": str(artist.get("name") or name).strip(),
            "score": _score(artist),
            "names": _entity_names(artist),
        })
    matches.sort(key=lambda item: (-item["score"], item["id"]))
    return matches[:MAX_ARTIST_CANDIDATES]


def _recording_credits(recording):
    credits = []
    for credit in recording.get("artist-credit") or []:
        if not isinstance(credit, dict):
            continue
        artist = credit.get("artist") or {}
        names = {
            normalize_text(credit.get("name")),
            normalize_text(artist.get("name")),
            normalize_text(artist.get("sort-name")),
        }
        for alias in artist.get("aliases") or []:
            if isinstance(alias, dict):
                names.add(normalize_text(alias.get("name")))
        names.discard("")
        credits.append({"id": str(artist.get("id") or ""), "names": names})
    return credits


def _credits_match(source_names, source_candidates, recording):
    credits = _recording_credits(recording)
    if len(credits) != len(source_names):
        return False
    unused = set(range(len(credits)))
    for name, candidates in zip(source_names, source_candidates):
        target_name = normalize_text(name)
        candidate_ids = {item["id"] for item in candidates}
        matched = next(
            (
                index
                for index in sorted(unused)
                if credits[index]["id"] in candidate_ids
                or (not any(item.get("verified") for item in candidates)
                    and target_name in credits[index]["names"])
            ),
            None,
        )
        if matched is None:
            return False
        unused.remove(matched)
    return not unused


def _has_version_marker(recording):
    text = " ".join(
        str(value or "")
        for value in (recording.get("title"), recording.get("disambiguation"))
    )
    normalized = normalize_text(text)
    return any(marker in normalized for marker in _VERSION_MARKERS)


def _artist_credit_name(entity):
    names = []
    for credit in entity.get("artist-credit") or []:
        if not isinstance(credit, dict):
            continue
        name = str(
            credit.get("name") or (credit.get("artist") or {}).get("name") or ""
        ).strip()
        if name:
            names.append(name)
    return " · ".join(names)


def _release_group_rank(release, recording):
    group = release.get("release-group") or {}
    secondary = [normalize_text(value) for value in group.get("secondary-types") or []]
    status = normalize_text(release.get("status"))
    status_rank = 0 if status == "official" else (1 if not status else 2)
    secondary_rank = int(bool(_LOW_PRIORITY_SECONDARY_TYPES.intersection(secondary)))
    primary = str(group.get("primary-type") or "other").casefold()
    return (
        status_rank,
        secondary_rank,
        _PRIMARY_TYPE_RANK.get(primary, 5),
        str(release.get("date") or recording.get("first-release-date") or "9999"),
        str(group.get("id") or ""),
    )


def _release_groups(recording):
    candidates = {}
    for release in recording.get("releases") or []:
        if not isinstance(release, dict):
            continue
        group = release.get("release-group") or {}
        group_id = str(group.get("id") or "").strip()
        if not group_id:
            continue
        rank = _release_group_rank(release, recording)
        current = candidates.get(group_id)
        if current and current["_rank"] <= rank:
            continue
        title = str(
            group.get("title") or release.get("title") or recording.get("title") or ""
        ).strip()
        candidates[group_id] = {
            "id": group_id,
            "name": title,
            "title": title,
            "romanizedTitle": musicbrainz.romanized_release_group_title(
                group or {"title": title}
            ),
            "artist": (
                _artist_credit_name(group)
                or _artist_credit_name(release)
                or _artist_credit_name(recording)
            ),
            "date": str(
                release.get("date") or recording.get("first-release-date") or ""
            ),
            "type": str(group.get("primary-type") or "Other"),
            "secondaryTypes": [
                str(value) for value in group.get("secondary-types") or [] if value
            ],
            "releaseId": str(release.get("id") or ""),
            "coverArt": release_group_cover_art(group_id),
            "_rank": rank,
        }
    ranked = sorted(candidates.values(), key=lambda item: item["_rank"])
    for candidate in ranked:
        candidate.pop("_rank", None)
    return ranked[:MAX_RELEASE_GROUP_CANDIDATES]


def _recording_candidate(recording):
    credits = _recording_credits(recording)
    return {
        "recordingId": str(recording.get("id") or ""),
        "recordingTitle": str(recording.get("title") or "").strip(),
        "artist": _artist_credit_name(recording),
        "artistIds": sorted({credit["id"] for credit in credits if credit["id"]}),
        "score": _score(recording),
        "releaseGroups": _release_groups(recording),
    }


def _release_group_card(group):
    group_id = str(group.get("id") or "").strip()
    title = str(group.get("title") or "").strip()
    return {
        "id": group_id,
        "name": title,
        "title": title,
        "romanizedTitle": musicbrainz.romanized_release_group_title(group),
        "artist": _artist_credit_name(group),
        "date": str(group.get("first-release-date") or ""),
        "type": str(group.get("primary-type") or "Other"),
        "secondaryTypes": [
            str(value) for value in group.get("secondary-types") or [] if value
        ],
        "releaseId": "",
        "coverArt": release_group_cover_art(group_id),
    }


def _release_group_entity_rank(group):
    secondary = [normalize_text(value) for value in group.get("secondary-types") or []]
    secondary_rank = int(bool(_LOW_PRIORITY_SECONDARY_TYPES.intersection(secondary)))
    primary = str(group.get("primary-type") or "other").casefold()
    return (
        _PRIMARY_TYPE_RANK.get(primary, 5),
        secondary_rank,
        str(group.get("first-release-date") or "9999"),
        str(group.get("id") or ""),
    )


def _browse_artist_release_groups(artist_id):
    """Return a bounded MusicBrainz release-group discography page set."""
    groups = []
    offset = 0
    for _page in range(MAX_RELEASE_GROUP_BROWSE_PAGES):
        response = musicbrainz.get(
            "/release-group",
            "artist-credits+aliases",
            priority="background",
            artist=artist_id,
            limit=RELEASE_GROUP_BROWSE_LIMIT,
            offset=offset,
        )
        if not isinstance(response, dict):
            raise ValueError("MusicBrainz returned an invalid browse response")
        page_groups = [
            group
            for group in response.get("release-groups") or []
            if isinstance(group, dict) and group.get("id")
        ]
        groups.extend(page_groups)
        offset += len(page_groups)
        try:
            total = int(response.get("release-group-count") or 0)
        except (TypeError, ValueError):
            total = 0
        if not page_groups or len(page_groups) < RELEASE_GROUP_BROWSE_LIMIT:
            break
        if total and offset >= total:
            break
    return groups


def _romanized_release_group_matches(
    title,
    source_artists,
    source_candidates,
):
    """Find exact title/credit matches in an exact artist's discography.

    This is deliberately a fallback rather than a fuzzy search.  It handles
    AnimeThemes' romanized titles when MusicBrainz only has the native-script
    canonical title, but it does not infer a result from similarity alone.
    """
    target_titles = _title_comparison_keys(title)
    groups = {}
    for artist in source_candidates[0][:MAX_ARTIST_CANDIDATES]:
        for group in _browse_artist_release_groups(artist["id"]):
            group_id = str(group.get("id") or "")
            if group_id in groups:
                continue
            if target_titles.isdisjoint(_title_names(group)):
                continue
            if not _credits_match(source_artists, source_candidates, group):
                continue
            if _has_version_marker(group):
                continue
            groups[group_id] = group
    return sorted(groups.values(), key=_release_group_entity_rank)


def _combined_release_groups(recording_candidates):
    """Flatten ambiguous recordings into a stable user-selectable group list."""
    groups = {}
    for candidate in recording_candidates:
        for group in candidate.get("releaseGroups") or []:
            groups.setdefault(group["id"], group)
    ranked = sorted(
        groups.values(),
        key=lambda group: (
            _PRIMARY_TYPE_RANK.get(str(group.get("type") or "other").casefold(), 5),
            int(
                bool(
                    _LOW_PRIORITY_SECONDARY_TYPES.intersection(
                        normalize_text(value)
                        for value in group.get("secondaryTypes") or []
                    )
                )
            ),
            str(group.get("date") or "9999"),
            str(group.get("id") or ""),
        ),
    )
    return ranked[:MAX_RELEASE_GROUP_CANDIDATES]


def _title_only_candidates(theme, title):
    """Return safe user-selectable candidates when artist credits are unknown.

    An exact title alone is never enough to resolve a theme automatically.  It
    only supplies candidates that a user can propose and an admin can confirm.
    """
    response = _search(f"recording:{_lucene_phrase(title)}", "recording")
    target_titles = _title_comparison_keys(title)
    recordings = {}
    for recording in response.get("recordings") or []:
        if not isinstance(recording, dict) or not recording.get("id"):
            continue
        if target_titles.isdisjoint(_title_names(recording)):
            continue
        if _has_version_marker(recording):
            continue
        recording_id = str(recording["id"])
        previous = recordings.get(recording_id)
        if previous is None or _score(recording) > _score(previous):
            recordings[recording_id] = recording

    exact = sorted(
        recordings.values(),
        key=lambda item: (
            -_score(item),
            0 if _release_groups(item) else 1,
            str(item.get("first-release-date") or "9999"),
            str(item.get("id") or ""),
        ),
    )
    candidates = [_recording_candidate(item) for item in exact]
    requestable = [
        item for item in candidates if item["releaseGroups"]
    ][:MAX_RECORDING_CANDIDATES]
    if requestable:
        return _result(
            theme,
            "ambiguous",
            "missing-artist",
            confidence=0,
            matchMethod="title-only-recording-search",
            releaseGroups=_combined_release_groups(requestable),
            recordingCandidates=requestable,
        )
    if candidates:
        return _result(
            theme,
            "unmatched",
            "no-requestable-release",
            recordingCandidates=candidates[:MAX_RECORDING_CANDIDATES],
        )
    return _result(theme, "unmatched", "missing-artist")


def _recording_catalog(artist_id):
    """Advance at most ten pages, saving progress for all songs by this artist."""
    now = time.time()
    with db() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO anime_recording_catalogs (artist_mbid, generation, updated_at) VALUES (?, ?, ?)",
            (artist_id, now, now),
        )
        connection.execute(
            "UPDATE anime_recording_catalogs SET recordings_json='{}', next_offset=0, complete=0, "
            "generation=?, updated_at=? WHERE artist_mbid=? AND complete=1 AND updated_at<?",
            (now, now, artist_id, now - 86400),
        )
    for _page in range(10):
        with db() as connection:
            row = connection.execute("SELECT * FROM anime_recording_catalogs WHERE artist_mbid=?", (artist_id,)).fetchone()
        records = json.loads(row["recordings_json"])
        if row["complete"]:
            return list(records.values()), True
        offset = row["next_offset"]
        response = musicbrainz.get(
            "/recording", "artist-credits+aliases", artist=artist_id,
            limit=100, offset=offset, priority="background", cache_ttl=86400,
        )
        if not isinstance(response, dict) or not isinstance(response.get("recordings"), list):
            raise ValueError("Invalid MusicBrainz recording catalog response")
        page = response["recordings"]
        total = response.get("recording-count")
        next_offset = offset + len(page)
        if not page and total is not None and offset < int(total):
            raise ValueError("Incomplete MusicBrainz recording catalog page")
        complete = next_offset >= int(total) if total is not None else len(page) < 100
        for recording in page:
            if not isinstance(recording, dict) or not recording.get("id"):
                raise ValueError("Invalid MusicBrainz recording")
            records[str(recording["id"])] = recording
        with db() as connection:
            # A simultaneous request may have advanced this catalog already.
            # Never overwrite its progress or a newer catalog generation.
            connection.execute(
                "UPDATE anime_recording_catalogs SET recordings_json=?, next_offset=?, complete=?, updated_at=? "
                "WHERE artist_mbid=? AND next_offset=? AND generation=?",
                (json.dumps(records), next_offset, int(complete), time.time(),
                 artist_id, offset, row["generation"]),
            )
    with db() as connection:
        row = connection.execute("SELECT * FROM anime_recording_catalogs WHERE artist_mbid=?", (artist_id,)).fetchone()
    return list(json.loads(row["recordings_json"]).values()), bool(row["complete"])


def _artist_recording_matches(title, source_artists, source_candidates, artist_id):
    """Compare romaji against a complete, resumable artist recording catalog."""
    records, complete = _recording_catalog(artist_id)
    if not complete:
        return [], False
    target = _title_comparison_keys(title)
    matches = {}
    for recording in records:
        titles = _title_names(recording)
        # Translated English aliases must not hide the native title's reading.
        for native in [recording.get("title"), *[
            alias.get("name") for alias in recording.get("aliases") or [] if isinstance(alias, dict)
        ]]:
            reading = musicbrainz.romanized_release_group_title({"title": native})
            if reading:
                titles.update(_title_comparison_keys(reading))
        if (target.isdisjoint(titles) or _has_version_marker(recording)
                or not _credits_match(source_artists, source_candidates, recording)):
            continue
        matches[str(recording["id"])] = {**recording, "score": 100}
    return list(matches.values()), True


def _resolve_theme_live(theme, verified_artists=None):
    """Resolve one normalized AnimeThemes theme without reading local state."""
    title = str(_song(theme).get("title") or "").strip()
    source_artists = _artist_names(theme)
    if not title:
        return _result(theme, "unmatched", "missing-title")

    try:
        if _has_missing_artist_credit(source_artists):
            return _title_only_candidates(theme, title)
        if len(source_artists) > MAX_SOURCE_ARTISTS:
            return _result(theme, "unmatched", "too-many-artists")
        if verified_artists is None:
            # Page-triggered matching uses the same saved identities as the worker.
            from . import anime_artist_links
            artists = _song(theme).get("artists") or []
            identities = anime_artist_links.musicbrainz_links(artists)
            verified_artists = {artist.get("name"): identities[str(artist["id"])]
                                for artist in artists if str(artist.get("id")) in identities}
        verified_artists = verified_artists or {}
        source_candidates = [
            [{"id": verified_artists[name], "name": name, "score": 100,
              "names": {normalize_text(name)}, "verified": True}]
            if name in verified_artists else _artist_candidates(name)
            for name in source_artists
        ]
        if any(not candidates for candidates in source_candidates):
            return _result(theme, "unmatched", "artist-not-found")

        # Prefer a verified identity even when it is a featured collaborator.
        # Every returned recording is still checked against all source credits.
        search_candidates = next(
            (items for items in source_candidates if any(item.get("verified") for item in items)),
            source_candidates[0],
        )
        recordings = {}
        for artist in search_candidates[:MAX_RECORDING_SEARCHES]:
            query = (
                f"recording:{_lucene_phrase(title)} AND "
                f"arid:{artist['id']}"
            )
            response = _search(query, "recording")
            for recording in response.get("recordings") or []:
                if not isinstance(recording, dict) or not recording.get("id"):
                    continue
                recording_id = str(recording["id"])
                previous = recordings.get(recording_id)
                if previous is None or _score(recording) > _score(previous):
                    recordings[recording_id] = recording

        target_titles = _title_comparison_keys(title)
        exact = [
            recording
            for recording in recordings.values()
            if not target_titles.isdisjoint(_title_names(recording))
            and _credits_match(source_artists, source_candidates, recording)
            and not _has_version_marker(recording)
        ]
        used_artist_recordings = False
        if not exact and verified_artists:
            verified_id = next(item["id"] for item in search_candidates if item.get("verified"))
            exact, complete = _artist_recording_matches(
                title, source_artists, source_candidates, verified_id)
            used_artist_recordings = True
            if not complete:
                return _result(theme, "unmatched", "artist-recording-catalog-incomplete")
        exact.sort(
            key=lambda item: (
                -_score(item),
                0 if _release_groups(item) else 1,
                str(item.get("first-release-date") or "9999"),
                str(item.get("id") or ""),
            )
        )
        if not exact:
            exact_groups = _romanized_release_group_matches(
                title,
                source_artists,
                source_candidates,
            )
            if not exact_groups:
                return _result(theme, "unmatched", "no-exact-recording")

            group_cards = [
                _release_group_card(group)
                for group in exact_groups[:MAX_RELEASE_GROUP_CANDIDATES]
            ]
            if len(exact_groups) > 1:
                return _result(
                    theme,
                    "ambiguous",
                    "multiple-exact-release-groups",
                    matchMethod="artist-discography-title",
                    releaseGroups=group_cards,
                )

            credits = _recording_credits(exact_groups[0])
            return _result(
                theme,
                "resolved",
                "",
                recordingTitle=str(exact_groups[0].get("title") or "").strip(),
                artistIds=sorted(
                    {credit["id"] for credit in credits if credit["id"]}
                ),
                confidence=min(
                    max(item["score"] for item in candidates)
                    for candidates in source_candidates
                ),
                matchMethod="artist-discography-title",
                releaseGroups=group_cards,
                preferredReleaseGroupId=group_cards[0]["id"],
            )

        if verified_artists:
            # Search responses can truncate releases. Browse the selected recording
            # explicitly to find its single and album appearances, with a hard cap.
            for item in exact[:MAX_RECORDING_CANDIDATES]:
                releases = list(item.get("releases") or [])
                for offset in range(0, 300, 100):
                    response = musicbrainz.get(
                        "/release", "release-groups+artist-credits",
                        recording=item["id"], limit=100, offset=offset, priority="background",
                    )
                    page = response.get("releases") or []
                    releases.extend(page)
                    if len(page) < 100:
                        break
                item["releases"] = releases
        candidates = [
            _recording_candidate(item) for item in exact[:MAX_RECORDING_CANDIDATES]
        ]
        requestable = [item for item in candidates if item["releaseGroups"]]
        if not requestable:
            return _result(
                theme,
                "unmatched",
                "no-requestable-release",
                recordingCandidates=candidates,
            )

        top = requestable[0]
        runner_up = requestable[1] if len(requestable) > 1 else None
        decisive = (
            runner_up is None
            or (top["score"] >= 90 and top["score"] - runner_up["score"] >= 15)
        )
        if used_artist_recordings and len(exact) > 1:
            decisive = False
        if not decisive:
            return _result(
                theme,
                "ambiguous",
                "multiple-exact-recordings",
                matchMethod="recording-search",
                releaseGroups=_combined_release_groups(requestable),
                recordingCandidates=requestable,
            )

        return _result(
            theme,
            "resolved",
            "",
            recordingId=top["recordingId"],
            recordingTitle=top["recordingTitle"],
            artistIds=top["artistIds"],
            confidence=top["score"],
            matchMethod="recording-search",
            titleMatchMethod="artist-recording-romanization" if used_artist_recordings else "recording-search",
            sourceSongTitle=title,
            releaseGroups=top["releaseGroups"],
            preferredReleaseGroupId=top["releaseGroups"][0]["id"],
        )
    except (requests.RequestException, ValueError, TypeError):
        return failed_mapping(theme)


def resolve_theme(theme, *, verified_artists=None):
    """Resolve a theme using registry, cache, then live MusicBrainz lookup."""
    existing = stored_mapping(theme)
    return existing if existing is not None else confirm_unique_single(
        theme, _resolve_theme_live(theme, verified_artists=verified_artists))
