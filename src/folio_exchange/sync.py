"""Pull records from FOLIO into the local cache, full or incremental (adapted from folio-chq-extract).

Only the reads named are fetched (see reads.py). All feeds share one cache and sync
state is kept per entity, so a run syncs the union of the reads of every enabled
feed: see ``reads_to_sync``.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from .cache import Cache
from .client import FolioClient, FolioError
from .dates import now_utc, parse, to_folio
from .reads import ENTITY_ORDER, READS, REFDATA

if TYPE_CHECKING:
    from .config import Site
    from .recipe import Feed

log = logging.getLogger(__name__)

MARC_SOURCES = {"MARC", "CONSORTIUM-MARC"}
OVERLAP = timedelta(minutes=10)
# Loan keys that identify a person. They are dropped the moment a page arrives.
LOAN_PATRON_KEYS = ("userId", "proxyUserId", "borrower", "patronGroupIdAtCheckout")


@dataclass
class SyncReport:
    mode: dict[str, str] = field(default_factory=dict)
    fetched: dict[str, int] = field(default_factory=dict)
    removed: dict[str, int] = field(default_factory=dict)
    marc_fetched: int = 0
    marc_missing: int = 0
    seconds: float = 0.0


def reads_to_sync(feeds: list[Feed]) -> set[str]:
    """The union of the reads of every enabled feed. Syncing less could leave another feed's data stale."""
    out: set[str] = set()
    for f in feeds:
        if f.enabled:
            out |= set(f.reads)
    return out


def strip_patron_keys(loan: dict) -> dict:
    for key in LOAN_PATRON_KEYS:
        loan.pop(key, None)
    return loan


