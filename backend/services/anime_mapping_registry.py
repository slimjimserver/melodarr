"""Durable, locally curated AnimeThemes-to-MusicBrainz mappings.

This registry is application state, not resolver cache data. A mapping belongs
to one AnimeThemes song and may expose several MusicBrainz release groups. An
active mapping has exactly one preferred Lidarr target; a rejected mapping is
a durable local tombstone with no targets.
"""

import json
import time
from uuid import UUID

if __package__ == "backend.services":
    from ..storage import db
else:  # Support the existing `python backend/app.py` entry point.
    from storage import db


SCHEMA_VERSION = 2
STATUSES = frozenset({"proposed", "confirmed", "rejected"})


def _song_id(value):
    if isinstance(value, bool):
        raise ValueError("AnimeThemes song ID must be a positive integer.")
    if isinstance(value, int):
        normalized = value
    elif isinstance(value, str) and value.strip().isdigit():
        normalized = int(value.strip())
    else:
        raise ValueError("AnimeThemes song ID must be a positive integer.")
    if normalized <= 0:
        raise ValueError("AnimeThemes song ID must be a positive integer.")
    return normalized


def _text(value, label, *, required=True):
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text.")
    normalized = value.strip()
    if required and not normalized:
        raise ValueError(f"{label} is required.")
    return normalized


def _mbid(value, label):
    raw = _text(value, label)
    try:
        return str(UUID(raw))
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"{label} must be a valid MusicBrainz UUID.") from exc


def _value(document, *names, default=None):
    for name in names:
        if name in document:
            return document[name]
    return default


def _mbid_list(target, plural_names, singular_names, label):
    raw = _value(target, *plural_names)
    if raw is None:
        singular = _value(target, *singular_names)
        raw = [] if singular in (None, "") else [singular]
    elif isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        raise ValueError(f"{label} must be a list of MusicBrainz UUIDs.")
    normalized = []
    for value in raw:
        mbid = _mbid(value, label[:-1] if label.endswith("s") else label)
        if mbid not in normalized:
            normalized.append(mbid)
    return normalized


def _normalize_target(target, default_scope):
    if not isinstance(target, dict):
        raise ValueError("Each mapping target must be an object.")
    release_group_id = _mbid(
        _value(
            target,
            "releaseGroupId",
            "releaseGroupMbid",
            "release_group_id",
            "release_group_mbid",
        ),
        "Release-group MBID",
    )
    preferred = _value(target, "preferred", "is_preferred", default=False)
    if not isinstance(preferred, bool):
        raise ValueError("Preferred target must be true or false.")
    return {
        "releaseGroupId": release_group_id,
        "recordingIds": _mbid_list(
            target,
            ("recordingIds", "recordingMbids", "recording_ids", "recording_mbids"),
            ("recordingId", "recordingMbid", "recording_id", "recording_mbid"),
            "Recording MBIDs",
        ),
        "artistIds": _mbid_list(
            target,
            ("artistIds", "artistMbids", "artist_ids", "artist_mbids"),
            ("artistId", "artistMbid", "artist_id", "artist_mbid"),
            "Artist MBIDs",
        ),
        "releaseGroupTitle": _text(
            _value(target, "releaseGroupTitle", "release_group_title"),
            "Release-group title",
        ),
        "artistName": _text(
            _value(target, "artistName", "artist_name"),
            "Target artist name",
        ),
        "primaryType": _text(
            _value(target, "primaryType", "primary_type", default=""),
            "Primary type",
            required=False,
        ),
        "firstReleaseDate": _text(
            _value(target, "firstReleaseDate", "first_release_date", default=""),
            "First release date",
            required=False,
        ),
        "scope": _text(
            _value(target, "scope", "mappingScope", "mapping_scope", default=default_scope),
            "Mapping scope",
        ),
        "preferred": preferred,
    }


def _normalize_artists(artists):
    if isinstance(artists, str):
        artists = [artists]
    if not isinstance(artists, (list, tuple)):
        raise ValueError("Source artists must be a list of names.")
    normalized = [_text(artist, "Source artist") for artist in artists]
    if not normalized:
        raise ValueError("At least one source artist is required.")
    return normalized


