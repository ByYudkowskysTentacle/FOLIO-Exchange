"""The field registry (MAP-1) and the row builder: every item-level field an output can use."""
from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import Any

from .dates import latest, norm

# Every item row joins an instance, its holdings and the item itself.
ITEM = frozenset({"instances", "holdings", "items"})
LOANS = ITEM | {"loans"}


@dataclass(frozen=True)
class FieldDef:
    name: str
    description: str
    needs: frozenset[str] = ITEM    # reads (see reads.py) a recipe must declare to use this field
    date: bool = False


def _f(name: str, description: str, *, needs: frozenset[str] = ITEM, date: bool = False) -> FieldDef:
    return FieldDef(name, description, needs, date)


# The field registry: every output field, defined once. Shown by `folio-exchange fields`.
FIELDS: dict[str, FieldDef] = {f.name: f for f in (
    _f("instance_id", "FOLIO instance UUID (also in MARC 999 ff $i)"),
    _f("instance_hrid", "Instance HRID; equals MARC 001"),
    _f("title", "Instance title"),
    _f("holdings_hrid", "Holdings record HRID"),
    _f("item_id", "FOLIO item UUID"),
    _f("item_hrid", "Item HRID"),
    _f("item_identifier", "Item identifier (item.itemIdentifier, often a legacy system id)"),
    _f("barcode", "Item barcode"),
    _f("material_type", "Material type name"),
    _f("loan_type", "Permanent loan type name"),
    _f("status", "Item status (Available, Checked out, Missing, ...)"),
    _f("status_date", "Date the item status last changed", date=True),
    _f("due_date", "Due date of the open loan, if checked out", needs=LOANS, date=True),
    _f("last_circ_date", "Last checkout date (or latest circ activity, see options.last_circ_date_from)",
       needs=LOANS, date=True),
    _f("last_checkout_date", "Most recent loan start date recorded in FOLIO (or legacy value)", needs=LOANS, date=True),
    _f("last_checkin_date", "Most recent check-in (item.lastCheckIn)", date=True),
    _f("last_return_date", "Most recent loan return date recorded in FOLIO", needs=LOANS, date=True),
    _f("loans_total", "Checkouts recorded in FOLIO plus legacy count, if configured", needs=LOANS),
    _f("loans_folio", "Checkouts recorded in FOLIO only", needs=LOANS),
    _f("loans_window", "Checkouts in the last N months (options.loan_window_months)", needs=LOANS),
    _f("renewals_total", "Renewals across all FOLIO loans", needs=LOANS),
    _f("legacy_circ_count", "Circulation count carried over from the previous system"),
    _f("created_date", "Item created date (legacy value if configured, else FOLIO record creation)", date=True),
    _f("location_code", "Effective location code"),
    _f("location_name", "Effective location name"),
    _f("location_name_code", "Effective location as 'Name [code]'"),
    _f("library_code", "Library code of the effective location"),
    _f("library_name", "Library name of the effective location"),
    _f("campus_code", "Campus code"),
    _f("campus_name", "Campus name"),
    _f("institution_code", "Institution code"),
    _f("institution_name", "Institution name"),
    _f("permanent_location_code", "Permanent (home) location code"),
    _f("call_number", "Effective call number with prefix and suffix"),
    _f("call_number_bare", "Effective call number without prefix/suffix"),
    _f("item_level_call_number", "Call number entered on the item record only (blank if it comes from holdings)"),
    _f("call_number_full", "Call number plus volume, enumeration, chronology and copy"),
    _f("call_number_prefix", "Call number prefix"),
    _f("call_number_suffix", "Call number suffix"),
    _f("call_number_type", "Call number type (e.g. Library of Congress classification)"),
    _f("volume", "Volume"),
    _f("enumeration", "Enumeration"),
    _f("chronology", "Chronology"),
    _f("copy_number", "Copy number"),
    _f("number_of_pieces", "Number of pieces"),
    _f("item_suppressed", "Item suppressed from discovery (Y/N)"),
    _f("instance_suppressed", "Instance suppressed (Y/N)"),
)}

ITEM_FIELDS: dict[str, str] = {name: f.description for name, f in FIELDS.items()}
DATE_FIELDS = {name for name, f in FIELDS.items() if f.date}


def suggest(name: str) -> str | None:
    """The closest registered field name, for "did you mean" messages."""
    match = difflib.get_close_matches(str(name), list(FIELDS), n=1, cutoff=0.6)
    return match[0] if match else None


@dataclass
class Ref:
    locations: dict
    libraries: dict
    campuses: dict
    institutions: dict
    material_types: dict
    loan_types: dict
    call_number_types: dict
    item_note_types: dict
    identifier_types: dict
    classification_types: dict

    @classmethod
    def from_cache(cls, cache) -> Ref:
        return cls(**{k: cache.refdata(k) for k in cls.__dataclass_fields__})

    def note_type_id(self, name: str | None) -> str | None:
        if not name:
            return None
        for i, t in self.item_note_types.items():
            if t.get("name", "").strip().lower() == name.strip().lower():
                return i
        raise ValueError(f"Item note type {name!r} not found in FOLIO (check the legacy: settings)")


def _name(table: dict, id_: str | None, attr: str = "name") -> str:
    return (table.get(id_) or {}).get(attr, "") if id_ else ""


