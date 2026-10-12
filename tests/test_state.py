"""The state file and the lock beside it."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, time, timedelta
from uuid import uuid4

import pytest

from ifl.state import Locked, State, StateError, hold_lock, lock_path

ALICE, BOB = uuid4(), uuid4()
NOW = datetime(2026, 10, 11, 22, 5, tzinfo=UTC)
TEN_PM = time(22, 0)


def test_missing_file_is_empty_and_save_round_trips(tmp_path):
    path = tmp_path / "sub" / "state.json"
    state = State.load(path)
    assert state.members == {} and state.last_report is None and not path.exists()

    state.set_mark(ALICE, NOW)
    state.add_reuploads(ALICE, 2)
    state.add_reuploads(BOB, 1)
    state.start_clock(NOW - timedelta(days=1))

    again = State.load(path)  # every change saved itself; no explicit save
    assert again.get_mark(ALICE) == NOW and again.get_mark(BOB) is None
    assert again.members[ALICE].reuploads == 2 and again.members[BOB].reuploads == 1
    assert again.last_report == NOW - timedelta(days=1)
    assert not (tmp_path / "sub" / "state.json.tmp").exists()
    raw = json.loads(path.read_text())
    assert set(raw) == {"last_report", "members"} and set(raw["members"]) == {str(ALICE), str(BOB)}


def test_the_owner_moves_a_file_that_does_not_parse_aside_and_a_reader_leaves_it(tmp_path, caplog):
    path = tmp_path / "state.json"
    path.write_text("{not json")

    with caplog.at_level(logging.WARNING, logger="ifl.state"):
        reader = State.load(path)
    assert reader.members == {} and path.read_text() == "{not json"
    assert "reading it as empty" in caplog.text

    with caplog.at_level(logging.WARNING, logger="ifl.state"):
        state = State.load(path, owner=True)
    assert state.members == {} and not path.exists()
    assert (tmp_path / "state.json.bad").read_text() == "{not json"
    assert "starting over" in caplog.text


@pytest.mark.parametrize(
    "content",
    [b"\xff\xfe\x00not text", b'{"members": {"%s": {"reuploads": 1e999}}}' % str(ALICE).encode()],
)
def test_bytes_that_are_not_text_and_a_number_out_of_range_do_not_parse(tmp_path, content):
    path = tmp_path / "state.json"
    path.write_bytes(content)
    assert State.load(path).members == {} and path.exists()
    assert State.load(path, owner=True).members == {} and not path.exists()
    assert (tmp_path / "state.json.bad").read_bytes() == content


def test_a_save_that_fails_is_logged_and_the_run_goes_on(tmp_path, caplog):
    blocker = tmp_path / "data"
    blocker.write_text("a file where the directory should be")
    state = State(blocker / "state.json")
    with caplog.at_level(logging.ERROR, logger="ifl.state"):
        state.set_mark(ALICE, NOW)
        state.add_reuploads(ALICE, 1)
    assert state.get_mark(ALICE) == NOW and state.members[ALICE].reuploads == 1
    assert caplog.text.count("cannot write the state file") == 2


def test_a_file_that_cannot_be_read_is_an_error_and_is_left_alone(tmp_path):
    path = tmp_path / "state.json"
    path.mkdir()  # reading a directory fails with an OSError that is not "missing"
    with pytest.raises(StateError, match="cannot read the state file"):
        State.load(path, owner=True)
    assert path.is_dir() and not (tmp_path / "state.json.bad").exists()


def test_a_lock_file_that_cannot_be_opened_is_an_error(tmp_path):
    (tmp_path / "state.json.lock").mkdir()
    with (
        pytest.raises(StateError, match="cannot open the run lock"),
        hold_lock(tmp_path / "state.json"),
    ):
        pass


def test_a_lock_file_that_is_not_writable_can_still_be_held(tmp_path):
    lock = lock_path(tmp_path / "state.json")
    lock.touch(mode=0o444)  # as another `user:` sees a lock file it did not create
    with hold_lock(tmp_path / "state.json"):
        pass


def test_a_save_replaces_a_leftover_temp_file_that_is_not_writable(tmp_path):
    state = State(tmp_path / "state.json")
    (tmp_path / "state.json.tmp").touch(mode=0o444)
    state.set_mark(ALICE, NOW)
    assert State.load(state.path).get_mark(ALICE) == NOW


def test_a_time_without_an_offset_is_read_as_utc(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "last_report": "2026-10-11T21:05:00",
                "members": {str(ALICE): {"scanned_until": "2026-10-01T00:00:00"}},
            }
        )
    )
    state = State.load(path)
    assert state.get_mark(ALICE) == datetime(2026, 10, 1, tzinfo=UTC)
    assert state.report_due(NOW, TEN_PM) is True  # compares, where a naive time would raise


@pytest.mark.parametrize(
    "last_report, now, due",
    [
        (None, NOW, False),  # never reported: the caller starts the clock
        (NOW, NOW + timedelta(minutes=10), False),  # reported at 22:05, now 22:15
        (NOW, NOW + timedelta(hours=23, minutes=50), False),  # 21:55 the next day
        (NOW, NOW + timedelta(hours=24), True),  # 22:05 the next day
        (NOW - timedelta(hours=1), NOW, True),  # reported at 21:05; 22:00 has passed since
        (NOW - timedelta(days=3), NOW, True),
    ],
)
def test_report_due_once_the_daily_time_has_passed_since_the_last_report(
    tmp_path, last_report, now, due
):
    state = State(tmp_path / "state.json", last_report=last_report)
    assert state.report_due(now, TEN_PM) is due


def test_take_report_returns_non_zero_tallies_and_resets_them(tmp_path):
    state = State(tmp_path / "state.json")
    state.add_reuploads(ALICE, 3)
    state.set_mark(BOB, NOW)
    assert state.take_report(NOW) == {ALICE: 3}
    assert state.last_report == NOW
    assert State.load(state.path).last_report == NOW
    assert state.members[ALICE].reuploads == 0 and state.get_mark(BOB) == NOW
    assert state.take_report(NOW) == {}


def test_keep_only_drops_other_members_and_saves(tmp_path, caplog):
    state = State(tmp_path / "state.json")
    state.set_mark(ALICE, NOW)
    state.add_reuploads(BOB, 2)
    with caplog.at_level(logging.INFO, logger="ifl.state"):
        state.keep_only({ALICE, BOB})
        assert not caplog.records
        state.keep_only({ALICE})
    assert set(state.members) == {ALICE} and set(State.load(state.path).members) == {ALICE}
    assert f"dropped state for 1 user(s) no longer in the config: ['{BOB}']" in caplog.text


def test_the_lock_refuses_a_second_holder_and_frees_on_exit(tmp_path):
    path = tmp_path / "data" / "state.json"
    with hold_lock(path):
        assert lock_path(path).exists()
        with pytest.raises(Locked, match="is the daemon running"), hold_lock(path):
            pass
    with hold_lock(path):
        pass
