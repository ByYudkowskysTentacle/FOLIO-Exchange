"""Long actions (check, preview, run, release, test delivery) in a background thread, one at a time.

Shared by the local page and the desktop app, so both show the same log lines and refuse a second action the
same way. Each job collects the log lines its own thread writes.
"""
from __future__ import annotations

import logging
import threading
import time
import traceback
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from .errors import FxError, LockBusy

log = logging.getLogger(__name__)


class Job:
    def __init__(self, kind: str, feed: str | None) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.kind, self.feed = kind, feed
        self.status = "running"                     # running | done | error
        self.started = time.monotonic()
        self.seconds: float | None = None
        self.lines: list[str] = []
        self.result: Any = None
        self.error: str | None = None
        self.thread: threading.Thread | None = None

    def as_dict(self, since: int = 0) -> dict:
        fresh = self.lines[since:]               # one snapshot, so log and log_total always agree
        return {"id": self.id, "kind": self.kind, "feed": self.feed, "status": self.status,
                "seconds": round(self.seconds if self.seconds is not None else time.monotonic() - self.started, 1),
                "log": fresh, "log_total": since + len(fresh), "result": self.result, "error": self.error}


class _JobLog(logging.Handler):
    """Copies log records written by one job's thread into that job."""

    def __init__(self, job: Job, thread_id: int) -> None:
        super().__init__(logging.INFO)
        self.job, self.thread_id = job, thread_id
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        if record.thread == self.thread_id and record.name.startswith("folio_exchange"):
            self.job.lines.append(self.format(record))


class Jobs:
    """At most one job at a time: the site lock allows one run anyway, and one at a time is easier to follow."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.jobs: dict[str, Job] = {}
        self.current: Job | None = None

    def _claim(self, kind: str, feed: str | None, refuse: Callable[[Job], str] | None = None) -> Job:
        """Make a new job the current one, or refuse while another runs. The lock is held only for this, never
        while a job works, so a window that asks is never kept waiting."""
        with self.lock:
            running = self.current
            if running is not None and running.status == "running":
                raise FxError(refuse(running) if refuse else
                              f"Another action is still running ({running.kind}"
                              f"{' ' + running.feed if running.feed else ''}). Wait for it to finish.")
            job = Job(kind, feed)
            self.jobs[job.id] = job
            self.current = job
            return job

    @contextmanager
    def holding(self, kind: str, refuse: Callable[[Job], str] | None = None) -> Iterator[Job]:
        """Do ``kind`` in the calling thread as the running action: others are refused (``refuse`` words the
        refusal of this one) and see it running until the block ends."""
        job = self._claim(kind, None, refuse)
        try:
            yield job
            job.status = "done"
        except BaseException as exc:
            job.status, job.error = "error", str(exc)
            raise
        finally:
            job.seconds = round(time.monotonic() - job.started, 1)

    def start(self, kind: str, feed: str | None, work) -> Job:
        job = self._claim(kind, feed)

        def body() -> None:
            handler = _JobLog(job, threading.get_ident())
            root = logging.getLogger()
            root.addHandler(handler)
            try:
                job.result = work()
                job.status = "done"
            except LockBusy as exc:
                job.status, job.error = "error", str(exc)
            except FxError as exc:
                job.status, job.error = "error", str(exc)
            except Exception as exc:  # noqa: BLE001 - shown to the person; the details go to the log
                job.status, job.error = "error", f"{type(exc).__name__}: {exc}"
                log.debug("%s", traceback.format_exc())
            finally:
                job.seconds = round(time.monotonic() - job.started, 1)
                root.removeHandler(handler)

        job.thread = threading.Thread(target=body, name=f"fx-job-{job.id}", daemon=True)
        job.thread.start()
        return job

    def running(self) -> Job | None:
        job = self.current
        return job if job is not None and job.status == "running" else None
