"""MARC record handling: SRS records, fallback records for FOLIO-source
instances, and embedding item data in a 9XX field."""
from __future__ import annotations

import json
import logging
from collections.abc import Iterable

from pymarc import Field, Indicators, JSONReader, Record, Subfield

from .dates import norm

log = logging.getLogger(__name__)

MAX_RECORD_BYTES = 99999
MAX_FIELD_BYTES = 9999  # ISO 2709 directory allows 4 digits per field length


def _field_bytes(f: Field) -> int:
    if f.control_field:
        return len((f.data or "").encode("utf-8")) + 1
    return 2 + sum(2 + len(s.value.encode("utf-8")) for s in f.subfields) + 1


def _cut(text: str, max_bytes: int) -> str:
    return text.encode("utf-8")[: max(0, max_bytes)].decode("utf-8", errors="ignore")


def shorten_long_fields_one(f: Field) -> int:
    holder = Record()
    holder.fields = [f]
    return shorten_long_fields(holder)


def shorten_long_fields(rec: Record) -> int:
    """Truncate any field too long for ISO 2709 (longest subfield first). Returns fields changed."""
    changed = 0
    for f in rec.fields:
        excess = _field_bytes(f) - MAX_FIELD_BYTES
        if excess <= 0:
            continue
        changed += 1
        if f.control_field:
            f.data = _cut(f.data, len(f.data.encode("utf-8")) - excess)
            continue
        while excess > 0:
            i = max(range(len(f.subfields)), key=lambda k: len(f.subfields[k].value.encode("utf-8")))
            sub = f.subfields[i]
            size = len(sub.value.encode("utf-8"))
            f.subfields[i] = Subfield(sub.code, _cut(sub.value, size - excess))
            excess = _field_bytes(f) - MAX_FIELD_BYTES
    return changed


def from_srs(content: dict) -> Record | None:
    try:
        return next(iter(JSONReader(json.dumps(content))))
    except Exception as exc:  # malformed SRS content: report and skip
        log.warning("Could not parse SRS MARC: %s", exc)
        return None


def _sf(pairs: Iterable[tuple[str, str | None]]) -> list[Subfield]:
    return [Subfield(code=c, value=str(v).strip()) for c, v in pairs if v and str(v).strip()]


def _df(tag: str, i1: str, i2: str, pairs: Iterable[tuple[str, str | None]]) -> Field | None:
    subs = _sf(pairs)
    return Field(tag=tag, indicators=Indicators(i1, i2), subfields=subs) if subs else None


def _text(v) -> str:
    if isinstance(v, dict):
        return v.get("value") or v.get("name") or ""
    return str(v or "")


def from_instance(inst: dict, ref) -> Record:
    """Build a brief MARC record from a FOLIO-source instance (no SRS record)."""
    rec = Record(force_utf8=True, leader="00000nam a2200000uu 4500")
    rec.add_field(Field(tag="001", data=inst.get("hrid") or inst["id"]))

    created = norm((inst.get("metadata") or {}).get("createdDate")) or "1970-01-01T00:00:00"
    year = ""
    for pub in inst.get("publication") or []:
        digits = "".join(ch for ch in str(pub.get("dateOfPublication") or "") if ch.isdigit())
        if len(digits) >= 4:
            year = digits[:4]
            break
    lang = ((inst.get("languages") or ["und"])[0] or "und")[:3].ljust(3)
    f008 = f"{created[2:4]}{created[5:7]}{created[8:10]}s{(year or 'uuuu'):4}    xx {' ' * 17}{lang} d"
    rec.add_field(Field(tag="008", data=f008))

    id_types = {k: (v.get("name") or "").lower() for k, v in ref.identifier_types.items()}
    for ident in inst.get("identifiers") or []:
        kind, val = id_types.get(ident.get("identifierTypeId"), ""), ident.get("value")
        tag = {"isbn": "020", "issn": "022", "lccn": "010"}.get(kind)
        if tag:
            _add(rec, _df(tag, " ", " ", [("a", val)]))
        elif kind in {"oclc", "system control number"}:
            _add(rec, _df("035", " ", " ", [("a", val)]))

    cls_types = {k: (v.get("name") or "").lower() for k, v in ref.classification_types.items()}
    for c in inst.get("classifications") or []:
        kind, num = cls_types.get(c.get("classificationTypeId"), ""), c.get("classificationNumber")
        if kind.startswith("lc") or "library of congress" in kind:
            _add(rec, _df("050", " ", "4", [("a", num)]))
        elif "dewey" in kind:
            _add(rec, _df("082", "0", "4", [("a", num)]))

    contributors = inst.get("contributors") or []
    primary = next((c for c in contributors if c.get("primary")), None)
    if primary:
        _add(rec, _df("100", "1", " ", [("a", primary.get("name"))]))
    title = inst.get("title") or "[No title]"
    _add(rec, _df("245", "1" if primary else "0", "0", [("a", title)]))
    for ed in inst.get("editions") or []:
        _add(rec, _df("250", " ", " ", [("a", _text(ed))]))
    for pub in (inst.get("publication") or [])[:1]:
        _add(rec, _df("264", " ", "1", [("a", pub.get("place")), ("b", pub.get("publisher")), ("c", pub.get("dateOfPublication"))]))
    for pd in inst.get("physicalDescriptions") or []:
        _add(rec, _df("300", " ", " ", [("a", _text(pd))]))
    for s in inst.get("series") or []:
        _add(rec, _df("490", "0", " ", [("a", _text(s))]))
    for c in contributors:
        if c is not primary and c.get("name"):
            _add(rec, _df("700", "1", " ", [("a", c["name"])]))
    _add(rec, _df("999", "f", "f", [("i", inst["id"])]))
    return rec


