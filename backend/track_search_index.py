"""Compact local identity index for confident music-search fast paths."""

import json
import logging
import os
import re
import sqlite3
import unicodedata

if __package__:
    from .api_cache import cache_db
    from .config import CACHE_DATABASE
else:  # Support the existing `python backend/app.py` entry point.
    from api_cache import cache_db
    from config import CACHE_DATABASE


logger = logging.getLogger(__name__)

# Version 4 can attach legacy release payloads, which omitted `release-group`,
# to one unambiguous cached group by artist and normalized release title.
SCHEMA_VERSION = "4"
SOURCE_MUSICBRAINZ = 1
SOURCE_LIDARR = 2
SOURCE_PLEX = 4
SOURCE_ALIAS_FALLBACK = 8
_initialized = False


def normalize_text(value):
    """Return the same compact, punctuation-insensitive search key as discovery."""
    decomposed = unicodedata.normalize("NFKD", str(value or "").casefold())
    without_marks = "".join(
        character
        for character in decomposed
        if not unicodedata.combining(character)
    )
    return " ".join(re.findall(r"[^\W_]+", without_marks, flags=re.UNICODE))


def _valid_mbid(value):
    return bool(re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        str(value or "").casefold(),
    ))


def initialize():
    """Create the compact index and import already-cached metadata once."""
    global _initialized
    if _initialized:
        return
    with cache_db() as connection:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS track_search_artist_names (
                normalized_name TEXT NOT NULL,
                artist_mbid TEXT NOT NULL,
                source_mask INTEGER NOT NULL,
                PRIMARY KEY (normalized_name, artist_mbid)
            ) WITHOUT ROWID
        """)
        connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_track_search_artist_mbid
            ON track_search_artist_names (artist_mbid)
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS track_search_relations (
                normalized_title TEXT NOT NULL,
                artist_mbid TEXT NOT NULL,
                recording_mbid TEXT NOT NULL,
                release_group_mbid TEXT NOT NULL,
                source_mask INTEGER NOT NULL,
                PRIMARY KEY (
                    normalized_title,
                    artist_mbid,
                    recording_mbid,
                    release_group_mbid
                )
            ) WITHOUT ROWID
        """)
        connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_track_search_artist_title
            ON track_search_relations (artist_mbid, normalized_title)
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS track_search_release_group_refs (
                release_group_mbid TEXT NOT NULL,
                cache_key TEXT NOT NULL,
                PRIMARY KEY (release_group_mbid, cache_key)
            ) WITHOUT ROWID
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS track_search_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            ) WITHOUT ROWID
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS track_search_release_groups (
                release_group_mbid TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                normalized_title TEXT NOT NULL,
                romanized_title TEXT NOT NULL,
                normalized_romanized_title TEXT NOT NULL,
                artist_name TEXT NOT NULL,
                normalized_artist_name TEXT NOT NULL,
                primary_type TEXT NOT NULL,
                secondary_types TEXT NOT NULL,
                first_release_date TEXT NOT NULL,
                disambiguation TEXT NOT NULL,
                source_mask INTEGER NOT NULL,
                metadata_quality INTEGER NOT NULL
            ) WITHOUT ROWID
        """)
        connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_track_search_group_title
            ON track_search_release_groups (normalized_title)
        """)
        connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_track_search_group_romanized
            ON track_search_release_groups (normalized_romanized_title)
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS track_search_release_group_artists (
                release_group_mbid TEXT NOT NULL,
                artist_mbid TEXT NOT NULL,
                credit_name TEXT NOT NULL,
                normalized_credit_name TEXT NOT NULL,
                source_mask INTEGER NOT NULL,
                PRIMARY KEY (release_group_mbid, artist_mbid)
            ) WITHOUT ROWID
        """)
        connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_track_search_group_artist
            ON track_search_release_group_artists
                (artist_mbid, release_group_mbid)
        """)
        row = connection.execute(
            "SELECT value FROM track_search_meta WHERE key = 'schema-version'"
        ).fetchone()
    if row is None or row["value"] != SCHEMA_VERSION:
        rebuild_from_cache()
    _initialized = True


def _artist_names(artist):
    names = {
        str(artist.get("name") or "").strip(),
        str(artist.get("sort-name") or artist.get("sortName") or "").strip(),
    }
    names.update(
        str(alias.get("name") or "").strip()
        for alias in artist.get("aliases") or []
        if isinstance(alias, dict)
    )
    normalized = {normalize_text(name) for name in names if normalize_text(name)}
    return {
        value
        for name in normalized
        for value in (name, name.replace(" ", ""))
        if value
    }


def _credit_artists(entity):
    artists = []
    for credit in entity.get("artist-credit") or []:
        if not isinstance(credit, dict):
            continue
        artist = credit.get("artist") or {}
        if not isinstance(artist, dict):
            artist = {}
        if credit.get("name") and not artist.get("name"):
            artist = {**artist, "name": credit["name"]}
        artists.append(artist)
    return artists


def _add_artist(artist_rows, artist, source_mask):
    artist_mbid = str(artist.get("id") or artist.get("musicbrainzId") or "").casefold()
    if not _valid_mbid(artist_mbid):
        return
    for name in _artist_names(artist):
        key = (name, artist_mbid)
        artist_rows[key] = artist_rows.get(key, 0) | source_mask


def _add_group_ref(group_refs, group, cache_key):
    group_mbid = str((group or {}).get("id") or "").casefold()
    if _valid_mbid(group_mbid) and cache_key:
        group_refs.add((group_mbid, cache_key))


