"""Where FOLIO-Exchange writes its log: the site's ``logs/folio-exchange.log`` and, with a console, stderr.

Shared by the command line and the desktop app, so both write the same log in the same form. Nothing logged
anywhere in the program holds a secret; screens and logs say where a secret comes from, never its value.

The log is rotated when a program starts (``rotate``), never while it runs. Each program keeps the file open
while it runs, as a plain FileHandler does, so logging never fails half way through a delivery, and Windows
still refuses to rename a site folder while another FOLIO-Exchange program has its log open. The price: while
the desktop app or the page stays open, the hourly task can't rotate the log, and the next start after they
close does it.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

LOG_FILE = "folio-exchange.log"
CRON_LOG = "cron.log"                     # where the crontab line `schedule` shows sends the hourly task's output
LOG_MAX_BYTES = 5 * 1024 * 1024           # a log larger than this is rotated when the next program starts
LOG_KEEP = 4                              # older logs kept beside it: folio-exchange.log.1 (newest) to .4


def rotate(path: Path, max_bytes: int = LOG_MAX_BYTES, keep: int = LOG_KEEP) -> bool:
    """Move ``path`` to ``path.1`` (``.1`` to ``.2`` and so on, dropping the oldest) when it is larger than
    ``max_bytes``. Returns whether it was moved. Never raises: a file another program holds open (Windows will
    not rename it) is left as it is and rotated by a later start."""
    try:
        if path.stat().st_size <= max_bytes:
            return False
        for n in range(keep - 1, 0, -1):
            older = path.with_name(f"{path.name}.{n}")
            if older.exists():
                os.replace(older, path.with_name(f"{path.name}.{n + 1}"))
        os.replace(path, path.with_name(f"{path.name}.1"))
        return True
    except OSError:
        return False


def setup_logging(logs_dir: Path | None, verbose: bool, *, console: bool = True) -> None:
    """Send log records to ``logs_dir/folio-exchange.log`` (when given) and to stderr (when ``console``; the
    desktop app started without a console has none). Calling it again replaces what an earlier call set up.
    The site's logs are rotated first when they have grown past LOG_MAX_BYTES (see ``rotate``)."""
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for old in [h for h in root.handlers if getattr(h, "_folio_exchange", False)]:
        root.removeHandler(old)
        old.close()
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)] if console else []
    if logs_dir is not None:
        logs_dir.mkdir(parents=True, exist_ok=True)
        for name in (LOG_FILE, CRON_LOG):
            rotate(logs_dir / name, LOG_MAX_BYTES, LOG_KEEP)
        handlers.append(logging.FileHandler(logs_dir / LOG_FILE, encoding="utf-8"))
    for h in handlers:
        h.setFormatter(fmt)
        h._folio_exchange = True
        root.addHandler(h)
    for noisy in ("urllib3", "paramiko"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
