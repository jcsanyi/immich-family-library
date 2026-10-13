"""The run lock."""

from __future__ import annotations

import pytest

from ifl.state import Locked, StateError, hold_lock, lock_path


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


def test_the_lock_refuses_a_second_holder_and_frees_on_exit(tmp_path):
    path = tmp_path / "data" / "state.json"
    with hold_lock(path):
        assert lock_path(path).exists()
        with pytest.raises(Locked, match="is the daemon running"), hold_lock(path):
            pass
    with hold_lock(path):
        pass
