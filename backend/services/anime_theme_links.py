"""Durable reverse associations from MusicBrainz releases to anime themes."""

import time
from uuid import UUID

import requests

if __package__ == "backend.services":
    from .. import detail_cache
    from ..storage import db
    from . import anime_artist_links, musicbrainz
else:  # Support the existing `python backend/app.py` entry point.
    import detail_cache
    from storage import db
    from services import anime_artist_links, musicbrainz


_RESOLVED_STATES = frozenset({"confirmed", "matched", "mapped", "resolved"})
_NON_CONFIRMED_REGISTRY_STATES = frozenset({"proposed", "rejected"})


def _text(value, fallback=""):
    return str(value or "").strip() or fallback


def _positive_integer(value):
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return None
    return normalized if normalized > 0 and str(value).strip() == str(normalized) else None


def _release_group_ids(mapping):
    if not isinstance(mapping, dict):
        return []
    state = _text(mapping.get("state") or mapping.get("status")).casefold()
    registry_status = _text(
        mapping.get("registryStatus") or mapping.get("registry_status")
    ).casefold()
    if (
        state not in _RESOLVED_STATES
        or registry_status in _NON_CONFIRMED_REGISTRY_STATES
    ):
        return []
    groups = mapping.get("releaseGroups") or mapping.get("release_groups") or []
    if not groups and (registry_status == "confirmed" or state == "confirmed"):
        groups = mapping.get("targets") or []
    release_group_ids = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        mbid = _text(
            group.get("id")
            or group.get("releaseGroupId")
            or group.get("releaseGroupMbid")
            or group.get("release_group_mbid")
        ).casefold()
        if mbid and mbid not in release_group_ids:
            release_group_ids.append(mbid)
    return release_group_ids


def _theme_snapshot(anime, theme):
    if not isinstance(anime, dict) or not isinstance(theme, dict):
        return None
    anime_slug = _text(anime.get("slug"))
    theme_id = _positive_integer(theme.get("id"))
    if not anime_slug or theme_id is None:
        return None
    song = theme.get("song") if isinstance(theme.get("song"), dict) else {}
    sequence = _positive_integer(theme.get("sequence"))
    return {
        "anime_slug": anime_slug,
        "anime_name": _text(anime.get("name"), "Untitled anime"),
        "theme_id": theme_id,
        "theme_label": _text(theme.get("label"), "Theme"),
        "theme_type": _text(theme.get("type"), "Theme"),
        "sequence": sequence,
        "song_id": _positive_integer(song.get("id")),
        "song_title": _text(song.get("title"), "Untitled song"),
    }


def _invalidate_release_groups(mbids):
    for mbid in sorted(set(mbids)):
        detail_cache.invalidate(("release-group", mbid.casefold()))


