"""The ``updating.json`` marker: while an update swaps the program folder, nothing starts from it.

``selfupdate.start_swap`` writes the marker beside the program folder (not in it: that folder is swapped), and
``swaphelper`` removes it when the swap is over, whatever happened. While it's less than an hour old, every
entry point refuses to start: the command line (``run --due`` skips its pass and exits 0), the desktop app and
the tray. Windows lets a folder be renamed while a program runs from it, so a program started during the swap
could load half its code from one version and half from the other. An older marker, from an update that
died, is ignored, so it can't stop runs for good.

Standard library only and quick to import: the command line and the app read it before anything else.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

MARKER = "updating.json"            # the same name as swaphelper.MARKER, which can't import this module
PROGRAM = "program"
MAX_AGE = timedelta(hours=1)
UPDATING = "FOLIO-Exchange is being updated. Try again in a few minutes."


def marker_path(root: str | os.PathLike) -> Path:
    """The marker for the install folder ``root``, beside its program folder."""
    return Path(root) / MARKER


def write_marker(root: str | os.PathLike, version: str, now: datetime | None = None) -> Path:
    now = now or datetime.now(timezone.utc)
    path = marker_path(root)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"version": version, "started": now.isoformat(timespec="seconds")}), encoding="utf-8")
    os.replace(tmp, path)
    return path


def marker_started(root: str | os.PathLike) -> datetime | None:
    """When the update that wrote the marker started, or None when there's no marker."""
    path = marker_path(root)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        started = datetime.fromisoformat(str(raw["started"]))
        return started if started.tzinfo else started.replace(tzinfo=timezone.utc)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError):
        try:
            return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        except OSError:
            return None


def marker_active(root: str | os.PathLike, now: datetime | None = None) -> bool:
    """True while a marker is less than an hour old. An older one is ignored; so is one dated more than five
    minutes ahead."""
    started = marker_started(root)
    if started is None:
        return False
    age = (now or datetime.now(timezone.utc)) - started
    return -timedelta(minutes=5) <= age < MAX_AGE


def update_in_progress(prefix: str | os.PathLike | None = None, now: datetime | None = None) -> bool:
    """Whether this install's program folder is being swapped right now. Only an install made by
    ``install.ps1`` (a Python environment called ``program``) has a marker."""
    prefix = Path(prefix if prefix is not None else sys.prefix)
    if prefix.name.lower() != PROGRAM:
        return False
    return marker_active(prefix.parent, now)
