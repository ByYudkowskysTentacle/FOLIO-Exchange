"""Minimal, read-only FOLIO HTTP client (adapted from folio-chq-extract).

Works with both Okapi-based FOLIO and Eureka (Kong gateway) deployments:
both expose ``POST /authn/login-with-expiry``, which returns the access and
refresh tokens as cookies. The access token is also sent as
``x-okapi-token`` so the client works with gateways that ignore cookies.

Every request passes two guards before anything is sent: user and patron paths
are refused outright (``PatronDataRefused``), and only the paths in the reads
registry may be called, with GET, or POST for logging in and for fetching MARC
records by instance id (a read).
"""
from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from . import __version__, reads
from .errors import FxError, PatronDataRefused

log = logging.getLogger(__name__)

RETRY_STATUSES = {408, 429, 500, 502, 503, 504}


class FolioError(FxError):
    pass


class LoginFailed(FolioError):
    """FOLIO refused the user name or password. Never retried: repeated failures can lock the account."""


class FolioUnreachable(FolioError):
    """FOLIO could not be reached at all (wrong address, DNS, firewall, VPN). Not worth retrying for minutes."""


def guard(method: str, path: str) -> None:
    """Refuse patron paths, paths outside the reads registry, and any write. Raises before sending."""
    if reads.is_patron_path(path):
        raise PatronDataRefused(
            f"Refused {method} {path.split('?', 1)[0]}: FOLIO-Exchange never reads user or patron data."
        )
    if not reads.is_allowed_path(path):
        raise FolioError(f"Refused {method} {path.split('?', 1)[0]}: not a path in the reads registry.")
    clean = "/" + "/".join(s for s in path.split("?", 1)[0].lower().split("/") if s)
    if method.upper() != "GET" and not (method.upper() == "POST" and clean in reads.POST_PATHS):
        raise FolioError(f"read-only: refused {method} {clean}; FOLIO-Exchange only reads from FOLIO.")


def _no_redirect(resp) -> None:
    if 300 <= resp.status_code < 400:
        raise FolioError(
            f"FOLIO answered with a redirect (HTTP {resp.status_code}) to {resp.headers.get('Location', '?')}. "
            "Redirects are never followed; set folio.base_url to the final address (the Kong gateway on Eureka)."
        )


def _reason(exc: Exception) -> str:
    text = str(exc)
    for marker, plain in (("10061", "connection refused"), ("Connection refused", "connection refused"),
                          ("getaddrinfo", "the name was not found"), ("Name or service not known",
                                                                      "the name was not found"),
                          ("CERTIFICATE_VERIFY_FAILED", "its certificate is not trusted")):
        if marker in text:
            return plain
    return type(exc).__name__


def _cql_quote(value: str) -> str:
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


class _TransientLoginError(Exception):
    """Login hit a temporary server error; retried by request()."""


