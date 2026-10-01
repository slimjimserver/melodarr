"""JSON-lines adapter for real Requests API responses in browser tests.

All storage is isolated by the backend suite's existing test environment.
Availability is supplied locally; attempted upstream I/O is a test failure.
"""

import json
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests import _test_environment  # noqa: E402,F401
from backend.application import create_app  # noqa: E402
from backend.request_history_search import save_names  # noqa: E402
from backend.storage import db  # noqa: E402


def respond(app, payload):
    items = payload["items"]
    username = payload.get("username") or "ada"
    columns = ("id", "kind", "mbid", "name", "artist_name", "anime_name", "anime_slug",
               "song_title", "theme_label", "theme_id", "created_at", "use_for_recommendations")
    with db() as connection:
        connection.execute("DELETE FROM request_history")
        connection.execute("DELETE FROM request_history_search_aliases")
        connection.execute("DELETE FROM anime_theme_release_group_links")
        connection.execute("INSERT OR IGNORE INTO users (username, password_hash, role, created_at) VALUES (?, 'fixture', 'admin', 0)", (username,))
        user_id = connection.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()[0]
        for item in items:
            connection.execute(
                "INSERT INTO request_history (user_id, " + ", ".join(columns) + ") VALUES (" + ", ".join("?" for _ in range(len(columns) + 1)) + ")",
                (user_id, *(item.get(column) for column in columns)),
            )
            connection.execute("INSERT INTO request_history_search_entities VALUES (?, ?, ?)", (item["id"], item["kind"], item["mbid"]))
            save_names(connection, item["kind"], item["mbid"], item.get("aliases", []))
    plex = {"artistsByMbid": {}, "releaseGroupsByMbid": {}}
    albums, downloads, queued = {}, {}, set()
    for item in items:
        if item.get("availableInPlex"):
            entry = {"url": item.get("plexUrl") or "", "plexampUrl": item.get("plexampUrl") or ""}
            plex["artistsByMbid" if item["kind"] == "artist" else "releaseGroupsByMbid"][item["mbid"]] = entry if item["kind"] == "artist" else [entry]
        if item.get("requestStatus") == "available":
            albums[item["mbid"]] = {"fullyAvailable": True}
        elif item.get("requestStatus") == "downloading":
            downloads[item["mbid"]] = {"progress": 25, "status": "downloading"}
        elif item.get("requestStatus") == "queued":
            queued.add(item["mbid"])
    with patch("backend.routes.account._profile_plex_index", return_value=plex), \
         patch("backend.services.lidarr.cached_library_availability", return_value=albums), \
         patch("backend.services.lidarr.cached_download_availability", return_value=downloads), \
         patch("backend.routes.account.pending_lidarr_search_mbids", return_value=queued):
        client = app.test_client()
        with client.session_transaction() as session:
            session["user_id"] = user_id
        response = client.get("/api/account/profile", query_string={
            "username": username, "q": payload.get("query", ""),
            "page": payload.get("page", "1"), "status": payload.get("status", "all"),
        })
        return {"status": response.status_code, "body": response.get_json()}


with ExitStack() as stack:
    network = stack.enter_context(patch("requests.sessions.Session.request", side_effect=AssertionError("Browser history fixture attempted upstream I/O")))
    app = create_app({"TESTING": True})
    for line in sys.stdin:
        try:
            result = respond(app, json.loads(line))
            network.assert_not_called()
        except Exception:
            result = {"status": 500, "body": {"error": "Requests fixture failed."}}
        print(json.dumps(result, ensure_ascii=True), flush=True)
