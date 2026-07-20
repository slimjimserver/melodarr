"""Melodarr development executable and production WSGI entry point."""

import os

if __package__:
    from .application import create_app
else:  # Support the existing `python backend/app.py` entry point.
    from application import create_app


debug = os.getenv("FLASK_DEBUG") == "1"
app = create_app()


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "5056")),
        debug=debug,
    )
