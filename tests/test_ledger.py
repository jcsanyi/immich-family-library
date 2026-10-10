from ifl.ledger import Ledger


def test_creates_file_and_starts_empty(tmp_path):
    path = tmp_path / "sub" / "ledger.sqlite"
    ledger = Ledger(path)
    try:
        assert path.exists()
        assert ledger.counts() == {"moved": 0, "reclaimed": 0, "reuploads": 0}
    finally:
        ledger.close()


def test_record_move_upserts_and_clears_reclaim(tmp_path):
    from uuid import uuid4

    from ifl.ledger import Ledger

    led = Ledger(tmp_path / "l.sqlite")
    alice, orig, fam = uuid4(), uuid4(), uuid4()
    led.record_move(
        checksum="c1",
        origin_user_id=alice,
        origin_asset_id=orig,
        family_asset_id=fam,
        file_name="a.jpg",
    )
    assert led.counts() == {"moved": 1, "reclaimed": 0, "reuploads": 0}
    led.db.execute("UPDATE moves SET reclaimed_at = 'x', reupload_count = 3 WHERE checksum = 'c1'")
    fam2 = uuid4()
    led.record_move(
        checksum="c1",
        origin_user_id=alice,
        origin_asset_id=orig,
        family_asset_id=fam2,
        file_name="a.jpg",
    )
    row = led.get("c1")
    assert (
        row["family_asset_id"] == str(fam2)
        and row["reclaimed_at"] is None
        and row["reupload_count"] == 3
    )
    assert led.get("nope") is None
    led.close()
