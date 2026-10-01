"""Durable, local-only names and entity associations for request-history search."""

import json
import sqlite3
from urllib.parse import quote

if __package__:
    from .track_search_index import normalize_text
else:
    from track_search_index import normalize_text


def search_key(value):
    # Keep discovery's Unicode/diacritic normalization, with punctuation and
    # whitespace ignored as well (it's / its, hyphens and AnimeThemes slugs).
    return normalize_text(value).replace(" ", "")


def initialize(connection):
    """Create durable alias storage; return whether legacy history needs backfill."""
    existed = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE name = 'request_history_search_entities'"
    ).fetchone()
    connection.execute("""
        CREATE TABLE IF NOT EXISTS request_history_search_entities (
            request_id INTEGER NOT NULL REFERENCES request_history(id) ON DELETE CASCADE,
            entity_kind TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            PRIMARY KEY(request_id, entity_kind, entity_id)
        ) WITHOUT ROWID
    """)
    connection.execute("""
        CREATE INDEX IF NOT EXISTS request_history_search_entity
        ON request_history_search_entities(entity_kind, entity_id, request_id)
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS request_history_search_aliases (
            entity_kind TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            search_key TEXT NOT NULL,
            PRIMARY KEY(entity_kind, entity_id, search_key)
        ) WITHOUT ROWID
    """)
    connection.execute("""
        CREATE INDEX IF NOT EXISTS request_history_search_alias_name
        ON request_history_search_aliases(entity_kind, search_key, entity_id)
    """)
    connection.execute("""
        CREATE TABLE IF NOT EXISTS request_history_search_migrations (
            name TEXT PRIMARY KEY
        ) WITHOUT ROWID
    """)
    migration = "catalog-only-anime-aliases-v1"
    if not connection.execute(
        "SELECT 1 FROM request_history_search_migrations WHERE name = ?", (migration,),
    ).fetchone():
        # Old anime aliases have no provenance. Discard them conservatively and
        # reconstruct only what trusted local catalog data can establish. Names
        # whose catalog/cache source is gone cannot safely be recovered.
        connection.execute("DELETE FROM request_history_search_aliases WHERE entity_kind = 'anime'")
        _backfill_catalog_anime_names(connection)
        connection.execute("INSERT INTO request_history_search_migrations (name) VALUES (?)", (migration,))
    return not existed


def _names(document):
    """Extract names only, never translating or copying full source metadata."""
    if not isinstance(document, dict):
        return []
    names = [document.get(field) for field in (
        "name", "title", "artistName", "sort-name", "sortName", "romanizedName",
        "romanizedTitle", "name_en", "name_romaji", "english_name", "englishTitle",
        "romanized_name", "name_english", "english", "romaji", "romanized",
    )]
    for field in ("aliases", "synonyms", "alternateNames", "titles"):
        variants = document.get(field) or []
        if isinstance(variants, dict):
            variants = list(variants.values())
        if isinstance(variants, str):
            variants = [variants]
        if not isinstance(variants, (list, tuple)):
            continue
        for alias in variants:
            if isinstance(alias, str):
                names.append(alias)
            elif isinstance(alias, dict):
                names.extend(alias.get(key) for key in ("name", "title", "text", "value"))
    return [name for name in names if isinstance(name, str) and name.strip()]


def save_names(connection, kind, entity_id, names, *, catalog=False):
    """Share entity aliases; anime names must come from a local catalog source."""
    if kind == "anime" and not catalog:
        raise ValueError("Shared anime aliases require trusted catalog metadata.")
    entity_id = str(entity_id or "").strip().casefold()
    if not entity_id:
        return
    keys = {search_key(name) for name in names if isinstance(name, str)} - {""}
    connection.executemany(
        "INSERT OR IGNORE INTO request_history_search_aliases "
        "(entity_kind, entity_id, search_key) VALUES (?, ?, ?)",
        ((kind, entity_id, key) for key in keys),
    )


