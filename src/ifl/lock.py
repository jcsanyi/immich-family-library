"""The run lock.

Only one command that writes to Immich runs at a time, because two at once
could create the same album or workflow twice, or work one photo. The daemon
takes the run lock for as long as it runs, and a one-off writing command for
its own length. `observe` only reads and never takes it.

The lock is a file, so it only works between processes that see the same
file. A command in a second container needs the lock on a volume that both
containers mount. The default is beside the config file, which is one lock per
config and which a read-only config mount refuses loudly.
"""

from __future__ import annotations

import fcntl
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger(__name__)


class Locked(Exception):
    pass


class LockError(Exception):
    """The run lock cannot be opened."""


@contextmanager
def hold_lock(path: Path) -> Iterator[None]:
    """Hold the run lock, the file at `path`, for the length of the block.
    Refuses at once if another process holds it. The kernel releases a flock
    when its holder dies, so a crash leaves nothing to clean up."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Read-only is enough for flock, and it lets a different `user:` from
        # the one that created the file take the lock.
        fd = os.open(path, os.O_RDONLY | os.O_CREAT, 0o644)
    except OSError as e:
        raise LockError(f"cannot open the run lock {path}: {e}") from None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Locked(
                "only one command that writes to Immich can run at a time, and another "
                f"holds the run lock {path}; is the daemon running?"
            ) from None
        yield
    finally:
        os.close(fd)
