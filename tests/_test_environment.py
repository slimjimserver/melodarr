"""Process-wide filesystem isolation for the backend test suite.

This module must be imported before any backend module.  ``backend.config``
resolves its paths once at import time, so per-test-module environment setup is
unsafe: test discovery is free to import modules in any order.
"""

import os
import sys
import tempfile


peer_name = (
    "tests._test_environment"
    if __name__ == "_test_environment"
    else "_test_environment"
)
peer = sys.modules.get(peer_name)
if peer is not None and hasattr(peer, "TEST_ROOT"):
    # A runner may mix ``test_backend`` and ``tests.test_backend`` imports in
    # one process.  Both module names must keep the first registered root.
    TEST_DATA = peer.TEST_DATA
    TEST_ROOT = peer.TEST_ROOT
else:
    TEST_DATA = tempfile.TemporaryDirectory(prefix="melodarr-tests-")
    TEST_ROOT = os.path.realpath(TEST_DATA.name)

os.environ.update({
    "MELODARR_TEST_ROOT": TEST_ROOT,
    "MELODARR_DATABASE": os.path.join(TEST_ROOT, "melodarr.db"),
    "MELODARR_CACHE_DATABASE": os.path.join(TEST_ROOT, "cache", "metadata.db"),
    "MELODARR_SETTINGS": os.path.join(TEST_ROOT, "settings.json"),
    "MELODARR_SECRET_KEY_FILE": os.path.join(TEST_ROOT, "session-secret.key"),
    "MELODARR_ARTWORK_CACHE": os.path.join(TEST_ROOT, "artwork"),
})


def _is_within_test_root(path):
    try:
        return os.path.commonpath((TEST_ROOT, os.path.realpath(path))) == TEST_ROOT
    except ValueError:
        return False


# Changing the environment cannot repair backend modules that another import
# has already initialized.  Refuse to continue instead of allowing a test to
# mutate whichever paths those modules captured.
configured = sys.modules.get("backend.config")
if configured is not None:
    captured_paths = {
        "DATABASE": configured.DATABASE,
        "CACHE_DATABASE": configured.CACHE_DATABASE,
        "SETTINGS_FILE": configured.SETTINGS_FILE,
        "ARTWORK_CACHE_DIRECTORY": configured.ARTWORK_CACHE_DIRECTORY,
        "SECRET_KEY_FILE": configured.SECRET_KEY_FILE,
    }
    unsafe = {
        name: path
        for name, path in captured_paths.items()
        if not _is_within_test_root(path)
    }
    if unsafe:
        details = ", ".join(f"{name}={path!r}" for name, path in unsafe.items())
        raise RuntimeError(
            "backend.config was imported before test isolation was installed: "
            f"{details}"
        )
