"""Frontend document and icon routes."""

import os
import re
from hashlib import sha256

from flask import Blueprint, Response, redirect, request, send_from_directory

if __package__ == "backend.routes":
    from ..config import FRONTEND_ROOT
    from ..storage import db
else:  # Support the existing `python backend/app.py` entry point.
    from config import FRONTEND_ROOT
    from storage import db


blueprint = Blueprint("pages", __name__)

STATIC_ROOT = os.path.join(FRONTEND_ROOT, "static")
FINGERPRINTED_ASSETS = ("app.js", "discovery.js", "style.css")
_document_cache = {}


def _asset_version():
    """Digest the served assets so a one-year static cache stays correct."""
    digest = sha256()
    for name in FINGERPRINTED_ASSETS:
        try:
            with open(os.path.join(STATIC_ROOT, name), "rb") as file:
                digest.update(file.read())
        except OSError:
            digest.update(name.encode())
    return digest.hexdigest()[:12]


def _index_document():
    """Return index.html with versioned asset references, built once."""
    path = os.path.join(STATIC_ROOT, "index.html")
    signature = os.path.getmtime(path)
    cached = _document_cache.get("index")
    if cached and cached[0] == signature:
        return cached[1]
    with open(path, encoding="utf-8") as file:
        document = file.read()
    version = _asset_version()
    document = re.sub(
        r'(/static/(?:' + "|".join(re.escape(name) for name in FINGERPRINTED_ASSETS) + r'))',
        rf"\1?v={version}",
        document,
    )
    _document_cache["index"] = (signature, document)
    return document


def frontend_index(**_route_values):
    if request.path == "/":
        with db() as connection:
            has_users = connection.execute("SELECT 1 FROM users LIMIT 1").fetchone()
        if not has_users:
            return redirect("/setup")
    response = Response(_index_document(), mimetype="text/html")
    response.headers["Cache-Control"] = "no-cache"
    return response


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


@blueprint.get("/manifest.webmanifest")
def manifest():
    return send_from_directory(
        STATIC_ROOT, "manifest.webmanifest", mimetype="application/manifest+json"
    )


@blueprint.get("/favicon.ico")
def favicon():
    return send_from_directory(STATIC_ROOT, "favicon.svg", mimetype="image/svg+xml")


@blueprint.get("/apple-touch-icon.png")
def apple_touch_icon():
    return send_from_directory(STATIC_ROOT, "apple-touch-icon.png")
