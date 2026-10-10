"""The ledger: a record of what moved, from whom, and where it went.

One SQLite table keyed by checksum. It's a record, not a controller: the move
path derives its state from Immich. The ledger exists for the two things
Immich can't tell us once an original is purged, who a family asset came from
and who is re-uploading, and doubles as the audit trail.

Stage 1 moves nothing, so this only creates the file and reports counts.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

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
"""


class Ledger:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    def counts(self) -> dict[str, int]:
        (moved,) = self.db.execute("SELECT COUNT(*) FROM moves").fetchone()
        (reclaimed,) = self.db.execute(
            "SELECT COUNT(*) FROM moves WHERE reclaimed_at IS NOT NULL"
        ).fetchone()
        return {"moved": moved, "reclaimed": reclaimed}
