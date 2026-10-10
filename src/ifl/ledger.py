"""The ledger: a record of what moved, from whom, and where it went.

One SQLite table keyed by checksum. It's a record, not a controller: the move
path derives its state from Immich. The ledger exists for the two things
Immich can't tell us once an original is purged, who a family asset came from
and who is re-uploading, and doubles as the audit trail.

"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

SCHEMA = """
CREATE TABLE IF NOT EXISTS moves (
    checksum        TEXT PRIMARY KEY,
    origin_user_id  TEXT NOT NULL,  -- Immich user id; config labels are never stored
    origin_asset_id TEXT NOT NULL,
    family_asset_id TEXT,
    file_name       TEXT,
    moved_at        TEXT NOT NULL,
    reclaimed_at    TEXT,
    reupload_count  INTEGER NOT NULL DEFAULT 0
);
-- How far the re-upload scan has looked, per member. A record: losing it means
-- one slow full rescan, nothing worse.
CREATE TABLE IF NOT EXISTS scan_marks (
    user_id         TEXT PRIMARY KEY,
    scanned_until   TEXT NOT NULL
);
"""


class Ledger:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    def record_move(
        self,
        *,
        checksum: str,
        origin_user_id: UUID,
        origin_asset_id: UUID,
        family_asset_id: UUID,
        file_name: str,
    ) -> None:
        """Insert or refresh the row for this checksum. A re-move after a reclaim
        clears reclaimed_at; the re-upload count is kept."""
        self.db.execute(
            """
            INSERT INTO moves (checksum, origin_user_id, origin_asset_id, family_asset_id,
                               file_name, moved_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(checksum) DO UPDATE SET
                origin_user_id = excluded.origin_user_id,
                origin_asset_id = excluded.origin_asset_id,
                family_asset_id = excluded.family_asset_id,
                file_name = excluded.file_name,
                moved_at = excluded.moved_at,
                reclaimed_at = NULL
            """,
            (
                checksum,
                str(origin_user_id),
                str(origin_asset_id),
                str(family_asset_id),
                file_name,
                datetime.now(UTC).isoformat(timespec="seconds"),
            ),
        )
        self.db.commit()

    def count_reupload(
        self,
        *,
        checksum: str,
        origin_user_id: UUID,
        origin_asset_id: UUID,
        family_asset_id: UUID,
        file_name: str,
    ) -> int:
        """One more re-upload of this checksum. A photo the family got some other
        way has no row yet; it gets one naming this member. Returns the new count."""
        self.db.execute(
            """
            INSERT INTO moves (checksum, origin_user_id, origin_asset_id, family_asset_id,
                               file_name, moved_at, reupload_count)
            VALUES (?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(checksum) DO UPDATE SET reupload_count = reupload_count + 1
            """,
            (
                checksum,
                str(origin_user_id),
                str(origin_asset_id),
                str(family_asset_id),
                file_name,
                datetime.now(UTC).isoformat(timespec="seconds"),
            ),
        )
        self.db.commit()
        (n,) = self.db.execute(
            "SELECT reupload_count FROM moves WHERE checksum = ?", (checksum,)
        ).fetchone()
        return n

    def get(self, checksum: str) -> dict | None:
        row = self.db.execute("SELECT * FROM moves WHERE checksum = ?", (checksum,)).fetchone()
        if row is None:
            return None
        return dict(
            zip(
                [c[0] for c in self.db.execute("SELECT * FROM moves LIMIT 0").description],
                row,
                strict=True,
            )
        )

    def get_mark(self, user_id: UUID) -> datetime | None:
        row = self.db.execute(
            "SELECT scanned_until FROM scan_marks WHERE user_id = ?", (str(user_id),)
        ).fetchone()
        return datetime.fromisoformat(row[0]) if row else None

    def set_mark(self, user_id: UUID, until: datetime) -> None:
        self.db.execute(
            "INSERT INTO scan_marks (user_id, scanned_until) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET scanned_until = excluded.scanned_until",
            (str(user_id), until.isoformat()),
        )
        self.db.commit()

    def counts(self) -> dict[str, int]:
        (moved,) = self.db.execute("SELECT COUNT(*) FROM moves").fetchone()
        (reclaimed,) = self.db.execute(
            "SELECT COUNT(*) FROM moves WHERE reclaimed_at IS NOT NULL"
        ).fetchone()
        (reuploads,) = self.db.execute(
            "SELECT COALESCE(SUM(reupload_count), 0) FROM moves"
        ).fetchone()
        return {"moved": moved, "reclaimed": reclaimed, "reuploads": reuploads}
