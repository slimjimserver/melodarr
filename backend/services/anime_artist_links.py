"""Artist identity evidence learned from resolved anime song mappings."""

import json
import unicodedata
from uuid import UUID

import requests

if __package__ == "backend.services":
    from ..storage import db
    from . import animethemes, musicbrainz
else:
    from storage import db
    from services import animethemes, musicbrainz


_VARIOUS_ARTISTS = "89ad4ac3-39f7-470e-963a-56509c546377"


def _name(value):
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


def _resolve_conflicting_identities(artist_ids):
    """Demote a false positive when only one MusicBrainz identity matches the credit.

    A single-artist release can credit a group (for example Girls Dead
    Monster) while AnimeThemes credits its singer. That release is valid, but
    its group MBID must not claim the singer's AnimeThemes artist identity.
    """
    if not artist_ids:
        return
    placeholders = ",".join("?" for _ in artist_ids)
    with db() as connection:
        rows = connection.execute(
            "SELECT animethemes_artist_id, artist_mbid, artist_name, credited_as "
            "FROM anime_artist_links WHERE verified = 1 "
            f"AND animethemes_artist_id IN ({placeholders})",
            tuple(sorted(artist_ids)),
        ).fetchall()
    by_artist = {}
    for row in rows:
        by_artist.setdefault(row["animethemes_artist_id"], {}).setdefault(
            row["artist_mbid"], []
        ).append(row)
    for artist_id, candidates in by_artist.items():
        if len(candidates) < 2:
            continue
        matching_mbids = []
        complete = True
        for mbid, evidence in candidates.items():
            try:
                artist = musicbrainz.get(f"/artist/{mbid}", "aliases")
            except requests.RequestException:
                complete = False
                break
            names = {_name(artist.get("name")), _name(artist.get("sort-name"))}
            names.update(_name(alias.get("name")) for alias in artist.get("aliases") or [])
            credited_names = {
                _name(value) for row in evidence
                for value in (row["artist_name"], row["credited_as"])
            }
            if (names - {""}).intersection(credited_names):
                matching_mbids.append(mbid)
        if complete and len(matching_mbids) == 1:
            with db() as connection:
                connection.execute(
                    "UPDATE anime_artist_links SET verified = 0 "
                    "WHERE animethemes_artist_id = ? AND artist_mbid != ?",
                    (artist_id, matching_mbids[0]),
                )


def sync(connection, anime, theme, mapping, resolved):
    """Keep source evidence in the same transaction as release associations.

    Never zip artist IDs: resolver IDs are sorted, not in credit order.
    Multiple-credit evidence is verified on the artist lookup instead.
    """
    mbids = set()
    for value in mapping.get("artistIds") or [] if resolved else []:
        try:
            mbid = str(UUID(str(value)))
        except (ValueError, TypeError, AttributeError):
            continue
        if mbid != _VARIOUS_ARTISTS:
            mbids.add(mbid)
    artists = {}
    for artist in (theme.get("song") or {}).get("artists") or []:
        if not isinstance(artist, dict):
            continue
        artist_id = artist.get("id")
        if isinstance(artist_id, int) and not isinstance(artist_id, bool) and artist_id > 0 and artist.get("slug"):
            artists[artist_id] = artist
    source = (anime["slug"], theme["id"])
    existing = connection.execute(
        "SELECT * FROM anime_artist_links WHERE anime_slug = ? AND theme_id = ?", source,
    ).fetchall()
    wanted = {(mbid, artist_id) for mbid in mbids for artist_id in artists}
    for row in existing:
        if (row["artist_mbid"], row["animethemes_artist_id"]) not in wanted:
            connection.execute(
                "DELETE FROM anime_artist_links WHERE anime_slug = ? AND theme_id = ? "
                "AND artist_mbid = ? AND animethemes_artist_id = ?",
                (*source, row["artist_mbid"], row["animethemes_artist_id"]),
            )
    for mbid, artist_id in wanted:
        artist = artists[artist_id]
        connection.execute(
            "INSERT INTO anime_artist_links "
            "(anime_slug, theme_id, artist_mbid, animethemes_artist_id, artist_slug, "
            "artist_name, credited_as, verified) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(anime_slug, theme_id, artist_mbid, animethemes_artist_id) "
            "DO UPDATE SET artist_slug = excluded.artist_slug, "
            "artist_name = excluded.artist_name, credited_as = excluded.credited_as, "
            "verified = CASE WHEN anime_artist_links.artist_name = excluded.artist_name "
            "AND anime_artist_links.credited_as = excluded.credited_as "
            "THEN anime_artist_links.verified ELSE excluded.verified END",
            (*source, mbid, artist_id, artist["slug"], artist.get("name") or "",
             artist.get("as") or "", int(len(mbids) == 1 and len(artists) == 1)),
        )


