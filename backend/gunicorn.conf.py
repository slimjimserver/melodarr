"""Gunicorn production configuration for Melodarr's single-container runtime."""

bind = "0.0.0.0:5056"
workers = 1
worker_class = "gthread"
threads = 16
# Query-aware local inference can make one small planning call and one ranking
# call, each with a bounded four-minute provider timeout. Keep the worker above
# that combined ceiling so Gunicorn does not cancel a healthy local request.
timeout = 600
preload_app = False
control_socket_disable = True
accesslog = "-"
errorlog = "-"
capture_output = True


def post_worker_init(worker):
    """Start exactly one recommendation loop after the web worker is ready."""
    from backend.worker import start_background_thread

    start_background_thread()
    worker.log.info("Background workers started")
