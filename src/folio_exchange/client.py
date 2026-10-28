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
import ssl
import sys
import time
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter

from . import __version__, reads
from .errors import ConfigError, FxError, PatronDataRefused

log = logging.getLogger(__name__)

RETRY_STATUSES = {408, 429, 500, 502, 503, 504}
JUDGED = {400, 401, 403, 422}         # login answers that mean FOLIO looked at the user name and password
# What FOLIO's certificate is checked against: True (the usual public certificates), False (not checked),
# the path of a PEM file, or an SSL context (the Windows certificate store).
Verify = bool | str | ssl.SSLContext
UNTRUSTED = ("FOLIO's certificate is not trusted by this computer. If your campus or a network proxy issues its "
             "own certificates, set folio.ca_bundle in site.yaml to that certificate's PEM file, or to windows "
             "to trust the Windows certificate store (as web browsers on this computer do). Do not switch off "
             "folio.verify_tls: that would let anyone on the network read the service account's password.")


class FolioError(FxError):
    """A FOLIO request failed. ``status`` is the HTTP status when FOLIO answered, else None."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


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


def tls_verify(folio: dict, root: Path | str | None = None) -> Verify:
    """What FOLIO's certificate is checked against, from the site's folio settings: ``verify_tls: false`` turns
    the check off; ``ca_bundle`` is a PEM file (relative paths are from the site folder ``root``) or windows,
    the Windows certificate store; otherwise the usual public certificates."""
    if folio.get("verify_tls") is False:
        return False
    bundle = folio.get("ca_bundle")
    if not bundle:
        return True
    bundle = str(bundle).strip()
    if bundle.lower() == "windows":
        return _windows_store()
    path = Path(bundle).expanduser()
    if not path.is_absolute() and root is not None:
        path = Path(root) / path
    if not path.is_file():
        raise ConfigError(f"folio.ca_bundle: {path} was not found. Save the certificate (a PEM file) there, or "
                          "set ca_bundle to windows to use the Windows certificate store.")
    return str(path.resolve())


def _windows_store() -> ssl.SSLContext:
    if sys.platform != "win32":
        raise ConfigError("folio.ca_bundle: windows (the Windows certificate store) only works on Windows. On "
                          "this computer, set ca_bundle to the path of a PEM file of the certificates to trust.")
    try:
        import truststore
    except ImportError:
        raise ConfigError("folio.ca_bundle: windows needs the truststore package, which is missing. Install "
                          "FOLIO-Exchange again to add it.") from None
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


class _ContextAdapter(HTTPAdapter):
    """Checks certificates with one SSL context (the Windows certificate store) instead of the PEM bundle."""

    def __init__(self, context: ssl.SSLContext) -> None:
        self._context = context
        super().__init__()

    def init_poolmanager(self, *args: Any, **kwargs: Any) -> None:
        kwargs["ssl_context"] = self._context
        super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, *args: Any, **kwargs: Any) -> Any:
        kwargs["ssl_context"] = self._context
        return super().proxy_manager_for(*args, **kwargs)


def _untrusted(exc: Exception) -> bool:
    text = str(exc)                           # the Windows store's own wording names the error class only
    return any(m in text for m in ("CERTIFICATE_VERIFY_FAILED", "certificate verify failed", "SSLCertVerificationError"))


def _reason(exc: Exception) -> str:
    text = str(exc)
    for marker, plain in (("10061", "connection refused"), ("Connection refused", "connection refused"),
                          ("getaddrinfo", "the name was not found"), ("Name or service not known",
                                                                      "the name was not found")):
        if marker in text:
            return plain
    return type(exc).__name__


def _cql_quote(value: str) -> str:
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


class _TransientLoginError(FolioError):
    """Login hit a temporary server error; retried by request() and connect()."""


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
        verify_tls: Verify = True,
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
        if isinstance(verify_tls, ssl.SSLContext):
            self.session.mount("https://", _ContextAdapter(verify_tls))
            self.session.verify = True
        else:
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
        self.clock_offset: timedelta | None = None  # FOLIO's clock minus this computer's
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
            if _untrusted(exc):
                raise FolioUnreachable(f"Cannot connect to FOLIO at {self.base_url}: {UNTRUSTED}") from None
            if self._reached:
                raise _TransientLoginError(type(exc).__name__) from None
            raise FolioUnreachable(
                f"Cannot reach FOLIO at {self.base_url} ({_reason(exc)}). Check folio.base_url in site.yaml and "
                "the network or VPN, then run `folio-exchange check`."
            ) from None
        except requests.Timeout:
            raise _TransientLoginError("login timed out") from None
        self._reached = True
        self._note_clock(resp)
        _no_redirect(resp)
        if resp.status_code in RETRY_STATUSES:
            raise _TransientLoginError(f"login returned HTTP {resp.status_code}")
        first = resp.status_code
        if first == 404:
            # Very old FOLIO releases (pre-Poppy) only have the legacy endpoint.
            resp = self.session.post(f"{self.base_url}/authn/login", json=body, timeout=self.timeout,
                                     allow_redirects=False)
            _no_redirect(resp)
            if resp.status_code in RETRY_STATUSES:
                raise _TransientLoginError(f"login returned HTTP 404, then HTTP {resp.status_code}")
            if resp.ok:
                self._token = resp.headers.get("x-okapi-token")
                self._expires = datetime.now(timezone.utc) + timedelta(days=1)
        elif resp.ok:
            self._token = self.session.cookies.get("folioAccessToken") or resp.headers.get("x-okapi-token")
            exp = (resp.json() or {}).get("accessTokenExpiration")
            self._expires = _parse_dt(exp) or datetime.now(timezone.utc) + timedelta(minutes=10)
        if not resp.ok and resp.status_code not in JUDGED:
            # No password was judged (the login module is missing or restarting, say during an upgrade): not a
            # refusal, so a later attempt may try again.
            also = f", then /authn/login HTTP {resp.status_code}" if first == 404 else ""
            raise FolioUnreachable(
                f"FOLIO's login service did not answer as expected (/authn/login-with-expiry returned HTTP {first}"
                f"{also}), so the password was not checked. FOLIO may be part way through an upgrade or restart: "
                "run again later (a scheduled run tries again at its next hourly start). If it continues, check "
                "folio.base_url in site.yaml and ask your FOLIO host.", status=first)
        if not resp.ok or not self._token:
            where = " from /authn/login; /authn/login-with-expiry returned HTTP 404" if first == 404 else ""
            self._refused = LoginFailed(
                f"FOLIO refused the login for user '{self.username}' (HTTP {resp.status_code}{where}). Check the user "
                "name, folio.tenant and the password (`folio-exchange secret status`). To avoid locking the "
                "account, FOLIO-Exchange does not try again until you run it again."
            )
            raise self._refused
        self.session.headers["x-okapi-token"] = self._token
        log.debug("Logged in; token expires %s", self._expires)

    def connect(self) -> None:
        """Log in now, retrying a busy gateway the way request() does. For check, init and the page, which
        talk to FOLIO before anything else and should never end in a traceback."""
        attempt = 0
        while True:
            try:
                self.login()
                return
            except _TransientLoginError as exc:
                attempt += 1
                if attempt > self.max_retries:
                    raise FolioError(
                        f"FOLIO did not answer the login properly ({exc}); the gateway may be busy or restarting. "
                        "Try again in a few minutes. If it continues, ask your FOLIO host.") from None
                wait = min(60, 2**attempt)
                log.warning("Login: %s; retrying in %ss", exc, wait)
                time.sleep(wait)

    def _note_clock(self, resp) -> None:
        """Remember how far this computer's clock is from FOLIO's (from the HTTP Date header)."""
        stamp = getattr(resp, "headers", {}).get("Date") if hasattr(resp, "headers") else None
        if not stamp or not isinstance(stamp, str):
            return
        try:
            server = parsedate_to_datetime(stamp)
        except (TypeError, ValueError):
            return
        if server.tzinfo is None:
            return
        offset = server - datetime.now(timezone.utc)
        if self.clock_offset is None and abs(offset) > timedelta(minutes=5):
            log.warning("This computer's clock is %d minutes %s FOLIO's; FOLIO's time is used for syncing",
                        abs(int(offset.total_seconds() // 60)), "behind" if offset > timedelta(0) else "ahead of")
        self.clock_offset = offset

    def server_now(self) -> datetime:
        """FOLIO's current time, as far as its last answer showed; this computer's time before any answer."""
        return datetime.now(timezone.utc) + (self.clock_offset or timedelta(0))

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
                self._note_clock(resp)
                _no_redirect(resp)
                if resp.status_code == 401 and not relogged:
                    relogged = True
                    self._token = None
                    continue
                if resp.ok:
                    return resp.json() if resp.content else None
                if resp.status_code not in RETRY_STATUSES:
                    raise FolioError(f"{method} {path} -> {resp.status_code}: {resp.text[:500]}",
                                     status=resp.status_code)
                err: Any = f"HTTP {resp.status_code}"
            except _TransientLoginError as exc:
                err = exc
            except (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError, ValueError) as exc:
                # ValueError covers a truncated/invalid JSON body
                if _untrusted(exc):
                    raise FolioUnreachable(f"Cannot connect to FOLIO at {self.base_url}: {UNTRUSTED}") from None
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


def probe(client: FolioClient, read_names) -> list[dict]:
    """Read one page of every endpoint ``read_names`` need, plus all reference data, and say for each whether
    it worked: ``ok`` (with a record count), ``denied`` (FOLIO refused: a permission is missing) or ``failed``
    (anything else: a wrong path, a module that is not there, a server error). Used by check, init and the page."""
    wanted = set(read_names)
    targets = [(n, reads.READS[n].path, reads.READS[n].permission) for n in reads.ENTITY_ORDER + ("marc",)
               if n in wanted]
    targets += [(kind, path, perm) for kind, (path, _key, perm) in reads.REFDATA.items()]
    out = []
    for name, path, perm in targets:
        row: dict[str, Any] = {"name": name, "path": path, "permission": perm, "count": None, "error": None}
        try:
            if path == reads.READS["marc"].path:
                client.get(path, {"limit": 0})
            else:
                row["count"] = client.count(path)
            row["status"] = "ok"
        except FolioError as exc:
            row["status"] = "denied" if exc.status in (401, 403) else "failed"
            row["error"] = str(exc)[:300]
        out.append(row)
    return out
