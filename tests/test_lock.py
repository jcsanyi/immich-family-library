"""The run lock."""

from __future__ import annotations

import pytest

from ifl.lock import Locked, LockError, hold_lock


def test_a_lock_file_that_cannot_be_opened_is_an_error(tmp_path):
    (tmp_path / "run.lock").mkdir()
    with (
        pytest.raises(LockError, match="cannot open the run lock"),
        hold_lock(tmp_path / "run.lock"),
    ):
        pass


def test_a_lock_file_that_is_not_writable_can_still_be_held(tmp_path):
    lock = tmp_path / "run.lock"
    lock.touch(mode=0o444)  # as another `user:` sees a lock file it did not create
    with hold_lock(lock):
        pass


def test_the_lock_refuses_a_second_holder_and_frees_on_exit(tmp_path):
    path = tmp_path / "data" / "run.lock"
    with hold_lock(path):
        assert path.exists()
        with pytest.raises(Locked, match="is the daemon running"), hold_lock(path):
            pass
    with hold_lock(path):
        pass