def sync_anime_theme_mapping(anime, theme, mapping):
    """Replace one theme's reverse links with its current resolved targets.

    Unresolved, proposed, rejected, or otherwise incomplete mappings remove
    stale associations. Returns whether durable state changed.
    """
    snapshot = _theme_snapshot(anime, theme)
    if snapshot is None:
        return False
    release_group_ids = _release_group_ids(mapping)
    now = time.time()
    affected_groups = set(release_group_ids)
    changed = False
    with db() as connection:
        anime_artist_links.sync(connection, anime, theme, mapping, bool(release_group_ids))
        existing = connection.execute(
            "SELECT * FROM anime_theme_release_group_links "
            "WHERE anime_slug = ? AND theme_id = ?",
            (snapshot["anime_slug"], snapshot["theme_id"]),
        ).fetchall()
        affected_groups.update(row["release_group_mbid"] for row in existing)
        existing_by_group = {
            row["release_group_mbid"]: row for row in existing
        }
        wanted = set(release_group_ids)
        stale = set(existing_by_group) - wanted
        if stale:
            placeholders = ",".join("?" for _ in stale)
            connection.execute(
                "DELETE FROM anime_theme_release_group_links "
                "WHERE anime_slug = ? AND theme_id = ? "
                f"AND release_group_mbid IN ({placeholders})",
                (snapshot["anime_slug"], snapshot["theme_id"], *sorted(stale)),
            )
            changed = True
        comparable_fields = (
            "anime_name",
            "theme_label",
            "theme_type",
            "sequence",
            "song_id",
            "song_title",
        )
        for mbid in release_group_ids:
            existing_row = existing_by_group.get(mbid)
            row_changed = existing_row is None or any(
                existing_row[field] != snapshot[field]
                for field in comparable_fields
            )
            if not row_changed:
                continue
            created_at = existing_row["created_at"] if existing_row else now
            connection.execute(
                "INSERT INTO anime_theme_release_group_links "
                "(anime_slug, anime_name, theme_id, theme_label, theme_type, "
                "sequence, song_id, song_title, release_group_mbid, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(anime_slug, theme_id, release_group_mbid) DO UPDATE SET "
                "anime_name = excluded.anime_name, "
                "theme_label = excluded.theme_label, "
                "theme_type = excluded.theme_type, sequence = excluded.sequence, "
                "song_id = excluded.song_id, song_title = excluded.song_title, "
                "updated_at = excluded.updated_at",
                (
                    snapshot["anime_slug"],
                    snapshot["anime_name"],
                    snapshot["theme_id"],
                    snapshot["theme_label"],
                    snapshot["theme_type"],
                    snapshot["sequence"],
                    snapshot["song_id"],
                    snapshot["song_title"],
                    mbid,
                    created_at,
                    now,
                ),
            )
            changed = True
        preferred = mapping.get("preferredReleaseGroupId")
        updated = connection.execute(
            "UPDATE anime_theme_release_group_links SET is_preferred=(release_group_mbid=?) "
            "WHERE anime_slug=? AND theme_id=? AND is_preferred!=(release_group_mbid=?)",
            (preferred or "", snapshot["anime_slug"], snapshot["theme_id"], preferred or ""),
        )
        changed = changed or bool(updated.rowcount)
        for group in mapping.get("releaseGroups") or mapping.get("release_groups") or mapping.get("targets") or []:
            group_id = _text(group.get("id") or group.get("releaseGroupId") or group.get("releaseGroupMbid")).casefold()
            title = _text(group.get("title") or group.get("name") or group.get("releaseGroupTitle"))
            if group_id in release_group_ids and title:
                updated = connection.execute(
                    "UPDATE anime_theme_release_group_links SET release_group_title = ?, updated_at = ? "
                    "WHERE anime_slug = ? AND theme_id = ? AND release_group_mbid = ? "
                    "AND release_group_title != ?",
                    (title, now, snapshot["anime_slug"], snapshot["theme_id"], group_id, title),
                )
                changed = changed or bool(updated.rowcount)
    # Attach current verified identities to the public mapping so initial,
    # progressive, and manual mapping responses all expose direct artist links.
    mapping["artistLinks"] = anime_artist_links.musicbrainz_links(
        (theme.get("song") or {}).get("artists") or []
    )
    if changed:
        _invalidate_release_groups(affected_groups)
        detail_cache.invalidate_kind("artist")
    return changed


def links_for_release_group(mbid):
    """Return all known anime-theme contexts for a release-group MBID."""
    mbid = _text(mbid).casefold()
    if not mbid:
        return []
    with db() as connection:
        rows = connection.execute(
            "SELECT anime_slug, anime_name, theme_id, theme_label, theme_type, "
            "sequence, song_id, song_title "
            "FROM anime_theme_release_group_links WHERE release_group_mbid = ? "
            "ORDER BY anime_name COLLATE NOCASE, anime_slug, "
            "CASE WHEN sequence IS NULL THEN 1 ELSE 0 END, sequence, theme_id",
            (mbid,),
        ).fetchall()
    return [
        {
            "animeSlug": row["anime_slug"],
            "animeName": row["anime_name"],
            "animePath": (
                f"/anime/{row['anime_slug']}#theme-{row['theme_id']}"
            ),
            "themeId": row["theme_id"],
            "themeLabel": row["theme_label"],
            "themeType": row["theme_type"],
            "sequence": row["sequence"],
            "songId": row["song_id"],
            "songTitle": row["song_title"],
        }
        for row in rows
    ]