def _is_latin_text(value):
    letters = [character for character in str(value or "") if character.isalpha()]
    return bool(letters) and all(
        "LATIN" in unicodedata.name(character, "") for character in letters
    )


def _romanized_title(group):
    canonical = str(group.get("title") or "").strip()
    if not canonical or _is_latin_text(canonical):
        return ""
    aliases = [
        alias
        for alias in group.get("aliases") or []
        if isinstance(alias, dict)
        and _is_latin_text(alias.get("name"))
        and str(alias.get("name") or "").strip().casefold() != canonical.casefold()
    ]
    priorities = (
        lambda alias: alias.get("locale") == "en" and alias.get("primary") is True,
        lambda alias: alias.get("locale") == "en",
        lambda alias: alias.get("primary") is True,
        lambda _alias: True,
    )
    for matches in priorities:
        alias = next((item for item in aliases if matches(item)), None)
        if alias:
            return str(alias["name"]).strip()
    return ""


def _metadata_quality(source_mask):
    if source_mask & SOURCE_MUSICBRAINZ:
        return 3
    if source_mask & SOURCE_LIDARR:
        return 2
    return 1


def _add_release_group(
    group_rows,
    group_artist_rows,
    artist_rows,
    group_refs,
    group,
    source_mask,
    cache_key="",
):
    group_mbid = str((group or {}).get("id") or "").casefold()
    title = str((group or {}).get("title") or "").strip()
    if not _valid_mbid(group_mbid) or not title:
        return
    _add_group_ref(group_refs, group, cache_key)
    artists = _credit_artists(group)
    for artist in artists:
        _add_artist(artist_rows, artist, source_mask)
    artist_name = " · ".join(
        str(
            credit.get("name")
            or (credit.get("artist") or {}).get("name")
            or ""
        ).strip()
        for credit in group.get("artist-credit") or []
        if isinstance(credit, dict)
    )
    artist_name = " · ".join(name for name in artist_name.split(" · ") if name)
    romanized_title = _romanized_title(group)
    record = {
        "release_group_mbid": group_mbid,
        "title": title,
        "normalized_title": normalize_text(title),
        "romanized_title": romanized_title,
        "normalized_romanized_title": normalize_text(romanized_title),
        "artist_name": artist_name,
        "normalized_artist_name": normalize_text(artist_name),
        "primary_type": str(group.get("primary-type") or "").strip(),
        "secondary_types": json.dumps(
            [str(value) for value in group.get("secondary-types") or [] if value],
            separators=(",", ":"),
        ),
        "first_release_date": str(group.get("first-release-date") or "").strip(),
        "disambiguation": str(group.get("disambiguation") or "").strip(),
        "source_mask": source_mask,
        "metadata_quality": _metadata_quality(source_mask),
    }
    existing = group_rows.get(group_mbid)
    if existing is None:
        group_rows[group_mbid] = record
    else:
        existing["source_mask"] |= source_mask
        if record["metadata_quality"] >= existing["metadata_quality"]:
            for key, value in record.items():
                if key not in {"source_mask", "release_group_mbid"} and value:
                    existing[key] = value
    for credit in group.get("artist-credit") or []:
        if not isinstance(credit, dict):
            continue
        artist = credit.get("artist") or {}
        artist_mbid = str(artist.get("id") or "").casefold()
        if not _valid_mbid(artist_mbid):
            continue
        credit_name = str(credit.get("name") or artist.get("name") or "").strip()
        key = (group_mbid, artist_mbid)
        current = group_artist_rows.get(key)
        if current is None:
            group_artist_rows[key] = {
                "credit_name": credit_name,
                "normalized_credit_name": normalize_text(credit_name),
                "source_mask": source_mask,
            }
        else:
            current["source_mask"] |= source_mask
            if credit_name:
                current["credit_name"] = credit_name
                current["normalized_credit_name"] = normalize_text(credit_name)


def _compact_group_payload(row, group_artist_rows):
    group_mbid = row["release_group_mbid"]
    try:
        secondary_types = json.loads(row["secondary_types"])
    except (TypeError, json.JSONDecodeError):
        secondary_types = []
    credits = []
    for (candidate_group, artist_mbid), artist_row in group_artist_rows.items():
        if candidate_group != group_mbid:
            continue
        name = artist_row["credit_name"]
        credits.append({
            "name": name,
            "artist": {"id": artist_mbid, "name": name},
        })
    return {
        "id": group_mbid,
        "title": row["title"],
        "artist-credit": credits,
        "primary-type": row["primary_type"],
        "secondary-types": secondary_types,
        "first-release-date": row["first_release_date"],
        "disambiguation": row["disambiguation"],
    }


def _infer_release_group(release, group_rows, group_artist_rows):
    """Return one safe cached group match for a legacy release payload."""
    normalized_title = normalize_text((release or {}).get("title"))
    artist_mbids = {
        str(artist.get("id") or "").casefold()
        for artist in _credit_artists(release or {})
        if _valid_mbid(artist.get("id"))
    }
    if not normalized_title or not artist_mbids:
        return None
    candidates = []
    for group_mbid, row in group_rows.items():
        titles = {
            row["normalized_title"],
            row["normalized_romanized_title"],
        }
        if normalized_title not in titles:
            continue
        group_artist_mbids = {
            artist_mbid
            for candidate_group, artist_mbid in group_artist_rows
            if candidate_group == group_mbid
        }
        if artist_mbids.isdisjoint(group_artist_mbids):
            continue
        candidates.append(row)
        if len(candidates) > 1:
            return None
    if len(candidates) != 1:
        return None
    return _compact_group_payload(candidates[0], group_artist_rows)


