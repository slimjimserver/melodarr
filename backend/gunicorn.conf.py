"""Gunicorn production configuration for Melodarr's single-container runtime."""

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

    start_background_thread()
    worker.log.info("Background workers started")