class FolioClient:
    def __init__(
        self,
        base_url: str,
        tenant: str,
        username: str,
        password: str,
        *,
        timeout: float = 120,
        max_retries: int = 5,
        verify_tls: bool = True,
        user_agent: str = f"folio-exchange/{__version__}",
    ) -> None:
        if not base_url:
            raise FolioError("folio.base_url is not set")
        if not password:
            raise FolioError(
                "No FOLIO password found. Store it with `folio-exchange secret set folio`, "
                "or set the FOLIO_PASSWORD environment variable."
            )
        self.base_url = base_url.rstrip("/")
        self.tenant = tenant
        self.username = username
        self._password = password
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = requests.Session()
        self.session.verify = verify_tls
        self.session.max_redirects = 0  # never follow a redirect: it could carry the token elsewhere
        self.session.headers.update(
            {
                "x-okapi-tenant": tenant,
                "Accept": "application/json",
                "User-Agent": user_agent,
            }
        )
        self._token: str | None = None
        self._expires: datetime | None = None
        self._reached = False                      # has FOLIO ever answered this client?
        self._refused: LoginFailed | None = None

    # ------------------------------------------------------------------ auth
    def login(self) -> None:
        """Log in once. Transient failures raise _TransientLoginError; use request() for retries.
        A refused login raises LoginFailed, and every later call raises it again without asking FOLIO."""
        if self._refused:
            raise self._refused
        guard("POST", "/authn/login-with-expiry")
        url = f"{self.base_url}/authn/login-with-expiry"
        body = {"username": self.username, "password": self._password}
        try:
            resp = self.session.post(url, json=body, timeout=self.timeout, allow_redirects=False)
        except requests.ConnectionError as exc:
            if self._reached:
                raise _TransientLoginError(type(exc).__name__) from None
            raise FolioUnreachable(
                f"Cannot reach FOLIO at {self.base_url} ({_reason(exc)}). Check folio.base_url in site.yaml and "
                "the network or VPN, then run `folio-exchange check`."
            ) from None
        except requests.Timeout:
            raise _TransientLoginError("login timed out") from None
        self._reached = True
        _no_redirect(resp)
        if resp.status_code in RETRY_STATUSES:
            raise _TransientLoginError(f"login returned HTTP {resp.status_code}")
        if resp.status_code == 404:
            # Very old FOLIO releases (pre-Poppy) only have the legacy endpoint.
            resp = self.session.post(f"{self.base_url}/authn/login", json=body, timeout=self.timeout,
                                     allow_redirects=False)
            _no_redirect(resp)
            if resp.ok:
                self._token = resp.headers.get("x-okapi-token")
                self._expires = datetime.now(timezone.utc) + timedelta(days=1)
        elif resp.ok:
            self._token = self.session.cookies.get("folioAccessToken") or resp.headers.get("x-okapi-token")
            exp = (resp.json() or {}).get("accessTokenExpiration")
            self._expires = _parse_dt(exp) or datetime.now(timezone.utc) + timedelta(minutes=10)
        if not resp.ok or not self._token:
            self._refused = LoginFailed(
                f"FOLIO refused the login for user '{self.username}' (HTTP {resp.status_code}). Check the user "
                "name, folio.tenant and the password (`folio-exchange secret status`). To avoid locking the "
                "account, FOLIO-Exchange does not try again until you run it again."
            )
            raise self._refused
        self.session.headers["x-okapi-token"] = self._token
        log.debug("Logged in; token expires %s", self._expires)

    def _ensure_token(self) -> None:
        if self._token is None or (
            self._expires is not None and datetime.now(timezone.utc) > self._expires - timedelta(seconds=60)
        ):
            self.login()

    # --------------------------------------------------------------- request
    def request(self, method: str, path: str, *, params: dict | None = None, json: Any = None) -> Any:
        guard(method, path)
        url = f"{self.base_url}/{path.lstrip('/')}"
        attempt = 0
        relogged = False
        while True:
            try:
                self._ensure_token()
                resp = self.session.request(method, url, params=params, json=json, timeout=self.timeout,
                                            allow_redirects=False)
                self._reached = True
                _no_redirect(resp)
                if resp.status_code == 401 and not relogged:
                    relogged = True
                    self._token = None
                    continue
                if resp.ok:
                    return resp.json() if resp.content else None
                if resp.status_code not in RETRY_STATUSES:
                    raise FolioError(f"{method} {path} -> {resp.status_code}: {resp.text[:500]}")
                err: Any = f"HTTP {resp.status_code}"
            except _TransientLoginError as exc:
                err = exc
            except (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError, ValueError) as exc:
                # ValueError covers a truncated/invalid JSON body
                err = exc
            attempt += 1
            if attempt > self.max_retries:
                raise FolioError(f"{method} {path} failed after {self.max_retries} retries: {err}")
            wait = min(60, 2**attempt)
            log.warning("%s %s failed (%s); retrying in %ss", method, path, err, wait)
            time.sleep(wait)

    def get(self, path: str, params: dict | None = None) -> Any:
        return self.request("GET", path, params=params)

    def post(self, path: str, body: Any, params: dict | None = None) -> Any:
        return self.request("POST", path, params=params, json=body)

    # ---------------------------------------------------------------- paging
    def count(self, path: str, query: str = "cql.allRecords=1") -> int:
        data = self.get(path, {"query": query, "limit": 0, "totalRecords": "exact"})
        return int(data.get("totalRecords", 0))

    def iter_records(
        self, path: str, key: str, query: str | None = None, page_size: int = 500
    ) -> Iterator[dict]:
        """Yield every record matching ``query`` using id-keyset paging.

        Keyset paging (``id > last sortBy id``) stays fast on large tables,
        unlike offset paging, which slows down as the offset grows.
        """
        base = f"({query})" if query else None
        last_id: str | None = None
        while True:
            clauses = [c for c in (base, f'id>"{last_id}"' if last_id else None) if c]
            cql = (" and ".join(clauses) if clauses else "cql.allRecords=1") + " sortBy id"
            data = self.get(path, {"query": cql, "limit": page_size, "totalRecords": "none"})
            records = data.get(key, []) if data else []
            if not records:
                return
            yield from records
            if len(records) < page_size:
                return
            last_id = records[-1]["id"]

    def iter_pages(self, path: str, key: str, query: str | None = None, page_size: int = 500) -> Iterator[list[dict]]:
        page: list[dict] = []
        for rec in self.iter_records(path, key, query, page_size):
            page.append(rec)
            if len(page) >= page_size:
                yield page
                page = []
        if page:
            yield page

    def iter_where_in(
        self, path: str, key: str, field: str, values: list[str], batch: int = 50
    ) -> Iterator[dict]:
        """Yield records whose ``field`` is any of ``values``, ``batch`` values per CQL query (for previews)."""
        values = [v for v in dict.fromkeys(values) if v]
        for i in range(0, len(values), batch):
            chunk = values[i : i + batch]
            cql = f"{field}==(" + " or ".join(_cql_quote(v) for v in chunk) + ")"
            yield from self.iter_records(path, key, cql, page_size=max(len(chunk) * 4, 100))

    def get_all(self, path: str, key: str, page_size: int = 1000) -> list[dict]:
        """Fetch a small reference-data collection in full."""
        out: list[dict] = []
        offset = 0
        while True:
            data = self.get(path, {"query": "cql.allRecords=1 sortBy id", "limit": page_size, "offset": offset})
            recs = data.get(key, [])
            out.extend(recs)
            if len(recs) < page_size:
                return out
            offset += page_size


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