def _target_document(row):
    return {
        "releaseGroupId": row["release_group_mbid"],
        "recordingIds": json.loads(row["recording_mbids_json"]),
        "artistIds": json.loads(row["artist_mbids_json"]),
        "releaseGroupTitle": row["release_group_title"],
        "artistName": row["artist_name"],
        "primaryType": row["primary_type"],
        "firstReleaseDate": row["first_release_date"],
        "scope": row["mapping_scope"],
        "preferred": bool(row["is_preferred"]),
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
    }


def _mapping_document(mapping, target_rows):
    if mapping is None:
        return None
    targets = [_target_document(row) for row in target_rows]
    return {
        "songId": mapping["song_id"],
        "title": mapping["title_snapshot"],
        "artists": json.loads(mapping["artists_json"]),
        "status": mapping["status"],
        "provenance": mapping["provenance"],
        "scope": next(
            (target["scope"] for target in targets if target["preferred"]),
            mapping["mapping_scope"],
        ),
        "targets": targets,
        "preferredTarget": next(
            (target for target in targets if target["preferred"]),
            None,
        ),
        "schemaVersion": mapping["schema_version"],
        "createdAt": mapping["created_at"],
        "updatedAt": mapping["updated_at"],
    }


def _read_mapping(connection, song_id):
    mapping = connection.execute(
        "SELECT * FROM anime_song_mappings WHERE song_id = ?",
        (song_id,),
    ).fetchone()
    if mapping is None:
        return None
    targets = connection.execute(
        "SELECT * FROM anime_song_mapping_targets WHERE song_id = ? "
        "ORDER BY is_preferred DESC, release_group_mbid",
        (song_id,),
    ).fetchall()
    return _mapping_document(mapping, targets)


def get_mapping(song_id):
    """Return one durable local mapping, or ``None`` when it is unknown."""
    song_id = _song_id(song_id)
    with db() as connection:
        return _read_mapping(connection, song_id)


def _normalize_mapping(
    song_id,
    *,
    title,
    artists,
    status,
    provenance,
    targets,
    scope="song",
    preferred_release_group_mbid=None,
):
    song_id = _song_id(song_id)
    title = _text(title, "Source song title")
    artists = _normalize_artists(artists)
    status = _text(status, "Mapping status").casefold()
    if status not in STATUSES:
        raise ValueError("Mapping status must be proposed, confirmed, or rejected.")
    provenance = _text(provenance, "Mapping provenance")
    scope = _text(scope, "Mapping scope")
    if not isinstance(targets, (list, tuple)):
        raise ValueError("Mapping targets must be a list.")
    if status != "rejected" and not targets:
        raise ValueError("At least one release-group target is required.")
    if status == "rejected" and targets:
        raise ValueError("Rejected mappings cannot contain release-group targets.")
    targets = [_normalize_target(target, scope) for target in targets]
    group_ids = [target["releaseGroupId"] for target in targets]
    if len(set(group_ids)) != len(group_ids):
        raise ValueError("Release-group targets must be unique per song.")

    explicitly_preferred = [
        target["releaseGroupId"] for target in targets if target["preferred"]
    ]
    if len(explicitly_preferred) > 1:
        raise ValueError("Exactly one release-group target may be preferred.")
    if not targets:
        preferred = None
        if preferred_release_group_mbid is not None:
            raise ValueError("Rejected mappings cannot have a preferred target.")
    elif preferred_release_group_mbid is not None:
        preferred = _mbid(
            preferred_release_group_mbid,
            "Preferred release-group MBID",
        )
        if preferred not in group_ids:
            raise ValueError("Preferred release-group target is not in targets.")
        if explicitly_preferred and explicitly_preferred[0] != preferred:
            raise ValueError("Preferred release-group targets conflict.")
    elif explicitly_preferred:
        preferred = explicitly_preferred[0]
    elif len(targets) == 1:
        preferred = targets[0]["releaseGroupId"]
    else:
        raise ValueError("Exactly one release-group target must be preferred.")
    for target in targets:
        target["preferred"] = target["releaseGroupId"] == preferred
    return {
        "song_id": song_id,
        "title": title,
        "artists": artists,
        "status": status,
        "provenance": provenance,
        "scope": scope,
        "targets": targets,
    }


