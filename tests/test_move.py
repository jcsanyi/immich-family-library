"""The per-asset move against the fake Immich. No network."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fakes import (
    ALICE,
    BOB,
    EDITOR,
    FAMILY,
    STRANGER,
    VIEWER,
    FakeAsset,
    make_accounts,
    patch_world,
)
from immichpy.client.generated.models.asset_visibility import AssetVisibility

from ifl.ledger import Ledger
from ifl.move import MoveBlocked, MoveError, MoveOptions, move_asset, pass_backlog
from ifl.sidecar import tags_in

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
OPTS = MoveOptions(
    dropbox_album="Drop",
    settle_seconds=60,
    moved_tag="family",
    from_prefix="from",
    sidecar_timeout=0.2,
    extraction_timeout=0.2,
)


@pytest.fixture
def world(monkeypatch):
    return patch_world(monkeypatch, "ifl.move")


@pytest.fixture
def ledger(tmp_path):
    led = Ledger(tmp_path / "ledger.sqlite")
    yield led
    led.close()


def alice_asset(world, **kw):
    return world.add_asset(
        FakeAsset(uuid4(), ALICE, checksum="sha-1", original_file_name="a.jpg", **kw)
    )


async def test_move_carries_everything_and_trashes_the_original(world, ledger):
    accounts = make_accounts()
    a = alice_asset(
        world, is_favorite=True, visibility=AssetVisibility.ARCHIVE, description="Ferry"
    )
    world.edits[a.id] = ["crop", "rotate"]
    trip = world.album("Trip", FAMILY, roles={ALICE: EDITOR, BOB: EDITOR}, assets=[a])
    world.album("Drop", FAMILY, roles={ALICE: EDITOR, BOB: EDITOR}, assets=[a])
    private = world.album("Private", ALICE, roles={BOB: VIEWER}, assets=[a])
    world.partners[FAMILY] = {ALICE, BOB}

    result = await move_asset(accounts, ledger, OPTS, a.id, accounts.members["alice"], now=NOW)

    # The copy: family-owned, same checksum, flags carried, edits and provenance set.
    copy = world.library[result.family_asset_id]
    assert copy.owner_id == FAMILY and copy.checksum == "sha-1" and copy.is_favorite
    assert copy.visibility == AssetVisibility.ARCHIVE
    assert result.uploaded and result.edits == 2
    assert world.edits[copy.id] == ["crop", "rotate"]
    assert world.metadata[copy.id]["ifl"]["origin_asset_id"] == str(a.id)
    assert world.metadata[copy.id]["ifl"]["origin_user_id"] == str(ALICE)
    # The sidecar that went up: Immich's, with only our tag.
    (up,) = world.uploads
    assert up["data"] == b"bytes-of-sha-1" and up["duration"] is None
    assert tags_in(up["sidecar"]) == ["from/alice"] and b"Ferry" in up["sidecar"]
    # Albums: family albums except the dropbox, and alice's own album with her key.
    assert result.relinked == ["Trip"] and result.relinked_member == ["Private"]
    assert copy in world.assets[trip.id] and copy in world.assets[private.id]
    assert ("add", ALICE, private.id, {copy.id}) in world.writes
    # The original: tagged, trashed, ledgered.
    assert [t.value for t in a.tags] == ["family"] and a.is_trashed and result.trashed
    row = ledger.get("sha-1")
    assert row["origin_user_id"] == str(ALICE) and row["family_asset_id"] == str(copy.id)
    # Keys: reads and the trash under alice, the upload under the family.
    assert ("download", ALICE, a.id) in world.writes and ("trash", ALICE, a.id) in world.writes
    assert ("upload", FAMILY, copy.id) in world.writes


async def test_rerun_after_upload_finishes_without_uploading_again(world, ledger):
    accounts = make_accounts()
    a = alice_asset(world)
    trip = world.album("Trip", FAMILY, roles={ALICE: EDITOR}, assets=[a])
    # A previous run got as far as the upload and died.
    copy = world.add_asset(FakeAsset(uuid4(), FAMILY, checksum="sha-1", original_file_name="a.jpg"))

    result = await move_asset(accounts, ledger, OPTS, a.id, accounts.members["alice"], now=NOW)

    assert result.family_asset_id == copy.id and not result.uploaded
    assert not world.uploads and not any(w[0] in ("download", "tag") for w in world.writes)
    assert copy in world.assets[trip.id] and a.is_trashed
    # And once more, with the original already trashed: nothing to trash, same answer.
    world.writes.clear()
    again = await move_asset(accounts, ledger, OPTS, a.id, accounts.members["alice"], now=NOW)
    assert again.family_asset_id == copy.id and not again.trashed
    assert not any(w[0] in ("trash", "upload", "tag") for w in world.writes)


@pytest.mark.parametrize(
    "setup, reason",
    [
        (lambda w, a: setattr(a, "exif_info", None), "extraction"),
        (lambda w, a: setattr(a, "updated_at", NOW - timedelta(seconds=10)), "settling"),
        (lambda w, a: setattr(a, "visibility", AssetVisibility.LOCKED), "visibility is locked"),
        (lambda w, a: setattr(a, "live_photo_video_id", uuid4()), "live photo"),
        (lambda w, a: w.shared_links.__setitem__(ALICE, {a.id}), "shared links"),
        (lambda w, a: setattr(w, "sidecar_writes", False), "sidecar not written"),
        (
            lambda w, a: w.album("Theirs", STRANGER, roles={ALICE: VIEWER}, assets=[a]),
            "owned by a non-participant",
        ),
    ],
)
async def test_blocked_conditions_leave_everything_untouched(world, ledger, setup, reason):
    accounts = make_accounts()
    a = alice_asset(world)
    world.album("Trip", FAMILY, roles={ALICE: EDITOR}, assets=[a])
    setup(world, a)

    with pytest.raises(MoveBlocked, match=reason):
        await move_asset(accounts, ledger, OPTS, a.id, accounts.members["alice"], now=NOW)

    assert not world.uploads and not a.is_trashed and ledger.get("sha-1") is None


async def test_relink_picks_the_album_owner_then_the_editor(world, ledger):
    accounts = make_accounts()
    a = alice_asset(world)
    world.album("Trip", FAMILY, roles={ALICE: EDITOR}, assets=[a])
    bobs = world.album("Bobs", BOB, roles={ALICE: EDITOR}, assets=[a])
    theirs = world.album("Theirs", STRANGER, roles={ALICE: EDITOR}, assets=[a])
    world.partners[FAMILY] = {ALICE, BOB}

    result = await move_asset(accounts, ledger, OPTS, a.id, accounts.members["alice"], now=NOW)

    copy = result.family_asset_id
    assert set(result.relinked_member) == {"Bobs", "Theirs"}
    adds = {(w[2], w[1]) for w in world.writes if w[0] == "add" and copy in w[3]}
    assert (bobs.id, BOB) in adds  # album owner first
    assert (theirs.id, ALICE) in adds  # non-participant owner: the editor's key
    assert (bobs.id, ALICE) not in adds


async def test_relink_falls_back_to_the_editor_when_the_owner_cannot(world, ledger):
    accounts = make_accounts()
    a = alice_asset(world)
    world.album("Trip", FAMILY, roles={ALICE: EDITOR}, assets=[a])
    bobs = world.album("Bobs", BOB, roles={ALICE: EDITOR}, assets=[a])
    world.partners[FAMILY] = {ALICE}  # bob isn't partnered with the family yet

    result = await move_asset(accounts, ledger, OPTS, a.id, accounts.members["alice"], now=NOW)

    copy = result.family_asset_id
    assert world.library[copy] in world.assets[bobs.id]
    adds = [w[1] for w in world.writes if w[0] == "add" and w[2] == bobs.id and copy in w[3]]
    assert adds == [BOB, ALICE]  # bob refused, alice succeeded


async def test_relink_failure_keeps_the_original(world, ledger):
    accounts = make_accounts()
    a = alice_asset(world)
    world.album("Trip", FAMILY, roles={ALICE: EDITOR}, assets=[a])
    world.album("Bobs", BOB, roles={ALICE: VIEWER}, assets=[a])  # alice can't add, bob unpartnered
    world.partners[FAMILY] = set()

    with pytest.raises(MoveError, match="could not re-link the copy into 'Bobs'"):
        await move_asset(accounts, ledger, OPTS, a.id, accounts.members["alice"], now=NOW)

    assert not a.is_trashed and ledger.get("sha-1") is None
    assert len(world.uploads) == 1  # the copy exists; a rerun after fixing the share finishes
    world.partners[FAMILY] = {ALICE, BOB}
    result = await move_asset(accounts, ledger, OPTS, a.id, accounts.members["alice"], now=NOW)
    assert not result.uploaded and a.is_trashed and result.relinked_member == ["Bobs"]


async def test_dying_at_the_edits_step_keeps_the_original_and_resumes(world, ledger, monkeypatch):
    accounts = make_accounts()
    a = alice_asset(world)
    world.edits[a.id] = ["crop"]
    world.album("Trip", FAMILY, roles={ALICE: EDITOR}, assets=[a])
    real_upload = world.upload_asset

    async def upload_without_dimensions(client, **kw):
        new_id, created = await real_upload(client, **kw)
        world.library[new_id].width = world.library[new_id].height = None
        return new_id, created

    monkeypatch.setattr("ifl.move.upload_asset", upload_without_dimensions)
    with pytest.raises(MoveError, match="no dimensions"):
        await move_asset(accounts, ledger, OPTS, a.id, accounts.members["alice"], now=NOW)

    (up,) = world.uploads
    assert not a.is_trashed and ledger.get("sha-1") is None and up["id"] not in world.edits
    # Extraction catches up; the rerun finishes without another upload.
    world.library[up["id"]].width, world.library[up["id"]].height = 640, 480
    result = await move_asset(accounts, ledger, OPTS, a.id, accounts.members["alice"], now=NOW)
    assert not result.uploaded and world.edits[up["id"]] == ["crop"] and a.is_trashed


async def test_wrong_owner_or_trashed_original_is_an_error(world, ledger):
    accounts = make_accounts()
    a = alice_asset(world)
    world.album("Trip", FAMILY, roles={ALICE: EDITOR, BOB: EDITOR}, assets=[a])
    with pytest.raises(MoveError, match="not owned by bob"):
        await move_asset(accounts, ledger, OPTS, a.id, accounts.members["bob"], now=NOW)
    a.is_trashed = True
    with pytest.raises(MoveError, match="in the trash"):
        await move_asset(accounts, ledger, OPTS, a.id, accounts.members["alice"], now=NOW)


async def test_pass_backlog_reports_only_the_watched_queues(world):
    accounts = make_accounts()
    assert await pass_backlog(accounts) is None  # no admin key configured
    from fakes import FakeClient

    from ifl.accounts import Account

    accounts.admin = Account(
        name="admin", email="root@x", user_id=uuid4(), client=FakeClient(uuid4())
    )
    world.queues = {"sidecar": 0, "metadataExtraction": 7, "faceDetection": 900}
    assert await pass_backlog(accounts) == {"metadataExtraction": 7}
    world.queues = {"faceDetection": 900}
    assert await pass_backlog(accounts) == {}


async def test_edits_wait_for_the_copy_to_have_dimensions(world, ledger, monkeypatch):
    accounts = make_accounts()
    a = alice_asset(world)
    world.edits[a.id] = ["crop"]
    world.album("Trip", FAMILY, roles={ALICE: EDITOR}, assets=[a])
    # The copy comes back from upload with no dimensions yet, then gets them.
    real_upload = world.upload_asset

    async def slow_upload(client, **kw):
        new_id, created = await real_upload(client, **kw)
        world.library[new_id].width = world.library[new_id].height = None
        return new_id, created

    polls = {"n": 0}
    real_get = world.get_asset

    async def get_asset(client, asset_id):
        a = await real_get(client, asset_id)
        if a.owner_id == FAMILY and a.width is None:
            polls["n"] += 1
            if polls["n"] >= 2:
                a.width, a.height = 640, 480
        return a

    monkeypatch.setattr("ifl.move.upload_asset", slow_upload)
    monkeypatch.setattr("ifl.move.get_asset", get_asset)
    result = await move_asset(accounts, ledger, OPTS, a.id, accounts.members["alice"], now=NOW)
    assert world.edits[result.family_asset_id] == ["crop"] and polls["n"] >= 2


async def test_move_all_honours_the_limit_and_continues_past_blocks_and_failures(world, ledger):
    from ifl.move import move_assets
    from ifl.observer import MoveAsset

    accounts = make_accounts()
    alice = accounts.members["alice"]
    trip = world.album("Trip", FAMILY, roles={ALICE: EDITOR})
    assets = []
    for i in range(5):
        a = world.add_asset(
            FakeAsset(uuid4(), ALICE, checksum=f"sha-{i}", original_file_name=f"{i}.jpg")
        )
        world.assets[trip.id].append(a)
        assets.append(a)
    assets[1].exif_info = None  # blocked
    # 2.jpg fails: it's in bob's album, alice is only a viewer, and bob isn't partnered yet.
    world.album("Bobs", BOB, roles={ALICE: VIEWER}, assets=[assets[2]])
    world.partners[FAMILY] = {ALICE}
    world.shared_links[ALICE] = {assets[3].id}  # blocked, via the once-per-owner lookup
    moves = [MoveAsset(a.id, a.checksum, alice, trip, a.original_file_name) for a in assets]

    result = await move_assets(accounts, ledger, OPTS, moves, limit=4)

    assert [r.file_name for r in result.done] == ["0.jpg"]
    assert [b[0] for b in result.blocked] == ["1.jpg", "3.jpg"]
    assert [f[0] for f in result.failed] == ["2.jpg"]
    assert result.remaining == 1
    assert assets[0].is_trashed and not any(a.is_trashed for a in assets[1:])
    assert str(result) == "moved 1, blocked 2, failed 1, remaining 1"

    world.partners[FAMILY] = {ALICE, BOB}  # the share arrives; the rerun finishes 2.jpg
    again = await move_assets(accounts, ledger, OPTS, moves[2:3] + moves[4:], limit=0)
    assert [r.file_name for r in again.done] == ["2.jpg", "4.jpg"] and again.remaining == 0
