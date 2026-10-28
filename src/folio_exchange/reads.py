"""What FOLIO-Exchange may read from FOLIO, and nothing else.

A recipe names *reads* (``instances``, ``holdings``, ``items``, ``loans``, ``marc``);
it can never name an endpoint. The client only sends requests to paths listed here
(the allowlist), and refuses user and patron paths outright with a clear error
(the denylist), so a recipe cannot reach patron data even by mistake.

Paths, response keys and permission names are the ones folio-chq-extract verified against
the folio-org module descriptors in Oct 2026 (see the CLAUDE.md of cHQ FOLIO Exchange Recipe).
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class Read:
    name: str
    path: str
    key: str            # response key that holds the records
    permission: str     # Eureka maps each permission to a capability
    description: str


READS: dict[str, Read] = {r.name: r for r in (
    Read("instances", "/instance-storage/instances", "instances", "inventory-storage.instances.collection.get",
         "Instance records (titles)"),
    Read("holdings", "/holdings-storage/holdings", "holdingsRecords", "inventory-storage.holdings.collection.get",
         "Holdings records"),
    Read("items", "/item-storage/items", "items", "inventory-storage.items.collection.get", "Item records"),
    Read("loans", "/loan-storage/loans", "loans", "circulation-storage.loans.collection.get",
         "Loans: only item, dates, status and renewal count are kept; the borrower is dropped on arrival"),
    Read("marc", "/source-storage/source-records", "sourceRecords", "source-storage.source-records.collection.get",
         "MARC bibliographic records from Source Record Storage"),
)}

# Reference data: always read in full, to turn ids into names and codes.
# kind -> (path, response key, permission)
REFDATA: dict[str, tuple[str, str, str]] = {
    "locations": ("/locations", "locations", "inventory-storage.locations.collection.get"),
    "libraries": ("/location-units/libraries", "loclibs", "inventory-storage.location-units.libraries.collection.get"),
    "campuses": ("/location-units/campuses", "loccamps", "inventory-storage.location-units.campuses.collection.get"),
    "institutions": ("/location-units/institutions", "locinsts",
                     "inventory-storage.location-units.institutions.collection.get"),
    "material_types": ("/material-types", "mtypes", "inventory-storage.material-types.collection.get"),
    "loan_types": ("/loan-types", "loantypes", "inventory-storage.loan-types.collection.get"),
    "call_number_types": ("/call-number-types", "callNumberTypes", "inventory-storage.call-number-types.collection.get"),
    "item_note_types": ("/item-note-types", "itemNoteTypes", "inventory-storage.item-note-types.collection.get"),
    "identifier_types": ("/identifier-types", "identifierTypes", "inventory-storage.identifier-types.collection.get"),
    "classification_types": ("/classification-types", "classificationTypes",
                             "inventory-storage.classification-types.collection.get"),
}

ENTITY_ORDER = ("instances", "holdings", "items", "loans")

LOGIN_PATHS = ("/authn/login-with-expiry", "/authn/login")
# The only POSTs the client sends: logging in, and fetching MARC records by instance id (a read).
POST_PATHS = frozenset(LOGIN_PATHS + (READS["marc"].path,))

# Every path the client may call.
ALLOWED_PATHS = frozenset(
    [r.path for r in READS.values()] + [path for path, _key, _perm in REFDATA.values()] + list(LOGIN_PATHS)
)

# Modules and paths that hold or embed user and patron data. Checked before the
# allowlist so the operator gets a clear "patron data" refusal. Matched on whole
# leading path segments, case-insensitively.
PATRON_PATH_PREFIXES: tuple[str, ...] = (
    "/users", "/users-keycloak", "/bl-users", "/groups-users", "/user-import", "/user-tenants",
    "/perms/users", "/roles/users", "/proxiesfor", "/patron", "/patron-pin", "/patron-block-conditions",
    "/automated-patron-blocks", "/manualblocks", "/request-storage", "/request-preference-storage",
    "/circulation", "/circulation-bff", "/loan-storage/loan-history", "/check-in-storage",
    "/patron-action-session-storage", "/scheduled-notice-storage", "/actual-cost-record-storage",
    "/accounts", "/feefineactions", "/feefines/accounts", "/refunds", "/transfers", "/waives",
    "/service-points-users", "/authn/credentials", "/authn/credentials-existence", "/authn/password",
    "/notes", "/note-links", "/email", "/sms", "/notify", "/message-delivery", "/audit-data",
    "/data-export-spring", "/dcb", "/consortia",
)


def _segments(path: str) -> list[str]:
    return [s for s in path.split("?", 1)[0].lower().split("/") if s]


def is_patron_path(path: str) -> bool:
    """True if ``path`` starts with a user or patron path (whole segments, any case)."""
    segs = _segments(path)
    for prefix in PATRON_PATH_PREFIXES:
        want = _segments(prefix)
        if segs[: len(want)] == want:
            return True
    return False


def is_allowed_path(path: str) -> bool:
    """True if the client may call ``path`` (an exact registry path, ignoring a query string)."""
    clean = "/" + "/".join(_segments(path))
    return clean in ALLOWED_PATHS


def permissions_for(reads: Iterable[str]) -> list[str]:
    """Every permission the given reads need, reference data included, sorted."""
    perms = {READS[r].permission for r in reads if r in READS}
    perms |= {perm for _path, _key, perm in REFDATA.values()}
    return sorted(perms)
