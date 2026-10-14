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
    TEMP_DIR,
    VIEWER,
    FakeAsset,
    make_accounts,
    make_tag,
    patch_world,
)
from immichpy.client.generated.models.asset_visibility import AssetVisibility
from immichpy.client.generated.models.bulk_id_error_reason import BulkIdErrorReason
from immichpy.client.generated.models.bulk_id_response_dto import BulkIdResponseDto

from ifl.config import ConfigError
from ifl.move import (
    MoveBlocked,
    MoveError,
    MoveOptions,
    move_asset,
    pass_backlog,
    prepare_temp_dir,
)
from ifl.sidecar import tags_in

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
OPTS = MoveOptions(
    dropbox_album="Drop",
    settle_seconds=60,
    moved_tag="family",
    from_prefix="from",
    temp_dir=TEMP_DIR,
    sidecar_timeout=0.2,
    extraction_timeout=0.2,
)


@pytest.fixture
def world(monkeypatch):
    return patch_world(monkeypatch, "ifl.move")


def alice_asset(world, **kw):
    return world.add_asset(
        FakeAsset(uuid4(), ALICE, checksum="sha-1", original_file_name="a.jpg", **kw)
    )


async def test_move_carries_everything_and_trashes_the_original(world):
    accounts = make_accounts()
    a = alice_asset(
        world, is_favorite=True, visibility=AssetVisibility.ARCHIVE, description="Ferry"
    )
    world.edits[a.id] = ["crop", "rotate"]
    trip = world.album("Trip", FAMILY, roles={ALICE: EDITOR, BOB: EDITOR}, assets=[a])
    world.album("Drop", FAMILY, roles={ALICE: EDITOR, BOB: EDITOR}, assets=[a])
    private = world.album("Private", ALICE, roles={BOB: VIEWER}, assets=[a])
    world.partners[FAMILY] = {ALICE, BOB}

    result = await move_asset(accounts, OPTS, a.id, accounts.members["alice"], now=NOW)

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
    assert not list(TEMP_DIR.iterdir())  # the downloaded original is gone again
    assert tags_in(up["sidecar"]) == ["from/alice"] and b"Ferry" in up["sidecar"]
    # Albums: family albums except the dropbox, and alice's own album with her key.
    assert result.relinked == ["Trip"] and result.relinked_member == ["Private"]
    assert copy in world.assets[trip.id] and copy in world.assets[private.id]
    assert ("add", ALICE, private.id, {copy.id}) in world.writes
    # The original: tagged, trashed.
    assert [t.value for t in a.tags] == ["family"] and a.is_trashed and result.trashed
    assert result.owner_id == ALICE and not result.reupload
    # Keys: reads and the trash under alice, the upload under the family.
    assert ("download", ALICE, a.id) in world.writes and ("trash", ALICE, a.id) in world.writes
    assert ("upload", FAMILY, copy.id) in world.writes


async def test_rerun_after_upload_finishes_without_uploading_again(world):
    accounts = make_accounts()
    # A previous run tagged the original, uploaded the copy, and died before it
    # wrote the provenance. The moved tag is what says this is ours to finish.
    a = alice_asset(world, tags=[make_tag("family")])
    world.edits[a.id] = ["crop"]
    trip = world.album("Trip", FAMILY, roles={ALICE: EDITOR}, assets=[a])
    copy = world.add_asset(FakeAsset(uuid4(), FAMILY, checksum="sha-1", original_file_name="a.jpg"))

    result = await move_asset(accounts, OPTS, a.id, accounts.members["alice"], now=NOW)

    assert result.family_asset_id == copy.id and not result.uploaded and not result.duplicate
    assert not world.uploads and not any(w[0] in ("download", "tag") for w in world.writes)
    assert world.edits[copy.id] == ["crop"]
    assert world.metadata[copy.id]["ifl"]["origin_asset_id"] == str(a.id)
    assert copy in world.assets[trip.id] and a.is_trashed
    # And once more, with the original already trashed: nothing to trash, same answer.
    world.writes.clear()
    again = await move_asset(accounts, OPTS, a.id, accounts.members["alice"], now=NOW)
    assert again.family_asset_id == copy.id and not again.trashed
    assert not any(w[0] in ("trash", "upload", "tag") for w in world.writes)


