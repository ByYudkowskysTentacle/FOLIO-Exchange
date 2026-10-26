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


class LockBusy(FxError):
    """Another run holds the site lock."""
