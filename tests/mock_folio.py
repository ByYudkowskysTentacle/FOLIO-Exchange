"""A small in-process fake FOLIO used by the tests.

It implements just enough of the FOLIO APIs (login, CQL id-keyset paging,
updatedDate filters, SRS lookup by instance id) to exercise the tool end to end.
"""
from __future__ import annotations

import copy
import json
import re
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

U = lambda n: str(uuid.UUID(int=n))  # noqa: E731 - deterministic UUIDs


def meta(created="2024-01-10T10:00:00.000+00:00", updated="2024-01-10T10:00:00.000+00:00"):
    return {"createdDate": created, "updatedDate": updated}


def marc_content(hrid, title, extra_949=False):
    fields = [
        {"001": hrid},
        {"008": "240110s2020    nyu           000 0 eng d"},
        {"020": {"ind1": " ", "ind2": " ", "subfields": [{"a": "9780000000001"}]}},
        {"050": {"ind1": " ", "ind2": "4", "subfields": [{"a": "QA76.73.P98"}, {"b": "L88 2020"}]}},
        {"100": {"ind1": "1", "ind2": " ", "subfields": [{"a": "Lutz, Mark."}]}},
        {"245": {"ind1": "1", "ind2": "0", "subfields": [{"a": title}, {"c": "Mark Lutz."}]}},
    ]
    if extra_949:
        fields.append({"949": {"ind1": " ", "ind2": " ", "subfields": [{"a": "OLD-LOCAL-DATA"}]}})
    fields.append({"999": {"ind1": "f", "ind2": "f", "subfields": [{"i": "x"}]}})
    return {"leader": "01000cam a2200277 i 4500", "fields": fields}


