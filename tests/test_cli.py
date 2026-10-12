"""The commands' exit codes, with the accounts and Immich faked. No network."""

from __future__ import annotations

from uuid import uuid4

import pytest
from fakes import ALICE, BOB, EDITOR, FAMILY, VIEWER, FakeAsset, make_accounts, patch_world
from immichpy.client.generated.models.bulk_id_error_reason import BulkIdErrorReason
from immichpy.client.generated.models.bulk_id_response_dto import BulkIdResponseDto

from ifl.accounts import AccountError
from ifl.cli import main

CONFIG = """
[immich]
url = "https://x.example"
[service]
readonly = {readonly}
state_path = "state.json"
settle_seconds = 0
[family]
email = "f@x"
api_key = "k"
dropbox_album = "Drop"
[members.alice]
email = "alice@x"
api_key = "k"
[members.bob]
email = "bob@x"
api_key = "k"
"""


@pytest.fixture
def world(monkeypatch):
    w = patch_world(monkeypatch, "ifl.observer", "ifl.convert", "ifl.reconcile", "ifl.move")
    w.partners[FAMILY] = {ALICE, BOB}

    async def fake_open(cfg):
        return make_accounts()

    monkeypatch.setattr("ifl.cli.open_accounts", fake_open)
    return w


@pytest.fixture
def config(tmp_path):
    def write(readonly: bool = False) -> str:
        path = tmp_path / "config.toml"
        path.write_text(CONFIG.format(readonly=str(readonly).lower()))
        return str(path)

    return write


def run(config_path: str, *args: str) -> int:
    return main(["-c", config_path, *args])


def test_config_and_key_problems_exit_2(world, config, monkeypatch, tmp_path):
    assert run(str(tmp_path / "missing.toml"), "observe") == 2
    assert run(config(readonly=True), "process") == 2  # readonly refuses a writing command
    assert run(config(readonly=True), "observe") == 0  # but not a reading one

    async def bad_key(cfg):
        raise AccountError("[alice] key is missing permissions")

    monkeypatch.setattr("ifl.cli.open_accounts", bad_key)
    assert run(config(), "observe") == 2


def test_observe_reports_and_exits_0_whatever_it_finds(world, config):
    a = world.add_asset(FakeAsset(uuid4(), ALICE, "aa", "a.jpg"))
    world.album("Trip", FAMILY, roles={ALICE: EDITOR}, assets=[a])  # a move, and no dropbox yet
    assert run(config(), "observe") == 0
    assert not a.is_trashed and not world.uploads


def test_process_exits_0_when_clean_and_1_when_a_move_fails(world, config, monkeypatch):
    path = config()
    world.album("Drop", FAMILY, roles={ALICE: EDITOR, BOB: EDITOR})
    a = world.add_asset(FakeAsset(uuid4(), ALICE, "aa", "a.jpg"))
    world.album("Trip", FAMILY, roles={ALICE: EDITOR, BOB: EDITOR}, assets=[a])
    assert run(path, "process") == 0 and a.is_trashed

    # bob's album also holds alice's next photo, and Immich refuses every add to
    # it. (A missing partner share wouldn't do here: the pass reconciles first.)
    b = world.add_asset(FakeAsset(uuid4(), ALICE, "bb", "b.jpg"))
    world.album("Trip2", FAMILY, roles={ALICE: EDITOR, BOB: EDITOR}, assets=[b])
    bobs = world.album("Bobs", BOB, roles={ALICE: EDITOR}, assets=[b])
    real_add = world.add_album_assets

    async def refuse_bobs(client, album_id, ids):
        if album_id == bobs.id:
            return [
                BulkIdResponseDto(id=i, success=False, error=BulkIdErrorReason.UNKNOWN) for i in ids
            ]
        return await real_add(client, album_id, ids)

    monkeypatch.setattr("ifl.move.add_album_assets", refuse_bobs)
    assert run(path, "process") == 1 and not b.is_trashed


