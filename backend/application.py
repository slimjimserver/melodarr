"""Flask application factory and extension registration."""

import os
from datetime import timedelta

from flask import Flask

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
    )
    if config:
        app.config.update(config)

    init_cache_db()
    migrate_legacy_cache()
    init_db()
    app.before_request(verify_csrf_token)
    for blueprint in BLUEPRINTS:
        app.register_blueprint(blueprint)
    return app