@pytest.mark.parametrize(
    "setup, reason",
    [
        (lambda w, a: setattr(a, "exif_info", None), "extraction"),
        (lambda w, a: setattr(a, "updated_at", NOW - timedelta(seconds=10)), "settling"),
        (lambda w, a: setattr(a, "visibility", AssetVisibility.LOCKED), "visibility is locked"),
        (lambda w, a: setattr(a, "live_photo_video_id", str(uuid4())), "live photo"),
        (lambda w, a: w.shared_links.__setitem__(ALICE, {a.id}), "shared links"),
        (
            lambda w, a: w.album("Theirs", STRANGER, roles={ALICE: VIEWER}, assets=[a]),
            "in 'Theirs', owned by non-participant stranger@x, which alice can see",
        ),
    ],
)
async def test_blocked_conditions_leave_everything_untouched(world, setup, reason):
    accounts = make_accounts()
    a = alice_asset(world)
    world.album("Trip", FAMILY, roles={ALICE: EDITOR}, assets=[a])
    setup(world, a)

    with pytest.raises(MoveBlocked, match=reason):
        await move_asset(accounts, OPTS, a.id, accounts.members["alice"], now=NOW)

    assert world.writes == [] and not world.uploads
    assert not a.is_trashed


async def test_a_sidecar_that_never_arrives_blocks_with_only_the_tag_written(world):
    accounts = make_accounts()
    a = alice_asset(world)
    world.album("Trip", FAMILY, roles={ALICE: EDITOR}, assets=[a])
    world.sidecar_writes = False

    with pytest.raises(MoveBlocked, match="sidecar not written"):
        await move_asset(accounts, OPTS, a.id, accounts.members["alice"], now=NOW)

    # The moved tag is the one write before this block: it is what asks Immich
    # for a fresh sidecar. Nothing was downloaded, uploaded, trashed or recorded.
    assert [w[0] for w in world.writes] == ["tag"] and [t.value for t in a.tags] == ["family"]
    assert not world.uploads and not a.is_trashed

    # The next pass sees the tag already there and writes nothing at all.
    world.writes.clear()
    with pytest.raises(MoveBlocked, match="sidecar not written"):
        await move_asset(accounts, OPTS, a.id, accounts.members["alice"], now=NOW)
    assert world.writes == []


async def test_a_failed_upload_leaves_no_file_behind(world, monkeypatch):
    accounts = make_accounts()
    a = alice_asset(world)
    world.album("Trip", FAMILY, roles={ALICE: EDITOR}, assets=[a])

    async def refuse(client, *, path, **kw):
        assert path.read_bytes() == b"bytes-of-sha-1"
        raise RuntimeError("413 too large")

    monkeypatch.setattr("ifl.move.upload_asset", refuse)

    with pytest.raises(RuntimeError, match="too large"):
        await move_asset(accounts, OPTS, a.id, accounts.members["alice"], now=NOW)

    assert not list(TEMP_DIR.iterdir()) and not a.is_trashed


def test_prepare_temp_dir_makes_it_and_clears_only_our_leftovers(tmp_path):
    temp = tmp_path / "a" / "b"
    prepare_temp_dir(temp)
    assert temp.is_dir()

    (temp / "ifl-0123").write_bytes(b"half a video")
    (temp / "notes.txt").write_text("someone else's")
    prepare_temp_dir(temp)
    assert [p.name for p in temp.iterdir()] == ["notes.txt"]

    blocker = tmp_path / "file"
    blocker.write_text("")
    with pytest.raises(ConfigError, match="temp_dir .* is not usable"):
        prepare_temp_dir(blocker / "under-a-file")


async def test_relink_picks_the_album_owner_then_the_editor(world):
    accounts = make_accounts()
    a = alice_asset(world)
    world.album("Trip", FAMILY, roles={ALICE: EDITOR}, assets=[a])
    bobs = world.album("Bobs", BOB, roles={ALICE: EDITOR}, assets=[a])
    theirs = world.album("Theirs", STRANGER, roles={ALICE: EDITOR}, assets=[a])
    world.partners[FAMILY] = {ALICE, BOB}

    result = await move_asset(accounts, OPTS, a.id, accounts.members["alice"], now=NOW)

    copy = result.family_asset_id
    assert set(result.relinked_member) == {"Bobs", "Theirs"}
    adds = {(w[2], w[1]) for w in world.writes if w[0] == "add" and copy in w[3]}
    assert (bobs.id, BOB) in adds  # album owner first
    assert (theirs.id, ALICE) in adds  # non-participant owner: the editor's key
    assert (bobs.id, ALICE) not in adds


