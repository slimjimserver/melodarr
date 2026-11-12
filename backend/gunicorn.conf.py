"""Gunicorn production configuration for Melodarr's single-container runtime."""

import logging

bind = "0.0.0.0:5056"
workers = 1
worker_class = "gthread"
threads = 16
timeout = 60
preload_app = False
control_socket_disable = True
accesslog = "-"
# Invitation query strings and incoming Referer headers can contain bearer
# secrets. Keep method/path/status logging without either URL-bearing field.
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(m)s %(U)s %(H)s" %(s)s %(b)s "%(a)s"'
errorlog = "-"
capture_output = True


def post_worker_init(worker):
    """Start exactly one recommendation loop after the web worker is ready."""
    from backend.worker import start_background_thread

    # Service loggers otherwise fall back to WARNING and drop timing records.
    # Reuse Gunicorn's stderr destination without changing other logger levels.
    summary_logger = logging.getLogger("backend.services.artist_summary")
    gunicorn_logger = logging.getLogger("gunicorn.error")
    if gunicorn_logger.handlers:
        summary_logger.handlers = list(gunicorn_logger.handlers)
        summary_logger.setLevel(gunicorn_logger.level)
        summary_logger.propagate = False

    start_background_thread()
    worker.log.info("Background workers started")
