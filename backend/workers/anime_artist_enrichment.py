"""Durable, single-item background enrichment of verified anime artists.

SQLite leases coordinate web/worker processes. Successful matches have no expiry;
provider failures and negative matches retain their own retry cooldowns.
"""
import json
import logging
import time
from threading import Event

if __package__ == "backend.workers":
    from ..storage import db
    from .. import detail_cache
    from ..services import anime_artist_links, anime_musicbrainz, anime_theme_links, animethemes
else:
    from storage import db
    import detail_cache
    from services import anime_artist_links, anime_musicbrainz, anime_theme_links, animethemes

logger = logging.getLogger(__name__)
REFRESH_INTERVAL = 24 * 60 * 60
LEASE_SECONDS = 60 * 60


def discover():
    """Backfill existing identities and pick up newly verified links each poll."""
    with db() as connection:
        connection.execute("""INSERT INTO anime_artist_refresh_jobs
            (artist_id, artist_mbid, slug)
            SELECT animethemes_artist_id, MIN(artist_mbid), MIN(artist_slug)
            FROM anime_artist_links WHERE verified=1 GROUP BY animethemes_artist_id
            HAVING COUNT(DISTINCT artist_mbid)=1
            ON CONFLICT(artist_id) DO UPDATE SET artist_mbid=excluded.artist_mbid,
            slug=excluded.slug""")
        connection.execute("""DELETE FROM anime_artist_refresh_jobs WHERE artist_id NOT IN
            (SELECT animethemes_artist_id FROM anime_artist_links WHERE verified=1
             GROUP BY animethemes_artist_id HAVING COUNT(DISTINCT artist_mbid)=1)""")


def _claim(table):
    now = time.time()
    with db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        guard = ""
        parameters = (now, now)
        if table == "anime_performance_jobs":
            guard = ("AND NOT EXISTS (SELECT 1 FROM anime_performance_jobs active "
                     "WHERE active.song_id=anime_performance_jobs.song_id AND active.lease_until>?) ")
            parameters += (now,)
        row = connection.execute(
            f"SELECT rowid AS job_id, * FROM {table} WHERE due_at<=? AND lease_until<=? "
            + guard + "ORDER BY due_at, rowid LIMIT 1", parameters,
        ).fetchone()
        if row:
            connection.execute(f"UPDATE {table} SET lease_until=? WHERE rowid=?",
                               (now + LEASE_SECONDS, row["job_id"]))
    return dict(row) if row else None


def refresh_artist(job):
    detail = animethemes.artist_detail(job["slug"], force_refresh=True)
    if not detail or detail.get("id") != job["artist_id"]:
        raise ValueError("AnimeThemes artist identity changed or is unavailable")
    now = time.time()
    with db() as connection:
        for anime in detail.get("anime") or []:
            for performance in anime.get("performances") or []:
                if not performance.get("songId"):
                    continue
                connection.execute("""INSERT INTO anime_performance_jobs
                    (anime_slug, theme_id, song_id, due_at) VALUES (?, ?, ?, ?)
                    ON CONFLICT(anime_slug, theme_id) DO UPDATE SET
                    song_id=excluded.song_id, due_at=MIN(anime_performance_jobs.due_at, excluded.due_at)""",
                    (anime["slug"], performance["themeId"], performance["songId"], now))
        connection.execute("""UPDATE anime_artist_refresh_jobs SET snapshot=?, updated_at=?,
            due_at=?, lease_until=0, failures=0 WHERE artist_id=?""",
            (json.dumps(detail), now, now + REFRESH_INTERVAL, job["artist_id"]))
    detail_cache.invalidate_kind("artist")


def match_performance(job):
    # Full anime metadata supplies every collaborator, rather than inventing
    # a single-artist credit from the artist's performance listing.
    anime = animethemes.detail(job["anime_slug"])
    theme = next((item for item in (anime or {}).get("themes", [])
                  if item["id"] == job["theme_id"]), None)
    if theme is None or (theme.get("song") or {}).get("id") != job["song_id"]:
        raise ValueError("Performance metadata not yet available")
    mapping = anime_musicbrainz.stored_mapping(theme)
    if mapping is None:
        artists = theme["song"].get("artists") or []
        identities = anime_artist_links.musicbrainz_links(artists)
        verified = {}
        for artist in artists:
            mbid = identities.get(str(artist.get("id")))
            name = artist.get("name")
            if mbid and name:
                verified[name] = mbid
        mapping = anime_musicbrainz.resolve_theme(theme, verified_artists=verified)
        anime_musicbrainz.cache_mapping(theme, mapping)
    elif mapping.get("mappingSource") not in {"local", "seed"}:
        # Promote pre-existing disposable results without searching again.
        if mapping.get("state") == "resolved":
            anime_musicbrainz.cache_mapping(theme, mapping)
    # Re-read manual state after network work so user review takes precedence.
    mapping = anime_musicbrainz.registered_mapping(theme) or mapping
    anime_theme_links.sync_anime_theme_mapping(anime, theme, mapping)
    with db() as connection:
        retry = connection.execute(
            "SELECT retry_at FROM anime_automatic_matches WHERE mapping_key=?",
            (anime_musicbrainz.theme_mapping_key(theme),),
        ).fetchone()
        due = time.time() + REFRESH_INTERVAL
        if retry and retry["retry_at"]:
            due = max(time.time() + 60, retry["retry_at"])
        connection.execute("""UPDATE anime_performance_jobs SET due_at=?, lease_until=0,
            failures=0 WHERE anime_slug=? AND theme_id=?""",
            (due, job["anime_slug"], job["theme_id"]))


def tick():
    """Process at most one artist refresh or one performance per iteration."""
    discover()
    for table, handler in (("anime_performance_jobs", match_performance),
                           ("anime_artist_refresh_jobs", refresh_artist)):
        job = _claim(table)
        if not job:
            continue
        try:
            handler(job)
        except Exception:
            logger.exception("Anime artist enrichment failed (%s, job %s)", table, job["job_id"])
            delay = min(REFRESH_INTERVAL, 300 * 2 ** min(job["failures"], 8))
            with db() as connection:
                connection.execute(f"UPDATE {table} SET due_at=?, lease_until=0, failures=failures+1 "
                                   "WHERE rowid=?", (time.time() + delay, job["job_id"]))
        return True
    return False


def run():
    pause = Event()
    while True:
        try:
            worked = tick()
        except Exception:
            logger.exception("Could not poll anime artist enrichment jobs")
            worked = False
        pause.wait(5 if worked else 60)