def anime_names_for_release_groups(mbids):
    """Batch the distinct anime names shown on discography release cards."""
    mbids = sorted({_text(mbid).casefold() for mbid in mbids if _text(mbid)})
    result = {}
    if not mbids:
        return result
    with db() as connection:
        for offset in range(0, len(mbids), 400):
            batch = mbids[offset:offset + 400]
            placeholders = ",".join("?" for _ in batch)
            rows = connection.execute(
                "SELECT DISTINCT release_group_mbid, anime_name "
                "FROM anime_theme_release_group_links "
                f"WHERE release_group_mbid IN ({placeholders}) "
                "ORDER BY anime_name COLLATE NOCASE, anime_name",
                batch,
            ).fetchall()
            for row in rows:
                result.setdefault(row["release_group_mbid"], []).append(row["anime_name"])
    return result



def release_groups_for_performances(performances):
    """Read saved targets without starting new MusicBrainz matching requests."""
    song_ids = sorted({item["songId"] for item in performances if item.get("songId")})
    theme_ids = sorted({item["themeId"] for item in performances if item.get("themeId")})
    registered = {}
    observed = {}
    with db() as connection:
        for offset in range(0, len(song_ids), 400):
            batch = song_ids[offset:offset + 400]
            placeholders = ",".join("?" for _ in batch)
            rows = connection.execute(
                "SELECT mapping.song_id, mapping.status, target.release_group_mbid, "
                "target.release_group_title, target.is_preferred "
                "FROM anime_song_mappings mapping LEFT JOIN anime_song_mapping_targets target "
                "ON target.song_id = mapping.song_id "
                f"WHERE mapping.song_id IN ({placeholders}) "
                "ORDER BY target.is_preferred DESC, target.release_group_mbid", batch,
            ).fetchall()
            for row in rows:
                targets = registered.setdefault(row["song_id"], [])
                if row["status"] == "confirmed" and row["release_group_mbid"]:
                    targets.append({"id": row["release_group_mbid"],
                                    "title": row["release_group_title"],
                                    "preferred": bool(row["is_preferred"])})
        for offset in range(0, len(theme_ids), 400):
            batch = theme_ids[offset:offset + 400]
            placeholders = ",".join("?" for _ in batch)
            rows = connection.execute(
                "SELECT anime_slug, theme_id, release_group_mbid, release_group_title, is_preferred "
                "FROM anime_theme_release_group_links "
                f"WHERE theme_id IN ({placeholders}) ORDER BY release_group_mbid", batch,
            ).fetchall()
            for row in rows:
                observed.setdefault((row["anime_slug"], row["theme_id"]), []).append({
                    "id": row["release_group_mbid"], "title": row["release_group_title"],
                    "preferred": bool(row["is_preferred"]),
                })
    result = {
        (item["animeSlug"], item["themeId"]): registered.get(
            item.get("songId"), observed.get((item["animeSlug"], item["themeId"]), []),
        ) for item in performances
    }
    # Old reverse links contain only the song title. Repair missing release
    # titles once, outside the database transaction, using cached MB metadata.
    repaired = {}
    for groups in result.values():
        for group in groups:
            if group["title"]:
                continue
            mbid = group["id"]
            if mbid not in repaired:
                repaired[mbid] = ""
                try:
                    UUID(mbid)
                    detail = musicbrainz.get(f"/release-group/{mbid}", "")
                    if detail and str(detail.get("id", "")).casefold() == mbid:
                        repaired[mbid] = _text(detail.get("title"))
                except (ValueError, requests.RequestException):
                    pass
                if repaired[mbid]:
                    with db() as connection:
                        connection.execute(
                            "UPDATE anime_theme_release_group_links SET release_group_title = ? "
                            "WHERE release_group_mbid = ? AND release_group_title = ''",
                            (repaired[mbid], mbid),
                        )
            group["title"] = repaired[mbid]
    return result
