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
from pathlib import Path

from . import credentials
from .errors import ConfigError, DeliveryError

log = logging.getLogger(__name__)


def sha256_fingerprint(key) -> str:
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


def host_key_fingerprint(host: str, port: int = 22, timeout: float = 30) -> str:
    """Read the server's host key without logging in, for a person to compare with the vendor's."""
    import paramiko

    transport = paramiko.Transport((host, int(port or 22)))
    try:
        transport.start_client(timeout=timeout)
        return sha256_fingerprint(transport.get_remote_server_key())
    finally:
        transport.close()


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
    client = paramiko.SSHClient()
    expected = (d.get("host_key_fingerprint") or "").strip()
    if expected:

        class _Pinned(paramiko.MissingHostKeyPolicy):
            def missing_host_key(self, _client, hostname, key):
                got = sha256_fingerprint(key)
                if got != expected:
                    raise paramiko.SSHException(f"Host key for {hostname} is {got}, expected {expected}")

        client.set_missing_host_key_policy(_Pinned())
    else:
        kh = os.path.expanduser(d.get("known_hosts") or "~/.ssh/known_hosts")
        if os.path.exists(kh):
            client.load_host_keys(kh)
        client.set_missing_host_key_policy(paramiko.RejectPolicy())

    key_path = (d.get("private_key") or "").strip()
    secret = _secret(d)
    if not key_path and not secret:
        raise ConfigError(
            f"No SFTP password found for {d['username']}@{host} "
            f"({credentials.source(d.get('secret'), credentials.SFTP_SERVICE, credentials.sftp_account(d['username'], host))}). "
            "Store it with `folio-exchange secret set <feed>`."
        )
    client.connect(
        hostname=host,
        port=int(d.get("port") or 22),
        username=d["username"],
        password=None if key_path else secret,
        key_filename=os.path.expanduser(key_path) if key_path else None,
        passphrase=secret if key_path else None,
        look_for_keys=False,
        allow_agent=False,
        timeout=60,
    )
    return client


def _remote_dir(d: dict) -> str:
    return (d.get("remote_dir") or ".").rstrip("/") or "."


def _where(d: dict, name: str) -> str:
    return f"sftp://{d['username']}@{d['host']}:{int(d.get('port') or 22)}/{_remote_dir(d)}/{name}"


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
        sftp = client.open_sftp()
        try:
            names: list[str] | None = sorted(sftp.listdir(_remote_dir(d)))
        except PermissionError:
            names = None
        sftp.close()
    finally:
        client.close()
    return names


def _deliver_sftp(files: list[Path], d: dict, uploaded: list[str] | None = None) -> list[str]:
    client = _connect(d)
    uploaded = [] if uploaded is None else uploaded
    try:
        sftp = client.open_sftp()
        remote_dir = _remote_dir(d)
        if (d.get("upload_mode") or "staged") == "direct":
            # Upload-only accounts can't stat or rename. On object-store-backed
            # servers (e.g. AWS Transfer Family) a file only appears once complete.
            for f in files:
                final = f"{remote_dir}/{f.name}"
                log.info("Uploading %s (%s bytes) to %s:%s", f.name, f.stat().st_size, d["host"], final)
                sftp.put(str(f), final, confirm=False)
                uploaded.append(_where(d, f.name))
            sftp.close()
            return uploaded
        # Phase 1: upload everything as .part, so a dropped connection never
        # leaves new bibs next to an old items file (or vice versa).
        staged = []
        for f in files:
            final = f"{remote_dir}/{f.name}"
            tmp = final + ".part"
            log.info("Uploading %s (%s bytes) to %s:%s", f.name, f.stat().st_size, d["host"], tmp)
            sftp.put(str(f), tmp, confirm=True)
            staged.append((tmp, final, f.name))
        # Phase 2: all uploads succeeded; rename them into place.
        for tmp, final, name in staged:
            try:
                sftp.posix_rename(tmp, final)
            except OSError:  # server without posix-rename extension
                try:
                    sftp.remove(final)
                except OSError:
                    pass
                sftp.rename(tmp, final)
            uploaded.append(_where(d, name))
        sftp.close()
    finally:
        client.close()
    return uploaded


def _deliver_local(files: list[Path], d: dict) -> list[str]:
    folder = Path(os.path.expanduser(d.get("directory") or ""))
    if not d.get("directory"):
        raise ConfigError("delivery.directory is not set (needed for transport: local)")
    folder.mkdir(parents=True, exist_ok=True)
    staged = []
    for f in files:  # copy everything first, then move into place, as SFTP staged mode does
        tmp = folder / (f.name + ".part")
        shutil.copyfile(f, tmp)
        staged.append((tmp, folder / f.name))
    for tmp, final in staged:
        os.replace(tmp, final)
        log.info("Copied %s to %s", final.name, folder)
    return [final.resolve().as_uri() for _tmp, final in staged]


def deliver(files: list[Path], delivery: dict, feed_name: str) -> list[str]:
    """Send ``files`` by the feed's transport and return where each went. DeliveryError when it stops part
    way (its ``sent`` lists the files the vendor may already have)."""
    transport = delivery.get("transport")
    if transport == "sftp":
        sent: list[str] = []
        try:
            return _deliver_sftp(files, delivery, sent)
        except ConfigError:
            raise
        except Exception as exc:  # noqa: BLE001 - paramiko and sockets raise many types
            if not sent and (delivery.get("upload_mode") or "staged") != "direct":
                raise
            done = ", ".join(s.rsplit("/", 1)[-1] for s in sent) or "none confirmed"
            raise DeliveryError(
                f"Delivery stopped part way ({type(exc).__name__}: {exc}). Files already at the vendor: {done}. "
                "The vendor may hold a mixed or partial set; send the whole run again with "
                f"`folio-exchange release {feed_name}` once the connection works.", sent,
            ) from exc
    if transport == "local":
        return _deliver_local(files, delivery)
    if transport == "none":
        log.info("Feed %s has transport none; files stay in the run folder", feed_name)
        return []
    raise ConfigError(f"delivery.transport {transport!r} is not supported (use sftp, local or none)")


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