async def test_relink_reaches_albums_the_asset_owner_is_not_on(world):
    accounts = make_accounts()
    a = alice_asset(world)
    world.album("Trip", FAMILY, roles={ALICE: EDITOR}, assets=[a])
    # None of these has alice on it, so her key lists none of them.
    handmade = world.album("Handmade", FAMILY, assets=[a])
    private = world.album("Bobs Private", BOB, assets=[a])
    theirs = world.album("Theirs", STRANGER, roles={BOB: EDITOR}, assets=[a])
    world.partners[FAMILY] = {ALICE, BOB}

    result = await move_asset(accounts, OPTS, a.id, accounts.members["alice"], now=NOW)

    copy = world.library[result.family_asset_id]
    assert set(result.relinked) == {"Trip", "Handmade"}
    assert set(result.relinked_member) == {"Bobs Private", "Theirs"}
    assert all(copy in world.assets[al.id] for al in (handmade, private, theirs))
    adds = {(w[2], w[1]) for w in world.writes if w[0] == "add" and copy.id in w[3]}
    assert {(handmade.id, FAMILY), (private.id, BOB), (theirs.id, BOB)} <= adds
    assert a.is_trashed


async def test_an_outsiders_album_that_only_a_viewer_sees_blocks_and_says_who_sees_it(world):
    accounts = make_accounts()
    a = alice_asset(world)
    world.album("Trip", FAMILY, roles={ALICE: EDITOR}, assets=[a])
    # Alice isn't on this album. Bob is, as a viewer, so his key lists it.
    world.album("Theirs", STRANGER, roles={BOB: VIEWER}, assets=[a])

    with pytest.raises(MoveBlocked) as blocked:
        await move_asset(accounts, OPTS, a.id, accounts.members["alice"], now=NOW)

    assert str(blocked.value) == (
        "in 'Theirs', owned by non-participant stranger@x, which bob can see but nobody "
        "with a key can add to; ask its owner to make a family member an editor there, "
        "or to take the photo out"
    )
    assert world.writes == [] and not a.is_trashed


async def test_relink_tries_a_key_once_when_the_asset_owner_owns_the_album(world):
    accounts = make_accounts()
    a = alice_asset(world)
    world.album("Trip", FAMILY, roles={ALICE: EDITOR}, assets=[a])
    private = world.album("Private", ALICE, assets=[a])
    world.partners[FAMILY] = set()  # so alice's add is refused

    with pytest.raises(MoveError, match=r"into 'Private' \(alice: [^;]*\); original left"):
        await move_asset(accounts, OPTS, a.id, accounts.members["alice"], now=NOW)

    assert [w[1] for w in world.writes if w[0] == "add" and w[2] == private.id] == [ALICE]


async def test_relink_falls_back_to_the_editor_when_the_owner_cannot(world):
    accounts = make_accounts()
    a = alice_asset(world)
    world.album("Trip", FAMILY, roles={ALICE: EDITOR}, assets=[a])
    bobs = world.album("Bobs", BOB, roles={ALICE: EDITOR}, assets=[a])
    world.partners[FAMILY] = {ALICE}  # bob isn't partnered with the family yet

    result = await move_asset(accounts, OPTS, a.id, accounts.members["alice"], now=NOW)

    copy = result.family_asset_id
    assert world.library[copy] in world.assets[bobs.id]
    adds = [w[1] for w in world.writes if w[0] == "add" and w[2] == bobs.id and copy in w[3]]
    assert adds == [BOB, ALICE]  # bob refused, alice succeeded


async def test_relink_failure_keeps_the_original(world):
    accounts = make_accounts()
    a = alice_asset(world)
    world.album("Trip", FAMILY, roles={ALICE: EDITOR}, assets=[a])
    world.album("Bobs", BOB, roles={ALICE: VIEWER}, assets=[a])  # alice can't add, bob unpartnered
    world.partners[FAMILY] = set()

    with pytest.raises(MoveError, match="could not re-link the copy into 'Bobs'") as failed:
        await move_asset(accounts, OPTS, a.id, accounts.members["alice"], now=NOW)

    assert str(failed.value).count("'Bobs'") == 1  # each key's refusal is the bare reason
    assert "move.py" not in str(failed.value)
    assert not a.is_trashed
    assert len(world.uploads) == 1  # the copy exists; a rerun after fixing the share finishes
    # A refused add that Immich gives no reason for still reads as a refusal.
    world.fail_adds[world.uploads[0]["id"]] = None
    with pytest.raises(MoveError, match="could not add the copy to 'Trip': unknown: nope"):
        await move_asset(accounts, OPTS, a.id, accounts.members["alice"], now=NOW)
    assert not a.is_trashed
    world.fail_adds.clear()
    world.partners[FAMILY] = {ALICE, BOB}
    result = await move_asset(accounts, OPTS, a.id, accounts.members["alice"], now=NOW)
    assert not result.uploaded and a.is_trashed and result.relinked_member == ["Bobs"]