def default_data() -> dict:
    lib_main, lib_sci = U(101), U(102)
    camp, inst = U(103), U(104)
    loc_stacks, loc_ref, loc_sci, loc_online = U(201), U(202), U(203), U(204)
    book, dvd = U(301), U(302)
    lt = U(311)
    lc = U(321)
    nt_count, nt_created = U(331), U(332)
    isbn = U(341)
    lcc = U(351)

    data = {
        "locations": [
            {"id": loc_stacks, "code": "MAIN-STACKS", "name": "Main Stacks", "libraryId": lib_main, "campusId": camp, "institutionId": inst},
            {"id": loc_ref, "code": "MAIN-REF", "name": "Main Reference", "libraryId": lib_main, "campusId": camp, "institutionId": inst},
            {"id": loc_sci, "code": "SCI-STACKS", "name": "Science Stacks", "discoveryDisplayName": "Science Library Stacks", "libraryId": lib_sci, "campusId": camp, "institutionId": inst},
            {"id": loc_online, "code": "ONLINE", "name": "Online", "libraryId": lib_main, "campusId": camp, "institutionId": inst},
        ],
        "loclibs": [{"id": lib_main, "code": "MAIN", "name": "Main Library"}, {"id": lib_sci, "code": "SCI", "name": "Science Library"}],
        "loccamps": [{"id": camp, "code": "C", "name": "Central Campus"}],
        "locinsts": [{"id": inst, "code": "U", "name": "Example University"}],
        "mtypes": [{"id": book, "name": "book"}, {"id": dvd, "name": "dvd"}],
        "loantypes": [{"id": lt, "name": "Can circulate"}],
        "callNumberTypes": [{"id": lc, "name": "Library of Congress classification"}],
        "itemNoteTypes": [{"id": nt_count, "name": "Legacy circ count"}, {"id": nt_created, "name": "Legacy created date"}],
        "identifierTypes": [{"id": isbn, "name": "ISBN"}],
        "classificationTypes": [{"id": lcc, "name": "LC"}],
    }

    instances = [
        {"id": U(1), "hrid": "in00000000001", "source": "MARC", "title": "Learning Python", "metadata": meta()},
        {"id": U(2), "hrid": "in00000000002", "source": "FOLIO", "title": "A FOLIO-only record",
         "contributors": [{"name": "Doe, Jane", "primary": True}, {"name": "Roe, Rick"}],
         "identifiers": [{"identifierTypeId": isbn, "value": "9781234567897"}],
         "classifications": [{"classificationTypeId": lcc, "classificationNumber": "Z678.9 .D64 2019"}],
         "publication": [{"publisher": "Example Press", "place": "Boston", "dateOfPublication": "c2019"}],
         "languages": ["eng"], "metadata": meta()},
        {"id": U(3), "hrid": "in00000000003", "source": "MARC", "title": "Suppressed title", "discoverySuppress": True, "metadata": meta()},
        {"id": U(4), "hrid": "in00000000004", "source": "MARC", "title": "MARC missing from SRS", "metadata": meta()},
        {"id": U(5), "hrid": "in00000000005", "source": "MARC", "title": "No items here", "metadata": meta()},
    ]
    holdings = [
        {"id": U(11), "hrid": "ho1", "instanceId": U(1), "permanentLocationId": loc_stacks, "metadata": meta()},
        {"id": U(12), "hrid": "ho2", "instanceId": U(2), "permanentLocationId": loc_sci, "metadata": meta()},
        {"id": U(13), "hrid": "ho3", "instanceId": U(3), "permanentLocationId": loc_stacks, "metadata": meta()},
        {"id": U(14), "hrid": "ho4", "instanceId": U(4), "permanentLocationId": loc_online, "metadata": meta()},
        {"id": U(15), "hrid": "ho5", "instanceId": U(5), "permanentLocationId": loc_stacks, "metadata": meta()},
    ]

    def item(n, h, barcode, status="Available", loc=loc_stacks, mtype=book, **kw):
        rec = {
            "id": U(n), "hrid": f"it{n}", "holdingsRecordId": h, "barcode": barcode,
            "status": {"name": status, "date": "2024-05-01T12:00:00.000+00:00"},
            "materialTypeId": mtype, "permanentLoanTypeId": lt, "effectiveLocationId": loc,
            "effectiveCallNumberComponents": {"callNumber": "QA76.73.P98 L88 2020", "typeId": lc},
            "metadata": meta(created="2023-08-01T09:00:00.000+00:00"),
        }
        rec.update(kw)
        return rec

    items = [
        item(21, U(11), "B0001", lastCheckIn={"dateTime": "2025-02-01T15:00:00.000+00:00"}, copyNumber="1",
             itemIdentifier="LEGACY-21", numberOfPieces="2", itemLevelCallNumber="QA76.73.P98 L88 2020"),
        item(22, U(11), "B0002", status="Checked out", volume="v.2"),
        item(23, U(12), "B0003", loc=loc_sci, mtype=dvd,
             effectiveCallNumberComponents={"prefix": "DVD", "callNumber": "PN1997 .X1", "typeId": lc},
             notes=[{"itemNoteTypeId": nt_count, "note": "42"}, {"itemNoteTypeId": nt_created, "note": "1998-03-15"}]),
        item(24, U(12), None, loc=loc_sci),
        item(25, U(12), "B0005", status="Withdrawn", loc=loc_sci),
        item(26, U(13), "B0006"),
        item(27, U(14), "B0007", loc=loc_online),
        item(28, U(11), "B0008", discoverySuppress=True),
    ]
    loans = [
        {"id": U(41), "itemId": U(21), "loanDate": "2023-09-01T10:00:00Z", "returnDate": "2023-09-20T10:00:00Z", "status": {"name": "Closed"}, "metadata": meta()},
        {"id": U(42), "itemId": U(21), "loanDate": "RECENT", "returnDate": "RECENT_RETURN", "status": {"name": "Closed"}, "renewalCount": 2, "metadata": meta()},
        {"id": U(43), "itemId": U(22), "loanDate": "RECENT", "dueDate": "2099-01-01T04:59:59Z", "status": {"name": "Open"}, "metadata": meta()},
        {"id": U(44), "itemId": U(23), "loanDate": "2020-01-05T10:00:00Z", "returnDate": "2020-01-25T10:00:00Z", "status": {"name": "Closed"}, "metadata": meta()},
    ]
    srs = {
        U(1): marc_content("in00000000001", "Learning Python /", extra_949=True),
        U(3): marc_content("in00000000003", "Suppressed title"),
        U(5): marc_content("in00000000005", "No items here"),
    }
    data.update({"instances": instances, "holdingsRecords": holdings, "items": items, "loans": loans, "srs": srs})
    return data


