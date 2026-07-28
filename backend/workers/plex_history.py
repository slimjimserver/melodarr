"""Scheduled import of per-user Plex music listening history."""

import logging
import time
from threading import Event, Lock

import requests

if __package__ == "backend.workers":
    from ..services import plex_history
    from ..storage import (
        db,
        get_service,
        insert_plex_listens,
        plex_listen_stats,
        prune_plex_listens,
    )
    from . import recommendations as recommendation_worker
else:  # Support the existing `python backend/worker.py` entry point.
    from services import plex_history
    from storage import (
        db,
        get_service,
        insert_plex_listens,
        plex_listen_stats,
        prune_plex_listens,
    )
    from workers import recommendations as recommendation_worker


logger = logging.getLogger(__name__)
HISTORY_RETENTION = 365 * 24 * 60 * 60
SYNC_INTERVAL = 24 * 60 * 60
CURSOR_OVERLAP = 60 * 60

wake_requested = Event()
request_lock = Lock()
sync_requested = False
full_sync_requested = False
job_state = {
    "running": False,
    "lastCompletedAt": None,
    "lastSuccessfulAt": None,
    "nextExecutionAt": None,
    "lastError": None,
    "pages": 0,
    "scanned": 0,
    "tracks": 0,
    "normalized": 0,
    "selected": 0,
    "sections": 0,
    "cachedArtists": 0,
    "cachedAlbums": 0,
    "fetched": 0,
    "mapped": 0,
    "inserted": 0,
    "pruned": 0,
    "stored": 0,
    "users": 0,
    "oldestPlayedAt": None,
    "newestPlayedAt": None,
}


def request_sync(*, full=False):
    """Wake the worker; ``full=True`` re-reads the rolling twelve months."""
    global sync_requested, full_sync_requested
    with request_lock:
        sync_requested = True
        full_sync_requested = full_sync_requested or full
    wake_requested.set()


def request_full_sync():
    """Request a twelve-month backfill, including newly linked Plex users."""
    request_sync(full=True)


def status():
    return dict(job_state)


def _linked_user_indexes():
    """Index linked users by global Plex ID and username-like aliases."""
    with db() as connection:
        rows = connection.execute(
            "SELECT id, plex_id, plex_username FROM users "
            "WHERE plex_id IS NOT NULL"
        ).fetchall()
    users_by_plex_id = {}
    users_by_name = {}
    for row in rows:
        plex_id = str(row["plex_id"] or "").strip()
        if plex_id:
            users_by_plex_id.setdefault(plex_id, set()).add(row["id"])
        username = str(row["plex_username"] or "").strip().casefold()
        if username:
            users_by_name.setdefault(username, set()).add(row["id"])
    return users_by_plex_id, users_by_name


def _account_user_map(config):
    users_by_plex_id, users_by_name = _linked_user_indexes()
    result = {}
    for account in plex_history.accounts(config):
        account_id = str(account["account_id"]).strip()
        direct_user_ids = users_by_plex_id.get(account_id, set())
        alias_user_ids = {
            user_id
            for alias in account["aliases"]
            for user_id in users_by_name.get(
                alias.strip().casefold(), set()
            )
        }
        if len(direct_user_ids) > 1:
            logger.warning(
                "Ignoring Plex account %s because its account ID matches "
                "multiple linked users",
                account_id,
            )
            continue
        if (
            direct_user_ids
            and alias_user_ids
            and direct_user_ids != alias_user_ids
        ):
            logger.warning(
                "Ignoring Plex account %s because its account ID and aliases "
                "match different linked users",
                account_id,
            )
            continue
        if len(direct_user_ids) == 1:
            result[account_id] = next(iter(direct_user_ids))
        elif len(alias_user_ids) == 1:
            result[account_id] = next(iter(alias_user_ids))
        elif len(alias_user_ids) > 1:
            logger.warning(
                "Ignoring Plex account %s because its aliases match multiple users",
                account_id,
            )
    return result


def _server_id(config):
    return str(config.get("machineIdentifier") or config.get("url") or "")