async def test_a_restored_original_is_trashed_again_without_redoing_the_fixups(world):
    accounts = make_accounts()
    a = alice_asset(world)
    world.edits[a.id] = ["alice-crop"]
    trip = world.album("Trip", FAMILY, roles={ALICE: EDITOR}, assets=[a])
    first = await move_asset(accounts, OPTS, a.id, accounts.members["alice"], now=NOW)
    copy = first.family_asset_id
    assert first.edits == 1 and world.edits[copy] == ["alice-crop"] and a.is_trashed
    entry = dict(world.metadata[copy]["ifl"])

    # The family re-crops its copy. Then alice takes her original back out of
    # the trash, where it is still a member of the family album.
    world.edits[copy] = ["family-crop"]
    a.is_trashed = False
    later = NOW + timedelta(days=3)
    again = await move_asset(accounts, OPTS, a.id, accounts.members["alice"], now=later)

    # The entry names this original, so the fix-ups are known to be done.
    assert world.edits[copy] == ["family-crop"] and world.metadata[copy]["ifl"] == entry
    assert again.edits == 0 and not again.uploaded and not again.duplicate
    assert a.is_trashed and again.trashed and world.library[copy] in world.assets[trip.id]


async def test_dying_at_the_edits_step_keeps_the_original_and_resumes(world, monkeypatch):
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
        await move_asset(accounts, OPTS, a.id, accounts.members["alice"], now=NOW)

    (up,) = world.uploads
    assert not a.is_trashed and up["id"] not in world.edits
    # Extraction catches up; the rerun finishes without another upload.
    world.library[up["id"]].width, world.library[up["id"]].height = 640, 480
    result = await move_asset(accounts, OPTS, a.id, accounts.members["alice"], now=NOW)
    assert not result.uploaded and world.edits[up["id"]] == ["crop"] and a.is_trashed


async def test_wrong_owner_or_trashed_original_is_an_error(world):
    accounts = make_accounts()
    a = alice_asset(world)
    world.album("Trip", FAMILY, roles={ALICE: EDITOR, BOB: EDITOR}, assets=[a])
    with pytest.raises(MoveError, match="not owned by bob"):
        await move_asset(accounts, OPTS, a.id, accounts.members["bob"], now=NOW)
    a.is_trashed = True
    with pytest.raises(MoveError, match="in the trash"):
        await move_asset(accounts, OPTS, a.id, accounts.members["alice"], now=NOW)


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


async def test_edits_wait_for_the_copy_to_have_dimensions(world, monkeypatch):
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
    result = await move_asset(accounts, OPTS, a.id, accounts.members["alice"], now=NOW)
    assert world.edits[result.family_asset_id] == ["crop"] and polls["n"] >= 2


async def test_move_all_spends_the_limit_on_moves_and_failures_but_not_on_blocks(world):
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
    assets[0].exif_info = None  # blocked
    world.shared_links[ALICE] = {assets[1].id}  # blocked, via the once-per-owner lookup
    # 2.jpg fails: it's in bob's album, alice is only a viewer, and bob isn't partnered yet.
    world.album("Bobs", BOB, roles={ALICE: VIEWER}, assets=[assets[2]])
    world.partners[FAMILY] = {ALICE}
    moves = [MoveAsset(a.id, alice, trip, a.original_file_name) for a in assets]

    result = await move_assets(accounts, OPTS, moves, limit=2)

    # The two blocks at the head are free; the failure and the move fill the limit.
    assert [b[0] for b in result.blocked] == ["0.jpg", "1.jpg"]
    assert [f[0] for f in result.failed] == ["2.jpg"]
    assert [r.file_name for r in result.done] == ["3.jpg"]
    assert result.remaining == 1
    assert result.unfinished == {a.id for a in assets} - {assets[3].id}
    assert assets[3].is_trashed and not any(a.is_trashed for a in assets if a is not assets[3])
    assert str(result) == "moved 1, blocked 2, failed 1, remaining 1"

    world.partners[FAMILY] = {ALICE, BOB}  # the share arrives; the rerun finishes 2.jpg
    again = await move_assets(accounts, OPTS, moves[2:3] + moves[4:], limit=0)
    assert [r.file_name for r in again.done] == ["2.jpg", "4.jpg"] and again.remaining == 0


async def test_move_all_spends_the_limit_on_sidecar_timeouts(world):
    from ifl.move import move_assets
    from ifl.observer import MoveAsset

    accounts = make_accounts()
    alice = accounts.members["alice"]
    trip = world.album("Trip", FAMILY, roles={ALICE: EDITOR})
    assets = []
    for i in range(3):
        a = world.add_asset(
            FakeAsset(uuid4(), ALICE, checksum=f"sha-{i}", original_file_name=f"{i}.jpg")
        )
        world.assets[trip.id].append(a)
        assets.append(a)
    world.sidecar_writes = False
    moves = [MoveAsset(a.id, alice, trip, a.original_file_name) for a in assets]

    result = await move_assets(accounts, OPTS, moves, limit=2)

    # Each timeout kept the pass waiting, so the third photo is not tried.
    assert [b[0] for b in result.blocked] == ["0.jpg", "1.jpg"]
    assert result.remaining == 1 and assets[2].tags == []