def _mapping_values(mapping, created_at, updated_at):
    return (
        mapping["song_id"],
        mapping["title"],
        json.dumps(
            mapping["artists"],
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        mapping["status"],
        mapping["provenance"],
        mapping["scope"],
        SCHEMA_VERSION,
        created_at,
        updated_at,
    )


def _insert_targets(connection, mapping, now, created_at_by_group=None):
    created_at_by_group = created_at_by_group or {}
    for target in mapping["targets"]:
        connection.execute(
            "INSERT INTO anime_song_mapping_targets "
            "(song_id, release_group_mbid, recording_mbids_json, "
            "artist_mbids_json, release_group_title, artist_name, "
            "primary_type, first_release_date, mapping_scope, is_preferred, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                mapping["song_id"],
                target["releaseGroupId"],
                json.dumps(target["recordingIds"], separators=(",", ":")),
                json.dumps(target["artistIds"], separators=(",", ":")),
                target["releaseGroupTitle"],
                target["artistName"],
                target["primaryType"],
                target["firstReleaseDate"],
                target["scope"],
                int(target["preferred"]),
                created_at_by_group.get(target["releaseGroupId"], now),
                now,
            ),
        )


def _upsert_mapping(connection, mapping, now):
    """Write a normalized mapping using the caller's active transaction."""
    song_id = mapping["song_id"]
    existing = connection.execute(
        "SELECT created_at FROM anime_song_mappings WHERE song_id = ?",
        (song_id,),
    ).fetchone()
    created_at = existing["created_at"] if existing else now
    target_created = {
        row["release_group_mbid"]: row["created_at"]
        for row in connection.execute(
            "SELECT release_group_mbid, created_at "
            "FROM anime_song_mapping_targets WHERE song_id = ?",
            (song_id,),
        )
    }
    connection.execute(
        "INSERT INTO anime_song_mappings "
        "(song_id, title_snapshot, artists_json, status, provenance, "
        "mapping_scope, schema_version, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(song_id) DO UPDATE SET "
        "title_snapshot = excluded.title_snapshot, "
        "artists_json = excluded.artists_json, status = excluded.status, "
        "provenance = excluded.provenance, "
        "mapping_scope = excluded.mapping_scope, "
        "schema_version = excluded.schema_version, "
        "updated_at = excluded.updated_at",
        _mapping_values(mapping, created_at, now),
    )
    connection.execute(
        "DELETE FROM anime_song_mapping_targets WHERE song_id = ?",
        (song_id,),
    )
    _insert_targets(connection, mapping, now, target_created)
    return _read_mapping(connection, song_id)


def upsert_mapping(
    song_id,
    *,
    title,
    artists,
    status,
    provenance,
    targets,
    scope="song",
    preferred_release_group_mbid=None,
):
    """Atomically create or replace a song mapping and all of its targets.

    ``status`` records local review state (proposed, confirmed, or rejected).
    Resolver output must remain proposed until a caller explicitly confirms it.
    """
    mapping = _normalize_mapping(
        song_id,
        title=title,
        artists=artists,
        status=status,
        provenance=provenance,
        targets=targets,
        scope=scope,
        preferred_release_group_mbid=preferred_release_group_mbid,
    )
    song_id = mapping["song_id"]

    now = time.time()
    with db() as connection:
        return _upsert_mapping(connection, mapping, now)


def create_mapping_if_absent(
    song_id,
    *,
    title,
    artists,
    status,
    provenance,
    targets,
    scope="song",
    preferred_release_group_mbid=None,
):
    """Create a mapping only when none exists, returning existing state intact.

    The insert decision and target writes share one transaction. This is the
    safe path for built-in seeds because a concurrent local correction wins
    without being overwritten by startup promotion.
    """
    mapping = _normalize_mapping(
        song_id,
        title=title,
        artists=artists,
        status=status,
        provenance=provenance,
        targets=targets,
        scope=scope,
        preferred_release_group_mbid=preferred_release_group_mbid,
    )
    song_id = mapping["song_id"]
    now = time.time()
    with db() as connection:
        cursor = connection.execute(
            "INSERT INTO anime_song_mappings "
            "(song_id, title_snapshot, artists_json, status, provenance, "
            "mapping_scope, schema_version, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(song_id) DO NOTHING",
            _mapping_values(mapping, now, now),
        )
        if cursor.rowcount:
            _insert_targets(connection, mapping, now)
        return _read_mapping(connection, song_id)


