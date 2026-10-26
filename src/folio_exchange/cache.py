"""Local SQLite cache of the FOLIO records needed for the extract.

Keeping a local copy means routine runs only fetch what changed since the
previous run, and output files can be rebuilt without touching FOLIO.
"""
from __future__ import annotations

import json
import sqlite3
import zlib
from collections.abc import Iterable
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS refdata (kind TEXT, id TEXT, data TEXT, PRIMARY KEY (kind, id));
CREATE TABLE IF NOT EXISTS instances (
    id TEXT PRIMARY KEY, hrid TEXT, source TEXT, suppressed INTEGER,
    data BLOB, marc BLOB, gen INTEGER);
CREATE TABLE IF NOT EXISTS holdings (
    id TEXT PRIMARY KEY, instance_id TEXT, data BLOB, gen INTEGER);
CREATE INDEX IF NOT EXISTS holdings_instance ON holdings(instance_id);
CREATE TABLE IF NOT EXISTS items (
    id TEXT PRIMARY KEY, holdings_id TEXT, data BLOB, gen INTEGER);
CREATE INDEX IF NOT EXISTS items_holdings ON items(holdings_id);
CREATE TABLE IF NOT EXISTS loans (
    id TEXT PRIMARY KEY, item_id TEXT, loan_date TEXT, return_date TEXT,
    due_date TEXT, status TEXT, renewals INTEGER, gen INTEGER);
CREATE INDEX IF NOT EXISTS loans_item ON loans(item_id);
"""


def pack(obj: Any) -> bytes:
    return zlib.compress(json.dumps(obj, separators=(",", ":")).encode("utf-8"), 6)


def unpack(blob: bytes | None) -> Any:
    if blob is None:
        return None
    return json.loads(zlib.decompress(blob).decode("utf-8"))


class Cache:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    # ------------------------------------------------------------------ meta
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key: str, value: str | None) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))
        self.db.commit()

    # --------------------------------------------------------------- refdata
    def replace_refdata(self, kind: str, records: Iterable[dict]) -> None:
        with self.db:
            self.db.execute("DELETE FROM refdata WHERE kind=?", (kind,))
            self.db.executemany(
                "INSERT INTO refdata(kind, id, data) VALUES (?, ?, ?)",
                ((kind, r["id"], json.dumps(r)) for r in records),
            )

    def refdata(self, kind: str) -> dict[str, dict]:
        return {i: json.loads(d) for i, d in self.db.execute("SELECT id, data FROM refdata WHERE kind=?", (kind,))}

    # --------------------------------------------------------------- records
    def upsert_instances(self, rows: list[dict], gen: int) -> None:
        self.db.executemany(
            """INSERT INTO instances(id, hrid, source, suppressed, data, gen) VALUES (?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET hrid=excluded.hrid, source=excluded.source,
               suppressed=excluded.suppressed, data=excluded.data, gen=excluded.gen,
               marc=CASE WHEN excluded.source IN ('MARC', 'CONSORTIUM-MARC') THEN instances.marc ELSE NULL END""",
            (
                (
                    r["id"],
                    r.get("hrid"),
                    r.get("source"),
                    int(bool(r.get("discoverySuppress") or r.get("staffSuppress"))),
                    pack(r),
                    gen,
                )
                for r in rows
            ),
        )

    def set_marc(self, pairs: Iterable[tuple[str, dict | None]]) -> None:
        self.db.executemany(
            "UPDATE instances SET marc=? WHERE id=?",
            ((pack(content) if content is not None else None, iid) for iid, content in pairs),
        )

    def upsert_holdings(self, rows: list[dict], gen: int) -> None:
        self.db.executemany(
            """INSERT INTO holdings(id, instance_id, data, gen) VALUES (?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET instance_id=excluded.instance_id,
               data=excluded.data, gen=excluded.gen""",
            ((r["id"], r.get("instanceId"), pack(r), gen) for r in rows),
        )

    def upsert_items(self, rows: list[dict], gen: int) -> None:
        self.db.executemany(
            """INSERT INTO items(id, holdings_id, data, gen) VALUES (?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET holdings_id=excluded.holdings_id,
               data=excluded.data, gen=excluded.gen""",
            ((r["id"], r.get("holdingsRecordId"), pack(r), gen) for r in rows),
        )

    def upsert_loans(self, rows: list[dict], gen: int) -> None:
        from .dates import norm

        self.db.executemany(
            """INSERT INTO loans(id, item_id, loan_date, return_date, due_date, status, renewals, gen)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET item_id=excluded.item_id, loan_date=excluded.loan_date,
               return_date=excluded.return_date, due_date=excluded.due_date,
               status=excluded.status, renewals=excluded.renewals, gen=excluded.gen""",
            (
                (
                    r["id"],
                    r.get("itemId"),
                    norm(r.get("loanDate")),
                    norm(r.get("returnDate")),
                    norm(r.get("dueDate")),
                    (r.get("status") or {}).get("name"),
                    int(r.get("renewalCount") or 0),
                    gen,
                )
                for r in rows
            ),
        )

    def delete_stale(self, table: str, gen: int) -> int:
        cur = self.db.execute(f"DELETE FROM {table} WHERE gen IS NOT ?", (gen,))  # noqa: S608 (fixed names)
        return cur.rowcount

    def count(self, table: str) -> int:
        return self.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608

    def commit(self) -> None:
        self.db.commit()
