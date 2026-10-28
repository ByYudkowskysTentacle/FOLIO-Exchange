"""Long actions and site reads off the window's thread, reported back as Qt signals.

A job runs in ``jobs.Jobs``' own daemon thread, as on the page; ``JobWatcher`` only looks at it on a timer.
The job's thread writes the Job object, so it is read only through ``Job.as_dict(since)``, which takes one
snapshot of the lines it returns.

Python collects reference cycles in whichever thread is running when its counts pass a threshold, and with reads,
jobs and the page's server in threads of their own that is often not the window's. A Qt object kept alive only by
a cycle would then be destroyed off the window's thread, which crashes the process. ``MainThreadCollector`` turns
that automatic collection off while the app runs and collects on a timer on the window's thread instead.
"""
from __future__ import annotations

import gc
import logging
import threading
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QTimer, Signal

from ..jobs import Job

log = logging.getLogger(__name__)

POLL_MS = 200
COLLECT_MS = 500                     # how often MainThreadCollector looks whether Python would collect now


class JobWatcher(QObject):
    """Streams a job's new log lines (``progress``) and, once it ends, its final state (``finished``, the
    job's as_dict: status, result, error, seconds)."""

    progress = Signal(list)
    finished = Signal(dict)

    def __init__(self, job: Job, parent: QObject | None = None, interval_ms: int = POLL_MS) -> None:
        super().__init__(parent)
        self.job = job
        self.seen = 0
        self.timer = QTimer(self)
        self.timer.setInterval(interval_ms)
        self.timer.timeout.connect(self.poll)

    def start(self) -> JobWatcher:
        self.timer.start()
        return self

    def poll(self) -> None:
        state = self.job.as_dict(self.seen)
        if state["status"] != "running":
            state = self.job.as_dict(self.seen)     # lines are all written before the status changes
        self.seen = state["log_total"]
        if state["log"]:
            self.progress.emit(state["log"])
        if state["status"] != "running":
            self.timer.stop()
            self.finished.emit(state)


class Reader(QObject):
    """Runs a read (``snapshot`` of a site) in a daemon thread and hands back its result or its exception, so
    a slow drive never freezes the window. Only the newest read's answer is delivered."""

    done = Signal(object, object)            # (result, exception): one of them is None
    _answer = Signal(int, object, object)    # from the reading thread, queued to this object's thread

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.serial = 0
        self.busy = False
        self._answer.connect(self._deliver)

    def read(self, fn: Callable[[], Any]) -> None:
        self.serial += 1
        serial = self.serial
        self.busy = True

        def body() -> None:
            try:
                result, error = fn(), None
            except Exception as exc:  # noqa: BLE001 - shown in the window; the details go to the log
                log.debug("Reading the site failed", exc_info=True)
                result, error = None, exc
            try:
                self._answer.emit(serial, result, error)
            except RuntimeError:                 # the window closed while this read ran
                pass
            finally:
                # The error's traceback holds this frame: let go of the error here, or the two make a cycle
                # (holding this Reader too) that only a garbage collection, in any thread, would free.
                result = error = None

        threading.Thread(target=body, name="fx-app-read", daemon=True).start()

    def _deliver(self, serial: int, result: Any, error: Exception | None) -> None:
        if serial != self.serial:
            return                               # an older read finished after a newer one began
        self.busy = False
        self.done.emit(result, error)


class MainThreadCollector(QObject):
    """Collects cyclic garbage on this (the window's) thread only, while it is ``start``ed.

    Python's automatic collection is turned off, and every COLLECT_MS this collects what Python itself would
    have by now: the youngest generation once its count passes the threshold, an older one when its count does
    too. ``stop`` collects once more, here, and turns automatic collection back on if it was on before, so
    collectors can be nested (the app's inside a test's)."""

    def __init__(self, parent: QObject | None = None, interval_ms: int = COLLECT_MS) -> None:
        super().__init__(parent)
        self.was_enabled: bool | None = None
        self.timer = QTimer(self)
        self.timer.setInterval(interval_ms)
        self.timer.timeout.connect(self.collect_due)

    def start(self) -> MainThreadCollector:
        if self.was_enabled is None:
            self.was_enabled = gc.isenabled()
            gc.disable()
            self.timer.start()
        return self

    def collect_due(self) -> None:
        counts, limits = gc.get_count(), gc.get_threshold()
        if counts[0] <= limits[0]:
            return
        generation = 0
        if counts[1] > limits[1]:
            generation = 2 if counts[2] > limits[2] else 1
        gc.collect(generation)

    def stop(self) -> None:
        if self.was_enabled is None:
            return
        self.timer.stop()
        gc.collect()
        if self.was_enabled:
            gc.enable()
        self.was_enabled = None
