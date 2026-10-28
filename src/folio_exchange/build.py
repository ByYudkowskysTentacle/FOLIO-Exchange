"""Write a feed's output files from the local cache in one streaming pass (adapted from folio-chq-extract).

Selection filters apply once, before any output sees a row, so the MARC file and the
delimited files always describe the same items.
"""
from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from dateutil.relativedelta import relativedelta

from .cache import Cache, unpack
from .dates import FMT, fmt, now_utc
from .delimited import DelimitedWriter
from .fields import DATE_FIELDS, Ref, RowBuilder
from .marc import MarcBuilder, from_instance, from_srs
from .sync import MARC_SOURCES

if TYPE_CHECKING:
    from .recipe import Feed

log = logging.getLogger(__name__)


@dataclass
class BuildReport:
    files: list[str] = field(default_factory=list)
    outputs: dict[str, list[str]] = field(default_factory=dict)
    bibs_written: int = 0
    bibs_from_srs: int = 0
    bibs_generated: int = 0
    bibs_without_items_skipped: int = 0
    items_written: int = 0
    items_skipped: Counter = field(default_factory=Counter)
    items_without_barcode: int = 0
    duplicate_barcodes: int = 0
    unfilled: Counter = field(default_factory=Counter)
    marc_001_corrected: int = 0
    marc_999_corrected: int = 0
    marc_items_truncated: int = 0
    marc_oversize_skipped: int = 0
    marc_long_fields_truncated: int = 0
    bibs_marc_missing: int = 0          # MARC-source instances with no SRS record: sent as brief records
    bibs_marc_missing_consortium: int = 0
    marc_unreadable: int = 0            # SRS records that could not be read or written: sent as brief records
    marc_problem_hrids: list[str] = field(default_factory=list)
    # Legacy notes present but unusable, per legacy setting: {"count": n, "examples": [item HRIDs]}
    legacy_unparsed: dict[str, dict] = field(default_factory=dict)


_LEGACY_FALLBACK = {
    "created_date_note_type": ("a date", "carry FOLIO's record-creation date instead"),
    "last_circ_date_note_type": ("a date", "carry only FOLIO's last checkout"),
    "circ_count_note_type": ("a whole number", "count FOLIO's loans only"),
}


def legacy_warnings(rep: BuildReport, legacy: dict) -> list[str]:
    """Plain warnings for legacy notes that were present but could not be read."""
    pattern = legacy.get("date_format")
    date_hint = ("Dates need a day, month and year from 1800 to today, as 1998-03-15 or "
                 + (f"in legacy.date_format ({pattern})." if pattern else
                    "3/15/1998; set legacy.date_format if they are written another way."))
    out = []
    for key, entry in rep.legacy_unparsed.items():
        what, fallback = _LEGACY_FALLBACK[key]
        hint = date_hint if what == "a date" else "Counts need one whole number, e.g. 12 or Checkouts: 1,234."
        out.append(f"{entry['count']:,} '{legacy.get(key)}' notes (legacy.{key}) could not be read as {what}, so "
                   f"those items {fallback} (for example {', '.join(entry['examples'])}). {hint}")
    return out


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


def file_base(template: str, prefix: str, stamp: str) -> str:
    return template.replace("{prefix}", prefix).replace("{date}", stamp)