def _add_release(
    relation_rows,
    artist_rows,
    group_refs,
    release,
    cache_key,
    group_rows=None,
    group_artist_rows=None,
):
    group = release.get("release-group") or {}
    group_mbid = str(group.get("id") or "").casefold()
    if not _valid_mbid(group_mbid):
        return
    _add_group_ref(group_refs, group, cache_key)
    if group_rows is not None and group_artist_rows is not None:
        _add_release_group(
            group_rows,
            group_artist_rows,
            artist_rows,
            group_refs,
            group,
            SOURCE_MUSICBRAINZ,
            cache_key,
        )
    release_artists = _credit_artists(release)
    for artist in [*_credit_artists(group), *release_artists]:
        _add_artist(artist_rows, artist, SOURCE_MUSICBRAINZ)

    for medium in release.get("media") or []:
        for track in medium.get("tracks") or []:
            recording = track.get("recording") or {}
            title = normalize_text(track.get("title") or recording.get("title"))
            if not title:
                continue
            recording_mbid = str(recording.get("id") or "").casefold()
            if not _valid_mbid(recording_mbid):
                recording_mbid = ""
            artists = (
                _credit_artists(track)
                or _credit_artists(recording)
                or release_artists
                or _credit_artists(group)
            )
            for artist in artists:
                _add_artist(artist_rows, artist, SOURCE_MUSICBRAINZ)
                artist_mbid = str(artist.get("id") or "").casefold()
                if not _valid_mbid(artist_mbid):
                    continue
                key = (title, artist_mbid, recording_mbid, group_mbid)
                relation_rows[key] = (
                    relation_rows.get(key, 0) | SOURCE_MUSICBRAINZ
                )


def _harvest_musicbrainz(
    payload,
    cache_key,
    artist_rows,
    relation_rows,
    group_refs,
    group_rows,
    group_artist_rows,
):
    if not isinstance(payload, dict):
        return
    if payload.get("id") and payload.get("sort-name"):
        _add_artist(artist_rows, payload, SOURCE_MUSICBRAINZ)
    for artist in _credit_artists(payload):
        _add_artist(artist_rows, artist, SOURCE_MUSICBRAINZ)
    for artist in payload.get("artists") or []:
        if isinstance(artist, dict):
            _add_artist(artist_rows, artist, SOURCE_MUSICBRAINZ)
    for group in payload.get("release-groups") or []:
        if not isinstance(group, dict):
            continue
        _add_release_group(
            group_rows,
            group_artist_rows,
            artist_rows,
            group_refs,
            group,
            SOURCE_MUSICBRAINZ,
            cache_key,
        )
    if payload.get("primary-type") and payload.get("id"):
        _add_release_group(
            group_rows,
            group_artist_rows,
            artist_rows,
            group_refs,
            payload,
            SOURCE_MUSICBRAINZ,
            cache_key,
        )
    if payload.get("release-group") and payload.get("media"):
        _add_release(
            relation_rows,
            artist_rows,
            group_refs,
            payload,
            cache_key,
            group_rows,
            group_artist_rows,
        )


def _lidarr_group(group_mbid, album):
    artist_mbid = str(album.get("artistMbid") or "").casefold()
    artist_name = str(album.get("artistName") or "").strip()
    credit = {"name": artist_name, "artist": {"name": artist_name}}
    if _valid_mbid(artist_mbid):
        credit["artist"]["id"] = artist_mbid
    return {
        "id": group_mbid,
        "title": album.get("title") or "",
        "first-release-date": album.get("releaseDate") or "",
        "primary-type": album.get("type") or "",
        "secondary-types": album.get("secondaryTypes") or [],
        "artist-credit": [credit] if artist_name or _valid_mbid(artist_mbid) else [],
    }


def _plex_groups(payload):
    artists_by_rating_key = {
        str(artist.get("ratingKey") or ""): artist
        for artist in payload.get("artists") or []
        if isinstance(artist, dict) and artist.get("ratingKey")
    }
    artists_by_name = {}
    for artist in payload.get("artists") or []:
        if not isinstance(artist, dict):
            continue
        name = normalize_text(artist.get("name"))
        if name:
            artists_by_name.setdefault(name, []).append(artist)
    for album in payload.get("releaseGroups") or []:
        if not isinstance(album, dict):
            continue
        group_mbid = str(album.get("musicbrainzReleaseGroupId") or "").casefold()
        if not _valid_mbid(group_mbid):
            continue
        artist = artists_by_rating_key.get(str(album.get("artistRatingKey") or ""))
        if artist is None:
            matches = artists_by_name.get(normalize_text(album.get("artistName")), [])
            artist = matches[0] if len(matches) == 1 else {}
        artist_mbid = str((artist or {}).get("musicbrainzId") or "").casefold()
        artist_name = str(album.get("artistName") or (artist or {}).get("name") or "")
        credit_artist = {"name": artist_name}
        if _valid_mbid(artist_mbid):
            credit_artist["id"] = artist_mbid
        release_type = str(album.get("releaseType") or "").strip()
        yield {
            "id": group_mbid,
            "title": album.get("name") or "",
            "first-release-date": str(album.get("year") or ""),
            "primary-type": release_type,
            "secondary-types": [],
            "artist-credit": [{
                "name": artist_name,
                "artist": credit_artist,
            }] if artist_name or _valid_mbid(artist_mbid) else [],
        }


