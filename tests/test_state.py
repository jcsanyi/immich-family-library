"""The state file and the lock beside it."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, time, timedelta
from uuid import uuid4

import pytest

from ifl.state import Locked, State, hold_lock, lock_path

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
    state.last_report = NOW - timedelta(days=1)
    state.save()

    again = State.load(path)
    assert again.get_mark(ALICE) == NOW and again.get_mark(BOB) is None
    assert again.members[ALICE].reuploads == 2 and again.members[BOB].reuploads == 1
    assert again.last_report == NOW - timedelta(days=1)
    assert not (tmp_path / "sub" / "state.json.tmp").exists()
    raw = json.loads(path.read_text())
    assert set(raw) == {"last_report", "members"} and set(raw["members"]) == {str(ALICE), str(BOB)}


def test_an_unreadable_file_is_moved_aside_and_the_state_starts_over(tmp_path, caplog):
    path = tmp_path / "state.json"
    path.write_text("{not json")

    with caplog.at_level(logging.WARNING, logger="ifl.state"):
        state = State.load(path)

    assert state.members == {} and not path.exists()
    assert (tmp_path / "state.json.bad").read_text() == "{not json"
    assert "is unreadable" in caplog.text and "starting over" in caplog.text


def test_reload_picks_up_what_another_writer_saved(tmp_path):
    path = tmp_path / "state.json"
    mine = State.load(path)
    theirs = State.load(path)
    theirs.set_mark(ALICE, NOW)
    theirs.save()
    assert mine.get_mark(ALICE) is None
    mine.reload()
    assert mine.get_mark(ALICE) == NOW


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
    assert state.members[ALICE].reuploads == 0 and state.get_mark(BOB) == NOW
    assert state.take_report(NOW) == {}


def test_the_lock_refuses_a_second_holder_and_frees_on_exit(tmp_path):
    path = tmp_path / "data" / "state.json"
    with hold_lock(path):
        assert lock_path(path).exists()
        with pytest.raises(Locked, match="is the daemon running"), hold_lock(path):
            pass
    with hold_lock(path):
        pass
