"""Flask application factory and extension registration."""

import gzip
import mimetypes
import os
from datetime import timedelta

from flask import Flask, current_app, request, send_file
from werkzeug.utils import safe_join

if __package__:
    from .api_cache import init_cache_db, migrate_legacy_cache
    from .config import FRONTEND_ROOT, load_session_secret
    from .routes.account import blueprint as account_blueprint
    from .routes.artwork import blueprint as artwork_blueprint
    from .routes.auth import blueprint as auth_blueprint
    from .routes.discovery import blueprint as discovery_blueprint
    from .routes.library import blueprint as library_blueprint
    from .routes.music import blueprint as music_blueprint
    from .routes.pages import blueprint as pages_blueprint
    from .routes.requests import blueprint as requests_blueprint
    from .routes.settings import blueprint as settings_blueprint
    from .security import verify_csrf_token
    from .storage import init_db
else:  # Support the existing `python backend/app.py` entry point.
    from api_cache import init_cache_db, migrate_legacy_cache
    from config import FRONTEND_ROOT, load_session_secret
    from routes.account import blueprint as account_blueprint
    from routes.artwork import blueprint as artwork_blueprint
    from routes.auth import blueprint as auth_blueprint
    from routes.discovery import blueprint as discovery_blueprint
    from routes.library import blueprint as library_blueprint
    from routes.music import blueprint as music_blueprint
    from routes.pages import blueprint as pages_blueprint
    from routes.requests import blueprint as requests_blueprint
    from routes.settings import blueprint as settings_blueprint
    from security import verify_csrf_token
    from storage import init_db


STATIC_CACHE_TTL = 365 * 24 * 60 * 60
COMPRESSIBLE_MIMETYPES = frozenset({
    "application/javascript",
    "application/json",
    "application/manifest+json",
    "image/svg+xml",
    "text/css",
    "text/html",
    "text/javascript",
    "text/plain",
})
COMPRESSION_MINIMUM_BYTES = 1024


def compress_response(response):
    """Gzip large text responses so mobile clients transfer far fewer bytes.

    Artwork is already stored in compressed image formats, and its mimetype
    keeps it out of this path so it stays streamed straight from disk. Static
    scripts and stylesheets are streamed too, so passthrough is turned off for
    them specifically in order to read and compress the body.
    """
    response.vary.add("Accept-Encoding")
    if (
        response.status_code != 200
        or "Content-Encoding" in response.headers
        or "Content-Range" in response.headers
        or response.mimetype not in COMPRESSIBLE_MIMETYPES
        or "gzip" not in request.headers.get("Accept-Encoding", "")
    ):
        return response
    response.direct_passthrough = False
    body = response.get_data()
    if len(body) < COMPRESSION_MINIMUM_BYTES:
        return response
    response.set_data(gzip.compress(body, compresslevel=6))
    response.headers["Content-Encoding"] = "gzip"
    response.headers["Content-Length"] = response.content_length
    return response


def serve_precompressed_static():
    """Serve build-time Brotli or gzip files without recompressing per request."""
    if request.method not in {"GET", "HEAD"} or not request.path.startswith("/static/"):
        return None
    relative_path = request.path[len("/static/"):]
    source_path = safe_join(current_app.static_folder, relative_path)
    if source_path is None:
        return None

    encodings = sorted(
        (
            (request.accept_encodings[encoding], preference, encoding)
            for preference, encoding in ((1, "br"), (0, "gzip"))
            if request.accept_encodings[encoding] > 0
        ),
        reverse=True,
    )
    for _, _, encoding in encodings:
        suffix = ".br" if encoding == "br" else ".gz"
        compressed_path = f"{source_path}{suffix}"
        if not os.path.isfile(compressed_path):
            continue
        response = send_file(
            compressed_path,
            conditional=True,
            max_age=STATIC_CACHE_TTL,
            mimetype=mimetypes.guess_type(source_path)[0],
        )
        response.headers["Content-Encoding"] = encoding
        response.vary.add("Accept-Encoding")
        return response
    return None


def cache_static_assets(response):
    """Let browsers keep fingerprinted assets without revalidating them."""
    if request.path.startswith("/static/") and response.status_code == 200:
        response.headers["Cache-Control"] = (
            f"public, max-age={STATIC_CACHE_TTL}, immutable"
        )
    return response


BLUEPRINTS = (
    account_blueprint,
    artwork_blueprint,
    auth_blueprint,
    discovery_blueprint,
    library_blueprint,
    music_blueprint,
    requests_blueprint,
    settings_blueprint,
    pages_blueprint,
)


def create_app(config=None):
    """Construct and configure a Melodarr Flask application."""
    app = Flask(
        __name__,
        static_folder=os.path.join(FRONTEND_ROOT, "static"),
        static_url_path="/static",
    )
    app.config.update(
        SECRET_KEY=load_session_secret(),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.getenv("MELODARR_COOKIE_SECURE", "false").lower() == "true",
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
        SEND_FILE_MAX_AGE_DEFAULT=STATIC_CACHE_TTL,
    )
    if config:
        app.config.update(config)

    init_cache_db()
    migrate_legacy_cache()
    init_db()
    app.before_request(verify_csrf_token)
    app.before_request(serve_precompressed_static)
    app.after_request(cache_static_assets)
    app.after_request(compress_response)
    for blueprint in BLUEPRINTS:
        app.register_blueprint(blueprint)
    return app
