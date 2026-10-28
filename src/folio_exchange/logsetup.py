"""Where FOLIO-Exchange writes its log: the site's ``logs/folio-exchange.log`` and, with a console, stderr.

Shared by the command line and the desktop app, so both write the same log in the same form. Nothing logged
anywhere in the program holds a secret; screens and logs say where a secret comes from, never its value.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

LOG_FILE = "folio-exchange.log"


def setup_logging(logs_dir: Path | None, verbose: bool, *, console: bool = True) -> None:
    """Send log records to ``logs_dir/folio-exchange.log`` (when given) and to stderr (when ``console``; the
    desktop app started without a console has none). Calling it again replaces what an earlier call set up."""
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for old in [h for h in root.handlers if getattr(h, "_folio_exchange", False)]:
        root.removeHandler(old)
        old.close()
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)] if console else []
    if logs_dir is not None:
        logs_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(logs_dir / LOG_FILE, encoding="utf-8"))
    for h in handlers:
        h.setFormatter(fmt)
        h._folio_exchange = True
        root.addHandler(h)
    for noisy in ("urllib3", "paramiko"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
