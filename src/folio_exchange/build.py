"""Generate the cHQ output files (MARC bibs + items CSV) from the local cache."""
from __future__ import annotations

import csv
import logging
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from dateutil.relativedelta import relativedelta

from .cache import Cache, unpack
from .dates import FMT, fmt, now_utc
from .fields import DATE_FIELDS, Ref, RowBuilder
from .marc import MarcBuilder, from_instance, from_srs

log = logging.getLogger(__name__)


@dataclass
class BuildReport:
    files: list[str] = field(default_factory=list)
    bibs_written: int = 0
    bibs_from_srs: int = 0
    bibs_generated: int = 0
    bibs_without_items_skipped: int = 0
    items_written: int = 0
    items_skipped: Counter = field(default_factory=Counter)
    items_without_barcode: int = 0
    duplicate_barcodes: int = 0
    marc_001_corrected: int = 0
    marc_999_corrected: int = 0
    marc_items_truncated: int = 0
    marc_oversize_skipped: int = 0
    marc_long_fields_truncated: int = 0


class _MarcSink:
    def __init__(self, directory: Path, base: str, split_every: int) -> None:
        self.directory, self.base, self.split = directory, base, split_every
        self.part, self.count, self.fh = 0, 0, None
        self.paths: list[Path] = []

    def write(self, data: bytes) -> None:
        if self.fh is None or (self.split and self.count >= self.split):
            self._next()
        self.fh.write(data)
        self.count += 1

    def _next(self) -> None:
        if self.fh:
            self.fh.close()
        self.part += 1
        self.count = 0
        name = f"{self.base}_{self.part:03d}.mrc" if self.split else f"{self.base}.mrc"
        path = self.directory / name
        self.paths.append(path)
        self.fh = open(path, "wb")

    def close(self) -> None:
        if self.fh is None:  # no records at all: still emit an empty file
            self._next()
        self.fh.close()


