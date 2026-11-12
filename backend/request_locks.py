"""OS-backed initiation locks shared by threads and web processes.

Lock files live beside the durable database, so all processes using that
database coordinate. Never unlink a lock file: waiters must share its inode.
The OS releases locks on close/process exit; no stale lease can block retries.
"""

import errno
import os
import time
from contextlib import contextmanager
from hashlib import sha256

if os.name == "nt":
    import msvcrt
else:
    import fcntl

if __package__:
    from . import storage
else:
    import storage


@contextmanager
def request_lock(kind, identity, *, timeout=45):
    directory = os.path.join(os.path.dirname(os.path.abspath(storage.DATABASE)), "request-locks")
    os.makedirs(directory, exist_ok=True)
    key = sha256(f"{kind}:{identity}".encode()).hexdigest()
    # Each acquisition uses an independent open file description (including in
    # one process). Opening without truncation preserves Windows byte locks.
    with open(os.path.join(directory, key + ".lock"), "a+b") as handle:
        deadline = time.monotonic() + timeout
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError("Request initiation is busy.") from None
                time.sleep(0.05)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
