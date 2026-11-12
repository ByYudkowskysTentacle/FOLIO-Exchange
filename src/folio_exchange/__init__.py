"""FOLIO-Exchange: scheduled data exchanges between FOLIO and vendor platforms, driven by recipes."""
from __future__ import annotations

__version__ = "0.2.0"


def installed_commit() -> str | None:
    """The git commit this copy was installed from, shortened to 12 characters, when pip recorded one in
    ``direct_url.json`` (an install from ``git+https://...``); None for any other install."""
    import json
    from importlib.metadata import distribution

    try:
        info = json.loads(distribution("folio-exchange").read_text("direct_url.json") or "{}")
        commit = (info.get("vcs_info") or {}).get("commit_id")
    except Exception:  # noqa: BLE001 - the version is enough when the commit cannot be known
        return None
    return str(commit)[:12] if commit else None


def version_text() -> str:
    """``folio-exchange 0.2.0 (commit 4d8082695058)``: what ``--version`` prints and the app and page show."""
    commit = installed_commit()
    return f"folio-exchange {__version__}" + (f" (commit {commit})" if commit else "")