def appearances(mbid):
    """Verify collaboration credits, persist identities, and expand performances."""
    mbid = str(UUID(mbid))
    _backfill(mbid)
    with db() as connection:
        candidates = connection.execute(
            "SELECT * FROM anime_artist_links WHERE artist_mbid = ?", (mbid,),
        ).fetchall()
    if not candidates:
        return {"anime": [], "artistLinks": []}
    _resolve_conflicting_identities({row["animethemes_artist_id"] for row in candidates})
    with db() as connection:
        candidates = connection.execute(
            "SELECT * FROM anime_artist_links WHERE artist_mbid = ?", (mbid,),
        ).fetchall()
    if any(not row["verified"] for row in candidates):
        artist = musicbrainz.get(f"/artist/{mbid}", "aliases")
        names = {_name(artist.get("name")), _name(artist.get("sort-name"))}
        names.update(_name(alias.get("name")) for alias in artist.get("aliases") or [])
        names.discard("")
        matching_ids = {
            row["animethemes_artist_id"] for row in candidates
            if names.intersection({_name(row["artist_name"]), _name(row["credited_as"])})
        }
        # A homonymous source credit is ambiguous even within a collaboration.
        if len(matching_ids) == 1:
            with db() as connection:
                connection.execute(
                    "UPDATE anime_artist_links SET verified = 1 "
                    "WHERE artist_mbid = ? AND animethemes_artist_id = ?",
                    (mbid, next(iter(matching_ids))),
                )
    with db() as connection:
        links = connection.execute(
            "SELECT DISTINCT animethemes_artist_id, artist_slug FROM anime_artist_links "
            "WHERE artist_mbid = ? AND verified = 1 AND animethemes_artist_id NOT IN "
            "(SELECT animethemes_artist_id FROM anime_artist_links WHERE verified = 1 "
            "GROUP BY animethemes_artist_id HAVING COUNT(DISTINCT artist_mbid) > 1)",
            (mbid,),
        ).fetchall()
    anime = {}
    artist_links = []
    for link in links:
        with db() as connection:
            snapshot = connection.execute(
                "SELECT snapshot FROM anime_artist_refresh_jobs WHERE artist_id=? "
                "AND artist_mbid=?",
                (link["animethemes_artist_id"], mbid),
            ).fetchone()
        detail = (json.loads(snapshot["snapshot"]) if snapshot and snapshot["snapshot"]
                  else animethemes.artist_detail(link["artist_slug"]))
        if detail is None or detail["id"] != link["animethemes_artist_id"]:
            continue
        artist_links.append({"id": detail["id"], "slug": detail["slug"], "name": detail["name"]})
        for item in detail["anime"]:
            current = anime.setdefault(item["slug"], {**item, "performances": []})
            seen = {performance["themeId"] for performance in current["performances"]}
            current["performances"].extend(p for p in item["performances"] if p["themeId"] not in seen)
    return {
        "artistLinks": artist_links,
        "anime": sorted(anime.values(), key=lambda item: (-(item.get("year") or 0), item["name"])),
    }


def _backfill(mbid):
    """Learn links from previously confirmed songs using their saved anime context."""
    with db() as connection:
        sources = connection.execute(
            "SELECT DISTINCT source.anime_slug FROM anime_theme_release_group_links source "
            "JOIN anime_song_mapping_targets target ON target.song_id = source.song_id "
            "JOIN anime_song_mappings mapping ON mapping.song_id = target.song_id "
            "WHERE mapping.status = 'confirmed' AND target.artist_mbids_json LIKE ? "
            "AND NOT EXISTS (SELECT 1 FROM anime_artist_links artist "
            "WHERE artist.anime_slug = source.anime_slug AND artist.theme_id = source.theme_id "
            "AND artist.artist_mbid = ?)",
            ('%"' + mbid + '"%', mbid),
        ).fetchall()
    if not sources:
        return
    # Import lazily: the release association service also calls sync().
    if __package__ == "backend.services":
        from . import anime_musicbrainz, anime_theme_links
    else:
        from services import anime_musicbrainz, anime_theme_links
    for source in sources:
        anime = animethemes.detail(source["anime_slug"])
        if not anime:
            continue
        for theme in anime.get("themes") or []:
            mapping = anime_musicbrainz.registered_mapping(theme)
            if mapping:
                anime_theme_links.sync_anime_theme_mapping(anime, theme, mapping)


def musicbrainz_links(artists):
    """Return unambiguous, verified identities for AnimeThemes artist credits."""
    ids = {
        artist["id"] for artist in artists or []
        if isinstance(artist, dict) and isinstance(artist.get("id"), int)
        and not isinstance(artist["id"], bool) and artist["id"] > 0
    }
    if not ids:
        return {}
    _resolve_conflicting_identities(ids)
    placeholders = ",".join("?" for _ in ids)
    with db() as connection:
        rows = connection.execute(
            "SELECT animethemes_artist_id, MIN(artist_mbid) AS artist_mbid "
            "FROM anime_artist_links WHERE verified = 1 "
            f"AND animethemes_artist_id IN ({placeholders}) "
            "GROUP BY animethemes_artist_id HAVING COUNT(DISTINCT artist_mbid) = 1",
            tuple(sorted(ids)),
        ).fetchall()
    return {str(row["animethemes_artist_id"]): row["artist_mbid"] for row in rows}
