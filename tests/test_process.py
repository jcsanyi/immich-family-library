"""One pass end to end against the fake Immich. No network."""

from __future__ import annotations

from uuid import uuid4

import pytest
from fakes import ALICE, BOB, EDITOR, FAMILY, FakeAsset, FakeClient, make_accounts, patch_world

from ifl.accounts import Account
from ifl.config import parse_config
from ifl.ledger import Ledger
from ifl.process import run_pass

RAW = {
    "immich": {"url": "https://x.example"},
    "service": {"readonly": False, "settle_seconds": 0},
    "family": {"email": "f@x", "api_key": "k", "dropbox_album": "Drop"},
    "members": {
        "alice": {"email": "alice@x", "api_key": "k"},
        "bob": {"email": "bob@x", "api_key": "k"},
    },
}


@pytest.fixture
def world(monkeypatch):
    w = patch_world(monkeypatch, "ifl.observer", "ifl.convert", "ifl.reconcile", "ifl.move")
    w.partners[FAMILY] = {ALICE, BOB}
    return w


@pytest.fixture
def ledger(tmp_path):
    led = Ledger(tmp_path / "ledger.sqlite")
    yield led
    led.close()


async def test_a_pass_converts_reconciles_and_moves_in_order(world, ledger):
    accounts = make_accounts()
    cfg = parse_config(RAW)
    # A member album shared with the family: converted, then its photo moved, same pass.
    beach = world.add_asset(
        FakeAsset(uuid4(), ALICE, checksum="beach", original_file_name="beach.jpg")
    )
    world.album("Beach", ALICE, roles={FAMILY: EDITOR}, assets=[beach])
    # A family album missing bob: reconciled. Its photo moves too.
    trip_photo = world.add_asset(
        FakeAsset(uuid4(), ALICE, checksum="trip", original_file_name="trip.jpg")
    )
    trip = world.album("Trip", FAMILY, roles={ALICE: EDITOR}, assets=[trip_photo])
    # No dropbox yet: created.

    result = await run_pass(accounts, cfg, ledger)

    assert len(result.converted.done) == 1 and not result.converted.failed
    assert result.reconciled and result.reconciled.created_dropbox
    assert result.reconciled.shared == 2 + 1  # dropbox gets both members; bob added to Trip
    assert result.backlog is None and result.moved is not None
    assert sorted(r.file_name for r in result.moved.done) == ["beach.jpg", "trip.jpg"]
    assert beach.is_trashed and trip_photo.is_trashed
    # Trashed originals stay in the album rows, as in Immich; the live contents are the copies.
    (fam_beach,) = world.family_albums_named("Beach")
    for album in (fam_beach, trip):
        live = [a for a in world.assets[album.id] if not a.is_trashed]
        assert len(live) == 1 and live[0].owner_id == FAMILY
    assert ledger.counts() == {"moved": 2, "reclaimed": 0, "reuploads": 0}
    assert not result.failed
    assert str(result).startswith(
        "albums: converted 1, failed 0, remaining 0; family albums: created dropbox"
    )

    # A second pass finds nothing to do.
    again = await run_pass(accounts, cfg, ledger)
    assert not again.converted.done and again.reconciled is None
    assert again.moved is not None and not again.moved.done
    assert str(again).endswith("assets: moved 0, blocked 0, failed 0, remaining 0")


async def test_limits_apply_per_pass(world, ledger):
    accounts = make_accounts()
    cfg = parse_config({**RAW, "limits": {"albums_per_pass": 1, "assets_per_pass": 1}})
    world.album("Drop", FAMILY, roles={ALICE: EDITOR, BOB: EDITOR})
    for name in ("A", "B"):
        a = world.add_asset(
            FakeAsset(uuid4(), ALICE, checksum=name, original_file_name=f"{name}.jpg")
        )
        world.album(name, ALICE, roles={FAMILY: EDITOR}, assets=[a])

    result = await run_pass(accounts, cfg, ledger)

    assert len(result.converted.done) == 1 and result.converted.remaining == 1
    assert result.moved and len(result.moved.done) == 1 and result.moved.remaining == 0
    # Only the converted album's photo was movable this pass; the other is still in alice's album.


async def test_a_queue_backlog_skips_the_moves_but_not_the_albums(world, ledger):
    accounts = make_accounts()
    accounts.admin = Account(
        name="admin", email="root@x", user_id=uuid4(), client=FakeClient(uuid4())
    )
    cfg = parse_config(RAW)
    world.album("Drop", FAMILY, roles={ALICE: EDITOR, BOB: EDITOR})
    a = world.add_asset(FakeAsset(uuid4(), ALICE, checksum="x", original_file_name="x.jpg"))
    world.album("X", ALICE, roles={FAMILY: EDITOR}, assets=[a])
    world.queues = {"metadataExtraction": 40}

    result = await run_pass(accounts, cfg, ledger)

    assert len(result.converted.done) == 1
    assert result.backlog == {"metadataExtraction": 40} and result.moved is None
    assert not a.is_trashed and not result.failed
    assert "skipped, Immich has jobs waiting" in str(result)


async def test_reuploads_are_handled_and_marks_advance_only_past_what_finished(world, ledger):
    from datetime import UTC, datetime, timedelta

    accounts = make_accounts()
    cfg = parse_config({**RAW, "service": {"readonly": False, "settle_seconds": 60}})
    world.album("Drop", FAMILY, roles={ALICE: EDITOR, BOB: EDITOR})
    old = datetime(2026, 10, 1, tzinfo=UTC)
    fresh = datetime.now(UTC) - timedelta(seconds=5)
    copy = world.add_asset(
        FakeAsset(uuid4(), FAMILY, checksum="beach", original_file_name="beach.jpg")
    )
    world.metadata[copy.id] = {"ifl": {"origin_asset_id": str(uuid4())}}
    settled = world.add_asset(
        FakeAsset(
            uuid4(),
            ALICE,
            checksum="beach",
            original_file_name="beach.jpg",
            created_at=old,
            updated_at=old,
        )
    )
    too_new = world.add_asset(
        FakeAsset(
            uuid4(),
            BOB,
            checksum="beach",
            original_file_name="beach.jpg",
            created_at=fresh,
            updated_at=fresh,
        )
    )

    result = await run_pass(accounts, cfg, ledger)

    assert [r.file_name for r in result.moved.done] == ["beach.jpg"] and result.moved.done[
        0
    ].duplicate
    assert settled.is_trashed and not too_new.is_trashed
    assert [b[1] for b in result.moved.blocked][0].endswith("settling")
    # alice: everything handled, marked at the scan time. bob: held at his unfinished upload.
    assert ledger.get_mark(ALICE) is not None and ledger.get_mark(ALICE) > fresh
    assert ledger.get_mark(BOB) == fresh
    assert ledger.counts()["reuploads"] == 1
    # Later, bob's has settled: found again from the held-back mark, handled, mark released.
    too_new.updated_at = old
    again = await run_pass(accounts, cfg, ledger)
    assert [r.file_name for r in again.moved.done] == ["beach.jpg"] and too_new.is_trashed
    assert ledger.get_mark(BOB) > fresh and ledger.counts()["reuploads"] == 2
