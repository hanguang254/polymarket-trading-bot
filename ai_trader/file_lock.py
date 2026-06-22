"""Cross-platform fcntl-style file locking.

Production code historically used ``fcntl.flock`` directly.  Windows does not
ship the ``fcntl`` module, so this wrapper exposes the tiny API surface the bot
needs while keeping native ``fcntl`` semantics on Unix-like systems.
"""
from __future__ import annotations

import os

LOCK_EX = 2
LOCK_SH = 1
LOCK_UN = 8
LOCK_NB = 4

if os.name == "nt":
    import msvcrt

    def _fileno(file_or_fd):
        return file_or_fd if isinstance(file_or_fd, int) else file_or_fd.fileno()

    def flock(file_or_fd, operation):
        """Lock or unlock one byte of a lock file on Windows."""
        fd = _fileno(file_or_fd)
        try:
            current_pos = os.lseek(fd, 0, os.SEEK_CUR)
        except OSError:
            current_pos = None
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            if operation & LOCK_UN:
                mode = msvcrt.LK_UNLCK
            elif operation & LOCK_NB:
                mode = msvcrt.LK_NBLCK
            else:
                mode = msvcrt.LK_LOCK
            msvcrt.locking(fd, mode, 1)
        finally:
            if current_pos is not None:
                try:
                    os.lseek(fd, current_pos, os.SEEK_SET)
                except OSError:
                    pass

else:
    import fcntl as _fcntl

    LOCK_EX = _fcntl.LOCK_EX
    LOCK_SH = _fcntl.LOCK_SH
    LOCK_UN = _fcntl.LOCK_UN
    LOCK_NB = _fcntl.LOCK_NB
    flock = _fcntl.flock