async def test_a_reupload_is_trashed_and_counted_without_touching_the_copy(world):
    accounts = make_accounts()
    # The family copy from an earlier move, with its edits and provenance.
    moved_orig = uuid4()
    copy = world.add_asset(FakeAsset(uuid4(), FAMILY, checksum="sha-1", original_file_name="a.jpg"))
    world.edits[copy.id] = ["crop"]
    world.metadata[copy.id] = {
        "ifl": {"origin_user_id": str(ALICE), "origin_asset_id": str(moved_orig)}
    }
    # Alice's phone re-uploads it, into her private album this time.
    re = alice_asset(world)
    private = world.album("Private", ALICE, roles={BOB: VIEWER}, assets=[re])
    world.partners[FAMILY] = {ALICE, BOB}

    result = await move_asset(
        accounts, OPTS, re.id, accounts.members["alice"], now=NOW, reupload=True
    )

    assert result.duplicate and not result.uploaded and result.reupload and result.trashed
    assert result.family_asset_id == copy.id
    assert world.edits[copy.id] == ["crop"]  # untouched
    assert world.metadata[copy.id]["ifl"]["origin_asset_id"] == str(moved_orig)  # untouched
    assert copy in world.assets[private.id] and re.is_trashed
    assert not world.uploads and not any(w[0] == "tag" for w in world.writes)
    assert "re-upload, family already has it" in str(result)


async def test_a_reupload_the_webhook_already_trashed_is_not_trashed_or_counted_again(world):
    from ifl.move import move_assets
    from ifl.observer import MoveAsset

    accounts = make_accounts()
    copy = world.add_asset(FakeAsset(uuid4(), FAMILY, checksum="sha-1", original_file_name="a.jpg"))
    re = alice_asset(world)
    # The pass scanned this item while the original was live. The webhook
    # finished with it before the pass reached it.
    item = MoveAsset(re.id, accounts.members["alice"], None, "a.jpg", reupload=True)

    by_webhook = await move_assets(accounts, OPTS, [item], limit=0, in_flight=set())
    by_pass = await move_assets(accounts, OPTS, [item], limit=0, in_flight=set())

    assert by_webhook.done[0].trashed and not by_pass.done[0].trashed
    assert [w for w in world.writes if w[0] == "trash"] == [("trash", ALICE, re.id)]
    assert by_webhook.reuploads_trashed() == {ALICE: 1} and by_pass.reuploads_trashed() == {}
    assert by_pass.done[0].family_asset_id == copy.id and not by_pass.unfinished


async def test_a_copy_the_family_got_some_other_way_is_authoritative(world):
    accounts = make_accounts()
    # No ifl entry: the family uploaded this itself, with its own crop. Alice's
    # copy of the same bytes carries no moved tag, so it is the duplicate.
    copy = world.add_asset(FakeAsset(uuid4(), FAMILY, checksum="sha-1", original_file_name="a.jpg"))
    world.edits[copy.id] = ["family-crop"]
    re = alice_asset(world)
    world.edits[re.id] = ["alice-crop"]
    trip = world.album("Trip", FAMILY, roles={ALICE: EDITOR}, assets=[re])

    result = await move_asset(accounts, OPTS, re.id, accounts.members["alice"], now=NOW)

    assert result.duplicate and result.family_asset_id == copy.id and not result.uploaded
    assert world.edits[copy.id] == ["family-crop"] and copy.id not in world.metadata
    assert copy in world.assets[trip.id] and re.is_trashed
    assert not any(w[0] in ("tag", "download", "upload") for w in world.writes)
    # From the re-upload scan, the same situation is counted against the member.
    re2 = world.add_asset(FakeAsset(uuid4(), ALICE, checksum="sha-1", original_file_name="a.jpg"))
    again = await move_asset(
        accounts, OPTS, re2.id, accounts.members["alice"], now=NOW, reupload=True
    )
    assert again.duplicate and again.reupload and re2.is_trashed
    assert copy.id not in world.metadata


