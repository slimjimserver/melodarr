"""Frontend document and icon routes."""

import os

from flask import Blueprint, current_app, redirect, request, send_from_directory

if __package__ == "backend.routes":
    from ..config import FRONTEND_ROOT
    from ..storage import db
else:  # Support the existing `python backend/app.py` entry point.
    from config import FRONTEND_ROOT
    from storage import db


blueprint = Blueprint("pages", __name__)


def frontend_index(**_route_values):
    if request.path == "/":
        with db() as connection:
            has_users = connection.execute("SELECT 1 FROM users LIMIT 1").fetchone()
        if not has_users:
            return redirect("/setup")
    return send_from_directory(current_app.static_folder, "index.html")


blueprint.add_url_rule("/", view_func=frontend_index)
blueprint.add_url_rule("/settings", view_func=frontend_index)
blueprint.add_url_rule("/settings/jobs", view_func=frontend_index)
blueprint.add_url_rule("/library", view_func=frontend_index)
blueprint.add_url_rule("/<username>", view_func=frontend_index)
blueprint.add_url_rule("/<username>/settings/<section>", view_func=frontend_index)
blueprint.add_url_rule("/artists/<mbid>", view_func=frontend_index)
blueprint.add_url_rule("/albums/<mbid>", view_func=frontend_index)
blueprint.add_url_rule("/releases/<mbid>", view_func=frontend_index)


@blueprint.get("/icons/<path:filename>")
def icons(filename):
    return send_from_directory(os.path.join(FRONTEND_ROOT, "icons"), filename)