def _set_instance_link(rec: Record, instance_id: str) -> bool:
    """Make 999 ff $i hold the instance UUID (the link cHQ uses). Returns True if it already did."""
    links = [f for f in rec.get_fields("999") if f.indicator1 == "f" and f.indicator2 == "f"]
    if links and [s.value for s in links[0].subfields if s.code == "i"] == [instance_id]:
        return True
    if not links:
        rec.add_ordered_field(Field(tag="999", indicators=Indicators("f", "f"), subfields=[Subfield("i", instance_id)]))
        return False
    others = [s for s in links[0].subfields if s.code != "i"]
    links[0].subfields = [Subfield("i", instance_id), *others]
    return False


def _add(rec: Record, field: Field | None) -> None:
    if field is not None:
        rec.add_field(field)


class MarcBuilder:
    def __init__(self, cfg: dict, date_fmt) -> None:
        m = cfg["output"]["marc"]
        self.embed = bool(m["embed_items"])
        self.tag = str(m["item_field_tag"])
        self.subfields: dict[str, str] = {str(k): v for k, v in m["item_subfields"].items()}
        self.strip = bool(m["strip_existing_item_field"])
        self.max_items = int(m["max_items_per_record"])
        self.date_fmt = date_fmt
        self.truncated = 0
        self.oversize = 0
        self.fixed_001 = 0
        self.fixed_999 = 0
        self.long_fields = 0

    def finalize(self, rec: Record, instance: dict, rows: list[dict]) -> bytes | None:
        """Ensure 001 = instance HRID and 999 ff $i = instance UUID, embed item fields, return ISO 2709 bytes."""
        hrid = instance.get("hrid")
        if hrid:
            cur = rec.get_fields("001")
            if not cur or cur[0].data != hrid:
                self.fixed_001 += 1
                rec.remove_fields("001")
                rec.add_ordered_field(Field(tag="001", data=hrid))
        if self.strip:
            rec.remove_fields(self.tag)
        if instance.get("id") and not _set_instance_link(rec, instance["id"]):
            self.fixed_999 += 1
        rec.leader = rec.leader[:9] + "a" + rec.leader[10:]  # UTF-8
        rec.force_utf8 = True
        self.long_fields += shorten_long_fields(rec)

        items = rows[: self.max_items] if self.embed else []
        truncated = self.embed and len(rows) > self.max_items
        fields = [self._item_field(r) for r in items]
        self.long_fields += sum(shorten_long_fields_one(f) for f in fields)
        for f in fields:
            rec.add_ordered_field(f)
        data = rec.as_marc()
        while len(data) > MAX_RECORD_BYTES and fields:
            # Too big for ISO 2709: shed embedded items (they stay in the CSV).
            for f in fields[len(fields) // 2 :]:
                rec.remove_field(f)
            fields = fields[: len(fields) // 2]
            truncated = True
            data = rec.as_marc()
        if truncated:
            self.truncated += 1
        if len(data) > MAX_RECORD_BYTES:
            self.oversize += 1
            log.warning("Instance %s exceeds the MARC record size limit; bib skipped (items kept in CSV)", hrid)
            return None
        return data

    def _item_field(self, row: dict) -> Field:
        pairs = []
        for code, name in self.subfields.items():
            val = row.get(name)
            if val in (None, ""):
                continue
            pairs.append((code, self.date_fmt(name, val)))
        return Field(tag=self.tag, indicators=Indicators(" ", " "), subfields=_sf(pairs))