def build(cache: Cache, cfg: dict, run_dir: Path) -> BuildReport:
    out = cfg["output"]
    ext = cfg["extract"]
    csv_cfg = out["items_csv"]
    run_dir.mkdir(parents=True, exist_ok=True)
    stamp = now_utc().strftime(out["date_stamp_format"])
    prefix = out["file_prefix"]
    tzname = csv_cfg.get("timezone") or "UTC"
    tz = ZoneInfo(tzname)
    date_format = csv_cfg["date_format"]
    null_value = csv_cfg["null_value"]

    def fmt_value(name: str, value):
        if name in DATE_FIELDS:
            return fmt(value, date_format, tz)
        return "" if value is None else value

    def csv_value(name: str, value):
        value = fmt_value(name, value)
        return null_value if value in (None, "") else value

    ref = Ref.from_cache(cache)
    rows_builder = RowBuilder(ref, cfg)
    marc_builder = MarcBuilder(cfg, fmt_value)
    rep = BuildReport()

    window_start = (now_utc() - relativedelta(months=int(ext["loan_window_months"]))).strftime(FMT)
    db = cache.db
    db.execute("DROP TABLE IF EXISTS temp.loanstats")
    db.execute(
        """CREATE TEMP TABLE loanstats AS
           SELECT item_id, MAX(loan_date) AS last_loan, MAX(return_date) AS last_return,
                  COUNT(*) AS total, SUM(CASE WHEN loan_date >= ? THEN 1 ELSE 0 END) AS win,
                  SUM(COALESCE(renewals, 0)) AS renewals,
                  MAX(CASE WHEN status = 'Open' THEN due_date END) AS open_due
           FROM loans WHERE item_id IS NOT NULL GROUP BY item_id""",
        (window_start,),
    )
    db.execute("CREATE INDEX temp.loanstats_item ON loanstats(item_id)")

    loc_codes = {k: v.get("code") for k, v in ref.locations.items()}
    include_locs = set(ext["include_location_codes"] or [])
    exclude_locs = set(ext["exclude_location_codes"] or [])
    exclude_status = {s.lower() for s in ext["exclude_item_statuses"] or []}
    keep_suppressed_inst = bool(ext["include_suppressed_instances"])
    keep_suppressed_item = bool(ext["include_suppressed_items"])
    keep_no_barcode = bool(ext["include_items_without_barcode"])
    keep_bibs_without_items = bool(ext["bibs_without_items"])

    columns = csv_cfg["columns"]
    csv_path = run_dir / f"{prefix}_items_{stamp}.{csv_cfg['file_extension']}"
    marc = _MarcSink(run_dir, f"{prefix}_bibs_{stamp}", int(out["marc"]["split_every"] or 0))
    seen_barcodes: set[str] = set()

    def item_ok(item: dict) -> str | None:
        if not keep_suppressed_item and item.get("discoverySuppress"):
            return "suppressed"
        if (item.get("status") or {}).get("name", "").lower() in exclude_status:
            return "excluded status"
        code = loc_codes.get(item.get("effectiveLocationId"))
        if include_locs and code not in include_locs:
            return "location not included"
        if code in exclude_locs:
            return "location excluded"
        if not item.get("barcode") and not keep_no_barcode:
            return "no barcode"
        return None

    def flush(inst_row, rows: list[dict]) -> None:
        if inst_row is None:
            return
        _iid, inst_blob, marc_blob = inst_row
        if not rows and not keep_bibs_without_items:
            rep.bibs_without_items_skipped += 1
            return
        inst = unpack(inst_blob)
        content = unpack(marc_blob)
        rec = from_srs(content) if content else None
        if rec is not None:
            rep.bibs_from_srs += 1
        else:
            rec = from_instance(inst, ref)
            rep.bibs_generated += 1
        data = marc_builder.finalize(rec, inst, rows)
        if data is not None:
            marc.write(data)
            rep.bibs_written += 1
        for r in rows:
            writer.writerow([csv_value(c["field"], r.get(c["field"])) for c in columns])
            rep.items_written += 1

    query = """
        SELECT i.id, i.data, i.marc, i.suppressed, h.data, it.data,
               ls.last_loan, ls.last_return, ls.total, ls.win, ls.renewals, ls.open_due
        FROM instances i
        LEFT JOIN holdings h ON h.instance_id = i.id
        LEFT JOIN items it ON it.holdings_id = h.id
        LEFT JOIN loanstats ls ON ls.item_id = it.id
        ORDER BY i.id, h.id, it.id"""

    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, delimiter=csv_cfg["delimiter"], quoting=csv.QUOTE_MINIMAL)
        writer.writerow([c["header"] for c in columns])
        current_id = None
        current_inst = None
        current_inst_json = None
        rows: list[dict] = []
        skip_instance = False
        for (iid, inst_blob, marc_blob, suppressed, h_blob, it_blob,
             last_loan, last_return, total, win, renewals, open_due) in db.execute(query):
            if iid != current_id:
                if not skip_instance:
                    flush(current_inst, rows)
                current_id, current_inst, rows = iid, (iid, inst_blob, marc_blob), []
                current_inst_json = None
                skip_instance = bool(suppressed) and not keep_suppressed_inst
            if skip_instance or it_blob is None:
                if skip_instance and it_blob is not None:
                    rep.items_skipped["instance suppressed"] += 1
                continue
            item = unpack(it_blob)
            reason = item_ok(item)
            if reason:
                rep.items_skipped[reason] += 1
                continue
            if current_inst_json is None:
                current_inst_json = unpack(inst_blob)
            loans = {"last_loan": last_loan, "last_return": last_return, "total": total,
                     "window": win, "renewals": renewals, "open_due": open_due}
            row = rows_builder.build(item, unpack(h_blob) or {}, current_inst_json, loans)
            bc = row["barcode"]
            if not bc:
                rep.items_without_barcode += 1
            elif bc in seen_barcodes:
                rep.duplicate_barcodes += 1
            else:
                seen_barcodes.add(bc)
            rows.append(row)
        if not skip_instance:
            flush(current_inst, rows)
    marc.close()

    rep.marc_001_corrected = marc_builder.fixed_001
    rep.marc_999_corrected = marc_builder.fixed_999
    rep.marc_items_truncated = marc_builder.truncated
    rep.marc_oversize_skipped = marc_builder.oversize
    rep.marc_long_fields_truncated = marc_builder.long_fields
    rep.files = [p.name for p in marc.paths] + [csv_path.name]
    log.info(
        "Built %d bib records (%d from SRS, %d generated) and %d item rows",
        rep.bibs_written, rep.bibs_from_srs, rep.bibs_generated, rep.items_written,
    )
    if rep.duplicate_barcodes:
        log.warning("%d items share a barcode with another item", rep.duplicate_barcodes)
    return rep

