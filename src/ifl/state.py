"""The run lock.

Only one command that writes to Immich runs at a time, because two at once
could create the same album or workflow twice, or work one photo. The daemon
takes the run lock for as long as it runs, and a one-off writing command for
its own length. `observe` only reads and never takes it.
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


class StateError(Exception):
    """The run lock cannot be opened."""


def lock_path(state_path: Path) -> Path:
    return state_path.with_name(state_path.name + ".lock")


@contextmanager
def hold_lock(state_path: Path) -> Iterator[None]:
    """Hold the run lock for the length of the block. Refuses at once if
    another process holds it. The kernel releases a flock when its holder dies,
    so a crash leaves nothing to clean up."""
    path = lock_path(state_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Read-only is enough for flock, and it lets a different `user:` from
        # the one that created the file take the lock.
        fd = os.open(path, os.O_RDONLY | os.O_CREAT, 0o644)
    except OSError as e:
        raise StateError(f"cannot open the run lock {path}: {e}") from None
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