def delete_mapping(song_id):
    """Delete one local mapping and its targets. Return whether it existed."""
    song_id = _song_id(song_id)
    with db() as connection:
        cursor = connection.execute(
            "DELETE FROM anime_song_mappings WHERE song_id = ?",
            (song_id,),
        )
        return bool(cursor.rowcount)


def reject_mapping(song_id, *, title, artists, provenance="manual"):
    """Persist a local tombstone that suppresses seeds and automated lookup."""
    return upsert_mapping(
        song_id,
        title=title,
        artists=artists,
        status="rejected",
        provenance=provenance,
        scope="unknown",
        targets=[],
    )


def _positive_id(value, label):
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a positive integer.")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a positive integer.") from exc
    if normalized <= 0 or str(value).strip() != str(normalized):
        raise ValueError(f"{label} must be a positive integer.")
    return normalized


def _proposal_document(row):
    if row is None:
        return None
    target = json.loads(row["target_json"])
    release_group_mbid = target["releaseGroupId"]
    return {
        "id": row["id"],
        "userId": row["submitter_user_id"],
        "animeSlug": row["anime_slug"],
        "animeName": row["anime_name"],
        "themeId": row["theme_id"],
        "themeLabel": row["theme_label"],
        "songId": row["song_id"],
        "songTitle": row["song_title"],
        "artists": json.loads(row["artists_json"]),
        "releaseGroupMbid": release_group_mbid,
        "releaseGroup": target,
        "musicBrainzUrl": (
            f"https://musicbrainz.org/release-group/{release_group_mbid}"
        ),
        "status": row["status"],
        "submittedBy": {
            "id": row["submitter_user_id"],
            "username": row["submitter_username"],
        },
        "reviewedByUserId": row["reviewed_by_user_id"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "reviewedAt": row["reviewed_at"],
    }


_PROPOSAL_SELECT = """
    SELECT proposal.*, users.username AS submitter_username
    FROM anime_mapping_proposals AS proposal
    JOIN users ON users.id = proposal.submitter_user_id
"""


def _read_proposal(connection, proposal_id):
    row = connection.execute(
        _PROPOSAL_SELECT + " WHERE proposal.id = ?",
        (proposal_id,),
    ).fetchone()
    return _proposal_document(row)


def submit_mapping_proposal(
    submitter_user_id,
    *,
    anime_slug,
    anime_name,
    theme_id,
    theme_label,
    song_id,
    song_title,
    artists,
    target,
):
    """Create or revise one user's pending override without touching registry state."""
    submitter_user_id = _positive_id(submitter_user_id, "Submitter user ID")
    anime_slug = _text(anime_slug, "Anime slug")
    anime_name = _text(anime_name, "Anime name")
    theme_id = _positive_id(theme_id, "AnimeThemes theme ID")
    theme_label = _text(theme_label, "Theme label")
    song_id = _song_id(song_id)
    song_title = _text(song_title, "Source song title")
    artists = _normalize_artists(artists)
    target = _normalize_target(target, "unknown")
    target["preferred"] = True
    artists_json = json.dumps(artists, ensure_ascii=False, separators=(",", ":"))
    target_json = json.dumps(target, ensure_ascii=False, separators=(",", ":"))
    now = time.time()
    with db() as connection:
        existing = connection.execute(
            "SELECT id FROM anime_mapping_proposals "
            "WHERE submitter_user_id = ? AND anime_slug = ? AND theme_id = ? "
            "AND status = 'pending'",
            (submitter_user_id, anime_slug, theme_id),
        ).fetchone()
        if existing:
            proposal_id = existing["id"]
            connection.execute(
                "UPDATE anime_mapping_proposals SET anime_name = ?, "
                "theme_label = ?, song_id = ?, song_title = ?, artists_json = ?, "
                "target_json = ?, updated_at = ? WHERE id = ?",
                (
                    anime_name,
                    theme_label,
                    song_id,
                    song_title,
                    artists_json,
                    target_json,
                    now,
                    proposal_id,
                ),
            )
        else:
            cursor = connection.execute(
                "INSERT INTO anime_mapping_proposals "
                "(submitter_user_id, anime_slug, anime_name, theme_id, "
                "theme_label, song_id, song_title, artists_json, target_json, "
                "status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                (
                    submitter_user_id,
                    anime_slug,
                    anime_name,
                    theme_id,
                    theme_label,
                    song_id,
                    song_title,
                    artists_json,
                    target_json,
                    now,
                    now,
                ),
            )
            proposal_id = cursor.lastrowid
        return _read_proposal(connection, proposal_id)


