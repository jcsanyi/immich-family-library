"""One pass end to end against the fake Immich. No network."""

from __future__ import annotations

from uuid import uuid4

import pytest
from fakes import (
    ALICE,
    BOB,
    EDITOR,
    FAMILY,
    TEMP_DIR,
    FakeAsset,
    FakeClient,
    make_accounts,
    patch_world,
)

from ifl.accounts import Account
from ifl.config import parse_config
from ifl.process import report_reuploads, run_pass
from ifl.state import State

RAW = {
    "immich": {"url": "https://x.example"},
    "service": {"readonly": False, "settle_seconds": 0, "temp_dir": str(TEMP_DIR)},
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
def state(tmp_path):
    return State(tmp_path / "state.json")


async def test_a_pass_converts_reconciles_and_moves_in_order(world, state):
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

    result = await run_pass(accounts, cfg, state)

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
    # The state is saved: both marks started, the report clock started.
    saved = State.load(state.path)
    assert saved.get_mark(ALICE) == state.get_mark(ALICE) is not None
    assert saved.last_report is not None
    assert not result.failed
    assert str(result).startswith(
        "albums: converted 1, failed 0, remaining 0; family albums: created dropbox"
    )

    # A second pass finds nothing to do.
    again = await run_pass(accounts, cfg, state)
    assert not again.converted.done and again.reconciled is None
    assert again.moved is not None and not again.moved.done
    assert str(again).endswith("assets: moved 0, blocked 0, failed 0, remaining 0")


async def test_limits_apply_per_pass(world, state):
    accounts = make_accounts()
    cfg = parse_config({**RAW, "limits": {"albums_per_pass": 1, "assets_per_pass": 1}})
    world.album("Drop", FAMILY, roles={ALICE: EDITOR, BOB: EDITOR})
    for name in ("A", "B"):
        a = world.add_asset(
            FakeAsset(uuid4(), ALICE, checksum=name, original_file_name=f"{name}.jpg")
        )
        world.album(name, ALICE, roles={FAMILY: EDITOR}, assets=[a])

    result = await run_pass(accounts, cfg, state)

    assert len(result.converted.done) == 1 and result.converted.remaining == 1
    assert result.moved and len(result.moved.done) == 1 and result.moved.remaining == 0
    # Only the converted album's photo was movable this pass; the other is still in alice's album.


async def test_a_queue_backlog_skips_the_moves_but_not_the_albums(world, state):
    accounts = make_accounts()
    accounts.admin = Account(
        name="admin", email="root@x", user_id=uuid4(), client=FakeClient(uuid4())
    )
    cfg = parse_config(RAW)
    world.album("Drop", FAMILY, roles={ALICE: EDITOR, BOB: EDITOR})
    a = world.add_asset(FakeAsset(uuid4(), ALICE, checksum="x", original_file_name="x.jpg"))
    world.album("X", ALICE, roles={FAMILY: EDITOR}, assets=[a])
    world.queues = {"metadataExtraction": 40}

    result = await run_pass(accounts, cfg, state)

    assert len(result.converted.done) == 1
    assert result.backlog == {"metadataExtraction": 40} and result.moved is None
    assert not a.is_trashed and not result.failed
    assert "skipped, Immich has jobs waiting" in str(result)


async def test_reuploads_are_handled_and_marks_advance_only_past_what_finished(world, state):
    from datetime import UTC, datetime, timedelta

    accounts = make_accounts()
    cfg = parse_config(RAW)
    world.album("Drop", FAMILY, roles={ALICE: EDITOR, BOB: EDITOR})
    old = datetime(2026, 10, 1, tzinfo=UTC)
    fresh = datetime.now(UTC) - timedelta(seconds=5)
    copy = world.add_asset(
        FakeAsset(uuid4(), FAMILY, checksum="beach", original_file_name="beach.jpg")
    )
    world.metadata[copy.id] = {"ifl": {"origin_asset_id": str(uuid4())}}
    # A re-upload needs no settling: seconds old and not yet extracted, it still goes.
    alices = world.add_asset(
        FakeAsset(
            uuid4(),
            ALICE,
            checksum="beach",
            original_file_name="beach.jpg",
            created_at=fresh,
            updated_at=fresh,
            exif_info=None,
        )
    )
    # Bob's is in one of his shared links, so it's blocked this pass.
    bobs = world.add_asset(
        FakeAsset(uuid4(), BOB, checksum="beach", original_file_name="beach.jpg", created_at=old)
    )
    world.shared_links[BOB] = {bobs.id}
    for uid in (ALICE, BOB):
        state.set_mark(uid, old - timedelta(days=1))  # both have been scanned before

    result = await run_pass(accounts, cfg, state)

    assert [r.file_name for r in result.moved.done] == ["beach.jpg"] and result.moved.done[
        0
    ].duplicate
    assert alices.is_trashed and not bobs.is_trashed
    assert [b[1] for b in result.moved.blocked] == ["in one of the owner's shared links"]
    # alice: everything handled, marked at the scan time. bob: held at his unfinished upload.
    assert state.get_mark(ALICE) is not None and state.get_mark(ALICE) > fresh
    assert state.get_mark(BOB) == old
    assert state.members[ALICE].reuploads == 1
    # Bob removes it from the link: found again from the held-back mark, handled, mark released.
    world.shared_links[BOB] = set()
    again = await run_pass(accounts, cfg, state)
    assert [r.file_name for r in again.moved.done] == ["beach.jpg"] and bobs.is_trashed
    assert state.get_mark(BOB) > old and state.members[BOB].reuploads == 1


async def test_in_flight_assets_are_left_to_the_other_path(world, state):
    accounts = make_accounts()
    cfg = parse_config(RAW)
    world.album("Drop", FAMILY, roles={ALICE: EDITOR, BOB: EDITOR})
    copy = world.add_asset(
        FakeAsset(uuid4(), FAMILY, checksum="beach", original_file_name="beach.jpg")
    )
    world.metadata[copy.id] = {"ifl": {"origin_asset_id": str(uuid4())}}
    re = world.add_asset(
        FakeAsset(uuid4(), ALICE, checksum="beach", original_file_name="beach.jpg")
    )
    busy = {re.id}  # the receiver has it
    from datetime import UTC, datetime

    state.set_mark(ALICE, datetime(2025, 1, 1, tzinfo=UTC))

    result = await run_pass(accounts, cfg, state, in_flight=busy)

    assert result.moved.skipped_in_flight == 1 and not result.moved.done and not re.is_trashed
    assert state.get_mark(ALICE) == re.created_at  # held back, in case the other path fails
    assert busy == {re.id}  # not ours to remove


async def test_a_first_pass_starts_each_members_mark_at_now_without_reading_their_library(
    world, state
):
    from datetime import UTC, datetime

    accounts = make_accounts()
    cfg = parse_config(RAW)
    world.album("Drop", FAMILY, roles={ALICE: EDITOR, BOB: EDITOR})
    # A photo both the family and alice held before the service: left alone.
    world.add_asset(FakeAsset(uuid4(), FAMILY, checksum="beach", original_file_name="beach.jpg"))
    alices = world.add_asset(
        FakeAsset(uuid4(), ALICE, checksum="beach", original_file_name="beach.jpg")
    )
    before = datetime.now(UTC)

    result = await run_pass(accounts, cfg, state)

    assert not result.moved.done and not alices.is_trashed
    assert all(state.get_mark(m.user_id) >= before for m in accounts.members.values())
    assert all(m.reuploads == 0 for m in state.members.values())


async def test_reconcile_sets_up_each_members_webhook_workflow(world, state):
    from fakes import make_workflow

    from ifl.immich import webhook_step
    from ifl.observer import WORKFLOW_NAME

    accounts = make_accounts()
    cfg = parse_config({**RAW, "webhook": {"url": "http://ifl:8080/hook", "token": "t0k"}})
    world.album("Drop", FAMILY, roles={ALICE: EDITOR, BOB: EDITOR})
    # bob has one pointing at an old URL; alice has none.
    stale = make_workflow(WORKFLOW_NAME, [webhook_step("http://old/hook", "t0k")])
    world.workflows[BOB] = [stale]

    result = await run_pass(accounts, cfg, state)

    assert result.reconciled and result.reconciled.workflows == 2
    assert ("workflow", ALICE, "create") in world.writes and (
        "workflow",
        BOB,
        "update",
    ) in world.writes
    assert stale.steps[0].config["url"] == "http://ifl:8080/hook"
    again = await run_pass(accounts, cfg, state)
    assert again.reconciled is None


def test_the_daily_report_names_members_with_trashed_reuploads(tmp_path, caplog):
    import logging
    from datetime import UTC, datetime, timedelta

    accounts = make_accounts()
    cfg = parse_config({**RAW, "warnings": {"report_at": "22:00"}})
    state = State(tmp_path / "state.json")
    at = datetime(2026, 10, 11, 22, 5, tzinfo=UTC)

    # The first report starts the clock and says nothing.
    with caplog.at_level(logging.WARNING, logger="ifl.process"):
        report_reuploads(state, cfg, accounts, now=at - timedelta(days=1))
        assert not caplog.records and state.last_report == at - timedelta(days=1)

        state.add_reuploads(ALICE, 3)
        state.add_reuploads(BOB, 1)
        # Not due yet: nothing said, the tallies kept.
        report_reuploads(state, cfg, accounts, now=at - timedelta(hours=1))
        assert not caplog.records and state.members[ALICE].reuploads == 3

        report_reuploads(state, cfg, accounts, now=at)
        assert sorted(r.getMessage() for r in caplog.records) == [
            "alice re-uploaded 3 photos the family already has in the last day; trashed",
            "bob re-uploaded 1 photo the family already has in the last day; trashed",
        ]
        assert all(r.levelno == logging.WARNING for r in caplog.records)
        assert state.members[ALICE].reuploads == 0 and state.last_report == at

        # The daemon was down for days: the window is named rather than called a day.
        caplog.clear()
        state.add_reuploads(ALICE, 2)
        report_reuploads(state, cfg, accounts, now=at + timedelta(days=3))
        assert [r.getMessage() for r in caplog.records] == [
            "alice re-uploaded 2 photos the family already has since 2026-10-11T22:05+00:00; trashed"
        ]


async def test_a_pass_that_raises_still_reports(world, state, monkeypatch, caplog):
    import logging
    from datetime import UTC, datetime, timedelta

    accounts = make_accounts()
    cfg = parse_config(RAW)
    at = datetime(2026, 10, 11, 22, 5, tzinfo=UTC)
    state.start_clock(at - timedelta(days=1))
    state.add_reuploads(ALICE, 2)  # tallied by the webhook while passes fail

    async def broken(accounts):
        raise RuntimeError("immich went away")

    monkeypatch.setattr("ifl.process.scan_albums", broken)
    with caplog.at_level(logging.WARNING, logger="ifl.process"), pytest.raises(RuntimeError):
        await run_pass(accounts, cfg, state, now=at)
    assert "alice re-uploaded 2 photos" in caplog.text and state.members[ALICE].reuploads == 0