def _backfill_catalog_anime_names(connection):
    """Rebuild reusable anime names without consulting request snapshots or HTTP."""
    if __package__:
        from .api_cache import cache_db
    else:
        from api_cache import cache_db
    for row in connection.execute("SELECT DISTINCT anime_slug, anime_name FROM anime_theme_release_group_links"):
        save_names(connection, "anime", row["anime_slug"], [row["anime_slug"], row["anime_name"]], catalog=True)
    with cache_db() as cached:
        for row in cached.execute("SELECT value FROM api_cache WHERE cache_key LIKE 'animethemes-detail:%'"):
            try:
                payload = json.loads(row[0])
            except (TypeError, ValueError):
                continue
            anime = payload.get("anime") if isinstance(payload, dict) else None
            if isinstance(anime, dict) and anime.get("slug"):
                save_names(connection, "anime", anime["slug"], [anime["slug"], *_names(anime)], catalog=True)


def remember_anime_names(anime):
    """Keep names already returned by AnimeThemes, even before a request exists."""
    if not isinstance(anime, dict) or not anime.get("slug"):
        return
    if __package__:
        from .storage import db
    else:
        from storage import db
    try:
        with db() as connection:
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'request_history_search_aliases'"
            ).fetchone():
                save_names(connection, "anime", anime["slug"], [anime["slug"], *_names(anime)], catalog=True)
    except (OSError, sqlite3.Error):
        # Alias persistence must not make existing anime detail lookups fail.
        # Request creation can still snapshot the same already-cached names.
        pass


def local_release_group_metadata(mbid):
    """Preserve cached card metadata without invoking a MusicBrainz client."""
    if __package__:
        from .api_cache import cache_db
        from .services import musicbrainz
    else:
        from api_cache import cache_db
        from services import musicbrainz
    key = musicbrainz.metadata_cache_key(f"/release-group/{quote(mbid)}", "aliases+artist-credits+url-rels")
    try:
        with cache_db() as connection:
            row = connection.execute("SELECT value FROM api_cache WHERE cache_key = ?", (key,)).fetchone()
        data = json.loads(row[0]) if row else {}
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        "artist_name": " · ".join(
            credit["name"] for credit in data.get("artist-credit") or []
            if isinstance(credit, dict) and isinstance(credit.get("name"), str)
        ),
        "release_type": data.get("primary-type") or "",
        "release_date": data.get("first-release-date") or "",
    }


