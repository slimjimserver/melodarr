"""Durable refresh-then-search processing for newly added Lidarr albums."""

import logging
import time
from collections import defaultdict
from threading import Event

import requests

if __package__ == "backend.workers":
    from ..services import lidarr
    from ..storage import (
        complete_lidarr_search,
        defer_lidarr_search,
        due_lidarr_searches,
        schedule_lidarr_search_poll,
        set_lidarr_refresh_command,
        set_lidarr_search_command,
    )
else:
    from services import lidarr
    from storage import (
        complete_lidarr_search,
        defer_lidarr_search,
        due_lidarr_searches,
        schedule_lidarr_search_poll,
        set_lidarr_refresh_command,
        set_lidarr_search_command,
    )


logger = logging.getLogger(__name__)
work_requested = Event()
running = Event()
last_completed_at = None
next_execution_at = None


def request_work():
    work_requested.set()


def status():
    return {
        "running": running.is_set(),
        "lastCompletedAt": last_completed_at,
        "nextExecutionAt": next_execution_at,
    }


def _defer_jobs(jobs, error, *, reset_refresh=False):
    for job in jobs:
        logger.warning("Lidarr follow-up for %s will retry: %s", job["name"], error)
        defer_lidarr_search(job["id"], error, reset_refresh=reset_refresh)


def _start_artist_refresh(jobs):
    """Start one refresh shared by jobs that were already queued for an artist."""
    try:
        response = lidarr.start_command({
            "name": "RefreshArtist",
            "artistId": jobs[0]["artist_id"],
        })
        response.raise_for_status()
        command_id = response.json()["id"]
        set_lidarr_refresh_command([job["id"] for job in jobs], command_id)
        logger.info(
            "Started one Lidarr artist refresh for %d queued release group(s)",
            len(jobs),
        )
    except (KeyError, ValueError, requests.RequestException) as exc:
        _defer_jobs(jobs, exc)


def _start_album_search(job):
    try:
        search = lidarr.start_command({
            "name": "AlbumSearch",
            "albumIds": [job["album_id"]],
        })
        search.raise_for_status()
        set_lidarr_search_command(job["id"], search.json()["id"])
        logger.info(
            "Queued Lidarr album search for %s after artist refresh completed",
            job["name"],
        )
    except (KeyError, ValueError, requests.RequestException) as exc:
        _defer_jobs([job], exc)


def _poll_artist_refresh(jobs):
    """Poll a shared refresh once, then advance every dependent album job."""
    command_id = jobs[0]["refresh_command_id"]
    try:
        response = lidarr.command(command_id)
        response.raise_for_status()
        status = response.json().get("status", "").lower()
    except requests.HTTPError as exc:
        reset_refresh = exc.response is not None and exc.response.status_code == 404
        _defer_jobs(jobs, exc, reset_refresh=reset_refresh)
        return
    except (KeyError, ValueError, requests.RequestException) as exc:
        _defer_jobs(jobs, exc)
        return

    if status == "completed":
        for job in jobs:
            _start_album_search(job)
    elif status in {"failed", "aborted", "cancelled"}:
        _defer_jobs(
            jobs,
            f"RefreshArtist command {status}",
            reset_refresh=True,
        )
    else:
        for job in jobs:
            schedule_lidarr_search_poll(job["id"])


def process_jobs(jobs):
    """Advance due jobs in batches, sharing work for the same artist."""
    new_by_artist = defaultdict(list)
    active_by_command = defaultdict(list)

    for job in jobs:
        if job["search_command_id"]:
            complete_lidarr_search(job["id"])
        elif job["refresh_command_id"]:
            active_by_command[job["refresh_command_id"]].append(job)
        else:
            new_by_artist[job["artist_id"]].append(job)

    for artist_jobs in new_by_artist.values():
        _start_artist_refresh(artist_jobs)
    for refresh_jobs in active_by_command.values():
        _poll_artist_refresh(refresh_jobs)


def process_job(job):
    """Advance one job; retained as a focused entry point for unit tests."""
    process_jobs([job])


def run():
    global last_completed_at, next_execution_at
    while True:
        running.set()
        try:
            process_jobs(due_lidarr_searches())
        except Exception:
            # Keep the durable queue alive if SQLite or an unexpected response
            # causes one polling pass to fail. The jobs remain persisted and
            # will be attempted again on the next pass or after a restart.
            logger.exception("Lidarr follow-up worker pass failed; queued work remains pending")
        finally:
            last_completed_at = time.time()
            next_execution_at = last_completed_at + 2
            running.clear()
        work_requested.wait(2)
        work_requested.clear()