PATHS = {
    "/locations": "locations", "/location-units/libraries": "loclibs", "/location-units/campuses": "loccamps",
    "/location-units/institutions": "locinsts", "/material-types": "mtypes", "/loan-types": "loantypes",
    "/call-number-types": "callNumberTypes", "/item-note-types": "itemNoteTypes",
    "/identifier-types": "identifierTypes", "/classification-types": "classificationTypes",
    "/instance-storage/instances": "instances", "/holdings-storage/holdings": "holdingsRecords",
    "/item-storage/items": "items", "/loan-storage/loans": "loans",
}


class MockFolio:
    def __init__(self, data: dict | None = None) -> None:
        self.data = data or default_data()
        self.lock = threading.Lock()
        self.requests: list[str] = []
        self.fail_next: list[int] = []   # status codes to return for the next N data requests
        self.expire_tokens = False
        self.valid_tokens: set[str] = set()
        handler = self._handler()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()

    # ------------------------------------------------------------ CQL subset
    @staticmethod
    def apply_query(records: list[dict], cql: str) -> list[dict]:
        body, _, sort = cql.partition(" sortBy ")
        out = list(records)
        m = re.search(r'id>"([^"]+)"', body)
        if m:
            out = [r for r in out if r["id"] > m.group(1)]
        m = re.search(r'metadata\.updatedDate>="([^"]+)"', body)
        if m:
            since = m.group(1).replace(".000Z", "")
            out = [r for r in out if (r.get("metadata") or {}).get("updatedDate", "")[:19] >= since[:19]]
        if sort.strip() == "id":
            out.sort(key=lambda r: r["id"])
        return out

    def _handler(self):
        mock = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj=None, headers=None):
                body = json.dumps(obj).encode() if obj is not None else b""
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def _authed(self):
                tok = self.headers.get("x-okapi-token")
                return tok in mock.valid_tokens and not mock.expire_tokens

            def do_POST(self):
                u = urlparse(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"null")
                mock.requests.append(f"POST {u.path}")
                if u.path == "/authn/login-with-expiry":
                    if payload.get("password") != "secret":
                        return self._send(422, {"errors": [{"message": "bad password"}]})
                    tok = uuid.uuid4().hex
                    mock.valid_tokens.add(tok)
                    mock.expire_tokens = False
                    return self._send(201, {"accessTokenExpiration": "2099-01-01T00:00:00Z"},
                                      {"Set-Cookie": f"folioAccessToken={tok}; Path=/"})
                if not self._authed():
                    return self._send(401, {"error": "unauthorized"})
                if u.path == "/source-storage/source-records":
                    recs = []
                    for iid in payload:
                        content = mock.data["srs"].get(iid)
                        if content:
                            recs.append({"recordId": U(9000), "externalIdsHolder": {"instanceId": iid},
                                         "parsedRecord": {"content": content}, "deleted": False})
                    return self._send(200, {"sourceRecords": recs, "totalRecords": len(recs)})
                return self._send(404, {"error": "not found"})

            def do_GET(self):
                u = urlparse(self.path)
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                mock.requests.append(f"GET {u.path}?{u.query}")
                if not self._authed():
                    return self._send(401, {"error": "unauthorized"})
                if mock.fail_next:
                    return self._send(mock.fail_next.pop(0), {"error": "transient"})
                if u.path == "/source-storage/source-records":
                    return self._send(200, {"sourceRecords": [], "totalRecords": len(mock.data["srs"])})
                key = PATHS.get(u.path)
                if not key:
                    return self._send(404, {"error": "not found"})
                with mock.lock:
                    recs = mock.apply_query(mock.data[key], q.get("query", "cql.allRecords=1"))
                limit, offset = int(q.get("limit", 10)), int(q.get("offset", 0))
                return self._send(200, {key: copy.deepcopy(recs[offset: offset + limit]), "totalRecords": len(recs)})

        return H