def _harvest_local_document(
    payload,
    namespace,
    artist_rows,
    group_rows,
    group_artist_rows,
    group_refs,
):
    if not isinstance(payload, dict):
        return
    if namespace == "lidarr-library":
        for artist_mbid, artist in (payload.get("artists") or {}).items():
            if isinstance(artist, dict):
                _add_artist(
                    artist_rows,
                    {**artist, "id": artist_mbid},
                    SOURCE_LIDARR,
                )
        for group_mbid, album in (payload.get("albums") or {}).items():
            if isinstance(album, dict):
                _add_release_group(
                    group_rows,
                    group_artist_rows,
                    artist_rows,
                    group_refs,
                    _lidarr_group(group_mbid, album),
                    SOURCE_LIDARR,
                )
    elif namespace == "plex-library":
        for artist in payload.get("artists") or []:
            if isinstance(artist, dict):
                _add_artist(artist_rows, artist, SOURCE_PLEX)
        for group in _plex_groups(payload):
            _add_release_group(
                group_rows,
                group_artist_rows,
                artist_rows,
                group_refs,
                group,
                SOURCE_PLEX,
            )


def _upsert_rows(
    connection,
    artist_rows,
    relation_rows,
    group_refs,
    release_group_rows=None,
    release_group_artist_rows=None,
):
    connection.executemany(
        """
        INSERT INTO track_search_artist_names
            (normalized_name, artist_mbid, source_mask)
        VALUES (?, ?, ?)
        ON CONFLICT(normalized_name, artist_mbid) DO UPDATE SET
            source_mask = source_mask | excluded.source_mask
        """,
        ((name, mbid, source) for (name, mbid), source in artist_rows.items()),
    )
    connection.executemany(
        """
        INSERT INTO track_search_relations
            (normalized_title, artist_mbid, recording_mbid,
             release_group_mbid, source_mask)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(
            normalized_title, artist_mbid, recording_mbid, release_group_mbid
        ) DO UPDATE SET source_mask = source_mask | excluded.source_mask
        """,
        (
            (*key, source)
            for key, source in relation_rows.items()
        ),
    )
    connection.executemany(
        """
        INSERT OR IGNORE INTO track_search_release_group_refs
            (release_group_mbid, cache_key)
        VALUES (?, ?)
        """,
        group_refs,
    )
    release_group_rows = release_group_rows or {}
    connection.executemany(
        """
        INSERT INTO track_search_release_groups
            (release_group_mbid, title, normalized_title, romanized_title,
             normalized_romanized_title, artist_name, normalized_artist_name,
             primary_type, secondary_types, first_release_date,
             disambiguation, source_mask, metadata_quality)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(release_group_mbid) DO UPDATE SET
            title = CASE
                WHEN excluded.metadata_quality >= metadata_quality
                     AND excluded.title != '' THEN excluded.title ELSE title END,
            normalized_title = CASE
                WHEN excluded.metadata_quality >= metadata_quality
                     AND excluded.normalized_title != ''
                THEN excluded.normalized_title ELSE normalized_title END,
            romanized_title = CASE
                WHEN excluded.metadata_quality >= metadata_quality
                     AND excluded.romanized_title != ''
                THEN excluded.romanized_title ELSE romanized_title END,
            normalized_romanized_title = CASE
                WHEN excluded.metadata_quality >= metadata_quality
                     AND excluded.normalized_romanized_title != ''
                THEN excluded.normalized_romanized_title
                ELSE normalized_romanized_title END,
            artist_name = CASE
                WHEN excluded.metadata_quality >= metadata_quality
                     AND excluded.artist_name != ''
                THEN excluded.artist_name ELSE artist_name END,
            normalized_artist_name = CASE
                WHEN excluded.metadata_quality >= metadata_quality
                     AND excluded.normalized_artist_name != ''
                THEN excluded.normalized_artist_name ELSE normalized_artist_name END,
            primary_type = CASE
                WHEN excluded.metadata_quality >= metadata_quality
                     AND excluded.primary_type != ''
                THEN excluded.primary_type ELSE primary_type END,
            secondary_types = CASE
                WHEN excluded.metadata_quality >= metadata_quality
                     AND excluded.secondary_types != '[]'
                THEN excluded.secondary_types ELSE secondary_types END,
            first_release_date = CASE
                WHEN excluded.metadata_quality >= metadata_quality
                     AND excluded.first_release_date != ''
                THEN excluded.first_release_date ELSE first_release_date END,
            disambiguation = CASE
                WHEN excluded.metadata_quality >= metadata_quality
                     AND excluded.disambiguation != ''
                THEN excluded.disambiguation ELSE disambiguation END,
            source_mask = source_mask | excluded.source_mask,
            metadata_quality = MAX(metadata_quality, excluded.metadata_quality)
        """,
        (
            (
                row["release_group_mbid"],
                row["title"],
                row["normalized_title"],
                row["romanized_title"],
                row["normalized_romanized_title"],
                row["artist_name"],
                row["normalized_artist_name"],
                row["primary_type"],
                row["secondary_types"],
                row["first_release_date"],
                row["disambiguation"],
                row["source_mask"],
                row["metadata_quality"],
            )
            for row in release_group_rows.values()
        ),
    )
    release_group_artist_rows = release_group_artist_rows or {}
    connection.executemany(
        """
        INSERT INTO track_search_release_group_artists
            (release_group_mbid, artist_mbid, credit_name,
             normalized_credit_name, source_mask)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(release_group_mbid, artist_mbid) DO UPDATE SET
            credit_name = CASE
                WHEN excluded.source_mask & 1 != 0
                     AND excluded.credit_name != '' THEN excluded.credit_name
                WHEN source_mask & 1 = 0 AND excluded.credit_name != ''
                THEN excluded.credit_name ELSE credit_name END,
            normalized_credit_name = CASE
                WHEN excluded.source_mask & 1 != 0
                     AND excluded.normalized_credit_name != ''
                THEN excluded.normalized_credit_name
                WHEN source_mask & 1 = 0
                     AND excluded.normalized_credit_name != ''
                THEN excluded.normalized_credit_name ELSE normalized_credit_name END,
            source_mask = source_mask | excluded.source_mask
        """,
        (
            (
                group_mbid,
                artist_mbid,
                row["credit_name"],
                row["normalized_credit_name"],
                row["source_mask"],
            )
            for (group_mbid, artist_mbid), row
            in release_group_artist_rows.items()
        ),
    )