def build(cache: Cache, feed: Feed, run_dir: Path, *, limit_bibs: int | None = None,
          stamp: str | None = None, name_prefix: str = "") -> BuildReport:
    """Write every output of ``feed`` into ``run_dir``. ``limit_bibs`` stops early and ``name_prefix`` is put
    before every file name (previews)."""
    run_dir.mkdir(parents=True, exist_ok=True)
    sel, opts = feed.selection, feed.options
    stamp = stamp or now_utc().strftime("%Y%m%d")
    tz = ZoneInfo(opts.get("timezone") or "UTC")
    rep = BuildReport()

    ref = Ref.from_cache(cache)
    rows_builder = RowBuilder(ref, opts, feed.legacy)

    marc_out = next((o for o in feed.outputs if o["format"] == "marc21"), None)
    marc_sink: _MarcSink | None = None
    marc_builder: MarcBuilder | None = None
    if marc_out:
        mfmt = marc_out["date_format"]
        marc_builder = MarcBuilder(marc_out, lambda name, v: fmt(v, mfmt, tz) if name in DATE_FIELDS else v)
        marc_sink = _MarcSink(run_dir, name_prefix + file_base(marc_out["file_name"], feed.file_prefix, stamp),
                              int(marc_out["split_every"] or 0))
    writers: list[tuple[dict, DelimitedWriter]] = []
    must_fill = list(feed.checks.get("must_be_filled") or [])

    window_start = (now_utc() - relativedelta(months=int(opts["loan_window_months"]))).strftime(FMT)
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
    if "loans" not in feed.reads:
        db.execute("DELETE FROM temp.loanstats")  # the feed may not use loans, even if another feed synced them

    loc_codes = {k: v.get("code") for k, v in ref.locations.items()}
    include_locs = set(sel["include_location_codes"] or [])
    exclude_locs = set(sel["exclude_location_codes"] or [])
    exclude_status = {s.lower() for s in sel["exclude_item_statuses"] or []}
    keep_suppressed_inst = bool(sel["include_suppressed_instances"])
    keep_suppressed_item = bool(sel["include_suppressed_items"])
    keep_no_barcode = bool(sel["include_items_without_barcode"])
    keep_bibs_without_items = bool(sel["bibs_without_items"])
    keep_suppressed_holdings = bool(sel.get("include_suppressed_holdings", True))
    seen_barcodes: set[str] = set()
    flushed = 0

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

    def note_problem(inst: dict) -> None:
        if len(rep.marc_problem_hrids) < 10:
            rep.marc_problem_hrids.append(str(inst.get("hrid") or inst.get("id")))

    def flush(inst_row, rows: list[dict]) -> None:
        nonlocal flushed
        if inst_row is None:
            return
        _iid, inst_blob, marc_blob = inst_row
        if not rows and not keep_bibs_without_items:
            rep.bibs_without_items_skipped += 1
            return
        flushed += 1
        if marc_builder is not None:
            inst = unpack(inst_blob)
            content = unpack(marc_blob) if "marc" in feed.reads else None
            rec = from_srs(content) if content else None
            data = None
            unreadable = bool(content) and rec is None
            if rec is not None:
                try:
                    data = marc_builder.finalize(rec, inst, rows)
                    rep.bibs_from_srs += 1
                except Exception as exc:  # noqa: BLE001 - one odd record must not stop the whole file
                    log.warning("The MARC record of %s (%s) could not be written (%s: %s); a brief record is "
                                "sent instead", inst.get("hrid"), inst.get("id"), type(exc).__name__, exc)
                    rec, unreadable = None, True
            if rec is None:
                if unreadable:
                    rep.marc_unreadable += 1
                    note_problem(inst)
                elif "marc" in feed.reads and inst.get("source") in MARC_SOURCES:
                    rep.bibs_marc_missing += 1
                    if inst.get("source") == "CONSORTIUM-MARC":
                        rep.bibs_marc_missing_consortium += 1
                    note_problem(inst)
                rec = from_instance(inst, ref)
                rep.bibs_generated += 1
                data = marc_builder.finalize(rec, inst, rows)
            if data is not None:
                marc_sink.write(data)
                rep.bibs_written += 1
        else:
            rep.bibs_written += 1
        for r in rows:
            for _out, w in writers:
                w.write(r)
            for name in must_fill:
                if r.get(name) in (None, ""):
                    rep.unfilled[name] += 1
            rep.items_written += 1

    query = """
        SELECT i.id, i.data, i.marc, i.suppressed, h.data, it.data,
               ls.last_loan, ls.last_return, ls.total, ls.win, ls.renewals, ls.open_due
        FROM instances i
        LEFT JOIN holdings h ON h.instance_id = i.id
        LEFT JOIN items it ON it.holdings_id = h.id
        LEFT JOIN loanstats ls ON ls.item_id = it.id
        ORDER BY i.id, h.id, it.id"""

    try:
        for o in feed.outputs:
            if o["format"] == "delimited":
                path = run_dir / f"{name_prefix}{file_base(o['file_name'], feed.file_prefix, stamp)}.{o['extension']}"
                writers.append((o, DelimitedWriter(o, path, tz)))
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
                if limit_bibs is not None and flushed >= limit_bibs:
                    current_inst = None
                    break
                current_id, current_inst, rows = iid, (iid, inst_blob, marc_blob), []
                current_inst_json = None
                skip_instance = bool(suppressed) and not keep_suppressed_inst
            if skip_instance or it_blob is None:
                if skip_instance and it_blob is not None:
                    rep.items_skipped["instance suppressed"] += 1
                continue
            item = unpack(it_blob)
            reason = item_ok(item)
            holdings = unpack(h_blob) or {}
            if not reason and not keep_suppressed_holdings and holdings.get("discoverySuppress"):
                reason = "holdings suppressed"
            if reason:
                rep.items_skipped[reason] += 1
                continue
            if current_inst_json is None:
                current_inst_json = unpack(inst_blob)
            loans = {"last_loan": last_loan, "last_return": last_return, "total": total,
                     "window": win, "renewals": renewals, "open_due": open_due}
            row = rows_builder.build(item, holdings, current_inst_json, loans)
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
    finally:
        for _o, w in writers:
            w.close()
        if marc_sink is not None:
            marc_sink.close()

    if marc_out and marc_sink is not None:
        rep.outputs[marc_out["name"]] = [p.name for p in marc_sink.paths]
    for o, w in writers:
        rep.outputs[o["name"]] = [w.path.name]
    rep.files = [name for o in feed.outputs for name in rep.outputs.get(o["name"], [])]
    rep.legacy_unparsed = rows_builder.unparsed
    if marc_builder is not None:
        rep.marc_001_corrected = marc_builder.fixed_001
        rep.marc_999_corrected = marc_builder.fixed_999
        rep.marc_items_truncated = marc_builder.truncated
        rep.marc_oversize_skipped = marc_builder.oversize
        rep.marc_long_fields_truncated = marc_builder.long_fields
    log.info(
        "Built %d bib records (%d from SRS, %d generated) and %d item rows",
        rep.bibs_written, rep.bibs_from_srs, rep.bibs_generated, rep.items_written,
    )
    if rep.duplicate_barcodes:
        log.warning("%d items share a barcode with another item", rep.duplicate_barcodes)
    return rep
