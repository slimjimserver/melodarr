"""Private recommendation feedback and exposure-based outcome measurement."""

import json
import time

if __package__:
    from .storage import db, get_service, get_plex_listens
    from .services import plex
else:
    from storage import db, get_service, get_plex_listens
    from services import plex

DAY = 86400


def prune_exposures(user_id):
    with db() as connection:
        connection.execute(
            "DELETE FROM recommendation_exposures WHERE user_id = ? AND shown_at < ?",
            (user_id, time.time() - 90 * DAY),
        )


def feedback_for(user_id):
    with db() as connection:
        return [dict(row) for row in connection.execute(
            "SELECT * FROM recommendation_feedback WHERE user_id = ?", (user_id,)
        )]


def exposure_counts(user_id):
    with db() as connection:
        return {(row["kind"], row["mbid"]): row["count"] for row in connection.execute(
            "SELECT kind, mbid, COUNT(*) AS count FROM recommendation_exposures "
            "WHERE user_id = ? AND shown_at >= ? GROUP BY kind, mbid",
            (user_id, time.time() - 14 * DAY),
        )}


def record_activity(user_id, item, action):
    """Only accepts a server-resolved item; repeated impressions count once/day."""
    now = time.time()
    kind, mbid = item["kind"], item["id"]
    snapshot = json.dumps(item, ensure_ascii=False)
    with db() as connection:
        connection.execute(
            "DELETE FROM recommendation_exposures WHERE user_id = ? AND shown_at < ?",
            (user_id, now - 90 * DAY),
        )
        if action == "undo":
            connection.execute(
                "DELETE FROM recommendation_feedback WHERE user_id = ? AND kind = ? AND mbid = ?",
                (user_id, kind, mbid),
            )
        elif action in {"dismiss", "more"}:
            connection.execute(
                "INSERT INTO recommendation_feedback VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id, kind, mbid) DO UPDATE SET "
                "action=excluded.action, item_json=excluded.item_json, updated_at=excluded.updated_at",
                (user_id, kind, mbid, action, snapshot, now),
            )
        else:
            connection.execute(
                "INSERT OR IGNORE INTO recommendation_exposures "
                "(user_id, kind, mbid, day, item_json, shown_at) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, kind, mbid, int(now // DAY), snapshot, now),
            )
            if action in {"open", "listen"}:
                column = "opened" if action == "open" else "listened"
                connection.execute(
                    f"UPDATE recommendation_exposures SET {column} = 1 "
                    "WHERE user_id = ? AND kind = ? AND mbid = ? AND day = ?",
                    (user_id, kind, mbid, int(now // DAY)),
                )


def prior_item(user_id, kind, mbid):
    """Allow undo after refresh removes a dismissed card from the feed."""
    with db() as connection:
        row = connection.execute(
            "SELECT item_json FROM recommendation_feedback "
            "WHERE user_id = ? AND kind = ? AND mbid = ?",
            (user_id, kind, mbid),
        ).fetchone()
        return json.loads(row["item_json"]) if row else None


def metrics_for(user_id):
    """Assisted requests: requested within seven days AFTER a recorded exposure.

    These are observational counts, not a claim that the feed caused a request.
    Playback is verified against this user's Plex events after that request.
    """
    since = time.time() - 30 * DAY
    with db() as connection:
        exposures = [dict(row) for row in connection.execute(
            "SELECT * FROM recommendation_exposures WHERE user_id = ? AND shown_at >= ?",
            (user_id, since),
        )]
        requests = [dict(row) for row in connection.execute(
            "SELECT kind, mbid, MIN(created_at) AS requested_at FROM request_history h "
            "WHERE user_id = ? AND EXISTS (SELECT 1 FROM recommendation_exposures e "
            "WHERE e.user_id=h.user_id AND e.kind=h.kind AND e.mbid=h.mbid "
            "AND e.shown_at >= ? AND h.created_at >= e.shown_at "
            "AND h.created_at <= e.shown_at + ?) GROUP BY kind, mbid",
            (user_id, since, 7 * DAY),
        )]
    shown = {(row["kind"], row["mbid"]) for row in exposures}
    opened = {(row["kind"], row["mbid"]) for row in exposures if row["opened"]}
    listening_links = {(row["kind"], row["mbid"]) for row in exposures if row["listened"]}
    requested = {(row["kind"], row["mbid"]): row["requested_at"] for row in requests}
    played = set()
    config = get_service("plex")
    playback_available = False
    if config:
        index = plex.cached_library_index(config)
        server_id = str(config.get("machineIdentifier") or "").strip()
        listens = get_plex_listens(user_id, since, server_id=server_id) if server_id else []
        playback_available = bool(listens)
        for listen in listens:
            artist = index.get("artistsByRatingKey", {}).get(str(listen["artist_rating_key"]), {})
            album = index.get("releaseGroupsByRatingKey", {}).get(str(listen["album_rating_key"]), {})
            for identity in (("artist", artist.get("musicbrainzId")),
                             ("release-group", album.get("musicbrainzReleaseGroupId"))):
                if identity in requested and listen["played_at"] >= requested[identity]:
                    played.add(identity)
    return {
        "days": 30, "shown": len(shown), "opened": len(opened),
        "listeningLinksOpened": len(listening_links), "requested": len(requested),
        "played": len(played) if playback_available else None,
        "requestRate": round(len(requested) / len(shown), 4) if shown else None,
        "attribution": "Requested within 7 days of seeing a suggestion; not proof of causation.",
        "playbackNote": "Verified Plex plays after requesting; unavailable without matching Plex history.",
    }