def rebuild_from_cache():
    """Rebuild identity rows from cached payloads without provider requests."""
    artist_rows = {}
    relation_rows = {}
    group_refs = set()
    release_group_rows = {}
    release_group_artist_rows = {}
    legacy_releases = []
    with cache_db() as connection:
        rows = connection.execute(
            "SELECT cache_key, value FROM api_cache"
        ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["value"])
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        namespace = row["cache_key"].partition(":")[0]
        if namespace in {"musicbrainz-metadata", "musicbrainz-search"}:
            _harvest_musicbrainz(
                payload,
                row["cache_key"],
                artist_rows,
                relation_rows,
                group_refs,
                release_group_rows,
                release_group_artist_rows,
            )
            if (
                isinstance(payload, dict)
                and payload.get("media")
                and not payload.get("release-group")
            ):
                legacy_releases.append((payload, row["cache_key"]))
        elif namespace in {"lidarr-library", "plex-library"}:
            _harvest_local_document(
                payload,
                namespace,
                artist_rows,
                release_group_rows,
                release_group_artist_rows,
                group_refs,
            )

    for release, cache_key in legacy_releases:
        group = _infer_release_group(
            release,
            release_group_rows,
            release_group_artist_rows,
        )
        if group is None:
            continue
        _add_release(
            relation_rows,
            artist_rows,
            group_refs,
            {**release, "release-group": group},
            cache_key,
            release_group_rows,
            release_group_artist_rows,
        )

    with cache_db() as connection:
        connection.execute("DELETE FROM track_search_artist_names")
        connection.execute("DELETE FROM track_search_relations")
        connection.execute("DELETE FROM track_search_release_group_refs")
        connection.execute("DELETE FROM track_search_release_groups")
        connection.execute("DELETE FROM track_search_release_group_artists")
        _upsert_rows(
            connection,
            artist_rows,
            relation_rows,
            group_refs,
            release_group_rows,
            release_group_artist_rows,
        )
        connection.execute(
            "INSERT OR REPLACE INTO track_search_meta (key, value) "
            "VALUES ('schema-version', ?)",
            (SCHEMA_VERSION,),
        )
    return stats()


def _index_writable():
    try:
        initialize()
        return True
    except (OSError, sqlite3.Error) as exc:
        logger.warning("Local search index write skipped: %s", type(exc).__name__)
        return False


def index_artist(artist, source_mask=SOURCE_MUSICBRAINZ):
    if not _index_writable():
        return
    artist_rows = {}
    _add_artist(artist_rows, artist, source_mask)
    with cache_db() as connection:
        _upsert_rows(connection, artist_rows, {}, set())


def index_release_group_page(page, cache_key):
    if not _index_writable():
        return
    artist_rows = {}
    group_refs = set()
    release_group_rows = {}
    release_group_artist_rows = {}
    for group in (page or {}).get("release-groups") or []:
        _add_release_group(
            release_group_rows,
            release_group_artist_rows,
            artist_rows,
            group_refs,
            group,
            SOURCE_MUSICBRAINZ,
            cache_key,
        )
    with cache_db() as connection:
        _upsert_rows(
            connection,
            artist_rows,
            {},
            group_refs,
            release_group_rows,
            release_group_artist_rows,
        )


def index_release_groups(groups, cache_key=""):
    """Index MusicBrainz release-group candidates already fetched elsewhere."""
    index_release_group_page({"release-groups": list(groups or [])}, cache_key)


def index_release(release, cache_key):
    if not _index_writable():
        return
    artist_rows = {}
    relation_rows = {}
    group_refs = set()
    release_group_rows = {}
    release_group_artist_rows = {}
    _add_release(
        relation_rows,
        artist_rows,
        group_refs,
        release,
        cache_key,
        release_group_rows,
        release_group_artist_rows,
    )
    with cache_db() as connection:
        _upsert_rows(
            connection,
            artist_rows,
            relation_rows,
            group_refs,
            release_group_rows,
            release_group_artist_rows,
        )


