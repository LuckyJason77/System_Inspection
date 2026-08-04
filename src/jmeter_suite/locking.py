"""提供跨进程文件锁，防止巡检套件或调度器重复并发运行。"""

from __future__ import annotations

import errno
import os
from pathlib import Path
from types import TracebackType


class LockUnavailableError(RuntimeError):
    """Raised when another process already owns a non-blocking file lock."""

    def __init__(self, path: Path):
        self.path = path
        super().__init__(f"文件锁已被占用: {path}")


def _try_platform_lock(file_descriptor: int) -> bool:
    os.lseek(file_descriptor, 0, os.SEEK_SET)
    if os.name == "nt":
        import msvcrt

        try:
            msvcrt.locking(file_descriptor, msvcrt.LK_NBLCK, 1)
        except OSError as error:
            if error.errno in {
                errno.EACCES,
                errno.EAGAIN,
                errno.EDEADLK,
            }:
                return False
            raise
        return True

    import fcntl

    try:
        fcntl.flock(file_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _platform_unlock(file_descriptor: int) -> None:
    os.lseek(file_descriptor, 0, os.SEEK_SET)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(file_descriptor, msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(file_descriptor, fcntl.LOCK_UN)


class FileLock:
    """A small, non-blocking cross-process lock backed by one file byte."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._file_descriptor: int | None = None

    def acquire(self) -> bool:
        if self._file_descriptor is not None:
            return True

        self.path.parent.mkdir(parents=True, exist_ok=True)
        file_descriptor = os.open(
            self.path,
            os.O_RDWR | os.O_CREAT,
            0o600,
        )
        try:
            if os.fstat(file_descriptor).st_size == 0:
                os.write(file_descriptor, b"\0")
            if not _try_platform_lock(file_descriptor):
                os.close(file_descriptor)
                return False
        except BaseException:
            os.close(file_descriptor)
            raise

        self._file_descriptor = file_descriptor
        return True

    def release(self) -> None:
        file_descriptor = self._file_descriptor
        if file_descriptor is None:
            return
        self._file_descriptor = None
        try:
            _platform_unlock(file_descriptor)
        finally:
            os.close(file_descriptor)

    def __enter__(self) -> FileLock:
        if not self.acquire():
            raise LockUnavailableError(self.path)
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()
