"""验证跨进程文件锁的互斥、释放和异常处理行为。"""

import importlib
from pathlib import Path

import pytest


def test_file_lock_rejects_a_second_holder_and_can_be_reacquired(
    tmp_path: Path,
):
    """Removing non-blocking exclusion would let concurrent suites run."""
    locking = importlib.import_module("jmeter_suite.locking")

    lock_path = tmp_path / "nested" / "suite.lock"
    first = locking.FileLock(lock_path)
    second = locking.FileLock(lock_path)

    assert first.acquire() is True
    assert lock_path.is_file()
    assert second.acquire() is False

    first.release()
    assert second.acquire() is True
    second.release()


def test_file_lock_context_releases_after_an_exception(tmp_path: Path):
    """Missing finally cleanup would leave the suite locked after a crash."""
    locking = importlib.import_module("jmeter_suite.locking")
    lock_path = tmp_path / "suite.lock"

    with pytest.raises(RuntimeError, match="boom"):
        with locking.FileLock(lock_path):
            raise RuntimeError("boom")

    replacement = locking.FileLock(lock_path)
    assert replacement.acquire() is True
    replacement.release()


def test_file_lock_context_raises_a_specific_conflict_error(tmp_path: Path):
    """A generic startup error would hide the actionable lock conflict."""
    locking = importlib.import_module("jmeter_suite.locking")
    lock_path = tmp_path / "suite.lock"
    first = locking.FileLock(lock_path)
    assert first.acquire() is True

    try:
        with pytest.raises(locking.LockUnavailableError) as captured:
            with locking.FileLock(lock_path):
                pass
        assert captured.value.path == lock_path
        assert str(lock_path) in str(captured.value)
    finally:
        first.release()