async def test_two_members_with_the_same_bytes_keep_the_first_movers_provenance(world):
    accounts = make_accounts()
    # bob's copy moved fully. alice's identical photo was tagged by an earlier
    # attempt that blocked at the sidecar, so it wears the moved tag too. The
    # copy's entry names bob's asset: alice's is a duplicate whatever it wears.
    bobs_orig = uuid4()
    copy = world.add_asset(FakeAsset(uuid4(), FAMILY, checksum="sha-1", original_file_name="a.jpg"))
    world.edits[copy.id] = ["bob-crop"]
    world.metadata[copy.id] = {
        "ifl": {"origin_user_id": str(BOB), "origin_asset_id": str(bobs_orig)}
    }
    a = alice_asset(world, tags=[make_tag("family")])
    world.edits[a.id] = ["alice-crop"]
    world.album("Trip", FAMILY, roles={ALICE: EDITOR, BOB: EDITOR}, assets=[a])

    result = await move_asset(accounts, OPTS, a.id, accounts.members["alice"], now=NOW)

    assert result.duplicate and a.is_trashed and not result.uploaded
    assert world.edits[copy.id] == ["bob-crop"]
    assert world.metadata[copy.id]["ifl"]["origin_asset_id"] == str(bobs_orig)


async def test_a_reshare_of_a_trashed_copy_does_not_wait_or_read_edits(world, monkeypatch):
    accounts = make_accounts()
    copy = world.add_asset(FakeAsset(uuid4(), FAMILY, checksum="sha-1", is_trashed=True))
    world.metadata[copy.id] = {"ifl": {"origin_asset_id": str(uuid4())}}
    # Seconds after the phone uploaded it again: not extracted, not settled.
    re = alice_asset(world, exif_info=None, updated_at=NOW - timedelta(seconds=5))
    world.album("Trip", FAMILY, roles={ALICE: EDITOR}, assets=[re])

    async def no_edits(client, asset_id):
        raise AssertionError("a duplicate's edits are never used")

    monkeypatch.setattr("ifl.move.get_asset_edits", no_edits)

    result = await move_asset(accounts, OPTS, re.id, accounts.members["alice"], now=NOW)

    assert result.restored and result.duplicate and not result.uploaded
    assert not copy.is_trashed and re.is_trashed


async def test_an_interrupted_move_with_a_trashed_copy_still_waits_and_carries_edits(world):
    accounts = make_accounts()
    # The move died after the upload, before the fix-ups, and the family then
    # trashed the copy. The original wears the moved tag, so it is no duplicate.
    a = alice_asset(world, updated_at=NOW - timedelta(seconds=5))
    a.tags = [make_tag("family")]
    world.edits[a.id] = ["crop"]
    copy = world.add_asset(FakeAsset(uuid4(), FAMILY, checksum="sha-1", is_trashed=True))
    world.album("Trip", FAMILY, roles={ALICE: EDITOR}, assets=[a])

    with pytest.raises(MoveBlocked, match="settling"):
        await move_asset(accounts, OPTS, a.id, accounts.members["alice"], now=NOW)
    assert copy.is_trashed and world.writes == []

    a.updated_at = NOW - timedelta(minutes=5)
    result = await move_asset(accounts, OPTS, a.id, accounts.members["alice"], now=NOW)

    assert result.restored and not result.duplicate and result.edits == 1
    assert world.edits[copy.id] == ["crop"] and not copy.is_trashed and a.is_trashed
    assert world.metadata[copy.id]["ifl"]["origin_asset_id"] == str(a.id)


async def test_a_trashed_family_copy_is_restored_when_shared_again(world):
    accounts = make_accounts()
    copy = world.add_asset(
        FakeAsset(uuid4(), FAMILY, checksum="sha-1", original_file_name="a.jpg", is_trashed=True)
    )
    world.metadata[copy.id] = {
        "ifl": {"origin_user_id": str(ALICE), "origin_asset_id": str(uuid4())}
    }
    re = alice_asset(world)
    trip = world.album("Trip", FAMILY, roles={ALICE: EDITOR}, assets=[re])

    result = await move_asset(accounts, OPTS, re.id, accounts.members["alice"], now=NOW)

    assert result.restored and result.duplicate and not result.uploaded
    assert not copy.is_trashed and ("restore", FAMILY, copy.id) in world.writes
    assert copy in world.assets[trip.id] and re.is_trashed
    assert not result.reupload  # a re-share isn't counted
    assert "restored from the trash" in str(result)
    # The same situation reached from the re-upload scan is blocked instead.
    copy.is_trashed = True
    re2 = world.add_asset(FakeAsset(uuid4(), ALICE, checksum="sha-1", original_file_name="a.jpg"))
    with pytest.raises(MoveBlocked, match="in the trash"):
        await move_asset(accounts, OPTS, re2.id, accounts.members["alice"], now=NOW, reupload=True)
    assert copy.is_trashed and not re2.is_trashed


