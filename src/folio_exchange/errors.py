"""Errors shown to library staff. Every message should say what went wrong and what to do next."""
from __future__ import annotations


class FxError(Exception):
    """Base class. The message is printed to the operator as-is, so keep it plain."""


class ConfigError(FxError):
    """A site, feed or recipe file is missing or invalid."""


class RecipeError(ConfigError):
    """A shared recipe is invalid, or no recipe version matches a feed's `uses`."""


class PatronDataRefused(FxError):
    """Something tried to read a user or patron endpoint without an approved recipe."""


class SafetyCheckFailed(FxError):
    """A check blocked delivery. Files were built but not sent (exit code 2, status "blocked")."""


class HeldForRelease(FxError):
    """A feed's first delivery waits until a person releases it (exit code 2, status "held")."""


class DeliveryError(FxError):
    """A delivery stopped part way. ``sent`` lists what the vendor already has."""

    def __init__(self, message: str, sent: list[str]) -> None:
        super().__init__(message)
        self.sent = sent


class LockBusy(FxError):
    """Another run holds the site lock."""


class DeliveryLoginRefused(FxError):
    """The vendor's server refused the SFTP login. Nothing reached it, and a scheduled run doesn't try again in the
    same slot: another wrong password could lock the account."""


class NewerData(FxError):
    """A data file (the run history or the cache) was written by a newer FOLIO-Exchange than this one: its format
    number (SQLite's user_version) is higher than this version reads."""


class NewerCache(NewerData):
    """The cache was written by a newer FOLIO-Exchange. It is refused as the history is (renaming it makes this
    version download everything again), so two versions on one site never rebuild each other's cache."""


class HistoryDamaged(FxError):
    """The site's run history file is damaged (SQLite says it is malformed or not a database)."""


SQLITE_CORRUPT, SQLITE_NOTADB = 11, 26


def damaged_database(exc: Exception) -> bool:
    """True only when SQLite says the file itself is damaged or is not a database. A full disk, an I/O error, a
    lock or a file that cannot be opened is not damage."""
    code = getattr(exc, "sqlite_errorcode", None)        # Python 3.11+
    if code is not None:
        return code & 0xFF in (SQLITE_CORRUPT, SQLITE_NOTADB)
    text = str(exc).lower()
    return "malformed" in text or "not a database" in text