def index_recording_search(response):
    """Index recording-search release relationships for on-demand track lookup."""
    if not _index_writable():
        return
    artist_rows = {}
    relation_rows = {}
    group_refs = set()
    release_group_rows = {}
    release_group_artist_rows = {}
    for recording in (response or {}).get("recordings") or []:
        if not isinstance(recording, dict):
            continue
        track = {
            "title": recording.get("title"),
            "artist-credit": recording.get("artist-credit") or [],
            "recording": recording,
        }
        for release in recording.get("releases") or []:
            if not isinstance(release, dict):
                continue
            _add_release(
                relation_rows,
                artist_rows,
                group_refs,
                {
                    **release,
                    "artist-credit": (
                        release.get("artist-credit")
                        or recording.get("artist-credit")
                        or []
                    ),
                    "media": [{"tracks": [track]}],
                },
                "",
                release_group_rows,
                release_group_artist_rows,
            )
    with cache_db() as connection:
        _upsert_rows(
            connection,
            artist_rows,
            relation_rows,
            group_refs,
            release_group_rows,
            release_group_artist_rows,
        )


def index_cached_release(cache_key):
    """Backfill one cached MusicBrainz release without a provider request."""
    if not _index_writable() or not cache_key:
        return False
    try:
        with cache_db() as connection:
            row = connection.execute(
                "SELECT value FROM api_cache WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
        if row is None:
            return False
        release = json.loads(row["value"])
    except (
        OSError,
        sqlite3.Error,
        TypeError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as exc:
        logger.warning(
            "Cached release could not be added to the track index: %s",
            type(exc).__name__,
        )
        return False
    if not isinstance(release, dict):
        return False
    if not release.get("release-group"):
        normalized_title = normalize_text(release.get("title"))
        artist_mbids = {
            str(artist.get("id") or "").casefold()
            for artist in _credit_artists(release)
            if _valid_mbid(artist.get("id"))
        }
        if not normalized_title or not artist_mbids:
            return False
        placeholders = ",".join("?" for _artist in artist_mbids)
        with cache_db() as connection:
            candidates = connection.execute(
                "SELECT DISTINCT groups.* "
                "FROM track_search_release_groups AS groups "
                "JOIN track_search_release_group_artists AS artists "
                "ON artists.release_group_mbid = groups.release_group_mbid "
                "WHERE (groups.normalized_title = ? "
                "OR groups.normalized_romanized_title = ?) "
                f"AND artists.artist_mbid IN ({placeholders}) "
                "ORDER BY groups.release_group_mbid LIMIT 2",
                (normalized_title, normalized_title, *sorted(artist_mbids)),
            ).fetchall()
            if len(candidates) != 1:
                return False
            group_mbid = candidates[0]["release_group_mbid"]
            artist_rows = connection.execute(
                "SELECT * FROM track_search_release_group_artists "
                "WHERE release_group_mbid = ? ORDER BY artist_mbid",
                (group_mbid,),
            ).fetchall()
        release = {
            **release,
            "release-group": _compact_group_payload(
                candidates[0],
                {
                    (row["release_group_mbid"], row["artist_mbid"]): row
                    for row in artist_rows
                },
            ),
        }
    index_release(release, cache_key)
    return True


def _clear_source(connection, source_mask):
    for table in (
        "track_search_artist_names",
        "track_search_release_groups",
        "track_search_release_group_artists",
    ):
        connection.execute(
            f"UPDATE {table} SET source_mask = source_mask & ? "
            "WHERE source_mask & ? != 0",
            (~source_mask, source_mask),
        )
        connection.execute(f"DELETE FROM {table} WHERE source_mask = 0")


def index_lidarr_library(payload):
    if not _index_writable():
        return
    artist_rows = {}
    release_group_rows = {}
    release_group_artist_rows = {}
    _harvest_local_document(
        payload,
        "lidarr-library",
        artist_rows,
        release_group_rows,
        release_group_artist_rows,
        set(),
    )
    with cache_db() as connection:
        _clear_source(connection, SOURCE_LIDARR)
        _upsert_rows(
            connection,
            artist_rows,
            {},
            set(),
            release_group_rows,
            release_group_artist_rows,
        )


def index_lidarr_artists(payload):
    """Backward-compatible name for the complete compact Lidarr index update."""
    index_lidarr_library(payload)


def index_plex_library(payload):
    if not _index_writable():
        return
    artist_rows = {}
    release_group_rows = {}
    release_group_artist_rows = {}
    _harvest_local_document(
        payload,
        "plex-library",
        artist_rows,
        release_group_rows,
        release_group_artist_rows,
        set(),
    )
    with cache_db() as connection:
        _clear_source(connection, SOURCE_PLEX)
        _upsert_rows(
            connection,
            artist_rows,
            {},
            set(),
            release_group_rows,
            release_group_artist_rows,
        )


def index_plex_artists(payload):
    """Backward-compatible name for the complete compact Plex index update."""
    index_plex_library(payload)


def cache_aliases(artist, matched_name):
    """Persist a strong MusicBrainz alias resolution for later local use."""
    if not _index_writable():
        return
    artist_rows = {}
    _add_artist(artist_rows, artist, SOURCE_ALIAS_FALLBACK)
    artist_mbid = str(artist.get("id") or "").casefold()
    name = normalize_text(matched_name)
    if name and _valid_mbid(artist_mbid):
        key = (name, artist_mbid)
        artist_rows[key] = artist_rows.get(key, 0) | SOURCE_ALIAS_FALLBACK
    with cache_db() as connection:
        _upsert_rows(connection, artist_rows, {}, set())


def resolve_artist(name):
    """Resolve an exact normalized alias only when it identifies one MBID."""
    try:
        initialize()
    except (OSError, sqlite3.Error) as exc:
        logger.warning("Local track index is unavailable: %s", type(exc).__name__)
        return {"status": "miss", "mbid": ""}
    normalized = normalize_text(name)
    if not normalized:
        return {"status": "miss", "mbid": ""}
    keys = {normalized, normalized.replace(" ", "")}
    placeholders = ",".join("?" for _ in keys)
    try:
        with cache_db() as connection:
            rows = connection.execute(
                "SELECT artist_mbid FROM track_search_artist_names "
                f"WHERE normalized_name IN ({placeholders}) "
                "GROUP BY artist_mbid ORDER BY artist_mbid",
                tuple(sorted(keys)),
            ).fetchall()
    except (OSError, sqlite3.Error) as exc:
        logger.warning("Local artist lookup failed: %s", type(exc).__name__)
        return {"status": "miss", "mbid": ""}
    mbids = [row["artist_mbid"] for row in rows]
    if len(mbids) == 1:
        return {"status": "unique", "mbid": mbids[0]}
    return {"status": "ambiguous" if mbids else "miss", "mbid": ""}


def exact_track_matches(artist_mbid, title):
    """Return deduplicated exact-title relations for one canonical artist."""
    normalized = normalize_text(title)
    if not normalized or not _valid_mbid(artist_mbid):
        return []
    with cache_db() as connection:
        rows = connection.execute(
            """
            SELECT normalized_title, recording_mbid, release_group_mbid,
                   source_mask
            FROM track_search_relations
            WHERE artist_mbid = ? AND normalized_title = ?
            ORDER BY release_group_mbid, recording_mbid
            """,
            (artist_mbid.casefold(), normalized),
        ).fetchall()
    return [dict(row) for row in rows]


def search_artist_tracks(artist_mbid, title):
    """Return cached track-title relations for one artist without provider I/O."""
    try:
        initialize()
    except (OSError, sqlite3.Error) as exc:
        logger.warning("Local track index is unavailable: %s", type(exc).__name__)
        return []
    normalized = normalize_text(title)
    artist_mbid = str(artist_mbid or "").casefold()
    if not normalized or not _valid_mbid(artist_mbid):
        return []
    terms = normalized.split()
    conditions = " AND ".join("normalized_title LIKE ?" for _term in terms)
    parameters = [artist_mbid, *(f"%{term}%" for term in terms), normalized]
    try:
        with cache_db() as connection:
            rows = connection.execute(
                f"""
                SELECT normalized_title, release_group_mbid
                FROM track_search_relations
                WHERE artist_mbid = ? AND {conditions}
                GROUP BY normalized_title, release_group_mbid
                ORDER BY CASE WHEN normalized_title = ? THEN 0 ELSE 1 END,
                         normalized_title, release_group_mbid
                """,
                parameters,
            ).fetchall()
    except (OSError, sqlite3.Error) as exc:
        logger.warning("Local artist track lookup failed: %s", type(exc).__name__)
        return []
    return [dict(row) for row in rows]


def search_release_groups(title, artist_mbid="", limit=250):
    """Return compact locally known groups matching all normalized title terms."""
    try:
        initialize()
    except (OSError, sqlite3.Error) as exc:
        logger.warning("Local release-group index is unavailable: %s", type(exc).__name__)
        return []
    normalized = normalize_text(title)
    if not normalized:
        return []
    artist_mbid = str(artist_mbid or "").casefold()
    if artist_mbid and not _valid_mbid(artist_mbid):
        return []
    terms = normalized.split()
    title_conditions = []
    parameters = []
    for field in ("normalized_title", "normalized_romanized_title"):
        title_conditions.append(
            "(" + " AND ".join(f"{field} LIKE ?" for _term in terms) + ")"
        )
        parameters.extend(f"%{term}%" for term in terms)
    artist_condition = ""
    if artist_mbid:
        artist_condition = """
            AND EXISTS (
                SELECT 1 FROM track_search_release_group_artists AS artists
                WHERE artists.release_group_mbid = groups.release_group_mbid
                  AND artists.artist_mbid = ?
            )
        """
        parameters.append(artist_mbid)
    parameters.append(max(1, min(int(limit), 500)))
    try:
        with cache_db() as connection:
            rows = connection.execute(
                f"""
                SELECT groups.*
                FROM track_search_release_groups AS groups
                WHERE ({' OR '.join(title_conditions)})
                {artist_condition}
                ORDER BY groups.release_group_mbid
                LIMIT ?
                """,
                parameters,
            ).fetchall()
            group_ids = [row["release_group_mbid"] for row in rows]
            artist_rows = []
            if group_ids:
                placeholders = ",".join("?" for _group_id in group_ids)
                artist_rows = connection.execute(
                    "SELECT * FROM track_search_release_group_artists "
                    f"WHERE release_group_mbid IN ({placeholders}) "
                    "ORDER BY release_group_mbid, artist_mbid",
                    group_ids,
                ).fetchall()
    except (OSError, sqlite3.Error) as exc:
        logger.warning("Local release-group lookup failed: %s", type(exc).__name__)
        return []
    artists_by_group = {}
    for row in artist_rows:
        artists_by_group.setdefault(row["release_group_mbid"], []).append({
            "name": row["credit_name"],
            "artist": {
                "id": row["artist_mbid"],
                "name": row["credit_name"],
            },
        })
    results = []
    for row in rows:
        aliases = []
        if row["romanized_title"]:
            aliases.append({"name": row["romanized_title"], "locale": "en"})
        credits = artists_by_group.get(row["release_group_mbid"], [])
        if not credits and row["artist_name"]:
            credits = [{
                "name": row["artist_name"],
                "artist": {"name": row["artist_name"]},
            }]
        try:
            secondary_types = json.loads(row["secondary_types"])
        except (TypeError, json.JSONDecodeError):
            secondary_types = []
        results.append({
            "id": row["release_group_mbid"],
            "title": row["title"],
            "aliases": aliases,
            "artist-credit": credits,
            "primary-type": row["primary_type"],
            "secondary-types": secondary_types,
            "first-release-date": row["first_release_date"],
            "disambiguation": row["disambiguation"],
            "romanizedTitle": row["romanized_title"],
            "sourceMask": row["source_mask"],
        })
    return results


def cached_release_groups(group_ids):
    """Read group display metadata from raw or compact cached metadata."""
    wanted = {str(group_id).casefold() for group_id in group_ids if group_id}
    if not wanted:
        return {}
    placeholders = ",".join("?" for _ in wanted)
    with cache_db() as connection:
        rows = connection.execute(
            f"""
            SELECT refs.release_group_mbid, cache.value
            FROM track_search_release_group_refs AS refs
            JOIN api_cache AS cache ON cache.cache_key = refs.cache_key
            WHERE refs.release_group_mbid IN ({placeholders})
            ORDER BY refs.release_group_mbid, refs.cache_key
            """,
            tuple(sorted(wanted)),
        ).fetchall()

    groups = {}
    for row in rows:
        group_mbid = row["release_group_mbid"]
        if group_mbid in groups:
            continue
        try:
            payload = json.loads(row["value"])
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        candidates = []
        if payload.get("id") == group_mbid and payload.get("primary-type"):
            candidates.append(payload)
        candidates.extend(payload.get("release-groups") or [])
        release_group = payload.get("release-group")
        if isinstance(release_group, dict):
            candidates.append(release_group)
        match = next((
            group
            for group in candidates
            if str(group.get("id") or "").casefold() == group_mbid
        ), None)
        if match is not None and match.get("primary-type"):
            groups[group_mbid] = match

    missing = wanted.difference(groups)
    if missing:
        placeholders = ",".join("?" for _ in missing)
        with cache_db() as connection:
            compact_rows = connection.execute(
                "SELECT * FROM track_search_release_groups "
                f"WHERE release_group_mbid IN ({placeholders}) "
                "ORDER BY release_group_mbid",
                tuple(sorted(missing)),
            ).fetchall()
            artist_rows = connection.execute(
                "SELECT * FROM track_search_release_group_artists "
                f"WHERE release_group_mbid IN ({placeholders}) "
                "ORDER BY release_group_mbid, artist_mbid",
                tuple(sorted(missing)),
            ).fetchall()
        artists_by_group = {}
        for row in artist_rows:
            artists_by_group.setdefault(row["release_group_mbid"], []).append({
                "name": row["credit_name"],
                "artist": {
                    "id": row["artist_mbid"],
                    "name": row["credit_name"],
                },
            })
        for row in compact_rows:
            try:
                secondary_types = json.loads(row["secondary_types"])
            except (TypeError, json.JSONDecodeError):
                secondary_types = []
            aliases = []
            if row["romanized_title"]:
                aliases.append({
                    "name": row["romanized_title"],
                    "locale": "en",
                })
            groups[row["release_group_mbid"]] = {
                "id": row["release_group_mbid"],
                "title": row["title"],
                "aliases": aliases,
                "artist-credit": artists_by_group.get(
                    row["release_group_mbid"],
                    [],
                ),
                "primary-type": row["primary_type"],
                "secondary-types": secondary_types,
                "first-release-date": row["first_release_date"],
                "disambiguation": row["disambiguation"],
            }
    return groups


def _database_bytes():
    total = 0
    for suffix in ("", "-wal", "-shm"):
        try:
            total += os.path.getsize(f"{CACHE_DATABASE}{suffix}")
        except OSError:
            pass
    return total


def stats():
    with cache_db() as connection:
        artist_rows = connection.execute(
            "SELECT COUNT(*) FROM track_search_artist_names"
        ).fetchone()[0]
        relation_rows = connection.execute(
            "SELECT COUNT(*) FROM track_search_relations"
        ).fetchone()[0]
        ref_rows = connection.execute(
            "SELECT COUNT(*) FROM track_search_release_group_refs"
        ).fetchone()[0]
        release_group_rows = connection.execute(
            "SELECT COUNT(*) FROM track_search_release_groups"
        ).fetchone()[0]
        release_group_artist_rows = connection.execute(
            "SELECT COUNT(*) FROM track_search_release_group_artists"
        ).fetchone()[0]
        index_bytes = None
        try:
            index_bytes = connection.execute("""
                SELECT COALESCE(SUM(pgsize), 0)
                FROM dbstat
                WHERE name LIKE 'track_search_%'
            """).fetchone()[0]
        except sqlite3.OperationalError:  # dbstat is optional in SQLite builds.
            pass
    return {
        "artistNameRows": artist_rows,
        "trackRelationRows": relation_rows,
        "releaseGroupRefRows": ref_rows,
        "releaseGroupRows": release_group_rows,
        "releaseGroupArtistRows": release_group_artist_rows,
        "indexRows": (
            artist_rows
            + relation_rows
            + ref_rows
            + release_group_rows
            + release_group_artist_rows
        ),
        "indexBytes": index_bytes,
        "databaseBytes": _database_bytes(),
    }