def get_mapping_proposal(proposal_id):
    proposal_id = _positive_id(proposal_id, "Mapping proposal ID")
    with db() as connection:
        return _read_proposal(connection, proposal_id)


def mapping_proposals_for_anime(
    anime_slug,
    *,
    submitter_user_id=None,
    include_all_pending=False,
):
    """Return proposals visible to a user or the administrator review queue."""
    anime_slug = _text(anime_slug, "Anime slug")
    parameters = [anime_slug]
    clauses = ["proposal.anime_slug = ?"]
    if include_all_pending and submitter_user_id is not None:
        submitter_user_id = _positive_id(submitter_user_id, "Submitter user ID")
        clauses.append(
            "(proposal.status = 'pending' OR proposal.submitter_user_id = ?)"
        )
        parameters.append(submitter_user_id)
    elif include_all_pending:
        clauses.append("proposal.status = 'pending'")
    elif submitter_user_id is not None:
        submitter_user_id = _positive_id(submitter_user_id, "Submitter user ID")
        clauses.append("proposal.submitter_user_id = ?")
        parameters.append(submitter_user_id)
    else:
        return []
    with db() as connection:
        rows = connection.execute(
            _PROPOSAL_SELECT
            + " WHERE "
            + " AND ".join(clauses)
            + " ORDER BY proposal.updated_at DESC, proposal.id DESC",
            parameters,
        ).fetchall()
    return [_proposal_document(row) for row in rows]


def approve_mapping_proposal(proposal_id, reviewer_user_id):
    """Atomically publish a verified proposal and close competing proposals."""
    proposal_id = _positive_id(proposal_id, "Mapping proposal ID")
    reviewer_user_id = _positive_id(reviewer_user_id, "Reviewer user ID")
    now = time.time()
    with db() as connection:
        proposal = _read_proposal(connection, proposal_id)
        if proposal is None:
            return None
        if proposal["status"] == "rejected":
            raise ValueError("Rejected mapping proposals cannot be approved.")
        if proposal["status"] == "approved":
            return proposal
        mapping = _normalize_mapping(
            proposal["songId"],
            title=proposal["songTitle"],
            artists=proposal["artists"],
            status="confirmed",
            provenance=f"user-proposal:{proposal_id}",
            scope=proposal["releaseGroup"].get("scope") or "unknown",
            targets=[proposal["releaseGroup"]],
            preferred_release_group_mbid=proposal["releaseGroupMbid"],
        )
        _upsert_mapping(connection, mapping, now)
        connection.execute(
            "UPDATE anime_mapping_proposals SET status = 'approved', "
            "reviewed_by_user_id = ?, reviewed_at = ?, updated_at = ? "
            "WHERE id = ? AND status = 'pending'",
            (reviewer_user_id, now, now, proposal_id),
        )
        connection.execute(
            "UPDATE anime_mapping_proposals SET status = 'rejected', "
            "reviewed_by_user_id = ?, reviewed_at = ?, updated_at = ? "
            "WHERE song_id = ? AND status = 'pending' AND id != ?",
            (reviewer_user_id, now, now, proposal["songId"], proposal_id),
        )
        return _read_proposal(connection, proposal_id)


def reject_mapping_proposal(proposal_id, reviewer_user_id):
    """Reject a pending proposal without modifying the universal mapping."""
    proposal_id = _positive_id(proposal_id, "Mapping proposal ID")
    reviewer_user_id = _positive_id(reviewer_user_id, "Reviewer user ID")
    now = time.time()
    with db() as connection:
        proposal = _read_proposal(connection, proposal_id)
        if proposal is None:
            return None
        if proposal["status"] == "approved":
            raise ValueError("Approved mapping proposals cannot be rejected.")
        if proposal["status"] == "pending":
            connection.execute(
                "UPDATE anime_mapping_proposals SET status = 'rejected', "
                "reviewed_by_user_id = ?, reviewed_at = ?, updated_at = ? "
                "WHERE id = ?",
                (reviewer_user_id, now, now, proposal_id),
            )
        return _read_proposal(connection, proposal_id)
