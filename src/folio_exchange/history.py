"""Run history (RUN-1) and the audit log: what each run sent, and who changed what.

Kept in ``data/history.sqlite``, apart from the cache, so rebuilding the cache never
loses the record of deliveries. Nothing secret is ever stored here.
"""
from __future__ import annotations

import getpass
import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    feed TEXT NOT NULL, recipe TEXT, recipe_version TEXT, trigger TEXT, run_dir TEXT,
    started TEXT NOT NULL, finished TEXT, status TEXT NOT NULL DEFAULT 'running',
    items INTEGER, bibs INTEGER, delivered INTEGER NOT NULL DEFAULT 0,
    files TEXT, destinations TEXT, error TEXT, report TEXT, actor TEXT);
CREATE INDEX IF NOT EXISTS runs_feed ON runs(feed, started);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL, actor TEXT, feed TEXT, action TEXT NOT NULL, detail TEXT);
CREATE INDEX IF NOT EXISTS audit_ts ON audit(ts);
"""

STATUSES = ("running", "success", "built", "blocked", "held", "failed", "preview")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def current_user() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 - no login name (e.g. some containers)
        return "unknown"


def file_digest(path: Path) -> dict:
    """Name, size and SHA-256 of a delivered file, for the audit trail."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return {"name": Path(path).name, "bytes": Path(path).stat().st_size, "sha256": h.hexdigest()}


def _row(cur: sqlite3.Cursor, row: tuple) -> dict:
    out = {d[0]: v for d, v in zip(cur.description, row, strict=True)}
    for key in ("files", "destinations", "report", "detail"):
        if key in out and out[key]:
            out[key] = json.loads(out[key])
    if "delivered" in out:
        out["delivered"] = bool(out["delivered"])
    return out


class History:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> History:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _query(self, sql: str, params: tuple = ()) -> list[dict]:
        cur = self.db.execute(sql, params)
        return [_row(cur, r) for r in cur.fetchall()]

    # ------------------------------------------------------------------ runs
    def start_run(self, feed: str, recipe: str, recipe_version: str, trigger: str, run_dir: Path | None) -> int:
        cur = self.db.execute(
            "INSERT INTO runs(feed, recipe, recipe_version, trigger, run_dir, started, actor) VALUES (?,?,?,?,?,?,?)",
            (feed, recipe, recipe_version, trigger, str(run_dir) if run_dir else None, _now(), current_user()),
        )
        self.db.commit()
        return int(cur.lastrowid)

    def finish_run(
        self,
        run_id: int,
        status: str,
        *,
        items: int | None = None,
        bibs: int | None = None,
        files: list[dict] | None = None,
        delivered: list[str] | None = None,
        error: str | None = None,
        report: dict | None = None,
    ) -> None:
        if status not in STATUSES:
            raise ValueError(f"unknown run status {status!r}")
        self.db.execute(
            """UPDATE runs SET finished=?, status=?, items=?, bibs=?, delivered=?, files=?, destinations=?,
               error=?, report=? WHERE id=?""",
            (
                _now(), status, items, bibs, int(bool(delivered)), json.dumps(files or []),
                json.dumps(delivered or []), error, json.dumps(report or {}, default=str), run_id,
            ),
        )
        self.db.commit()

    def last_delivered(self, feed: str) -> dict | None:
        """The latest run of ``feed`` that delivered files (the safety check's baseline)."""
        rows = self._query("SELECT * FROM runs WHERE feed=? AND delivered=1 ORDER BY id DESC LIMIT 1", (feed,))
        return rows[0] if rows else None

    def runs(self, feed: str | None = None, limit: int = 20) -> list[dict]:
        if feed:
            return self._query("SELECT * FROM runs WHERE feed=? ORDER BY id DESC LIMIT ?", (feed, limit))
        return self._query("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))

    def runs_since(self, feed: str, since: datetime) -> list[dict]:
        stamp = since.astimezone(timezone.utc).isoformat(timespec="seconds")
        return self._query("SELECT * FROM runs WHERE feed=? AND started>=? ORDER BY id", (feed, stamp))

    def last_run(self, feed: str) -> dict | None:
        rows = self.runs(feed, 1)
        return rows[0] if rows else None

    # ----------------------------------------------------------------- audit
    def audit(self, actor: str | None, feed: str | None, action: str, detail: dict[str, Any] | None = None) -> None:
        self.db.execute(
            "INSERT INTO audit(ts, actor, feed, action, detail) VALUES (?,?,?,?,?)",
            (_now(), actor or current_user(), feed, action, json.dumps(detail or {}, default=str)),
        )
        self.db.commit()

    def audit_log(self, limit: int = 50, feed: str | None = None) -> list[dict]:
        if feed:
            return self._query("SELECT * FROM audit WHERE feed=? ORDER BY id DESC LIMIT ?", (feed, limit))
        return self._query("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))

    # ------------------------------------------------------------- retention
    def prune(self, months: int) -> int:
        """Delete history and audit rows older than ``months``, but always keep each feed's last delivery."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=31 * max(1, months))).isoformat(timespec="seconds")
        keep = [r[0] for r in self.db.execute(
            "SELECT MAX(id) FROM runs WHERE delivered=1 GROUP BY feed").fetchall() if r[0] is not None]
        marks = ",".join("?" * len(keep)) or "NULL"
        n = self.db.execute(f"DELETE FROM runs WHERE started<? AND id NOT IN ({marks})",  # noqa: S608 (placeholders)
                            (cutoff, *keep)).rowcount
        n += self.db.execute("DELETE FROM audit WHERE ts<?", (cutoff,)).rowcount
        self.db.commit()
        return n
