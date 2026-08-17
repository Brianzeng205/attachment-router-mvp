"""Cross-platform, single-machine OS advisory lock for one polling cycle."""

from __future__ import annotations

import errno
import os
from pathlib import Path
from typing import BinaryIO


def poll_lock_path(state_database_path: Path) -> Path:
    return Path(f"{state_database_path}.poll.lock")


class ProcessLock:
    """A non-blocking advisory lock whose file may safely persist between runs."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._file: BinaryIO | None = None

    @property
    def acquired(self) -> bool:
        return self._file is not None

    def acquire(self) -> bool:
        if self._file is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK, 13, 36}:
                        handle.close()
                        return False
                    raise
            else:
                import fcntl

                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    if exc.errno in {errno.EACCES, errno.EAGAIN}:
                        handle.close()
                        return False
                    raise
        except BaseException:
            if not handle.closed:
                handle.close()
            raise
        self._file = handle
        return True

    def release(self) -> None:
        handle, self._file = self._file, None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self) -> "ProcessLock":
        if not self.acquire():
            raise RuntimeError("Polling process lock is already held")
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()
