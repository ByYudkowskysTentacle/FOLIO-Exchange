"""Deliver output files: SFTP with a pinned host key (DEL-1, DEL-2), or a local folder.

Unknown host keys are never accepted. Plain FTP is never offered. Destinations
reported back (for history and the audit log) never contain a password.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import shutil
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import credentials
from .errors import ConfigError, DeliveryError, FxError

log = logging.getLogger(__name__)

SFTP_TIMEOUT = 300   # seconds without progress before an SFTP read or write gives up


def sha256_fingerprint(key) -> str:
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


def md5_fingerprint(key) -> str:
    """The older aa:bb:... form some vendors still quote."""
    return ":".join(f"{b:02x}" for b in hashlib.md5(key.asbytes(), usedforsecurity=False).digest())


_quiet_lock = threading.Lock()
_quiet_depth = [0, logging.NOTSET]       # handshakes being probed now, and the level to put back after them


@contextmanager
def _quiet_handshakes() -> Iterator[None]:
    """Keep paramiko from logging an error with a traceback for each key type a probing handshake finds missing
    (an expected answer, not a fault), even when several probes overlap."""
    logger = logging.getLogger("paramiko.transport")
    with _quiet_lock:
        if not _quiet_depth[0]:
            _quiet_depth[1] = logger.level
            logger.setLevel(logging.CRITICAL)
        _quiet_depth[0] += 1
    try:
        yield
    finally:
        with _quiet_lock:
            _quiet_depth[0] -= 1
            if not _quiet_depth[0]:
                logger.setLevel(_quiet_depth[1])


def _probe_order() -> list[str]:
    """The key types to probe, the common ones (Ed25519, then RSA, the usual older pin) before ECDSA."""
    return sorted(KEY_TYPES, key=lambda kind: kind.startswith("ecdsa"))


def _read_host_key(host: str, port: int, timeout: float, key_type: str | None = None) -> dict[str, str]:
    import paramiko

    off = {"keys": disabled_host_key_algorithms(key_type)} if key_type else None
    transport = paramiko.Transport((host, port), disabled_algorithms=off)
    try:
        transport.start_client(timeout=timeout)
        key = transport.get_remote_server_key()
        return {"sha256": sha256_fingerprint(key), "md5": md5_fingerprint(key), "type": key.get_name()}
    finally:
        transport.close()


def host_key_fingerprints(host: str, port: int = 22, timeout: float = 30) -> dict[str, Any]:
    """Read the server's host keys without logging in, for a person to compare with the vendor's. The result
    describes the key the server prefers, {"sha256": "SHA256:...", "md5": "aa:bb:...", "type": "ssh-ed25519"}, and
    lists under "keys" every key it offers (that one first, then one per other type in ``KEY_TYPES``), since a
    vendor may quote the fingerprint of an older kind than the one the server leads with. The SHA-256 value is
    pinned with the key's type (``host_key_type``), so later logins ask for a key of that type even if the server
    adds a newer kind."""
    import paramiko

    port = int(port or 22)
    first = _read_host_key(host, port, timeout)
    offered = [first]
    for kind in _probe_order():
        if kind == first["type"]:
            continue
        try:
            with _quiet_handshakes():
                key = _read_host_key(host, port, min(timeout, 10), kind)
        except (paramiko.SSHException, EOFError, OSError):     # the server has no key of this type
            continue
        if key["type"] == kind and all(key["sha256"] != k["sha256"] for k in offered):
            offered.append(key)
    return {**first, "keys": offered}


def host_key_fingerprint(host: str, port: int = 22, timeout: float = 30) -> str:
    """Read the server's host key without logging in, for a person to compare with the vendor's."""
    return host_key_fingerprints(host, port, timeout)["sha256"]


# The host key types a pin can name (as the key reports its type), and the algorithms that ask for each.
KEY_TYPES: dict[str, tuple[str, ...]] = {
    "ssh-ed25519": ("ssh-ed25519",),
    "ecdsa-sha2-nistp256": ("ecdsa-sha2-nistp256",),
    "ecdsa-sha2-nistp384": ("ecdsa-sha2-nistp384",),
    "ecdsa-sha2-nistp521": ("ecdsa-sha2-nistp521",),
    "ssh-rsa": ("rsa-sha2-512", "rsa-sha2-256", "ssh-rsa"),
}


def disabled_host_key_algorithms(key_type: str) -> list[str]:
    """Every host key algorithm except those for ``key_type`` (paramiko's ``disabled_algorithms["keys"]``), so
    the server is asked for its key of that type even when it also has a newer kind."""
    import paramiko

    allowed = KEY_TYPES.get(key_type, (key_type,))
    every = dict.fromkeys([*paramiko.Transport._preferred_keys, *(a for t in KEY_TYPES.values() for a in t)])
    return [a for a in every if a not in allowed]


def _connect_pinned(login: dict, expected: str, key_type: str | None):
    """Log in only to a server whose host key has the pinned SHA-256 value; any other key is refused before
    logging in. With the pinned key's type only that type is negotiated, and a server that no longer offers that
    type is reported as a changed key. A pin without a type (from before types were kept) is tried in paramiko's
    own order first, then once for each other key type it has not yet seen."""
    import paramiko

    class Mismatch(paramiko.SSHException):
        pass

    seen: set[str] = set()

    class Pinned(paramiko.MissingHostKeyPolicy):
        def missing_host_key(self, _client, hostname, key):
            seen.add(key.get_name())
            got = sha256_fingerprint(key)
            if got != expected:
                raise Mismatch(f"Host key for {hostname} is {got}, expected {expected}")

    tries: list[str | None] = [key_type] if key_type else [None, *_probe_order()]
    first: Exception | None = None
    for kind in tries:
        if kind in seen:                       # the server already showed its key of this type
            continue
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(Pinned())
        extra = {"disabled_algorithms": {"keys": disabled_host_key_algorithms(kind)}} if kind else {}
        try:
            if kind and not key_type:
                with _quiet_handshakes():
                    client.connect(**login, **extra)
            else:
                client.connect(**login, **extra)
            return client
        except paramiko.IncompatiblePeer as exc:        # the server has no key of this type
            client.close()
            if key_type:
                raise Mismatch(f"Host key for {login['hostname']}: the server no longer offers the pinned {key_type} key "
                               f"(pinned {expected}){_offered_now(login)}") from exc
            first = first or exc
        except Mismatch as exc:
            client.close()
            if key_type:
                raise
            first = first or exc
        except BaseException:
            client.close()
            raise
    raise first or paramiko.SSHException(f"Host key for {login['hostname']}: none matches {expected}")


def _offered_now(login: dict) -> str:
    """'; it now offers <type> SHA256:...' naming the server's preferred key, or '' when it cannot be read."""
    import paramiko

    try:
        with _quiet_handshakes():
            key = _read_host_key(login["hostname"], int(login.get("port") or 22), 10)
    except (paramiko.SSHException, EOFError, OSError):
        return ""
    return f"; it now offers {key['type']} {key['sha256']}"


def _secret(d: dict) -> str | None:
    return credentials.resolve(d.get("secret"), credentials.SFTP_SERVICE,
                               credentials.sftp_account(d.get("username") or "", d.get("host") or ""))


def _connect(d: dict):
    import paramiko

    host = (d.get("host") or "").strip()
    if not host:
        raise ConfigError("delivery.host is not set")
    if not (d.get("username") or "").strip():
        raise ConfigError("delivery.username is not set")
    expected = (d.get("host_key_fingerprint") or "").strip()
    if d.get("host_key_confirmed") is False:
        raise ConfigError(
            f"The key of {host} ({expected}) has not been confirmed with the vendor yet, so FOLIO-Exchange will not "
            "log in to it. Once the vendor confirms their fingerprint, run `folio-exchange init` again and answer "
            "that it matches."
        )
    key_path = (d.get("private_key") or "").strip()
    secret = _secret(d)
    if not key_path and not secret:
        raise ConfigError(
            f"No SFTP password found for {d['username']}@{host} "
            f"({credentials.source(d.get('secret'), credentials.SFTP_SERVICE, credentials.sftp_account(d['username'], host))}). "
            "Store it with `folio-exchange secret set <feed>`."
        )
    login = {
        "hostname": host, "port": int(d.get("port") or 22), "username": d["username"],
        "password": None if key_path else secret,
        "key_filename": os.path.expanduser(key_path) if key_path else None,
        "passphrase": secret if key_path else None,
        "look_for_keys": False, "allow_agent": False, "timeout": 60, "banner_timeout": 60, "auth_timeout": 60,
    }
    if not expected:
        client = paramiko.SSHClient()
        kh = os.path.expanduser(d.get("known_hosts") or "~/.ssh/known_hosts")
        if os.path.exists(kh):
            client.load_host_keys(kh)
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        client.connect(**login)
    else:
        client = _connect_pinned(login, expected, (d.get("host_key_type") or "").strip() or None)
    transport = client.get_transport()
    if transport is not None:
        transport.set_keepalive(30)               # notice a connection that has silently gone away
    return client


def _open_sftp(client):
    """An SFTP session whose reads and writes give up after SFTP_TIMEOUT seconds without progress, so a
    stalled transfer becomes an ordinary failed delivery instead of holding the run (and the lock) for days."""
    sftp = client.open_sftp()
    channel = sftp.get_channel()
    if channel is not None:
        channel.settimeout(SFTP_TIMEOUT)
    return sftp


def _remote_dir(d: dict) -> str:
    return (d.get("remote_dir") or ".").rstrip("/") or "."


def _where(d: dict, name: str) -> str:
    return f"sftp://{d['username']}@{d['host']}:{int(d.get('port') or 22)}/{_remote_dir(d)}/{name}"


def plain_error(exc: BaseException, d: dict) -> str:
    """A delivery failure in words a librarian can act on (the page shows this instead of the raw error)."""
    import socket

    import paramiko

    host, port, user = d.get("host") or "the server", int(d.get("port") or 22), d.get("username") or "the user"
    if isinstance(exc, (ConfigError, DeliveryError)):
        return str(exc)
    if isinstance(exc, socket.gaierror):
        return f"Could not find the server {host}: check the server name the vendor gave you."
    if isinstance(exc, ConnectionRefusedError):
        return f"{host} refused the connection on port {port}: check the port, and that this network may reach it."
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return (f"No answer from {host} on port {port}: a firewall, or a VPN that is not connected, may be blocking "
                "it.")
    if isinstance(exc, paramiko.AuthenticationException):
        return f"{host} did not accept the login for {user}: check the user name and the stored password or key."
    if isinstance(exc, paramiko.SSHException) and "Host key for" in str(exc):
        return (f"{exc}. This may not be the vendor's server, so nothing was sent and nothing more will be: ask the "
                "vendor before going on.")
    if isinstance(exc, PermissionError):
        return f"The folder cannot be written to ({exc.strerror or exc})."
    return f"{type(exc).__name__}: {exc}"


def check_delivery(d: dict) -> list[str] | None:
    """Test the destination without sending anything. SFTP: log in, verify the host key and list the
    folder (None when listing is refused, as on upload-only accounts). Local: the folder is writable."""
    transport = d.get("transport")
    if transport == "local":
        folder = Path(os.path.expanduser(d.get("directory") or ""))
        if not d.get("directory") or not folder.is_dir():
            raise ConfigError(f"delivery.directory {folder} does not exist")
        if not os.access(folder, os.W_OK):
            raise ConfigError(f"delivery.directory {folder} is not writable")
        return sorted(p.name for p in folder.iterdir())
    if transport != "sftp":
        return []
    client = _connect(d)
    try:
        sftp = _open_sftp(client)
        try:
            names: list[str] | None = sorted(sftp.listdir(_remote_dir(d)))
        except PermissionError:
            names = None
        sftp.close()
    finally:
        client.close()
    return names


def _deliver_sftp(files: list[Path], d: dict, sent: list[str], touched: list[str]) -> list[str]:
    """Upload ``files``. ``touched`` gets each file name once the vendor's copy may have changed (a direct
    upload started, or a staged rename); ``sent`` gets each destination once it is complete."""
    client = _connect(d)
    try:
        sftp = _open_sftp(client)
        remote_dir = _remote_dir(d)
        if (d.get("upload_mode") or "staged") == "direct":
            # Upload-only accounts can't stat or rename. On object-store-backed
            # servers (e.g. AWS Transfer Family) a file only appears once complete.
            for f in files:
                final = f"{remote_dir}/{f.name}"
                log.info("Uploading %s (%s bytes) to %s:%s", f.name, f.stat().st_size, d["host"], final)
                touched.append(f.name)
                sftp.put(str(f), final, confirm=False)
                sent.append(_where(d, f.name))
            sftp.close()
            return sent
        # Phase 1: upload everything as .part, so a dropped connection never
        # leaves new bibs next to an old items file (or vice versa).
        staged = []
        inflight = None
        try:
            for f in files:
                final = f"{remote_dir}/{f.name}"
                tmp = final + ".part"
                log.info("Uploading %s (%s bytes) to %s:%s", f.name, f.stat().st_size, d["host"], tmp)
                inflight = tmp
                sftp.put(str(f), tmp, confirm=True)
                staged.append((tmp, final, f.name))
                inflight = None
        except Exception as exc:
            # Nothing was renamed into place. Remove the temporary files, and say which may remain.
            left = []
            for tmp in [s[0] for s in staged] + ([inflight] if inflight else []):
                try:
                    sftp.remove(tmp)
                except Exception:  # noqa: BLE001 - the connection may be gone; report, do not mask the cause
                    left.append(tmp.rsplit("/", 1)[-1])
            try:
                exc.fx_leftovers = left
            except AttributeError:
                pass
            raise
        # Phase 2: all uploads succeeded; rename them into place.
        for tmp, final, name in staged:
            touched.append(name)
            try:
                sftp.posix_rename(tmp, final)
            except OSError:  # server without posix-rename extension
                try:
                    sftp.remove(final)
                except OSError:
                    pass
                sftp.rename(tmp, final)
            sent.append(_where(d, name))
        sftp.close()
    finally:
        client.close()
    return sent


def _deliver_local(files: list[Path], d: dict, sent: list[str], touched: list[str]) -> list[str]:
    folder = Path(os.path.expanduser(d.get("directory") or ""))
    if not d.get("directory"):
        raise ConfigError("delivery.directory is not set (needed for transport: local)")
    folder.mkdir(parents=True, exist_ok=True)
    staged = []
    try:
        for f in files:  # copy everything first, then move into place, as SFTP staged mode does
            tmp = folder / (f.name + ".part")
            staged.append((tmp, folder / f.name))
            shutil.copyfile(f, tmp)
        for tmp, final in staged:
            touched.append(final.name)
            os.replace(tmp, final)
            sent.append(final.resolve().as_uri())
            log.info("Copied %s to %s", final.name, folder)
    finally:
        for tmp, _final in staged:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
    return sent


def deliver(files: list[Path], delivery: dict, feed_name: str) -> list[str]:
    """Send ``files`` by the feed's transport and return where each went.

    Raises DeliveryError (with ``sent``) when it stops after the vendor's copy may have changed, and FxError
    when it fails before anything reached the vendor. ConfigError for settings problems."""
    transport = delivery.get("transport")
    if transport == "none":
        log.info("Feed %s has transport none; files stay in the run folder", feed_name)
        return []
    if transport not in ("sftp", "local"):
        raise ConfigError(f"delivery.transport {transport!r} is not supported (use sftp, local or none)")
    sent: list[str] = []
    touched: list[str] = []
    try:
        if transport == "sftp":
            return _deliver_sftp(files, delivery, sent, touched)
        return _deliver_local(files, delivery, sent, touched)
    except FxError:
        raise
    except Exception as exc:  # noqa: BLE001 - paramiko, sockets and file systems raise many types
        reason = f"{type(exc).__name__}: {str(exc)[:200]}"
        again = (f"Fix the problem, then run `folio-exchange run {feed_name}` (a scheduled run also tries again "
                 "on its own).")
        if not touched:
            left = getattr(exc, "fx_leftovers", None)
            where = ("No file was put in place at the destination" if not left else
                     "No file was put in place at the destination, but these temporary files may remain there: "
                     + ", ".join(left))
            raise FxError(f"Delivery failed ({reason}). {where}. {again}") from exc
        done = ", ".join(s.rsplit("/", 1)[-1] for s in sent) or "none complete"
        raise DeliveryError(
            f"Delivery stopped part way ({reason}). Files already at the vendor: {done}; {touched[-1]} may be "
            f"incomplete, so the vendor may hold a mixed or partial set. {again}", sent,
        ) from exc


def destination_key(d: dict) -> str:
    """Where a feed delivers, as one string. A first delivery to a new destination is held for review."""
    transport = d.get("transport") or "none"
    if transport == "sftp":
        return f"sftp://{d.get('username')}@{d.get('host')}:{int(d.get('port') or 22)}/{_remote_dir(d)}"
    if transport == "local":
        return Path(os.path.expanduser(d.get("directory") or "")).resolve().as_uri()
    return transport


def describe_destination(d: dict) -> str:
    key = destination_key(d)
    return "nowhere (transport none: files stay in the run folder)" if key == "none" else key


def prune_runs(feed_out_dir: Path, keep: int, protect: set[Path] | None = None) -> None:
    """Keep the newest ``keep`` run folders (and three previews) for one feed. 0 keeps everything.
    Folders in ``protect`` (runs awaiting release, the last delivered run) are never removed."""
    if keep <= 0 or not feed_out_dir.is_dir():
        return
    keep_safe = {Path(p).resolve() for p in (protect or set())}
    dirs = [p for p in feed_out_dir.iterdir() if p.is_dir()]
    runs = sorted((p for p in dirs if p.name[:1].isdigit()), key=lambda p: p.name)
    previews = sorted((p for p in dirs if p.name.startswith("preview-")), key=lambda p: p.name)
    for old in [p for p in runs[:-keep] if p.resolve() not in keep_safe] + previews[:-3]:
        shutil.rmtree(old, ignore_errors=True)
        log.info("Removed old run folder %s", old.name)
