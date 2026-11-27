"""Cross-process exclusive file locks (Sprint 13 P1.1).

``exclusive(path)`` holds an OS-level lock on a lock file for the duration of a ``with`` block. Other processes
block (up to ``timeout`` seconds, then :class:`LockTimeout`) until it is released; the operating system releases
it automatically when the holding process dies, so a crash never leaves a lock behind.

* Windows: ``msvcrt.locking`` on the first byte of the lock file. POSIX: ``fcntl.flock``.
* Re-entrant inside one process and thread (nested ``with exclusive(p)`` blocks share the OS lock), so a caller
  holding a lock can call code that takes the same lock.
* ``try_exclusive(path)`` never waits: it returns a held :class:`Held` or None. It is used to PROVE that the
  owner of a lock is gone (a dead process cannot hold an OS lock).
"""

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from src.config import ConfigError


class LockTimeout(ConfigError):
    """The lock could not be acquired in time (another process holds it): the operation is refused."""


_STATE: dict[str, dict] = {}
_GUARD = threading.Lock()


def _os_lock(fh, blocking: bool) -> bool:
    try:
        import msvcrt

        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        return True
    except ImportError:  # pragma: no cover - POSIX
        import fcntl

        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            return True
        except BlockingIOError:
            return False
    except OSError:
        return False


def _os_unlock(fh) -> None:
    try:
        import msvcrt

        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    except ImportError:  # pragma: no cover - POSIX
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


class Held:
    """A lock held by this process (release with :meth:`release`)."""

    def __init__(self, key: str) -> None:
        self.key = key
        self.released = False

    def release(self) -> None:
        if self.released:
            return
        self.released = True
        with _GUARD:
            st = _STATE.get(self.key)
            if st is None:
                return
            st["depth"] -= 1
            if st["depth"] > 0:
                return
            _STATE.pop(self.key, None)
        try:
            _os_unlock(st["fh"])
        finally:
            st["fh"].close()


def _key(path: str | Path) -> str:
    """Process-local identity of a lock file. Pure string normalisation: it must not depend on whether the file
    exists yet (``Path.resolve`` can differ before and after creation on Windows)."""
    return os.path.normcase(os.path.abspath(str(path)))


def _acquire(path: Path, timeout: float | None) -> Held | None:
    path = Path(path)
    key = _key(path)
    ident = threading.get_ident()
    with _GUARD:
        st = _STATE.get(key)
        if st is not None and st["thread"] == ident:
            st["depth"] += 1                          # re-entrant in the holding thread
            return Held(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Nothing is ever written to the lock file: on Windows the locked byte may lie beyond end-of-file, and writing
    # into a byte another process has locked would fail (mandatory locking).
    fh = open(path, "a+b")
    try:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with _GUARD:
                busy = key in _STATE                  # another thread of this process holds it
            if not busy and _os_lock(fh, blocking=False):
                with _GUARD:
                    _STATE[key] = {"fh": fh, "depth": 1, "thread": ident}
                return Held(key)
            if deadline is not None and time.monotonic() >= deadline:
                fh.close()
                return None
            time.sleep(0.05)
    except BaseException:
        fh.close()
        raise


@contextmanager
def exclusive(path: str | Path, timeout: float = 60.0) -> Iterator[Held]:
    """Hold the exclusive lock on ``path`` for the ``with`` block (refused after ``timeout`` seconds)."""
    held = _acquire(Path(path), timeout)
    if held is None:
        raise LockTimeout(f"{Path(path).name} is locked by another process (waited {timeout:g} s): refused, try again")
    try:
        yield held
    finally:
        held.release()


def acquire(path: str | Path, timeout: float = 60.0) -> Held:
    """Hold the lock until :meth:`Held.release` (or process exit)."""
    held = _acquire(Path(path), timeout)
    if held is None:
        raise LockTimeout(f"{Path(path).name} is locked by another process (waited {timeout:g} s): refused")
    return held


def fsync_dir(path: str | Path) -> None:
    """Flush a directory entry (POSIX: makes a rename / file creation durable). No-op where unsupported."""
    if os.name == "nt":                               # NTFS journals the metadata; see durable_replace
        return
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def durable_replace(src: str | Path, dst: str | Path) -> None:
    """Atomically replace ``dst`` with ``src`` and make the rename durable.

    Windows: ``MoveFileExW(REPLACE_EXISTING | WRITE_THROUGH)`` (returns only after the move is flushed), retried a few
    times on a transient sharing violation (e.g. a scanner holding the file). POSIX: ``os.replace`` then an fsync of
    the directory. The caller fsyncs ``src``'s contents first."""
    src, dst = str(src), str(dst)
    if os.name == "nt":
        import ctypes

        move = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
        move.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32)
        move.restype = ctypes.c_int
        for attempt in range(20):
            if move(src, dst, 0x1 | 0x8):            # MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH
                return
            err = ctypes.get_last_error()
            if err not in (5, 32) or attempt == 19:  # access denied / sharing violation: transient, retry
                raise OSError(err, f"MoveFileExW({src} -> {dst}) failed")
            time.sleep(0.05)
    os.replace(src, dst)
    fsync_dir(Path(dst).parent)


def held_here(path: str | Path) -> bool:
    """True if a thread of THIS process holds the lock (a liveness proof must not count its own lock)."""
    with _GUARD:
        return _key(path) in _STATE


def try_exclusive(path: str | Path) -> Held | None:
    """The lock if nobody holds it right now, else None (never waits). Only meaningful across processes."""
    return _acquire(Path(path), 0.0)


__all__ = ["exclusive", "acquire", "try_exclusive", "held_here", "durable_replace", "fsync_dir", "Held",
           "LockTimeout"]
