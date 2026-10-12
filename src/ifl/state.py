"""The state file: what the service remembers between runs.

One JSON file. Per member, the re-upload scan's mark and a tally of the
re-uploads trashed since the last daily report, plus when that report last
ran. Nothing in it is needed for correctness: the move derives its state from
Immich, and the `ifl` entry on each family copy records where it came from.
Losing the file restarts the marks at the next pass and drops one report.

Every method that changes the state saves before it returns, so the file
always equals memory. The daemon's pass and webhook batch share one state on
one event loop, and a change and its save happen in one synchronous step, so
neither half can interleave with the other's write. A crash or a kill loses
nothing either. The price is deliberate: a pass rewrites the file a few times
per member where one batched save would do. The file is a few hundred bytes,
and a batched save would bring back a window, between a change and its save,
that every caller would have to keep free of `await`.

This module also holds the run lock, which is not a lock on the state file.
Only one command that writes to Immich runs at a time, because two at once
could create the same album or workflow twice, or work one photo. The daemon
takes the run lock for as long as it runs, and a one-off writing command for
its own length, whether or not it uses the state. The lock file sits beside
the state file only because that directory is the one place the service can
write. Its side effect is that the holder is the state file's only writer:
it loads the file once, when it starts, and the file needs no lock of its
own. `observe` only reads. Editing the file by hand is not supported.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from uuid import UUID

log = logging.getLogger(__name__)


class Locked(Exception):
    pass


class StateError(Exception):
    """The state file cannot be read, or the run lock cannot be opened."""


@dataclass
class MemberState:
    scanned_until: datetime | None = None  # the re-upload scan's mark; None means never scanned
    reuploads: int = 0  # trashed re-uploads since the last report


@dataclass
class State:
    path: Path
    members: dict[UUID, MemberState] = field(default_factory=dict)
    last_report: datetime | None = None

    @classmethod
    def load(cls, path: Path, *, owner: bool = False) -> State:
        """The file's contents, or an empty state when there is no file.

        A file that does not parse starts the state over. The lock holder, with
        `owner`, first moves it aside; a reader leaves it where it is. A file
        that cannot be read at all is an error, never a reason to start over:
        a passing fault would otherwise throw the marks away."""
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return cls(path)
        except OSError as e:
            raise StateError(f"cannot read the state file {path}: {e}") from None
        try:
            raw = json.loads(data.decode())
            members = {
                UUID(uid): MemberState(
                    scanned_until=_when(m.get("scanned_until")),
                    reuploads=int(m.get("reuploads", 0)),
                )
                for uid, m in raw.get("members", {}).items()
            }
            return cls(path, members, _when(raw.get("last_report")))
        except (ValueError, TypeError, AttributeError, OverflowError) as e:
            if not owner:
                log.warning("%s does not parse (%s); reading it as empty", path, e)
                return cls(path)
            aside = path.with_name(path.name + ".bad")
            try:
                path.replace(aside)
            except OSError as err:
                raise StateError(f"cannot move the bad state file {path} aside: {err}") from None
            log.warning("%s does not parse (%s); moved to %s and starting over", path, e, aside)
            return cls(path)

    def save(self) -> None:
        """Write the file. Every mutating method calls this; calling it again is harmless.

        A write that fails is logged and the run goes on: memory is still right,
        the next change tries again, and a pass that has already trashed
        re-uploads must still advance the other marks."""
        raw = {
            "last_report": _iso(self.last_report),
            "members": {
                str(uid): {"scanned_until": _iso(m.scanned_until), "reuploads": m.reuploads}
                for uid, m in sorted(self.members.items(), key=lambda kv: str(kv[0]))
            },
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            # A leftover from a run under another `user:` cannot be opened for writing.
            tmp.unlink(missing_ok=True)
            tmp.write_text(json.dumps(raw, indent=2) + "\n")
            tmp.replace(self.path)
        except OSError as e:
            log.error("cannot write the state file %s: %s", self.path, e)

    def _member(self, user_id: UUID) -> MemberState:
        return self.members.setdefault(user_id, MemberState())

    def get_mark(self, user_id: UUID) -> datetime | None:
        m = self.members.get(user_id)
        return m.scanned_until if m else None

    def set_mark(self, user_id: UUID, until: datetime) -> None:
        self._member(user_id).scanned_until = until
        self.save()

    def add_reuploads(self, user_id: UUID, n: int) -> None:
        self._member(user_id).reuploads += n
        self.save()

    def keep_only(self, user_ids: set[UUID]) -> None:
        """Drop every member not in `user_ids`. A member taken out of the config
        and added back later starts as new, so nothing from the gap is trashed."""
        gone = sorted(str(uid) for uid in self.members.keys() - user_ids)
        if not gone:
            return
        for uid in gone:
            del self.members[UUID(uid)]
        log.info("dropped state for %d user(s) no longer in the config: %s", len(gone), gone)
        self.save()

    def start_clock(self, now: datetime) -> None:
        """The first report time, from which the next report is due. Says nothing."""
        self.last_report = now
        self.save()

    def report_due(self, now: datetime, at: time) -> bool:
        """Whether the daily report time has passed since the last report. A
        state that has never reported is not due: the caller starts the clock."""
        if self.last_report is None:
            return False
        scheduled = now.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)
        if scheduled > now:
            scheduled -= timedelta(days=1)
        return self.last_report < scheduled

    def take_report(self, now: datetime) -> dict[UUID, int]:
        """The non-zero tallies, which are reset, and the report time moves to now."""
        out = {uid: m.reuploads for uid, m in self.members.items() if m.reuploads}
        for m in self.members.values():
            m.reuploads = 0
        self.last_report = now
        self.save()
        return out


def _when(value: str | None) -> datetime | None:
    if not value:
        return None
    when = datetime.fromisoformat(value)
    # The service only writes times with an offset; one without is read as UTC
    # so that it still compares.
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


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
