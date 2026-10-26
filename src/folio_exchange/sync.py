"""Pull records from FOLIO into the local cache (full or incremental)."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .cache import Cache
from .client import FolioClient, FolioError
from .dates import now_utc, parse, to_folio

log = logging.getLogger(__name__)

# kind -> (path, response key)
REFDATA = {
    "locations": ("/locations", "locations"),
    "libraries": ("/location-units/libraries", "loclibs"),
    "campuses": ("/location-units/campuses", "loccamps"),
    "institutions": ("/location-units/institutions", "locinsts"),
    "material_types": ("/material-types", "mtypes"),
    "loan_types": ("/loan-types", "loantypes"),
    "call_number_types": ("/call-number-types", "callNumberTypes"),
    "item_note_types": ("/item-note-types", "itemNoteTypes"),
    "identifier_types": ("/identifier-types", "identifierTypes"),
    "classification_types": ("/classification-types", "classificationTypes"),
}

# entity -> (path, response key, cache table)
ENTITIES = {
    "instances": ("/instance-storage/instances", "instances"),
    "holdings": ("/holdings-storage/holdings", "holdingsRecords"),
    "items": ("/item-storage/items", "items"),
    "loans": ("/loan-storage/loans", "loans"),
}

MARC_SOURCES = {"MARC", "CONSORTIUM-MARC"}
OVERLAP = timedelta(minutes=10)


@dataclass
class SyncReport:
    mode: dict[str, str] = field(default_factory=dict)
    fetched: dict[str, int] = field(default_factory=dict)
    removed: dict[str, int] = field(default_factory=dict)
    marc_fetched: int = 0
    marc_missing: int = 0
    seconds: float = 0.0


class Syncer:
    def __init__(self, client: FolioClient, cache: Cache, cfg: dict) -> None:
        self.client = client
        self.cache = cache
        self.cfg = cfg
        self.page_size = int(cfg["folio"]["page_size"])
        self.srs_batch = int(cfg["folio"]["srs_batch_size"])
        self.report = SyncReport()

    # -------------------------------------------------------------- planning
    def choose_mode(self, entity: str, force_full: bool) -> tuple[str, datetime | None]:
        requested = self.cfg["extract"]["mode"]
        last_full = parse(self.cache.get_meta(f"{entity}.last_full"))
        last_sync = parse(self.cache.get_meta(f"{entity}.last_sync"))
        incomplete = self.cache.get_meta(f"{entity}.incomplete") == "1"
        if force_full or requested == "full" or incomplete or last_sync is None:
            return "full", None
        if requested == "auto":
            days = int(self.cfg["extract"]["full_refresh_days"] or 0)
            if days and (last_full is None or now_utc() - last_full > timedelta(days=days)):
                return "full", None
        return "delta", last_sync - OVERLAP

    # ------------------------------------------------------------------- run
    def run(self, force_full: bool = False) -> SyncReport:
        t0 = time.monotonic()
        self.sync_refdata()
        for entity in ENTITIES:
            mode, since = self.choose_mode(entity, force_full)
            self.sync_entity(entity, mode, since)
        self.report.seconds = round(time.monotonic() - t0, 1)
        return self.report

    def sync_refdata(self) -> None:
        for kind, (path, key) in REFDATA.items():
            try:
                recs = self.client.get_all(path, key)
            except FolioError as exc:
                log.warning("Could not load %s (%s); names for these will be blank", kind, exc)
                continue
            self.cache.replace_refdata(kind, recs)
            log.info("Reference data: %d %s", len(recs), kind)

    def sync_entity(self, entity: str, mode: str, since: datetime | None) -> None:
        path, key = ENTITIES[entity]
        started = now_utc()
        gen = int(self.cache.get_meta("gen", "0") or 0) + 1
        self.cache.set_meta("gen", str(gen))
        query = None if mode == "full" else f'metadata.updatedDate>="{to_folio(since)}"'
        log.info("Syncing %s (%s%s)", entity, mode, f" since {since:%Y-%m-%d %H:%M}Z" if since else "")
        if mode == "full":
            self.cache.set_meta(f"{entity}.incomplete", "1")

        n = 0
        for page in self.client.iter_pages(path, key, query, self.page_size):
            self._store(entity, page, gen)
            self.cache.commit()
            n += len(page)
            if n % (self.page_size * 20) == 0:
                log.info("  %s: %d fetched", entity, n)
        self.report.fetched[entity] = n

        removed = 0
        if mode == "full":
            removed = self.cache.delete_stale(entity, gen)
            self.cache.set_meta(f"{entity}.last_full", started.isoformat())
            self.cache.set_meta(f"{entity}.incomplete", "0")
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
            remote = self.client.count(path)
            local = self.cache.count(entity)
            if local > remote:
                log.info("  %s: local %d != FOLIO %d (deletions); running full resync", entity, local, remote)
                self.sync_entity(entity, "full", None)
                return
        self.report.removed[entity] = removed

    # --------------------------------------------------------------- storage
    def _store(self, entity: str, page: list[dict], gen: int) -> None:
        if entity == "instances":
            self.cache.upsert_instances(page, gen)
            self._fetch_marc([r["id"] for r in page if r.get("source") in MARC_SOURCES])
        elif entity == "holdings":
            self.cache.upsert_holdings(page, gen)
        elif entity == "items":
            self.cache.upsert_items(page, gen)
        elif entity == "loans":
            self.cache.upsert_loans(page, gen)

    def _fetch_marc(self, instance_ids: list[str]) -> None:
        params = {"idType": "INSTANCE", "recordType": "MARC_BIB"}
        if self.cfg["folio"].get("srs_include_shared"):
            params["includeShared"] = "true"
        for i in range(0, len(instance_ids), self.srs_batch):
            batch = instance_ids[i : i + self.srs_batch]
            data = self.client.post("/source-storage/source-records", batch, params=params) or {}
            found: dict[str, dict] = {}
            for sr in data.get("sourceRecords", []):
                iid = (sr.get("externalIdsHolder") or {}).get("instanceId")
                content = (sr.get("parsedRecord") or {}).get("content")
                if iid and content and not sr.get("deleted"):
                    found[iid] = content
            self.cache.set_marc((iid, found.get(iid)) for iid in batch)
            self.report.marc_fetched += len(found)
            self.report.marc_missing += len(batch) - len(found)