async def test_a_reupload_whose_family_copy_is_gone_is_left_alone(world):
    accounts = make_accounts()
    # The scan saw a family copy; it was deleted for good before the move ran.
    re = alice_asset(world)

    with pytest.raises(MoveBlocked, match="no longer has a copy"):
        await move_asset(accounts, OPTS, re.id, accounts.members["alice"], now=NOW, reupload=True)

    assert not re.is_trashed and not world.uploads and not world.writes


async def test_a_duplicate_skips_the_extraction_and_settle_checks(world):
    accounts = make_accounts()
    copy = world.add_asset(FakeAsset(uuid4(), FAMILY, checksum="sha-1", original_file_name="a.jpg"))
    world.metadata[copy.id] = {"ifl": {"origin_asset_id": str(uuid4())}}
    re = alice_asset(world, exif_info=None, updated_at=NOW)  # just landed, nothing extracted
    world.album("Trip", FAMILY, roles={ALICE: EDITOR}, assets=[re])

    result = await move_asset(
        accounts, OPTS, re.id, accounts.members["alice"], now=NOW, reupload=True
    )

    assert result.duplicate and re.is_trashed and result.reupload
    assert not any(w[0] in ("tag", "download", "upload") for w in world.writes)
    # Not a duplicate: the same fresh asset waits.
    fresh = world.add_asset(
        FakeAsset(
            uuid4(),
            ALICE,
            checksum="new",
            original_file_name="n.jpg",
            exif_info=None,
            updated_at=NOW,
        )
    )
    world.album("Trip2", FAMILY, roles={ALICE: EDITOR}, assets=[fresh])
    with pytest.raises(MoveBlocked, match="extraction"):
        await move_asset(accounts, OPTS, fresh.id, accounts.members["alice"], now=NOW)


async def test_an_unexpected_error_on_one_asset_does_not_stop_the_others(world, monkeypatch):
    from ifl.move import move_assets
    from ifl.observer import MoveAsset

    accounts = make_accounts()
    alice = accounts.members["alice"]
    trip = world.album("Trip", FAMILY, roles={ALICE: EDITOR})
    assets = []
    for i in range(2):
        a = world.add_asset(
            FakeAsset(uuid4(), ALICE, checksum=f"sha-{i}", original_file_name=f"{i}.jpg")
        )
        world.assets[trip.id].append(a)
        assets.append(a)
    # The family copy of 0.jpg can't go into Trip, for a reason that isn't "duplicate".
    # That path raises RuntimeError, not MoveError.
    real_add = world.add_album_assets

    async def add(client, album_id, ids):
        ids = list(ids)
        if client.user_id == FAMILY and any(world.library[i].checksum == "sha-0" for i in ids):
            return [
                BulkIdResponseDto(
                    id=i, success=False, error=BulkIdErrorReason.UNKNOWN, error_message="boom"
                )
                for i in ids
            ]
        return await real_add(client, album_id, ids)

    monkeypatch.setattr("ifl.move.add_album_assets", add)
    moves = [MoveAsset(a.id, alice, trip, a.original_file_name) for a in assets]
    in_flight: set = set()

    result = await move_assets(accounts, OPTS, moves, limit=0, in_flight=in_flight)

    assert [f[0] for f in result.failed] == ["0.jpg"] and "boom" in result.failed[0][1]
    assert [r.file_name for r in result.done] == ["1.jpg"]
    assert not assets[0].is_trashed and assets[1].is_trashed
    assert result.unfinished == {assets[0].id} and not in_flight


async def test_a_failed_per_owner_lookup_does_not_leave_the_asset_in_flight(world, monkeypatch):
    from ifl.move import move_assets
    from ifl.observer import MoveAsset

    accounts = make_accounts()
    alice = accounts.members["alice"]
    trip = world.album("Trip", FAMILY, roles={ALICE: EDITOR})
    a = alice_asset(world)
    world.assets[trip.id].append(a)

    async def down(client):
        raise ConnectionError("immich went away")

    monkeypatch.setattr("ifl.move.shared_link_asset_ids", down)
    moves = [MoveAsset(a.id, alice, trip, a.original_file_name)]
    in_flight: set = set()

    result = await move_assets(accounts, OPTS, moves, limit=0, in_flight=in_flight)

    assert [f[0] for f in result.failed] == ["a.jpg"] and result.unfinished == {a.id}
    assert not in_flight  # otherwise both paths skip this asset until a restart
    assert not a.is_trashed