def capture(connection, request_id, *, metadata=()):
    """Snapshot already-known names on creation or migration, with no HTTP calls."""
    if __package__:
        from .api_cache import cache_db, cache_key
        from .services import animethemes, musicbrainz
    else:
        from api_cache import cache_db, cache_key
        from services import animethemes, musicbrainz

    row = dict(connection.execute(
        "SELECT * FROM request_history WHERE id = ?", (request_id,),
    ).fetchone())
    entities = {}

    def add(kind, entity_id, names=()):
        entity_id = str(entity_id or "").strip().casefold()
        if entity_id:
            entities.setdefault((kind, entity_id), []).extend(names)

    add(row["kind"], row["mbid"], [row["name"]])
    # A submitted slug may link this row to established catalog aliases, but its
    # submitted name remains row-local and must never become a shared alias.
    add("anime", row.get("anime_slug"))
    for document in metadata:
        if not isinstance(document, dict):
            continue
        if row["kind"] == "artist":
            add("artist", row["mbid"], _names(document))
        else:
            add("release-group", row["mbid"], _names(document))
            artist_id = document.get("foreignArtistId") or document.get("artistMbid")
            if isinstance(artist_id, str):
                add("artist", artist_id, [document.get("artistName")])
            artist = document.get("artist") or {}
            if isinstance(artist, dict):
                artist_id = artist.get("foreignArtistId") or artist.get("musicbrainzId") or artist.get("id")
                if isinstance(artist_id, str):
                    add("artist", artist_id, _names(artist))
            for credit in document.get("artist-credit") or []:
                if isinstance(credit, dict) and isinstance(credit.get("artist"), dict):
                    artist = credit["artist"]
                    add("artist", artist.get("id"), [credit.get("name"), *_names(artist)])

    explicit_artists = {
        entity_id for kind, entity_id in entities if kind == "artist"
    } if row["kind"] == "release-group" else set()

    def add_credit(artist_id, names):
        if not explicit_artists or str(artist_id or "").strip().casefold() in explicit_artists:
            add("artist", artist_id, names)

    if row["kind"] == "release-group":
        for link in connection.execute(
            "SELECT anime_slug, anime_name FROM anime_theme_release_group_links "
            "WHERE release_group_mbid = ?", (row["mbid"].casefold(),),
        ):
            add("anime", link["anime_slug"], [link["anime_name"], link["anime_slug"]])

    # Read the existing local index and cached documents directly. Do not use
    # service get/detail methods: a cache miss must never become an HTTP lookup.
    try:
        with cache_db() as cached:
            tables = {item[0] for item in cached.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if row["kind"] == "release-group" and "track_search_release_groups" in tables:
                group = cached.execute(
                    "SELECT title, romanized_title, artist_name "
                    "FROM track_search_release_groups WHERE release_group_mbid = ?",
                    (row["mbid"].casefold(),),
                ).fetchone()
                if group:
                    add("release-group", row["mbid"], [group["title"], group["romanized_title"]])
                    row["artist_name"] = row.get("artist_name") or group["artist_name"]
                for credit in cached.execute(
                    "SELECT artist_mbid, credit_name FROM track_search_release_group_artists WHERE release_group_mbid = ?",
                    (row["mbid"].casefold(),),
                ):
                    add_credit(credit["artist_mbid"], [credit["credit_name"]])
            if "api_cache" in tables:
                cache_keys = set()
                for kind, entity_id in list(entities):
                    if kind in {"artist", "release-group"}:
                        includes = ("", "aliases", "aliases+url-rels+genres", "aliases+artist-credits+url-rels")
                        for include in includes:
                            cache_keys.add(musicbrainz.metadata_cache_key(f"/{kind}/{quote(entity_id)}", include))
                    else:
                        cache_keys.add(cache_key(
                            "animethemes-detail", f"{animethemes._ANIMETHEMES_URL}/anime/{quote(entity_id, safe='')}",
                            {"include": animethemes._DETAIL_INCLUDE},
                        ))
                if "track_search_release_group_refs" in tables and row["kind"] == "release-group":
                    cache_keys.update(item[0] for item in cached.execute(
                        "SELECT cache_key FROM track_search_release_group_refs WHERE release_group_mbid = ?",
                        (row["mbid"].casefold(),),
                    ))
                read_keys = set()
                while cache_keys:
                    key = cache_keys.pop()
                    if key in read_keys:
                        continue
                    read_keys.add(key)
                    document = cached.execute("SELECT value FROM api_cache WHERE cache_key = ?", (key,)).fetchone()
                    if not document:
                        continue
                    try:
                        payload = json.loads(document[0])
                    except (TypeError, ValueError):
                        continue
                    if not isinstance(payload, dict):
                        continue
                    if key.startswith("animethemes-detail:"):
                        anime = payload.get("anime") or {}
                        if isinstance(anime, dict):
                            add("anime", anime.get("slug"), _names(anime))
                    else:
                        documents = [payload, *(payload.get("release-groups") or [])]
                        for entity in documents:
                            if not isinstance(entity, dict):
                                continue
                            if str(entity.get("id") or "").casefold() == row["mbid"].casefold():
                                add(row["kind"], row["mbid"], _names(entity))
                                if row["kind"] == "release-group":
                                    add("release-group", row["mbid"], [musicbrainz.romanized_release_group_title(entity)])
                                    for credit in entity.get("artist-credit") or []:
                                        if not isinstance(credit, dict):
                                            continue
                                        artist = credit.get("artist") or {}
                                        if not isinstance(artist, dict):
                                            continue
                                        add_credit(artist.get("id"), [credit.get("name"), *_names(artist)])
                                        if ("artist", str(artist.get("id") or "").casefold()) in entities:
                                            cache_keys.update(
                                                musicbrainz.metadata_cache_key(f"/artist/{quote(str(artist['id']))}", include)
                                                for include in ("", "aliases", "aliases+url-rels+genres")
                                            )
                                else:
                                    add("artist", row["mbid"], [musicbrainz.romanized_artist_name(entity)])
                            elif ("artist", str(entity.get("id") or "").casefold()) in entities:
                                add("artist", entity["id"], _names(entity))
            # Only infer identity after all authoritative local credits have
            # been read. A unique name in our partial catalog is not evidence
            # that it supersedes an explicit artist identifier.
            if (row["kind"] == "release-group" and row.get("artist_name")
                    and not any(kind == "artist" for kind, _ in entities)):
                artist_ids = {item[0] for item in connection.execute(
                    "SELECT entity_id FROM request_history_search_aliases "
                    "WHERE entity_kind = 'artist' AND search_key = ?", (search_key(row["artist_name"]),),
                )}
                if "track_search_artist_names" in tables:
                    artist_ids.update(item[0] for item in cached.execute(
                        "SELECT artist_mbid FROM track_search_artist_names WHERE normalized_name IN (?, ?)",
                        (normalize_text(row["artist_name"]), search_key(row["artist_name"])),
                    ))
                if len(artist_ids) == 1:
                    artist_id = next(iter(artist_ids))
                    add("artist", artist_id, [row["artist_name"]])
                    if "api_cache" in tables:
                        for include in ("", "aliases", "aliases+url-rels+genres"):
                            key = musicbrainz.metadata_cache_key(f"/artist/{quote(artist_id)}", include)
                            document = cached.execute("SELECT value FROM api_cache WHERE cache_key = ?", (key,)).fetchone()
                            try:
                                artist = json.loads(document[0]) if document else {}
                            except (TypeError, ValueError):
                                continue
                            if isinstance(artist, dict) and str(artist.get("id") or "").casefold() == artist_id:
                                add("artist", artist_id, [*_names(artist), musicbrainz.romanized_artist_name(artist)])
            if "track_search_artist_names" in tables:
                for kind, entity_id in list(entities):
                    if kind == "artist":
                        add(kind, entity_id, [name[0] for name in cached.execute(
                            "SELECT normalized_name FROM track_search_artist_names WHERE artist_mbid = ?", (entity_id,),
                        )])
    except (OSError, sqlite3.Error):
        # The disposable cache may be absent or busy. Canonical local fields
        # and previously captured durable aliases remain sufficient to search.
        pass

    connection.executemany(
        "INSERT OR IGNORE INTO request_history_search_entities (request_id, entity_kind, entity_id) VALUES (?, ?, ?)",
        ((request_id, kind, entity_id) for kind, entity_id in entities),
    )
    for (kind, entity_id), names in entities.items():
        save_names(connection, kind, entity_id, names, catalog=kind == "anime")


def predicate(connection, query):
    """A parameterized predicate applied before LIMIT/OFFSET; no cache reads."""
    key = search_key(query)
    if not str(query or "").strip():
        return "", ()
    if not key:
        return " AND 0", ()
    connection.create_function("history_search_key", 1, search_key, deterministic=True)
    return """ AND (
        instr(history_search_key(name), ?) > 0 OR
        instr(history_search_key(artist_name), ?) > 0 OR
        instr(history_search_key(anime_name), ?) > 0 OR
        instr(history_search_key(anime_slug), ?) > 0 OR
        instr(history_search_key(song_title), ?) > 0 OR
        instr(history_search_key(theme_label), ?) > 0 OR
        EXISTS (
            SELECT 1 FROM request_history_search_entities entity
            JOIN request_history_search_aliases alias
              ON alias.entity_kind = entity.entity_kind AND alias.entity_id = entity.entity_id
            WHERE entity.request_id = request_history.id AND instr(alias.search_key, ?) > 0
        ) OR (kind = 'release-group' AND EXISTS (
            SELECT 1 FROM anime_theme_release_group_links link
            WHERE link.release_group_mbid = lower(request_history.mbid) AND (
                instr(history_search_key(link.anime_name), ?) > 0 OR
                instr(history_search_key(link.anime_slug), ?) > 0 OR
                instr(history_search_key(link.song_title), ?) > 0 OR
                instr(history_search_key(link.theme_label), ?) > 0 OR EXISTS (
                    SELECT 1 FROM request_history_search_aliases alias
                    WHERE alias.entity_kind = 'anime' AND alias.entity_id = link.anime_slug
                      AND instr(alias.search_key, ?) > 0
                )
            )
        ))
    )""", (key,) * 12
