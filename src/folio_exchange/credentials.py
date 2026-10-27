"""Secrets come from the environment or the OS keyring, never from settings files.

(An SFTP private key is a file of its own, named by a feed's ``private_key``; the page keeps one it is given
in the site's ``keys`` folder, readable only by this user. See ``setupops.store_private_key``.)

Files hold a *secret reference* instead of a secret: ``keyring`` (Windows Credential
Manager, macOS Keychain, Secret Service) or ``env:NAME``. Nothing here ever logs,
prints or returns a secret except ``resolve()``, whose value goes straight to a login.
"""
from __future__ import annotations

import logging
import os
import re

from .errors import FxError

log = logging.getLogger(__name__)

FOLIO_SERVICE = "folio-exchange/folio"
SFTP_SERVICE = "folio-exchange/sftp"
SMTP_SERVICE = "folio-exchange/smtp"
WEBHOOK_SERVICE = "folio-exchange/webhook"

# Keys that must never appear in a site, feed or recipe file, whatever their value.
SECRET_KEYS = frozenset({"password", "passphrase", "private_key_passphrase", "token", "api_key", "webhook_url"})

_REF = re.compile(r"^(keyring|env:[A-Za-z_][A-Za-z0-9_]*)$")


def is_secret_ref(value: object) -> bool:
    """True for ``keyring`` or ``env:NAME``."""
    return isinstance(value, str) and bool(_REF.match(value.strip()))


def folio_account(username: str, tenant: str) -> str:
    return f"{username}@{tenant}"


def sftp_account(username: str, host: str) -> str:
    return f"{username}@{host}"


def _keyring():
    try:
        import keyring
    except ImportError:
        return None
    return keyring


def keyring_available() -> bool:
    """True when the keyring package is installed and has a usable backend."""
    kr = _keyring()
    if kr is None:
        return False
    try:
        backend = kr.get_keyring()
    except Exception:  # noqa: BLE001 - any backend failure means "not usable"
        return False
    return getattr(backend, "priority", 1) > 0


def _from_keyring(service: str, account: str | None) -> str | None:
    if not account:
        return None
    kr = _keyring()
    if kr is None:
        return None
    try:
        return kr.get_password(service, account) or None
    except Exception as exc:  # noqa: BLE001 - any backend failure just means "nothing stored"
        log.warning("Could not read %s from the system keyring: %s", service, type(exc).__name__)
        return None


def resolve(ref: str | None, service: str, account: str | None, *, env_first: str | None = None) -> str | None:
    """The secret, or None. Order: ``env_first`` if set, then ``env:NAME``, then the keyring.

    A missing reference means ``keyring``."""
    if env_first and os.environ.get(env_first):
        return os.environ[env_first]
    ref = (ref or "keyring").strip()
    if ref.startswith("env:"):
        return os.environ.get(ref[4:]) or None
    if ref == "keyring":
        return _from_keyring(service, account)
    return None


def source(ref: str | None, service: str, account: str | None, *, env_first: str | None = None) -> str:
    """Where ``resolve()`` finds the secret, for screens and `check`. Never includes the secret."""
    if env_first and os.environ.get(env_first):
        return f"environment ({env_first})"
    ref = (ref or "keyring").strip()
    if ref.startswith("env:"):
        name = ref[4:]
        return f"environment ({name})" if os.environ.get(name) else f"NOT FOUND (environment variable {name} is not set)"
    if ref == "keyring":
        if _from_keyring(service, account):
            return f"system keyring ({service}, {account})"
        if _keyring() is None:
            return "NOT FOUND (the keyring package is not installed)"
        return f"NOT FOUND (nothing stored in the system keyring under {service}, {account})"
    return f"NOT FOUND ({ref!r} is not a secret reference; use keyring or env:NAME)"


def store(service: str, account: str, value: str) -> None:
    """Save a secret in the OS keyring. Raises FxError when there is no usable keyring."""
    if not value:
        raise FxError("Nothing was entered, so nothing was stored.")
    kr = _keyring()
    if kr is None or not keyring_available():
        raise FxError(
            "No system keyring is available on this computer. Set the secret in an environment variable "
            "instead and point the setting at it, e.g. secret: env:MY_PASSWORD."
        )
    try:
        kr.set_password(service, account, value)
    except Exception as exc:  # noqa: BLE001 - report every backend failure the same way
        raise FxError(f"The system keyring refused to store {service} ({type(exc).__name__}).") from None