def _join(*parts: Any) -> str:
    return " ".join(str(p).strip() for p in parts if p and str(p).strip())


def _note(item: dict, type_id: str | None) -> str | None:
    if not type_id:
        return None
    for n in item.get("notes") or []:
        if n.get("itemNoteTypeId") == type_id and n.get("note"):
            return str(n["note"]).strip()
    return None


def _int(value: str | None) -> int:
    try:
        return int(float(str(value).replace(",", "").strip()))
    except (TypeError, ValueError):
        return 0


class RowBuilder:
    def __init__(self, ref: Ref, options: dict, legacy: dict | None) -> None:
        self.ref = ref
        self.any_activity = options.get("last_circ_date_from", "checkout") == "any"
        from zoneinfo import ZoneInfo

        # Legacy values are usually plain dates in local time, e.g. "1998-03-15".
        self.local_tz = ZoneInfo(options.get("timezone") or "UTC")
        leg = legacy or {}
        self.legacy_created = ref.note_type_id(leg.get("created_date_note_type"))
        self.legacy_count = ref.note_type_id(leg.get("circ_count_note_type"))
        self.legacy_last = ref.note_type_id(leg.get("last_circ_date_note_type"))

    def build(self, item: dict, holdings: dict, instance: dict, loans: dict) -> dict:
        ref = self.ref
        loc_id = item.get("effectiveLocationId")
        loc = ref.locations.get(loc_id) or {}
        cn = item.get("effectiveCallNumberComponents") or {}
        status = item.get("status") or {}

        legacy_count = _int(_note(item, self.legacy_count)) if self.legacy_count else 0
        legacy_last = norm(_note(item, self.legacy_last), self.local_tz) if self.legacy_last else None
        legacy_created = norm(_note(item, self.legacy_created), self.local_tz) if self.legacy_created else None

        last_checkout = latest(loans.get("last_loan"), legacy_last)
        last_checkin = norm((item.get("lastCheckIn") or {}).get("dateTime"))
        last_circ = latest(last_checkout, last_checkin, loans.get("last_return")) if self.any_activity else last_checkout
        folio_loans = int(loans.get("total") or 0)

        call_number = _join(cn.get("prefix"), cn.get("callNumber"), cn.get("suffix"))
        location_name = loc.get("discoveryDisplayName") or loc.get("name", "")
        copy = item.get("copyNumber")
        return {
            "instance_id": instance.get("id", ""),
            "instance_hrid": instance.get("hrid", ""),
            "title": instance.get("title", ""),
            "holdings_hrid": holdings.get("hrid", ""),
            "item_id": item.get("id", ""),
            "item_hrid": item.get("hrid", ""),
            "item_identifier": item.get("itemIdentifier") or "",
            "barcode": item.get("barcode") or "",
            "material_type": _name(ref.material_types, item.get("materialTypeId")),
            "loan_type": _name(ref.loan_types, item.get("permanentLoanTypeId")),
            "status": status.get("name", ""),
            "status_date": norm(status.get("date")),
            "due_date": loans.get("open_due") if status.get("name") != "Available" else None,
            "last_circ_date": last_circ,
            "last_checkout_date": last_checkout,
            "last_checkin_date": last_checkin,
            "last_return_date": loans.get("last_return"),
            "loans_total": folio_loans + legacy_count,
            "loans_folio": folio_loans,
            "loans_window": int(loans.get("window") or 0),
            "renewals_total": int(loans.get("renewals") or 0),
            "legacy_circ_count": legacy_count if self.legacy_count else "",
            "created_date": legacy_created or norm((item.get("metadata") or {}).get("createdDate")),
            "location_code": loc.get("code", ""),
            "location_name": location_name,
            "location_name_code": f"{location_name} [{loc['code']}]" if loc.get("code") else location_name,
            "library_code": _name(ref.libraries, loc.get("libraryId"), "code"),
            "library_name": _name(ref.libraries, loc.get("libraryId")),
            "campus_code": _name(ref.campuses, loc.get("campusId"), "code"),
            "campus_name": _name(ref.campuses, loc.get("campusId")),
            "institution_code": _name(ref.institutions, loc.get("institutionId"), "code"),
            "institution_name": _name(ref.institutions, loc.get("institutionId")),
            "permanent_location_code": _name(
                ref.locations, item.get("permanentLocationId") or holdings.get("permanentLocationId"), "code"
            ),
            "call_number": call_number,
            "call_number_bare": cn.get("callNumber", "") or "",
            "item_level_call_number": item.get("itemLevelCallNumber") or "",
            "call_number_full": _join(
                call_number,
                item.get("volume"),
                item.get("enumeration"),
                item.get("chronology"),
                f"c.{copy}" if copy else None,
            ),
            "call_number_prefix": cn.get("prefix", "") or "",
            "call_number_suffix": cn.get("suffix", "") or "",
            "call_number_type": _name(ref.call_number_types, cn.get("typeId")),
            "volume": item.get("volume") or "",
            "enumeration": item.get("enumeration") or "",
            "chronology": item.get("chronology") or "",
            "copy_number": copy or "",
            "number_of_pieces": item.get("numberOfPieces") or "",
            "item_suppressed": "Y" if item.get("discoverySuppress") else "N",
            "instance_suppressed": "Y" if instance.get("discoverySuppress") or instance.get("staffSuppress") else "N",
        }