def test_move_one_exits_1_when_blocked_or_not_movable_and_0_when_done(world, config):
    path = config()
    world.album("Drop", FAMILY, roles={ALICE: EDITOR, BOB: EDITOR})
    a = world.add_asset(FakeAsset(uuid4(), ALICE, "aa", "a.jpg", exif_info=None))  # blocked
    world.album("Trip", FAMILY, roles={ALICE: EDITOR}, assets=[a])
    stray = world.add_asset(FakeAsset(uuid4(), ALICE, "ss", "s.jpg"))  # in no family album

    assert run(path, "move", str(a.id)) == 1 and not a.is_trashed
    assert run(path, "move", str(stray.id)) == 1 and not stray.is_trashed
    assert run(path, "move", str(uuid4())) == 1
    a.exif_info = world.find_asset(stray.id).exif_info  # extraction catches up
    assert run(path, "move", str(a.id)) == 0 and a.is_trashed


def test_move_all_exits_1_only_for_failures_not_blocks(world, config):
    path = config()
    world.album("Drop", FAMILY, roles={ALICE: EDITOR, BOB: EDITOR})
    blocked = world.add_asset(FakeAsset(uuid4(), ALICE, "aa", "a.jpg", exif_info=None))
    world.album("Trip", FAMILY, roles={ALICE: EDITOR}, assets=[blocked])
    assert run(path, "move", "--all") == 0 and not blocked.is_trashed


def test_convert_album_exits_1_for_an_album_the_scan_would_not_convert(world, config):
    path = config()
    world.album("Drop", FAMILY, roles={ALICE: EDITOR, BOB: EDITOR})
    private = world.album("Private", ALICE, roles={BOB: VIEWER})  # not shared with the family
    shared = world.album("Beach", ALICE, roles={FAMILY: EDITOR}, assets=[FakeAsset(uuid4(), ALICE)])
    assert run(path, "convert-album", str(private.id)) == 1 and private.id in world.albums
    assert run(path, "convert-album", str(shared.id)) == 0 and shared.id not in world.albums
    assert run(path, "convert-album", "--all") == 0


def test_reconcile_albums_exits_0_and_fixes_what_it_finds(world, config):
    world.album("Hikes", FAMILY, roles={ALICE: EDITOR, BOB: VIEWER})
    assert run(config(), "reconcile-albums") == 0
    assert any(w[0] == "create" for w in world.writes)  # the dropbox
    assert any(w[0] == "role" for w in world.writes)  # bob promoted


def test_a_writing_command_refuses_while_another_holds_the_lock(world, config, tmp_path):
    from ifl.state import hold_lock

    path = config()
    world.album("Drop", FAMILY, roles={ALICE: EDITOR, BOB: EDITOR})
    with hold_lock(tmp_path / "state.json"):
        assert run(path, "process") == 3
        assert run(path, "move", "--all") == 3
        assert run(path, "observe") == 0  # reads only, so it never needs the lock
    assert run(path, "process") == 0


def test_observe_reads_the_state_without_creating_it_and_process_writes_it(world, config, tmp_path):
    path = config()
    world.album("Drop", FAMILY, roles={ALICE: EDITOR, BOB: EDITOR})
    assert run(path, "observe") == 0 and not (tmp_path / "state.json").exists()
    assert run(path, "process") == 0 and (tmp_path / "state.json").exists()
    assert run(path, "observe") == 0


def test_move_one_tallies_a_reupload(world, config, tmp_path):
    from datetime import UTC, datetime

    from ifl.state import State

    path = config()
    world.album("Drop", FAMILY, roles={ALICE: EDITOR, BOB: EDITOR})
    copy = world.add_asset(FakeAsset(uuid4(), FAMILY, "aa", "a.jpg"))
    world.metadata[copy.id] = {"ifl": {"origin_asset_id": str(uuid4())}}
    re = world.add_asset(FakeAsset(uuid4(), ALICE, "aa", "a.jpg"))
    state = State(tmp_path / "state.json")
    state.set_mark(ALICE, datetime(2025, 1, 1, tzinfo=UTC))

    assert run(path, "move", str(re.id)) == 0 and re.is_trashed
    assert State.load(state.path).members[ALICE].reuploads == 1


def test_a_lock_that_cannot_be_opened_exits_2_without_a_traceback(world, config, tmp_path):
    (tmp_path / "state.json.lock").mkdir()
    assert run(config(), "reconcile-albums") == 2
    assert run(config(), "observe") == 0
