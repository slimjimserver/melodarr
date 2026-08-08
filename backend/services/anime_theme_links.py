"""Durable reverse associations from MusicBrainz releases to anime themes."""

import time

if __package__ == "backend.services":
    from .. import detail_cache
    from ..storage import db
else:  # Support the existing `python backend/app.py` entry point.
    import detail_cache
    from storage import db


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
    if changed:
        _invalidate_release_groups(affected_groups)
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
