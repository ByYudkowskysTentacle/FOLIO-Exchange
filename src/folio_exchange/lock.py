"""One run at a time per site, using an OS file lock that is released if the process dies."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from .errors import LockBusy


class RunLock:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._fh = None

    def __enter__(self) -> RunLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")
        try:
            if sys.platform == "win32":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            raise LockBusy(
                f"Another FOLIO-Exchange run is in progress for this site (lock: {self.path}). "
                "Wait for it to finish; if none is running, the lock clears when the stuck process ends."
            ) from None
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()).encode())
        fh.flush()
        self._fh = fh
        return self

    def __exit__(self, *exc) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()