def _incremental_since(server_id, retention_cutoff, *, full):
    if full:
        return retention_cutoff
    stats = plex_listen_stats(server_id=server_id)
    newest = stats.get("newest_played_at")
    try:
        newest = float(newest)
    except (TypeError, ValueError):
        return retention_cutoff
    return max(retention_cutoff, newest - CURSOR_OVERLAP)


def synchronize(config, *, full=False, now=None):
    """Import one stable Plex history window and then apply retention."""
    now = time.time() if now is None else float(now)
    retention_cutoff = now - HISTORY_RETENTION
    server_id = _server_id(config)
    if not server_id:
        raise ValueError("Plex history synchronization requires a server identity")
    account_users = _account_user_map(config)
    since = _incremental_since(server_id, retention_cutoff, full=full)

    fetched = 0
    mapped = 0
    listens = []
    diagnostics = {
        "pages": 0,
        "scanned": 0,
        "tracks": 0,
        "normalized": 0,
        "selected": 0,
        "sections": 0,
        "cachedArtists": 0,
        "cachedAlbums": 0,
    }
    for event in plex_history.iter_history(
        config,
        since=since,
        until=now,
        diagnostics=diagnostics,
    ):
        fetched += 1
        if event["played_at"] < since or event["played_at"] > now:
            continue
        user_id = account_users.get(event["account_id"])
        if user_id is None:
            continue
        mapped += 1
        listens.append({
            "server_id": server_id,
            "history_key": event["history_key"],
            "user_id": user_id,
            "artist_rating_key": event["artist_rating_key"],
            "album_rating_key": event["album_rating_key"],
            "played_at": event["played_at"],
        })

    # Do not advance the durable cursor with a partial backfill. Both insertion
    # and retention happen only after every history page completed.
    inserted = insert_plex_listens(listens) if listens else 0
    pruned = prune_plex_listens(retention_cutoff)
    stats = plex_listen_stats(server_id=server_id)
    return {
        **diagnostics,
        "fetched": fetched,
        "mapped": mapped,
        "inserted": inserted,
        "pruned": pruned,
        "stored": stats.get("count", 0),
        "users": stats.get("users", 0),
        "oldestPlayedAt": stats.get("oldest_played_at"),
        "newestPlayedAt": stats.get("newest_played_at"),
    }


def _run_sync(*, full=False):
    config = get_service("plex")
    job_state.update(
        running=True,
        lastError=None,
        pages=0,
        scanned=0,
        tracks=0,
        normalized=0,
        selected=0,
        sections=0,
        cachedArtists=0,
        cachedAlbums=0,
        fetched=0,
        mapped=0,
        inserted=0,
        pruned=0,
    )
    succeeded = False
    try:
        if not config:
            return
        result = synchronize(config, full=full)
        job_state.update(result)
        succeeded = True
        if result["inserted"] or result["pruned"]:
            recommendation_worker.request_refresh()
    except (ValueError, requests.RequestException) as exc:
        job_state["lastError"] = str(exc)
        logger.warning("Plex listening-history synchronization failed: %s", exc)
    except Exception as exc:
        job_state["lastError"] = str(exc)
        logger.exception("Plex listening-history synchronization failed")
    finally:
        completed_at = time.time()
        job_state.update(
            running=False,
            lastCompletedAt=completed_at,
            nextExecutionAt=completed_at + SYNC_INTERVAL,
        )
        if succeeded:
            job_state["lastSuccessfulAt"] = completed_at


def run():
    """Run immediately, then service five-minute and manual sync requests."""
    global sync_requested, full_sync_requested
    job_state["nextExecutionAt"] = time.time()
    while True:
        now = time.time()
        with request_lock:
            requested = sync_requested
            full = full_sync_requested
            sync_requested = False
            full_sync_requested = False
        due = now >= (job_state["nextExecutionAt"] or now)
        if requested or due:
            _run_sync(full=full)

        timeout = max(
            0.1,
            (job_state["nextExecutionAt"] or (time.time() + SYNC_INTERVAL))
            - time.time(),
        )
        wake_requested.wait(timeout)
        wake_requested.clear()