async def test_a_video_carries_its_duration(world):
    accounts = make_accounts()
    v = world.add_asset(
        FakeAsset(uuid4(), ALICE, checksum="sha-v", original_file_name="clip.mp4", duration=12_345)
    )
    world.album("Trip", FAMILY, roles={ALICE: EDITOR}, assets=[v])

    result = await move_asset(accounts, OPTS, v.id, accounts.members["alice"], now=NOW)

    (up,) = world.uploads
    assert result.uploaded and up["duration"] == 12_345 and v.is_trashed


def test_reuploads_trashed_counts_per_member_and_shows_in_the_summary():
    from ifl.move import MoveManyResult, MoveResult

    def done(owner, reupload, trashed=True):
        return MoveResult(
            asset_id=uuid4(),
            owner_id=owner,
            family_asset_id=uuid4(),
            file_name="x.jpg",
            uploaded=False,
            edits=0,
            duplicate=reupload,
            reupload=reupload,
            trashed=trashed,
        )

    many = MoveManyResult(
        done=[done(ALICE, True), done(ALICE, True), done(BOB, True), done(BOB, False)]
    )
    assert many.reuploads_trashed() == {ALICE: 2, BOB: 1}
    # A scan item that finished an interrupted move is not counted.
    real = done(ALICE, True)
    real.duplicate = False
    assert MoveManyResult(done=[real]).reuploads_trashed() == {}
    assert str(many) == "moved 4 (3 re-uploads), blocked 0, failed 0, remaining 0"
    assert (
        str(MoveManyResult(done=[done(BOB, False)])) == "moved 1, blocked 0, failed 0, remaining 0"
    )
    assert MoveManyResult(done=[done(ALICE, True, trashed=False)]).reuploads_trashed() == {}


async def test_reuploads_never_use_the_limit(world, monkeypatch):
    from ifl.move import MoveError, MoveResult, move_assets
    from ifl.observer import MoveAsset

    accounts = make_accounts()
    alice = accounts.members["alice"]
    reuploads = [MoveAsset(uuid4(), alice, None, f"re{i}.jpg", reupload=True) for i in range(3)]
    plain = [MoveAsset(uuid4(), alice, None, f"new{i}.jpg") for i in range(2)]

    async def move(accounts, opts, asset_id, owner, **kw):
        if asset_id == reuploads[1].asset_id:
            raise MoveError("immich said no")
        return MoveResult(
            asset_id=asset_id,
            owner_id=owner.user_id,
            family_asset_id=uuid4(),
            file_name="x.jpg",
            uploaded=False,
            edits=0,
            trashed=True,
        )

    monkeypatch.setattr("ifl.move.move_asset", move)

    # The re-uploads come last here: the limit must not depend on the order.
    result = await move_assets(accounts, OPTS, plain + reuploads, limit=1)

    # One real move fitted the limit. Every re-upload was still tried, the failed one included.
    assert [r.asset_id for r in result.done] == [
        plain[0].asset_id,
        reuploads[0].asset_id,
        reuploads[2].asset_id,
    ]
    assert [f[0] for f in result.failed] == ["re1.jpg"] and result.remaining == 1
    assert result.unfinished == {plain[1].asset_id, reuploads[1].asset_id}


async def test_relink_takes_an_outside_error_as_a_refusal_and_lets_a_fault_rise(world, monkeypatch):
    from fakes import outage

    accounts = make_accounts()
    a = alice_asset(world)
    world.album("Trip", FAMILY, roles={ALICE: EDITOR}, assets=[a])
    bobs = world.album("Bobs", BOB, roles={ALICE: EDITOR}, assets=[a])
    world.partners[FAMILY] = {ALICE, BOB}  # so alice's key can add the family's copy
    real = world.add_album_assets
    answers = {BOB: outage("albums.add_assets_to_album")}

    async def add(client, album_id, asset_ids):
        if album_id == bobs.id and client.user_id in answers:
            raise answers[client.user_id]
        return await real(client, album_id, asset_ids)

    monkeypatch.setattr("ifl.move.add_album_assets", add)

    # Bob's key, the album owner's, gets no answer from Immich: alice's is tried next.
    result = await move_asset(accounts, OPTS, a.id, accounts.members["alice"], now=NOW)
    assert result.relinked_member == ["Bobs"] and a.is_trashed

    # A fault in the service is no refusal. It is not tried past, and not reworded.
    b = world.add_asset(FakeAsset(uuid4(), ALICE, checksum="sha-2", original_file_name="b.jpg"))
    world.assets[bobs.id].append(b)
    answers[BOB] = KeyError("owner")
    with pytest.raises(KeyError):
        await move_asset(accounts, OPTS, b.id, accounts.members["alice"], now=NOW)
    assert not b.is_trashed