class Syncer:
    def __init__(self, client: FolioClient, cache: Cache, site: Site) -> None:
        self.client = client
        self.cache = cache
        self.site = site
        self.page_size = int(site.folio["page_size"])
        self.srs_batch = int(site.folio["srs_batch_size"])
        self.include_shared = bool(site.folio.get("srs_include_shared"))
        self.mode = site.sync["mode"]
        self.full_refresh_days = int(site.sync["full_refresh_days"] or 0)
        self.report = SyncReport()
        self._marc = False

    # -------------------------------------------------------------- planning
    def choose_mode(self, entity: str, force_full: bool) -> tuple[str, datetime | None]:
        last_full = parse(self.cache.get_meta(f"{entity}.last_full"))
        last_sync = parse(self.cache.get_meta(f"{entity}.last_sync"))
        incomplete = self.cache.get_meta(f"{entity}.incomplete") == "1"
        if force_full or self.mode == "full" or incomplete or last_sync is None:
            return "full", None
        if entity == "instances" and self._marc and self.cache.get_meta("instances.marc") != "1":
            # MARC was not fetched last time (no feed read it); a delta would leave older bibs without it.
            return "full", None
        if self.mode == "auto" and self.full_refresh_days and (
            last_full is None or now_utc() - last_full > timedelta(days=self.full_refresh_days)
        ):
            return "full", None
        return "delta", last_sync - OVERLAP

    # ------------------------------------------------------------------- run
    def run(self, reads: Iterable[str], force_full: bool = False) -> SyncReport:
        wanted = set(reads)
        unknown = wanted - set(READS)
        if unknown:
            raise FolioError(f"Unknown reads: {', '.join(sorted(unknown))}")
        self._marc = "marc" in wanted
        t0 = time.monotonic()
        self.sync_refdata()
        for entity in ENTITY_ORDER:
            if entity in wanted:
                mode, since = self.choose_mode(entity, force_full)
                self.sync_entity(entity, mode, since)
        if self._marc:
            missing = self.cache.marc_source_without_marc()
            if missing:
                log.info("Retrying MARC for %d MARC-source instances that had none", len(missing))
                self._fetch_marc(missing)
                self.cache.commit()
        self.report.seconds = round(time.monotonic() - t0, 1)
        return self.report

    def sync_refdata(self) -> None:
        for kind, (path, key, _perm) in REFDATA.items():
            try:
                recs = self.client.get_all(path, key)
            except FolioError as exc:
                if not self.cache.refdata(kind):
                    raise FolioError(f"Could not load {kind} from FOLIO ({exc}). Their names and codes go into "
                                     "the files, so nothing was built.") from None
                log.warning("Could not load %s (%s); using the copy from the last sync", kind, exc)
                continue
            self.cache.replace_refdata(kind, recs)
            log.info("Reference data: %d %s", len(recs), kind)

    def sync_entity(self, entity: str, mode: str, since: datetime | None) -> None:
        read = READS[entity]
        started = now_utc()
        gen = int(self.cache.get_meta("gen", "0") or 0) + 1
        self.cache.set_meta("gen", str(gen))
        query = None if mode == "full" else f'metadata.updatedDate>="{to_folio(since)}"'
        log.info("Syncing %s (%s%s)", entity, mode, f" since {since:%Y-%m-%d %H:%M}Z" if since else "")
        if mode == "full":
            self.cache.set_meta(f"{entity}.incomplete", "1")

        n = 0
        for page in self.client.iter_pages(read.path, read.key, query, self.page_size):
            self._store(entity, page, gen)
            self.cache.commit()
            n += len(page)
            if n % (self.page_size * 20) == 0:
                log.info("  %s: %d fetched", entity, n)
        self.report.fetched[entity] = n

        removed = 0
        if mode == "full":
            remote = self.client.count(read.path)
            if n < remote - max(5, remote // 1000):
                raise FolioError(
                    f"Fetched {n:,} {entity} but FOLIO reports {remote:,}; the download stopped early. Nothing "
                    "was removed from the cache. Try again; if it repeats, lower folio.page_size."
                )
            removed = self.cache.delete_stale(entity, gen)
            self.cache.set_meta(f"{entity}.last_full", started.isoformat())
            self.cache.set_meta(f"{entity}.incomplete", "0")
            if entity == "instances":
                self.cache.set_meta("instances.marc", "1" if self._marc else "0")
        elif entity == "instances" and not self._marc:
            self.cache.set_meta("instances.marc", "0")
        self.cache.set_meta(f"{entity}.last_sync", started.isoformat())
        self.cache.commit()
        self.report.mode[entity] = mode
        log.info("  %s: %d fetched, %d removed (%s)", entity, n, removed, mode)

        if mode == "delta":
            # Deleted records never show up in a delta query. If the cache now holds
            # more records than FOLIO, something was deleted: fall back to a full
            # resync. (Fewer locally just means records were created mid-run; the
            # next delta picks them up. Mixed adds+deletes are caught by the
            # periodic full refresh.)
            remote = self.client.count(read.path)
            local = self.cache.count(entity)
            if local > remote or local < remote - max(5, remote // 1000):
                log.info("  %s: local %d != FOLIO %d; running full resync", entity, local, remote)
                self.sync_entity(entity, "full", None)
                return
        self.report.removed[entity] = removed

    # --------------------------------------------------------------- preview
    def sample(self, reads: Iterable[str], instances: int) -> SyncReport:
        """Fetch the first ``instances`` instances by id that have items, with their holdings, items, loans
        and MARC, into this cache (a temporary one, for previews). Nothing else is read."""
        wanted = set(reads)
        self._marc = "marc" in wanted
        t0 = time.monotonic()
        self.sync_refdata()
        gen = 1
        inst_read, hold_read, item_read = READS["instances"], READS["holdings"], READS["items"]
        insts: list[dict] = []
        last_id: str | None = None
        # Read a few pages of instances and keep those with holdings, so the sample has item rows.
        for _ in range(5):
            cql = (f'id>"{last_id}" sortBy id' if last_id else "cql.allRecords=1 sortBy id")
            page = (self.client.get(inst_read.path, {"query": cql, "limit": max(instances * 4, 20),
                                                     "totalRecords": "none"}) or {}).get(inst_read.key, [])
            if not page:
                break
            last_id = page[-1]["id"]
            ids = [r["id"] for r in page]
            with_holdings = {h.get("instanceId") for h in
                             self.client.iter_where_in(hold_read.path, hold_read.key, "instanceId", ids)}
            insts += [r for r in page if r["id"] in with_holdings]
            if len(insts) >= instances:
                break
        insts = insts[:instances]
        self._store("instances", insts, gen)
        holdings = list(self.client.iter_where_in(hold_read.path, hold_read.key, "instanceId",
                                                  [r["id"] for r in insts]))
        self._store("holdings", holdings, gen)
        items: list[dict] = []
        if "items" in wanted:
            items = list(self.client.iter_where_in(item_read.path, item_read.key, "holdingsRecordId",
                                                   [h["id"] for h in holdings]))
            self._store("items", items, gen)
        if "loans" in wanted and items:
            loans_read = READS["loans"]
            loans = list(self.client.iter_where_in(loans_read.path, loans_read.key, "itemId",
                                                   [i["id"] for i in items]))
            self._store("loans", loans, gen)
            self.report.fetched["loans"] = len(loans)
        self.cache.commit()
        self.report.fetched.update({"instances": len(insts), "holdings": len(holdings), "items": len(items)})
        self.report.mode = {k: "sample" for k in self.report.fetched}
        self.report.seconds = round(time.monotonic() - t0, 1)
        return self.report

    # --------------------------------------------------------------- storage
    def _store(self, entity: str, page: list[dict], gen: int) -> None:
        if entity == "instances":
            self.cache.upsert_instances(page, gen)
            if self._marc:
                self._fetch_marc([r["id"] for r in page if r.get("source") in MARC_SOURCES])
        elif entity == "holdings":
            self.cache.upsert_holdings(page, gen)
        elif entity == "items":
            self.cache.upsert_items(page, gen)
        elif entity == "loans":
            self.cache.upsert_loans([strip_patron_keys(r) for r in page], gen)

    def _fetch_marc(self, instance_ids: list[str]) -> None:
        params = {"idType": "INSTANCE", "recordType": "MARC_BIB"}
        if self.include_shared:
            params["includeShared"] = "true"
        for i in range(0, len(instance_ids), self.srs_batch):
            batch = instance_ids[i : i + self.srs_batch]
            data = self.client.post(READS["marc"].path, batch, params=params) or {}
            found: dict[str, dict] = {}
            for sr in data.get("sourceRecords", []):
                iid = (sr.get("externalIdsHolder") or {}).get("instanceId")
                content = (sr.get("parsedRecord") or {}).get("content")
                if iid and content and not sr.get("deleted"):
                    found[iid] = content
            self.cache.set_marc((iid, found.get(iid)) for iid in batch)
            self.report.marc_fetched += len(found)
            self.report.marc_missing += len(batch) - len(found)
